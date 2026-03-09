import pytest
from pathlib import Path
from emend.editor_search import EditorSearchEngine
from emend.transform import warm_caches

def test_goto_local_python(tmp_path):
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
    
    # Initialize index
    warm_caches(str(tmp_path))
    
    # We need to initialize the project and index it
    engine = EditorSearchEngine(str(tmp_path))
    
    # Try to find definition of 'y' at line 3, col 12
    # This should find the assignment at line 2
    res = engine.goto_local(str(file_path), line=3, col=12)
    assert len(res.items) == 1
    assert res.items[0]["line"] == 2
    assert res.items[0]["name"] == "y"

    # Try to find definition of 'x' at line 2, col 9 (usage of x)
    res = engine.goto_local(str(file_path), line=2, col=9)
    assert len(res.items) >= 1
    assert res.items[0]["line"] == 1
    assert res.items[0]["name"] == "x"

    engine.close()

def test_goto_local_ts(tmp_path):
    code = """
function greet(name: string) {
    console.log(name);
}
"""
    file_path = tmp_path / "test.ts"
    file_path.write_text(code.strip())
    
    (tmp_path / ".emend/cache").mkdir(parents=True, exist_ok=True)
    warm_caches(str(tmp_path))
    engine = EditorSearchEngine(str(tmp_path))
    
    # name is at line 1, col 16 (0-based) -> 1-based: 16
    # function greet(name: string) {
    # 0123456789012345
    # usage at line 2:
    #     console.log(name);
    # 01234567890123456
    
    res = engine.goto_local(str(file_path), line=2, col=17)
    assert len(res.items) >= 1
    assert res.items[0]["line"] == 1
    assert res.items[0]["name"] == "name"

    engine.close()
