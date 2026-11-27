"""Project-scoped owner for source revisions and derived analysis state."""

from __future__ import annotations

import atexit
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import cache
import hashlib
import importlib.util
import json
import logging
import os
import posixpath
from pathlib import Path
import re
import secrets
import socket
import shutil
import sqlite3
import threading
import time
import weakref
import zlib
from collections.abc import Callable, Iterable, Iterator

from emend import cache_codec
from emend.analysis_snapshot import AnalysisSnapshot, ExtractedFile, FileRevision, TypeFact
from emend.errors import BUG_EXCEPTIONS
from emend.file_collection import file_stat_identity
from emend.project_config import find_project_root
from emend.symbol_projection import SymbolInfo, _symbol_info_view
from emend.sqlite_writer import SQLiteWriter


EXTRACTION_ARTIFACT_VERSION = "25"
TYPE_FACTS_ARTIFACT_VERSION = "1"
TYPE_RESULT_VERSION = 5  # FileTypes now carries failure diagnostics.
logger = logging.getLogger(__name__)

# Private fact generations are named ``facts-<role>-h<host>p<pid>-<token>.db``
# so that a later process can tell whether the owner is still running.
_GENERATION_NAME = re.compile(
    r"^facts-[a-z]+-(?:h(?P<host>[0-9a-f]{8})p(?P<pid>\d+)-)?[A-Za-z0-9_]+\.db$"
)
_GENERATION_SIDECARS = ("-journal", "-wal", "-shm")
# Names written before owners were recorded cannot be attributed; they are
# removed once they are old enough that only an abandoned copy remains.
_UNOWNED_GENERATION_AGE = 3600
# A generation from another host or container shares no process table with
# this one, so only age can retire it.
_FOREIGN_GENERATION_AGE = 7 * 24 * 3600
# Files whose extracted facts are held in memory before they are stored.
_EXTRACTION_BATCH_FILES = 32
_LIVE_STORES: "weakref.WeakSet[AnalysisStore]" = weakref.WeakSet()
_LIVE_STORES_LOCK = threading.Lock()


@cache
def _host_tag() -> str:
    """Identify the process table that this process's pid belongs to."""
    identity = socket.gethostname()
    try:
        identity += os.readlink("/proc/self/ns/pid")
    except OSError:
        pass
    return hashlib.sha256(identity.encode()).hexdigest()[:8]


def generation_owner_tag() -> str:
    """Return the owner marker embedded in this process's generation names."""
    return f"h{_host_tag()}p{os.getpid()}"


def _process_is_gone(pid: int) -> bool:
    """Whether *pid* is known to have exited (never true on Windows)."""
    if os.name == "nt":
        # Signal 0 terminates a process there; an open file cannot be removed
        # anyway, so removal itself is the liveness test.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _close_live_stores() -> None:
    """Release every generation this process still owns at interpreter exit."""
    with _LIVE_STORES_LOCK:
        stores = list(_LIVE_STORES)
    for store in stores:
        # A forked child inherits its parent's stores but owns none of their
        # files; only the creating process may remove them.
        if store._owner_pid != os.getpid():
            continue
        try:
            store._release_generations()
        except Exception:
            logger.debug("Could not release fact generations", exc_info=True)


atexit.register(_close_live_stores)


def _canonical_paths(paths: Iterable[str]) -> Iterator[str]:
    """Resolve every path, resolving each directory once.

    Resolving a path asks the file system about every directory level, so
    doing it per file repeats the same questions for a whole directory.
    """
    directories: dict[str, str] = {}
    for path in paths:
        directory, name = os.path.split(os.path.abspath(path))
        resolved = directories.get(directory)
        if resolved is None:
            resolved = directories[directory] = os.path.realpath(directory)
        candidate = os.path.join(resolved, name)
        yield os.path.realpath(candidate) if os.path.islink(candidate) else candidate


def collect_symbol_info(filepath: Path, source: str) -> list[SymbolInfo]:
    """Project exact source into fresh, caller-owned lookup/index results."""
    store = AnalysisStore.existing_for_path(filepath) or AnalysisStore.open(filepath.parent)
    return _symbol_info_view(store.symbols(source, filepath.suffix.lstrip('.') or 'py'), str(filepath))



@dataclass(frozen=True)
class OverlayUpdate:
    """Result of applying an editor overlay revision."""

    file_path: str
    version: int | None
    accepted: bool
    current_version: int | None


@dataclass(frozen=True)
class _DiskScan:
    snapshot: AnalysisSnapshot
    contents: dict[str, str]


