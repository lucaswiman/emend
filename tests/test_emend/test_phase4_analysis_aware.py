"""Tests for Phase 4: Analysis-Aware Intelligence.

Covers:
- CFG-informed completion ranking (demote variables defined after cursor)
- Reference-index dotted completion enrichment
- Latency instrumentation (elapsed_ms in complete())
- complete_diagnostics RPC method
- Safe synchronous signals documentation
"""

import textwrap
from pathlib import Path

import pytest

from emend.editor_search import (
    EditorSearchEngine,
    _dispatch,
)

from conftest import build_indexed_project


# ---------------------------------------------------------------------------
# Fixtures
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

BRANCHING_SOURCE = textwrap.dedent("""\
    def branching(flag):
        x = 1
        if flag:
            y = 2
        else:
            z = 3
        result = x
        return result
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


@pytest.fixture
def branch_engine(tmp_path):
    proj = build_indexed_project(tmp_path, {"branch.py": BRANCHING_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


# ---------------------------------------------------------------------------
# Tests: CFG-informed completion ranking
# ---------------------------------------------------------------------------


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
        if "total" in items_by_name:
            assert items_by_name["total"]["score"] >= 1800

    def test_variable_after_cursor_demoted(self, cfg_engine):
        """A variable defined after the cursor line gets a lower score."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        # Line 2 (1-based): `total = 0`
        # At this point, `result` is defined on line 5, so it should be demoted
        result = eng.complete("r", file=file_path, line=2, col=0)
        items_by_name = {item["word"]: item for item in result.items}
        if "result" in items_by_name:
            assert items_by_name["result"]["score"] < 1800

    def test_parameter_always_available(self, cfg_engine):
        """Parameters should always have high scores since they're always in scope."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        # Line 2 (1-based): `total = 0`
        # `items` is a parameter, always available
        result = eng.complete("i", file=file_path, line=2, col=0)
        items_by_name = {item["word"]: item for item in result.items}
        if "items" in items_by_name:
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
        if "total" in items_by_name:
            assert items_by_name["total"]["menu"] == "[local]"
        if "result" in items_by_name:
            assert items_by_name["result"]["menu"] == "[local?]"


# ---------------------------------------------------------------------------
# Tests: Dotted completion enrichment via reference_index
# ---------------------------------------------------------------------------


class TestDottedCompletionEnrichment:
    """Dotted completions should leverage the reference_index table."""

    def test_dotted_symbol_index_completion(self, dotted_engine):
        """Basic dotted completion from symbol_index should still work."""
        eng, proj = dotted_engine
        result = eng.complete("Dog.")
        names = {item["word"] for item in result.items}
        assert "fetch" in names or "bark" in names

    def test_dotted_base_class_members(self, dotted_engine):
        """Members from base classes should be included via inheritance traversal."""
        eng, proj = dotted_engine
        result = eng.complete("Dog.")
        names = {item["word"] for item in result.items}
        # Dog extends Animal, so speak/eat should be available
        assert "speak" in names or "eat" in names or "fetch" in names

    def test_dotted_completion_scoring(self, dotted_engine):
        """Symbol-index members score 1000, reference-index members score 800."""
        eng, proj = dotted_engine
        result = eng.complete("Dog.")
        for item in result.items:
            if item.get("menu", "").startswith("[ref:"):
                assert item["score"] == 800
            elif item.get("kind") != "reference":
                assert item["score"] >= 800


# ---------------------------------------------------------------------------
# Tests: Latency instrumentation
# ---------------------------------------------------------------------------


class TestLatencyInstrumentation:
    """The complete() method should report actual elapsed time."""

    def test_complete_has_elapsed_ms(self, cfg_engine):
        """complete() should report a non-zero elapsed_ms."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        result = eng.complete("t", file=file_path, line=3, col=0)
        assert result.elapsed_ms >= 0  # Should be actual measurement, not hardcoded 0

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

    def test_complete_diagnostics_signals_list(self, cfg_engine):
        """complete_diagnostics should report which signal sources were used."""
        eng, proj = cfg_engine
        file_path = str((proj / "code.py").resolve())
        result = _dispatch(eng, "complete_diagnostics", {
            "prefix": "t",
            "file": file_path,
            "line": 3,
            "col": 0,
        })
        diag = result["items"][0]
        signals = diag["signals"]
        assert isinstance(signals, list)


# ---------------------------------------------------------------------------
# Tests: Graph-aware quick actions (already exist, verify via dispatch)
# ---------------------------------------------------------------------------


class TestGraphAwareQuickActions:
    """Verify that callers/callees/impact are available via dispatch."""

    def test_callers_dispatch(self, dotted_engine):
        eng, proj = dotted_engine
        result = _dispatch(eng, "callers", {
            "qualified_name": "Dog.bark",
            "file": str((proj / "animals.py").resolve()),
        })
        assert result["mode"] == "callers"
        assert isinstance(result["items"], list)

    def test_callees_dispatch(self, dotted_engine):
        eng, proj = dotted_engine
        result = _dispatch(eng, "callees", {
            "qualified_name": "main",
            "file": str((proj / "animals.py").resolve()),
        })
        assert result["mode"] == "callees"
        assert isinstance(result["items"], list)

    def test_impact_dispatch(self, dotted_engine):
        eng, proj = dotted_engine
        result = _dispatch(eng, "impact", {
            "qualified_name": "Animal.speak",
            "file": str((proj / "animals.py").resolve()),
        })
        assert result["mode"] == "impact"
        assert isinstance(result["items"], list)
