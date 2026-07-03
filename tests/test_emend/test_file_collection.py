"""Tests for emend.file_collection caching behaviour."""

from __future__ import annotations

import os
from pathlib import Path

from emend import file_collection
from emend.file_collection import collect_source_files


def _bump_mtime(path: Path) -> None:
    """Advance *path*'s mtime deterministically (avoid fs-granularity flakiness)."""
    st = path.stat()
    future = st.st_mtime_ns + 5_000_000_000  # +5s in ns
    os.utime(path, ns=(future, future))


def test_collect_source_files_detects_new_file_in_subdir(tmp_path, monkeypatch):
    """A file added to a subdirectory must invalidate the process-lifetime cache.

    Regression: the cache was keyed only on the project ROOT's mtime, which on
    Linux does not change when a file is created inside a subdirectory.  A
    long-running server (editor / MCP) therefore kept serving a stale list.
    """
    monkeypatch.setattr(file_collection, "_file_list_cache", {})

    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "a.py").write_text("x = 1\n")

    first = collect_source_files(str(tmp_path))
    assert {Path(f).name for f in first} == {"a.py"}

    # Add a new file inside the subdirectory; root mtime is unchanged on Linux.
    (sub / "b.py").write_text("y = 2\n")
    _bump_mtime(sub)  # deterministic: adding an entry advances the dir's mtime

    second = collect_source_files(str(tmp_path))
    assert {Path(f).name for f in second} == {"a.py", "b.py"}


def test_collect_source_files_cache_hit_when_unchanged(tmp_path, monkeypatch):
    """An unchanged tree must not trigger a rescan (cache stays effective)."""
    monkeypatch.setattr(file_collection, "_file_list_cache", {})

    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "a.py").write_text("x = 1\n")

    calls = {"n": 0}
    real = file_collection.collect_source_files_scandir

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(file_collection, "collect_source_files_scandir", counting)

    first = collect_source_files(str(tmp_path))
    second = collect_source_files(str(tmp_path))
    assert first == second
    assert calls["n"] == 1, "unchanged tree should be served from cache without rescan"


def test_collect_source_files_detects_removed_file(tmp_path, monkeypatch):
    """Removing a file must invalidate the cache."""
    monkeypatch.setattr(file_collection, "_file_list_cache", {})

    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "a.py").write_text("x = 1\n")
    (sub / "b.py").write_text("y = 2\n")

    first = collect_source_files(str(tmp_path))
    assert {Path(f).name for f in first} == {"a.py", "b.py"}

    (sub / "b.py").unlink()
    _bump_mtime(sub)

    second = collect_source_files(str(tmp_path))
    assert {Path(f).name for f in second} == {"a.py"}
