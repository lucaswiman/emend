import pytest
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from emend.editor_search import EditorSearchEngine, is_fuzzy_subsequence

def test_is_fuzzy_subsequence():
    assert is_fuzzy_subsequence("foo/bar", "src/foo/bar/baz.py")
    assert is_fuzzy_subsequence("fxo/bar", "src/foo/bar/baz.py")
    assert not is_fuzzy_subsequence("xyz", "src/foo/bar/baz.py")
    assert is_fuzzy_subsequence("abc", "axbycz")
    assert is_fuzzy_subsequence("abc", "axy") is False
    # 1 substitution allowed by default
    assert is_fuzzy_subsequence("abcd", "abxd")
    assert is_fuzzy_subsequence("abcd", "axyd") is False

def test_editor_search_files(tmp_path):
    # Setup a dummy DB with some files
    db_path = tmp_path / ".emend/cache/parse.db"
    db_path.parent.mkdir(parents=True)
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE symbol_index (name, qualified_name, kind, file_path, line, end_line, signature, returns, depth, parent)")
    conn.execute("INSERT INTO symbol_index (name, file_path, kind) VALUES (?, ?, ?)", ("foo", "src/foo.py", "function"))
    conn.execute("INSERT INTO symbol_index (name, file_path, kind) VALUES (?, ?, ?)", ("bar", "src/bar.py", "function"))
    conn.execute("INSERT INTO symbol_index (name, file_path, kind) VALUES (?, ?, ?)", ("baz", "pkg/baz.ts", "class"))
    conn.commit()
    conn.close()

    engine = EditorSearchEngine(str(tmp_path))
    directory = tmp_path / "docs"
    directory.mkdir()
    
    # Search for "foo.py"
    res = engine.search("foo.py")
    assert any(item["kind"] == "file" and item["file_path"] == "src/foo.py" for item in res.items)

    # Search for "pkg/baz"
    res = engine.search("pkg/baz")
    assert any(item["kind"] == "file" and item["file_path"] == "pkg/baz.ts" for item in res.items)

    # Fuzzy search "p/bz" -> pkg/baz.ts
    res = engine.search("p/bz")
    assert any(item["kind"] == "file" and item["file_path"] == "pkg/baz.ts" for item in res.items)
    
    # Changes in a nested directory must be visible without touching the root.
    assert engine.search("guide.txt").items == []
    guide = directory / "guide.txt"
    guide.write_text("guide")
    assert [item["file_path"] for item in engine.search("guide.txt").items] == [str(guide)]
    guide.unlink()
    assert engine.search("guide.txt").items == []
    engine.close()


def test_cold_file_scope_applies_before_candidate_limit(tmp_path):
    for directory, count in [("outside", 250), ("inside", 1)]:
        folder = tmp_path / directory
        folder.mkdir()
        for index in range(count):
            (folder / f"match{index}.txt").write_text("")
    engine = EditorSearchEngine(str(tmp_path))
    try:
        result = engine.search("match", file_scope="inside")
        assert [item["file_path"] for item in result.items] == [str(tmp_path / "inside/match0.txt")]
    finally:
        engine.close()


def test_nested_git_project_inventory_refreshes_on_index_change(tmp_path):
    project = tmp_path / "nested"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'nested'\nversion = '0.0.0'\n")
    path = project / "notes.txt"
    path.write_text("notes")
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    engine = EditorSearchEngine(str(project))
    try:
        assert engine.search("notes.txt").items
        git("rm", "--cached", "nested/notes.txt")
        assert path.exists()
        assert engine.search("notes.txt").items == []
    finally:
        engine.close()


@pytest.mark.parametrize("mutation", ["create_file", "remove_directory"])
def test_inventory_changed_during_enumeration_refreshes_next_query(tmp_path, monkeypatch, mutation):
    directory = tmp_path / "docs"
    directory.mkdir()
    path = directory / "later.txt"
    original = os.scandir

    @contextmanager
    def changing_scan(folder):
        with original(folder) as entries:
            yield entries
        if mutation == "create_file" and Path(folder) == directory and not path.exists():
            path.write_text("new file")
        elif mutation == "remove_directory" and Path(folder) == tmp_path and directory.exists():
            directory.rmdir()

    monkeypatch.setattr(os, "scandir", changing_scan)
    engine = EditorSearchEngine(str(tmp_path))
    try:
        assert engine.search("later.txt").items == []
        assert [item["file_path"] for item in engine.search("later.txt").items] == (
            [str(path)] if mutation == "create_file" else [])
    finally:
        engine.close()


@pytest.mark.parametrize("cache", ["missing", "empty", "partial"])
def test_cold_picker_uses_files_without_analysis(tmp_path, monkeypatch, cache):
    import sqlite3
    from unittest.mock import Mock

    (tmp_path / "main.py").write_text("def navigate_workspace():\n    pass\n")
    (tmp_path / "notes.txt").write_text("ordinary files")
    db_path = tmp_path / ".emend/cache/parse.db"
    if cache != "missing":
        db_path.parent.mkdir(parents=True)
        with sqlite3.connect(db_path) as db:
            if cache == "partial":
                db.execute("CREATE TABLE symbol_index (name TEXT)")
    engine = EditorSearchEngine(str(tmp_path))
    try:
        reader = Mock(side_effect=AssertionError("cold picker opened the analysis cache"))
        lookup = Mock(side_effect=AssertionError("cold picker indexed dependencies"))
        monkeypatch.setattr(engine, "_get_conn", reader)
        monkeypatch.setattr("emend.transform.lookup_venv_symbol", lookup)
        for query, expected in [("", {"main.py", "notes.txt"}), ("main", {"main.py"})]:
            result = engine.search(query)
            assert {item["name"] for item in result.items} == expected
            assert all(item["kind"] == "file" for item in result.items)
        assert engine.search("navigate_workspace").items == []
        reader.assert_not_called()
        lookup.assert_not_called()
        if cache == "missing":
            assert not db_path.exists()
    finally:
        engine.close()
