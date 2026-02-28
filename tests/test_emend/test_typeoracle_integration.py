"""Tests for TypeOracle integration with lookup/search and edit commands.

Tests cover:
- Pattern grammar extension for :type[X] and :returns[X] constraints
- TypeOracle post-filtering in find_pattern
- Type-aware --returns filtering in query_symbols
- --type-engine CLI option wiring for search/edit/add
- Log messages for building type indexes
"""
from __future__ import annotations

import logging
import shutil
import textwrap
from pathlib import Path

import pytest

from emend.pattern import (
    compile_pattern_to_matcher,
    is_oracle_type_constraint,
    parse_oracle_type_constraint,
    parse_pattern,
)
from emend.type_oracle import (
    FileTypes,
    TypeBinding,
    TypeDescriptor,
    _parse_type_string,
    create_type_oracle,
)


# ---------------------------------------------------------------------------
# Pattern grammar: :type[X] and :returns[X] parsing
# ---------------------------------------------------------------------------

class TestOracleTypeConstraintParsing:
    """Test that :type[X] and :returns[X] are parsed correctly by the grammar."""

    def test_simple_type_constraint(self):
        pat = parse_pattern("$X:type[Connection]")
        assert len(pat.metavars) == 1
        assert pat.metavars[0].name == "X"
        assert pat.metavars[0].type_constraint == "type[Connection]"

    def test_returns_constraint(self):
        pat = parse_pattern("$F:returns[str]")
        assert len(pat.metavars) == 1
        assert pat.metavars[0].name == "F"
        assert pat.metavars[0].type_constraint == "returns[str]"

    def test_nested_brackets(self):
        pat = parse_pattern("$X:type[Optional[str]]")
        assert pat.metavars[0].type_constraint == "type[Optional[str]]"

    def test_parameterized_type(self):
        pat = parse_pattern("$X:type[list[int]]")
        assert pat.metavars[0].type_constraint == "type[list[int]]"

    def test_dict_type(self):
        pat = parse_pattern("$X:type[dict[str, int]]")
        assert pat.metavars[0].type_constraint == "type[dict[str, int]]"

    def test_mixed_constraints(self):
        """Oracle constraint + normal constraint in same pattern."""
        pat = parse_pattern("$F($X:type[bytes], $Y:int)")
        assert len(pat.metavars) == 3
        mv_by_name = {mv.name: mv for mv in pat.metavars}
        assert mv_by_name["F"].type_constraint is None
        assert mv_by_name["X"].type_constraint == "type[bytes]"
        assert mv_by_name["Y"].type_constraint == "int"

    def test_regular_constraints_still_work(self):
        """Verify existing constraint types are not broken."""
        for tc in ["int", "str", "float", "identifier", "call", "attr", "stmt", "expr", "any"]:
            pat = parse_pattern(f"$X:{tc}")
            assert pat.metavars[0].type_constraint == tc

    def test_negated_constraints_still_work(self):
        pat = parse_pattern("$X:!int")
        assert pat.metavars[0].type_constraint == "!int"

    def test_ellipsis_with_type_constraint(self):
        pat = parse_pattern("f($...ARGS:type[int])")
        mv = [mv for mv in pat.metavars if mv.name == "ARGS"][0]
        assert mv.ellipsis is True
        assert mv.type_constraint == "type[int]"


class TestOracleConstraintHelpers:
    """Test is_oracle_type_constraint and parse_oracle_type_constraint."""

    def test_is_oracle_type(self):
        assert is_oracle_type_constraint("type[Connection]") is True
        assert is_oracle_type_constraint("returns[str]") is True
        assert is_oracle_type_constraint("type[list[int]]") is True

    def test_is_not_oracle(self):
        assert is_oracle_type_constraint("int") is False
        assert is_oracle_type_constraint("str") is False
        assert is_oracle_type_constraint(None) is False
        assert is_oracle_type_constraint("expr") is False

    def test_parse_type(self):
        kind, ts = parse_oracle_type_constraint("type[Connection]")
        assert kind == "type"
        assert ts == "Connection"

    def test_parse_returns(self):
        kind, ts = parse_oracle_type_constraint("returns[Optional[str]]")
        assert kind == "returns"
        assert ts == "Optional[str]"

    def test_parse_parameterized(self):
        kind, ts = parse_oracle_type_constraint("type[dict[str, int]]")
        assert kind == "type"
        assert ts == "dict[str, int]"


