
import pytest
import textwrap
import sqlite3
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

def test_completion_ranking_triple_tier(engine_with_remote):
    eng, proj = engine_with_remote
    app_path = str((proj / "app.py").resolve())
    
    # At # cursor_here
    lines = SAMPLE_SOURCE.splitlines()
    line_no = 0
    for i, line in enumerate(lines):
        if "# cursor_here" in line:
            line_no = i + 1
            break
            
    # Complete 'm'
    # Expected: 
    # 1. module_var (Module item - 1500)
    # 2. math (Import - 500)
    # 3. module_remote (Remote - 100)
    
    # We need to add 'import math' to app.py
    source_with_import = SAMPLE_SOURCE.replace("import sys", "import math")
    (proj / "app.py").write_text(source_with_import)
    # Reindex is okay for just updating one file
    _dispatch(eng, "reindex", {})

    result = _dispatch(eng, "complete", {
        "prefix": "m",
        "file": app_path,
        "line": line_no,
        "col": 8
    })
    
    words = [item["word"] for item in result["items"]]
    assert "module_var" in words
    assert "math" in words
    assert "module_remote" in words
    
    idx_module = words.index("module_var")
    idx_import = words.index("math")
    idx_remote = words.index("module_remote")
    
    assert idx_module < idx_import < idx_remote

@pytest.fixture
def engine_with_remote(tmp_path):
    proj = build_indexed_project(tmp_path, {
        "app.py": SAMPLE_SOURCE,
        "other.py": "module_remote = 1"
    })
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()

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


def test_completion_function_parameters(engine):
    eng, proj = engine
    app_path = str((proj / "app.py").resolve())

    # Test completion for function parameters
    source = textwrap.dedent("""\
        def my_func(param_one, param_two):
            # cursor_here
            param
        """)
    (proj / "test.py").write_text(source)

    lines = source.splitlines()
    line_no = 0
    for i, line in enumerate(lines):
        if "# cursor_here" in line:
            line_no = i + 1
            break

    test_path = str((proj / "test.py").resolve())
    _dispatch(eng, "reindex", {})

    # Complete "param" inside the function
    result = _dispatch(eng, "complete", {
        "prefix": "param",
        "file": test_path,
        "line": line_no,
        "col": 4
    })

    words = [item["word"] for item in result["items"]]
    # Should find param_one and param_two
    assert "param_one" in words, f"param_one not in {words}"
    assert "param_two" in words, f"param_two not in {words}"
