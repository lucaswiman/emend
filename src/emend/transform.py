"""Transform engine for extended selectors."""
from __future__ import annotations
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
import ast
import difflib
import hashlib
import logging
import threading
import libcst as cst
from libcst import matchers as m
from dataclasses import dataclass
import re
import sys
import io
import json
import time
from .component_selector import ExtendedSelector, parse_extended_selector
from .pattern import parse_pattern, compile_pattern_to_matcher, Pattern, is_oracle_type_constraint, parse_oracle_type_constraint

if TYPE_CHECKING:
    from .type_oracle import TypeOracle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module parse cache: avoids re-parsing the same source text
# ---------------------------------------------------------------------------
# Two-tier cache:
#   1. In-memory dict (fast, lost on exit)
#   2. On-disk SQLite DB at .emend/cache/parse.db (persists across runs)
# Key: md5 of source text.  Value: compressed-pickled LibCST Module.
_parse_cache: dict[bytes, cst.Module] = {}
_parse_cache_lock = threading.Lock()
_PARSE_CACHE_MAX = 256

# Disk cache (SQLite) — lazily initialized on first use.
_disk_cache_conn: sqlite3.Connection | None = None
_disk_cache_lock = threading.Lock()
_disk_cache_checked = False


def _get_disk_cache() -> sqlite3.Connection | None:
    """Return a thread-safe SQLite connection for the parse cache, or None."""
    global _disk_cache_conn, _disk_cache_checked
    if _disk_cache_checked:
        return _disk_cache_conn
    with _disk_cache_lock:
        if _disk_cache_checked:
            return _disk_cache_conn
        _disk_cache_checked = True
        try:
            import sqlite3
            root = _find_project_root(".")
            cache_dir = Path(root) / ".emend" / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            # Ensure ignore files exist so cache DB is never checked in
            _ensure_cache_ignore_files(root)
            db_path = cache_dir / "parse.db"
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS parse_cache "
                "(hash BLOB PRIMARY KEY, data BLOB)"
            )
            conn.commit()
            _disk_cache_conn = conn
            logger.debug("disk parse cache opened at %s", db_path)
        except Exception as exc:
            logger.debug("disk parse cache unavailable: %s", exc)
            _disk_cache_conn = None
    return _disk_cache_conn


def _disk_cache_get(key: bytes) -> cst.Module | None:
    """Look up a parsed module in the disk cache."""
    conn = _get_disk_cache()
    if conn is None:
        return None
    try:
        import pickle
        import zlib
        row = conn.execute(
            "SELECT data FROM parse_cache WHERE hash = ?", (key,)
        ).fetchone()
        if row is not None:
            return pickle.loads(zlib.decompress(row[0]))
    except Exception:
        pass
    return None


def _disk_cache_put(key: bytes, module: cst.Module) -> None:
    """Store a parsed module in the disk cache (best-effort)."""
    conn = _get_disk_cache()
    if conn is None:
        return
    try:
        import pickle
        import zlib
        data = zlib.compress(
            pickle.dumps(module, protocol=pickle.HIGHEST_PROTOCOL), level=1
        )
        with _disk_cache_lock:
            conn.execute(
                "INSERT OR REPLACE INTO parse_cache VALUES (?, ?)", (key, data)
            )
            conn.commit()
    except Exception:
        pass


def _cached_parse(source: str) -> cst.Module:
    """Parse Python source into a LibCST Module, with two-tier caching.

    Checks in-memory cache first, then disk cache, then parses.
    Thread-safe for use with ThreadPoolExecutor.
    """
    key = hashlib.md5(source.encode(), usedforsecurity=False).digest()
    # Tier 1: in-memory
    with _parse_cache_lock:
        cached = _parse_cache.get(key)
    if cached is not None:
        return cached
    # Tier 2: disk
    module = _disk_cache_get(key)
    if module is None:
        # Parse from scratch and write to disk cache
        module = cst.parse_module(source)
        _disk_cache_put(key, module)
    # Store in memory cache
    with _parse_cache_lock:
        if len(_parse_cache) >= _PARSE_CACHE_MAX:
            keys_to_evict = list(_parse_cache.keys())[:_PARSE_CACHE_MAX // 4]
            for k in keys_to_evict:
                del _parse_cache[k]
        _parse_cache[key] = module
    return module


# ---------------------------------------------------------------------------
# Qualified-name index cache: per-file set of QN strings for pre-filtering
# ---------------------------------------------------------------------------
# After the first cross-project operation populates this cache, subsequent
# operations can skip MetadataWrapper for files whose QN set doesn't overlap
# with the target.  Content-hash keyed, persisted in the same SQLite DB.

def _ensure_qn_table() -> None:
    """Create the qn_index table if it doesn't exist (idempotent)."""
    conn = _get_disk_cache()
    if conn is None:
        return
    try:
        with _disk_cache_lock:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS qn_index "
                "(hash BLOB PRIMARY KEY, qnames BLOB)"
            )
            conn.commit()
    except Exception:
        pass

_qn_table_ready = False


def _get_cached_qnames(content_hash: bytes) -> set[str] | None:
    """Look up cached qualified-name set for a file by content hash."""
    global _qn_table_ready
    conn = _get_disk_cache()
    if conn is None:
        return None
    if not _qn_table_ready:
        _ensure_qn_table()
        _qn_table_ready = True
    try:
        import pickle
        import zlib
        row = conn.execute(
            "SELECT qnames FROM qn_index WHERE hash = ?", (content_hash,)
        ).fetchone()
        if row is not None:
            return pickle.loads(zlib.decompress(row[0]))
    except Exception:
        pass
    return None


def _store_qnames(content_hash: bytes, qnames: set[str]) -> None:
    """Cache a file's qualified-name set (best-effort)."""
    global _qn_table_ready
    conn = _get_disk_cache()
    if conn is None:
        return
    if not _qn_table_ready:
        _ensure_qn_table()
        _qn_table_ready = True
    try:
        import pickle
        import zlib
        data = zlib.compress(
            pickle.dumps(qnames, protocol=pickle.HIGHEST_PROTOCOL), level=1
        )
        with _disk_cache_lock:
            conn.execute(
                "INSERT OR REPLACE INTO qn_index VALUES (?, ?)",
                (content_hash, data),
            )
            conn.commit()
    except Exception:
        pass


class _QNCollector(cst.CSTVisitor):
    """Lightweight visitor that collects all qualified-name strings in a file.

    Designed to run as a second pass on an already-resolved MetadataWrapper
    (so QualifiedNameProvider metadata is already computed — this just reads it).
    """
    METADATA_DEPENDENCIES = (cst.metadata.QualifiedNameProvider,)

    def __init__(self) -> None:
        self.all_qnames: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        try:
            for qn in self.get_metadata(cst.metadata.QualifiedNameProvider, node):
                self.all_qnames.add(qn.name)
        except KeyError:
            pass

    def visit_Attribute(self, node: cst.Attribute) -> None:
        try:
            for qn in self.get_metadata(cst.metadata.QualifiedNameProvider, node):
                self.all_qnames.add(qn.name)
        except KeyError:
            pass


def _index_batch(args: tuple[str, list[tuple[str, str]]]) -> tuple[int, int]:
    """Worker function for process-pool indexing.

    Runs in a subprocess.  Parses a batch of files, resolves qualified
    names, and writes directly to the SQLite disk cache — avoiding the
    cost of serialising LibCST modules back to the main process.

    Args:
        args: (db_path, [(file_path, content), ...])

    Returns:
        (parse_count, qn_count) — number of entries written.
    """
    import pickle
    import sqlite3
    import zlib

    db_path, file_batch = args
    parse_rows: list[tuple[bytes, bytes]] = []
    qn_rows: list[tuple[bytes, bytes]] = []

    for _py_file, content in file_batch:
        content_hash = hashlib.md5(
            content.encode(), usedforsecurity=False
        ).digest()

        # Parse
        try:
            module = cst.parse_module(content)
        except Exception:
            continue

        try:
            parse_blob = zlib.compress(
                pickle.dumps(module, protocol=pickle.HIGHEST_PROTOCOL), level=1
            )
            parse_rows.append((content_hash, parse_blob))
        except Exception:
            pass

        # QN index
        try:
            wrapper = cst.metadata.MetadataWrapper(module)
            collector = _QNCollector()
            wrapper.visit(collector)
            qn_blob = zlib.compress(
                pickle.dumps(collector.all_qnames, protocol=pickle.HIGHEST_PROTOCOL),
                level=1,
            )
            qn_rows.append((content_hash, qn_blob))
        except Exception:
            pass

    # Bulk-write to SQLite from this worker process.
    # WAL mode allows concurrent readers/writers across processes.
    if parse_rows or qn_rows:
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            if parse_rows:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS parse_cache "
                    "(hash BLOB PRIMARY KEY, data BLOB)"
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO parse_cache VALUES (?, ?)", parse_rows
                )
            if qn_rows:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS qn_index "
                    "(hash BLOB PRIMARY KEY, qnames BLOB)"
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO qn_index VALUES (?, ?)", qn_rows
                )
            conn.commit()
            conn.close()
        except Exception:
            pass

    return (len(parse_rows), len(qn_rows))