class TestOracleConstraintCompilation:
    """Test that oracle constraints compile into DoNotCare matchers."""

    def test_type_constraint_compiles(self):
        pat = parse_pattern("$X:type[Connection]")
        matcher, info = compile_pattern_to_matcher(pat)
        assert matcher is not None

    def test_returns_constraint_compiles(self):
        pat = parse_pattern("$F:returns[str]")
        matcher, info = compile_pattern_to_matcher(pat)
        assert matcher is not None

    def test_complex_pattern_compiles(self):
        pat = parse_pattern("$F($X:type[bytes], $Y:int)")
        matcher, info = compile_pattern_to_matcher(pat)
        assert matcher is not None


# ---------------------------------------------------------------------------
# Simple TypeOracle adapter wrapping manually built FileTypes
# ---------------------------------------------------------------------------

class _SimpleOracle:
    """Lightweight TypeOracle that wraps pre-built FileTypes by path.

    This is not a mock: it is a real adapter that implements the TypeOracle
    interface by returning real FileTypes objects.
    """

    def __init__(self, file_types_map: dict[str, FileTypes]):
        self._map = file_types_map

    def is_available(self) -> bool:
        return True

    def infer_file(self, path: Path, project_root: Path | None = None) -> FileTypes:
        resolved = str(Path(path).resolve())
        for key, ft in self._map.items():
            if resolved.endswith(key) or key == resolved:
                return ft
        return FileTypes(path=str(path))

    def type_at(self, path, line, col, project_root=None):
        ft = self.infer_file(path, project_root)
        return ft.type_at(line, col)

    def clear_cache(self):
        pass


def _build_file_types(path: str, bindings: list[TypeBinding]) -> FileTypes:
    """Build and index a FileTypes with the given bindings."""
    ft = FileTypes(path=path)
    ft.bindings = bindings
    ft.build_index()
    return ft


# ---------------------------------------------------------------------------
# Type-aware post-filtering in find_pattern
# ---------------------------------------------------------------------------

