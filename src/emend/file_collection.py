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

# Module-level cache: (project_root, language) → (root_mtime_ns, file_list)
_file_list_cache: dict[tuple[str, str], tuple[int, list[str]]] = {}


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
    except Exception:
        return None


def collect_source_files(
    project_root: str,
    language: str = "python",
    git_tracked_only: bool = False,
) -> list[str]:
    """Collect all source files for *language* in project, with caching.

    Uses the Rust backend for speed.  Caches the file list per project root,
    invalidated when the root directory's mtime changes.

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

    import os
    resolved = str(Path(project_root).resolve())
    try:
        root_mtime = os.stat(resolved).st_mtime_ns
    except OSError:
        t0 = time.monotonic()
        files = collect_source_files_scandir(resolved, language=language)
        logger.info(
            "collect_source_files: %d files in %.3fs (scandir, %s)",
            len(files), time.monotonic() - t0, resolved,
        )
        return files

    cache_key = (resolved, language)
    cached = _file_list_cache.get(cache_key)
    if cached is not None and cached[0] == root_mtime:
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
    _file_list_cache[cache_key] = (root_mtime, files)
    return files
