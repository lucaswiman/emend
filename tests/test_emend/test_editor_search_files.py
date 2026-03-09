import pytest
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
    
    # Search for "foo.py"
    res = engine.search("foo.py")
    assert any(item["kind"] == "file" and item["file_path"] == "src/foo.py" for item in res.items)

    # Search for "pkg/baz"
    res = engine.search("pkg/baz")
    assert any(item["kind"] == "file" and item["file_path"] == "pkg/baz.ts" for item in res.items)

    # Fuzzy search "p/bz" -> pkg/baz.ts
    res = engine.search("p/bz")
    assert any(item["kind"] == "file" and item["file_path"] == "pkg/baz.ts" for item in res.items)
    
    engine.close()
