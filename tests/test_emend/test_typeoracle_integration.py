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
    compile_pattern_to_rust_ir,
    is_oracle_type_constraint,
    parse_oracle_type_constraint,
    parse_pattern,
)
from emend.type_oracle import (
    FileTypes,
    TypeBinding,
    TypeDescriptor,
    TypeOracle,
    create_type_oracle,
)


# ---------------------------------------------------------------------------
# Pattern grammar: :type[X] and :returns[X] parsing
# ---------------------------------------------------------------------------

class TestOracleTypeConstraintParsing:
    """Test that :type[X] and :returns[X] are parsed correctly by the grammar."""

    @pytest.mark.parametrize(
        "pattern, name, constraint",
        [
            pytest.param("$X:type[Connection]", "X", "type[Connection]", id="type"),
            pytest.param("$F:returns[str]", "F", "returns[str]", id="returns"),
            pytest.param("$X:type[Optional[str]]", "X", "type[Optional[str]]", id="nested"),
            pytest.param("$X:type[list[int]]", "X", "type[list[int]]", id="parameterized"),
            pytest.param("$X:type[dict[str, int]]", "X", "type[dict[str, int]]", id="multiple-params"),
        ],
    )
    def test_oracle_constraint(self, pattern, name, constraint):
        pat = parse_pattern(pattern)
        assert [(mv.name, mv.type_constraint) for mv in pat.metavars] == [
            (name, constraint)
        ]

    def test_mixed_constraints(self):
        """Oracle constraint + normal constraint in same pattern."""
        pat = parse_pattern("$F($X:type[bytes], $Y:int)")
        assert len(pat.metavars) == 3
        mv_by_name = {mv.name: mv for mv in pat.metavars}
        assert mv_by_name["F"].type_constraint is None
        assert mv_by_name["X"].type_constraint == "type[bytes]"
        assert mv_by_name["Y"].type_constraint == "int"

    @pytest.mark.parametrize(
        "constraint", ["int", "str", "float", "identifier", "call", "attr", "stmt", "expr", "any"]
    )
    def test_regular_constraints_still_work(self, constraint):
        assert parse_pattern(f"$X:{constraint}").metavars[0].type_constraint == constraint

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

    @pytest.mark.parametrize(
        "constraint, expected",
        [
            pytest.param("type[Connection]", True, id="type"),
            pytest.param("returns[str]", True, id="returns"),
            pytest.param("type[list[int]]", True, id="nested"),
            pytest.param("int", False, id="builtin"),
            pytest.param("str", False, id="string"),
            pytest.param(None, False, id="none"),
            pytest.param("expr", False, id="structural"),
        ],
    )
    def test_is_oracle_constraint(self, constraint, expected):
        assert is_oracle_type_constraint(constraint) is expected

    @pytest.mark.parametrize(
        "constraint, expected",
        [
            pytest.param("type[Connection]", ("type", "Connection"), id="type"),
            pytest.param("returns[Optional[str]]", ("returns", "Optional[str]"), id="returns-nested"),
            pytest.param("type[dict[str, int]]", ("type", "dict[str, int]"), id="type-multiple-params"),
        ],
    )
    def test_parse_oracle_constraint(self, constraint, expected):
        assert parse_oracle_type_constraint(constraint) == expected


class TestOracleConstraintCompilation:
    """Test that oracle constraints compile to Rust IR (oracle metavars become metavars)."""

    @pytest.mark.parametrize("pattern", [
        "$X:type[Connection]",
        "$F:returns[str]",
        "$F($X:type[bytes], $Y:int)",
    ])
    def test_oracle_constraint_compiles(self, pattern):
        # Oracle constraints are resolved post-match, so the metavar compiles
        # through to a plain metavar in the IR rather than a simple constraint.
        assert compile_pattern_to_rust_ir(pattern) is not None


# ---------------------------------------------------------------------------
# Simple TypeOracle adapter wrapping manually built FileTypes
# ---------------------------------------------------------------------------