def warm_caches(
    project_path: str = ".",
    *,
    jobs: int | None = None,
    callback: Callable[[str, str], None] | None = None,
) -> dict[str, int]:
    """Pre-populate the parse and QN-index caches for all project files.

    Designed to be called from the ``emend index`` CLI command or at MCP
    server start-up.  Each file is parsed, then QualifiedNameProvider is
    resolved to build the QN index.

    Uses a ``ProcessPoolExecutor`` so that LibCST parsing (CPU-bound)
    runs across multiple cores without GIL contention.  Files are split
    into batches; each worker process parses its batch and writes results
    directly to the SQLite disk cache (WAL mode allows concurrent writers),
    avoiding the overhead of serialising LibCST modules back to the main
    process.

    Args:
        project_path: Root directory of the project.
        jobs: Max parallelism (defaults to CPU count).
        callback: Called with ``(phase, file_path)`` for progress reporting.

    Returns:
        Dict with stats: ``{"files", "parse_cached", "qn_cached"}``.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    project_root = _find_project_root(project_path)
    # Collect files from the user-specified path (not the project root)
    # so that `emend index src/` only indexes src/, not the entire repo.
    scan_root = str(Path(project_path).resolve())
    py_files = _collect_python_files_scandir(scan_root)
    logger.info("warm_caches: %d python files in %s", len(py_files), scan_root)

    max_workers = jobs or multiprocessing.cpu_count() or 4

    # Phase 1: read all files (Rust parallel I/O)
    t0 = time.monotonic()
    file_contents = _rust.read_and_filter_files(py_files, [])
    logger.info("warm_caches: read %d files in %.3fs", len(file_contents), time.monotonic() - t0)

    stats = {"files": len(file_contents), "parse_cached": 0, "qn_cached": 0}

    # Phase 2: parse + QN index in subprocesses.
    # Ensure the DB exists before spawning workers.
    _get_disk_cache()
    _ensure_qn_table()

    # Resolve the DB path so workers can open their own connections.
    cache_dir = Path(project_root) / ".emend" / "cache"
    db_path = str(cache_dir / "parse.db")

    # Split files into batches — one batch per worker.
    batch_size = max(1, len(file_contents) // max_workers)
    batches: list[tuple[str, list[tuple[str, str]]]] = []
    for i in range(0, len(file_contents), batch_size):
        chunk = file_contents[i : i + batch_size]
        batches.append((db_path, chunk))

    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        # TODO: Conditionally use ProcessPoolExecutor or ThreadPoolExecutor for GIL-python vs free-threaded.
        for batch_idx, (parse_n, qn_n) in enumerate(
            executor.map(_index_batch, batches)
        ):
            stats["parse_cached"] += parse_n
            stats["qn_cached"] += qn_n
            # Report progress for all files in this batch
            if callback:
                _db_path, chunk = batches[batch_idx]
                for py_file, _content in chunk:
                    callback("index", py_file)

    logger.info(
        "warm_caches: indexed %d files in %.3fs (parse=%d, qn=%d)",
        stats["files"], time.monotonic() - t0,
        stats["parse_cached"], stats["qn_cached"],
    )

    # Ensure the cache directory has ignore files so it doesn't get checked
    # in or built into Docker images.
    _ensure_cache_ignore_files(project_root)

    return stats


def _ensure_cache_ignore_files(project_root: str) -> None:
    """Create .gitignore and .dockerignore in the cache directory."""
    cache_dir = Path(project_root) / ".emend" / "cache"
    if not cache_dir.is_dir():
        return
    gitignore = cache_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Auto-generated by emend index\n*\n")
    dockerignore = cache_dir / ".dockerignore"
    if not dockerignore.exists():
        dockerignore.write_text("# Auto-generated by emend index\n*\n")


# ---------------------------------------------------------------------------
# Rust accelerator (bundled with the emend wheel via maturin)
# ---------------------------------------------------------------------------
from emend import emend_core as _rust

_METAVAR_RE = re.compile(r'\$(?:\.\.\.)?[A-Z_][A-Z_0-9]*')


def extract_pattern_literals(pattern_str: str) -> list[str]:
    """Extract literal identifier tokens from a pattern string for pre-filtering.

    For a pattern like "$X.objects.filter($...ARGS)", returns ["objects", "filter"].
    These can be used with Rust filter_files_by_content to quickly eliminate files
    that cannot possibly match the pattern.
    """
    # Remove metavariables
    cleaned = _METAVAR_RE.sub('', pattern_str)
    # Extract identifier-like tokens (Python identifiers)
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z_0-9]*', cleaned)
    # Filter out Python keywords and very short tokens that would match too broadly
    _PY_KEYWORDS = {'if', 'else', 'elif', 'for', 'while', 'try', 'except',
                    'finally', 'with', 'as', 'import', 'from', 'class', 'def',
                    'return', 'yield', 'raise', 'pass', 'break', 'continue',
                    'and', 'or', 'not', 'in', 'is', 'True', 'False', 'None',
                    'lambda', 'global', 'nonlocal', 'del', 'assert', 'async',
                    'await'}
    return [t for t in tokens if t not in _PY_KEYWORDS and len(t) > 1]


# Helper functions for cross-project operations

def _find_project_root(start_path: str) -> str:
    """Find project root by looking for markers."""
    path = Path(start_path).resolve()
    if path.is_file():
        path = path.parent

    markers = ['.git', 'pyproject.toml', 'setup.py', 'setup.cfg']

    current = path
    while current != current.parent:
        for marker in markers:
            if (current / marker).exists():
                return str(current)
        current = current.parent

    return str(path)


@lru_cache(maxsize=64)
def _find_python_source_root(project_root: str) -> str:
    """Find the Python source root directory for a project.

    Detects ``src/`` layout by checking (in order):
    1. ``pyproject.toml`` settings (maturin, setuptools, hatch)
    2. ``setup.cfg`` [options] package_dir
    3. Heuristic: ``src/`` exists and contains a package (dir with ``__init__.py``)

    Returns the resolved source root (e.g. ``/repo/src``), or the
    project root itself if no ``src/`` layout is detected.
    """
    root = Path(project_root).resolve()

    # --- pyproject.toml -------------------------------------------------
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
        except ModuleNotFoundError:          # Python < 3.11
            try:
                import tomli as tomllib      # type: ignore[no-redef]
            except ModuleNotFoundError:
                tomllib = None               # type: ignore[assignment]
        if tomllib is not None:
            try:
                data = tomllib.loads(pyproject.read_text())
                # maturin: python-source = "src"
                ps = (data.get("tool", {}).get("maturin", {})
                      .get("python-source"))
                if ps:
                    candidate = root / ps
                    if candidate.is_dir():
                        return str(candidate)
                # setuptools: [tool.setuptools.packages.find] where = ["src"]
                where = (data.get("tool", {}).get("setuptools", {})
                         .get("packages", {}).get("find", {}).get("where"))
                if isinstance(where, list) and where:
                    candidate = root / where[0]
                    if candidate.is_dir():
                        return str(candidate)
                # hatch / hatchling
                where = (data.get("tool", {}).get("hatch", {})
                         .get("build", {}).get("sources", {}).get("src"))
                if isinstance(where, str):
                    candidate = root / where
                    if candidate.is_dir():
                        return str(candidate)
            except Exception:
                pass

    # --- setup.cfg ------------------------------------------------------
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        try:
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(str(setup_cfg))
            pkg_dir = cfg.get("options", "package_dir", fallback=None)
            if pkg_dir:
                # Format: "= src" or "\n= src"
                for part in pkg_dir.splitlines():
                    part = part.strip()
                    if part.startswith("="):
                        src_dir = part[1:].strip()
                        candidate = root / src_dir
                        if candidate.is_dir():
                            return str(candidate)
        except Exception:
            pass

    # --- Heuristic: src/ with an __init__.py package --------------------
    src_dir = root / "src"
    if src_dir.is_dir():
        for child in src_dir.iterdir():
            if child.is_dir() and (child / "__init__.py").is_file():
                return str(src_dir)

    return str(root)


def _file_to_module(file_path: str, project_path: str | None) -> str:
    """Convert file path to Python module name.

    Detects ``src/`` layout automatically so that
    ``src/pkg/mod.py`` becomes ``pkg.mod`` rather than ``src.pkg.mod``.
    """
    abs_file = Path(file_path).resolve()
    proj_root = Path(project_path or _find_project_root(file_path)).resolve()
    source_root = Path(_find_python_source_root(str(proj_root)))

    # Use the source root if the file lives under it; otherwise fall
    # back to the project root (e.g. for test files outside src/).
    try:
        rel_path = abs_file.relative_to(source_root)
    except ValueError:
        rel_path = abs_file.relative_to(proj_root)

    module_parts = list(rel_path.parts[:-1]) + [rel_path.stem]
    return '.'.join(module_parts)


# Non-dot directories to skip.  All directories starting with '.' are
# skipped automatically by the Rust scanner (emend_core.collect_python_files).
# The canonical list lives in Rust (scanner.rs); we import it here so
# Python and Rust always agree.
_SKIP_DIRS = frozenset(_rust.skip_dirs())

# Module-level file-list cache: maps resolved project root to (mtime_ns, file_list)
_file_list_cache: dict[str, tuple[int, list[str]]] = {}


def _collect_python_files_scandir(root_path: str) -> list[str]:
    """Walk a directory tree using the Rust emend_core module."""
    return _rust.collect_python_files(root_path)


def _collect_git_tracked_python_files(project_root: str) -> list[str] | None:
    """Return git-tracked .py files, or None if not in a git repo."""
    import subprocess
    resolved = str(Path(project_root).resolve())
    try:
        result = subprocess.run(
            ['git', 'ls-files', '-z', '*.py'],
            capture_output=True, timeout=10,
            cwd=resolved,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout
        if not raw:
            return []
        # git ls-files -z uses null separators; paths are relative to cwd
        rel_paths = raw.decode('utf-8', errors='replace').split('\0')
        abs_paths = []
        for p in rel_paths:
            p = p.strip()
            if p:
                abs_paths.append(str(Path(resolved) / p))
        return abs_paths
    except Exception:
        return None


def _collect_python_files(project_root: str, git_tracked_only: bool = False) -> list[str]:
    """Collect all Python files in project, with caching.

    Uses os.scandir for speed. Caches the file list per project root,
    invalidated when the root directory's mtime changes (which happens
    when files are added or removed).

    If *git_tracked_only* is True, uses ``git ls-files`` to only return
    files tracked by git.  Falls back to directory scan if not in a
    git repository.
    """
    if git_tracked_only:
        tracked = _collect_git_tracked_python_files(project_root)
        if tracked is not None:
            logger.info("collect_python_files: %d git-tracked files in %s", len(tracked), project_root)
            return tracked

    import os
    resolved = str(Path(project_root).resolve())
    try:
        root_mtime = os.stat(resolved).st_mtime_ns
    except OSError:
        t0 = time.monotonic()
        files = _collect_python_files_scandir(resolved)
        logger.info("collect_python_files: %d files in %.3fs (scandir, %s)", len(files), time.monotonic() - t0, resolved)
        return files

    cached = _file_list_cache.get(resolved)
    if cached is not None and cached[0] == root_mtime:
        logger.debug("collect_python_files: %d files (cached, %s)", len(cached[1]), resolved)
        return cached[1]

    t0 = time.monotonic()
    files = _collect_python_files_scandir(resolved)
    logger.info("collect_python_files: %d files in %.3fs (%s)", len(files), time.monotonic() - t0, resolved)
    _file_list_cache[resolved] = (root_mtime, files)
    return files


def _files_importing_module(project_root: str, module_dotted: str) -> set[str] | None:
    """Return the set of files that import from *module_dotted*, or None if unknown.

    Uses the Rust targeted import filter: text-prefilters then tree-sitter-parses
    only candidate files, avoiding building the full import graph.

    Returns None if the filter cannot be applied (caller should fall back
    to scanning all files).
    """
    py_files = _collect_python_files(project_root)
    try:
        matching = _rust.files_importing_module(py_files, module_dotted)
        return set(matching)
    except Exception:
        return None


def prefilter_files_structural(files: list[str], name: str) -> list[str]:
    """Structural pre-filter: use tree-sitter to find files containing
    an actual identifier matching name (not just substring in strings/comments).
    """
    matches = _rust.find_name_in_files(files, name)
    return list({m.file for m in matches})


def visit_project(
    name_hint: str,
    visitor_factory: Callable[[str, bool], cst.CSTVisitor | cst.CSTTransformer],
    project_path: str | None = None,
    metadata_providers: Sequence = (),
    target_file: str | None = None,
    candidate_files: set[str] | None = None,
    target_qnames: set[str] | None = None,
) -> Iterator[tuple[str, cst.Module, object]]:
    """Iterate over Python files in the project, yielding (file_path, module, visitor).

    Args:
        name_hint: A string that must appear in the file text for pre-filtering.
        visitor_factory: Called with (file_path, is_definition_file) -> visitor.
        project_path: Project root directory.
        metadata_providers: LibCST metadata providers to use with MetadataWrapper.
        target_file: The resolved path of the file defining the symbol (for is_def_file).
        candidate_files: If provided, only visit these files (pre-filtered by import graph).
        target_qnames: If provided, use QN index cache to skip files whose cached
                       qualified-name set has no overlap with these names.
    """
    t_start = time.monotonic()
    project_root = project_path or "."
    py_files = _collect_python_files(project_root)
    logger.info("visit_project: %d python files collected from %s", len(py_files), project_root)
    if candidate_files is not None:
        # Pre-filter to only files in the candidate set
        # Always include the target_file itself (definition file)
        before = len(py_files)
        py_files = [f for f in py_files
                    if f in candidate_files
                    or (target_file and str(Path(f).resolve()) == target_file)]
        logger.info("visit_project: candidate_files filter %d -> %d", before, len(py_files))

    # Structural pre-filter for cross-project ops: use tree-sitter to find
    # files with actual identifier matches (eliminates strings/comments false positives)
    if metadata_providers and name_hint:
        before = len(py_files)
        t0 = time.monotonic()
        py_files = prefilter_files_structural(py_files, name_hint)
        logger.info("visit_project: structural prefilter %d -> %d in %.3fs (hint=%r)", before, len(py_files), time.monotonic() - t0, name_hint)
        # Re-add target_file if it was filtered out (definition file must always be visited)
        if target_file and target_file not in py_files:
            py_files.append(target_file)

    # Batch read + filter in Rust (parallel I/O + substring pre-filter)
    hints = [name_hint] if name_hint and not metadata_providers else []
    t0 = time.monotonic()
    file_contents = _rust.read_and_filter_files(py_files, hints)
    logger.info("visit_project: read_and_filter %d -> %d files in %.3fs (hints=%r)", len(py_files), len(file_contents), time.monotonic() - t0, hints)

    # Ensure target_file is always included (definition file must be visited)
    if target_file and not metadata_providers:
        seen = {str(Path(f).resolve()) for f, _ in file_contents}
        if target_file not in seen:
            try:
                content = Path(target_file).read_text()
                file_contents.append((target_file, content))
            except Exception:
                pass

    # QN-index pre-filter: skip files whose cached qualified-name set
    # has no overlap with the target QNs.  Files without cached data are
    # kept (they'll be cached after MetadataWrapper runs).
    _uses_qnp = cst.metadata.QualifiedNameProvider in metadata_providers
    if target_qnames and _uses_qnp:
        before = len(file_contents)
        t0 = time.monotonic()
        filtered_contents: list[tuple[str, str]] = []
        n_cache_hits = 0
        for py_file, content in file_contents:
            # Always visit the definition file
            if target_file and str(Path(py_file).resolve()) == target_file:
                filtered_contents.append((py_file, content))
                continue
            content_hash = hashlib.md5(
                content.encode(), usedforsecurity=False
            ).digest()
            cached_qns = _get_cached_qnames(content_hash)
            if cached_qns is not None:
                n_cache_hits += 1
                if not target_qnames.intersection(cached_qns):
                    continue  # no overlap → skip
            filtered_contents.append((py_file, content))
        logger.info(
            "visit_project: qn_index filter %d -> %d in %.3fs "
            "(%d cache hits)",
            before, len(filtered_contents),
            time.monotonic() - t0, n_cache_hits,
        )
        file_contents = filtered_contents

    if metadata_providers and len(file_contents) > 1:
        # Parallel path: each MetadataWrapper is independent per file — no shared state
        def _visit_one(args: tuple[str, str]) -> tuple[str, cst.Module, object] | None:
            py_file, content = args
            is_def_file = (target_file is not None
                           and str(Path(py_file).resolve()) == target_file)
            try:
                module = _cached_parse(content)
                visitor = visitor_factory(py_file, is_def_file)
                wrapper = cst.metadata.MetadataWrapper(module)
                result_module = wrapper.visit(visitor)
                # Cache QN data as a side-effect (second pass is cheap
                # because QualifiedNameProvider is already resolved).
                if _uses_qnp:
                    try:
                        collector = _QNCollector()
                        wrapper.visit(collector)
                        content_hash = hashlib.md5(
                            content.encode(), usedforsecurity=False
                        ).digest()
                        _store_qnames(content_hash, collector.all_qnames)
                    except Exception:
                        pass
                return (py_file, result_module, visitor)
            except Exception:
                return None

        t0 = time.monotonic()
        n_visited = 0
        with ThreadPoolExecutor() as executor:
            for result in executor.map(_visit_one, file_contents):
                if result is not None:
                    n_visited += 1
                    yield result
        logger.info("visit_project: parallel visit %d files in %.3fs (total %.3fs)", n_visited, time.monotonic() - t0, time.monotonic() - t_start)
    else:
        # Sequential path: no metadata providers or single file
        t0 = time.monotonic()
        n_visited = 0
        for py_file, content in file_contents:
            is_def_file = (target_file is not None
                           and str(Path(py_file).resolve()) == target_file)
            try:
                t_file = time.monotonic()
                module = _cached_parse(content)
                visitor = visitor_factory(py_file, is_def_file)
                if metadata_providers:
                    wrapper = cst.metadata.MetadataWrapper(module)
                    result_module = wrapper.visit(visitor)
                    # Cache QN data
                    if _uses_qnp:
                        try:
                            collector = _QNCollector()
                            wrapper.visit(collector)
                            content_hash = hashlib.md5(
                                content.encode(), usedforsecurity=False
                            ).digest()
                            _store_qnames(content_hash, collector.all_qnames)
                        except Exception:
                            pass
                else:
                    result_module = module.visit(visitor)
                logger.debug("visit_project: visited %s in %.3fs", py_file, time.monotonic() - t_file)
                n_visited += 1
                yield (py_file, result_module, visitor)
            except Exception:
                continue
        logger.info("visit_project: sequential visit %d files in %.3fs (total %.3fs)", n_visited, time.monotonic() - t0, time.monotonic() - t_start)


class SymbolFinder(cst.CSTVisitor):
    """Visitor to find a symbol by path in the CST."""

    def __init__(self, target_path: list[str]):
        self.target_path = target_path
        self.current_path: list[str] = []
        self.found_node: cst.FunctionDef | cst.ClassDef | None = None

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        """Visit class definition."""
        self.current_path.append(node.name.value)
        if self.current_path == self.target_path:
            self.found_node = node
            return False  # Stop traversal
        return True

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        """Leave class definition."""
        if self.current_path:
            self.current_path.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        """Visit function definition."""
        self.current_path.append(node.name.value)
        if self.current_path == self.target_path:
            self.found_node = node
            return False  # Stop traversal
        return True

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        """Leave function definition."""
        if self.current_path:
            self.current_path.pop()


def _get_all_params(params: cst.Parameters) -> list[cst.Param]:
    """Get all parameters from a Parameters node."""
    all_params = []
    if hasattr(params, 'posonly_params'):
        all_params.extend(params.posonly_params)
    all_params.extend(params.params)
    if params.star_arg and isinstance(params.star_arg, cst.Param):
        all_params.append(params.star_arg)
    all_params.extend(params.kwonly_params)
    if params.star_kwarg:
        all_params.append(params.star_kwarg)
    return all_params


def _param_to_string(param: cst.Param) -> str:
    """Convert parameter to string without trailing comma."""
    # Remove the trailing comma from the parameter
    param_without_comma = param.with_changes(comma=cst.MaybeSentinel.DEFAULT)
    return cst.Module([]).code_for_node(param_without_comma).strip()


def _find_param_by_name(params: list[cst.Param], name: str) -> cst.Param | None:
    """Find a parameter by name."""
    for param in params:
        if param.name.value == name:
            return param
    return None


def _get_decorator_name(decorator: cst.Decorator) -> str:
    """Extract decorator name from decorator node."""
    dec = decorator.decorator
    if isinstance(dec, cst.Name):
        return dec.value
    elif isinstance(dec, cst.Attribute):
        # For @module.decorator
        return cst.Module([]).code_for_node(dec).strip()
    elif isinstance(dec, cst.Call):
        # For @decorator() or @decorator(args)
        if isinstance(dec.func, cst.Name):
            return dec.func.value
        elif isinstance(dec.func, cst.Attribute):
            return cst.Module([]).code_for_node(dec.func).strip()
    return ""


def _find_decorator_by_name(decorators: list[cst.Decorator], name: str) -> cst.Decorator | None:
    """Find a decorator by name."""
    for decorator in decorators:
        dec_name = _get_decorator_name(decorator)
        if dec_name == name:
            return decorator
    return None


def _get_base_name(base: cst.Arg) -> str:
    """Extract base class name from base argument."""
    return cst.Module([]).code_for_node(base.value).strip()


def _find_base_by_name(bases: list[cst.Arg], name: str) -> cst.Arg | None:
    """Find a base class by name."""
    for base in bases:
        if _get_base_name(base) == name:
            return base
    return None


def _get_imports(module: cst.Module) -> str:
    """Extract all import statements from a module."""
    imports = []
    for stmt in module.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            # Check if it contains import statements
            for item in stmt.body:
                if isinstance(item, (cst.Import, cst.ImportFrom)):
                    imports.append(module.code_for_node(stmt).strip())
                    break
    return "\n".join(imports)


def _add_import(
    module: cst.Module,
    import_str: str,
    position: int,
    file_path: Path,
    apply: bool,
    source_code: str
) -> str:
    """Add an import statement to a module.

    Args:
        module: Parsed CST module
        import_str: Import statement to add (e.g., "import os")
        position: 0 for prepend, -1 for append
        file_path: Path to the file
        apply: Whether to apply changes
        source_code: Original source code

    Returns:
        Unified diff showing changes
    """
    # Parse the import statement
    import_stmt = cst.parse_statement(import_str)

    # Find the last import in the module
    last_import_idx = -1
    first_import_idx = -1
    for i, stmt in enumerate(module.body):
        if isinstance(stmt, cst.SimpleStatementLine):
            for item in stmt.body:
                if isinstance(item, (cst.Import, cst.ImportFrom)):
                    if first_import_idx == -1:
                        first_import_idx = i
                    last_import_idx = i
                    break

    # Build new body
    new_body = list(module.body)

    if position == 0:
        # Prepend: insert at the beginning or after __future__ imports
        insert_idx = 0
        # Check for __future__ imports and insert after them
        for i, stmt in enumerate(module.body):
            if isinstance(stmt, cst.SimpleStatementLine):
                for item in stmt.body:
                    if isinstance(item, cst.ImportFrom):
                        if item.module and cst.Module([]).code_for_node(item.module) == "__future__":
                            insert_idx = i + 1
                            break
        new_body.insert(insert_idx, import_stmt)
    else:
        # Append: insert after the last import or at the beginning if no imports
        if last_import_idx >= 0:
            new_body.insert(last_import_idx + 1, import_stmt)
        else:
            # No imports yet, add at the beginning
            new_body.insert(0, import_stmt)

    new_module = module.with_changes(body=new_body)
    new_code = new_module.code

    # Generate diff
    diff = _generate_diff(str(file_path), source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def get_component(selector: ExtendedSelector) -> str:
    """Get value of component.

    Args:
        selector: Extended selector with component specified

    Returns:
        String representation of the component value

    Example:
        >>> sel = parse_extended_selector("file.py::func[params]")
        >>> get_component(sel)
        'ctx, request'

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found, invalid component for symbol type,
                   or accessor not found
    """
    # Read and parse file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()
    module = cst.parse_module(source_code)

    # Handle module-level components (empty symbol_path)
    if not selector.symbol_path:
        if selector.component == "imports":
            return _get_imports(module)
        else:
            raise ValueError(f"Component '{selector.component}' requires a symbol path")

    # Find target symbol
    finder = SymbolFinder(selector.symbol_path)
    module.visit(finder)

    if finder.found_node is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    node = finder.found_node

    # Extract component value
    component = selector.component
    accessor = selector.accessor

    # Validate component for symbol type
    if isinstance(node, cst.ClassDef):
        if component in ("params", "returns"):
            raise ValueError(f"Component '{component}' not valid for ClassDef")
    elif isinstance(node, cst.FunctionDef):
        if component == "bases":
            raise ValueError(f"Component '{component}' not valid for FunctionDef")

    # Extract component
    if component == "params":
        if not isinstance(node, cst.FunctionDef):
            raise ValueError(f"Component 'params' not valid for {type(node).__name__}")

        all_params = _get_all_params(node.params)

        if accessor is None:
            # Return all params comma-separated, including * separator
            if not all_params:
                return ""

            # Build complete params string including / and * separators
            parts = []

            # Positional-only params (before /)
            if hasattr(node.params, 'posonly_params') and node.params.posonly_params:
                for p in node.params.posonly_params:
                    parts.append(_param_to_string(p))
                parts.append("/")

            # Regular params
            for p in node.params.params:
                parts.append(_param_to_string(p))

            # Star arg - could be *args (Param) or bare * (ParamStar)
            if node.params.star_arg is not None:
                if isinstance(node.params.star_arg, cst.Param):
                    parts.append(_param_to_string(node.params.star_arg))
                elif isinstance(node.params.star_arg, cst.ParamStar):
                    parts.append("*")

            # Keyword-only params
            for p in node.params.kwonly_params:
                parts.append(_param_to_string(p))

            # Star kwarg (**kwargs)
            if node.params.star_kwarg:
                parts.append(_param_to_string(node.params.star_kwarg))

            return ", ".join(parts)
        elif isinstance(accessor, int):
            # Return param by index
            try:
                return _param_to_string(all_params[accessor])
            except IndexError as e:
                raise ValueError(f"Parameter index {accessor} out of range") from e
        else:
            # Return param by name
            param = _find_param_by_name(all_params, accessor)
            if param is None:
                raise ValueError(f"Parameter '{accessor}' not found")
            return _param_to_string(param)

    elif component == "returns":
        if not isinstance(node, cst.FunctionDef):
            raise ValueError(f"Component 'returns' not valid for {type(node).__name__}")

        if node.returns is None:
            raise ValueError(f"Function {'.'.join(selector.symbol_path)} has no return annotation")

        return cst.Module([]).code_for_node(node.returns.annotation).strip()

    elif component == "decorators":
        decorators = list(node.decorators) if hasattr(node, 'decorators') else []

        if accessor is None:
            # Return all decorators newline-separated
            if not decorators:
                return ""
            return "\n".join(cst.Module([]).code_for_node(d).strip() for d in decorators)
        elif isinstance(accessor, int):
            # Return decorator by index
            try:
                return cst.Module([]).code_for_node(decorators[accessor]).strip()
            except IndexError as e:
                raise ValueError(f"Decorator index {accessor} out of range") from e
        else:
            # Return decorator by name
            decorator = _find_decorator_by_name(decorators, accessor)
            if decorator is None:
                raise ValueError(f"Decorator '{accessor}' not found")
            return cst.Module([]).code_for_node(decorator).strip()

    elif component == "bases":
        if not isinstance(node, cst.ClassDef):
            raise ValueError(f"Component 'bases' not valid for {type(node).__name__}")

        bases = list(node.bases)

        if accessor is None:
            # Return all bases comma-separated
            if not bases:
                return ""
            return ", ".join(_get_base_name(b) for b in bases)
        elif isinstance(accessor, int):
            # Return base by index
            try:
                return _get_base_name(bases[accessor])
            except IndexError as e:
                raise ValueError(f"Base class index {accessor} out of range") from e
        else:
            # Return base by name
            base = _find_base_by_name(bases, accessor)
            if base is None:
                raise ValueError(f"Base class '{accessor}' not found")
            return _get_base_name(base)

    elif component == "body":
        # Return body with indentation preserved
        body_code = cst.Module([]).code_for_node(node.body)
        # Strip leading and trailing newlines but preserve indentation
        return body_code.strip('\n').rstrip()

    else:
        raise ValueError(f"Unknown component: {component}")


def _generate_diff(file_path: str, old_code: str, new_code: str) -> str:
    """Generate unified diff string."""
    old_lines = old_code.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)
    return ''.join(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=file_path,
        tofile=file_path
    ))


def _parse_params(param_str: str) -> cst.Parameters:
    """Parse comma-separated params into Parameters node."""
    if not param_str.strip():
        return cst.Parameters()
    code = f'def _({param_str}): pass'
    module = cst.parse_module(code)
    return module.body[0].params


def _parse_param(param_str: str) -> cst.Param:
    """Parse single parameter string into Param node."""
    code = f'def _({param_str}): pass'
    module = cst.parse_module(code)
    return module.body[0].params.params[0].with_changes(comma=cst.MaybeSentinel.DEFAULT)


def _parse_decorator(dec_str: str) -> cst.Decorator:
    """Parse decorator string (with @) into Decorator node."""
    if not dec_str.startswith('@'):
        dec_str = '@' + dec_str
    code = f'{dec_str}\ndef _(): pass'
    module = cst.parse_module(code)
    return module.body[0].decorators[0]


def _parse_base(base_str: str) -> cst.Arg:
    """Parse base class string into Arg node."""
    code = f'class _({base_str}): pass'
    module = cst.parse_module(code)
    return module.body[0].bases[0]


def _parse_body(body_str: str) -> cst.IndentedBlock:
    """Parse body string into IndentedBlock."""
    code = f'def _():\n{body_str}'
    module = cst.parse_module(code)
    return module.body[0].body


class ComponentSetter(cst.CSTTransformer):
    """Transformer to set component values on a target symbol."""

    def __init__(self, target_path: list[str], component: str,
                 accessor: str | int | None, new_value: str):
        self.target_path = target_path
        self.component = component
        self.accessor = accessor
        self.new_value = new_value
        self.current_path: list[str] = []
        self.modified = False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        """Visit class definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        """Leave class definition, possibly modifying it."""
        if self.current_path == self.target_path:
            updated_node = self._modify_node(updated_node)

        if self.current_path:
            self.current_path.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        """Visit function definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        """Leave function definition, possibly modifying it."""
        if self.current_path == self.target_path:
            updated_node = self._modify_node(updated_node)

        if self.current_path:
            self.current_path.pop()
        return updated_node

    def _modify_node(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify node based on component and accessor."""
        self.modified = True

        if self.component == "params":
            return self._modify_params(node)
        elif self.component == "returns":
            return self._modify_returns(node)
        elif self.component == "decorators":
            return self._modify_decorators(node)
        elif self.component == "bases":
            return self._modify_bases(node)
        elif self.component == "body":
            return self._modify_body(node)

        return node

    def _modify_params(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify function parameters."""
        if not isinstance(node, cst.FunctionDef):
            raise ValueError(f"Component 'params' not valid for {type(node).__name__}")

        if self.accessor is None:
            # Replace all params
            new_params = _parse_params(self.new_value)
            return node.with_changes(params=new_params)
        else:
            # Replace specific param
            all_params = _get_all_params(node.params)

            # Find the param to replace
            if isinstance(self.accessor, int):
                if self.accessor < 0 or self.accessor >= len(all_params):
                    raise ValueError(f"Parameter index {self.accessor} out of range")
                target_idx = self.accessor
            else:
                # Find by name
                param = _find_param_by_name(all_params, self.accessor)
                if param is None:
                    raise ValueError(f"Parameter '{self.accessor}' not found")
                target_idx = all_params.index(param)

            # Parse new param
            new_param = _parse_param(self.new_value)

            # Build new params lists
            new_posonly_list = []
            new_params_list = []
            new_kwonly_list = []
            new_star_arg = node.params.star_arg
            new_star_kwarg = node.params.star_kwarg

            # Determine which list the target is in
            posonly_count = len(node.params.posonly_params) if hasattr(node.params, 'posonly_params') else 0
            regular_count = len(node.params.params)
            star_arg_count = 1 if node.params.star_arg and isinstance(node.params.star_arg, cst.Param) else 0
            kwonly_count = len(node.params.kwonly_params)

            if target_idx < posonly_count:
                # Target is in posonly_params
                for i, p in enumerate(node.params.posonly_params):
                    if i == target_idx:
                        new_posonly_list.append(new_param)
                    else:
                        new_posonly_list.append(p)
            elif target_idx < posonly_count + regular_count:
                # Target is in regular params
                regular_idx = target_idx - posonly_count
                for i, p in enumerate(node.params.params):
                    if i == regular_idx:
                        new_params_list.append(new_param)
                    else:
                        new_params_list.append(p)
            elif target_idx < posonly_count + regular_count + star_arg_count:
                # Target is star_arg
                new_star_arg = new_param
            elif target_idx < posonly_count + regular_count + star_arg_count + kwonly_count:
                # Target is in kwonly_params
                kwonly_idx = target_idx - posonly_count - regular_count - star_arg_count
                for i, p in enumerate(node.params.kwonly_params):
                    if i == kwonly_idx:
                        new_kwonly_list.append(new_param)
                    else:
                        new_kwonly_list.append(p)
            else:
                # Target is star_kwarg
                new_star_kwarg = new_param

            posonly = new_posonly_list if new_posonly_list else (list(node.params.posonly_params) if hasattr(node.params, 'posonly_params') else [])
            params_kwargs = dict(
                params=new_params_list if new_params_list else node.params.params,
                star_arg=new_star_arg,
                kwonly_params=new_kwonly_list if new_kwonly_list else node.params.kwonly_params,
                star_kwarg=new_star_kwarg,
            )
            if hasattr(cst.Parameters, 'posonly_params'):
                params_kwargs['posonly_params'] = posonly
            return node.with_changes(params=cst.Parameters(**params_kwargs))

    def _modify_returns(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify function return annotation."""
        if not isinstance(node, cst.FunctionDef):
            raise ValueError(f"Component 'returns' not valid for {type(node).__name__}")

        if not self.new_value.strip():
            # Remove return annotation
            return node.with_changes(returns=None)
        else:
            # Set or update return annotation
            # Parse the annotation by creating a temporary function
            code = f'def _() -> {self.new_value}: pass'
            module = cst.parse_module(code)
            new_returns = module.body[0].returns
            return node.with_changes(returns=new_returns)

    def _modify_decorators(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify decorators."""
        if self.accessor is None:
            # Replace all decorators
            if not self.new_value.strip():
                return node.with_changes(decorators=[])

            # Parse multiple decorators - split on lines that start with @
            # This preserves multiline decorators
            decorator_strings = []
            current_decorator = []
            for line in self.new_value.split('\n'):
                if line.strip().startswith('@'):
                    # Start of new decorator
                    if current_decorator:
                        decorator_strings.append('\n'.join(current_decorator))
                    current_decorator = [line]
                else:
                    # Continuation of current decorator
                    if current_decorator:
                        current_decorator.append(line)
            # Don't forget the last decorator
            if current_decorator:
                decorator_strings.append('\n'.join(current_decorator))

            new_decorators = []
            for dec_str in decorator_strings:
                new_decorators.append(_parse_decorator(dec_str.strip()))
            return node.with_changes(decorators=new_decorators)
        else:
            # Replace specific decorator
            decorators = list(node.decorators)

            # Find the decorator to replace
            if isinstance(self.accessor, int):
                if self.accessor < 0 or self.accessor >= len(decorators):
                    raise ValueError(f"Decorator index {self.accessor} out of range")
                target_idx = self.accessor
            else:
                # Find by name
                decorator = _find_decorator_by_name(decorators, self.accessor)
                if decorator is None:
                    raise ValueError(f"Decorator '{self.accessor}' not found")
                target_idx = decorators.index(decorator)

            # Parse new decorator
            new_decorator = _parse_decorator(self.new_value)

            # Replace decorator at target index
            new_decorators = decorators[:target_idx] + [new_decorator] + decorators[target_idx + 1:]
            return node.with_changes(decorators=new_decorators)

    def _modify_bases(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify class base classes."""
        if not isinstance(node, cst.ClassDef):
            raise ValueError(f"Component 'bases' not valid for {type(node).__name__}")

        if self.accessor is None:
            # Replace all bases
            if not self.new_value.strip():
                return node.with_changes(bases=[])

            # Parse multiple bases
            code = f'class _({self.new_value}): pass'
            module = cst.parse_module(code)
            new_bases = list(module.body[0].bases)
            return node.with_changes(bases=new_bases)
        else:
            # Replace specific base
            bases = list(node.bases)

            # Find the base to replace
            if isinstance(self.accessor, int):
                if self.accessor < 0 or self.accessor >= len(bases):
                    raise ValueError(f"Base class index {self.accessor} out of range")
                target_idx = self.accessor
            else:
                # Find by name
                base = _find_base_by_name(bases, self.accessor)
                if base is None:
                    raise ValueError(f"Base class '{self.accessor}' not found")
                target_idx = bases.index(base)

            # Parse new base
            new_base = _parse_base(self.new_value)

            # Replace base at target index
            new_bases = bases[:target_idx] + [new_base] + bases[target_idx + 1:]
            return node.with_changes(bases=new_bases)

    def _modify_body(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify function or class body."""
        new_body = _parse_body(self.new_value)
        return node.with_changes(body=new_body)


def set_component(selector: ExtendedSelector, value: str, apply: bool = False) -> str:
    """Set value of component. Returns diff.

    Args:
        selector: Extended selector with component specified
        value: New value for the component
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes

    Example:
        >>> sel = parse_extended_selector("file.py::func[returns]")
        >>> diff = set_component(sel, "int", apply=False)
        >>> print(diff)
        --- file.py
        +++ file.py
        @@ -1,3 +1,3 @@
        -def func() -> None:
        +def func() -> int:
             pass

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found, invalid component for symbol type,
                   or accessor not found
    """
    # Read and parse file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()
    module = cst.parse_module(source_code)

    # Validate that symbol exists first
    finder = SymbolFinder(selector.symbol_path)
    module.visit(finder)

    if finder.found_node is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    node = finder.found_node

    # Validate component for symbol type
    if isinstance(node, cst.ClassDef):
        if selector.component in ("params", "returns"):
            raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
    elif isinstance(node, cst.FunctionDef):
        if selector.component == "bases":
            raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")

    # Apply transformation
    transformer = ComponentSetter(
        selector.symbol_path,
        selector.component,
        selector.accessor,
        value
    )
    new_module = module.visit(transformer)
    new_code = new_module.code

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


class ComponentAdder(cst.CSTTransformer):
    """Transformer to add items to list components."""

    def __init__(self, target_path: list[str], component: str, new_value: str, position: int, before: str | None = None, after: str | None = None, kind: str | None = None):
        self.target_path = target_path
        self.component = component
        self.new_value = new_value
        self.position = position
        self.before = before
        self.after = after
        self.kind = kind
        self.current_path: list[str] = []
        self.modified = False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        """Visit class definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        """Leave class definition, possibly modifying it."""
        if self.current_path == self.target_path:
            updated_node = self._modify_node(updated_node)

        if self.current_path:
            self.current_path.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        """Visit function definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        """Leave function definition, possibly modifying it."""
        if self.current_path == self.target_path:
            updated_node = self._modify_node(updated_node)

        if self.current_path:
            self.current_path.pop()
        return updated_node

    def _modify_node(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify node based on component."""
        self.modified = True

        if self.component == "params":
            return self._add_param(node)
        elif self.component == "decorators":
            return self._add_decorator(node)
        elif self.component == "bases":
            return self._add_base(node)

        return node

    def _find_param_index(self, node: cst.FunctionDef, name: str) -> int | None:
        """Find index of parameter by name. Returns None if not found."""
        # Check posonly params first
        posonly_count = 0
        if hasattr(node.params, 'posonly_params'):
            for i, param in enumerate(node.params.posonly_params):
                if param.name.value == name:
                    return i
            posonly_count = len(node.params.posonly_params)
            if posonly_count > 0:
                posonly_count += 1  # Account for / separator

        for i, param in enumerate(node.params.params):
            if param.name.value == name:
                return posonly_count + i
        # Also check kwonly params
        kwonly_offset = posonly_count + len(node.params.params)
        if isinstance(node.params.star_arg, (cst.Param, cst.ParamStar)):
            kwonly_offset += 1  # Account for * or *args
        for i, param in enumerate(node.params.kwonly_params):
            if param.name.value == name:
                return kwonly_offset + i
        return None

    def _find_decorator_index(self, node: cst.FunctionDef | cst.ClassDef, name: str) -> int | None:
        """Find index of decorator by name. Returns None if not found."""
        for i, decorator in enumerate(node.decorators):
            # Extract decorator name (handle both @name and @name(...))
            if isinstance(decorator.decorator, cst.Name):
                dec_name = decorator.decorator.value
            elif isinstance(decorator.decorator, cst.Call):
                if isinstance(decorator.decorator.func, cst.Name):
                    dec_name = decorator.decorator.func.value
                elif isinstance(decorator.decorator.func, cst.Attribute):
                    dec_name = decorator.decorator.func.attr.value
                else:
                    continue
            elif isinstance(decorator.decorator, cst.Attribute):
                dec_name = decorator.decorator.attr.value
            else:
                continue

            if dec_name == name:
                return i
        return None

    def _find_base_index(self, node: cst.ClassDef, name: str) -> int | None:
        """Find index of base class by name. Returns None if not found."""
        for i, arg in enumerate(node.bases):
            # Extract base name (handle both Name and Attribute)
            if isinstance(arg.value, cst.Name):
                base_name = arg.value.value
            elif isinstance(arg.value, cst.Attribute):
                base_name = arg.value.attr.value
            elif isinstance(arg.value, cst.Subscript):
                # Handle Generic[T] style
                if isinstance(arg.value.value, cst.Name):
                    base_name = arg.value.value.value
                else:
                    continue
            else:
                continue

            if base_name == name:
                return i
        return None

    def _add_param(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Add parameter to function at specified position.

        Handles insertion into both regular params and keyword-only params,
        and auto-inserts a * separator when adding keyword-only params to a
        function that doesn't have one.

        The position parameter is a "logical" index counting all params including
        separators:
        - For `def f(a, *, b, c)`, the logical positions are:
          - 0: a (regular)
          - 1: * (separator)
          - 2: b (kwonly)
          - 3: c (kwonly)
        """
        if not isinstance(node, cst.FunctionDef):
            raise ValueError(f"Component 'params' not valid for {type(node).__name__}")

        # Parse new param
        new_param = _parse_param(self.new_value)

        # Calculate counts for each section
        regular_count = len(node.params.params)
        # Check if there's actually a star (not MaybeSentinel.DEFAULT)
        has_star = isinstance(node.params.star_arg, (cst.Param, cst.ParamStar))
        star_is_args = has_star and isinstance(node.params.star_arg, cst.Param)
        kwonly_count = len(node.params.kwonly_params)
        has_kwargs = node.params.star_kwarg is not None

        # Calculate logical boundaries:
        # Position in logical list = [regular params] [* or *args] [kwonly params] [**kwargs]
        # star_logical_idx: index of * in logical list (or None if no star)
        # kwonly_start_logical: first kwonly index in logical list
        star_logical_idx = regular_count if has_star else None
        kwonly_start_logical = regular_count + (1 if has_star else 0)
        kwargs_logical_idx = kwonly_start_logical + kwonly_count if has_kwargs else None

        # Normalize position
        if self.before is not None:
            # Insert before named parameter
            idx = self._find_param_index(node, self.before)
            if idx is None:
                raise ValueError(f"Parameter '{self.before}' not found")
            insert_pos = idx
        elif self.after is not None:
            # Insert after named parameter
            idx = self._find_param_index(node, self.after)
            if idx is None:
                raise ValueError(f"Parameter '{self.after}' not found")
            insert_pos = idx + 1
        elif self.position == -1:
            # Append: add to kwonly if has_star, else to regular
            if has_star:
                # Append to end of kwonly (before **kwargs if present)
                insert_pos = kwonly_start_logical + kwonly_count
            else:
                insert_pos = regular_count

            # Override with kind if specified
            if self.kind:
                if self.kind == "KEYWORD_ONLY":
                    # Add to keyword-only section
                    if has_star:
                        # Append to end of kwonly
                        insert_pos = kwonly_start_logical + kwonly_count
                    else:
                        # Need to add * separator, insert param after it
                        insert_pos = regular_count + 1
                elif self.kind in ("POSITIONAL_OR_KEYWORD", "POSITIONAL_ONLY"):
                    # Add to regular params section
                    insert_pos = regular_count
                else:
                    raise ValueError(f"Invalid kind value: {self.kind}. Must be one of: POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, KEYWORD_ONLY")
        else:
            insert_pos = self.position

        # Start with copies of existing params
        new_params = list(node.params.params)
        new_star_arg = node.params.star_arg
        new_kwonly = list(node.params.kwonly_params)
        new_star_kwarg = node.params.star_kwarg

        # Determine which section to modify based on logical position
        if insert_pos <= regular_count:
            # Insert into regular params
            new_params.insert(insert_pos, new_param)
        elif has_star and insert_pos > star_logical_idx:
            # Insert into kwonly params (position is after *)
            # Convert logical position to kwonly index
            kwonly_idx = insert_pos - kwonly_start_logical
            if kwonly_idx < 0:
                kwonly_idx = 0
            if kwonly_idx > len(new_kwonly):
                kwonly_idx = len(new_kwonly)
            new_kwonly.insert(kwonly_idx, new_param)
        elif not has_star and insert_pos > regular_count:
            # No star yet but position is beyond regular params
            # This means user wants keyword-only - auto-insert *
            new_star_arg = cst.ParamStar()
            kwonly_idx = insert_pos - regular_count - 1  # -1 for the * we're inserting
            if kwonly_idx < 0:
                kwonly_idx = 0
            new_kwonly.insert(kwonly_idx, new_param)
        else:
            # Fallback: append to kwonly if star exists, else to regular
            if has_star:
                new_kwonly.append(new_param)
            else:
                new_params.append(new_param)

        params_kwargs = dict(
            params=new_params,
            star_arg=new_star_arg,
            kwonly_params=new_kwonly,
            star_kwarg=new_star_kwarg,
        )
        if hasattr(node.params, 'posonly_params'):
            params_kwargs['posonly_params'] = list(node.params.posonly_params)
        return node.with_changes(params=cst.Parameters(**params_kwargs))

    def _add_decorator(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Add decorator to function or class."""
        # Parse new decorator
        new_decorator = _parse_decorator(self.new_value)

        # Get current decorators
        decorators = list(node.decorators)

        # Calculate insertion position
        if self.before is not None:
            # Insert before named decorator
            idx = self._find_decorator_index(node, self.before)
            if idx is None:
                raise ValueError(f"Decorator '{self.before}' not found")
            insert_pos = idx
        elif self.after is not None:
            # Insert after named decorator
            idx = self._find_decorator_index(node, self.after)
            if idx is None:
                raise ValueError(f"Decorator '{self.after}' not found")
            insert_pos = idx + 1
        elif self.position == -1:
            insert_pos = len(decorators)
        else:
            insert_pos = self.position

        # Insert at position
        decorators.insert(insert_pos, new_decorator)

        return node.with_changes(decorators=decorators)

    def _add_base(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Add base class to class."""
        if not isinstance(node, cst.ClassDef):
            raise ValueError(f"Component 'bases' not valid for {type(node).__name__}")

        # Parse new base
        new_base = _parse_base(self.new_value)

        # Get current bases
        bases = list(node.bases)

        # Calculate insertion position
        if self.before is not None:
            # Insert before named base
            idx = self._find_base_index(node, self.before)
            if idx is None:
                raise ValueError(f"Base class '{self.before}' not found")
            insert_pos = idx
        elif self.after is not None:
            # Insert after named base
            idx = self._find_base_index(node, self.after)
            if idx is None:
                raise ValueError(f"Base class '{self.after}' not found")
            insert_pos = idx + 1
        elif self.position == -1:
            insert_pos = len(bases)
        else:
            insert_pos = self.position

        # Insert at position
        bases.insert(insert_pos, new_base)

        return node.with_changes(bases=bases)


def add_to_component(
    selector: ExtendedSelector,
    value: str,
    position: int = -1,
    before: str | None = None,
    after: str | None = None,
    apply: bool = False,
    kind: str | None = None
) -> str:
    """Add item to list component. Returns diff.

    Args:
        selector: Extended selector with list component (params, decorators, bases)
        value: Item to add to the list
        position: Index to insert at (-1 for append)
        before: Name of item to insert before (mutually exclusive with after)
        after: Name of item to insert after (mutually exclusive with before)
        apply: If True, write changes to file. If False, return diff only.
        kind: For params component, specifies parameter kind (POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, KEYWORD_ONLY)

    Returns:
        Unified diff showing the changes

    Example:
        >>> sel = parse_extended_selector("file.py::func[params]")
        >>> diff = add_to_component(sel, "debug: bool = False", position=-1, apply=False)
        >>> print(diff)
        --- file.py
        +++ file.py
        @@ -1,3 +1,3 @@
        -def func(ctx, request):
        +def func(ctx, request, debug: bool = False):
             pass

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If selector has accessor, component is not a list type,
                   or symbol not found
    """
    # Validate mutually exclusive position options
    if before is not None and after is not None:
        raise ValueError("Cannot specify both --before and --after")

    # Validate that component is a list type
    if selector.component not in ("params", "decorators", "bases", "imports"):
        raise ValueError(f"Component '{selector.component}' is not a list component")

    # Validate that accessor is None
    if selector.accessor is not None:
        raise ValueError("add_to_component requires accessor must be None")

    # Validate kind parameter
    if kind is not None:
        if selector.component != "params":
            raise ValueError("'kind' parameter can only be used with 'params' component")
        if kind not in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"):
            raise ValueError(f"Invalid kind value: {kind}. Must be one of: POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, KEYWORD_ONLY")

    # Read and parse file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()
    module = cst.parse_module(source_code)

    # Handle module-level imports component
    if selector.component == "imports" and not selector.symbol_path:
        return _add_import(module, value, position, file_path, apply, source_code)

    # Validate that symbol exists first
    finder = SymbolFinder(selector.symbol_path)
    module.visit(finder)

    if finder.found_node is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    node = finder.found_node

    # Validate component for symbol type
    if isinstance(node, cst.ClassDef):
        if selector.component == "params":
            raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
    elif isinstance(node, cst.FunctionDef):
        if selector.component == "bases":
            raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")

    # Apply transformation
    transformer = ComponentAdder(
        selector.symbol_path,
        selector.component,
        value,
        position,
        before,
        after,
        kind
    )
    new_module = module.visit(transformer)
    new_code = new_module.code

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


class ComponentRemover(cst.CSTTransformer):
    """Transformer to remove items from components."""

    def __init__(self, target_path: list[str], component: str, accessor: str | int | None):
        self.target_path = target_path
        self.component = component
        self.accessor = accessor
        self.current_path: list[str] = []
        self.modified = False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        """Visit class definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        """Leave class definition, possibly modifying it."""
        if self.current_path == self.target_path:
            updated_node = self._modify_node(updated_node)

        if self.current_path:
            self.current_path.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        """Visit function definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        """Leave function definition, possibly modifying it."""
        if self.current_path == self.target_path:
            updated_node = self._modify_node(updated_node)

        if self.current_path:
            self.current_path.pop()
        return updated_node

    def _modify_node(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Modify node based on component."""
        self.modified = True

        if self.component == "params":
            return self._remove_params(node)
        elif self.component == "returns":
            return self._remove_returns(node)
        elif self.component == "decorators":
            return self._remove_decorators(node)
        elif self.component == "bases":
            return self._remove_bases(node)

        return node

    def _remove_params(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Remove parameter(s) from function."""
        if not isinstance(node, cst.FunctionDef):
            raise ValueError(f"Component 'params' not valid for {type(node).__name__}")

        if self.accessor is None:
            # Remove all params
            return node.with_changes(params=cst.Parameters())

        # Get all current params
        all_params = _get_all_params(node.params)

        # Find the param to remove
        if isinstance(self.accessor, int):
            if self.accessor < 0:
                # Support negative indexing
                target_idx = len(all_params) + self.accessor
            else:
                target_idx = self.accessor
            if target_idx < 0 or target_idx >= len(all_params):
                raise ValueError(f"Parameter index {self.accessor} out of range")
        else:
            # Find by name
            param = _find_param_by_name(all_params, self.accessor)
            if param is None:
                raise ValueError(f"Parameter '{self.accessor}' not found")
            target_idx = all_params.index(param)

        # Rebuild params without the target
        new_posonly_list = None
        new_params_list = []
        new_kwonly_list = []
        new_star_arg = node.params.star_arg
        new_star_kwarg = node.params.star_kwarg

        # Determine which list the target is in
        posonly_count = len(node.params.posonly_params) if hasattr(node.params, 'posonly_params') else 0
        regular_count = len(node.params.params)
        star_arg_count = 1 if node.params.star_arg and isinstance(node.params.star_arg, cst.Param) else 0
        kwonly_count = len(node.params.kwonly_params)

        if target_idx < posonly_count:
            # Target is in posonly_params
            new_posonly_list = [p for i, p in enumerate(node.params.posonly_params) if i != target_idx]
            if new_posonly_list:
                new_posonly_list[-1] = new_posonly_list[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
            new_params_list = list(node.params.params)
        elif target_idx < posonly_count + regular_count:
            # Target is in regular params - remove trailing comma from last param
            regular_idx = target_idx - posonly_count
            new_params_list = [p for i, p in enumerate(node.params.params) if i != regular_idx]
            # Remove trailing comma from the last param if it exists
            if new_params_list:
                new_params_list[-1] = new_params_list[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        elif target_idx < posonly_count + regular_count + star_arg_count:
            # Target is star_arg
            new_star_arg = cst.MaybeSentinel.DEFAULT
            new_params_list = list(node.params.params)
        elif target_idx < posonly_count + regular_count + star_arg_count + kwonly_count:
            # Target is in kwonly_params
            kwonly_idx = target_idx - posonly_count - regular_count - star_arg_count
            new_kwonly_list = [p for i, p in enumerate(node.params.kwonly_params) if i != kwonly_idx]
            new_params_list = list(node.params.params)
            # Remove trailing comma from the last kwonly param if it exists
            if new_kwonly_list:
                new_kwonly_list[-1] = new_kwonly_list[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        else:
            # Target is star_kwarg
            new_star_kwarg = None
            new_params_list = list(node.params.params)
            new_kwonly_list = list(node.params.kwonly_params)

        posonly = new_posonly_list if new_posonly_list is not None else (list(node.params.posonly_params) if hasattr(node.params, 'posonly_params') else [])
        params_kwargs = dict(
            params=new_params_list,
            star_arg=new_star_arg,
            kwonly_params=new_kwonly_list,
            star_kwarg=new_star_kwarg,
        )
        if hasattr(cst.Parameters, 'posonly_params'):
            params_kwargs['posonly_params'] = posonly
        return node.with_changes(params=cst.Parameters(**params_kwargs))

    def _remove_returns(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Remove return annotation from function."""
        if not isinstance(node, cst.FunctionDef):
            raise ValueError(f"Component 'returns' not valid for {type(node).__name__}")

        # Remove return annotation
        return node.with_changes(returns=None)

    def _remove_decorators(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Remove decorator(s) from function or class."""
        if self.accessor is None:
            # Remove all decorators
            return node.with_changes(decorators=[])

        # Get current decorators
        decorators = list(node.decorators)

        # Find the decorator to remove
        if isinstance(self.accessor, int):
            if self.accessor < 0:
                # Support negative indexing
                target_idx = len(decorators) + self.accessor
            else:
                target_idx = self.accessor
            if target_idx < 0 or target_idx >= len(decorators):
                raise ValueError(f"Decorator index {self.accessor} out of range")
        else:
            # Find by name
            decorator = _find_decorator_by_name(decorators, self.accessor)
            if decorator is None:
                raise ValueError(f"Decorator '{self.accessor}' not found")
            target_idx = decorators.index(decorator)

        # Remove decorator at target index
        new_decorators = decorators[:target_idx] + decorators[target_idx + 1:]
        return node.with_changes(decorators=new_decorators)

    def _remove_bases(self, node: cst.FunctionDef | cst.ClassDef) -> cst.FunctionDef | cst.ClassDef:
        """Remove base class(es) from class."""
        if not isinstance(node, cst.ClassDef):
            raise ValueError(f"Component 'bases' not valid for {type(node).__name__}")

        if self.accessor is None:
            # Remove all bases - also need to remove the parentheses
            # by setting lpar and rpar to default (which removes them when there are no bases)
            return node.with_changes(
                bases=[],
                lpar=cst.MaybeSentinel.DEFAULT,
                rpar=cst.MaybeSentinel.DEFAULT
            )

        # Get current bases
        bases = list(node.bases)

        # Find the base to remove
        if isinstance(self.accessor, int):
            if self.accessor < 0:
                # Support negative indexing
                target_idx = len(bases) + self.accessor
            else:
                target_idx = self.accessor
            if target_idx < 0 or target_idx >= len(bases):
                raise ValueError(f"Base class index {self.accessor} out of range")
        else:
            # Find by name
            base = _find_base_by_name(bases, self.accessor)
            if base is None:
                raise ValueError(f"Base class '{self.accessor}' not found")
            target_idx = bases.index(base)

        # Remove base at target index
        new_bases = bases[:target_idx] + bases[target_idx + 1:]

        # If removing all bases, also remove the parentheses
        if not new_bases:
            return node.with_changes(
                bases=[],
                lpar=cst.MaybeSentinel.DEFAULT,
                rpar=cst.MaybeSentinel.DEFAULT
            )

        return node.with_changes(bases=new_bases)


def remove_component(selector: ExtendedSelector, apply: bool = False) -> str:
    """Remove component or item. Returns diff.

    Args:
        selector: Extended selector with component specified
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes

    Example:
        >>> sel = parse_extended_selector("file.py::func[decorators][0]")
        >>> diff = remove_component(sel, apply=False)
        >>> print(diff)
        --- file.py
        +++ file.py
        @@ -1,4 +1,3 @@
        -@deprecated
         @cache
         def func():
             pass

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If trying to remove body, or symbol not found
    """
    # If no component specified, remove the entire symbol
    if selector.component is None:
        return remove_symbol(selector, apply=apply)

    # Validate that body cannot be removed
    if selector.component == "body":
        raise ValueError("Cannot remove body component")

    # Read and parse file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()
    module = cst.parse_module(source_code)

    # Validate that symbol exists first
    finder = SymbolFinder(selector.symbol_path)
    module.visit(finder)

    if finder.found_node is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    node = finder.found_node

    # Validate component for symbol type
    if isinstance(node, cst.ClassDef):
        if selector.component in ("params", "returns"):
            raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
    elif isinstance(node, cst.FunctionDef):
        if selector.component == "bases":
            raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")

    # Apply transformation
    transformer = ComponentRemover(
        selector.symbol_path,
        selector.component,
        selector.accessor
    )
    new_module = module.visit(transformer)
    new_code = new_module.code

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff

def _slice_ellipsis_sequence(sequence: tuple, position: int, total_pattern_items: int) -> tuple:
    """Helper to slice a sequence (args/elements) for ellipsis capture.

    Args:
        sequence: The tuple of items to slice (e.g., node.args or node.elements)
        position: Position of ellipsis in pattern
        total_pattern_items: Total number of items in the pattern

    Returns:
        Tuple of items captured by the ellipsis
    """
    # If ellipsis is the only item, capture all
    if total_pattern_items == 1:
        return sequence
    # If ellipsis is at the end, capture from position onwards
    elif position == total_pattern_items - 1:
        return sequence[position:]
    # If ellipsis is at the start, calculate how many to capture
    elif position == 0:
        num_fixed_after = total_pattern_items - 1
        return sequence[:-num_fixed_after] if num_fixed_after > 0 else sequence
    # If ellipsis is in the middle
    else:
        num_fixed_after = total_pattern_items - position - 1
        return sequence[position:-num_fixed_after] if num_fixed_after > 0 else sequence[position:]


def _extract_ellipsis_and_partial_captures(
    node: cst.CSTNode,
    ellipsis_info: dict,
    captures: dict,
) -> None:
    """Extract captures for ellipsis metavars and partial dict patterns.

    Mutates `captures` in place.
    """
    for name, info in ellipsis_info.items():
        if name == "__partial_dict__":
            continue
        if isinstance(node, cst.Call) and "total_args" in info:
            position = info["position"]
            total_pattern_args = info["total_args"]
            captures[name] = _slice_ellipsis_sequence(tuple(node.args), position, total_pattern_args)
        elif isinstance(node, (cst.List, cst.Tuple, cst.Set)) and "total_elements" in info:
            position = info["position"]
            total_pattern_elements = info["total_elements"]
            captures[name] = _slice_ellipsis_sequence(tuple(node.elements), position, total_pattern_elements)
        elif isinstance(node, cst.Dict) and "total_elements" in info:
            position = info["position"]
            total_pattern_elements = info["total_elements"]
            captures[name] = _slice_ellipsis_sequence(tuple(node.elements), position, total_pattern_elements)

    # Handle partial dict captures (extract from individual elements)
    if "__partial_dict__" in ellipsis_info and isinstance(node, cst.Dict):
        partial_info = ellipsis_info["__partial_dict__"]
        for elem_matcher in partial_info["element_matchers"]:
            for elem in node.elements:
                extracted = m.extract(elem, elem_matcher)
                if extracted is not None:
                    captures.update(extracted)
                    break


_CONTENT_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\.content\}")


def _extract_string_content(node: cst.CSTNode) -> str | None:
    """Extract the inner content of a string literal, stripping quotes.

    For a SimpleString like ``"MyClass"`` returns ``MyClass``.
    Returns None for non-string nodes or string types that cannot be
    trivially unwrapped (f-strings, concatenated strings).
    """
    if isinstance(node, cst.SimpleString):
        return str(ast.literal_eval(node.value))
    return None


@dataclass
class PatternMatch:
    """Represents a match of a pattern in code."""
    node: cst.CSTNode | None
    captures: dict[str, cst.CSTNode]
    line: int | None = None
    matched_text: str | None = None


def _extract_all_captures(node: cst.CSTNode, matcher: m.BaseMatcherNode, metavar_names: set[str]) -> dict[str, list[cst.CSTNode]]:
    """Extract all occurrences of each metavariable from a matched node.

    LibCST's m.extract() only returns one value per metavar name, overwriting
    if the same name appears multiple times. This function collects ALL
    occurrences by walking both the pattern and matched node in parallel.

    Args:
        node: The matched CST node
        matcher: The matcher pattern
        metavar_names: Set of metavar names to collect

    Returns:
        Dictionary mapping metavar name to list of all captured nodes
    """
    captures = {name: [] for name in metavar_names}

    def walk_parallel(node, matcher):
        """Recursively walk node and matcher in parallel, collecting captures."""
        # If matcher is SaveMatchedNode (check by __class__.__name__ since it's internal)
        if hasattr(matcher, 'name') and hasattr(matcher, 'matcher') and matcher.__class__.__name__ == '_ExtractMatchingNode':
            name = matcher.name
            if name in captures:
                captures[name].append(node)
            # Continue with inner matcher
            walk_parallel(node, matcher.matcher)
            return

        # For compound matchers, recurse on corresponding parts
        if isinstance(matcher, m.ListComp) and isinstance(node, cst.ListComp):
            walk_parallel(node.elt, matcher.elt)
            walk_parallel(node.for_in, matcher.for_in)
        elif isinstance(matcher, m.SetComp) and isinstance(node, cst.SetComp):
            walk_parallel(node.elt, matcher.elt)
            walk_parallel(node.for_in, matcher.for_in)
        elif isinstance(matcher, m.DictComp) and isinstance(node, cst.DictComp):
            walk_parallel(node.key, matcher.key)
            walk_parallel(node.value, matcher.value)
            walk_parallel(node.for_in, matcher.for_in)
        elif isinstance(matcher, m.GeneratorExp) and isinstance(node, cst.GeneratorExp):
            walk_parallel(node.elt, matcher.elt)
            walk_parallel(node.for_in, matcher.for_in)
        elif isinstance(matcher, m.CompFor) and isinstance(node, cst.CompFor):
            walk_parallel(node.target, matcher.target)
            walk_parallel(node.iter, matcher.iter)
            # Handle ifs if present
            if hasattr(matcher, 'ifs') and matcher.ifs and node.ifs:
                for node_if, matcher_if in zip(node.ifs, matcher.ifs):
                    if hasattr(matcher_if, 'test'):
                        walk_parallel(node_if.test, matcher_if.test)
        elif isinstance(matcher, m.Call) and isinstance(node, cst.Call):
            walk_parallel(node.func, matcher.func)
            # Handle args
            if hasattr(matcher, 'args'):
                for node_arg, matcher_arg in zip(node.args, matcher.args):
                    if isinstance(matcher_arg, m.Arg):
                        walk_parallel(node_arg.value, matcher_arg.value)
        elif isinstance(matcher, m.Tuple) and isinstance(node, cst.Tuple):
            if hasattr(matcher, 'elements'):
                for node_elem, matcher_elem in zip(node.elements, matcher.elements):
                    if isinstance(matcher_elem, m.Element) and isinstance(node_elem, cst.Element):
                        walk_parallel(node_elem.value, matcher_elem.value)
        elif isinstance(matcher, m.List) and isinstance(node, cst.List):
            if hasattr(matcher, 'elements'):
                for node_elem, matcher_elem in zip(node.elements, matcher.elements):
                    if isinstance(matcher_elem, m.Element) and isinstance(node_elem, cst.Element):
                        walk_parallel(node_elem.value, matcher_elem.value)
        elif isinstance(matcher, m.Set) and isinstance(node, cst.Set):
            if hasattr(matcher, 'elements'):
                for node_elem, matcher_elem in zip(node.elements, matcher.elements):
                    if isinstance(matcher_elem, m.Element) and isinstance(node_elem, cst.Element):
                        walk_parallel(node_elem.value, matcher_elem.value)
        elif isinstance(matcher, m.FormattedString) and isinstance(node, cst.FormattedString):
            if hasattr(matcher, 'parts'):
                for node_part, matcher_part in zip(node.parts, matcher.parts):
                    if isinstance(matcher_part, m.FormattedStringExpression) and isinstance(node_part, cst.FormattedStringExpression):
                        walk_parallel(node_part.expression, matcher_part.expression)
        # Add more node types as needed

    walk_parallel(node, matcher)
    return captures


def _validate_repeated_metavars(node: cst.CSTNode, matcher: m.BaseMatcherNode, metavar_names: set[str]) -> bool:
    """Validate that repeated metavariables captured identical values.

    When a metavar appears multiple times in a pattern (e.g., $X in both
    positions of [$X for $X in $Y]), we need to verify all occurrences
    captured structurally equal nodes.

    Args:
        node: The matched CST node
        matcher: The matcher pattern
        metavar_names: Set of metavar names in the pattern

    Returns:
        True if all repeated metavars captured equal values
    """
    all_captures = _extract_all_captures(node, matcher, metavar_names)

    # Check each metavar - all captures should be equal
    for name, captured_nodes in all_captures.items():
        if len(captured_nodes) > 1:
            # Compare all captures - they should be structurally equal
            first = captured_nodes[0]
            for other in captured_nodes[1:]:
                if not first.deep_equals(other):
                    return False

    return True


class PatternFinder(cst.CSTVisitor):
    """Visitor to find all matches of a pattern."""

    def __init__(self, matcher: m.BaseMatcherNode, ellipsis_info: dict | None = None, position_provider: cst.metadata.PositionProvider | None = None, metavar_names: set[str] | None = None):
        self.matcher = matcher
        self.ellipsis_info = ellipsis_info or {}
        self.position_provider = position_provider
        self.metavar_names = metavar_names or set()
        self.matches: list[PatternMatch] = []

    def on_visit(self, node: cst.CSTNode) -> bool:
        """Check if this node matches the pattern."""
        # Try to match this node
        if m.matches(node, self.matcher):
            # Validate repeated metavars captured equal values
            if not _validate_repeated_metavars(node, self.matcher, self.metavar_names):
                return True  # Continue visiting, but don't add this as a match

            # Extract captures
            captures = m.extract(node, self.matcher)

            # Handle ellipsis and partial dict captures
            _extract_ellipsis_and_partial_captures(node, self.ellipsis_info, captures)

            # Get line number if position provider is available
            line = None
            if self.position_provider is not None:
                try:
                    pos = self.position_provider[node]
                    line = pos.start.line
                except KeyError:
                    pass  # Node position not available

            self.matches.append(PatternMatch(node=node, captures=captures, line=line))
        return True  # Continue visiting children


def _parse_constraint(constraint: str) -> callable:
    """Parse a constraint string into a node checker function.

    Supports:
    - Simple keywords: "def", "class", "for", "while", "try", "with", "if", "async def"
    - Pattern with name glob: "def test_*", "class My*", "def *_helper"
    - Compound statement patterns: "try:", "except *:", "for $V in $I:"

    Returns:
        A function (node) -> bool that checks if a node matches.
    """
    import fnmatch as fnmatch_mod

    # Simple keyword checkers
    KEYWORD_CHECKERS = {
        "def": lambda node: isinstance(node, cst.FunctionDef),
        "async def": lambda node: isinstance(node, cst.FunctionDef) and node.asynchronous is not None,
        "class": lambda node: isinstance(node, cst.ClassDef),
        "for": lambda node: isinstance(node, (cst.For, cst.ListComp, cst.SetComp, cst.DictComp, cst.GeneratorExp)),
        "while": lambda node: isinstance(node, cst.While),
        "with": lambda node: isinstance(node, cst.With),
        "try": lambda node: isinstance(node, (cst.Try, cst.TryStar)),
        "if": lambda node: isinstance(node, (cst.If, cst.IfExp)),
    }

    # Check for exact keyword match first
    if constraint in KEYWORD_CHECKERS:
        return KEYWORD_CHECKERS[constraint]

    # Check for "keyword name_pattern" form: "def test_*", "class My*", etc.
    for keyword in ("async def", "def", "class"):
        if constraint.startswith(keyword + " "):
            name_pattern = constraint[len(keyword) + 1:].strip()
            if keyword == "def":
                def checker(node, pat=name_pattern):
                    return (isinstance(node, cst.FunctionDef)
                            and fnmatch_mod.fnmatch(node.name.value, pat))
                return checker
            elif keyword == "async def":
                def checker(node, pat=name_pattern):
                    return (isinstance(node, cst.FunctionDef)
                            and node.asynchronous is not None
                            and fnmatch_mod.fnmatch(node.name.value, pat))
                return checker
            elif keyword == "class":
                def checker(node, pat=name_pattern):
                    return (isinstance(node, cst.ClassDef)
                            and fnmatch_mod.fnmatch(node.name.value, pat))
                return checker

    # Check for compound statement pattern forms ending with ':'
    stripped = constraint.rstrip()
    if stripped.endswith(":"):
        body = stripped[:-1].strip()
        # "try" (with colon)
        if body == "try":
            return KEYWORD_CHECKERS["try"]
        # "except ..." pattern
        if body.startswith("except"):
            exc_part = body[6:].strip()
            if not exc_part:
                # bare "except:"
                def checker(node):
                    if isinstance(node, (cst.Try, cst.TryStar)):
                        return True
                    return isinstance(node, cst.ExceptHandler)
                return checker
            else:
                # "except SomeError:" or "except *:" pattern
                def checker(node, pat=exc_part):
                    if not isinstance(node, cst.ExceptHandler):
                        return False
                    if node.type is None:
                        return False
                    type_code = cst.Module([]).code_for_node(node.type).strip()
                    return fnmatch_mod.fnmatch(type_code, pat)
                return checker

    raise ValueError(
        f"Unknown inside/not_inside constraint: '{constraint}'. "
        f"Valid keywords: {', '.join(KEYWORD_CHECKERS.keys())}. "
        f"Or use patterns like 'def test_*', 'class MyClass', 'try:'"
    )


class ConstrainedPatternFinder(cst.CSTVisitor):
    """Visitor to find matches of a pattern with inside/not_inside constraints."""

    # Mapping of keyword shortcuts to node type checkers (kept for reference)
    KEYWORD_CHECKERS = {
        "def": lambda node: isinstance(node, cst.FunctionDef),
        "async def": lambda node: isinstance(node, cst.FunctionDef) and node.asynchronous is not None,
        "class": lambda node: isinstance(node, cst.ClassDef),
        "for": lambda node: isinstance(node, (cst.For, cst.ListComp, cst.SetComp, cst.DictComp, cst.GeneratorExp)),
        "while": lambda node: isinstance(node, cst.While),
        "with": lambda node: isinstance(node, cst.With),
        "try": lambda node: isinstance(node, (cst.Try, cst.TryStar)),
        "if": lambda node: isinstance(node, (cst.If, cst.IfExp)),
    }

    def __init__(
        self,
        matcher: m.BaseMatcherNode,
        ellipsis_info: dict | None = None,
        position_provider: cst.metadata.PositionProvider | None = None,
        inside: str | None = None,
        not_inside: str | None = None,
        metavar_names: set[str] | None = None,
    ):
        self.matcher = matcher
        self.ellipsis_info = ellipsis_info or {}
        self.position_provider = position_provider
        self.inside = inside
        self.not_inside = not_inside
        self.metavar_names = metavar_names or set()
        self.ancestor_stack: list[cst.CSTNode] = []
        self.matches: list[PatternMatch] = []

        # Parse constraints into checker functions
        self._inside_checker = _parse_constraint(inside) if inside else None
        self._not_inside_checker = _parse_constraint(not_inside) if not_inside else None

    def on_visit(self, node: cst.CSTNode) -> bool:
        """Track ancestors and check if this node matches the pattern."""
        # Push current node onto ancestor stack
        self.ancestor_stack.append(node)

        # Try to match this node
        if m.matches(node, self.matcher):
            # Validate repeated metavars captured equal values
            if not _validate_repeated_metavars(node, self.matcher, self.metavar_names):
                return True  # Continue visiting, but don't add this as a match

            # Extract captures
            captures = m.extract(node, self.matcher)

            # Handle ellipsis and partial dict captures
            _extract_ellipsis_and_partial_captures(node, self.ellipsis_info, captures)

            # Check if match satisfies inside/not_inside constraint
            if self._satisfies_constraint():
                # Get line number if position provider is available
                line = None
                if self.position_provider is not None:
                    try:
                        pos = self.position_provider[node]
                        line = pos.start.line
                    except KeyError:
                        pass  # Node position not available

                self.matches.append(PatternMatch(node=node, captures=captures, line=line))

        return True  # Continue visiting children

    def on_leave(self, original_node: cst.CSTNode) -> None:
        """Pop from ancestor stack when leaving a node."""
        if self.ancestor_stack and self.ancestor_stack[-1] is original_node:
            self.ancestor_stack.pop()

    def _satisfies_constraint(self) -> bool:
        """Check if current match satisfies the inside/not_inside constraint."""
        if self._inside_checker:
            # Must be inside at least one matching ancestor
            # Don't check the current node (last item), check ancestors only
            return any(self._inside_checker(ancestor) for ancestor in self.ancestor_stack[:-1])

        if self._not_inside_checker:
            # Must NOT be inside any matching ancestor
            # Don't check the current node (last item), check ancestors only
            return not any(self._not_inside_checker(ancestor) for ancestor in self.ancestor_stack[:-1])

        return True  # No constraint


class ScopedPatternFinder(cst.CSTVisitor):
    """Visitor to find all matches of a pattern within a specific scope."""

    def __init__(self, matcher: m.BaseMatcherNode, ellipsis_info: dict | None = None, position_provider: cst.metadata.PositionProvider | None = None, scope: list[str] | None = None, metavar_names: set[str] | None = None):
        self.matcher = matcher
        self.ellipsis_info = ellipsis_info or {}
        self.position_provider = position_provider
        self.scope = scope or []
        self.metavar_names = metavar_names or set()
        self.current_path: list[str] = []
        self.matches: list[PatternMatch] = []

    def _is_in_scope(self) -> bool:
        """Check if we're currently inside the target scope."""
        if len(self.current_path) < len(self.scope):
            return False
        return self.current_path[:len(self.scope)] == self.scope

    def _track_scope_entry(self, node: cst.CSTNode, name: str) -> None:
        """Track entering a scope (class or function)."""
        self.current_path.append(name)
        # Store a marker so we know to pop on leave
        if not hasattr(self, '_scope_stack'):
            self._scope_stack = []
        self._scope_stack.append((id(node), name))

    def _track_scope_exit(self, node: cst.CSTNode) -> None:
        """Track leaving a scope."""
        if hasattr(self, '_scope_stack') and self._scope_stack:
            node_id, name = self._scope_stack[-1]
            if node_id == id(node):
                self._scope_stack.pop()
                if self.current_path and self.current_path[-1] == name:
                    self.current_path.pop()

    def on_visit(self, node: cst.CSTNode) -> bool:
        """Check if this node matches the pattern and is in scope."""
        # Track scope for function and class definitions
        if isinstance(node, cst.FunctionDef):
            self._track_scope_entry(node, node.name.value)
        elif isinstance(node, cst.ClassDef):
            self._track_scope_entry(node, node.name.value)

        if not self._is_in_scope():
            return True  # Continue visiting to find the scope

        # Try to match this node
        if m.matches(node, self.matcher):
            # Validate repeated metavars captured equal values
            if not _validate_repeated_metavars(node, self.matcher, self.metavar_names):
                return True  # Continue visiting, but don't add this as a match

            # Extract captures
            captures = m.extract(node, self.matcher)

            # Handle ellipsis and partial dict captures
            _extract_ellipsis_and_partial_captures(node, self.ellipsis_info, captures)

            # Get line number if position provider is available
            line = None
            if self.position_provider is not None:
                try:
                    pos = self.position_provider[node]
                    line = pos.start.line
                except KeyError:
                    pass  # Node position not available

            self.matches.append(PatternMatch(node=node, captures=captures, line=line))
        return True  # Continue visiting children

    def on_leave(self, original_node: cst.CSTNode) -> None:
        """Track leaving scopes."""
        if isinstance(original_node, (cst.FunctionDef, cst.ClassDef)):
            self._track_scope_exit(original_node)


class _ImportOriginCollector(cst.CSTVisitor):
    """Collect QualifiedNames for all Name nodes, keyed by node identity."""

    METADATA_DEPENDENCIES = (cst.metadata.QualifiedNameProvider,)

    def __init__(self):
        self.qnames_by_id: dict[int, set] = {}

    def visit_Name(self, node: cst.Name) -> None:
        try:
            qnames = self.get_metadata(cst.metadata.QualifiedNameProvider, node)
            # Store as list of (name, source) tuples
            self.qnames_by_id[id(node)] = qnames
        except KeyError:
            pass


def _filter_matches_by_import(
    matches: list[PatternMatch],
    imported_from: str,
    wrapper: cst.metadata.MetadataWrapper,
) -> list[PatternMatch]:
    """Post-filter pattern matches to only include those where the root name
    is imported from the specified module.

    Uses QualifiedNameProvider to resolve the qualified name of the leftmost
    Name node in each match. If the QN starts with ``imported_from.``, the
    match is kept.
    """
    collector = _ImportOriginCollector()
    wrapper.visit(collector)

    filtered = []
    for match in matches:
        node = match.node
        # Walk to the leftmost Name node (for dotted access like json.loads)
        root_name = node
        while isinstance(root_name, cst.Call):
            root_name = root_name.func
        while isinstance(root_name, cst.Attribute):
            root_name = root_name.value

        if not isinstance(root_name, cst.Name):
            continue

        qnames = collector.qnames_by_id.get(id(root_name), set())

        for qn in qnames:
            # Only consider IMPORT-sourced names (not LOCAL definitions)
            if qn.source == cst.metadata.QualifiedNameSource.LOCAL:
                continue
            # Match if QN starts with module prefix (e.g. "json.loads")
            # or if QN equals the module name itself
            if qn.name == imported_from or qn.name.startswith(imported_from + "."):
                filtered.append(match)
                break
    return filtered


def _assign_line_numbers_from_source(
    matches: list[PatternMatch],
    source_code: str,
    module: cst.Module,
) -> None:
    """Assign line numbers to pattern matches without using MetadataWrapper.

    Computes line numbers by generating the code for each matched node and
    finding its position in the source text. This is much cheaper than
    MetadataWrapper which requires a deep_clone + full code generation pass.
    """
    if not matches:
        return

    import bisect
    # Build a newline offset table for the source
    line_starts = [0]
    for i, ch in enumerate(source_code):
        if ch == '\n':
            line_starts.append(i + 1)

    def offset_to_line(offset: int) -> int:
        """Convert a character offset to a 1-based line number."""
        return bisect.bisect_right(line_starts, offset)

    # For each match, find its code in the source to determine line number.
    # Track search positions to handle duplicate code correctly - matches
    # come from a DFS walk so they appear in source order.
    search_start = 0
    for match in matches:
        code_snippet = module.code_for_node(match.node).lstrip()
        idx = source_code.find(code_snippet, search_start)
        if idx < 0:
            # If not found from current position, search from beginning
            idx = source_code.find(code_snippet)
        if idx >= 0:
            match.line = offset_to_line(idx)
            search_start = idx + 1


def find_pattern(
    pattern_str: str,
    file_path: str,
    scope: list[str] | None = None,
    inside: str | None = None,
    not_inside: str | None = None,
    imported_from: str | None = None,
    where: str | None = None,
    scope_local: bool = False,
    source_override: str | None = None,
    type_oracle: TypeOracle | None = None,
) -> list[PatternMatch]:
    """Find all matches of pattern in file.

    Args:
        pattern_str: Pattern string with metavariables like "print($X)"
        file_path: Path to Python file to search
        scope: Optional symbol path to limit matches to (e.g., ["MyClass", "method"])
        inside: Optional constraint - only match inside this structure.
                Keywords: "def", "async def", "class", "for", "while", "try", "with", "if".
                Patterns: "def test_*", "class MyClass", "try:", "except ValueError:".
        not_inside: Optional constraint - only match outside this structure.
                    Supports same syntax as inside.
        imported_from: Optional module name - only match when the root name
                       in the pattern is imported from this module
        where: Optional constraint - only match inside a structure matching
               this pattern (e.g., 'class MyClass', 'def test_*').
               Alias for inside with pattern support.
        scope_local: If True, only match names that are locally defined
                     (not imported). Uses QualifiedNameProvider.
        source_override: If provided, search this source string instead of reading from file_path.
        type_oracle: Optional TypeOracle instance for :type[X] and :returns[X] constraints.

    Returns:
        List of matches with locations and captured values

    Example:
        >>> matches = find_pattern("print($X)", "file.py")
        >>> len(matches)
        2
        >>> matches[0].captures['X']
        <SimpleString node>

        # Scoped search within a function:
        >>> matches = find_pattern("old_name", "file.py", scope=["my_func"])

        # Find only inside functions:
        >>> matches = find_pattern("print($X)", "file.py", inside="def")

        # Find only inside test functions:
        >>> matches = find_pattern("print($X)", "file.py", inside="def test_*")

        # Only match json.loads when json is the real json module:
        >>> matches = find_pattern("json.loads($X)", "file.py", imported_from="json")

        # Find inside specific class:
        >>> matches = find_pattern("$X = $Y", "file.py", where="class MyClass")
    """
    # Handle --where as alias for --inside
    if where is not None:
        if inside is not None:
            raise ValueError("Cannot specify both 'where' and 'inside' parameters")
        inside = where

    # Validate inside/not_inside constraints
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")

    # Parse pattern and compile to matcher
    pattern = parse_pattern(pattern_str)
    matcher, ellipsis_info = compile_pattern_to_matcher(pattern)

    # Extract metavar names for validation
    metavar_names = {mv.name for mv in pattern.metavars}

    # Detect oracle type constraints on metavars
    oracle_constraints: dict[str, tuple[str, str]] = {}
    for mv in pattern.metavars:
        if is_oracle_type_constraint(mv.type_constraint):
            oracle_constraints[mv.name] = parse_oracle_type_constraint(mv.type_constraint)

    # Read and parse file (or use source_override)
    if source_override is not None:
        source_code = source_override
    else:
        file = Path(file_path)
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        source_code = file.read_text()
    module = _cached_parse(source_code)

    # Fast path: for basic pattern matching (no constraints, no scope,
    # no import/scope filters), skip the expensive MetadataWrapper and
    # compute line numbers from the source text afterwards.
    # Oracle type constraints need position info for TypeOracle lookups.
    needs_wrapper = bool(inside or not_inside or scope is not None
                         or imported_from is not None or scope_local
                         or oracle_constraints)

    if not needs_wrapper:
        # Basic pattern matching without MetadataWrapper
        finder = PatternFinder(matcher, ellipsis_info, None, metavar_names)
        module.visit(finder)
        # Compute line numbers from source text for each match
        if finder.matches:
            _assign_line_numbers_from_source(finder.matches, source_code, module)
        return finder.matches

    # Full path: use MetadataWrapper for position info and post-filters
    wrapper = cst.MetadataWrapper(module)
    position_provider = wrapper.resolve(cst.metadata.PositionProvider)

    # Find all matches - choose finder based on parameters
    if inside or not_inside:
        # Use constrained finder for inside/not_inside constraints
        finder = ConstrainedPatternFinder(matcher, ellipsis_info, position_provider, inside, not_inside, metavar_names)
    elif scope is not None:
        # Use scoped finder for scope-based searching
        finder = ScopedPatternFinder(matcher, ellipsis_info, position_provider, scope, metavar_names)
    else:
        # Use basic finder for unconstrained searching
        finder = PatternFinder(matcher, ellipsis_info, position_provider, metavar_names)
    wrapper.visit(finder)

    matches = finder.matches

    # Post-filter by import origin if requested
    if imported_from is not None:
        matches = _filter_matches_by_import(matches, imported_from, wrapper)

    # Post-filter by scope locality if requested
    if scope_local:
        matches = _filter_matches_by_scope_local(matches, wrapper)

    # Post-filter by TypeOracle type constraints
    if oracle_constraints and type_oracle is not None:
        matches = _filter_matches_by_type_oracle(
            matches, oracle_constraints, type_oracle,
            file_path, position_provider,
        )

    return matches


def _filter_matches_by_scope_local(
    matches: list[PatternMatch],
    wrapper: cst.metadata.MetadataWrapper,
) -> list[PatternMatch]:
    """Post-filter pattern matches to only include those where the root name
    is locally defined (not imported).

    Uses QualifiedNameProvider to check if the root Name node in each match
    has a LOCAL source.
    """
    collector = _ImportOriginCollector()
    wrapper.visit(collector)

    filtered = []
    for match in matches:
        node = match.node
        # Walk to the leftmost Name node
        root_name = node
        while isinstance(root_name, cst.Call):
            root_name = root_name.func
        while isinstance(root_name, cst.Attribute):
            root_name = root_name.value

        if not isinstance(root_name, cst.Name):
            # Non-name matches are kept (e.g., literals)
            filtered.append(match)
            continue

        qnames = collector.qnames_by_id.get(id(root_name), set())

        if not qnames:
            # No QN info -- keep the match (could be a builtin or unresolved)
            filtered.append(match)
            continue

        # Keep if at least one QN has LOCAL source
        if any(qn.source == cst.metadata.QualifiedNameSource.LOCAL for qn in qnames):
            filtered.append(match)

    return filtered


def _filter_matches_by_type_oracle(
    matches: list[PatternMatch],
    oracle_constraints: dict[str, tuple[str, str]],
    type_oracle: TypeOracle,
    file_path: str,
    position_provider: Mapping,
) -> list[PatternMatch]:
    """Post-filter pattern matches using TypeOracle type constraints.

    For each match, checks whether captured metavar values satisfy their
    :type[X] or :returns[X] constraints by querying the TypeOracle for
    inferred types at the capture's source position.

    Args:
        matches: Pattern matches to filter.
        oracle_constraints: Mapping from metavar name to (kind, type_string)
            where kind is 'type' or 'returns'.
        type_oracle: A TypeOracle instance.
        file_path: Path to the source file being searched.
        position_provider: Resolved LibCST PositionProvider metadata
            (mapping from CSTNode to CodeRange).
    """
    from .type_oracle import FileTypes, TypeDescriptor, parse_type_string

    # Build the type index for this file once
    file_types: FileTypes = type_oracle.infer_file(Path(file_path))
    logger.debug("Filtering %d matches by type oracle constraints", len(matches))

    filtered = []
    for match in matches:
        keep = True
        for metavar_name, (kind, type_string) in oracle_constraints.items():
            captured = match.captures.get(metavar_name)
            if captured is None:
                continue

            constraint_td = parse_type_string(type_string)

            if kind == "type":
                # Look up the inferred type at the captured node's position
                try:
                    pos = position_provider[captured]
                    binding = file_types.type_at(pos.start.line, pos.start.column + 1)
                except (KeyError, AttributeError):
                    binding = None

                if binding is None:
                    # No type info — skip this match (can't confirm)
                    keep = False
                    break

                if not binding.type_descriptor.matches(constraint_td):
                    keep = False
                    break

            elif kind == "returns":
                # For :returns[X], the captured node should be a function definition.
                # Look up the function's return type from the oracle.
                try:
                    pos = position_provider[captured]
                    line = pos.start.line
                    col = pos.start.column + 1
                except (KeyError, AttributeError):
                    keep = False
                    break

                matched_return = False

                # Try exact positional lookup first (O(1))
                binding = file_types.type_at(line, col)
                if binding is not None and binding.binding_kind == "definition":
                    td = binding.type_descriptor
                    if td.kind == "callable" and td.return_type is not None:
                        matched_return = td.return_type.matches(constraint_td)
                    elif td.matches(constraint_td):
                        matched_return = True

                # Fall back to name-based lookup if positional miss
                if not matched_return and isinstance(captured, cst.Name):
                    for b in file_types.types_for_name(captured.value):
                        if b.line == line and b.binding_kind == "definition":
                            td = b.type_descriptor
                            if td.kind == "callable" and td.return_type is not None:
                                matched_return = td.return_type.matches(constraint_td)
                            elif td.matches(constraint_td):
                                matched_return = True
                            if matched_return:
                                break

                if not matched_return:
                    keep = False
                    break

        if keep:
            filtered.append(match)

    logger.debug(
        "Type oracle filtering: %d/%d matches passed", len(filtered), len(matches),
    )
    return filtered


class PatternReplacer(cst.CSTTransformer):
    """Transformer to replace pattern matches with replacement template."""

    def __init__(self, matcher: m.BaseMatcherNode, pattern: Pattern, replacement_str: str, ellipsis_info: dict | None = None):
        self.matcher = matcher
        self.pattern = pattern
        self.replacement_str = replacement_str
        self.ellipsis_info = ellipsis_info or {}
        self.modified = False
        self.replacement_count = 0

    def _do_replacement(self, node: cst.CSTNode) -> cst.CSTNode | None:
        """Try to replace node if it matches pattern. Returns replacement or None."""
        # Try to match this node
        if m.matches(node, self.matcher):
            # Extract captures
            captures = m.extract(node, self.matcher)

            # Handle ellipsis and partial dict captures
            _extract_ellipsis_and_partial_captures(node, self.ellipsis_info, captures)

            # Build replacement by substituting metavars
            replacement_code = self.replacement_str

            # First pass: resolve ${NAME.content} references (string
            # interpolation).  These extract the inner content of a string
            # literal, stripping the surrounding quotes.  If any reference
            # cannot be resolved (e.g. the captured node is not a string
            # literal), skip the entire replacement to avoid producing
            # nonsense output.
            content_failed = False
            for ref_match in _CONTENT_REF_RE.finditer(replacement_code):
                ref_name = ref_match.group(1)
                captured = captures.get(ref_name)
                if (
                    captured is None
                    or isinstance(captured, tuple)
                    or (content := _extract_string_content(captured)) is None
                ):
                    content_failed = True
                    break
                replacement_code = replacement_code.replace(
                    ref_match.group(0), content
                )
            if content_failed:
                return None

            # Second pass: substitute regular metavar references ($NAME,
            # $...NAME).
            for name, captured_node in captures.items():
                # Handle ellipsis captures (tuples of args/elements)
                if isinstance(captured_node, tuple):
                    # Convert each item to code and join with commas
                    if len(captured_node) == 0:
                        code = ""
                    else:
                        # Unwrap based on type: Arg.value, Element.value, or whole DictElement
                        item_codes = []
                        for item in captured_node:
                            if isinstance(item, cst.Arg):
                                # Preserve full Arg node (keyword=, *, **) but strip trailing comma
                                clean_arg = item.with_changes(comma=cst.MaybeSentinel.DEFAULT)
                                item_codes.append(cst.Module([]).code_for_node(clean_arg))
                            elif isinstance(item, cst.Element):
                                item_codes.append(cst.Module([]).code_for_node(item.value))
                            elif isinstance(item, cst.DictElement):
                                # For dict elements, include key: value
                                key_code = cst.Module([]).code_for_node(item.key)
                                value_code = cst.Module([]).code_for_node(item.value)
                                item_codes.append(f"{key_code}: {value_code}")
                            else:
                                item_codes.append(cst.Module([]).code_for_node(item))
                        code = ", ".join(item_codes)
                    # Replace $...NAME with the comma-separated items
                    replacement_code = replacement_code.replace(f"$...{name}", code)
                else:
                    # Convert captured node to code string
                    code = cst.Module([]).code_for_node(captured_node)
                    # Replace metavar in replacement string
                    replacement_code = replacement_code.replace(f"${name}", code)

            # Clean up comma artifacts from empty ellipsis substitutions
            replacement_code = re.sub(r'(\()\s*,\s*', r'\1', replacement_code)
            replacement_code = re.sub(r'(\[)\s*,\s*', r'\1', replacement_code)
            replacement_code = re.sub(r',\s*,', ',', replacement_code)

            # Try to parse replacement - first as expression, then as statement
            try:
                new_node = cst.parse_expression(replacement_code)
                self.modified = True
                self.replacement_count += 1
                return new_node
            except Exception:
                # Try parsing as statement
                try:
                    temp_module = cst.parse_module(replacement_code)
                    if temp_module.body:
                        # Return the statement (or expression statement)
                        stmt = temp_module.body[0]
                        # If it's a SimpleStatementLine with a single statement, extract it
                        if isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
                            self.modified = True
                            self.replacement_count += 1
                            return stmt.body[0]
                        else:
                            self.modified = True
                            self.replacement_count += 1
                            return stmt
                except Exception:
                    # If replacement doesn't parse, return None
                    return None

        return None

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        """Replace Call nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.BaseExpression:
        """Replace Name nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Float(self, original_node: cst.Float, updated_node: cst.Float) -> cst.BaseExpression:
        """Replace Float nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.BaseExpression:
        """Replace Attribute nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_BinaryOperation(self, original_node: cst.BinaryOperation, updated_node: cst.BinaryOperation) -> cst.BaseExpression:
        """Replace BinaryOperation nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Comparison(self, original_node: cst.Comparison, updated_node: cst.Comparison) -> cst.BaseExpression:
        """Replace Comparison nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_BooleanOperation(self, original_node: cst.BooleanOperation, updated_node: cst.BooleanOperation) -> cst.BaseExpression:
        """Replace BooleanOperation nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_UnaryOperation(self, original_node: cst.UnaryOperation, updated_node: cst.UnaryOperation) -> cst.BaseExpression:
        """Replace UnaryOperation nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Subscript(self, original_node: cst.Subscript, updated_node: cst.Subscript) -> cst.BaseExpression:
        """Replace Subscript nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_IfExp(self, original_node: cst.IfExp, updated_node: cst.IfExp) -> cst.BaseExpression:
        """Replace IfExp (ternary) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Await(self, original_node: cst.Await, updated_node: cst.Await) -> cst.BaseExpression:
        """Replace Await nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Tuple(self, original_node: cst.Tuple, updated_node: cst.Tuple) -> cst.BaseExpression:
        """Replace Tuple nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_List(self, original_node: cst.List, updated_node: cst.List) -> cst.BaseExpression:
        """Replace List nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Set(self, original_node: cst.Set, updated_node: cst.Set) -> cst.BaseExpression:
        """Replace Set nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Dict(self, original_node: cst.Dict, updated_node: cst.Dict) -> cst.BaseExpression:
        """Replace Dict nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Lambda(self, original_node: cst.Lambda, updated_node: cst.Lambda) -> cst.BaseExpression:
        """Replace Lambda nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_NamedExpr(self, original_node: cst.NamedExpr, updated_node: cst.NamedExpr) -> cst.BaseExpression:
        """Replace NamedExpr (walrus operator) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.BaseSmallStatement:
        """Replace Assign nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_AugAssign(self, original_node: cst.AugAssign, updated_node: cst.AugAssign) -> cst.BaseSmallStatement:
        """Replace AugAssign (augmented assignment) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Return(self, original_node: cst.Return, updated_node: cst.Return) -> cst.BaseSmallStatement:
        """Replace Return statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Assert(self, original_node: cst.Assert, updated_node: cst.Assert) -> cst.BaseSmallStatement:
        """Replace Assert statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Raise(self, original_node: cst.Raise, updated_node: cst.Raise) -> cst.BaseSmallStatement:
        """Replace Raise statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Del(self, original_node: cst.Del, updated_node: cst.Del) -> cst.BaseSmallStatement:
        """Replace Del statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        if replacement is not None:
            # If replacement is already a statement, return it
            if isinstance(replacement, cst.BaseSmallStatement):
                return replacement
            # If replacement is an expression, wrap it in Expr
            elif isinstance(replacement, cst.BaseExpression):
                return cst.Expr(value=replacement)
        return updated_node

    def leave_Global(self, original_node: cst.Global, updated_node: cst.Global) -> cst.BaseSmallStatement:
        """Replace Global statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Nonlocal(self, original_node: cst.Nonlocal, updated_node: cst.Nonlocal) -> cst.BaseSmallStatement:
        """Replace Nonlocal statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> cst.BaseSmallStatement:
        """Replace ImportFrom statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> cst.BaseSmallStatement:
        """Replace Import statement nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_ListComp(self, original_node: cst.ListComp, updated_node: cst.ListComp) -> cst.BaseExpression:
        """Replace ListComp (list comprehension) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_SetComp(self, original_node: cst.SetComp, updated_node: cst.SetComp) -> cst.BaseExpression:
        """Replace SetComp (set comprehension) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_DictComp(self, original_node: cst.DictComp, updated_node: cst.DictComp) -> cst.BaseExpression:
        """Replace DictComp (dict comprehension) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_GeneratorExp(self, original_node: cst.GeneratorExp, updated_node: cst.GeneratorExp) -> cst.BaseExpression:
        """Replace GeneratorExp (generator expression) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_FormattedString(self, original_node: cst.FormattedString, updated_node: cst.FormattedString) -> cst.BaseExpression:
        """Replace FormattedString (f-string) nodes that match the pattern."""
        replacement = self._do_replacement(updated_node)
        return replacement if replacement is not None else updated_node

    def leave_Expr(self, original_node: cst.Expr, updated_node: cst.Expr) -> cst.BaseSmallStatement:
        """Replace Expr statement nodes when the inner expression matches."""
        # Check if the inner value matches
        replacement = self._do_replacement(updated_node.value)
        if replacement is not None:
            # If replacement is a statement, return it directly
            if isinstance(replacement, cst.BaseSmallStatement):
                return replacement
            # If replacement is an expression, wrap it in Expr
            elif isinstance(replacement, cst.BaseExpression):
                return updated_node.with_changes(value=replacement)
        return updated_node


class _PositionFilteredReplacer(PatternReplacer):
    """Replacer that only modifies nodes at pre-approved line positions.

    Used when ``find_pattern`` has already determined which positions satisfy
    oracle type constraints (or other post-filters).  The replacer uses
    ``PositionProvider`` metadata to check ``original_node`` positions in each
    ``leave_*`` method before delegating to the parent replacer.

    Note: overriding ``on_visit``/``on_leave`` would disable LibCST's specific
    ``leave_*`` dispatch, so we override each ``leave_*`` method individually
    via ``_make_position_filtered_leave``.
    """

    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider,)

    def __init__(
        self,
        matcher: m.BaseMatcherNode,
        pattern: Pattern,
        replacement_str: str,
        ellipsis_info: dict | None = None,
        allowed_lines: set[int] | None = None,
    ):
        super().__init__(matcher, pattern, replacement_str, ellipsis_info)
        self.allowed_lines: set[int] = allowed_lines or set()

    def _position_allowed(self, original_node: cst.CSTNode) -> bool:
        try:
            pos = self.get_metadata(cst.metadata.PositionProvider, original_node)
            return pos.start.line in self.allowed_lines
        except KeyError:
            return False


def _make_position_filtered_leave(base_method):
    """Create a leave_* override that checks position before delegating."""
    def leave_method(self, original_node, updated_node):
        if not self._position_allowed(original_node):
            return updated_node
        return base_method(self, original_node, updated_node)
    return leave_method


# Dynamically override every leave_* method from PatternReplacer so that each
# checks position before delegating to the parent implementation.
for _name in list(vars(PatternReplacer)):
    if _name.startswith("leave_"):
        _base = getattr(PatternReplacer, _name)
        setattr(_PositionFilteredReplacer, _name, _make_position_filtered_leave(_base))


class ConstrainedPatternReplacer(PatternReplacer):
    """Transformer to replace pattern matches with inside/not_inside constraints.

    Tracks ancestors using visit_* methods to push onto stack and leave_* methods to pop.
    """

    # Use same keyword checkers as ConstrainedPatternFinder
    KEYWORD_CHECKERS = {
        "def": lambda node: isinstance(node, cst.FunctionDef),
        "async def": lambda node: isinstance(node, cst.FunctionDef) and node.asynchronous is not None,
        "class": lambda node: isinstance(node, cst.ClassDef),
        "for": lambda node: isinstance(node, (cst.For, cst.ListComp, cst.SetComp, cst.DictComp, cst.GeneratorExp)),
        "while": lambda node: isinstance(node, cst.While),
        "with": lambda node: isinstance(node, cst.With),
        "try": lambda node: isinstance(node, (cst.Try, cst.TryStar)),
        "if": lambda node: isinstance(node, (cst.If, cst.IfExp)),
    }

    def __init__(
        self,
        matcher: m.BaseMatcherNode,
        pattern: Pattern,
        replacement_str: str,
        ellipsis_info: dict | None = None,
        inside: str | None = None,
        not_inside: str | None = None,
    ):
        super().__init__(matcher, pattern, replacement_str, ellipsis_info)
        self.inside = inside
        self.not_inside = not_inside
        self.ancestor_stack: list[cst.CSTNode] = []

        # Parse constraints into checker functions
        self._inside_checker = _parse_constraint(inside) if inside else None
        self._not_inside_checker = _parse_constraint(not_inside) if not_inside else None

    def _satisfies_constraint(self) -> bool:
        """Check if current context satisfies the inside/not_inside constraint."""
        if self._inside_checker:
            # Must be inside at least one matching ancestor
            return any(self._inside_checker(ancestor) for ancestor in self.ancestor_stack)

        if self._not_inside_checker:
            # Must NOT be inside any matching ancestor
            return not any(self._not_inside_checker(ancestor) for ancestor in self.ancestor_stack)

        return True  # No constraint

    def _do_replacement(self, node: cst.CSTNode) -> cst.CSTNode | None:
        """Override to check constraint before performing replacement."""
        # Only perform replacement if constraint is satisfied
        if self._satisfies_constraint():
            return super()._do_replacement(node)
        return None

    # Override visit/leave for all node types that might be constraints
    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        self.ancestor_stack.pop()
        return updated_node

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        self.ancestor_stack.pop()
        return updated_node

    def visit_For(self, node: cst.For) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_For(self, original_node: cst.For, updated_node: cst.For) -> cst.For:
        self.ancestor_stack.pop()
        return updated_node

    def visit_While(self, node: cst.While) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_While(self, original_node: cst.While, updated_node: cst.While) -> cst.While:
        self.ancestor_stack.pop()
        return updated_node

    def visit_With(self, node: cst.With) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_With(self, original_node: cst.With, updated_node: cst.With) -> cst.With:
        self.ancestor_stack.pop()
        return updated_node

    def visit_Try(self, node: cst.Try) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_Try(self, original_node: cst.Try, updated_node: cst.Try) -> cst.Try:
        self.ancestor_stack.pop()
        return updated_node

    def visit_TryStar(self, node: cst.TryStar) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_TryStar(self, original_node: cst.TryStar, updated_node: cst.TryStar) -> cst.TryStar:
        self.ancestor_stack.pop()
        return updated_node

    def visit_If(self, node: cst.If) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_If(self, original_node: cst.If, updated_node: cst.If) -> cst.If:
        self.ancestor_stack.pop()
        return updated_node

    def visit_IfExp(self, node: cst.IfExp) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_IfExp(self, original_node: cst.IfExp, updated_node: cst.IfExp) -> cst.BaseExpression:
        result = super().leave_IfExp(original_node, updated_node)
        self.ancestor_stack.pop()
        return result

    # Comprehensions (for "for" constraint)
    def visit_ListComp(self, node: cst.ListComp) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_ListComp(self, original_node: cst.ListComp, updated_node: cst.ListComp) -> cst.BaseExpression:
        result = super().leave_ListComp(original_node, updated_node)
        self.ancestor_stack.pop()
        return result

    def visit_SetComp(self, node: cst.SetComp) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_SetComp(self, original_node: cst.SetComp, updated_node: cst.SetComp) -> cst.BaseExpression:
        result = super().leave_SetComp(original_node, updated_node)
        self.ancestor_stack.pop()
        return result

    def visit_DictComp(self, node: cst.DictComp) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_DictComp(self, original_node: cst.DictComp, updated_node: cst.DictComp) -> cst.BaseExpression:
        result = super().leave_DictComp(original_node, updated_node)
        self.ancestor_stack.pop()
        return result

    def visit_GeneratorExp(self, node: cst.GeneratorExp) -> bool:
        self.ancestor_stack.append(node)
        return True

    def leave_GeneratorExp(self, original_node: cst.GeneratorExp, updated_node: cst.GeneratorExp) -> cst.BaseExpression:
        result = super().leave_GeneratorExp(original_node, updated_node)
        self.ancestor_stack.pop()
        return result


class ScopedPatternReplacer(PatternReplacer):
    """Transformer to replace pattern matches only within a specific scope."""

    def __init__(self, matcher: m.BaseMatcherNode, pattern: Pattern, replacement_str: str, scope: list[str], ellipsis_info: dict | None = None):
        super().__init__(matcher, pattern, replacement_str, ellipsis_info)
        self.scope = scope
        self.current_path: list[str] = []

    def _is_in_scope(self) -> bool:
        """Check if we're currently inside the target scope."""
        if len(self.current_path) < len(self.scope):
            return False
        return self.current_path[:len(self.scope)] == self.scope

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        """Visit class definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        """Leave class definition."""
        self.current_path.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        """Visit function definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        """Leave function definition."""
        self.current_path.pop()
        return updated_node

    def _do_replacement(self, node: cst.CSTNode) -> cst.CSTNode | None:
        """Try to replace node if it matches pattern and we're in scope. Returns replacement or None."""
        if not self._is_in_scope():
            return None
        return super()._do_replacement(node)


class SymbolRemover(cst.CSTTransformer):
    """Transformer to remove a symbol by path."""

    def __init__(self, target_path: list[str]):
        self.target_path = target_path
        self.current_path: list[str] = []
        self.removed = False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        """Visit class definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef | cst.RemovalSentinel:
        """Leave class definition, possibly removing it."""
        if self.current_path == self.target_path:
            self.removed = True
            self.current_path.pop()
            return cst.RemovalSentinel.REMOVE
        self.current_path.pop()
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        """Visit function definition."""
        self.current_path.append(node.name.value)
        return True

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef | cst.RemovalSentinel:
        """Leave function definition, possibly removing it."""
        if self.current_path == self.target_path:
            self.removed = True
            self.current_path.pop()
            return cst.RemovalSentinel.REMOVE
        self.current_path.pop()
        return updated_node


def remove_symbol(selector: ExtendedSelector, apply: bool = False) -> str:
    """Remove a symbol (function, class) from a file.

    Args:
        selector: Extended selector specifying the symbol to remove
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found
    """
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()
    module = cst.parse_module(source_code)

    # Validate that symbol exists
    finder = SymbolFinder(selector.symbol_path)
    module.visit(finder)

    if finder.found_node is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    # Apply transformation
    remover = SymbolRemover(selector.symbol_path)
    new_module = module.visit(remover)

    if not remover.removed:
        raise ValueError(f"Failed to remove symbol {'.'.join(selector.symbol_path)}")

    new_code = new_module.code

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def get_symbol_source(selector: ExtendedSelector, dedent: bool = False) -> str:
    """Get the complete source code of a symbol including decorators.

    Args:
        selector: Extended selector specifying the symbol
        dedent: If True, remove leading indentation

    Returns:
        String containing the complete source code of the symbol

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found
    """
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    # Handle line-based selectors (file.py:42 or file.py:10-20)
    if selector.line_start is not None:
        # Read the lines directly
        with open(file_path) as f:
            lines = f.readlines()

        # Extract the specified lines (1-indexed)
        start_idx = selector.line_start - 1
        end_idx = (selector.line_end or selector.line_start) - 1

        if start_idx < 0 or end_idx >= len(lines):
            raise ValueError(f"Line range {selector.line_start}-{selector.line_end or selector.line_start} out of bounds")

        code = ''.join(lines[start_idx:end_idx + 1])

        if dedent:
            import textwrap
            code = textwrap.dedent(code)

        return code

    # Handle symbol-based selectors
    source_code = file_path.read_text()
    module = cst.parse_module(source_code)

    # Find the symbol
    finder = SymbolFinder(selector.symbol_path)
    module.visit(finder)

    if finder.found_node is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    # Convert the node to source code
    code = module.code_for_node(finder.found_node)

    if dedent:
        import textwrap
        code = textwrap.dedent(code)

    return code


class _NameCollector(cst.CSTVisitor):
    """Visitor to collect all Name references in code."""

    def __init__(self):
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        """Collect name references."""
        self.names.add(node.value)

    def visit_Attribute(self, node: cst.Attribute) -> None:
        """Collect the base name from attribute access (e.g., 'ast' from 'ast.parse')."""
        # Only collect the leftmost name in the chain
        current = node.value
        while isinstance(current, cst.Attribute):
            current = current.value
        if isinstance(current, cst.Name):
            self.names.add(current.value)


def analyze_imports(symbol_source: str, source_file: str) -> list[str]:
    """Analyze which imports from source_file are needed by symbol_source.

    Args:
        symbol_source: Source code of the symbol being copied
        source_file: Path to file where symbol originated (to read imports from)

    Returns:
        List of import statement strings needed for the symbol

    Example:
        >>> source = "def func():\\n    return ast.parse('x = 1')"
        >>> imports = analyze_imports(source, "module.py")
        >>> # Returns ["import ast"] if module.py has that import
    """
    # Parse the symbol source to collect all name references
    try:
        symbol_module = cst.parse_module(symbol_source)
    except Exception:
        # If we can't parse, return empty - better than crashing
        return []

    collector = _NameCollector()
    symbol_module.visit(collector)
    used_names = collector.names

    # Parse the source file to get its imports
    source_path = Path(source_file)
    if not source_path.exists():
        return []

    try:
        source_module = cst.parse_module(source_path.read_text())
    except Exception:
        return []

    # Collect needed imports
    needed_imports = []

    for stmt in source_module.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            for inner_stmt in stmt.body:
                if isinstance(inner_stmt, cst.Import):
                    # Handle "import X" or "import X as Y"
                    for name_item in inner_stmt.names:
                        if isinstance(name_item, cst.ImportAlias):
                            # Get the module name
                            module_name = name_item.name.value if isinstance(name_item.name, cst.Name) else str(name_item.name)
                            # Get the alias if present
                            if name_item.asname:
                                alias = name_item.asname.name.value
                                # Check if alias is used
                                if alias in used_names:
                                    needed_imports.append(cst.Module([stmt]).code.strip())
                                    break
                            else:
                                # Check if module name is used
                                if module_name in used_names:
                                    needed_imports.append(f"import {module_name}")

                elif isinstance(inner_stmt, cst.ImportFrom):
                    # Handle "from X import Y, Z"
                    if isinstance(inner_stmt.module, cst.Name):
                        module_name = inner_stmt.module.value
                    elif isinstance(inner_stmt.module, cst.Attribute):
                        # Handle dotted imports like "from a.b import c"
                        module_name = cst.Module([]).code_for_node(inner_stmt.module)
                    else:
                        continue

                    if isinstance(inner_stmt.names, cst.ImportStar):
                        # Can't analyze star imports easily, skip
                        continue

                    # Collect which names are actually used
                    used_import_names = []
                    for name_item in inner_stmt.names:
                        if isinstance(name_item, cst.ImportAlias):
                            import_name = name_item.name.value if isinstance(name_item.name, cst.Name) else str(name_item.name)
                            # Check if the imported name or its alias is used
                            if name_item.asname:
                                alias = name_item.asname.name.value
                                if alias in used_names:
                                    used_import_names.append((import_name, alias))
                            else:
                                if import_name in used_names:
                                    used_import_names.append((import_name, None))

                    # Generate the from import statement with only used names
                    if used_import_names:
                        if len(used_import_names) == 1 and not used_import_names[0][1]:
                            # Single name, no alias
                            needed_imports.append(f"from {module_name} import {used_import_names[0][0]}")
                        else:
                            # Multiple names or with aliases
                            import_parts = []
                            for name, alias in used_import_names:
                                if alias:
                                    import_parts.append(f"{name} as {alias}")
                                else:
                                    import_parts.append(name)
                            needed_imports.append(f"from {module_name} import {', '.join(import_parts)}")

    return needed_imports


def copy_symbol(
    selector: ExtendedSelector,
    dest_file: str,
    position: str = "end",
    dedent: bool = False,
    include_imports: bool = False,
    apply: bool = False
) -> str:
    """Copy a symbol from one location to another.

    Args:
        selector: Extended selector specifying the source symbol
        dest_file: Path to destination file
        position: Where to insert: "start", "end" (default)
        dedent: If True, dedent the source code to remove common indentation
        include_imports: If True, analyze and include necessary imports from source file
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes to the destination file

    Raises:
        FileNotFoundError: If source file doesn't exist
        ValueError: If symbol not found
    """
    import textwrap

    # Get source code of the symbol
    source = get_symbol_source(selector)

    # Dedent if requested
    if dedent:
        source = textwrap.dedent(source)

    # Analyze and prepend imports if requested
    if include_imports:
        imports = analyze_imports(source, selector.file_path)
        if imports:
            source = "\n".join(imports) + "\n\n" + source

    # Read destination file (create if doesn't exist)
    dest_path = Path(dest_file)
    if dest_path.exists():
        dest_content = dest_path.read_text()
    else:
        dest_content = ""

    # Build new content based on position
    if position == "start":
        if dest_content:
            new_content = source + "\n\n" + dest_content
        else:
            new_content = source
    else:  # "end"
        if dest_content:
            new_content = dest_content.rstrip() + "\n\n\n" + source + "\n"
        else:
            new_content = source

    # Generate diff
    diff = _generate_diff(dest_file, dest_content, new_content)

    # Apply changes if requested
    if apply:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(new_content)

    return diff


def replace_pattern(
    pattern_str: str,
    replacement_str: str,
    file_path: str,
    scope: list[str] | None = None,
    apply: bool = False,
    inside: str | None = None,
    not_inside: str | None = None,
    where: str | None = None,
    type_oracle: TypeOracle | None = None,
) -> tuple[str, int]:
    """Replace pattern matches with replacement template.

    Args:
        pattern_str: Pattern string with metavariables like "print($X)"
        replacement_str: Replacement template like "logger.info($X)"
        file_path: Path to Python file to transform
        scope: Optional symbol path to limit replacements to (e.g., ["MyClass", "method"])
        apply: If True, write changes to file. If False, return diff only.
        inside: Optional constraint - only replace inside this structure.
                Keywords: "def", "async def", "class", "for", "while", "try", "with", "if".
                Patterns: "def test_*", "class MyClass", "try:", "except ValueError:".
        not_inside: Optional constraint - only replace outside this structure.
                    Supports same syntax as inside.
        where: Optional constraint - alias for inside with pattern support.
        type_oracle: Optional TypeOracle instance for :type[X] and :returns[X]
                     constraints.  When present, matching is delegated to
                     ``find_pattern`` so that the oracle post-filter is applied
                     and only type-verified positions are replaced.

    Returns:
        Tuple of (diff, count) where diff is a unified diff and count is number of replacements

    Example:
        >>> diff, count = replace_pattern("print($X)", "logger.info($X)", "file.py")
        >>> print(diff)
        --- file.py
        +++ file.py
        @@ -1,2 +1,2 @@
        -print('hello')
        +logger.info('hello')

        # Scoped replacement within a function:
        >>> diff, count = replace_pattern("old_name", "new_name", "file.py", scope=["my_func"])

        # Replace only inside functions:
        >>> diff, count = replace_pattern("print($X)", "logger.info($X)", "file.py", inside="def")

        # Type-aware replacement:
        >>> diff, count = replace_pattern(
        ...     "$X:type[Connection].close()", "$X.shutdown()",
        ...     "file.py", type_oracle=oracle,
        ... )
    """
    # Handle --where as alias for --inside
    if where is not None:
        if inside is not None:
            raise ValueError("Cannot specify both 'where' and 'inside' parameters")
        inside = where

    # Validate inside/not_inside constraints
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")
    # Parse pattern and compile to matcher
    pattern = parse_pattern(pattern_str)
    matcher, ellipsis_info = compile_pattern_to_matcher(pattern)

    # Detect oracle type constraints
    oracle_constraints: dict[str, tuple[str, str]] = {}
    for mv in pattern.metavars:
        if is_oracle_type_constraint(mv.type_constraint):
            oracle_constraints[mv.name] = parse_oracle_type_constraint(mv.type_constraint)

    # Read and parse file
    file = Path(file_path)
    if not file.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file.read_text()
    module = cst.parse_module(source_code)

    # When oracle constraints are present, delegate matching to find_pattern
    # which handles the type post-filtering, then use a position-filtered
    # replacer to only replace at verified positions.
    if oracle_constraints and type_oracle is not None:
        matches = find_pattern(
            pattern_str, file_path, scope=scope,
            inside=inside, not_inside=not_inside,
            type_oracle=type_oracle,
            source_override=source_code,
        )
        if not matches:
            return "", 0
        allowed_lines = {match.line for match in matches if match.line is not None}
        replacer = _PositionFilteredReplacer(
            matcher, pattern, replacement_str, ellipsis_info,
            allowed_lines=allowed_lines,
        )
        wrapper = cst.MetadataWrapper(module)
        new_module = wrapper.visit(replacer)
    elif inside or not_inside:
        # Use constrained replacer for inside/not_inside constraints
        replacer = ConstrainedPatternReplacer(matcher, pattern, replacement_str, ellipsis_info, inside, not_inside)
        new_module = module.visit(replacer)
    elif scope is not None:
        # Use scoped replacer for scope-based replacements
        replacer = ScopedPatternReplacer(matcher, pattern, replacement_str, scope, ellipsis_info)
        new_module = module.visit(replacer)
    else:
        # Use basic replacer for unconstrained replacements
        replacer = PatternReplacer(matcher, pattern, replacement_str, ellipsis_info)
        new_module = module.visit(replacer)

    # If no modifications, return empty diff and zero count
    if not replacer.modified:
        return "", 0

    new_code = new_module.code

    # Generate diff
    diff = _generate_diff(file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file.write_text(new_code)

    return diff, replacer.replacement_count


# Cross-project semantic primitives

@dataclass
class Reference:
    """A reference to a symbol."""
    file_path: str
    line: int
    column: int
    offset: int
    is_definition: bool
    is_import: bool
    is_write: bool


class _SymbolRenamer(cst.CSTTransformer):
    """Scope-aware rename of symbol occurrences in a single file.

    Uses QualifiedNameProvider to only rename Name nodes whose qualified
    name matches the target symbol. For import aliases (which have empty
    QN in LibCST), tracks the parent ImportFrom module path instead.
    """

    METADATA_DEPENDENCIES = (cst.metadata.QualifiedNameProvider,)

    def __init__(self, old_name: str, new_name: str, target_qns: set[str],
                 target_module: str | None = None):
        self.old_name = old_name
        self.new_name = new_name
        self.target_qns = target_qns
        self.target_module = target_module
        self.changed = False
        self._current_import_module: str | None = None

    def _matches_target(self, node: cst.CSTNode) -> bool:
        try:
            qnames = self.get_metadata(cst.metadata.QualifiedNameProvider, node)
            return any(qn.name in self.target_qns for qn in qnames)
        except KeyError:
            return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        if node.module is not None:
            self._current_import_module = cst.Module([]).code_for_node(node.module).strip()
        return True

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        self._current_import_module = None
        return updated_node

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
        if updated_node.value == self.old_name and self._matches_target(original_node):
            self.changed = True
            return updated_node.with_changes(value=self.new_name)
        return updated_node

    def leave_ImportAlias(
        self, original_node: cst.ImportAlias, updated_node: cst.ImportAlias
    ) -> cst.ImportAlias:
        if (isinstance(updated_node.name, cst.Name)
                and updated_node.name.value == self.old_name
                and self._current_import_module is not None
                and self.target_module is not None
                and self._current_import_module == self.target_module):
            self.changed = True
            return updated_node.with_changes(
                name=updated_node.name.with_changes(value=self.new_name)
            )
        return updated_node


class _ReferenceFinder(cst.CSTVisitor):
    """Scope-aware reference finder using QualifiedNameProvider.

    Only reports references whose qualified name matches the target symbol.
    """

    METADATA_DEPENDENCIES = (
        cst.metadata.PositionProvider,
        cst.metadata.QualifiedNameProvider,
        cst.metadata.ParentNodeProvider,
    )

    def __init__(self, symbol_name: str, file_path: str,
                 target_qns: set[str], target_module: str | None,
                 is_definition_file: bool):
        self.symbol_name = symbol_name
        self.file_path = file_path
        self.target_qns = target_qns
        self.target_module = target_module
        self.is_definition_file = is_definition_file
        self.references: list[Reference] = []
        self._in_import = False
        self._current_import_module: str | None = None

    def _matches_target(self, node: cst.CSTNode) -> bool:
        try:
            qnames = self.get_metadata(cst.metadata.QualifiedNameProvider, node)
            return any(qn.name in self.target_qns for qn in qnames)
        except KeyError:
            return False

    def _is_write_context(self, node: cst.Name) -> bool:
        """Check if a Name node is in a write (store) context."""
        try:
            parent = self.get_metadata(cst.metadata.ParentNodeProvider, node)
        except KeyError:
            return False

        # Direct assignment target: x = ...
        if isinstance(parent, cst.AssignTarget):
            return True

        # Augmented assignment target: x += ...
        if isinstance(parent, cst.AugAssign):
            return parent.target is node

        # Annotated assignment target: x: int = ...
        if isinstance(parent, cst.AnnAssign):
            return parent.target is node

        # For loop target: for x in ...
        if isinstance(parent, cst.For):
            return parent.target is node

        # With...as target: with ... as x
        if isinstance(parent, cst.AsName):
            return True

        # Named expression (walrus): (x := ...)
        if isinstance(parent, cst.NamedExpr):
            return parent.target is node

        return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        self._in_import = True
        if node.module is not None:
            self._current_import_module = cst.Module([]).code_for_node(node.module).strip()
        return True

    def leave_ImportFrom(self, node: cst.ImportFrom) -> None:
        self._in_import = False
        self._current_import_module = None

    def visit_Import(self, node: cst.Import) -> bool:
        self._in_import = True
        return True

    def leave_Import(self, node: cst.Import) -> None:
        self._in_import = False

    def visit_Name(self, node: cst.Name) -> None:
        if node.value != self.symbol_name:
            return
        if not self._matches_target(node):
            return
        pos = self.get_metadata(cst.metadata.PositionProvider, node)
        self.references.append(Reference(
            file_path=self.file_path,
            line=pos.start.line,
            column=pos.start.column,
            offset=0,
            is_definition=False,
            is_import=self._in_import,
            is_write=self._is_write_context(node),
        ))

    def visit_ImportAlias(self, node: cst.ImportAlias) -> None:
        if (isinstance(node.name, cst.Name)
                and node.name.value == self.symbol_name
                and self._current_import_module is not None
                and self.target_module is not None
                and self._current_import_module == self.target_module):
            pos = self.get_metadata(cst.metadata.PositionProvider, node.name)
            self.references.append(Reference(
                file_path=self.file_path,
                line=pos.start.line,
                column=pos.start.column,
                offset=0,
                is_definition=False,
                is_import=True,
                is_write=False,
            ))

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if (self.is_definition_file
                and node.name.value == self.symbol_name
                and self._matches_target(node.name)):
            pos = self.get_metadata(cst.metadata.PositionProvider, node.name)
            self.references.append(Reference(
                file_path=self.file_path,
                line=pos.start.line,
                column=pos.start.column,
                offset=0,
                is_definition=True,
                is_import=False,
                is_write=False,
            ))
        return True

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if (self.is_definition_file
                and node.name.value == self.symbol_name
                and self._matches_target(node.name)):
            pos = self.get_metadata(cst.metadata.PositionProvider, node.name)
            self.references.append(Reference(
                file_path=self.file_path,
                line=pos.start.line,
                column=pos.start.column,
                offset=0,
                is_definition=True,
                is_import=False,
                is_write=False,
            ))
        return True


def _compute_target_qns(symbol_name: str, target_module: str,
                        is_definition_file: bool) -> set[str]:
    """Compute the set of qualified names to match for a target symbol.

    In the definition file, the QN is the bare symbol name (LOCAL).
    In other files, the QN is target_module.symbol_name (IMPORT).
    """
    if is_definition_file:
        return {symbol_name}
    else:
        return {f"{target_module}.{symbol_name}"}


class _DocstringRenamer(cst.CSTTransformer):
    """Replace old_name with new_name inside docstrings only.

    A docstring is the first statement in a function, class, or module body
    when it's a bare string expression (Expr containing a string literal).
    """

    def __init__(self, old_name: str, new_name: str):
        self.old_name = old_name
        self.new_name = new_name
        self.changed = False

    def _replace_in_string(self, node: cst.BaseExpression) -> cst.BaseExpression:
        if isinstance(node, cst.SimpleString):
            new_value = node.value.replace(self.old_name, self.new_name)
            if new_value != node.value:
                self.changed = True
                return node.with_changes(value=new_value)
        elif isinstance(node, cst.ConcatenatedString):
            new_parts = []
            parts_changed = False
            for part in node.parts:
                if isinstance(part, cst.FormattedStringText):
                    new_value = part.value.replace(self.old_name, self.new_name)
                    if new_value != part.value:
                        parts_changed = True
                        new_parts.append(part.with_changes(value=new_value))
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            if parts_changed:
                self.changed = True
                return node.with_changes(parts=new_parts)
        return node

    def _process_body(
        self, body: cst.IndentedBlock
    ) -> cst.IndentedBlock:
        """Check if the first statement is a docstring and replace if so."""
        stmts = list(body.body)
        if not stmts:
            return body

        first = stmts[0]
        if (isinstance(first, cst.SimpleStatementLine)
                and len(first.body) == 1
                and isinstance(first.body[0], cst.Expr)):
            expr = first.body[0].value
            if isinstance(expr, (cst.SimpleString, cst.ConcatenatedString)):
                new_expr = self._replace_in_string(expr)
                if new_expr is not expr:
                    new_stmt = first.body[0].with_changes(value=new_expr)
                    new_first = first.with_changes(body=[new_stmt])
                    stmts[0] = new_first
                    return body.with_changes(body=stmts)
        return body

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if isinstance(updated_node.body, cst.IndentedBlock):
            new_body = self._process_body(updated_node.body)
            if new_body is not updated_node.body:
                return updated_node.with_changes(body=new_body)
        return updated_node

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef:
        if isinstance(updated_node.body, cst.IndentedBlock):
            new_body = self._process_body(updated_node.body)
            if new_body is not updated_node.body:
                return updated_node.with_changes(body=new_body)
        return updated_node

    def leave_Module(
        self, original_node: cst.Module, updated_node: cst.Module
    ) -> cst.Module:
        stmts = list(updated_node.body)
        if not stmts:
            return updated_node

        first = stmts[0]
        if (isinstance(first, cst.SimpleStatementLine)
                and len(first.body) == 1
                and isinstance(first.body[0], cst.Expr)):
            expr = first.body[0].value
            if isinstance(expr, (cst.SimpleString, cst.ConcatenatedString)):
                new_expr = self._replace_in_string(expr)
                if new_expr is not expr:
                    new_stmt = first.body[0].with_changes(value=new_expr)
                    new_first = first.with_changes(body=[new_stmt])
                    stmts[0] = new_first
                    return updated_node.with_changes(body=stmts)
        return updated_node


def _rename_in_docstrings(content: str, old_name: str, new_name: str) -> str | None:
    """Replace old_name with new_name in all docstrings.

    Returns new content if changes were made, None otherwise.
    """
    try:
        module = cst.parse_module(content)
        renamer = _DocstringRenamer(old_name, new_name)
        new_module = module.visit(renamer)
        if renamer.changed:
            return new_module.code
        return None
    except Exception:
        return None


def find_references(
    selector: ExtendedSelector,
    project_path: str | None = None,
    include_definition: bool = True,
    include_imports: bool = True,
    writes_only: bool = False,
    reads_only: bool = False,
) -> Iterator[Reference]:
    """Find all references to a symbol across the project.

    Uses LibCST QualifiedNameProvider for scope-aware resolution:
    only returns references that actually refer to the target symbol,
    not coincidental same-named symbols in other scopes or files.

    Args:
        selector: Symbol to find references for
        project_path: Project root (auto-detected if None)
        include_definition: Include the definition itself
        include_imports: Include import statements
        writes_only: Only return write (assignment) references
        reads_only: Only return read (load) references

    Returns:
        List of Reference objects with location info

    Raises:
        ValueError: If symbol not found
    """
    if writes_only and reads_only:
        raise ValueError("Cannot specify both writes_only and reads_only")

    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_references")

    # scan_root: where to collect files (respects --project for scope limiting)
    # module_root: project root for computing dotted module names (always git root)
    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    resolved_target = str(Path(selector.file_path).resolve())
    target_module = _file_to_module(selector.file_path, module_root)

    def factory(py_file: str, is_def_file: bool):
        target_qns = _compute_target_qns(symbol_name, target_module, is_def_file)
        return _ReferenceFinder(
            symbol_name, py_file, target_qns, target_module, is_def_file
        )

    # Use import graph to pre-filter: only check files that import
    # from the module defining the target symbol.
    candidates = _files_importing_module(scan_root, target_module)

    # Compute the set of qualified names we're looking for (for QN-index filtering)
    all_target_qns = {symbol_name, f"{target_module}.{symbol_name}"}

    # Use an inner generator so validation above runs eagerly on call, not on first iteration.
    def _gen() -> Iterator[Reference]:
        for _py_file, _module, finder in visit_project(
            name_hint=symbol_name,
            visitor_factory=factory,
            project_path=scan_root,
            metadata_providers=_ReferenceFinder.METADATA_DEPENDENCIES,
            target_file=resolved_target,
            candidate_files=candidates,
            target_qnames=all_target_qns,
        ):
            for ref in finder.references:
                if not include_definition and ref.is_definition:
                    continue
                if not include_imports and ref.is_import:
                    continue
                if writes_only and not ref.is_write:
                    continue
                if reads_only and ref.is_write:
                    continue
                yield ref

    return _gen()


@dataclass
class Callee:
    """A function/method called by a function."""
    name: str
    qualified_name: str | None
    file_path: str | None
    line: int | None


class _CallerFilter(cst.CSTVisitor):
    """Visitor that checks if a Name node appears as the function in a Call.

    Used to filter find_references results to only call sites.
    """

    METADATA_DEPENDENCIES = (
        cst.metadata.PositionProvider,
        cst.metadata.QualifiedNameProvider,
    )

    def __init__(self, symbol_name: str, file_path: str,
                 target_qns: set[str], target_module: str | None,
                 is_definition_file: bool):
        self.symbol_name = symbol_name
        self.file_path = file_path
        self.target_qns = target_qns
        self.target_module = target_module
        self.is_definition_file = is_definition_file
        self.call_references: list[Reference] = []
        self._in_import = False
        self._current_import_module: str | None = None

    def _matches_target(self, node: cst.CSTNode) -> bool:
        try:
            qnames = self.get_metadata(cst.metadata.QualifiedNameProvider, node)
            return any(qn.name in self.target_qns for qn in qnames)
        except KeyError:
            return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        self._in_import = True
        if node.module is not None:
            self._current_import_module = cst.Module([]).code_for_node(node.module).strip()
        return True

    def leave_ImportFrom(self, node: cst.ImportFrom) -> None:
        self._in_import = False
        self._current_import_module = None

    def visit_Call(self, node: cst.Call) -> bool:
        func = node.func
        # Direct call: process(...)
        if isinstance(func, cst.Name) and func.value == self.symbol_name:
            if self._matches_target(func):
                pos = self.get_metadata(cst.metadata.PositionProvider, func)
                self.call_references.append(Reference(
                    file_path=self.file_path,
                    line=pos.start.line,
                    column=pos.start.column,
                    offset=0,
                    is_definition=False,
                    is_import=False,
                    is_write=False,
                ))
        # Attribute call: module.process(...)
        elif isinstance(func, cst.Attribute) and isinstance(func.attr, cst.Name):
            if func.attr.value == self.symbol_name:
                if self._matches_target(func):
                    pos = self.get_metadata(cst.metadata.PositionProvider, func)
                    self.call_references.append(Reference(
                        file_path=self.file_path,
                        line=pos.start.line,
                        column=pos.start.column,
                        offset=0,
                        is_definition=False,
                        is_import=False,
                        is_write=False,
                    ))
        return True


def find_callers(
    selector: ExtendedSelector,
    project_path: str | None = None,
) -> Iterator[Reference]:
    """Find all places where a function is called across the project.

    Unlike find_references, this only returns actual call sites,
    not imports or other references.

    Args:
        selector: Symbol to find callers for
        project_path: Project root (auto-detected if None)

    Returns:
        List of Reference objects at call sites
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callers")

    # scan_root: where to collect files (respects --project for scope limiting)
    # module_root: project root for computing dotted module names (always git root)
    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    resolved_target = str(Path(selector.file_path).resolve())
    target_module = _file_to_module(selector.file_path, module_root)

    def factory(py_file: str, is_def_file: bool):
        target_qns = _compute_target_qns(symbol_name, target_module, is_def_file)
        return _CallerFilter(
            symbol_name, py_file, target_qns, target_module, is_def_file
        )

    # Use import graph to pre-filter files
    candidates = _files_importing_module(scan_root, target_module)

    all_target_qns = {symbol_name, f"{target_module}.{symbol_name}"}

    def _gen() -> Iterator[Reference]:
        for _py_file, _module, visitor in visit_project(
            name_hint=symbol_name,
            visitor_factory=factory,
            project_path=scan_root,
            metadata_providers=_CallerFilter.METADATA_DEPENDENCIES,
            target_file=resolved_target,
            candidate_files=candidates,
            target_qnames=all_target_qns,
        ):
            yield from visitor.call_references

    return _gen()


class _CalleeCollector(cst.CSTVisitor):
    """Visitor that collects all Call nodes inside a function body."""

    METADATA_DEPENDENCIES = (
        cst.metadata.PositionProvider,
        cst.metadata.QualifiedNameProvider,
    )

    def __init__(self):
        self.callees: list[Callee] = []
        self._seen: set[str] = set()

    def visit_Call(self, node: cst.Call) -> bool:
        func = node.func
        name = None
        qn_str = None

        if isinstance(func, cst.Name):
            name = func.value
            try:
                qnames = self.get_metadata(cst.metadata.QualifiedNameProvider, func)
                if qnames:
                    qn_str = next(iter(qnames)).name
            except KeyError:
                pass
        elif isinstance(func, cst.Attribute) and isinstance(func.attr, cst.Name):
            name = func.attr.value
            try:
                qnames = self.get_metadata(cst.metadata.QualifiedNameProvider, func)
                if qnames:
                    qn_str = next(iter(qnames)).name
            except KeyError:
                pass

        if name and name not in self._seen:
            self._seen.add(name)
            line = None
            try:
                pos = self.get_metadata(cst.metadata.PositionProvider, node)
                line = pos.start.line
            except KeyError:
                pass
            self.callees.append(Callee(
                name=name,
                qualified_name=qn_str,
                file_path=None,
                line=line,
            ))

        return True


def find_callees(
    selector: ExtendedSelector,
    project_path: str | None = None,
) -> list[Callee]:
    """Find all functions/methods called inside a function.

    Args:
        selector: Function to analyze
        project_path: Project root (auto-detected if None)

    Returns:
        List of Callee objects
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callees")

    file_path = selector.file_path
    try:
        content = Path(file_path).read_text()
    except FileNotFoundError:
        raise ValueError(f"File not found: {file_path}")

    # Find the function body
    module = cst.parse_module(content)
    finder = SymbolFinder(selector.symbol_path)
    module.visit(finder)
    if finder.found_node is None:
        raise ValueError(f"Symbol not found: {'.'.join(selector.symbol_path)}")

    # Extract just the function body and analyze calls
    func_node = finder.found_node
    # Build a mini-module wrapping the function so MetadataWrapper works
    wrapper_module = cst.Module(body=[func_node])
    try:
        # Re-parse the function code so MetadataWrapper can process it
        func_code = wrapper_module.code
        reparsed = cst.parse_module(func_code)
        mw = cst.metadata.MetadataWrapper(reparsed)
        collector = _CalleeCollector()
        mw.visit(collector)
        return collector.callees
    except Exception:
        return []


def generate_graph(
    file_path: str,
    project_path: str | None = None,
    format: str = "plain",
) -> str:
    """Generate a call graph for all functions in a file.

    Args:
        file_path: Python file to analyze
        project_path: Project root (auto-detected if None)
        format: Output format - "plain", "json", or "dot"

    Returns:
        Graph in the requested format
    """
    from .component_selector import ExtendedSelector

    content = Path(file_path).read_text()

    raw = _rust.collect_callees(content)
    edges: dict[str, list[str]] = {name: callees for name, callees in raw}

    if format == "json":
        return json.dumps(edges, indent=2)
    elif format == "dot":
        lines = ["digraph callgraph {"]
        for caller, callees_list in edges.items():
            for callee in callees_list:
                lines.append(f'  "{caller}" -> "{callee}";')
        lines.append("}")
        return "\n".join(lines)
    else:
        # plain text
        lines = []
        for caller, callees_list in edges.items():
            if callees_list:
                lines.append(f"{caller} -> {', '.join(callees_list)}")
            else:
                lines.append(f"{caller} (no calls)")
        return "\n".join(lines)


@dataclass
class DeadSymbol:
    """A symbol detected as potentially dead (unreferenced) code."""
    file_path: str
    name: str
    kind: str  # 'function', 'class', 'async_function'
    line: int
    selector: str  # e.g. "file.py::func_name"
    reason: str  # Why it's flagged (e.g. "no references found")
    last_reference_commit: str | None = None  # git commit that last touched this symbol


# Decorator prefixes that indicate a symbol is an entry point / framework hook
_ENTRY_POINT_DECORATORS = frozenset({
    'app.command', 'app.route', 'app.get', 'app.post', 'app.put',
    'app.delete', 'app.patch',
    'pytest.fixture', 'fixture',
    'staticmethod', 'classmethod', 'property',
    'abstractmethod', 'abc.abstractmethod',
    'override',
    'overload', 'typing.overload',
    'click.command', 'click.group',
    'celery.task',
    'register',
})

# Decorator base names that indicate entry points
_ENTRY_POINT_DECORATOR_BASENAMES = frozenset({
    'route', 'get', 'post', 'put', 'delete', 'patch', 'head', 'options',
    'command', 'task', 'hook', 'listener',
    'receiver', 'signal', 'handler', 'middleware',
    'register', 'export',
})

# Names that are conventional entry points and should never be flagged
_ENTRY_POINT_NAMES = frozenset({
    'main', 'setup', 'teardown', 'configure',
    'setUp', 'tearDown', 'setUpClass', 'tearDownClass',
    'setUpModule', 'tearDownModule',
})


def _is_dunder(name: str) -> bool:
    """Check if a name is a dunder (double underscore) name."""
    return name.startswith('__') and name.endswith('__') and len(name) > 4


def _is_likely_entry_point(name: str, kind: str, decorators: list[str], depth: int) -> bool:
    """Check if a symbol is likely an entry point based on heuristics.

    Entry points are symbols that are invoked by frameworks or conventions
    rather than explicit code references.
    """
    # Dunder methods/functions are always entry points
    if _is_dunder(name):
        return True

    # Conventional entry-point names
    if name in _ENTRY_POINT_NAMES:
        return True

    # Names starting with test_ (pytest discovery)
    if name.startswith('test_') or name.startswith('Test'):
        return True

    # Private names (single underscore prefix) at depth > 1 are methods,
    # which may be called via getattr or framework internals
    # We only flag private top-level symbols

    # Check decorators
    for dec in decorators:
        # Strip @ prefix if present
        dec_name = dec[1:] if dec.startswith('@') else dec
        # Strip arguments: @app.command("name") -> app.command
        if '(' in dec_name:
            dec_name = dec_name[:dec_name.index('(')]

        if dec_name in _ENTRY_POINT_DECORATORS:
            return True

        # Check basename: @anything.route -> "route" is entry point
        basename = dec_name.rsplit('.', 1)[-1] if '.' in dec_name else dec_name
        if basename in _ENTRY_POINT_DECORATOR_BASENAMES:
            return True

    return False


def _get_all_exported_names(content: str) -> set[str]:
    """Extract names listed in __all__ from file content."""
    try:
        module = _cached_parse(content)
    except Exception:
        return set()

    names: set[str] = set()
    for stmt in module.body:
        # Match: __all__ = [...]  or  __all__ = (...)
        if isinstance(stmt, cst.SimpleStatementLine):
            for item in stmt.body:
                if (isinstance(item, cst.Assign)
                        and len(item.targets) == 1
                        and isinstance(item.targets[0].target, cst.Name)
                        and item.targets[0].target.value == '__all__'):
                    value = item.value
                    elements = None
                    if isinstance(value, (cst.List, cst.Tuple)):
                        elements = value.elements
                    if elements:
                        for el in elements:
                            if isinstance(el, cst.Element) and isinstance(el.value, (cst.SimpleString, cst.ConcatenatedString)):
                                # Extract the string value
                                try:
                                    raw = el.value.evaluated_value
                                    if isinstance(raw, str):
                                        names.add(raw)
                                except Exception:
                                    pass
    return names


class _BulkReferenceFinder(cst.CSTVisitor):
    """Single-pass visitor that discovers which symbols from a candidate set
    are referenced in a file.

    For each Name/Attribute node, resolves its qualified name and checks
    whether it matches any of the candidate symbols.  Optionally also
    treats string literals containing the symbol name as references.

    Records which candidates were seen, keyed by (defining_file, symbol_name).
    """

    METADATA_DEPENDENCIES = (
        cst.metadata.QualifiedNameProvider,
        cst.metadata.PositionProvider,
    )

    def __init__(
        self,
        file_path: str,
        is_definition_file: bool,
        # candidate_qns: maps qualified-name -> (defining_file, symbol_name)
        candidate_qns: dict[str, tuple[str, str]],
        # For each (file, name) that is a definition in *this* file,
        # record the definition line so we can exclude self-references.
        local_def_lines: dict[str, int],
        # candidate_names_by_key: maps symbol_name -> set of candidate keys
        # Used for string-based reference detection.
        candidate_names_by_key: dict[str, set[tuple[str, str]]] | None = None,
        # String patterns that count as references: maps pattern -> set of keys
        string_patterns: dict[str, set[tuple[str, str]]] | None = None,
    ):
        self.file_path = file_path
        self.is_definition_file = is_definition_file
        self.candidate_qns = candidate_qns
        self.local_def_lines = local_def_lines
        self.candidate_names_by_key = candidate_names_by_key
        self.string_patterns = string_patterns
        # Set of (defining_file, symbol_name) that are referenced
        self.referenced: set[tuple[str, str]] = set()

    def _check_node(self, node: cst.CSTNode) -> None:
        """Resolve the QN of *node* and mark matching candidates as referenced."""
        try:
            qnames = self.get_metadata(cst.metadata.QualifiedNameProvider, node)
        except KeyError:
            return
        for qn in qnames:
            key = self.candidate_qns.get(qn.name)
            if key is None:
                continue
            # If this name appears at the definition line in the
            # defining file, skip it (it's the definition itself).
            def_file, sym_name = key
            if (str(Path(self.file_path).resolve()) == def_file
                    and sym_name in self.local_def_lines):
                try:
                    pos = self.get_metadata(cst.metadata.PositionProvider, node)
                    if pos.start.line == self.local_def_lines[sym_name]:
                        continue
                except KeyError:
                    pass
            self.referenced.add(key)

    def visit_Name(self, node: cst.Name) -> None:
        self._check_node(node)

    def visit_Attribute(self, node: cst.Attribute) -> None:
        # Catches module.symbol references like ast_commands.cmd_copy_to
        self._check_node(node)

    def _check_string(self, value: str) -> None:
        """Check if a string literal contains any candidate symbol name."""
        if not self.string_patterns:
            return
        for pattern, keys in self.string_patterns.items():
            if pattern in value:
                self.referenced.update(keys)

    def visit_SimpleString(self, node: cst.SimpleString) -> None:
        if self.string_patterns:
            try:
                val = node.evaluated_value
                if isinstance(val, str):
                    self._check_string(val)
            except Exception:
                pass

    def visit_FormattedStringText(self, node: cst.FormattedStringText) -> None:
        if self.string_patterns:
            self._check_string(node.value)


def _has_noqa_deadcode(source: str, line: int) -> bool:
    """Check whether *line* has a ``# noqa: emend:deadcode`` comment."""
    from .lint import parse_noqa_comments
    noqa = parse_noqa_comments(source)
    if line not in noqa:
        return False
    entry = noqa[line]
    # None means bare noqa (suppresses everything)
    if entry is None:
        return True
    return 'deadcode' in entry


def _get_last_reference_commit(file_path: str, symbol_name: str) -> str | None:
    """Use ``git log -S`` to find the last commit that added/removed *symbol_name*.

    Returns a one-line summary like ``abc1234 2024-01-15 Fix: remove usage``
    or None if git is unavailable or nothing found.
    """
    import subprocess
    cwd = str(Path(file_path).resolve().parent)
    try:
        result = subprocess.run(
            ['git', 'log', '-S', symbol_name, '--format=%h %ai %s',
             '-1', '--', file_path],
            capture_output=True, text=True, timeout=10,
            cwd=cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def find_dead_code(
    project_path: str,
    kind: str | None = None,
    include_private: bool = False,
    exclude_references_from: list[str] | None = None,
    strings_count_as_references: bool = True,
    show_last_reference: bool = True,
    all_files: bool = False,
) -> Iterator[DeadSymbol]:
    """Find potentially dead (unreferenced) code in a project.

    Performs two passes over project files:
      1. Collect all top-level symbol definitions and their qualified names.
      2. Visit every file once to discover which candidate symbols are
         actually referenced anywhere outside their definition site.

    This is O(files) rather than O(symbols * files).

    Args:
        project_path: Project root directory.
        kind: Optional filter: 'function', 'class', or None for all.
        include_private: If True, include _private symbols (excluded by default).
        exclude_references_from: Directories/globs to exclude when scanning for
            references (e.g. ``["tests/"]``).  Symbols are still collected from
            these paths but references *in* them are ignored.
        strings_count_as_references: If True (default), string literals that
            contain the symbol name are treated as references.  This reduces
            false positives from dynamic dispatch, serialization, and similar.
        show_last_reference: If True (default), annotate each dead symbol with
            the last ``git log -S`` commit that touched its name.
        all_files: If True, scan all Python files (including untracked).
            By default only git-tracked files are scanned when inside a
            git repository.

    Yields:
        DeadSymbol objects sorted by file path and line number.
    """
    from .query import _collect_symbols, SymbolInfo

    # scan_root: where to collect files (respects the user-supplied path)
    # module_root: project root for computing dotted module names (always
    #              the real project root so that QNs match across files)
    scan_root = str(Path(project_path).resolve())
    module_root = _find_project_root(project_path)
    py_files = _collect_python_files(scan_root, git_tracked_only=not all_files)

    # Build resolved exclude set for reference scanning
    exclude_resolved: set[str] = set()
    if exclude_references_from:
        for pattern in exclude_references_from:
            p = Path(pattern).resolve()
            exclude_resolved.add(str(p))

    def _is_excluded_ref_file(py_file: str) -> bool:
        """Check if a file should be excluded from reference scanning."""
        if not exclude_resolved:
            return False
        resolved = str(Path(py_file).resolve())
        return any(resolved.startswith(ex) for ex in exclude_resolved)

    # Phase 1: Collect candidate symbols and build QN lookup table
    # candidate key = (resolved_file_path, symbol_name)
    candidates: dict[tuple[str, str], tuple[str, SymbolInfo]] = {}
    # Maps qualified-name-string -> candidate key
    candidate_qns: dict[str, tuple[str, str]] = {}
    # Per-file definition lines for self-reference filtering
    file_def_lines: dict[str, dict[str, int]] = {}  # file -> {name -> line}
    # __all__ exports per file
    all_exports: dict[str, set[str]] = {}
    # file contents cache (needed for noqa checking)
    file_contents_cache: dict[str, str] = {}

    for py_file in py_files:
        try:
            content = Path(py_file).read_text()
        except Exception:
            continue

        resolved_file = str(Path(py_file).resolve())
        file_contents_cache[resolved_file] = content
        symbols = _collect_symbols(Path(py_file), content)
        exports = _get_all_exported_names(content)
        if exports:
            all_exports[resolved_file] = exports

        file_module = _file_to_module(py_file, module_root)
        local_defs: dict[str, int] = {}

        for sym in symbols:
            if sym.depth != 1:
                continue
            if kind:
                if kind == 'function' and sym.kind not in ('function', 'async_function'):
                    continue
                if kind == 'class' and sym.kind != 'class':
                    continue
            if not include_private and sym.name.startswith('_') and not _is_dunder(sym.name):
                continue
            if _is_likely_entry_point(sym.name, sym.kind, sym.decorators, sym.depth):
                continue
            if sym.name in exports:
                continue
            # Check for # noqa: emend:deadcode on the definition line
            if _has_noqa_deadcode(content, sym.line):
                continue

            cand_key = (resolved_file, sym.name)
            candidates[cand_key] = (py_file, sym)
            local_defs[sym.name] = sym.line

            # In the definition file, QN is just the bare name (LOCAL).
            candidate_qns[sym.name] = cand_key
            # In other files, QN is module.name (IMPORT).
            candidate_qns[f"{file_module}.{sym.name}"] = cand_key

        if local_defs:
            file_def_lines[resolved_file] = local_defs

    if not candidates:
        return []

    # Build string-pattern lookup for string-as-reference detection.
    # For each candidate we generate patterns that are likely to appear
    # in dynamic references: the bare name, the qualified name, and
    # the selector-style "::name".
    candidate_names_by_key: dict[str, set[tuple[str, str]]] = {}
    string_patterns: dict[str, set[tuple[str, str]]] | None = None

    if strings_count_as_references:
        string_patterns = {}
        for cand_key, (file_path, sym) in candidates.items():
            name = sym.name
            # Bare name (e.g. "my_func")
            string_patterns.setdefault(name, set()).add(cand_key)
        # Filter out very short names (<=3 chars) to avoid false matches
        # from strings like "x" or "id" appearing everywhere.
        string_patterns = {
            k: v for k, v in string_patterns.items() if len(k) > 3
        }

    # Phase 2: Single pass over all files to find references
    referenced: set[tuple[str, str]] = set()

    def factory(py_file: str, is_def_file: bool):
        resolved = str(Path(py_file).resolve())
        local_defs = file_def_lines.get(resolved, {})
        return _BulkReferenceFinder(
            py_file, is_def_file, candidate_qns, local_defs,
            candidate_names_by_key=candidate_names_by_key or None,
            string_patterns=string_patterns if not _is_excluded_ref_file(py_file) else None,
        )

    # Pre-filter files for Phase 2: skip files that cannot possibly
    # reference any candidate symbol (the symbol name doesn't even appear
    # as a substring).  This avoids the expensive MetadataWrapper +
    # QualifiedNameProvider scope analysis on irrelevant files — typically
    # eliminating 70-90% of files.
    candidate_name_set = {sym.name for (_, sym) in candidates.values()}
    ref_scan_files: set[str] = set()
    for py_file in py_files:
        if exclude_resolved and _is_excluded_ref_file(py_file):
            continue
        resolved = str(Path(py_file).resolve())
        content = file_contents_cache.get(resolved)
        if content is None:
            # File not in cache (e.g. untracked) — include conservatively
            ref_scan_files.add(py_file)
            continue
        for name in candidate_name_set:
            if name in content:
                ref_scan_files.add(py_file)
                break

    for _py_file, _module, visitor in visit_project(
        name_hint="",
        visitor_factory=factory,
        project_path=scan_root,
        metadata_providers=_BulkReferenceFinder.METADATA_DEPENDENCIES,
        target_file=None,
        candidate_files=ref_scan_files,
        target_qnames=set(candidate_qns.keys()),
    ):
        referenced.update(visitor.referenced)

    # Phase 3: Yield results — candidates not in referenced set
    dead_symbols: list[DeadSymbol] = []
    for cand_key, (file_path, sym) in candidates.items():
        if cand_key not in referenced:
            dead_symbols.append(DeadSymbol(
                file_path=file_path,
                name=sym.name,
                kind=sym.kind,
                line=sym.line,
                selector=sym.path,
                reason="no references found",
            ))

    dead_symbols.sort(key=lambda d: (d.file_path, d.line))

    if show_last_reference and dead_symbols:
        from concurrent.futures import ThreadPoolExecutor

        def _git_lookup(d: DeadSymbol) -> tuple[DeadSymbol, str | None]:
            return d, _get_last_reference_commit(d.file_path, d.name)

        with ThreadPoolExecutor() as pool:
            for d, commit in pool.map(_git_lookup, dead_symbols):
                d.last_reference_commit = commit
                yield d
    else:
        yield from dead_symbols


def _rename_symbol_in_file(
    content: str, old_name: str, new_name: str,
    target_qns: set[str] | None = None,
    target_module: str | None = None,
) -> str | None:
    """Rename occurrences of old_name to new_name in a single file.

    When target_qns is provided, uses scope-aware renaming via
    QualifiedNameProvider. Falls back to name-based renaming otherwise.

    Returns the new content if changes were made, None otherwise.
    """
    try:
        module = cst.parse_module(content)
        if target_qns is not None:
            wrapper = cst.metadata.MetadataWrapper(module)
            renamer = _SymbolRenamer(old_name, new_name, target_qns, target_module)
            new_module = wrapper.visit(renamer)
        else:
            # Fallback: name-based renaming (no scope analysis)
            renamer = _SymbolRenamer(old_name, new_name, {old_name})
            new_module = module.visit(renamer)
        if renamer.changed:
            return new_module.code
        return None
    except Exception:
        return None


def rename_symbol(
    selector: ExtendedSelector,
    new_name: str,
    project_path: str | None = None,
    in_hierarchy: bool = True,
    docs: bool = False,
    unsure: bool = False,
    apply: bool = False,
) -> dict[str, str]:
    """Rename a symbol across the entire project.

    Uses LibCST QualifiedNameProvider for scope-aware renaming:
    only renames references that actually refer to the target symbol,
    not coincidental same-named symbols in other scopes or files.

    Args:
        selector: Symbol to rename
        new_name: New name for the symbol
        project_path: Project root (auto-detected if None)
        in_hierarchy: Also rename in class hierarchies
        docs: Also rename in docstrings
        unsure: Rename uncertain occurrences
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes

    Raises:
        ValueError: If symbol not found
    """
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for rename_symbol")

    # scan_root: where to collect files (respects --project for scope limiting)
    # module_root: project root for computing dotted module names (always git root)
    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    resolved_target = str(Path(selector.file_path).resolve())
    target_module = _file_to_module(selector.file_path, module_root)

    def factory(py_file: str, is_def_file: bool):
        target_qns = _compute_target_qns(symbol_name, target_module, is_def_file)
        return _SymbolRenamer(symbol_name, new_name, target_qns, target_module)

    # Use import graph to pre-filter files
    candidates = _files_importing_module(scan_root, target_module)

    all_target_qns = {symbol_name, f"{target_module}.{symbol_name}"}

    diffs = {}
    for py_file, result_module, renamer in visit_project(
        name_hint=symbol_name,
        visitor_factory=factory,
        project_path=scan_root,
        metadata_providers=_SymbolRenamer.METADATA_DEPENDENCIES,
        target_file=resolved_target,
        candidate_files=candidates,
        target_qnames=all_target_qns,
    ):
        if not renamer.changed:
            continue

        content = Path(py_file).read_text()
        new_content = result_module.code

        # Apply docstring renaming if requested -- but only in files where
        # the scope-aware code rename found changes (proving the file refers
        # to the target symbol, not a coincidental same-named symbol).
        if docs:
            docs_result = _rename_in_docstrings(new_content, symbol_name, new_name)
            if docs_result is not None:
                new_content = docs_result

        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    return diffs


def _rewrite_imports(
    content: str,
    source_module: str,
    dest_module: str,
    symbol_name: str
) -> str:
    """Rewrite imports in content to reflect symbol move."""
    import libcst as cst
    from libcst import matchers as m

    class ImportRewriter(cst.CSTTransformer):
        """Transformer to rewrite import statements."""

        def __init__(self, source_mod: str, dest_mod: str, sym_name: str):
            self.source_mod = source_mod
            self.dest_mod = dest_mod
            self.sym_name = sym_name

        def leave_ImportFrom(
            self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
        ) -> cst.ImportFrom:
            """Rewrite from X import Y statements."""
            # Check if this imports from the source module
            if updated_node.module is None:
                return updated_node

            module_name = cst.Module([]).code_for_node(updated_node.module).strip()

            if module_name == self.source_mod:
                # Check if it imports our symbol
                if isinstance(updated_node.names, cst.ImportStar):
                    return updated_node

                for name in updated_node.names:
                    if isinstance(name, cst.ImportAlias):
                        imported_name = name.name.value if isinstance(name.name, cst.Name) else str(name.name)
                        if imported_name == self.sym_name:
                            # Rewrite the module name
                            new_module = cst.parse_expression(self.dest_mod)
                            if isinstance(new_module, (cst.Name, cst.Attribute)):
                                return updated_node.with_changes(module=new_module)

            return updated_node

    try:
        module = cst.parse_module(content)
        rewriter = ImportRewriter(source_module, dest_module, symbol_name)
        new_module = module.visit(rewriter)
        return new_module.code
    except Exception:
        # If parsing fails, return original content
        return content


def move_symbol(
    selector: ExtendedSelector,
    dest_file: str,
    position: str = "end",
    dedent: bool = False,
    update_imports: bool = True,
    project_path: str | None = None,
    apply: bool = False,
) -> dict[str, str]:
    """Move a symbol to another file with import updates.

    1. Copies the symbol to the destination file
    2. Removes the symbol from the source file
    3. Updates all import statements that reference the symbol

    Args:
        selector: Symbol to move
        dest_file: Destination file path
        position: Where to insert ("start" or "end")
        dedent: If True, dedent the source code to remove common indentation
        update_imports: If True, update imports across project
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes

    Raises:
        ValueError: If symbol not found
    """
    diffs = {}
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for move_symbol")

    # Step 1: Copy symbol to destination
    copy_diff = copy_symbol(selector, dest_file, position=position, dedent=dedent, apply=apply)
    diffs[dest_file] = copy_diff

    # Step 2: Remove from source
    remove_diff = remove_symbol(selector, apply=apply)
    diffs[selector.file_path] = remove_diff

    # Step 3: Update imports if requested
    if update_imports:
        import_diffs = _update_imports_for_move(
            selector.file_path,
            dest_file,
            symbol_name,
            project_path,
            apply=apply,
        )
        diffs.update(import_diffs)

    return diffs


def _update_imports_for_move(
    source_file: str,
    dest_file: str,
    symbol_name: str,
    project_path: str | None,
    apply: bool,
) -> dict[str, str]:
    """Update imports across project when a symbol moves."""
    diffs = {}

    # Get module names
    source_module = _file_to_module(source_file, project_path)
    dest_module = _file_to_module(dest_file, project_path)

    # Resolve skip paths
    resolved_source = str(Path(source_file).resolve())
    resolved_dest = str(Path(dest_file).resolve())
    proj_root = _find_project_root(project_path or source_file)

    # Use _rewrite_imports which handles the ImportRewriter transformer
    import_pattern = f"from {source_module} import"

    def factory(py_file: str, is_def_file: bool):
        # We use _ModuleImportRenamer-like approach but specific to symbol moves.
        # Return a no-op transformer for files we want to skip.
        resolved_py = str(Path(py_file).resolve())
        if resolved_py == resolved_source or resolved_py == resolved_dest:
            return _NoOpTransformer()
        return _ImportRewriterForMove(source_module, dest_module, symbol_name)

    for py_file, result_module, rewriter in visit_project(
        name_hint=import_pattern,
        visitor_factory=factory,
        project_path=proj_root,
    ):
        if isinstance(rewriter, _NoOpTransformer) or not rewriter.changed:
            continue

        content = Path(py_file).read_text()
        new_content = result_module.code

        if new_content != content:
            diff = _generate_diff(py_file, content, new_content)
            diffs[py_file] = diff

            if apply:
                Path(py_file).write_text(new_content)

    return diffs


def _transform_line_skipping_strings(line: str, pattern, replacement: str) -> str:
    """Apply regex transformation to a line, but skip matches inside string literals and comments.

    This function tokenizes the line to identify string literals and comments,
    then only applies the pattern to code portions.
    """
    import tokenize
    import io
    import re

    # Handle empty or whitespace-only lines
    if not line.strip():
        return line

    try:
        # Tokenize the line
        tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))

        # Build a list of (start, end, is_string_or_comment) ranges
        protected_ranges = []
        for token in tokens:
            token_type, _, (_, start_col), (_, end_col), _ = token
            if token_type in (tokenize.STRING, tokenize.COMMENT):
                protected_ranges.append((start_col, end_col))

        # If no protected ranges, just apply the pattern to the whole line
        if not protected_ranges:
            return pattern.sub(replacement, line)

        # Apply transformation to non-protected portions
        result = []
        last_pos = 0

        for start, end in protected_ranges:
            # Transform the code portion before this protected range
            if start > last_pos:
                code_portion = line[last_pos:start]
                result.append(pattern.sub(replacement, code_portion))

            # Keep the protected portion as-is
            result.append(line[start:end])
            last_pos = end

        # Transform any remaining code after the last protected range
        if last_pos < len(line):
            code_portion = line[last_pos:]
            result.append(pattern.sub(replacement, code_portion))

        return ''.join(result)

    except tokenize.TokenError:
        # If tokenization fails (e.g., incomplete line), fall back to simple regex
        return pattern.sub(replacement, line)


def transform_references(
    selector: ExtendedSelector,
    from_pattern: str,
    to_pattern: str,
    apply: bool = False,
) -> tuple[str, int]:
    """Scoped regex replacement that skips strings and comments.

    Args:
        selector: Symbol to transform within
        from_pattern: Regex pattern to search for
        to_pattern: Replacement pattern (can include backreferences like \1)
        apply: If True, write changes. If False, return diff only.

    Returns:
        Tuple of (unified_diff, change_count)

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found
    """
    import re
    from emend.ast_utils import find_nested_definitions, find_symbol_by_path

    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    # Find the symbol using ast_utils to get line numbers
    symbols = find_nested_definitions(selector.file_path)
    symbol = find_symbol_by_path(symbols, selector.symbol_path)

    if symbol is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    # Read file lines
    file_lines = file_path.read_text().splitlines(keepends=True)

    # Compile pattern
    pattern = re.compile(from_pattern)
    changes = 0
    new_lines = file_lines[:]

    # Transform each line in the symbol's range
    for i in range(symbol.line_start - 1, min(symbol.line_end, len(file_lines))):
        line = file_lines[i]
        new_line = _transform_line_skipping_strings(line, pattern, to_pattern)
        if new_line != line:
            changes += 1
            new_lines[i] = new_line

    # Generate diff
    if changes == 0:
        return ("", 0)

    new_content = ''.join(new_lines)
    original_content = ''.join(file_lines)
    diff = _generate_diff(selector.file_path, original_content, new_content)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_content)

    return (diff, changes)


def _rename_module_references(
    project_root: str,
    old_module: str,
    new_module: str,
    apply: bool,
) -> dict[str, str]:
    """Update all imports from old_module to new_module across the project."""
    diffs = {}

    def factory(py_file: str, is_def_file: bool):
        return _ModuleImportRenamer(old_module, new_module)

    for py_file, result_module, renamer in visit_project(
        name_hint=old_module,
        visitor_factory=factory,
        project_path=project_root,
    ):
        if not renamer.changed:
            continue

        content = Path(py_file).read_text()
        new_content = result_module.code
        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    return diffs


class _NoOpTransformer(cst.CSTTransformer):
    """A no-op transformer that makes no changes."""
    changed = False


class _ImportRewriterForMove(cst.CSTTransformer):
    """Rewrite 'from source_module import symbol_name' to 'from dest_module import symbol_name'."""

    def __init__(self, source_module: str, dest_module: str, symbol_name: str):
        self.source_module = source_module
        self.dest_module = dest_module
        self.symbol_name = symbol_name
        self.changed = False

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated_node.module is None:
            return updated_node

        module_name = cst.Module([]).code_for_node(updated_node.module).strip()

        if module_name == self.source_module:
            if isinstance(updated_node.names, cst.ImportStar):
                return updated_node

            for name in updated_node.names:
                if isinstance(name, cst.ImportAlias):
                    imported_name = name.name.value if isinstance(name.name, cst.Name) else str(name.name)
                    if imported_name == self.symbol_name:
                        new_module = cst.parse_expression(self.dest_module)
                        if isinstance(new_module, (cst.Name, cst.Attribute)):
                            self.changed = True
                            return updated_node.with_changes(module=new_module)

        return updated_node


class _ModuleImportRenamer(cst.CSTTransformer):
    """Rewrite import statements to replace old_module with new_module."""

    def __init__(self, old_module: str, new_module: str):
        self.old_module = old_module
        self.new_module = new_module
        self.changed = False

    def _module_name_str(self, node) -> str:
        """Convert a module attribute/name node to dotted string."""
        return cst.Module([]).code_for_node(node).strip()

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated_node.module is None:
            return updated_node

        module_name = self._module_name_str(updated_node.module)

        if module_name == self.old_module:
            # Exact match: from old_module import X -> from new_module import X
            new_mod_node = cst.parse_expression(self.new_module)
            if isinstance(new_mod_node, (cst.Name, cst.Attribute)):
                self.changed = True
                return updated_node.with_changes(module=new_mod_node)
        elif module_name.startswith(self.old_module + '.'):
            # Prefix match: from old_module.sub import X -> from new_module.sub import X
            suffix = module_name[len(self.old_module):]
            new_name = self.new_module + suffix
            new_mod_node = cst.parse_expression(new_name)
            if isinstance(new_mod_node, (cst.Name, cst.Attribute)):
                self.changed = True
                return updated_node.with_changes(module=new_mod_node)

        return updated_node

    def leave_Import(
        self, original_node: cst.Import, updated_node: cst.Import
    ) -> cst.Import:
        if isinstance(updated_node.names, cst.ImportStar):
            return updated_node

        new_names = []
        any_changed = False
        for alias in updated_node.names:
            if isinstance(alias, cst.ImportAlias):
                name_str = self._module_name_str(alias.name)
                if name_str == self.old_module:
                    new_mod_node = cst.parse_expression(self.new_module)
                    if isinstance(new_mod_node, (cst.Name, cst.Attribute)):
                        any_changed = True
                        new_names.append(alias.with_changes(name=new_mod_node))
                        continue
                elif name_str.startswith(self.old_module + '.'):
                    suffix = name_str[len(self.old_module):]
                    new_name = self.new_module + suffix
                    new_mod_node = cst.parse_expression(new_name)
                    if isinstance(new_mod_node, (cst.Name, cst.Attribute)):
                        any_changed = True
                        new_names.append(alias.with_changes(name=new_mod_node))
                        continue
            new_names.append(alias)

        if any_changed:
            self.changed = True
            return updated_node.with_changes(names=new_names)
        return updated_node


def move_module(
    source_path: str,
    destination: str,
    project_path: str | None = None,
    apply: bool = False
) -> dict[str, str]:
    """Move a module to another package, updating imports.

    Args:
        source_path: Path to module file to move
        destination: Destination package path like 'pkg.subpkg' or folder path
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes
    """
    import shutil
    import os

    project_root = _find_project_root(project_path or source_path)
    old_module = _file_to_module(source_path, project_root)

    # Resolve destination to a directory path
    if '.' in destination and not os.path.isdir(destination):
        # Dotted module path like "pkg.subpkg"
        dest_dir = Path(project_root) / Path(destination.replace('.', '/'))
    else:
        # Could be a relative path or absolute path
        dest_dir_candidate = Path(destination)
        if not dest_dir_candidate.is_absolute():
            dest_dir = Path(project_root) / dest_dir_candidate
        else:
            dest_dir = dest_dir_candidate

    # New file location
    new_path = dest_dir / Path(source_path).name
    new_module = _file_to_module(str(new_path), project_root)

    # Update all imports across project
    diffs = _rename_module_references(project_root, old_module, new_module, apply)

    if apply:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(new_path))
        return {}

    # For dry-run, describe the file move
    description = f"Move {source_path} -> {new_path}"
    diffs["__description__"] = description
    return diffs


def rename_module(
    file_path: str,
    new_name: str,
    project_path: str | None = None,
    apply: bool = False
) -> dict[str, str]:
    """Rename a module file, updating imports across the project.

    Args:
        file_path: Path to module file to rename
        new_name: New name for the module (without .py extension)
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes
    """
    project_root = _find_project_root(project_path or file_path)
    old_module = _file_to_module(file_path, project_root)
    parts = old_module.rsplit('.', 1)
    new_module = f"{parts[0]}.{new_name}" if len(parts) > 1 else new_name

    diffs = _rename_module_references(project_root, old_module, new_module, apply)

    if apply:
        new_path = Path(file_path).parent / f"{new_name}.py"
        Path(file_path).rename(new_path)
        return {}

    # For dry-run, describe the file rename
    new_path = Path(file_path).parent / f"{new_name}.py"
    description = f"Rename {file_path} -> {new_path}"
    diffs["__description__"] = description
    return diffs


# ============================================================================
# Unified Commands (lookup, edit) - simplified interface combining multiple
# commands with convenient aliases
# ============================================================================

def _cmd_lookup_single_selector(
    selector: ExtendedSelector,
    file_or_pattern: str,
    case_insensitive: bool = False,
    smart_case: bool = False,
    json_output: bool = False,
    metadata: bool = False,
    paths_only: bool = False,
    count: bool = False,
    dedent: bool = False,
) -> str:
    """Lookup logic for a single (non-glob) selector."""
    # Handle line-based selectors with metadata - find containing symbol
    if selector.line_start is not None and metadata:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_line
        file_path = Path(selector.file_path)
        symbols = find_nested_definitions(str(file_path))
        symbol = find_symbol_by_line(symbols, selector.line_start, selector.line_end)

        if symbol is None:
            print(f"No symbol found at line {selector.line_start}")
            raise SystemExit(1)

        selector = ExtendedSelector(
            file_path=selector.file_path,
            symbol_path=symbol.path,
        )

    # Handle metadata output
    if metadata:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_path
        file_path = Path(selector.file_path)
        symbols = find_nested_definitions(str(file_path))
        symbol = find_symbol_by_path(symbols, selector.symbol_path)

        if symbol is None:
            raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

        selector_path = f"{selector.file_path}::{'.'.join(symbol.path)}"
        total_lines = symbol.line_end - symbol.line_start + 1

        with open(selector.file_path) as f:
            lines = f.readlines()
        offset_start = sum(len(line) for line in lines[:symbol.line_start - 1])
        offset_end = sum(len(line) for line in lines[:symbol.line_end])

        output = [
            selector_path,
            "-" * 50,
            f"  Lines: {symbol.line_start}-{symbol.line_end} ({total_lines} lines)",
            f"  Offset: {offset_start}-{offset_end}",
        ]

        if symbol.decorators:
            decs_with_prefix = [f"@{d}" if not d.startswith('@') else d for d in symbol.decorators]
            dec_str = ", ".join(decs_with_prefix)
            output.append(f"  Decorators: {dec_str}")

        if symbol.parameters:
            param_names = ", ".join(symbol.parameters)
            output.append(f"  Parameters: {len(symbol.parameters)} ({param_names})")

        output.append(f"  Kind: {symbol.kind}")

        return "\n".join(output) + "\n"

    # If wildcard without component and with query flags, treat as query
    if selector.has_wildcards() and not selector.component and (count or paths_only or json_output):
        from emend.query import cmd_query

        old_stdout = sys.stdout
        sys.stdout = buffer = io.StringIO()
        try:
            cmd_query(
                filepath=file_or_pattern,
                kinds=None,
                names=None,
                decorators=None,
                returns_patterns=None,
                in_classes=None,
                depths=None,
                params=None,
                case_insensitive=case_insensitive,
                smart_case=smart_case,
                output_json=json_output,
                paths_only=paths_only,
                count_only=count,
            )
        finally:
            sys.stdout = old_stdout

        return buffer.getvalue()

    # If component specified, act like get
    if selector.component:
        if selector.has_wildcards():
            from emend.ast_utils import find_nested_definitions, expand_wildcard_path
            file_path = Path(selector.file_path)
            symbols = find_nested_definitions(str(file_path))
            matched_symbols = expand_wildcard_path(symbols, selector.symbol_path)

            if not matched_symbols:
                raise ValueError(f"No symbols match pattern {'.'.join(selector.symbol_path)}")

            results = []
            for sym in matched_symbols:
                specific_selector = ExtendedSelector(
                    file_path=selector.file_path,
                    symbol_path=sym.path,
                    component=selector.component,
                    accessor=selector.accessor,
                    pseudo_class=selector.pseudo_class,
                )
                try:
                    result = get_component(specific_selector)
                    if json_output:
                        results.append({"symbol": '.'.join(sym.path), "value": result})
                    else:
                        results.append(result)
                except (ValueError, FileNotFoundError):
                    pass

            if json_output:
                return json.dumps(results, indent=2)
            else:
                return '\n'.join(results)
        else:
            return get_component(selector)
    else:
        # No component - act like show
        if selector.has_wildcards():
            from emend.ast_utils import find_nested_definitions, expand_wildcard_path
            file_path = Path(selector.file_path)
            symbols = find_nested_definitions(str(file_path))
            matched_symbols = expand_wildcard_path(symbols, selector.symbol_path)

            if not matched_symbols:
                raise ValueError(f"No symbols match pattern {'.'.join(selector.symbol_path)}")

            results = []
            for sym in matched_symbols:
                specific_selector = ExtendedSelector(
                    file_path=selector.file_path,
                    symbol_path=sym.path,
                )
                try:
                    result = get_symbol_source(specific_selector, dedent=dedent)
                    results.append(result)
                except (ValueError, FileNotFoundError):
                    pass

            return '\n'.join(results)
        return get_symbol_source(selector, dedent=dedent)


def cmd_lookup(
    file_or_pattern: str,
    selector_str: str | None = None,
    kind: list[str] | None = None,
    name: list[str] | None = None,
    has_decorator: list[str] | None = None,
    returns: list[str] | None = None,
    in_class: list[str] | None = None,
    depth: list[str] | None = None,
    has_param: list[str] | None = None,
    case_insensitive: bool = False,
    smart_case: bool = False,
    json_output: bool = False,
    metadata: bool = False,
    paths_only: bool = False,
    count: bool = False,
    dedent: bool = False,
    matching: str | None = None,
    type_oracle: TypeOracle | None = None,
    out: "IO[str] | None" = None,
) -> str:
    """Unified lookup command combining get, query, and show.

    If selector_str contains component (e.g., [params], [returns]), acts like get.
    If filter flags provided, acts like query.
    Otherwise acts like show (display source code).
    """
    # If filter flags provided, act as query
    if any([kind, name, has_decorator, returns, in_class, depth, has_param]):
        from emend.query import cmd_query

        # Expand file globs for query mode
        import glob as glob_mod
        files_to_query = []
        fop = Path(file_or_pattern)
        if fop.is_dir():
            files_to_query = [str(f) for f in fop.rglob("*.py")]
        elif '*' in file_or_pattern or '?' in file_or_pattern:
            files_to_query = [f for f in glob_mod.glob(file_or_pattern, recursive=True) if f.endswith('.py')]
        else:
            files_to_query = [file_or_pattern]

        if out is not None and not count:
            # Streaming path: write each file's output directly to out as it completes
            for fpath in files_to_query:
                old_stdout = sys.stdout
                sys.stdout = out
                try:
                    cmd_query(
                        filepath=fpath,
                        kinds=kind,
                        names=name,
                        decorators=has_decorator,
                        returns_patterns=returns,
                        in_classes=in_class,
                        depths=depth,
                        params=has_param,
                        case_insensitive=case_insensitive,
                        smart_case=smart_case,
                        output_json=json_output,
                        paths_only=paths_only,
                        count_only=False,
                        type_oracle=type_oracle,
                    )
                finally:
                    sys.stdout = old_stdout
                out.flush()
            return ''

        all_output = []
        total_count_val = 0
        for fpath in files_to_query:
            old_stdout = sys.stdout
            sys.stdout = buffer = io.StringIO()
            try:
                cmd_query(
                    filepath=fpath,
                    kinds=kind,
                    names=name,
                    decorators=has_decorator,
                    returns_patterns=returns,
                    in_classes=in_class,
                    depths=depth,
                    params=has_param,
                    case_insensitive=case_insensitive,
                    smart_case=smart_case,
                    output_json=json_output,
                    paths_only=paths_only,
                    count_only=count,
                    type_oracle=type_oracle,
                )
            finally:
                sys.stdout = old_stdout
            result = buffer.getvalue()
            if result:
                if count:
                    try:
                        total_count_val += int(result.strip())
                    except ValueError:
                        all_output.append(result)
                else:
                    all_output.append(result)

        if count:
            return str(total_count_val) + '\n'
        return ''.join(all_output)

    # Parse selector if provided
    if selector_str:
        selector = parse_extended_selector(selector_str)

        # Reject line selectors with file globs
        if selector.has_file_glob() and selector.line_start is not None:
            raise ValueError("Line selectors cannot be combined with file globs")

        # Multi-file dispatch for file globs
        if selector.has_file_glob():
            expanded_files = selector.expand_file_glob()

            if out is not None and not matching:
                # Streaming path: write each file's result to out as it completes
                any_results = False
                for fpath in expanded_files:
                    concrete = selector.with_file_path(fpath)
                    try:
                        result = _cmd_lookup_single_selector(
                            concrete,
                            file_or_pattern=fpath,
                            case_insensitive=case_insensitive,
                            smart_case=smart_case,
                            json_output=json_output,
                            metadata=metadata,
                            paths_only=paths_only,
                            count=count,
                            dedent=dedent,
                        )
                        if result:
                            out.write(result)
                            if not result.endswith('\n'):
                                out.write('\n')
                            out.flush()
                            any_results = True
                    except (ValueError, FileNotFoundError):
                        continue
                if not any_results:
                    raise ValueError(f"No symbols found matching {selector_str}")
                return ''

            all_results = []
            for fpath in expanded_files:
                concrete = selector.with_file_path(fpath)
                try:
                    result = _cmd_lookup_single_selector(
                        concrete,
                        file_or_pattern=fpath,
                        case_insensitive=case_insensitive,
                        smart_case=smart_case,
                        json_output=json_output,
                        metadata=metadata,
                        paths_only=paths_only,
                        count=count,
                        dedent=dedent,
                    )
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue

            if not all_results:
                raise ValueError(f"No symbols found matching {selector_str}")

            combined = '\n'.join(all_results)

            # Apply --matching filter if specified
            if matching:
                combined = _apply_matching_filter(combined, matching, selector, expanded_files, json_output)

            return combined

        result = _cmd_lookup_single_selector(
            selector,
            file_or_pattern=file_or_pattern,
            case_insensitive=case_insensitive,
            smart_case=smart_case,
            json_output=json_output,
            metadata=metadata,
            paths_only=paths_only,
            count=count,
            dedent=dedent,
        )

        # Apply --matching filter for single-file selectors
        if matching and result:
            result = _apply_matching_filter(
                result, matching, selector, [selector.file_path], json_output
            )

        return result
    else:
        raise ValueError("No selector provided")


def _apply_matching_filter(
    lookup_result: str,
    matching_pattern: str,
    selector: ExtendedSelector,
    files: list[str],
    json_output: bool = False,
) -> str:
    """Filter lookup results to only symbols whose body matches a pattern."""
    filtered_parts = []
    for part in lookup_result.split('\n'):
        part = part.strip()
        if not part:
            continue
        # Try to parse as a selector path (file.py::Symbol.path format)
        if '::' in part:
            try:
                sel = parse_extended_selector(part)
                source = get_symbol_source(sel)
                matches = find_pattern(matching_pattern, sel.file_path, source_override=source)
                if matches:
                    filtered_parts.append(part)
            except (ValueError, FileNotFoundError):
                filtered_parts.append(part)
        else:
            # For source code output, check the whole result against the pattern
            for fpath in files:
                try:
                    matches = find_pattern(matching_pattern, fpath, source_override=lookup_result)
                    if matches:
                        return lookup_result
                except (ValueError, FileNotFoundError):
                    pass
            return ""

    return '\n'.join(filtered_parts)


def _merge_type_filter(
    selector: ExtendedSelector,
    returns_filter: list[str] | None,
) -> list[str] | None:
    """Merge a selector's :returns[X] type_filter into the returns_filter list.

    If the selector has a ``type_filter`` like ``returns[str]``, the type
    string is appended to (or creates) the returns_filter list so the
    existing returns-based filtering logic handles it.
    """
    if selector.type_filter is None:
        return returns_filter
    # Parse "returns[str]" or "type[Connection]"
    tf = selector.type_filter
    bracket = tf.index("[")
    kind = tf[:bracket]
    type_string = tf[bracket + 1:-1]
    if kind == "returns":
        merged = list(returns_filter) if returns_filter else []
        merged.append(type_string)
        return merged
    # For :type[X], pass through as-is (future: filter by inferred type)
    return returns_filter


def _expand_selector_with_returns_filter(
    selector: ExtendedSelector,
    returns_filter: list[str],
    type_oracle: TypeOracle | None = None,
) -> list[ExtendedSelector]:
    """Expand a selector to only include symbols matching a returns filter.

    Uses annotation-based matching, falling back to type oracle when available.
    Returns concrete selectors for each matching symbol.
    """
    import fnmatch as _fnmatch
    from .query import _collect_symbols, _filter_by_returns_with_oracle

    file_path = Path(selector.file_path)
    if not file_path.exists():
        return []
    source = file_path.read_text()
    symbols = _collect_symbols(file_path, source)

    # Build type index if oracle available
    file_types = None
    if type_oracle is not None:
        file_types = type_oracle.infer_file(file_path)

    result = []
    for symbol in symbols:
        # Extract symbol's path segments from its full path (file.py::Class.method → [Class, method])
        parts = symbol.path.split("::")
        sym_path = parts[1].split(".") if len(parts) > 1 else [symbol.name]

        # Check if symbol matches the selector's symbol_path pattern
        if len(sym_path) != len(selector.symbol_path):
            continue
        match = True
        for seg, pat in zip(sym_path, selector.symbol_path):
            if pat != "*" and not _fnmatch.fnmatch(seg, pat):
                match = False
                break
        if not match:
            continue

        # Check returns filter
        if not _filter_by_returns_with_oracle(
            symbol, returns_filter, case_insensitive=False, file_types=file_types,
        ):
            continue

        # Create concrete selector for this symbol
        concrete = ExtendedSelector(
            file_path=selector.file_path,
            symbol_path=sym_path,
            component=selector.component,
            accessor=selector.accessor,
            pseudo_class=selector.pseudo_class,
        )
        result.append(concrete)

    return result


def _cmd_edit_single(
    selector: ExtendedSelector,
    value: str | None = None,
    rm: bool = False,
    apply: bool = False,
) -> str:
    """Edit logic for a single (non-glob) selector."""
    if rm or value == "":
        return remove_component(selector, apply=apply)

    if selector.pseudo_class is not None:
        raise ValueError(
            f"Cannot use pseudo-class '{selector.pseudo_class}' with 'edit' command. "
            "Use 'add' command to insert new items."
        )

    if value is not None:
        return set_component(selector, value, apply=apply)

    raise ValueError("No operation specified (provide value or --rm)")


def cmd_edit(
    selector_str: str,
    value: str | None = None,
    rm: bool = False,
    apply: bool = False,
    returns_filter: list[str] | None = None,
    type_oracle: TypeOracle | None = None,
) -> str:
    """Edit or replace existing symbol components.

    - If rm=True or value="", remove the component or symbol
    - If accessor present + value, modify specific item (e.g., [params][x])
    - If no accessor + value, replace entire component (e.g., [returns])
    - If returns_filter or selector :returns[X] specified, only edit symbols
      whose return type matches (annotation first, then inferred via oracle)
    """
    selector = parse_extended_selector(selector_str)

    # Merge selector type_filter into returns_filter
    returns_filter = _merge_type_filter(selector, returns_filter)

    # When returns_filter is specified, expand the selector to only include
    # symbols that match the return type constraint.
    if returns_filter:
        files = (
            selector.expand_file_glob()
            if selector.has_file_glob()
            else [selector.file_path]
        )
        all_results = []
        for fpath in files:
            concrete_base = selector.with_file_path(fpath) if fpath != selector.file_path else selector
            matching = _expand_selector_with_returns_filter(
                concrete_base, returns_filter, type_oracle,
            )
            for concrete in matching:
                try:
                    result = _cmd_edit_single(concrete, value=value, rm=rm, apply=apply)
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str} with --returns {returns_filter}")
        return '\n'.join(all_results)

    # Multi-file dispatch for file globs
    if selector.has_file_glob():
        expanded_files = selector.expand_file_glob()
        all_results = []
        for fpath in expanded_files:
            concrete = selector.with_file_path(fpath)
            try:
                result = _cmd_edit_single(concrete, value=value, rm=rm, apply=apply)
                if result:
                    all_results.append(result)
            except (ValueError, FileNotFoundError):
                continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str}")
        return '\n'.join(all_results)

    return _cmd_edit_single(selector, value=value, rm=rm, apply=apply)


def _cmd_add_single(
    selector: ExtendedSelector,
    value: str,
    before: str | None = None,
    after: str | None = None,
    at: int | None = None,
    apply: bool = False,
) -> str:
    """Add logic for a single (non-glob) selector."""
    position = at if at is not None else -1
    kind = selector.pseudo_class if selector.pseudo_class else None
    return add_to_component(
        selector,
        value,
        position=position,
        before=before,
        after=after,
        apply=apply,
        kind=kind,
    )


def cmd_add(
    selector_str: str,
    value: str,
    before: str | None = None,
    after: str | None = None,
    at: int | None = None,
    apply: bool = False,
    returns_filter: list[str] | None = None,
    type_oracle: TypeOracle | None = None,
) -> str:
    """Add new items to symbol components.

    - Position can be specified with --at, --before, or --after
    - Default is to append to end
    - Pseudo-class (e.g., :KEYWORD_ONLY) specifies parameter kind
    - If returns_filter or selector :returns[X] specified, only add to symbols
      whose return type matches (annotation first, then inferred via oracle)
    """
    selector = parse_extended_selector(selector_str)

    # Merge selector type_filter into returns_filter
    returns_filter = _merge_type_filter(selector, returns_filter)

    # When returns_filter is specified, expand the selector to only include
    # symbols that match the return type constraint.
    if returns_filter:
        files = (
            selector.expand_file_glob()
            if selector.has_file_glob()
            else [selector.file_path]
        )
        all_results = []
        for fpath in files:
            concrete_base = selector.with_file_path(fpath) if fpath != selector.file_path else selector
            matching = _expand_selector_with_returns_filter(
                concrete_base, returns_filter, type_oracle,
            )
            for concrete in matching:
                try:
                    result = _cmd_add_single(concrete, value=value, before=before, after=after, at=at, apply=apply)
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str} with --returns {returns_filter}")
        return '\n'.join(all_results)

    # Multi-file dispatch for file globs
    if selector.has_file_glob():
        expanded_files = selector.expand_file_glob()
        all_results = []
        for fpath in expanded_files:
            concrete = selector.with_file_path(fpath)
            try:
                result = _cmd_add_single(concrete, value=value, before=before, after=after, at=at, apply=apply)
                if result:
                    all_results.append(result)
            except (ValueError, FileNotFoundError):
                continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str}")
        return '\n'.join(all_results)

    return _cmd_add_single(selector, value=value, before=before, after=after, at=at, apply=apply)
