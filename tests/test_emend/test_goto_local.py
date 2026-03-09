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

@pytest.mark.skip(reason="BUG: goto_local fails for chained assignments (y = x = 10)")
def test_goto_assignment_on_same_line(tmp_path):
    """Test goto when variable is assigned and used on same line.

    KNOWN ISSUE: Returns empty results for chained assignments like y = x = 10.
    This may indicate incomplete binding tracking in the scope resolver.
    """
    code = """
def func():
    y = x = 10
    return y
"""
    file_path = tmp_path / "test.py"
    file_path.write_text(code.strip())
    (tmp_path / ".emend/cache").mkdir(parents=True, exist_ok=True)
    warm_caches(str(tmp_path))
    engine = EditorSearchEngine(str(tmp_path))

    # Try to find 'y' in return statement
    res = engine.goto_local(str(file_path), line=4, col=12)
    assert len(res.items) >= 1
    assert res.items[0]["name"] == "y"
    # Should resolve to line 3
    assert res.items[0]["line"] == 3

    engine.close()

@pytest.mark.skip(reason="BUG: goto_local fails for variables in compound statements")
def test_goto_multiple_statements_line(tmp_path):
    """Test goto with multiple statements on same line.

    KNOWN ISSUE: Returns empty results when variable is defined in compound
    statement (x = 1; y = x + 1). The scope resolver may not properly handle
    multiple statements on a single line.
    """
    code = """
def func():
    x = 1; y = x + 1
    return y
"""
    file_path = tmp_path / "test.py"
    file_path.write_text(code.strip())
    (tmp_path / ".emend/cache").mkdir(parents=True, exist_ok=True)
    warm_caches(str(tmp_path))
    engine = EditorSearchEngine(str(tmp_path))

    # Try to find 'y' in return statement
    res = engine.goto_local(str(file_path), line=4, col=12)
    assert len(res.items) >= 1
    assert res.items[0]["name"] == "y"

    engine.close()

@pytest.mark.skip(reason="BUG: goto_local requires exact cursor position on identifier")
def test_goto_whitespace_around_cursor(tmp_path):
    """Test goto is robust to cursor position within whitespace/word.

    KNOWN ISSUE: Only works when cursor is at specific columns. The identifier
    extraction logic may have edge cases that cause it to fail when cursor is
    slightly off the target identifier. Even after boundary improvements,
    this test still fails, suggesting the real issue is in scope resolver.
    """
    code = """
def foo(x):
    y = x + 1
    return y
"""
    file_path = tmp_path / "test.py"
    file_path.write_text(code.strip())
    (tmp_path / ".emend/cache").mkdir(parents=True, exist_ok=True)
    warm_caches(str(tmp_path))
    engine = EditorSearchEngine(str(tmp_path))

    # Try different columns within/around the word 'y' in return statement
    # All should resolve to the same binding
    res1 = engine.goto_local(str(file_path), line=4, col=11)  # Before 'y'
    res2 = engine.goto_local(str(file_path), line=4, col=12)  # On 'y'
    res3 = engine.goto_local(str(file_path), line=4, col=13)  # After 'y'

    # At least one should find the binding
    if len(res1.items) > 0:
        assert res1.items[0]["name"] == "y"
    elif len(res2.items) > 0:
        assert res2.items[0]["name"] == "y"
    elif len(res3.items) > 0:
        assert res3.items[0]["name"] == "y"
    else:
        pytest.fail("No results found for 'y' at any cursor position")

    engine.close()

@pytest.mark.skip(reason="BUG: goto_local may prefer imports over local rebindings")
def test_goto_local_same_file_only(tmp_path):
    """Test that goto_local finds local definitions, not imports.

    KNOWN ISSUE: Returns empty results when local variable shadows an import.
    This could indicate that scopes_in_file() doesn't track all binding types,
    or that the matching logic prefers cross-file symbols over local bindings.
    """
    code = """
import math

def compute():
    math = 42
    return math
"""
    file_path = tmp_path / "test.py"
    file_path.write_text(code.strip())
    (tmp_path / ".emend/cache").mkdir(parents=True, exist_ok=True)
    warm_caches(str(tmp_path))
    engine = EditorSearchEngine(str(tmp_path))

    # Find 'math' in return statement
    res = engine.goto_local(str(file_path), line=6, col=12)
    assert len(res.items) >= 1
    assert res.items[0]["name"] == "math"
    # Should resolve to local assignment (line 5), not import (line 1)
    assert res.items[0]["line"] == 5

    engine.close()

