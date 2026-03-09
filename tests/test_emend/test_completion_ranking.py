
import pytest
import textwrap
from pathlib import Path
from emend.editor_search import EditorSearchEngine, _dispatch
from conftest import build_indexed_project

SAMPLE_SOURCE = textwrap.dedent("""\
    import os
    import sys

    module_var = 1

    class Base:
        def base_method(self):
            pass

    class Derived(Base):
        def derived_method(self):
            local_in_derived = 2
            # cursor_here
            pass

    def func_top_level():
        func_local = 3
        # func_cursor_here
        pass

    d = Derived()
    # attr_cursor_here
""")

@pytest.fixture
def engine(tmp_path):
    proj = build_indexed_project(tmp_path, {"app.py": SAMPLE_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()

def test_completion_ranking_locals(engine):
    eng, proj = engine
    app_path = str((proj / "app.py").resolve())
    
    # Test completion at # func_cursor_here
    lines = SAMPLE_SOURCE.splitlines()
    line_no = 0
    for i, line in enumerate(lines):
        if "# func_cursor_here" in line:
            line_no = i + 1
            break
            
    # Complete "func"
    result = _dispatch(eng, "complete", {
        "prefix": "func",
        "file": app_path,
        "line": line_no,
        "col": 4
    })
    
    words = [item["word"] for item in result["items"]]
    # Should find func_local (local) and func_top_level (module)
    assert "func_local" in words
    assert "func_top_level" in words
    
    # func_local should be ranked higher than func_top_level because it's a local
    idx_local = words.index("func_local")
    idx_module = words.index("func_top_level")
    assert idx_local < idx_module

def test_completion_ranking_scopes(engine):
    eng, proj = engine
    app_path = str((proj / "app.py").resolve())
    
    # At # cursor_here
    lines = SAMPLE_SOURCE.splitlines()
    line_no = 0
    for i, line in enumerate(lines):
        if "# cursor_here" in line:
            line_no = i + 1
            break

    # Complete "module"
    result = _dispatch(eng, "complete", {
        "prefix": "module",
        "file": app_path,
        "line": line_no,
        "col": 8
    })
    words = [item["word"] for item in result["items"]]
    assert "module_var" in words

    # Complete "os" (import)
    result = _dispatch(eng, "complete", {
        "prefix": "o",
        "file": app_path,
        "line": line_no,
        "col": 8
    })
    words = [item["word"] for item in result["items"]]
    assert "os" in words

def test_attribute_completion_inheritance(engine):
    eng, proj = engine
    app_path = str((proj / "app.py").resolve())
    
    # At # attr_cursor_here
    lines = SAMPLE_SOURCE.splitlines()
    line_no = 0
    for i, line in enumerate(lines):
        if "# attr_cursor_here" in line:
            line_no = i + 1
            break

    # Complete "Derived."
    result = _dispatch(eng, "complete", {
        "prefix": "Derived.",
        "file": app_path,
        "line": line_no,
        "col": 0
    })
    
    words = [item["word"] for item in result["items"]]
    # Should show both methods
    assert "derived_method" in words
    assert "base_method" in words
