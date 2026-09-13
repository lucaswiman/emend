"""Project file collection utilities.

Centralises all file-discovery logic so that CLI commands, duplicate
detection, lint, and the MCP surface share one code path with
consistent directory filtering.
"""

from __future__ import annotations

import logging
import time
import stat as stat_module
from pathlib import Path


def file_stat_identity(stat):
    return (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
        getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000)),
    )


def source_file_identities(paths):
    """Inventory source metadata without reading or parsing unchanged files."""
    result = {}
    for path in paths:
        try:
            stat = Path(path).stat()
        except OSError:
            continue
        if not stat_module.S_ISREG(stat.st_mode):
            continue
        result[str(path)] = file_stat_identity(stat)
    return result

logger = logging.getLogger(__name__)

def collect_source_files_scandir(
    root_path: str,
    language: str = "python",
    *,
    skip_dirs: list[str] | None = None,
) -> list[str]:
    """Walk source files; optional exclusions match names or ``*suffix``."""
    from emend.language_registry import get_extensions
    from emend import emend_core as _rust
    exts = get_extensions(language)
    if skip_dirs is None:
        return _rust.collect_files(root_path, exts)
    return _rust.collect_files(root_path, exts, skip_dirs)


def detect_project_languages(project_root: str) -> list[str]:
    """Detect which languages are present in a project.

    Walks source files using the configured language-extension registry and
    also checks root-level Python, TypeScript, and Rust project markers.

    Returns a list of detected language names (e.g. ``["python", "typescript"]``).
    """
    root = Path(project_root).resolve()
    from emend import emend_core as _rust
    from emend.language_registry import registry_snapshot

    extension_languages, language_extensions = registry_snapshot(root)
    extensions = sorted(extension_languages)
    files = _rust.collect_files(str(root), extensions)
    detected = {
        extension_languages[Path(file).suffix.removeprefix(".").lower()]
        for file in files
        if Path(file).suffix.removeprefix(".").lower() in extension_languages
    }

    py_markers = {"pyproject.toml", "setup.py", "setup.cfg"}
    if any((root / marker).is_file() for marker in py_markers):
        detected.add("python")

    ts_markers = {"package.json", "tsconfig.json"}
    if any((root / marker).is_file() for marker in ts_markers):
        detected.add("typescript")

    rs_markers = {"Cargo.toml", "Cargo.lock"}
    if any((root / marker).is_file() for marker in rs_markers):
        detected.add("rust")

    return [language for language in language_extensions if language in detected]


def collect_all_source_files(
    root_path: str,
    languages: list[str] | None = None,
    *,
    registry: tuple[dict[str, str], dict[str, list[str]]] | None = None,
) -> list[str]:
    """Collect source files for all detected (or specified) languages.

    When *languages* is ``None``, scans once for every configured extension.
    Returns a de-duplicated list of absolute file paths.
    """
    from emend import emend_core as _rust
    from emend.language_registry import registry_snapshot

    registry = registry or registry_snapshot(root_path)
    language_extensions = registry[1]
    languages = languages if languages is not None else list(language_extensions)
    extensions = sorted({
        extension
        for language in languages
        for extension in language_extensions.get(language, ())
    })
    return _rust.collect_files(root_path, extensions) if extensions else []


def collect_git_tracked_source_files(
    project_root: str, language: str = "python",
) -> list[str] | None:
    """Return git-tracked source files, or None if not in a git repo."""
    from os import fsdecode
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
        return [
            str(Path(resolved) / fsdecode(path))
            for path in result.stdout.split(b'\0') if path
        ]
    except (OSError, subprocess.SubprocessError):
        logger.debug("git ls-files failed in %s", resolved, exc_info=True)
        return None


def collect_source_files(
    project_root: str,
    language: str = "python",
    git_tracked_only: bool = False,
) -> list[str]:
    """Collect all source files for *language* using a fresh Rust directory scan.

    Rescanning also discovers the first source file added to an existing empty
    directory and avoids relying on filesystem timestamp granularity.

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

    t0 = time.monotonic()
    files = collect_source_files_scandir(resolved, language=language)
    logger.info(
        "collect_source_files: %d files in %.3fs (%s)",
        len(files), time.monotonic() - t0, resolved,
    )
    return files
