"""Tests for the location resolver (Phase 7)."""
import pytest

from emend.location_resolver import (
    LocationResolver,
    MODULE_LEVEL_BLOCK,
    MODULE_LEVEL_FUNC,
    ResolvedLocation,
)
from emend.fact_graph import (
    CfgBlockFact,
    FactGraph,
    SourceLocFact,
    SymbolFact,
)


class TestResolvedLocation:
    def test_module_level_sentinel(self):
        loc = ResolvedLocation(
            file_path="a.py",
            func_qn=MODULE_LEVEL_FUNC,
            block_id=MODULE_LEVEL_BLOCK,
            line=1,
        )
        assert loc.is_module_level

    def test_function_level(self):
        loc = ResolvedLocation(file_path="a.py", func_qn="mod.func", block_id=0, line=5)
        assert not loc.is_module_level

    def test_frozen(self):
        loc = ResolvedLocation(file_path="a.py", func_qn="mod.func", block_id=0, line=5)
        with pytest.raises((AttributeError, TypeError)):
            loc.line = 10  # type: ignore[misc]

    def test_default_col(self):
        loc = ResolvedLocation(file_path="a.py", func_qn="mod.func", block_id=0, line=5)
        assert loc.col == 0

    def test_captures_default_empty(self):
        loc = ResolvedLocation(file_path="a.py", func_qn="mod.func", block_id=0, line=5)
        assert loc.captures == {}


class TestLocationResolverFromSource:
    """Test resolver when no FactGraph is available."""

    def test_resolve_function_level(self, tmp_path):
        source = """\
def foo():
    x = 1
    return x

def bar():
    y = 2
    return y
"""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        # line 2: "    x = 1" is inside foo (lines 1-3)
        loc = resolver.resolve(str(p), 2)
        assert "foo" in loc.func_qn
        assert not loc.is_module_level

    def test_resolve_module_level(self, tmp_path):
        source = """\
x = 1

def foo():
    y = 2
"""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        # line 1: "x = 1" is module-level
        loc = resolver.resolve(str(p), 1)
        assert loc.is_module_level

    def test_resolve_nested_function(self, tmp_path):
        source = """\
def outer():
    def inner():
        x = 1
    return inner
"""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        # line 3: "        x = 1" is inside inner
        loc = resolver.resolve(str(p), 3)
        assert "inner" in loc.func_qn

    def test_resolve_batch(self, tmp_path):
        source = """\
def foo():
    x = 1
    y = 2
    return x + y
"""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        locs = resolver.resolve_batch(str(p), [2, 3, 4])
        assert len(locs) == 3
        for loc in locs:
            assert not loc.is_module_level
            assert "foo" in loc.func_qn

    def test_resolve_preserves_col_and_captures(self, tmp_path):
        source = """\
def foo():
    x = 1
"""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        loc = resolver.resolve(str(p), 2, col=4, captures={"X": "1"})
        assert loc.col == 4
        assert loc.captures == {"X": "1"}

    def test_resolve_batch_with_col(self, tmp_path):
        source = """\
def foo():
    x = 1
    y = 2
"""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        locs = resolver.resolve_batch(str(p), [2, 3], col=4)
        assert locs[0].col == 4
        assert locs[1].col == 4

    def test_second_function_resolved_correctly(self, tmp_path):
        source = """\
def foo():
    x = 1

def bar():
    y = 2
    return y
"""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        # line 5: "    y = 2" should be in bar, not foo
        loc = resolver.resolve(str(p), 5)
        assert "bar" in loc.func_qn
        assert "foo" not in loc.func_qn

    def test_empty_file(self, tmp_path):
        source = ""
        p = tmp_path / "test.py"
        p.write_text(source)
        resolver = LocationResolver.from_source(str(p), source)
        loc = resolver.resolve(str(p), 1)
        assert loc.is_module_level


