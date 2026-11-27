
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


def test_attribute_completion_prefers_local_scope_usage(engine):
    eng, proj = engine
    source = textwrap.dedent("""\
        class Workflow:
            def alpha(self):
                pass

            def archive(self):
                pass

        class Worker:
            def run(self):
                wf = Workflow()
                wf.archive()
                return wf.
    """)
    (proj / "workflow.py").write_text(source)
    _dispatch(eng, "reindex", {})
    file_path = str((proj / "workflow.py").resolve())

    result = _dispatch(eng, "complete", {
        "prefix": "wf.",
        "file": file_path,
        "line": 12,
        "col": 18,
    })

    words = [item["word"] for item in result["items"]]
    menus = {item["word"]: item.get("menu", "") for item in result["items"]}
    assert "archive" in words
    assert "alpha" in words
    assert menus["archive"] == "[scope:wf]"
    assert words.index("archive") < words.index("alpha")


# ---------------------------------------------------------------------------
# Analysis-aware completion: CFG-informed ranking, dotted completion scoring,
# and the complete_diagnostics RPC
# ---------------------------------------------------------------------------


CFG_RANKING_SOURCE = textwrap.dedent("""\
    def process(items):
        total = 0
        for item in items:
            total += item
        result = total * 2
        return result
""")


DOTTED_COMPLETION_SOURCE = textwrap.dedent("""\
    class Animal:
        def speak(self):
            pass

        def eat(self, food):
            pass

    class Dog(Animal):
        def fetch(self, ball):
            pass

        def bark(self):
            pass

    def main():
        d = Dog()
        d.bark()
        d.fetch("ball")
        d.speak()
""")


MULTI_FUNCTION_SOURCE = textwrap.dedent("""\
    def first():
        alpha = 1
        beta = 2
        return alpha + beta

    def second():
        gamma = 10
        delta = 20
        return gamma + delta
""")


@pytest.fixture
def cfg_engine(tmp_path):
    proj = build_indexed_project(tmp_path, {"code.py": CFG_RANKING_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


@pytest.fixture
def dotted_engine(tmp_path):
    proj = build_indexed_project(tmp_path, {"animals.py": DOTTED_COMPLETION_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


@pytest.fixture
def multi_engine(tmp_path):
    proj = build_indexed_project(tmp_path, {"funcs.py": MULTI_FUNCTION_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


class TestCfgInformedCompletion:
    """Variables defined after the cursor should be demoted in ranking."""

    def test_variable_before_cursor_ranked_high(self, cfg_engine):
        """A variable defined before the cursor line gets score >= 1800."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        # Line 5 (1-based): `result = total * 2`
        # At this point, `total` is defined on line 2, so it should rank high
        result = eng.complete("t", file=file_path, line=5, col=0)
        items_by_name = {item["word"]: item for item in result.items}
        assert items_by_name["total"]["score"] >= 1800

    def test_variable_after_cursor_demoted(self, cfg_engine):
        """A variable defined after the cursor line gets a lower score."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        # Line 2 (1-based): `total = 0`
        # At this point, `result` is defined on line 5, so it should be demoted
        result = eng.complete("r", file=file_path, line=2, col=0)
        items_by_name = {item["word"]: item for item in result.items}
        assert items_by_name["result"]["score"] < 1800

    def test_parameter_always_available(self, cfg_engine):
        """Parameters should always have high scores since they're always in scope."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        # Line 2 (1-based): `total = 0`
        # `items` is a parameter, always available
        result = eng.complete("i", file=file_path, line=2, col=0)
        items_by_name = {item["word"]: item for item in result.items}
        assert items_by_name["items"]["score"] >= 1800

    def test_multi_function_isolation(self, multi_engine):
        """Variables from a different function should not appear as locals."""
        eng, proj = multi_engine
        file_path = str((proj / "funcs.py").resolve())
        # Inside `first()` at line 3 (1-based): `return alpha + beta`
        result = eng.complete("g", file=file_path, line=3, col=0)
        local_items = [i for i in result.items if i.get("menu", "").startswith("[local")]
        local_names = {i["word"] for i in local_items}
        # gamma/delta are in second(), not first()
        assert "gamma" not in local_names
        assert "delta" not in local_names

    def test_local_menu_label(self, cfg_engine):
        """Variables defined before cursor get [local], after get [local?]."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        # Line 3 (1-based): `for item in items:`
        # `total` defined on line 2 -> [local], `result` on line 5 -> [local?]
        result = eng.complete("", file=file_path, line=3, col=0)
        items_by_name = {item["word"]: item for item in result.items}
        assert items_by_name["total"]["menu"] == "[local]"
        assert items_by_name["result"]["menu"] == "[local?]"


class TestDottedCompletionEnrichment:
    """Dotted completions should leverage the reference_index table."""

    def test_dotted_completion_scoring(self, dotted_engine):
        """Symbol-index members score 1000, reference-index members score 800."""
        eng, proj = dotted_engine
        result = eng.complete("Dog.")
        assert result.items
        for item in result.items:
            if item.get("menu", "").startswith("[ref:"):
                assert item["score"] == 800
            elif item.get("kind") != "reference":
                assert item["score"] >= 800


REFERENCED_MEMBER_SOURCE = textwrap.dedent("""\
    class Dog:
        def bark(self):
            pass

    def main():
        Dog.fetch_stick()
        Dog.bark()
""")


def test_dotted_completion_offers_members_that_are_only_referenced(tmp_path):
    """A member used on a parent but defined nowhere comes from the fact graph."""
    from emend.transform import warm_caches

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "animals.py").write_text(REFERENCED_MEMBER_SOURCE)
    warm_caches(str(proj), type_engine=None)
    eng = EditorSearchEngine(str(proj))
    try:
        items = {item["word"]: item for item in eng.complete("Dog.fe").items}
        defined = {item["word"]: item for item in eng.complete("Dog.ba").items}
    finally:
        eng.close()
    assert items["fetch_stick"] == {
        "word": "fetch_stick",
        "kind": "reference",
        "menu": "[ref:animals.Dog.fetch_stick]",
        "score": 800,
    }
    # A defined member is offered as the symbol it is, not as a reference.
    assert defined["bark"]["kind"] != "reference"


class TestLatencyInstrumentation:
    """The complete() method should report actual elapsed time."""

    def test_complete_diagnostics_rpc(self, cfg_engine):
        """complete_diagnostics returns timing breakdown and signal info."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        result = _dispatch(eng, "complete_diagnostics", {
            "prefix": "t",
            "file": file_path,
            "line": 3,
            "col": 0,
        })
        assert result["mode"] == "complete_diagnostics"
        assert len(result["items"]) >= 1
        # First item should be the diagnostics dict
        diag = result["items"][0]
        assert "timings" in diag
        assert "signals" in diag
        assert diag["timings"]["total_ms"] >= 0
        assert isinstance(diag["timings"]["item_count"], int)