class _SimpleOracle(TypeOracle):
    """Lightweight TypeOracle that wraps pre-built FileTypes by path.

    Subclasses the real TypeOracle ABC so that interface changes (e.g.
    renamed or added abstract methods) cause test failures immediately.
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

    def type_at(self, path: Path, line: int, col: int,
                project_root: Path | None = None) -> TypeBinding | None:
        ft = self.infer_file(path, project_root)
        return ft.type_at(line, col)

    def clear_cache(self) -> None:
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

        oracle = create_type_oracle(engine="pyright", project_root=tmp_path)
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
        with caplog.at_level(logging.DEBUG, logger="emend.type_oracle"):
            ft.build_index()

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
# cmd_edit/cmd_add smoke tests
# ---------------------------------------------------------------------------

class TestCmdEditAddSmoke:
    """Smoke tests for cmd_edit and cmd_add (no type_oracle wiring needed)."""

    def test_cmd_edit_works(self, tmp_path):
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
        )
        assert "int" in result

    def test_cmd_add_works(self, tmp_path):
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
        )
        assert "age" in result


# ---------------------------------------------------------------------------
# replace_pattern with TypeOracle
# ---------------------------------------------------------------------------

class TestReplacePatternTypeOracle:
    """Test that replace_pattern uses TypeOracle for :type[X] constraints."""

    def test_type_constraint_filters_replacements(self, tmp_path):
        """Only replace matches where the captured variable has the right inferred type."""
        source = textwrap.dedent("""\
            x = get_connection()
            y = get_name()
            x.close()
            y.close()
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
            # Bindings for usage sites (x.close(), y.close())
            TypeBinding(
                name="x", line=3, col_start=1, col_end=2,
                type_descriptor=TypeDescriptor.named("Connection"),
                raw_type="Connection", binding_kind="inferred",
            ),
            TypeBinding(
                name="y", line=4, col_start=1, col_end=2,
                type_descriptor=TypeDescriptor.named("str"),
                raw_type="str", binding_kind="inferred",
            ),
        ])
        oracle = _SimpleOracle({str(f): ft})

        from emend.transform import replace_pattern

        # Without oracle: both .close() calls match
        diff_no_oracle, count_no_oracle = replace_pattern(
            "$X.close()", "$X.shutdown()", str(f),
        )
        assert count_no_oracle == 2

        # With oracle + type constraint: only x (Connection) matches
        diff_oracle, count_oracle = replace_pattern(
            "$X:type[Connection].close()", "$X.shutdown()", str(f),
            type_oracle=oracle,
        )
        assert count_oracle == 1
        assert "x.shutdown()" in diff_oracle
        assert "y.shutdown()" not in diff_oracle

    def test_replace_no_oracle_constraints_unchanged(self, tmp_path):
        """Patterns without oracle constraints work as before (no regression)."""
        source = textwrap.dedent("""\
            print("hello")
            print("world")
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.transform import replace_pattern

        diff, count = replace_pattern(
            'print($X)', 'log($X)', str(f),
        )
        assert count == 2

    def test_replace_oracle_no_matches(self, tmp_path):
        """When oracle filters out all matches, no replacement occurs."""
        source = textwrap.dedent("""\
            x = get_name()
            x.close()
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        ft = _build_file_types(str(f), [
            TypeBinding(
                name="x", line=1, col_start=1, col_end=2,
                type_descriptor=TypeDescriptor.named("str"),
                raw_type="str", binding_kind="definition",
            ),
            TypeBinding(
                name="x", line=2, col_start=1, col_end=2,
                type_descriptor=TypeDescriptor.named("str"),
                raw_type="str", binding_kind="inferred",
            ),
        ])
        oracle = _SimpleOracle({str(f): ft})

        from emend.transform import replace_pattern

        diff, count = replace_pattern(
            "$X:type[Connection].close()", "$X.shutdown()", str(f),
            type_oracle=oracle,
        )
        assert count == 0
        assert diff == ""


# ---------------------------------------------------------------------------
# cmd_edit with returns_filter + TypeOracle
# ---------------------------------------------------------------------------

class TestCmdEditReturnsFilter:
    """Test cmd_edit with --returns filter for type-aware editing."""

    # The --returns flag and the :returns[str] selector are two input styles
    # for the same filter; both must produce identical edits.
    _RETURNS_STR_STYLES = [
        ("*[returns]", {"returns_filter": ["str"]}),
        ("*:returns[str][returns]", {}),
    ]

    @pytest.mark.parametrize("selector_suffix, extra_kwargs", _RETURNS_STR_STYLES)
    def test_edit_filters_by_annotation(self, tmp_path, selector_suffix, extra_kwargs):
        """Functions with matching return annotation are edited; others are skipped."""
        source = textwrap.dedent("""\
            def get_name() -> str:
                return "alice"

            def get_count() -> int:
                return 42
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.transform import cmd_edit

        result = cmd_edit(
            selector_str=f"{f}::{selector_suffix}",
            value="str | None",
            **extra_kwargs,
        )
        # Only get_name (returning str) should be edited
        assert "str | None" in result
        # get_name's return type was changed (appears in diff as modified line)
        assert "-def get_name() -> str:" in result
        assert "+def get_name() -> str | None:" in result
        # get_count's return type was NOT changed (no diff line for it)
        assert "-def get_count()" not in result
        assert "+def get_count()" not in result

    @pytest.mark.parametrize("selector_suffix, extra_kwargs", _RETURNS_STR_STYLES)
    def test_edit_filters_by_oracle(self, tmp_path, selector_suffix, extra_kwargs):
        """Functions without annotations are filtered by inferred return type."""
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

        from emend.transform import cmd_edit

        # Add return annotation to functions returning str (inferred)
        result = cmd_edit(
            selector_str=f"{f}::{selector_suffix}",
            value="str",
            type_oracle=oracle,
            **extra_kwargs,
        )
        # get_name's return type was changed
        assert "-def get_name():" in result
        assert "+def get_name() -> str:" in result
        # get_count was NOT changed
        assert "-def get_count()" not in result
        assert "+def get_count()" not in result


# ---------------------------------------------------------------------------
# cmd_add with returns_filter + TypeOracle
# ---------------------------------------------------------------------------

class TestCmdAddReturnsFilter:
    """Test cmd_add with --returns filter for type-aware parameter insertion."""

    @pytest.mark.parametrize("selector_suffix, extra_kwargs", [
        ("*[params]", {"returns_filter": ["Connection"]}),
        ("*:returns[Connection][params]", {}),
    ])
    def test_add_filters_by_annotation(self, tmp_path, selector_suffix, extra_kwargs):
        """Only add parameter to functions whose return type matches — via the
        --returns flag or the :returns[Connection] selector."""
        source = textwrap.dedent("""\
            def connect() -> Connection:
                pass

            def get_name() -> str:
                return "alice"
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        from emend.transform import cmd_add

        result = cmd_add(
            selector_str=f"{f}::{selector_suffix}",
            value="timeout: int = 30",
            **extra_kwargs,
        )
        # Only connect (returning Connection) should get the new param
        assert "timeout" in result
        # connect was changed (timeout added)
        assert "+def connect(timeout: int = 30)" in result
        # get_name was NOT changed
        assert "+def get_name(" not in result

    def test_add_filters_by_oracle(self, tmp_path):
        """Functions without annotations are filtered by inferred return type."""
        source = textwrap.dedent("""\
            def connect():
                pass

            def get_name():
                return "alice"
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        ft = _build_file_types(str(f), [
            TypeBinding(
                name="connect", line=1, col_start=5, col_end=12,
                type_descriptor=TypeDescriptor.callable_((), TypeDescriptor.named("Connection")),
                raw_type="() -> Connection", binding_kind="definition",
            ),
            TypeBinding(
                name="get_name", line=4, col_start=5, col_end=13,
                type_descriptor=TypeDescriptor.callable_((), TypeDescriptor.named("str")),
                raw_type="() -> str", binding_kind="definition",
            ),
        ])
        oracle = _SimpleOracle({str(f): ft})

        from emend.transform import cmd_add

        result = cmd_add(
            selector_str=f"{f}::*[params]",
            value="timeout: int = 30",
            returns_filter=["Connection"],
            type_oracle=oracle,
        )
        assert "timeout" in result
        # connect was changed (timeout added)
        assert "+def connect(timeout: int = 30)" in result
        # get_name was NOT changed
        assert "+def get_name(" not in result


# ---------------------------------------------------------------------------
# Selector type_filter syntax (:returns[X], :type[X])
# ---------------------------------------------------------------------------

class TestSelectorTypeFilter:
    """Test :returns[X] and :type[X] in selector syntax."""

    def test_parse_returns_filter(self):
        """Selector grammar parses :returns[str] correctly."""
        from emend.component_selector import parse_extended_selector

        sel = parse_extended_selector("file.py::*:returns[str][params]")
        assert sel.symbol_path == ["*"]
        assert sel.type_filter == "returns[str]"
        assert sel.component == "params"

    def test_parse_type_filter(self):
        """Selector grammar parses :type[Connection] correctly."""
        from emend.component_selector import parse_extended_selector

        sel = parse_extended_selector("file.py::*:type[Connection]")
        assert sel.type_filter == "type[Connection]"
        assert sel.component is None

    def test_parse_nested_type(self):
        """Selector grammar handles nested brackets like Optional[str]."""
        from emend.component_selector import parse_extended_selector

        sel = parse_extended_selector("file.py::*:returns[Optional[str]][params]")
        assert sel.type_filter == "returns[Optional[str]]"
        assert sel.component == "params"

    def test_parse_no_filter(self):
        """Selectors without type filter have type_filter=None."""
        from emend.component_selector import parse_extended_selector

        sel = parse_extended_selector("file.py::func[params]")
        assert sel.type_filter is None

    # The cmd_edit/cmd_add :returns[...] selector paths are exercised alongside
    # the --returns flag by the parametrized tests in TestCmdEditReturnsFilter
    # and TestCmdAddReturnsFilter.
