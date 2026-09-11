"""Project-scoped owner for source revisions and derived analysis state."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import logging
import os
import pickle
from pathlib import Path
import sqlite3
import threading
import tempfile
import zlib
from collections.abc import Callable, Iterable

from emend.analysis_snapshot import AnalysisSnapshot, ExtractedFile, FileRevision, TypeFact
from emend.errors import BUG_EXCEPTIONS
from emend.project_config import find_project_root
from emend.symbol_projection import SymbolInfo, _symbol_info_view


EXTRACTION_ARTIFACT_VERSION = "7"
TYPE_FACTS_ARTIFACT_VERSION = "1"
logger = logging.getLogger(__name__)

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
        self._connection_lock = threading.RLock()
        self._refresh_lock = threading.RLock()
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

    def symbols(self, source: str, ext: str = "py") -> tuple:
        """Return immutable syntax symbols; content identity never includes a path."""
        from emend import emend_core
        from emend.language_registry import detect_language
        from emend.symbol_projection import project_symbols

        key = (ext, hashlib.sha256(source.encode()).hexdigest())
        with self._connection_lock:
            if key not in self._symbols:
                symbols = None
                try:
                    conn = self.artifact_connection()
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS symbol_projection "
                        "(identity TEXT PRIMARY KEY, payload BLOB NOT NULL)"
                    )
                    from emend.language_registry import config_identity
                    config = config_identity(detect_language(f"file.{ext}") or "python")
                    identity = repr(("2", EXTRACTION_ARTIFACT_VERSION, key, config))
                    row = conn.execute(
                        "SELECT payload FROM symbol_projection WHERE identity = ?", (identity,)
                    ).fetchone()
                    if row is not None:
                        symbols = pickle.loads(zlib.decompress(row[0]))
                    else:
                        symbols = project_symbols(emend_core.collect_symbols_from_str(source, ext=ext))
                        conn.execute(
                            "INSERT OR IGNORE INTO symbol_projection VALUES (?, ?)",
                            (identity, zlib.compress(pickle.dumps(symbols))),
                        )
                        conn.commit()
                except (OSError, sqlite3.Error):
                    logger.debug("Symbol artifact cache unavailable", exc_info=True)
                    if self._artifact_connection is not None:
                        self._artifact_connection.rollback()
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
        """Return this project's long-lived SQLite connection."""
        with self._connection_lock:
            if self._connection is None:
                self.ensure_cache_directory()
                self._connection = sqlite3.connect(
                    str(self.db_path), check_same_thread=False
                )
            if schema_initializer is not None:
                schema_initializer(self._connection)
            return self._connection

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

    def _temporary_db_path(self, prefix: str) -> Path:
        """Allocate an empty private database in the owner cache."""
        self.ensure_cache_directory()
        fd, name = tempfile.mkstemp(prefix=prefix, suffix=".db", dir=self.cache_dir)
        os.close(fd)
        return Path(name)

    @staticmethod
    def _unlink(path: Path | None) -> None:
        """Best-effort removal for an obsolete private generation."""
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _graph_path(graph: object | None) -> Path | None:
        path = getattr(graph, "_db_path", None)
        return Path(path) if path is not None else None

    @staticmethod
    def _stat_identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000)),
        )

    def close(self) -> None:
        self._discard_overlay_graph(close=True)
        self._overlays.clear()
        if self._typed_graph is not None:
            typed_graph = self._typed_graph[1]
            typed_path = self._graph_path(typed_graph)
            typed_graph.close()
            self._unlink(typed_path)
            self._typed_graph = None
        self._type_oracle = None
        if self._disk_graph is not None:
            disk_path = self._graph_path(self._disk_graph)
            self._disk_graph.close()
            self._disk_graph = None
            self._unlink(disk_path)
        with self._connection_lock:
            self._symbols.clear()
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._artifact_connection is not None:
                self._artifact_connection.close()
                self._artifact_connection = None

    def _extraction_context_id(self, revisions: Iterable[FileRevision]) -> str:
        """Return the schema/config identity governing extracted facts."""
        from emend.fact_graph import FACT_GRAPH_SCHEMA_VERSION

        configurations = {
            (revision.language, revision.analysis_config.identity)
            for revision in revisions
            if revision.analysis_config is not None
        }
        payload = (
            FACT_GRAPH_SCHEMA_VERSION,
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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS analysis_file_revision ("
            "path TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL, "
            "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, "
            "content_hash TEXT NOT NULL, language TEXT NOT NULL, module_name TEXT NOT NULL)"
        )
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
        conn = self.connection()
        changed = [
            (path, *identity)
            for path, identity in observed.items()
            if previous.get(path) != identity
        ]
        if changed:
            conn.executemany(
                "INSERT OR REPLACE INTO analysis_file_revision "
                "(path, device, inode, size, mtime_ns, ctime_ns, content_hash, "
                "language, module_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                changed,
            )
        deleted = set(previous) - set(observed)
        if deleted:
            conn.executemany(
                "DELETE FROM analysis_file_revision WHERE path = ?",
                ((path,) for path in deleted),
            )
        if changed or deleted:
            conn.commit()

    def _scan_disk(self) -> _DiskScan:
        """Read the current source inventory, hashing only stat changes."""
        from emend.file_collection import collect_all_source_files
        from emend.language_registry import (
            detect_language,
            get_module_separator,
            registry_and_config_snapshots,
        )
        from emend.project_config import find_source_root

        self._load_observed_files()
        # Source-root configuration is mutable during long editor sessions.
        # Recompute it once per language for this inventory, then let the LRU
        # absorb all per-file module-name lookups below.
        find_source_root.cache_clear()
        previous = self._observed_files
        registry, configs = registry_and_config_snapshots(self.project_root)
        module_separators = {
            language: get_module_separator(language, configs.get(language))
            for language in registry[1]
        }
        files = sorted(
            str(Path(path).resolve())
            for path in collect_all_source_files(
                str(self.project_root), languages=list(registry[1]), registry=registry
            )
        )
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
            module_name = self._module_name(
                file_path, language, module_separators.get(language)
            )
            identity = self._stat_identity(stat)
            if old is not None and old[:5] == identity:
                content_hash = old[5]
            else:
                # Verify the identity around the read so a concurrent editor
                # save cannot bind bytes to the wrong revision.
                for _attempt in range(3):
                    before = os.stat(file_path)
                    try:
                        content = Path(file_path).read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        break
                    after = os.stat(file_path)
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
            revisions.append(FileRevision.create(
                self.project_root, file_path, content_hash, language, module_name,
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
                self._artifact_connection = sqlite3.connect(
                    str(self.artifact_path), check_same_thread=False
                )
            return self._artifact_connection

    def _extract_revisions(
        self,
        revisions: Iterable[FileRevision],
        contents: dict[str, str],
    ) -> list[ExtractedFile]:
        """Load content-addressed revision artifacts or extract them once."""
        from emend.analysis_extraction import _extract_file_facts
        from emend.fact_graph import FACT_GRAPH_SCHEMA_VERSION

        db_path = self._shared_artifact_path()
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS extracted_file_artifact ("
            "artifact_key TEXT PRIMARY KEY, payload BLOB NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS source_artifact ("
            "content_hash TEXT PRIMARY KEY, payload BLOB NOT NULL)"
        )
        revisions = list(revisions)
        result: list[ExtractedFile | None] = [None] * len(revisions)
        pending: list[tuple[int, FileRevision, str, str, str]] = []
        try:
            for index, revision in enumerate(revisions):
                if revision.analysis_config is None:
                    raise RuntimeError(
                        f"snapshot lacks language config for {revision.file_path}"
                    )
                try:
                    stored_path = str(
                        Path(revision.file_path).relative_to(self.project_root)
                    )
                except ValueError:
                    stored_path = revision.file_path
                key_payload = (
                    "facts", FACT_GRAPH_SCHEMA_VERSION,
                    EXTRACTION_ARTIFACT_VERSION, revision.language,
                    revision.module_name, stored_path, revision.content_hash,
                    revision.analysis_config.identity,
                )
                key = hashlib.sha256(repr(key_payload).encode()).hexdigest()
                row = conn.execute(
                    "SELECT payload FROM extracted_file_artifact WHERE artifact_key = ?",
                    (key,),
                ).fetchone()
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
                if row is not None:
                    cached = pickle.loads(zlib.decompress(row[0]))
                    result[index] = ExtractedFile(
                        revision=revision,
                        qnames=cached.qnames,
                        rows=cached.rows,
                    )
                    continue
                if content is None:
                    assert source_row is not None
                    content = zlib.decompress(source_row[0]).decode()

                pending.append((index, revision, stored_path, key, content))

            # Publish source blobs before parsing; type inference and other
            # worktrees must be able to write their independent artifacts.
            conn.commit()

            def extract(item):
                index, revision, stored_path, key, content = item
                return index, key, _extract_file_facts(revision, stored_path, content)

            if pending:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor() as pool:
                    extracted_files = list(pool.map(extract, pending))
                for index, key, extracted in extracted_files:
                    result[index] = extracted
                    conn.execute(
                        "INSERT OR REPLACE INTO extracted_file_artifact "
                        "(artifact_key, payload) VALUES (?, ?)",
                        (key, zlib.compress(pickle.dumps(extracted))),
                    )
            conn.commit()
        finally:
            conn.close()
        assert all(extracted is not None for extracted in result)
        return [extracted for extracted in result if extracted is not None]

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
    def _copy_sqlite(source_path: Path, destination_path: Path) -> None:
        """Copy a coherent SQLite generation, including uncheckpointed WAL."""
        with (
            closing(sqlite3.connect(source_path)) as source,
            closing(sqlite3.connect(destination_path)) as destination,
        ):
            source.backup(destination)

    def _snapshot_copy(self, source_path: Path, prefix: str) -> Path:
        """Create a stable private copy of one SQLite generation."""
        path = self._temporary_db_path(prefix)
        try:
            self._copy_sqlite(source_path, path)
        except BaseException:
            self._unlink(path)
            raise
        return path

    def _set_disk_graph(self, graph: object) -> None:
        previous_path = self._graph_path(self._disk_graph)
        self._disk_graph = graph
        if previous_path != self._graph_path(graph):
            self._unlink(previous_path)

    def _updated_graph(self, previous, snapshot: AnalysisSnapshot, contents):
        """Apply one revision delta, equally for disk and overlay generations."""
        from emend.analysis_linking import ModuleCatalog, link_extracted_files
        from emend.fact_graph import FactGraph

        if previous is None:
            path = self._temporary_db_path("facts-next-")
            graph = FactGraph(db_path=str(path))
            before = {}
        else:
            graph, path = self._clone_graph(Path(previous._db_path), snapshot)
            before = {revision.file_path: revision for revision in previous.snapshot.files}
            if previous.snapshot.analysis_context_id != snapshot.analysis_context_id:
                before = {}
        graph.bind_snapshot(snapshot)
        after = {revision.file_path: revision for revision in snapshot.files}
        changed = [revision for file_path, revision in after.items()
                   if before.get(file_path) != revision]
        removed = ({revision.file_path for revision in previous.snapshot.files}
                   if previous is not None else set()) - after.keys()
        try:
            graph.clear_snapshot_marker()
            local = self._extract_revisions(changed, contents)
            catalog = ModuleCatalog.from_revisions(snapshot.files)
            # Import targets depend on module inventory (for example TS index
            # aliases). Relink cached callers when that inventory changes;
            # ordinary body edits still replace only the edited file.
            def module_inventory(revisions):
                return {(r.file_path, r.language, r.module_name) for r in revisions}

            if module_inventory(before.values()) != module_inventory(snapshot.files):
                local.extend(self._extract_revisions(
                    (r for r in snapshot.files if before.get(r.file_path) == r), {}
                ))
                local.sort(key=lambda file: file.revision.file_path)
                changed = list(snapshot.files)
            graph.replace_extracted(
                link_extracted_files(local, catalog),
                stored_paths=[graph.stored_path(file_path)
                              for file_path in [*(r.file_path for r in changed), *removed]],
            )
            graph.publish_snapshot(snapshot)
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

    def _ensure_disk_facts(self, scan: _DiskScan):
        from emend.fact_graph import FactGraph

        graph = self._disk_graph
        if graph is None and self.facts_path.is_file():
            path = candidate = None
            try:
                path = self._snapshot_copy(self.facts_path, "facts-open-")
                candidate = FactGraph(db_path=str(path))
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
        if graph is not None and graph.snapshot.snapshot_id == scan.snapshot.snapshot_id:
            graph.bind_snapshot(scan.snapshot, source_loader=self._revision_source)
            return graph
        candidate, path = self._updated_graph(graph, scan.snapshot, scan.contents)
        publish_path = None
        try:
            publish_path = self._snapshot_copy(path, "facts-publish-")
            os.replace(publish_path, self.facts_path)
        except BaseException:
            candidate.close()
            self._unlink(path)
            raise
        finally:
            self._unlink(publish_path)
        self._set_disk_graph(candidate)
        return candidate

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

        with self._refresh_lock:
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
        snapshot = self._overlay_snapshot(disk_graph.snapshot)
        if (self._overlay_graph is not None
                and self._overlay_graph.snapshot.snapshot_id == snapshot.snapshot_id):
            return self._overlay_graph
        previous_path = self._graph_path(self._overlay_graph)
        graph, path = self._updated_graph(
            self._overlay_graph or disk_graph, snapshot,
            {path: value[2] for path, value in self._overlays.items()},
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
            self._type_oracle = (oracle_key, oracle)
        cache_context = str(getattr(oracle, "cache_context_id", ""))
        key = (
            graph.snapshot.snapshot_id,
            f"{TYPE_FACTS_ARTIFACT_VERSION}:{cache_context}",
            resolved_engine,
        )
        cached = self._typed_graph
        if cached is not None and cached[0] == key:
            return cached[1]
        available = oracle.is_available()
        if self._overlays and not getattr(oracle, "supports_source_overrides", False):
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
            source_overrides={
                path: value[2] for path, value in self._overlays.items()
            },
            source_loader=self._revision_source,
        )
        type_facts: list[TypeFact] = []
        if paths and available:
            try:
                results = oracle.infer_batch(paths, project_root=self.project_root)
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
        current_disk = self._scan_disk().snapshot
        current_id = (
            self._overlay_snapshot(current_disk).snapshot_id
            if self._overlays else current_disk.snapshot_id
        )
        if current_id != graph.snapshot.snapshot_id:
            typed.close()
            self._unlink(typed_path)
            if _retry:
                raise RuntimeError("source changed during typed analysis")
            return self._typed_facts(
                self.query_facts(), engine, _retry=True
            )
        previous = self._typed_graph
        self._typed_graph = (key, typed)
        previous_path = self._graph_path(previous[1]) if previous is not None else None
        if previous_path != typed_path:
            # The old graph may still be held by a reader.  SQLite keeps its
            # open handle valid after unlinking the private backing file.
            self._unlink(previous_path)
        return typed

    def query_facts(
        self, *, include_types: bool = False, type_engine: str = "auto"
    ):
        """Return the current facts, optionally with an owner-managed type view."""
        with self._refresh_lock:
            scan = self._scan_disk()
            disk_graph = self._disk_graph
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
            graph = self._ensure_overlay_facts(disk_graph) if self._overlays else disk_graph
            if not include_types:
                return graph
            return self._typed_facts(graph, type_engine)

    def source_snapshot(self) -> AnalysisSnapshot:
        """Return current disk/overlay identity without constructing a graph."""
        with self._refresh_lock:
            disk = self._scan_disk().snapshot
            return self._overlay_snapshot(disk) if self._overlays else disk

    def disk_snapshot(self) -> AnalysisSnapshot:
        """Return current on-disk identity without constructing derived facts."""
        with self._refresh_lock:
            return self._scan_disk().snapshot

    def type_context_id(self) -> str:
        """Return configuration, lockfile, and environment identity."""
        digest = hashlib.sha256()
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
            digest.update(str(path).encode())
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
    ) -> str:
        """Return logical file/content plus transitive local dependency identity."""
        resolved = str(Path(file_path).resolve())
        return self.type_file_identities(
            [resolved],
            {resolved: content_hash} if content_hash else None,
            include_overlays=include_overlays,
        )[resolved]

    def _type_dependency_state(self, graph=None, *, include_overlays=False):
        """Resolve import dependencies without requiring graph materialization."""
        from emend.language_registry import registry_snapshot

        _, language_extensions = registry_snapshot()
        if graph is None:
            scan = self._scan_disk()
            snapshot = (self._overlay_snapshot(scan.snapshot)
                        if include_overlays and self._overlays else scan.snapshot)
            contents = scan.contents | (
                {path: value[2] for path, value in self._overlays.items()}
                if include_overlays else {}
            )
            files = self._extract_revisions(snapshot.files, contents)
            imports_by_path = {
                file.revision.file_path: [(row[1], row[2])
                                         for row in file.rows.get("imports", ())]
                for file in files
            }
        else:
            if not include_overlays and self._disk_graph is not None:
                graph = self._disk_graph
            snapshot = graph.snapshot
            imports_by_path = {
                revision.file_path: [(row.imported_module, row.imported_name)
                                     for row in graph.imports_in(graph.stored_path(revision.file_path))]
                for revision in snapshot.files
            }

        revisions = {revision.file_path: revision for revision in snapshot.files}
        module_to_revision = {
            revision.module_name.replace("::", ".").replace("/", "."): revision
            for revision in revisions.values()
        }

        suffix_to_revisions: dict[str, list[FileRevision]] = {}
        for module, revision in module_to_revision.items():
            parts = module.split(".")
            for index in range(len(parts)):
                suffix_to_revisions.setdefault(".".join(parts[index:]), []).append(
                    revision
                )

        def module_revision(name: str):
            exact = module_to_revision.get(name)
            if exact is not None:
                return exact
            matches = list(suffix_to_revisions.get(name, ()))
            parts = name.split(".")
            matches.extend(
                candidate
                for index in range(1, len(parts))
                if (candidate := module_to_revision.get(".".join(parts[index:])))
                is not None
            )
            matches = list({
                candidate.file_path: candidate for candidate in matches
            }.values())
            return matches[0] if len(matches) == 1 else None

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
            return aliases

        def source_candidates(base: Path, language: str) -> list[Path]:
            extensions = language_extensions.get(language, ())
            candidates = [base]
            candidates.extend(base.with_suffix(f".{ext}") for ext in extensions)
            if not base.suffix:
                candidates.extend(base / f"index.{ext}" for ext in extensions)
            return candidates

        dependencies: dict[str, set[str]] = {}
        ambient_typescript = {
            revision.file_path
            for revision in revisions.values()
            if revision.language == "typescript"
            and revision.file_path.endswith(".d.ts")
        }
        for revision in revisions.values():
            resolved_dependencies = (
                ambient_typescript - {revision.file_path}
                if revision.language == "typescript" else set()
            )
            for name, imported_name in imports_by_path[revision.file_path]:
                candidates = []
                if revision.language == "python":
                    if name.startswith("."):
                        package = revision.module_name
                        if Path(revision.file_path).name != "__init__.py":
                            package = package.rpartition(".")[0]
                        try:
                            name = importlib.util.resolve_name(name, package)
                        except (ImportError, ValueError):
                            pass
                    name = name.lstrip(".").replace("::", ".").replace("/", ".")
                    candidates.append(module_revision(name))
                    if imported_name not in (None, "*"):
                        candidates.append(module_revision(
                            f"{name}.{imported_name}"
                        ))
                elif name.startswith("."):
                    base = (Path(revision.file_path).parent / name).resolve()
                    # TypeScript commonly imports emitted ``.js`` names whose
                    # source dependency is ``.ts``/``.tsx``.
                    paths = source_candidates(base, revision.language)
                    candidates.extend(revisions.get(str(path)) for path in paths)
                else:
                    if revision.language == "typescript" and ts_paths:
                        candidates.extend(
                            revisions.get(str(path))
                            for base in typescript_aliases(name)
                            for path in source_candidates(base, revision.language)
                        )
                    normalized = name.replace("::", ".").replace("/", ".")
                    candidates.append(module_revision(normalized))
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
    ) -> dict[str, str]:
        """Compute cache identities against one source generation."""
        with self._refresh_lock:
            revisions, dependencies = self._type_dependency_state(
                graph, include_overlays=include_overlays
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
    ) -> tuple[dict[str, str], dict[str, str], set[str]]:
        """Capture identities, transitive sources, and project file membership."""
        with self._refresh_lock:
            paths = [str(Path(path).resolve()) for path in file_paths]
            revisions, dependencies = self._type_dependency_state(
                graph, include_overlays=include_overlays
            )
            paths = [path for path in paths if path in revisions]
            identities = self._type_identities(
                paths, None, revisions, dependencies
            )
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
                set(revisions),
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
        with self._refresh_lock:
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
        with self._refresh_lock:
            resolved = str(Path(file_path).resolve())
            current = self._overlays.get(resolved)
            accepted = current is not None and (
                owner is None or current[0] is owner
            ) and (version is None or version >= current[1])
            if accepted:
                del self._overlays[resolved]
                if not self._overlays:
                    self._discard_overlay_graph()
            return OverlayUpdate(
                resolved, version, accepted,
                None if accepted else current[1] if current else None,
            )

    def remove_overlays(self, owner: object) -> int:
        """Release every overlay still owned by one editor session."""
        with self._refresh_lock:
            paths = [path for path, value in self._overlays.items()
                     if value[0] is owner]
            for path in paths:
                del self._overlays[path]
            if paths and not self._overlays:
                self._discard_overlay_graph()
            return len(paths)

    def overlay_content(self, file_path: str | Path) -> str | None:
        with self._refresh_lock:
            value = self._overlays.get(str(Path(file_path).resolve()))
            return value[2] if value else None