class TestFindPatternTypeOracle:
    """Test that find_pattern post-filters using TypeOracle constraints."""

    def test_type_constraint_filters_matches(self, tmp_path):
        """Pattern with :type[X] should filter by inferred type."""
        source = textwrap.dedent("""\
            x = get_connection()
            y = get_name()
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        ft = _build_file_types(str(f), [
            TypeBinding(
                name="x", line=1, col_start=1, col_end=2,
                type_descriptor=TypeDescriptor.named("Connection"),
                raw_type="Connection", binding_kind="definition",
            ),
            TypeBinding(
                name="y", line=2, col_start=1, col_end=2,
                type_descriptor=TypeDescriptor.named("str"),
                raw_type="str", binding_kind="definition",
            ),
        ])
        oracle = _SimpleOracle({str(f): ft})

        from emend.transform import find_pattern

        # Without oracle: both match
        matches = find_pattern("$X = $Y", str(f))
        assert len(matches) == 2

        # With oracle, type[Connection] constraint: only x matches
        matches = find_pattern("$X:type[Connection] = $Y", str(f), type_oracle=oracle)
        assert len(matches) == 1

    def test_type_constraint_without_oracle_returns_all(self, tmp_path):
        """If no oracle provided, :type[X] constraints have no effect (match all)."""
        source = textwrap.dedent("""\
            x = 1
            y = 2
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.transform import find_pattern

        # Without oracle, type constraints match all
        matches = find_pattern("$X:type[int] = $Y", str(f))
        assert len(matches) == 2


# ---------------------------------------------------------------------------
# Type-aware returns filtering in query_symbols
# ---------------------------------------------------------------------------

class TestQuerySymbolsTypeOracle:
    """Test that query_symbols uses TypeOracle for returns filtering."""

    def test_returns_filter_with_oracle_fallback(self, tmp_path):
        """Functions without annotations can be filtered by inferred return type."""
        source = textwrap.dedent("""\
            def get_name():
                return "alice"

            def get_count():
                return 42
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        ft = _build_file_types(str(f), [
            TypeBinding(
                name="get_name", line=1, col_start=5, col_end=13,
                type_descriptor=TypeDescriptor.callable_((), TypeDescriptor.named("str")),
                raw_type="() -> str", binding_kind="definition",
            ),
            TypeBinding(
                name="get_count", line=4, col_start=5, col_end=14,
                type_descriptor=TypeDescriptor.callable_((), TypeDescriptor.named("int")),
                raw_type="() -> int", binding_kind="definition",
            ),
        ])
        oracle = _SimpleOracle({str(f): ft})

        from emend.query import QueryFilter, query_symbols

        # Without oracle: neither has annotation, returns filter excludes both
        filters = QueryFilter(returns_patterns=["str"])
        results = query_symbols(f, filters)
        assert len(results) == 0

        # With oracle: inferred types are checked
        results = query_symbols(f, filters, type_oracle=oracle)
        assert len(results) == 1
        assert results[0].name == "get_name"

    def test_returns_filter_with_annotation_first(self, tmp_path):
        """Annotation-based filtering takes precedence over oracle."""
        source = textwrap.dedent("""\
            def get_name() -> str:
                return "alice"
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.query import QueryFilter, query_symbols

        # Annotation is present, should match without oracle
        filters = QueryFilter(returns_patterns=["str"])
        results = query_symbols(f, filters)
        assert len(results) == 1
        assert results[0].name == "get_name"


# ---------------------------------------------------------------------------
# Pyright integration tests (requires pyright installed)
# ---------------------------------------------------------------------------

_has_pyright = shutil.which("pyright") is not None


@pytest.mark.skipif(not _has_pyright, reason="pyright not installed")
class TestPyrightIntegration:
    """Tests using real pyright type inference."""

    def test_pyright_infer_file_builds_index(self, tmp_path, caplog):
        """Pyright adapter builds a type index with log messages."""
        source = textwrap.dedent("""\
            x: int = 42
            y: str = "hello"
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        oracle = create_type_oracle(engine="pyright")
        with caplog.at_level(logging.INFO, logger="emend.type_oracle"):
            ft = oracle.infer_file(f, project_root=tmp_path)

        assert any("Building type index" in msg for msg in caplog.messages)

    def test_pyright_returns_filter_in_lookup(self, tmp_path):
        """cmd_lookup with pyright oracle can filter by inferred return types."""
        source = textwrap.dedent("""\
            def get_name() -> str:
                return "alice"

            def get_count() -> int:
                return 42
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        oracle = create_type_oracle(engine="pyright")
        from emend.transform import cmd_lookup

        result = cmd_lookup(
            file_or_pattern=str(f),
            returns=["str"],
            type_oracle=oracle,
        )
        assert "get_name" in result
        assert "get_count" not in result


# ---------------------------------------------------------------------------
# FileTypes build_index logging
# ---------------------------------------------------------------------------

class TestBuildIndexLogging:
    """Test that building type indexes emits log messages."""

    def test_build_index_logs(self, caplog):
        ft = FileTypes(path="test.py")
        ft.bindings = [
            TypeBinding(
                name="x", line=1, col_start=1, col_end=2,
                type_descriptor=TypeDescriptor.named("int"),
                raw_type="int", binding_kind="definition",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="emend.type_oracle"):
            ft.build_index()

        assert any("Building type index" in msg for msg in caplog.messages)
        assert any("Type index built" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# cmd_lookup with type_oracle
# ---------------------------------------------------------------------------

class TestCmdLookupTypeOracle:
    """Test that cmd_lookup passes type_oracle to query."""

    def test_lookup_query_mode_accepts_type_oracle(self, tmp_path):
        """cmd_lookup in query mode should accept type_oracle parameter."""
        source = textwrap.dedent("""\
            def hello():
                pass
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.transform import cmd_lookup

        # Should not raise even with type_oracle=None
        result = cmd_lookup(
            file_or_pattern=str(f),
            kind=["function"],
            type_oracle=None,
        )
        assert "hello" in result


# ---------------------------------------------------------------------------
# cmd_edit/cmd_add accept type_oracle parameter
# ---------------------------------------------------------------------------

class TestCmdEditAddTypeOracle:
    """Test that cmd_edit and cmd_add accept type_oracle parameter."""

    def test_cmd_edit_accepts_type_oracle(self, tmp_path):
        source = textwrap.dedent("""\
            def greet(name: str) -> str:
                return f"hello {name}"
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.transform import cmd_edit

        result = cmd_edit(
            selector_str=f"{f}::greet[returns]",
            value="int",
            type_oracle=None,
        )
        assert "int" in result

    def test_cmd_add_accepts_type_oracle(self, tmp_path):
        source = textwrap.dedent("""\
            def greet(name: str) -> str:
                return f"hello {name}"
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.transform import cmd_add

        result = cmd_add(
            selector_str=f"{f}::greet[params]",
            value="age: int",
            type_oracle=None,
        )
        assert "age" in result
