"""Project file collection utilities.

Centralises all file-discovery logic so that CLI commands, duplicate
detection, lint, and the MCP surface share one code path with
consistent caching and gitignore handling.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level cache: (project_root, language) → (dir_signature, file_list).
# The signature maps each directory that contains (or leads to) a collected
# source file to its ``st_mtime_ns``.  Keying on the root directory alone is
# insufficient: on Linux a directory's mtime only changes when entries are
# added/removed *directly* inside it, so creating ``pkg/new_module.py`` never
# touches the project root's mtime.  A long-running process (editor / MCP
# server) would then keep serving a stale list.  Validating the per-directory
# signature costs only O(#dirs) ``stat`` calls on a cache hit — no re-walk.
_file_list_cache: dict[tuple[str, str], tuple[dict[str, int], list[str]]] = {}


def _dir_signature(files: list[str], resolved: str) -> dict[str, int]:
    """Map every directory containing/leading to a collected file to its mtime.

    Includes each file's parent directory and all ancestors up to (and
    including) *resolved*.  Adding or removing a source file bumps its parent
    directory's mtime; creating a new package directory bumps its parent's
    mtime — both are ancestors of existing files (or the root), so the change
    is detected without walking the whole tree again.
    """
    import os

    root = Path(resolved)
    dirs: set[str] = {resolved}
    for f in files:
        p = Path(f).parent
        while True:
            s = str(p)
            if s in dirs:
                break
            dirs.add(s)
            if p == root or root not in p.parents:
                break
            p = p.parent

    sig: dict[str, int] = {}
    for d in dirs:
        try:
            sig[d] = os.stat(d).st_mtime_ns
        except OSError:
            # Directory vanished between scan and signature build; skip it.
            # A missing tracked directory is detected as invalid on next check.
            pass
    return sig


def _signature_valid(sig: dict[str, int]) -> bool:
    """True iff every recorded directory still has its recorded mtime."""
    import os

    for d, mtime in sig.items():
        try:
            if os.stat(d).st_mtime_ns != mtime:
                return False
        except OSError:
            return False
    return True


def collect_source_files_scandir(root_path: str, language: str = "python") -> list[str]:
    """Walk a directory tree using the Rust emend_core module."""
    from emend.language_registry import get_extensions
    from emend import emend_core as _rust
    exts = get_extensions(language)
    return _rust.collect_files(root_path, exts)


def detect_project_languages(project_root: str) -> list[str]:
    """Detect which languages are present in a project.

    Inspects the project root for language markers:
    - Python: any .py file or pyproject.toml/setup.py
    - TypeScript: package.json, tsconfig.json, or any .ts/.tsx/.js/.jsx file
    - Rust: Cargo.toml or any .rs file

    Returns a list of detected language names (e.g. ``["python", "typescript"]``).
    """
    import os

    root = Path(project_root).resolve()
    detected: list[str] = []

    def _scan_dir(directory: Path) -> list[str]:
        names: list[str] = []
        try:
            for entry in os.scandir(str(directory)):
                names.append(entry.name)
        except OSError:
            pass
        return names

    root_names = set(_scan_dir(root))

    all_names: set[str] = set(root_names)
    for entry_name in root_names:
        child = root / entry_name
        if entry_name.startswith(".") or entry_name in {
            "node_modules", "target", "__pycache__", ".venv", "venv",
        }:
            continue
        if child.is_dir():
            all_names.update(_scan_dir(child))
    if (root / "src").is_dir():
        all_names.update(_scan_dir(root / "src"))

    py_markers = {"pyproject.toml", "setup.py", "setup.cfg"}
    py_exts = (".py", ".pyi")
    if py_markers & root_names or any(n.endswith(py_exts) for n in all_names):
        detected.append("python")

    ts_markers = {"package.json", "tsconfig.json"}
    ts_exts = (".ts", ".tsx", ".js", ".jsx")
    if ts_markers & root_names or any(n.endswith(ts_exts) for n in all_names):
        detected.append("typescript")

    rs_markers = {"Cargo.toml", "Cargo.lock"}
    rs_exts = (".rs",)
    if rs_markers & root_names or any(n.endswith(rs_exts) for n in all_names):
        detected.append("rust")

    return detected


def collect_all_source_files(
    root_path: str,
    languages: list[str] | None = None,
) -> list[str]:
    """Collect source files for all detected (or specified) languages.

    When *languages* is ``None``, calls :func:`detect_project_languages` to
    determine which languages are present.  Returns a de-duplicated list of
    absolute file paths.
    """
    if languages is None:
        languages = detect_project_languages(root_path)
    all_files: list[str] = []
    seen: set[str] = set()
    for lang in languages:
        for f in collect_source_files_scandir(root_path, language=lang):
            if f not in seen:
                seen.add(f)
                all_files.append(f)
    return all_files


def collect_git_tracked_source_files(
    project_root: str, language: str = "python",
) -> list[str] | None:
    """Return git-tracked source files, or None if not in a git repo."""
    import subprocess
    from emend.language_registry import get_extensions
    exts = get_extensions(language)

    resolved = str(Path(project_root).resolve())
    try:
        pathspecs = [f"*.{ext}" for ext in exts]
        result = subprocess.run(
            ['git', 'ls-files', '-z'] + pathspecs,
            capture_output=True, timeout=10,
            cwd=resolved,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout
        if not raw:
            return []
        rel_paths = raw.decode('utf-8', errors='replace').split('\0')
        abs_paths = []
        for p in rel_paths:
            p = p.strip()
            if p:
                abs_paths.append(str(Path(resolved) / p))
        return abs_paths
    except (OSError, subprocess.SubprocessError):
        logger.debug("git ls-files failed in %s", resolved, exc_info=True)
        return None


def collect_source_files(
    project_root: str,
    language: str = "python",
    git_tracked_only: bool = False,
) -> list[str]:
    """Collect all source files for *language* in project, with caching.

    Uses the Rust backend for speed.  Caches the file list per project root,
    invalidated when any directory containing (or leading to) a source file
    changes its mtime — so a file added inside a subdirectory is picked up even
    within a single long-running process.

    If *git_tracked_only* is True, uses ``git ls-files`` to only return
    files tracked by git.  Falls back to directory scan if not in a
    git repository.
    """
    if git_tracked_only:
        tracked = collect_git_tracked_source_files(project_root, language=language)
        if tracked is not None:
            logger.info(
                "collect_source_files: %d git-tracked files in %s",
                len(tracked), project_root,
            )
            return tracked

    resolved = str(Path(project_root).resolve())

    cache_key = (resolved, language)
    cached = _file_list_cache.get(cache_key)
    if cached is not None and _signature_valid(cached[0]):
        logger.debug(
            "collect_source_files: %d files (cached, %s)",
            len(cached[1]), resolved,
        )
        return cached[1]

    t0 = time.monotonic()
    files = collect_source_files_scandir(resolved, language=language)
    logger.info(
        "collect_source_files: %d files in %.3fs (%s)",
        len(files), time.monotonic() - t0, resolved,
    )
    _file_list_cache[cache_key] = (_dir_signature(files, resolved), files)
    return files
