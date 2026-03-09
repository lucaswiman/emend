import pytest
from pathlib import Path
from emend.editor_search import EditorSearchEngine

def test_goto_local(tmp_path):
    # Create a test file with some local variables
    code = """
def foo(x):
    y = x + 1
    return y

z = foo(10)
"""
    file_path = tmp_path / "test.py"
    file_path.write_text(code.strip())
    
    # Ensure cache directory exists
    (tmp_path / ".emend/cache").mkdir(parents=True, exist_ok=True)
    
    # We need to initialize the project and index it
    engine = EditorSearchEngine(str(tmp_path))
    
    # Mocking references_in_file or actually running it if emend_core is available.
    # Since I built emend_core, I should be able to run it.
    
    # x is at line 1, col 8 (0-indexed line 0, col 8? No, 1-indexed line 1, col 8)
    # y = x + 1 (line 2)
    # y (line 3)
    
    # Try to find definition of 'y' at line 3, col 11
    res = engine.goto_local(str(file_path), line=3, col=11)
    assert len(res.items) == 1
    assert res.items[0]["line"] == 2
    assert res.items[0]["name"] == "y"

    # Try to find definition of 'x' at line 2, col 8
    res = engine.goto_local(str(file_path), line=2, col=8)
    assert len(res.items) == 1
    assert res.items[0]["line"] == 1
    assert res.items[0]["name"] == "x"

    engine.close()