class AnalysisStore:
    """The single lifetime and identity owner for one project's analysis."""

    _instances: dict[str, "AnalysisStore"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = find_project_root(project_root)
        self.cache_dir = self.project_root / ".emend" / "cache"
        self.db_path = self.cache_dir / "parse.db"
        self.facts_path = self.cache_dir / "facts.db"
        self._connection: sqlite3.Connection | None = None
        self._artifact_connection: sqlite3.Connection | None = None
        self._reader_connections: set[sqlite3.Connection] = set()
        self._connection_lock = threading.RLock()
        self._schema_initializers = set()
        self._symbols_lock = threading.RLock()
        self._writers: dict[bool, SQLiteWriter] = {}
        self._closing = False
        # Source inventory/overlays and graph publication have independent
        # lifetimes. Never hold the source lock while deriving/materializing.
        self._source_lock = threading.RLock()
        self._graph_lock = threading.RLock()
        self._disk_graph: object | None = None
        self._overlay_graph: object | None = None
        self._overlays: dict[str, tuple[object | None, int, str]] = {}
        # Type bindings are an optional derived view.  Keep each one on its
        # own immutable graph so asking for types never mutates the fast base
        # generation or invalidates readers that still hold it.
        self._typed_graph: tuple[tuple[str, str, str], object] | None = None
        self._type_oracle: tuple[tuple[str, str], object] | None = None
        self._observed_files: dict[
            str, tuple[int, int, int, int, int, str, str, str]
        ] = {}
        self._observed_loaded = False
        self._symbols: dict[tuple[str, str], tuple] = {}
        self._external_type_files = {}
        self._owner_pid = os.getpid()
        with _LIVE_STORES_LOCK:
            _LIVE_STORES.add(self)
        self._sweep_abandoned_generations()

    def symbols(self, source: str, ext: str = "py") -> tuple:
        """Return immutable syntax symbols; content identity never includes a path."""
        from emend import emend_core
        from emend.language_registry import detect_language
        from emend.symbol_projection import (
            project_symbols,
            symbols_from_rows,
            symbols_to_rows,
        )

        key = (ext, hashlib.sha256(source.encode()).hexdigest())
        with self._symbols_lock:
            if key not in self._symbols:
                symbols = None
                try:
                    conn = self.artifact_connection()
                    from emend.language_registry import config_identity
                    config = config_identity(detect_language(f"file.{ext}") or "python")
                    identity = repr(("5", EXTRACTION_ARTIFACT_VERSION, key, config))
                    row = conn.execute(
                        "SELECT payload FROM symbol_projection WHERE identity = ?", (identity,)
                    ).fetchone()
                    if row is not None:
                        try:
                            symbols = symbols_from_rows(cache_codec.decode(row[0]))
                        except (ValueError, TypeError):
                            logger.debug("Unreadable symbol artifact", exc_info=True)
                    if symbols is None:
                        symbols = project_symbols(emend_core.collect_symbols_from_str(source, ext=ext))
                        payload = cache_codec.encode(symbols_to_rows(symbols))
                        self.write(lambda db: db.execute(
                            "INSERT OR REPLACE INTO symbol_projection VALUES (?, ?)",
                            (identity, payload),
                        ).close(), artifacts=True)
                except (OSError, sqlite3.Error):
                    logger.debug("Symbol artifact cache unavailable", exc_info=True)
                if symbols is None:
                    symbols = project_symbols(emend_core.collect_symbols_from_str(source, ext=ext))
                if len(self._symbols) >= 256:
                    del self._symbols[next(iter(self._symbols))]
                self._symbols[key] = symbols
            return self._symbols[key]

    @classmethod
    def open(cls, project_root: str | Path = ".") -> "AnalysisStore":
        root = str(find_project_root(project_root))
        with cls._instances_lock:
            store = cls._instances.get(root)
            if store is None:
                store = cls(root)
                cls._instances[root] = store
            return store

    @classmethod
    def existing_for_path(cls, path: str | Path) -> "AnalysisStore" | None:
        """Return the most specific open owner containing *path*, if any."""
        resolved = Path(path).resolve()
        with cls._instances_lock:
            candidates = [
                store for store in cls._instances.values()
                if resolved == store.project_root
                or store.project_root in resolved.parents
            ]
        return max(
            candidates,
            key=lambda store: len(store.project_root.parts),
            default=None,
        )

    def connection(
        self,
        schema_initializer: Callable[[sqlite3.Connection], None] | None = None,
    ) -> sqlite3.Connection:
        """Return the shared read-only connection; queue mutations with write()."""
        with self._connection_lock:
            if self._connection is None:
                self.ensure_cache_directory()
                self._connection = self._read_connection(self.db_path)
            if schema_initializer is not None and schema_initializer not in self._schema_initializers:
                self.write(schema_initializer)
                self._schema_initializers.add(schema_initializer)
            return self._connection

    @staticmethod
    def _read_connection(path):
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA query_only=ON")
        return conn

    def reader(self, *, artifacts=False) -> sqlite3.Connection:
        """Return an independent read handle tracked for store shutdown."""
        with self._connection_lock:
            if self._closing:
                raise RuntimeError("Analysis store is closing")
            self.ensure_cache_directory()
            conn = self._read_connection(
                self.artifact_path if artifacts else self.db_path
            )
            self._reader_connections.add(conn)
            return conn

    def close_reader(self, conn: sqlite3.Connection) -> None:
        """Release a read handle previously returned by reader()."""
        with self._connection_lock:
            self._reader_connections.discard(conn)
        conn.close()

    def submit_write(self, transaction, *, artifacts=False):
        """Queue a complete transaction; callers must observe its Future."""
        with self._connection_lock:
            if self._closing:
                raise RuntimeError("Analysis store is closing")
            writer = self._writers.get(artifacts)
            if writer is None:
                self.ensure_cache_directory()
                writer = self._writers[artifacts] = SQLiteWriter(
                    self.artifact_path if artifacts else self.db_path
                )
        return writer.submit(transaction)

    def write(self, transaction, *, artifacts=False):
        """Queue a transaction and wait for its commit (or propagate failure)."""
        return self.submit_write(transaction, artifacts=artifacts).result()

    @staticmethod
    def _prepare_cache_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for name, heading in (
            (".gitignore", "# Auto-generated by emend index\n*\n"),
            (".dockerignore", "# Auto-generated by emend index\n*\n"),
        ):
            marker = path / name
            if not marker.exists():
                marker.write_text(heading)

    def ensure_cache_directory(self) -> None:
        """Create the owner directory and keep generated data out of VCS."""
        self._prepare_cache_directory(self.cache_dir)

    def _generation_name(self, prefix: str) -> Path:
        """Return an unused, owner-tagged database name in the owner cache."""
        self.ensure_cache_directory()
        return self.cache_dir / (
            f"{prefix}{generation_owner_tag()}-{secrets.token_hex(6)}.db"
        )

    def _temporary_db_path(self, prefix: str) -> Path:
        """Allocate an empty private database in the owner cache."""
        while True:
            path = self._generation_name(prefix)
            try:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                continue
            return path

    def _sweep_abandoned_generations(self) -> None:
        """Remove private generations whose owning process no longer exists.

        A process that is killed cannot remove its own generations, and each
        one can be as large as the published database.
        """
        try:
            with os.scandir(self.cache_dir) as entries:
                names = [entry.name for entry in entries]
        except OSError:
            return
        now = time.time()
        for name in names:
            base = name
            for suffix in _GENERATION_SIDECARS:
                if name.endswith(suffix):
                    base = name[: -len(suffix)]
            match = _GENERATION_NAME.match(base)
            if match is None:
                continue
            path = self.cache_dir / name
            try:
                stat = path.stat()
            except OSError:
                continue
            # Unowned names are always copies, so their write time is their
            # age.  An owned name can be a link to an old generation; linking
            # refreshes only the change time.
            written = now - stat.st_mtime
            age = now - max(stat.st_mtime, stat.st_ctime)
            if match["pid"] is None:
                abandoned = written > _UNOWNED_GENERATION_AGE
            elif match["host"] != _host_tag():
                abandoned = age > _FOREIGN_GENERATION_AGE
            elif os.name == "nt":
                # Give a live owner time to open what it has just created.
                abandoned = int(match["pid"]) != os.getpid() and age > 60
            else:
                abandoned = (
                    int(match["pid"]) != os.getpid()
                    and _process_is_gone(int(match["pid"]))
                )
            if abandoned:
                self._unlink(path)

    @staticmethod
    def _unlink(path: Path | None) -> None:
        """Best-effort removal for an obsolete private generation."""
        if path is not None:
            for candidate in (path, *(
                path.with_name(path.name + suffix) for suffix in _GENERATION_SIDECARS
            )):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _graph_path(graph: object | None) -> Path | None:
        path = getattr(graph, "_db_path", None)
        return Path(path) if path is not None else None

    _stat_identity = staticmethod(file_stat_identity)

    def _release_generations(self) -> None:
        """Close every owned graph and remove its private backing file."""
        self._discard_overlay_graph(close=True)
        if self._typed_graph is not None:
            typed_graph = self._typed_graph[1]
            typed_path = self._graph_path(typed_graph)
            typed_graph.close()
            self._unlink(typed_path)
            self._typed_graph = None
        if self._disk_graph is not None:
            disk_path = self._graph_path(self._disk_graph)
            self._disk_graph.close()
            self._disk_graph = None
            self._unlink(disk_path)

    def close(self) -> None:
        with self._connection_lock:
            self._closing = True
            writers = list(self._writers.values())
            self._writers.clear()
        for writer in writers:
            writer.close()
        self._overlays.clear()
        if self._type_oracle is not None:
            self._type_oracle[1].close()
            self._type_oracle = None
        self._release_generations()
        with self._connection_lock:
            self._symbols.clear()
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                self._schema_initializers.clear()
            if self._artifact_connection is not None:
                self._artifact_connection.close()
                self._artifact_connection = None
            for conn in self._reader_connections:
                conn.close()
            self._reader_connections.clear()
            self._closing = False

    def _extraction_context_id(self, revisions: Iterable[FileRevision]) -> str:
        """Return the schema/config identity governing extracted facts."""
        from emend.fact_graph import FACT_GRAPH_SCHEMA_VERSION
        from emend.analysis_linking import LINKER_VERSION

        configurations = {
            (revision.language, revision.analysis_config.identity)
            for revision in revisions
            if revision.analysis_config is not None
        }
        payload = (
            FACT_GRAPH_SCHEMA_VERSION,
            LINKER_VERSION,
            EXTRACTION_ARTIFACT_VERSION,
            tuple(sorted(configurations)),
        )
        return hashlib.sha256(repr(payload).encode()).hexdigest()

    def _snapshot_id(
        self, revisions: Iterable[FileRevision], context_id: str
    ) -> str:
        ordered = sorted(revisions, key=lambda item: item.file_path)
        payload = (
            context_id,
            tuple(
                (item.file_path, item.content_hash, item.language, item.module_name,
                 item.origin, item.version)
                for item in ordered
            ),
        )
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":")).encode()
        ).hexdigest()

    def _make_snapshot(
        self,
        revisions: Iterable[FileRevision],
        *,
        base_snapshot_id: str | None = None,
    ) -> AnalysisSnapshot:
        ordered = tuple(sorted(revisions, key=lambda item: item.file_path))
        context_id = self._extraction_context_id(ordered)
        return AnalysisSnapshot(
            project_root=str(self.project_root),
            snapshot_id=self._snapshot_id(ordered, context_id),
            files=ordered,
            base_snapshot_id=base_snapshot_id,
            analysis_context_id=context_id,
        )

    def _module_name(
        self, file_path: str, language: str, module_separator: str | None = None
    ) -> str:
        """Return the canonical module identity used by selectors and facts."""
        from emend.project_config import module_name_for_file

        return module_name_for_file(
            file_path,
            self.project_root,
            language=language,
            module_separator=module_separator,
        )

    def _load_observed_files(self) -> None:
        """Load the durable stat-to-content identities once per process."""
        if self._observed_loaded:
            return
        conn = self.connection()
        self.write(lambda db: db.execute(
            "CREATE TABLE IF NOT EXISTS analysis_file_revision ("
            "path TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL, "
            "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, "
            "content_hash TEXT NOT NULL, language TEXT NOT NULL, module_name TEXT NOT NULL)"
        ).close())
        self._observed_files = {
            row[0]: tuple(row[1:])
            for row in conn.execute(
                "SELECT path, device, inode, size, mtime_ns, ctime_ns, content_hash, "
                "language, module_name FROM analysis_file_revision"
            )
        }
        self._observed_loaded = True

    def _save_observed_files(
        self,
        previous: dict[str, tuple[int, int, int, int, int, str, str, str]],
        observed: dict[str, tuple[int, int, int, int, int, str, str, str]],
    ) -> None:
        """Persist only changed identities for fast, correct future processes."""
        changed = [
            (path, *identity)
            for path, identity in observed.items()
            if previous.get(path) != identity
        ]
        deleted = set(previous) - set(observed)
        def persist(conn):
            conn.executemany(
                "INSERT OR REPLACE INTO analysis_file_revision "
                "(path, device, inode, size, mtime_ns, ctime_ns, content_hash, "
                "language, module_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                changed,
            )
            conn.executemany(
                "DELETE FROM analysis_file_revision WHERE path = ?",
                ((path,) for path in deleted),
            )
        if changed or deleted:
            self.write(persist)

    def _scan_disk(self) -> _DiskScan:
        """Read the current source inventory, hashing only stat changes."""
        from emend.project_config import module_context_snapshot

        with self._source_lock, module_context_snapshot():
            return self._scan_disk_locked()

    def _scan_disk_locked(self) -> _DiskScan:
        from emend.file_collection import collect_all_source_files
        from emend.language_registry import (
            detect_language,
            get_module_separator,
            registry_and_config_snapshots,
        )
        self._load_observed_files()
        previous = self._observed_files
        registry, configs = registry_and_config_snapshots(self.project_root)
        module_separators = {
            language: get_module_separator(language, configs.get(language))
            for language in registry[1]
        }
        from emend.project_config import module_names_under_root, module_resolution_context

        files = sorted(_canonical_paths(collect_all_source_files(
            str(self.project_root), languages=list(registry[1]), registry=registry
        )))
        # Every path above is canonical and so is the project root; resolving
        # them again per file costs a system call per directory level.
        root = self.project_root.resolve()
        root_text = str(root)
        module_namers: dict[str, Callable[[str], str]] = {}
        contents: dict[str, str] = {}
        revisions: list[FileRevision] = []
        observed: dict[str, tuple[int, int, int, int, int, str, str, str]] = {}
        for file_path in files:
            content: str | None = None
            try:
                stat = os.stat(file_path)
            except OSError:
                continue
            old = previous.get(file_path)
            language = detect_language(file_path, registry=registry) or "python"
            module_name_of = module_namers.get(language)
            if module_name_of is None:
                module_name_of = module_namers[language] = module_names_under_root(
                    root, module_resolution_context(root, language)[0], language,
                    module_separators.get(language) or get_module_separator(language),
                )
            module_name = module_name_of(file_path)
            identity = self._stat_identity(stat)
            if old is not None and old[:5] == identity:
                content_hash = old[5]
            else:
                # Verify the identity around the read so a concurrent editor
                # save cannot bind bytes to the wrong revision.
                for _attempt in range(3):
                    try:
                        before = os.stat(file_path)
                        content = Path(file_path).read_text(encoding="utf-8")
                        after = os.stat(file_path)
                    except (OSError, UnicodeDecodeError):
                        # The file may disappear between inventory and the
                        # snapshot boundary.  Do not publish bytes read from
                        # a revision whose verification failed.
                        content = None
                        break
                    before_id = self._stat_identity(before)
                    after_id = self._stat_identity(after)
                    if before_id == after_id:
                        identity = after_id
                        break
                else:
                    raise RuntimeError(f"source changed repeatedly while reading: {file_path}")
                if content is None:
                    continue
                contents[file_path] = content
                content_hash = hashlib.sha256(content.encode()).hexdigest()
            revisions.append(FileRevision(
                root_text, file_path, content_hash, language, module_name,
                analysis_config=configs.get(language),
            ))
            observed[file_path] = (*identity, content_hash, language, module_name)
        self._save_observed_files(previous, observed)
        self._observed_files = observed
        return _DiskScan(
            snapshot=self._make_snapshot(revisions),
            contents=contents,
        )

    def _shared_artifact_path(self) -> Path:
        """Return the checkout-family cache shared by linked worktrees."""
        root = self.project_root
        git_path = next(
            (candidate / ".git" for candidate in (root, *root.parents)
             if (candidate / ".git").is_file()
             or ((candidate / ".git") / "HEAD").is_file()),
            None,
        )
        if git_path is not None and git_path.is_dir():
            root = git_path.parent
        elif git_path is not None:
            try:
                text_value = git_path.read_text().strip()
                if text_value.startswith("gitdir:"):
                    git_dir = Path(text_value.split(":", 1)[1].strip())
                    if not git_dir.is_absolute():
                        git_dir = (git_path.parent / git_dir).resolve()
                    common_file = git_dir / "commondir"
                    if common_file.is_file():
                        common = (git_dir / common_file.read_text().strip()).resolve()
                        root = common.parent
            except OSError:
                pass
        path = root / ".emend" / "cache" / "analysis-artifacts.db"
        self._prepare_cache_directory(path.parent)
        return path

    @property
    def artifact_path(self) -> Path:
        """Content-addressed cache shared by linked worktrees."""
        return self._shared_artifact_path()

    def artifact_connection(self) -> sqlite3.Connection:
        """Return an owner-held connection to the shared artifact database."""
        with self._connection_lock:
            if self._artifact_connection is None:
                def initialize(db):
                    db.execute("CREATE TABLE IF NOT EXISTS symbol_projection "
                               "(identity TEXT PRIMARY KEY, payload BLOB NOT NULL)")
                    db.execute("CREATE TABLE IF NOT EXISTS dependency_import_artifact "
                               "(identity TEXT PRIMARY KEY, payload BLOB NOT NULL)")
                    db.execute("CREATE TABLE IF NOT EXISTS source_artifact "
                               "(content_hash TEXT PRIMARY KEY, payload BLOB NOT NULL)")
                self.write(initialize, artifacts=True)
                self._artifact_connection = self._read_connection(self.artifact_path)
            return self._artifact_connection

    @staticmethod
    def _retire_extraction_cache(conn: sqlite3.Connection) -> None:
        """Drop the per-file fact cache written by earlier versions.

        It duplicated the fact database, cost more to write than extraction
        saves, and was the largest table in the shared artifact database.
        """
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'extracted_file_artifact'"
        ).fetchone() is None:
            return
        conn.execute("DROP TABLE IF EXISTS extracted_file_artifact")
        conn.commit()
        try:
            conn.execute("VACUUM")
        except sqlite3.Error:
            # Another process is reading; the freed pages are reused instead.
            logger.debug("Could not compact the artifact database", exc_info=True)

    def _stored_path(self, revision: FileRevision) -> str:
        try:
            return str(Path(revision.file_path).relative_to(self.project_root))
        except ValueError:
            return revision.file_path

    def _import_artifact_key(self, revision: FileRevision) -> str:
        """Identify the import list of one exact revision under one config."""
        from emend.fact_graph import FACT_GRAPH_SCHEMA_VERSION

        if revision.analysis_config is None:
            raise RuntimeError(
                f"snapshot lacks language config for {revision.file_path}"
            )
        return hashlib.sha256(repr((
            "imports", FACT_GRAPH_SCHEMA_VERSION, EXTRACTION_ARTIFACT_VERSION,
            revision.language, revision.module_name, self._stored_path(revision),
            revision.content_hash, revision.analysis_config.identity,
        )).encode()).hexdigest()

    @contextmanager
    def _artifact_writer(self):
        """Open a short-lived writer on the shared artifact database."""
        conn = sqlite3.connect(str(self._shared_artifact_path()), timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Rebuildable artifacts need atomic commits, not a disk sync per file.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS source_artifact ("
                "content_hash TEXT PRIMARY KEY, payload BLOB NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS revision_import_artifact ("
                "artifact_key TEXT PRIMARY KEY, payload BLOB NOT NULL)"
            )
            self._retire_extraction_cache(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _file_imports(extracted: ExtractedFile) -> list[tuple[str, str | None]]:
        return [(row[1], row[2] or None) for row in extracted.rows.get("imports", ())]

    def _cached_imports(
        self, revisions: Iterable[FileRevision],
    ) -> dict[str, list[tuple[str, str | None]]]:
        """Return the remembered import list of each revision that has one.

        Dependency identities need only a file's imports.  Remembering those
        few names lets type inference and warm indexing skip unchanged files
        without keeping a second copy of every fact.
        """
        keys = {self._import_artifact_key(revision): revision.file_path
                for revision in revisions}
        if not keys or not self.artifact_path.is_file():
            return {}
        found: dict[str, list[tuple[str, str | None]]] = {}
        try:
            with closing(sqlite3.connect(
                f"{self.artifact_path.as_uri()}?mode=ro", uri=True, timeout=30,
            )) as conn:
                wanted = list(keys)
                for offset in range(0, len(wanted), 900):
                    batch = wanted[offset:offset + 900]
                    for key, payload in conn.execute(
                        "SELECT artifact_key, payload FROM revision_import_artifact "
                        "WHERE artifact_key IN (" + ",".join("?" for _ in batch) + ")",
                        batch,
                    ):
                        try:
                            found[keys[key]] = [
                                (str(module), None if name is None else str(name))
                                for module, name in cache_codec.decode(payload)
                            ]
                        except (ValueError, TypeError):
                            logger.debug("Unreadable import artifact", exc_info=True)
        except sqlite3.Error:
            logger.debug("Import artifacts unavailable", exc_info=True)
        return found

    def _revision_imports(
        self, revisions: Iterable[FileRevision], contents: dict[str, str],
    ) -> dict[str, list[tuple[str, str | None]]]:
        """Return every revision's imports, extracting only unseen revisions."""
        revisions = list(revisions)
        imports = self._cached_imports(revisions)
        missing = [revision for revision in revisions if revision.file_path not in imports]
        for extracted in self._extract_revisions(missing, contents):
            imports[extracted.revision.file_path] = self._file_imports(extracted)
        return imports

    def _extract_revisions(
        self,
        revisions: Iterable[FileRevision],
        contents: dict[str, str],
        *, prepare=None, prepared=None, started=None, jobs=None,
    ) -> Iterable[ExtractedFile]:
        """Extract each revision's facts and retain its exact source text.

        Facts are not persisted here: the fact database is their only store.
        Two small things are kept.  Source text lets a reader of an older
        snapshot and type inference load the exact bytes a revision describes,
        and each revision's import list lets dependency identities skip it.
        """
        for _revision, extracted, _imports in self._prepare_revisions(
            revisions, contents, prepare=prepare, prepared=prepared,
            started=started, jobs=jobs,
        ):
            yield extracted

    def _extract_batches(
        self, revisions: Iterable[FileRevision], contents: dict[str, str],
    ) -> Iterable[list[ExtractedFile]]:
        """Extract revisions a few files at a time.

        A whole project's facts are several times larger in memory than in the
        database, so a writer stores each batch before the next is parsed.
        """
        from itertools import islice

        extracted = iter(self._extract_revisions(revisions, contents))
        while batch := list(islice(extracted, _EXTRACTION_BATCH_FILES)):
            yield batch

    def _prepare_revisions(
        self,
        revisions: Iterable[FileRevision],
        contents: dict[str, str],
        *, prepare=None, prepared=None, started=None, jobs=None, needs_facts=None,
    ):
        """Yield ``(revision, extracted, imports)`` for every revision.

        *needs_facts* names the revisions whose facts a consumer will store.
        Any other revision whose import list is already remembered is not
        parsed: its ``extracted`` is ``None``.  ``prepare(revision, content,
        extracted)`` still runs for every revision, on the worker that handles
        it and after that revision's parse, if it had one.
        """
        from emend.analysis_extraction import _extract_file_facts

        revisions = list(revisions)
        known_imports = (
            {} if needs_facts is None else self._cached_imports(
                revision for revision in revisions if not needs_facts(revision)
            )
        )
        pending = []
        with self._artifact_writer() as conn:
            for revision in revisions:
                if revision.analysis_config is None:
                    raise RuntimeError(
                        f"snapshot lacks language config for {revision.file_path}"
                    )
                source_row = conn.execute(
                    "SELECT payload FROM source_artifact WHERE content_hash = ?",
                    (revision.content_hash,),
                ).fetchone()
                content = contents.get(revision.file_path)
                if source_row is None:
                    if content is None:
                        content = Path(revision.file_path).read_text(encoding="utf-8")
                    if hashlib.sha256(content.encode()).hexdigest() != revision.content_hash:
                        raise RuntimeError(
                            f"source changed after inventory: {revision.file_path}"
                        )
                    conn.execute(
                        "INSERT OR IGNORE INTO source_artifact "
                        "(content_hash, payload) VALUES (?, ?)",
                        (revision.content_hash, zlib.compress(content.encode())),
                    )
                elif content is None:
                    content = zlib.decompress(source_row[0]).decode()
                pending.append((revision, self._stored_path(revision), content))
            # Source blobs are published before parsing; type inference and
            # other worktrees must be able to write their own artifacts.

        def handle(item):
            revision, stored_path, content = item
            imports = known_imports.get(revision.file_path)
            extracted = None
            if imports is None:
                extracted = _extract_file_facts(revision, stored_path, content)
                imports = self._file_imports(extracted)
            preparation = (
                prepare(revision, content, extracted) if prepare is not None else None
            )
            return revision, extracted, imports, preparation

        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
        from itertools import islice

        learned: list[tuple[str, bytes]] = []

        def remember() -> None:
            if learned:
                with self._artifact_writer() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO revision_import_artifact "
                        "(artifact_key, payload) VALUES (?, ?)", learned,
                    )
                learned.clear()

        try:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                items = iter(pending)

                def submit(item):
                    if started is not None:
                        started(item[0])
                    return pool.submit(handle, item)

                futures = {submit(item) for item in islice(items, jobs or 8)}
                while futures:
                    done, futures = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        revision, extracted, imports, preparation = future.result()
                        if prepared is not None:
                            prepared(revision, preparation)
                        if extracted is not None:
                            learned.append((
                                self._import_artifact_key(revision),
                                cache_codec.encode(imports),
                            ))
                            if len(learned) >= 512:
                                remember()
                        yield revision, extracted, imports
                    futures.update(submit(item) for item in islice(items, len(done)))
        finally:
            remember()

    def _revision_source(self, revision: FileRevision) -> str | None:
        """Load exact source bytes lazily for a reader of an older snapshot."""
        if not self.artifact_path.is_file():
            return None
        try:
            with closing(sqlite3.connect(self.artifact_path)) as conn:
                row = conn.execute(
                    "SELECT payload FROM source_artifact WHERE content_hash = ?",
                    (revision.content_hash,),
                ).fetchone()
            return zlib.decompress(row[0]).decode() if row is not None else None
        except sqlite3.Error:
            return None

    @contextmanager
    def _facts_write_lock(self):
        """Serialize snapshot publication by processes in this worktree."""
        self.ensure_cache_directory()
        lock = sqlite3.connect(
            self.cache_dir / "facts.lock", timeout=120, isolation_level=None
        )
        try:
            lock.execute("BEGIN IMMEDIATE")
            yield
        finally:
            if lock.in_transaction:
                lock.rollback()
            lock.close()

    @staticmethod
    def _backup_sqlite(source_path: Path, destination_path: Path) -> None:
        with (
            closing(sqlite3.connect(source_path)) as source,
            closing(sqlite3.connect(destination_path)) as destination,
        ):
            source.backup(destination)

    @classmethod
    def _copy_sqlite(cls, source_path: Path, destination_path: Path) -> None:
        """Copy a coherent SQLite generation, including uncheckpointed WAL.

        A generation is never written in place once it can be seen, so with
        no log beside it the file is the whole database.  Copying the file is
        several times faster than SQLite's page-by-page backup, and a file
        system that can share blocks copies nothing at all.
        """
        logs = (Path(f"{source_path}{suffix}") for suffix in ("-wal", "-journal"))
        if any(log.exists() and log.stat().st_size for log in logs):
            cls._backup_sqlite(source_path, destination_path)
        else:
            shutil.copyfile(source_path, destination_path)

    def _compact_generation(self, path: Path) -> None:
        """Rewrite a bulk-saved generation with full pages.

        The save leaves about a tenth of every page empty.  Rewriting costs
        a second on a 400 MB database, once per full build; every later copy
        and read of the generation is that much smaller.
        """
        compact = self._temporary_db_path("facts-next-")
        try:
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("VACUUM INTO ?", (str(compact),))
            os.replace(compact, path)
        except BaseException:
            self._unlink(compact)
            raise

    def _snapshot_copy(self, source_path: Path, prefix: str) -> Path:
        """Create a stable private copy of one SQLite generation."""
        path = self._temporary_db_path(prefix)
        try:
            self._copy_sqlite(source_path, path)
        except BaseException:
            self._unlink(path)
            raise
        return path

    def _link_generation(self, source_path: Path, prefix: str) -> Path | None:
        """Give one generation a second name without copying its contents.

        The engine reopens a database by name, so a reader needs a name that
        publication never replaces.  Returns ``None`` where another name cannot
        serve: an open file cannot be replaced on Windows, and some filesystems
        have no hard links.
        """
        if os.name == "nt":
            return None
        for _ in range(8):
            path = self._generation_name(prefix)
            try:
                os.link(source_path, path)
            except FileExistsError:
                continue
            except OSError:
                return None
            return path
        return None

    def _privatize_generation(self, current_path: str) -> str:
        """Copy a shared generation before one of its graphs is written."""
        current = Path(current_path)
        private = self._snapshot_copy(current, "facts-private-")
        if current != self.facts_path:
            self._unlink(current)
        return str(private)

    def _open_published(self, source_path: Path):
        """Open the published generation under a name only this process uses."""
        from emend.fact_graph import FactGraph

        path = self._link_generation(source_path, "facts-open-")
        shared = path is not None
        if path is None:
            path = self._snapshot_copy(source_path, "facts-open-")
        try:
            # The engine aborts on a file that is not a database and would
            # initialize an empty one in place; reject both before it opens.
            # A table from before keys were stored once is rebuilt, since
            # copying it forward would keep its doubled size forever.
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
                row = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cozo'"
                ).fetchone()
                if row is None or "WITHOUT ROWID" not in str(row[0]).upper():
                    raise sqlite3.DatabaseError(
                        f"no current fact storage in {source_path}"
                    )
            graph = FactGraph(db_path=str(path), initialize_schema=False)
        except BaseException:
            self._unlink(path)
            raise
        if shared:
            graph.mark_shared(self._privatize_generation)
        return graph, path

    def _publish_generation(self, graph, path: Path) -> None:
        """Atomically make a finished private generation the published one."""
        publish_path = self._link_generation(path, "facts-publish-")
        shared = publish_path is not None
        if publish_path is None:
            publish_path = self._snapshot_copy(path, "facts-publish-")
        try:
            os.replace(publish_path, self.facts_path)
        finally:
            self._unlink(publish_path)
        if shared:
            # The published name now refers to this graph's own file.
            graph.mark_shared(self._privatize_generation)

    def _set_disk_graph(self, graph: object) -> None:
        previous_path = self._graph_path(self._disk_graph)
        self._disk_graph = graph
        if previous_path != self._graph_path(graph):
            self._unlink(previous_path)

    def _updated_graph(self, previous, snapshot: AnalysisSnapshot, contents, extract=None):
        """Apply one revision delta, equally for disk and overlay generations."""
        from emend.analysis_linking import ModuleCatalog, link_extracted_files
        from emend.fact_graph import FactGraph, prepare_cozo_storage

        def module_inventory(revisions):
            return {(r.file_path, r.language, r.module_name) for r in revisions}

        # Import targets depend on the module inventory (for example TS index
        # aliases), so a changed inventory relinks every file, as a changed
        # analysis context does.  Nothing of the previous generation survives
        # that: build a new one, which costs a fraction of deleting and
        # re-inserting every row of a copy.  Ordinary body edits still replace
        # only the edited file.
        if previous is not None and (
            previous.snapshot.analysis_context_id != snapshot.analysis_context_id
            or module_inventory(previous.snapshot.files) != module_inventory(snapshot.files)
        ):
            previous = None
        if previous is None:
            path = self._temporary_db_path("facts-next-")
            graph = FactGraph()
            before = {}
        else:
            graph, path = self._clone_graph(Path(previous._db_path), snapshot)
            before = {revision.file_path: revision for revision in previous.snapshot.files}
        graph.bind_snapshot(snapshot)
        after = {revision.file_path: revision for revision in snapshot.files}
        changed = [revision for file_path, revision in after.items()
                   if before.get(file_path) != revision]
        removed = before.keys() - after.keys()
        try:
            graph.clear_snapshot_marker()
            catalog = ModuleCatalog.from_revisions(snapshot.files)
            batches = (extract or self._extract_batches)(changed, contents)
            stored_paths = [graph.stored_path(file_path)
                            for file_path in [*(r.file_path for r in changed), *removed]]
            # This private generation is invisible until publication. Stream
            # file transactions into it; any failure discards the generation.
            graph.replace_extracted([], stored_paths=stored_paths)
            for batch in batches:
                graph.replace_extracted(link_extracted_files(batch, catalog), stored_paths=[])
            graph.publish_snapshot(snapshot)
            if previous is None:
                # Bulk-save the cold build once; subsequent edits keep using
                # disk-backed deltas rather than loading the graph into RAM.
                prepare_cozo_storage(path)
                graph._client.backup(str(path))
                graph.close()
                self._compact_generation(path)
                graph = FactGraph(db_path=str(path))
            graph.bind_snapshot(
                snapshot,
                source_overrides={r.file_path: contents[r.file_path]
                                  for r in snapshot.files if r.origin == "overlay"},
                source_loader=self._revision_source,
            )
            return graph, path
        except BaseException:
            graph.close()
            self._unlink(path)
            raise

    def _adopt_published_facts(self):
        """Bind the published generation, if this store has none and it is valid.

        Publication is an atomic rename of a finished generation, so this
        needs no lock: whatever generation is found is complete.
        """
        graph = self._disk_graph
        if graph is not None and not self._generation_exists(graph):
            # Another process retired this generation's name; reopen.
            graph.close()
            self._disk_graph = graph = None
        if graph is None and self.facts_path.is_file():
            path = candidate = None
            try:
                candidate, path = self._open_published(self.facts_path)
                published = candidate.published_snapshot(self.project_root)
                if published is not None:
                    candidate.bind_snapshot(published, source_loader=self._revision_source)
                    self._set_disk_graph(candidate)
                    graph = candidate
            except (OSError, RuntimeError, sqlite3.Error):
                logger.debug("Could not reuse published facts", exc_info=True)
            finally:
                if graph is not candidate or graph is None:
                    if candidate is not None:
                        candidate.close()
                    self._unlink(path)
        return graph

    def _ensure_disk_facts(self, scan: _DiskScan, extract=None):
        graph = self._adopt_published_facts()
        if graph is not None and graph.snapshot.snapshot_id == scan.snapshot.snapshot_id:
            if extract is not None:
                for _ in extract((), {}):
                    pass
            graph.bind_snapshot(scan.snapshot, source_loader=self._revision_source)
            return graph
        candidate, path = self._updated_graph(graph, scan.snapshot, scan.contents, extract)
        try:
            self._publish_generation(candidate, path)
        except BaseException:
            candidate.close()
            self._unlink(path)
            raise
        self._set_disk_graph(candidate)
        return candidate

    def _generation_exists(self, graph: object) -> bool:
        path = self._graph_path(graph)
        return path is None or path.exists()

    def _discard_overlay_graph(self, *, close: bool = False) -> None:
        graph = self._overlay_graph
        path = self._graph_path(graph)
        self._overlay_graph = None
        if close and graph is not None:
            graph.close()
        self._unlink(path)

    def _clone_graph(self, source_path: Path, snapshot: AnalysisSnapshot):
        """Copy a stable SQLite snapshot without serializing every fact."""
        from emend.fact_graph import FactGraph

        path = self._snapshot_copy(source_path, "facts-overlay-")
        try:
            graph = FactGraph(db_path=str(path))
            graph.bind_snapshot(snapshot, source_loader=self._revision_source)
        except BaseException:
            self._unlink(path)
            raise
        return graph, path

    def detached_facts(
        self,
        graph=None,
        *,
        db_path: str | None = None,
        file_paths: Iterable[str | Path] | None = None,
    ):
        """Return an independently closable copy of an owned fact view.

        Compatibility APIs historically returned a graph whose lifecycle was
        owned by the caller. Keep that boundary without making those APIs
        collect or infer facts themselves: copy the owner's coherent SQLite
        generation and give the caller the resulting graph.
        """
        from emend.fact_graph import FactGraph

        with self._graph_lock:
            graph = graph or self.query_facts()
            source_path = getattr(graph, "_db_path", None)
            if source_path is None:
                # Owned project generations are always backed by SQLite. This
                # fallback is useful for injected in-memory test graphs.
                clone = FactGraph.from_json(graph.to_json())
                clone.bind_snapshot(graph.snapshot)
                return clone

            source = Path(source_path).resolve()
            requested = Path(db_path).resolve() if db_path is not None else None
            owns_path = requested is None
            if requested is None:
                target = self._temporary_db_path("facts-detached-")
            else:
                target = requested
                target.parent.mkdir(parents=True, exist_ok=True)

            owned_paths = {
                path.resolve()
                for path in (
                    self.facts_path,
                    self._graph_path(self._disk_graph),
                    self._graph_path(self._overlay_graph),
                    self._graph_path(self._typed_graph[1])
                    if self._typed_graph is not None else None,
                )
                if path is not None
            }
            # Never let a legacy caller mutate an owner-managed generation.
            if target == source or target in owned_paths:
                target = self._temporary_db_path("facts-detached-")
                owns_path = True
            try:
                self._copy_sqlite(source, target)
                detached = FactGraph(db_path=str(target))
                revisions = graph.snapshot.files
                if file_paths is not None:
                    allowed = {str(Path(path).resolve()) for path in file_paths}
                    revisions = tuple(
                        revision for revision in revisions
                        if str(Path(revision.file_path).resolve()) in allowed
                    )
                    omitted = [
                        graph.stored_path(revision.file_path)
                        for revision in graph.snapshot.files
                        if revision not in revisions
                    ]
                    detached.remove_files(omitted)
                snapshot = self._make_snapshot(
                    revisions, base_snapshot_id=graph.snapshot.base_snapshot_id
                )
                detached.clear_snapshot_marker()
                detached.publish_snapshot(snapshot)
                detached.bind_snapshot(
                    snapshot,
                    source_overrides=getattr(graph, "_source_overrides", None),
                    source_loader=self._revision_source,
                )
                detached._close_unlinks_db = owns_path
                return detached
            except BaseException:
                self._unlink(target)
                raise

    def _overlay_snapshot(self, disk: AnalysisSnapshot) -> AnalysisSnapshot:
        revisions = {revision.file_path: revision for revision in disk.files}
        from emend.language_registry import (
            detect_language,
            get_module_separator,
            language_config_snapshot,
            registry_snapshot,
        )
        registry = registry_snapshot(self.project_root)
        for path, (_owner, version, content) in self._overlays.items():
            language = detect_language(path, registry=registry) or "python"
            config = next(
                (revision.analysis_config for revision in disk.files
                 if revision.language == language and revision.analysis_config is not None),
                None,
            ) or language_config_snapshot(language, self.project_root)
            revisions[path] = FileRevision.create(
                self.project_root,
                path,
                hashlib.sha256(content.encode()).hexdigest(),
                language,
                self._module_name(path, language, get_module_separator(language, config)),
                analysis_config=config,
                origin="overlay",
                version=version,
            )
        return self._make_snapshot(
            revisions.values(), base_snapshot_id=disk.snapshot_id
        )

    def _ensure_overlay_facts(self, disk_graph):
        with self._source_lock:
            snapshot = self._overlay_snapshot(disk_graph.snapshot) if self._overlays else None
            contents = {path: value[2] for path, value in self._overlays.items()}
        if snapshot is None:
            self._discard_overlay_graph()
            return disk_graph
        if (self._overlay_graph is not None
                and self._overlay_graph.snapshot.snapshot_id == snapshot.snapshot_id):
            return self._overlay_graph
        previous_path = self._graph_path(self._overlay_graph)
        graph, path = self._updated_graph(
            self._overlay_graph or disk_graph, snapshot,
            contents,
        )
        self._overlay_graph = graph
        if previous_path != path:
            self._unlink(previous_path)
        return graph

    def _typed_facts(self, graph, engine: str, *, _retry: bool = False):
        """Return a cached typed view for *graph* without changing it.

        Syntax/fact extraction is intentionally kept independent from type
        checking.  A type query opts into this method, which infers against
        the exact current snapshot and publishes the result on a private COW
        graph.  Engines that cannot consume editor overlays are not run while
        overlays are active; returning an empty typed view is safer than
        attaching types read from disk to overlay facts.
        """
        from emend.type_oracle import (
            _type_engine_context,
            create_type_oracle,
            parse_type_string,
        )

        resolved_engine = engine
        if engine == "auto":
            from emend.type_oracle import detect_type_engine

            resolved_engine = detect_type_engine(self.project_root)
        expected_context = (
            f"{self.type_context_id()}|{_type_engine_context(resolved_engine, {})}"
        )
        expected_oracle_key = (resolved_engine, expected_context)
        if self._type_oracle is not None and self._type_oracle[0] == expected_oracle_key:
            oracle = self._type_oracle[1]
        else:
            candidate = create_type_oracle(
                engine=resolved_engine, project_root=self.project_root
            )
            cache_context = str(getattr(candidate, "cache_context_id", ""))
            oracle_key = (resolved_engine, cache_context)
            oracle = (
                self._type_oracle[1]
                if self._type_oracle is not None
                and self._type_oracle[0] == oracle_key
                else candidate
            )
            if oracle is not candidate:
                candidate.close()
            if self._type_oracle is not None and self._type_oracle[1] is not oracle:
                self._type_oracle[1].close()
            self._type_oracle = (oracle_key, oracle)
        cache_context = str(getattr(oracle, "cache_context_id", ""))
        def type_generation():
            identities = self.type_file_identities(
                (revision.file_path for revision in graph.snapshot.files),
                graph=graph, include_overlays=True,
            )
            return hashlib.sha256(repr(sorted(identities.items())).encode()).hexdigest()

        generation = type_generation()
        key = (
            f"{graph.snapshot.snapshot_id}:{generation}",
            f"{TYPE_FACTS_ARTIFACT_VERSION}:{cache_context}",
            resolved_engine,
        )
        cached = self._typed_graph
        if cached is not None and cached[0] == key:
            return cached[1]
        available = oracle.is_available()
        if any(r.origin == "overlay" for r in graph.snapshot.files) and not getattr(oracle, "supports_source_overrides", False):
            # Pyrefly and the compiler API read disk themselves.  They cannot
            # produce a result for this coherent overlay generation.
            paths: list[Path] = []
        else:
            paths = [Path(revision.file_path) for revision in graph.snapshot.files]

        base_path = self._graph_path(graph)
        if base_path is None:
            return graph
        typed, typed_path = self._clone_graph(base_path, graph.snapshot)
        typed.bind_snapshot(
            graph.snapshot,
            source_loader=self._revision_source,
        )
        type_facts: list[TypeFact] = []
        complete = available
        if paths and available:
            try:
                results = oracle.infer_batch(paths, project_root=self.project_root)
                complete = all(str(path) in results and results[str(path)].complete for path in paths)
                for revision in graph.snapshot.files:
                    file_types = results.get(str(Path(revision.file_path).resolve()))
                    if file_types is None:
                        continue
                    for binding in file_types.bindings:
                        type_facts.append(TypeFact(
                            symbol_qn=binding.name,
                            type_str=parse_type_string(binding.raw_type).name,
                            file_path=typed.stored_path(revision.file_path),
                            line=binding.line,
                            binding_kind=binding.binding_kind,
                        ))
            except BUG_EXCEPTIONS:
                typed.close()
                self._unlink(typed_path)
                raise
            except Exception:
                logger.debug("Could not populate type bindings", exc_info=True)
                complete = False
        try:
            typed.replace_types_batch(type_facts)
        except BaseException:
            typed.close()
            self._unlink(typed_path)
            raise
        # An analyzer subprocess can outlive a source edit.  Do not publish
        # its answer under the generation captured before that subprocess
        # started; refresh once and fail rather than returning a mismatch if
        # the project is being edited continuously.
        current_id = self.source_snapshot().snapshot_id
        if current_id != graph.snapshot.snapshot_id or type_generation() != generation:
            typed.close()
            self._unlink(typed_path)
            if _retry:
                raise RuntimeError("source changed during typed analysis")
            return self._typed_facts(
                self.query_facts(), engine, _retry=True
            )
        previous = self._typed_graph
        self._typed_graph = (key, typed) if complete else None
        previous_path = self._graph_path(previous[1]) if previous is not None else None
        if previous_path != typed_path:
            # The old graph may still be held by a reader.  SQLite keeps its
            # open handle valid after unlinking the private backing file.
            self._unlink(previous_path)
        if not complete:
            self._unlink(typed_path)
        return typed

    @contextmanager
    def prepare_index_facts(self, file_paths=None, *, include_overlays=False,
                            include_environment=True, prepare=None, prepared=None,
                            callback=None, jobs=None):
        """Fan one pass over the project into type inputs and a private fact view.

        The fact writer decides which revisions it must store; only those, and
        revisions whose imports were never recorded, are parsed.
        """
        from concurrent.futures import Future, ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutureTimeout
        from queue import Queue, Full, Empty

        scan = self._scan_disk()
        if callback is not None:
            callback("total", str(len(scan.snapshot.files)))
        queue = Queue(maxsize=8)
        end = object()
        plan: Future = Future()

        def consume(revisions, contents):
            required = {revision.file_path: revision for revision in revisions}
            if not plan.done():
                plan.set_result(frozenset(required))

            def batches():
                while True:
                    items = [queue.get()]
                    for _ in range(7):
                        try:
                            items.append(queue.get_nowait())
                        except Empty:
                            break
                    batch, finished = [], False
                    for item in items:
                        if item is end:
                            finished = True
                            break
                        if isinstance(item, BaseException):
                            raise item
                        expected = required.pop(item.revision.file_path, None)
                        if expected is not None:
                            if expected != item.revision:
                                raise RuntimeError("extraction stream changed revisions")
                            batch.append(item)
                    if batch:
                        yield batch
                    if finished:
                        if required:
                            raise RuntimeError("incomplete extraction stream")
                        return

            return batches()

        def materialize():
            with self._graph_lock, self._facts_write_lock():
                return self._ensure_disk_facts(scan, consume)

        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(materialize)

            def send(item):
                while not writer.done():
                    try:
                        queue.put(item, timeout=0.05)
                        return
                    except Full:
                        pass
                writer.result()
                raise RuntimeError("fact writer ended before extraction")

            def required_paths():
                while True:
                    try:
                        return plan.result(timeout=0.05)
                    except FutureTimeout:
                        if writer.done():
                            writer.result()
                            raise RuntimeError("fact writer ended before planning")

            def file_imports():
                required = required_paths()
                for revision, extracted, imports in self._prepare_revisions(
                    scan.snapshot.files, scan.contents,
                    prepare=prepare, prepared=prepared, jobs=jobs,
                    needs_facts=lambda revision: revision.file_path in required,
                    started=(lambda revision: callback("start", revision.file_path))
                    if callback is not None else None,
                ):
                    if extracted is not None and revision.file_path in required:
                        send(extracted)
                    yield revision.file_path, imports
                send(end)

            try:
                stream = file_imports()
                if file_paths is None:
                    for _ in stream:
                        pass
                    inputs = None
                else:
                    inputs = self.type_file_inputs(
                        file_paths,
                        _local_state=(scan.snapshot, stream),
                        include_environment=include_environment,
                    )
                    if include_overlays and self._overlays:
                        inputs = self.type_file_inputs(
                            file_paths,
                            include_overlays=True,
                            include_environment=include_environment,
                        )
                yield inputs
            except BaseException as error:
                if not writer.done():
                    send(error)
                raise
            finally:
                writer.result()

    def query_facts(
        self, *, include_types: bool = False, type_engine: str = "auto"
    ):
        """Return the current facts, optionally with an owner-managed type view."""
        with self._graph_lock:
            scan = self._scan_disk()
            # A reader of an up-to-date index neither waits for writers nor
            # scans again: it adopts the generation that is already published.
            disk_graph = self._adopt_published_facts()
            if (
                disk_graph is None
                or disk_graph.snapshot.snapshot_id != scan.snapshot.snapshot_id
            ):
                with self._facts_write_lock():
                    # A waiting process may have observed an older revision;
                    # rescan while publication is serialized.
                    prior_scan, scan = scan, self._scan_disk()
                    prior_revisions = {
                        revision.file_path: revision
                        for revision in prior_scan.snapshot.files
                    }
                    scan = _DiskScan(scan.snapshot, scan.contents | {
                        revision.file_path: prior_scan.contents[revision.file_path]
                        for revision in scan.snapshot.files
                        if revision == prior_revisions.get(revision.file_path)
                        and revision.file_path in prior_scan.contents
                    })
                    disk_graph = self._ensure_disk_facts(scan)
            graph = self._ensure_overlay_facts(disk_graph)
            if not include_types:
                return graph
            return self._typed_facts(graph, type_engine)

    def published_facts(self):
        """Return the facts this store last saw published, without checking them.

        Interactive lookups use this: it never scans the project, builds
        facts, or waits for a build.  The answer is as old as the last index.
        ``None`` means that nothing is published, or that a build holds the
        store before this process has opened a generation.
        """
        graph = self._disk_graph
        if graph is not None and self._generation_exists(graph):
            return graph
        if not self._graph_lock.acquire(blocking=False):
            return None
        try:
            return self._adopt_published_facts()
        finally:
            self._graph_lock.release()

    def source_snapshot(self) -> AnalysisSnapshot:
        """Return current disk/overlay identity without constructing a graph."""
        with self._source_lock:
            disk = self._scan_disk().snapshot
            return self._overlay_snapshot(disk) if self._overlays else disk

    def disk_snapshot(self) -> AnalysisSnapshot:
        """Return current on-disk identity without constructing derived facts."""
        return self._scan_disk().snapshot

    def type_context_id(self) -> str:
        """Return configuration, lockfile, and environment identity."""
        digest = hashlib.sha256()
        digest.update(f"type-results:{TYPE_RESULT_VERSION}\0".encode())
        for relative in (
            ".emend/config.toml", "pyproject.toml", "pyrefly.toml",
            "pyrightconfig.json", "ty.toml", "tsconfig.json", "Cargo.toml",
            "Cargo.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
            "uv.lock", "poetry.lock", "requirements.txt", "package.json",
        ):
            path = self.project_root / relative
            digest.update(relative.encode())
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<missing>")
        from emend.project_config import load_typescript_config

        try:
            _options, _origins, inherited_configs = load_typescript_config(
                self.project_root
            )
        except (OSError, ValueError, TypeError, AttributeError):
            inherited_configs = ()
        for path in inherited_configs:
            digest.update(os.path.relpath(path, self.project_root).encode())
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<missing>")
        return digest.hexdigest()

    def type_file_identity(
        self,
        file_path: str | Path,
        content_hash: str | None = None,
        *,
        include_overlays: bool = False,
        include_environment: bool = True,
    ) -> str:
        """Return logical file/content plus transitive local dependency identity."""
        resolved = str(Path(file_path).resolve())
        return self.type_file_identities(
            [resolved],
            {resolved: content_hash} if content_hash else None,
            include_overlays=include_overlays,
            include_environment=include_environment,
        )[resolved]

    def _external_type_source(self, path, language, root, module, config):
        """Observe one resolved dependency; reuse path-free parsing artifacts."""
        from emend.file_collection import source_file_identities
        from emend import emend_core

        path = Path(path)
        with self._source_lock:
            identity = source_file_identities([path]).get(str(path))
            if identity is None:
                return None
            path = path.resolve()
            observed_key = (str(path), config.identity)
            previous = self._external_type_files.get(observed_key)
            if previous is not None and previous[0] == identity:
                digest, imports = previous[1:]
            else:
                try:
                    source = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
                if source_file_identities([path]).get(str(path)) != identity:
                    raise RuntimeError(f"dependency changed while reading: {path}")
                digest = hashlib.sha256(source.encode()).hexdigest()
                artifact_key = repr(("3", path.suffix, digest, config.identity))
                row = self.artifact_connection().execute(
                    "SELECT payload FROM dependency_import_artifact WHERE identity = ?",
                    (artifact_key,),
                ).fetchone()
                imports = None
                if row is not None:
                    try:
                        imports = tuple(
                            (str(module), name) for module, name in cache_codec.decode(row[0])
                        )
                    except (ValueError, TypeError):
                        logger.debug("Unreadable dependency import artifact", exc_info=True)
                if imports is None:
                    resolver = emend_core.PyScopeResolver(str(self.project_root), path.suffix.lstrip("."))
                    resolver.index_file(str(path), source)
                    imports = tuple((row[1].strip("'\""), row[2])
                                    for row in resolver.imports_in_file(str(path)))
                    def publish(db):
                        db.execute("INSERT OR REPLACE INTO dependency_import_artifact VALUES (?, ?)",
                                   (artifact_key, cache_codec.encode(imports)))
                        db.execute("INSERT OR IGNORE INTO source_artifact VALUES (?, ?)",
                                   (digest, zlib.compress(source.encode())))
                    self.write(publish, artifacts=True)
                self._external_type_files[observed_key] = (identity, digest, imports)
            return FileRevision.create(root, path, digest, language, module), imports

    def _type_dependency_state(
        self,
        graph=None,
        *,
        include_overlays=False,
        local_state=None,
        include_environment=True,
    ):
        """Resolve import dependencies without requiring graph materialization."""
        from emend.language_registry import registry_snapshot

        _, language_extensions = registry_snapshot()
        if graph is None or local_state is not None:
            if local_state is not None:
                snapshot, file_imports = local_state
                imports_by_path = {path: list(imports) for path, imports in file_imports}
            else:
                with self._source_lock:
                    scan = self._scan_disk()
                    snapshot = (self._overlay_snapshot(scan.snapshot)
                                if include_overlays and self._overlays else scan.snapshot)
                    contents = scan.contents | (
                        {path: value[2] for path, value in self._overlays.items()}
                        if include_overlays else {}
                    )
                imports_by_path = self._revision_imports(snapshot.files, contents)
        else:
            if not include_overlays and self._disk_graph is not None:
                graph = self._disk_graph
            snapshot = graph.snapshot
            stored_imports = graph.imports_by_file()
            imports_by_path = {
                revision.file_path: stored_imports.get(self._stored_path(revision), [])
                for revision in snapshot.files
            }

        revisions = {revision.file_path: revision for revision in snapshot.files}
        module_to_revision = {
            (revision.language, revision.module_name.replace("::", ".").replace("/", ".")): revision
            for revision in revisions.values()
        }

        suffix_to_revisions: dict[tuple[str, str], list[FileRevision]] = {}
        for (language, module), revision in module_to_revision.items():
            parts = module.split(".")
            for index in range(len(parts)):
                suffix_to_revisions.setdefault((language, ".".join(parts[index:])), []).append(
                    revision
                )

        external_roots = {}
        pending_revisions = list(revisions.values())
        configs = {r.language: r.analysis_config for r in revisions.values()}

        @cache
        def external_revision(path, language, root, module):
            existing = revisions.get(str(path))
            if existing is not None:
                return existing
            if configs.get(language) is None:
                from emend.language_registry import language_config_snapshot
                configs[language] = language_config_snapshot(language, self.project_root)
            result = self._external_type_source(path, language, root, module, configs[language])
            if result is None:
                return None
            revision, imports = result
            if revision.file_path in revisions:
                return revisions[revision.file_path]
            revisions[revision.file_path] = revision
            imports_by_path[revision.file_path] = imports
            pending_revisions.append(revision)
            return revision

        @cache
        def module_revision(name: str, language: str):
            local_name = name
            if language == "typescript":
                from emend.emend_core import resolve_node_import
                local_name = resolve_node_import(name, "")
            exact = module_to_revision.get((language, local_name))
            if exact is not None:
                return exact
            matches = list(suffix_to_revisions.get((language, local_name), ()))
            parts = (local_name or "").split(".")
            matches.extend(
                candidate
                for index in range(1, len(parts))
                if (candidate := module_to_revision.get((language, ".".join(parts[index:]))))
                is not None
            )
            matches = list({
                candidate.file_path: candidate for candidate in matches
            }.values())
            if matches and language == "rust":
                return matches[0] if len(matches) == 1 else None
            if not include_environment:
                return None
            from emend.project_config import resolve_environment_path
            if language not in external_roots:
                external_roots[language] = resolve_environment_path(
                    str(self.project_root), language, require_enabled=False
                )
            root = external_roots[language]
            if root is None:
                return None
            base = root / name if language == "typescript" else root.joinpath(*name.split("."))
            if language == "python":
                candidates = [base.with_suffix(".pyi"), base.with_suffix(".py"),
                              base / "__init__.pyi", base / "__init__.py"]
            else:
                candidates = source_candidates(base, language)
            for path in candidates:
                result = external_revision(path, language, root, name)
                if result is not None:
                    return result
            return None

        from emend.project_config import pyrefly_search_roots
        python_search_roots = (
            pyrefly_search_roots(self.project_root)
            if include_environment and "python" in configs else ()
        )

        @cache
        def configured_python_revisions(name):
            candidates = []
            for root in python_search_roots:
                base = root.joinpath(*name.split("."))
                for path in (base.with_suffix(".pyi"), base.with_suffix(".py"),
                             base / "__init__.pyi", base / "__init__.py"):
                    revision = external_revision(path, "python", root, name)
                    if revision is not None:
                        candidates.append(revision)
            return candidates

        from emend.project_config import load_typescript_config

        try:
            compiler_options, option_origins, _sources = load_typescript_config(
                self.project_root
            )
        except (OSError, ValueError, TypeError, AttributeError):
            compiler_options = {}
            option_origins = {}
        base_origin = option_origins.get(
            "baseUrl", option_origins.get("paths", self.project_root)
        )
        base_url = compiler_options.get("baseUrl", ".")
        ts_base = (base_origin / (base_url if isinstance(base_url, str) else ".")).resolve()
        ts_paths = compiler_options.get("paths", {})
        if not isinstance(ts_paths, dict):
            ts_paths = {}

        def typescript_aliases(name: str) -> list[Path]:
            aliases: list[Path] = []
            for pattern, replacements in ts_paths.items():
                prefix, wildcard, suffix = str(pattern).partition("*")
                if wildcard:
                    if not name.startswith(prefix) or not name.endswith(suffix):
                        continue
                    captured = name[len(prefix):len(name) - len(suffix) or None]
                elif name == prefix:
                    captured = ""
                else:
                    continue
                for replacement in (
                    replacements if isinstance(replacements, list) else [replacements]
                ):
                    aliases.append(
                        (ts_base / str(replacement).replace("*", captured)).resolve()
                    )
            if "baseUrl" in compiler_options:
                aliases.append((ts_base / name).resolve())
            return aliases

        def source_candidates(base: Path, language: str) -> list[Path]:
            extensions = list(language_extensions.get(language, ()))
            if language == "typescript":
                extensions.insert(2, "d.ts")
            stem = base.with_suffix("") if base.suffix.lstrip(".") in extensions else base
            candidates = [base]
            candidates.extend(Path(f"{stem}.{ext}") for ext in extensions)
            candidates.extend(base / f"index.{ext}" for ext in extensions)
            return candidates

        dependencies: dict[str, set[str]] = {}
        ambient_typescript = {
            revision.file_path
            for revision in revisions.values()
            if revision.language == "typescript"
            and revision.file_path.endswith(".d.ts")
        }
        for revision in pending_revisions:
            resolved_dependencies = (
                ambient_typescript - {revision.file_path}
                if revision.language == "typescript" else set()
            )
            for name, imported_name in imports_by_path[revision.file_path]:
                candidates = []
                if revision.language == "python":
                    if name.startswith("."):
                        package = revision.module_name
                        if Path(revision.file_path).stem != "__init__":
                            package = package.rpartition(".")[0]
                        try:
                            name = importlib.util.resolve_name(name, package)
                        except (ImportError, ValueError):
                            pass
                    name = name.lstrip(".").replace("::", ".").replace("/", ".")
                    names = [name, *(".".join(name.split(".")[:i])
                                     for i in range(1, len(name.split("."))))]
                    if imported_name not in (None, "*"):
                        names.append(f"{name}.{imported_name}")
                    for name in names:
                        candidates.append(module_revision(name, revision.language))
                        candidates.extend(configured_python_revisions(name))
                elif name.startswith("."):
                    base = (Path(revision.file_path).parent / name).resolve()
                    # TypeScript commonly imports emitted ``.js`` names whose
                    # source dependency is ``.ts``/``.tsx``.
                    paths = source_candidates(base, revision.language)
                    module = revision.module_name
                    if Path(revision.file_path).name not in {
                        f"index.{ext}" for ext in (*language_extensions.get(revision.language, ()), "d.ts")
                    }:
                        module = module.rpartition("/")[0]
                    imported_module = posixpath.normpath(posixpath.join(module, name))
                    candidates.extend(
                        revisions.get(str(path)) or (
                            external_revision(path, revision.language, Path(revision.project_root),
                                              imported_module)
                            if revision.project_root != str(self.project_root) else None
                        ) for path in paths
                    )
                else:
                    if revision.language == "typescript":
                        candidates.extend(
                            revisions.get(str(path))
                            for base in typescript_aliases(name)
                            for path in source_candidates(base, revision.language)
                        )
                    normalized = name.replace("::", ".").replace("/", ".")
                    if not any(candidates):
                        candidates.append(module_revision(
                            name if revision.language == "typescript" else normalized, revision.language
                        ))
                resolved_dependencies.update(
                    candidate.file_path for candidate in candidates
                    if candidate is not None
                )
            dependencies[revision.file_path] = resolved_dependencies
        return revisions, dependencies

    def _type_identities(
        self, file_paths, content_hashes, revisions, dependencies
    ) -> dict[str, str]:
        """Hash targets and their transitive local inputs."""
        result: dict[str, str] = {}
        for file_path in file_paths:
            resolved = str(Path(file_path).resolve())
            target = revisions.get(resolved)
            try:
                logical = str(Path(resolved).relative_to(self.project_root))
            except ValueError:
                logical = Path(resolved).name
            supplied_hash = (content_hashes or {}).get(resolved)
            target_hash = supplied_hash or (
                target.content_hash if target is not None
                else hashlib.sha256(Path(resolved).read_bytes()).hexdigest()
            )
            dependency_rows: set[tuple[str, str]] = set()
            pending = list(dependencies.get(resolved, ()))
            seen: set[str] = set()
            while pending:
                dependency_path = pending.pop()
                if dependency_path in seen:
                    continue
                seen.add(dependency_path)
                dependency = revisions[dependency_path]
                dependency_rows.add(
                    (dependency.module_name, dependency.content_hash)
                )
                pending.extend(dependencies.get(dependency_path, ()))
            result[resolved] = hashlib.sha256(repr(
                (logical, target_hash, sorted(dependency_rows))
            ).encode()).hexdigest()
        return result

    def type_file_identities(
        self,
        file_paths: Iterable[str | Path],
        content_hashes: dict[str, str] | None = None,
        *,
        include_overlays: bool = False,
        graph: object | None = None,
        include_environment: bool = True,
    ) -> dict[str, str]:
        """Compute cache identities against one source generation."""
        revisions, dependencies = self._type_dependency_state(
            graph,
            include_overlays=include_overlays,
            include_environment=include_environment,
        )
        return self._type_identities(
            file_paths, content_hashes, revisions, dependencies
        )

    def type_file_inputs(
        self,
        file_paths: Iterable[str | Path],
        *,
        include_overlays: bool = False,
        graph: object | None = None,
        _local_state=None,
        include_environment: bool = True,
    ) -> tuple[dict[str, str], dict[str, str], set[str]]:
        """Capture identities, transitive sources, and project file membership."""
        paths = [str(Path(path).resolve()) for path in file_paths]
        revisions, dependencies = self._type_dependency_state(
            graph,
            include_overlays=include_overlays,
            local_state=_local_state,
            include_environment=include_environment,
        )
        paths = [path for path in paths if path in revisions]
        identities = self._type_identities(paths, None, revisions, dependencies)
        inputs, pending = set(paths), list(paths)
        while pending:
            dependency = pending.pop()
            for child in dependencies.get(dependency, ()):
                if child not in inputs:
                    inputs.add(child)
                    pending.append(child)
        return (
            identities,
            {path: self._revision_source(revisions[path]) for path in inputs},
            {path for path, revision in revisions.items()
             if revision.project_root == str(self.project_root)},
        )

    def update_overlay(
        self,
        file_path: str | Path,
        content: str,
        version: int,
        *,
        owner: object | None = None,
    ) -> OverlayUpdate:
        """Publish a monotonic editor-buffer revision."""
        with self._source_lock:
            resolved = str(Path(file_path).resolve())
            current = self._overlays.get(resolved)
            same_owner = current is not None and current[0] is owner
            accepted = current is None or not same_owner or (
                version == 0 and current[1] == 0
            ) or version > current[1] or (
                version == current[1] and content == current[2]
            )
            if accepted:
                self._overlays[resolved] = (owner, version, content)
            return OverlayUpdate(
                resolved, version, accepted,
                version if accepted else current[1] if current else None,
            )

    def remove_overlay(
        self,
        file_path: str | Path,
        version: int | None = None,
        *,
        owner: object | None = None,
    ) -> OverlayUpdate:
        """Remove an editor overlay unless the close notification is stale."""
        with self._source_lock:
            resolved = str(Path(file_path).resolve())
            current = self._overlays.get(resolved)
            accepted = current is not None and (
                owner is None or current[0] is owner
            ) and (version is None or version >= current[1])
            if accepted:
                del self._overlays[resolved]
            return OverlayUpdate(
                resolved, version, accepted,
                None if accepted else current[1] if current else None,
            )

    def remove_overlays(self, owner: object) -> int:
        """Release every overlay still owned by one editor session."""
        with self._source_lock:
            paths = [path for path, value in self._overlays.items()
                     if value[0] is owner]
            for path in paths:
                del self._overlays[path]
            return len(paths)

    def overlay_content(self, file_path: str | Path) -> str | None:
        with self._source_lock:
            value = self._overlays.get(str(Path(file_path).resolve()))
            return value[2] if value else None