class TestLocationResolverFromFactGraph:
    """Test resolver when FactGraph is available."""

    def _make_graph_with_func(self, file_path: str = "app.py") -> FactGraph:
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo",
            qualified_name="mod.foo",
            kind="function",
            file_path=file_path,
            line=2,
            end_line=5,
        ))
        g.add_cfg_block(CfgBlockFact(
            file_path=file_path,
            func_qn="mod.foo",
            block_id=0,
            is_entry=True,
        ))
        g.add_source_loc(SourceLocFact(
            file_path=file_path,
            loc_kind="block",
            loc_id="mod.foo:0",
            line=2,
            end_line=5,
        ))
        return g

    def test_resolve_with_facts(self):
        g = self._make_graph_with_func()
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        loc = resolver.resolve("app.py", 3)
        assert loc.func_qn == "mod.foo"
        assert loc.block_id == 0
        assert not loc.is_module_level

    def test_module_level_with_facts(self):
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo",
            qualified_name="mod.foo",
            kind="function",
            file_path="app.py",
            line=5,
            end_line=10,
        ))
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        # line 2 is before foo (lines 5-10)
        loc = resolver.resolve("app.py", 2)
        assert loc.is_module_level

    def test_file_path_filter(self):
        """Facts from other files are excluded when file_path is specified."""
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo", qualified_name="mod.foo", kind="function",
            file_path="app.py", line=2, end_line=5,
        ))
        g.add_symbol(SymbolFact(
            name="bar", qualified_name="other.bar", kind="function",
            file_path="other.py", line=2, end_line=5,
        ))
        # Ask only about app.py — other.py functions should not be indexed
        resolver_a = LocationResolver.from_fact_graph(g, "app.py")
        loc_a = resolver_a.resolve("app.py", 3)
        assert loc_a.func_qn == "mod.foo"

        resolver_b = LocationResolver.from_fact_graph(g, "other.py")
        loc_b = resolver_b.resolve("other.py", 3)
        assert loc_b.func_qn == "other.bar"

    def test_resolve_with_multiple_blocks(self):
        """Line falling in exactly one block is resolved correctly."""
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo", qualified_name="mod.foo", kind="function",
            file_path="app.py", line=1, end_line=10,
        ))
        g.add_cfg_block(CfgBlockFact(file_path="app.py", func_qn="mod.foo", block_id=0, is_entry=True))
        g.add_cfg_block(CfgBlockFact(file_path="app.py", func_qn="mod.foo", block_id=1))
        g.add_source_loc(SourceLocFact(file_path="app.py", loc_kind="block", loc_id="mod.foo:0", line=1, end_line=4))
        g.add_source_loc(SourceLocFact(file_path="app.py", loc_kind="block", loc_id="mod.foo:1", line=5, end_line=10))
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        loc = resolver.resolve("app.py", 7)
        assert loc.block_id == 1

    def test_no_blocks_falls_back_to_module_block(self):
        """When no blocks are found, block_id defaults to MODULE_LEVEL_BLOCK."""
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo", qualified_name="mod.foo", kind="function",
            file_path="app.py", line=2, end_line=5,
        ))
        # No cfg_block or source_loc added
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        loc = resolver.resolve("app.py", 3)
        # func_qn resolved via symbol facts
        assert loc.func_qn == "mod.foo"
        # block_id falls back to MODULE_LEVEL_BLOCK (0)
        assert loc.block_id == MODULE_LEVEL_BLOCK

    def test_method_kind_included(self):
        """Methods are included in function range detection."""
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="method", qualified_name="MyClass.method", kind="method",
            file_path="app.py", line=10, end_line=15,
        ))
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        loc = resolver.resolve("app.py", 12)
        assert loc.func_qn == "MyClass.method"

    def test_async_function_kind_included(self):
        """Async functions are included in function range detection."""
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="afunc", qualified_name="mod.afunc", kind="async_function",
            file_path="app.py", line=3, end_line=8,
        ))
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        loc = resolver.resolve("app.py", 5)
        assert loc.func_qn == "mod.afunc"


class TestSameLineMultipleBlocks:
    """Regression: same line can appear in multiple blocks (e.g., if header)."""

    def test_most_specific_block(self):
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo", qualified_name="mod.foo", kind="function",
            file_path="app.py", line=1, end_line=10,
        ))
        # Two blocks: block 0 spans lines 1-10 (wider), block 1 spans 3-5 (narrower)
        g.add_cfg_block(CfgBlockFact(file_path="app.py", func_qn="mod.foo", block_id=0))
        g.add_cfg_block(CfgBlockFact(file_path="app.py", func_qn="mod.foo", block_id=1))
        g.add_source_loc(SourceLocFact(file_path="app.py", loc_kind="block", loc_id="mod.foo:0", line=1, end_line=10))
        g.add_source_loc(SourceLocFact(file_path="app.py", loc_kind="block", loc_id="mod.foo:1", line=3, end_line=5))
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        # line 4 is within block 1's narrower range — should prefer block 1
        loc = resolver.resolve("app.py", 4)
        assert loc.block_id == 1

    def test_wider_block_when_narrow_not_containing(self):
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo", qualified_name="mod.foo", kind="function",
            file_path="app.py", line=1, end_line=10,
        ))
        g.add_cfg_block(CfgBlockFact(file_path="app.py", func_qn="mod.foo", block_id=0))
        g.add_cfg_block(CfgBlockFact(file_path="app.py", func_qn="mod.foo", block_id=1))
        g.add_source_loc(SourceLocFact(file_path="app.py", loc_kind="block", loc_id="mod.foo:0", line=1, end_line=10))
        g.add_source_loc(SourceLocFact(file_path="app.py", loc_kind="block", loc_id="mod.foo:1", line=6, end_line=8))
        resolver = LocationResolver.from_fact_graph(g, "app.py")
        # line 2 is only within block 0
        loc = resolver.resolve("app.py", 2)
        assert loc.block_id == 0


class TestFactGraphResolveLocation:
    """Test the FactGraph.resolve_location() convenience method."""

    def test_resolve_location_returns_func_and_block(self):
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo", qualified_name="mod.foo", kind="function",
            file_path="app.py", line=2, end_line=6,
        ))
        g.add_cfg_block(CfgBlockFact(file_path="app.py", func_qn="mod.foo", block_id=0, is_entry=True))
        g.add_source_loc(SourceLocFact(
            file_path="app.py", loc_kind="block", loc_id="mod.foo:0", line=2, end_line=6,
        ))
        func_qn, block_id = g.resolve_location("app.py", 4)
        assert func_qn == "mod.foo"
        assert block_id == 0

    def test_resolve_location_module_level(self):
        g = FactGraph()
        g.add_symbol(SymbolFact(
            name="foo", qualified_name="mod.foo", kind="function",
            file_path="app.py", line=5, end_line=10,
        ))
        func_qn, block_id = g.resolve_location("app.py", 2)
        from emend.location_resolver import MODULE_LEVEL_BLOCK, MODULE_LEVEL_FUNC
        assert func_qn == MODULE_LEVEL_FUNC
        assert block_id == MODULE_LEVEL_BLOCK
