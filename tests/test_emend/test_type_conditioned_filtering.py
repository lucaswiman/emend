"""Tests for Phase 4: Type-Conditioned Filtering (Taint-CFG Precision).

Tests cover:
- type_constraint field on TaintSource/TaintSink/TaintSanitizer dataclasses
- evaluate_type_constraint() boolean expression parser
- YAML config loading with type_constraint
- Datalog scalar_types parameter on taint_propagation_datalog()
- Python fallback type filtering via _filter_vars_by_type()
- build_from_project() type_binding population (via add_types_batch)
"""

import pytest
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

from emend.fact_graph import (
    CfgBlockFact,
    CfgEdgeFact,
    DefUseFact,
    FactGraph,
    SymbolFact,
    TypeFact,
)
from emend.taint import (
    TaintConfig,
    TaintSanitizer,
    TaintSink,
    TaintSource,
    evaluate_type_constraint,
    load_taint_config,
    _filter_vars_by_type,
    _has_type_constraints,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_linear_cfg(n_blocks: int = 4) -> FactGraph:
    """Build a linear CFG with n_blocks blocks: 0 -> 1 -> ... -> n-1."""
    g = FactGraph()
    blocks = []
    for i in range(n_blocks):
        blocks.append(CfgBlockFact(
            file_path="test.py", func_qn="mod.f",
            block_id=i, is_entry=(i == 0), is_exit=(i == n_blocks - 1),
        ))
    g.add_cfg_blocks_batch(blocks)
    edges = []
    for i in range(n_blocks - 1):
        edges.append(CfgEdgeFact(
            file_path="test.py", func_qn="mod.f",
            from_block=i, to_block=i + 1,
            edge_kind="fallthrough", from_line=0, to_line=0,
        ))
    g.add_cfg_edges_batch(edges)
    return g


# ---------------------------------------------------------------------------
# Test: evaluate_type_constraint
# ---------------------------------------------------------------------------


class TestEvaluateTypeConstraint:
    """Unit tests for the boolean type constraint evaluator."""

    def test_empty_constraint_always_true(self):
        assert evaluate_type_constraint("", "int") is True
        assert evaluate_type_constraint("  ", "anything") is True

    def test_bare_name_exact_match(self):
        assert evaluate_type_constraint("int", "int") is True
        assert evaluate_type_constraint("int", "float") is False

    def test_negation(self):
        assert evaluate_type_constraint("!int", "int") is False
        assert evaluate_type_constraint("!int", "float") is True
        assert evaluate_type_constraint("!int", "str") is True

    def test_conjunction(self):
        # !int & !float: true for str, false for int or float
        assert evaluate_type_constraint("!int & !float", "str") is True
        assert evaluate_type_constraint("!int & !float", "int") is False
        assert evaluate_type_constraint("!int & !float", "float") is False

    def test_disjunction(self):
        # int | float: true for int or float, false for str
        assert evaluate_type_constraint("int | float", "int") is True
        assert evaluate_type_constraint("int | float", "float") is True
        assert evaluate_type_constraint("int | float", "str") is False

    def test_and_binds_tighter_than_or(self):
        # "int | !int & !float" should be "int | (!int & !float)"
        # For int: int matches first disjunct → True
        assert evaluate_type_constraint("int | !int & !float", "int") is True
        # For float: int fails, (!int=True & !float=False) → False → False
        assert evaluate_type_constraint("int | !int & !float", "float") is False
        # For str: int fails, (!int=True & !float=True) → True → True
        assert evaluate_type_constraint("int | !int & !float", "str") is True

    def test_full_scalar_exclusion(self):
        """The canonical TOCTOU constraint: !int & !float & !bool & !str."""
        constraint = "!int & !float & !bool & !str"
        assert evaluate_type_constraint(constraint, "int") is False
        assert evaluate_type_constraint(constraint, "float") is False
        assert evaluate_type_constraint(constraint, "bool") is False
        assert evaluate_type_constraint(constraint, "str") is False
        assert evaluate_type_constraint(constraint, "Query") is True
        assert evaluate_type_constraint(constraint, "User") is True
        assert evaluate_type_constraint(constraint, "Optional") is True

    def test_top_level_constructor_only(self):
        """Constraint matches top-level name, not substrings."""
        # "int" should not match "Optional" even if Optional[int]
        assert evaluate_type_constraint("int", "Optional") is False
        assert evaluate_type_constraint("!int", "Optional") is True

    def test_whitespace_tolerance(self):
        assert evaluate_type_constraint("  !int  &  !float  ", "str") is True
        assert evaluate_type_constraint("  !int  &  !float  ", "int") is False


# ---------------------------------------------------------------------------
# Test: TaintSource/TaintSink/TaintSanitizer type_constraint field
# ---------------------------------------------------------------------------


class TestTypeConstraintField:
    """Tests for the type_constraint field on taint dataclasses."""

    def test_source_default_empty(self):
        src = TaintSource(pattern="foo($X)", label="lbl")
        assert src.type_constraint == ""

    def test_source_with_constraint(self):
        src = TaintSource(
            pattern="session.query($MODEL)",
            label="unlocked_read",
            type_constraint="!int & !float & !bool & !str",
        )
        assert src.type_constraint == "!int & !float & !bool & !str"

    def test_sink_default_empty(self):
        sink = TaintSink(pattern="foo($X)", label="lbl", message="msg")
        assert sink.type_constraint == ""

    def test_sink_with_constraint(self):
        sink = TaintSink(
            pattern="foo($X)", label="lbl", message="msg",
            type_constraint="!int",
        )
        assert sink.type_constraint == "!int"

    def test_sanitizer_default_empty(self):
        san = TaintSanitizer(pattern="sanitize($X)", label="lbl")
        assert san.type_constraint == ""

    def test_sanitizer_with_constraint(self):
        san = TaintSanitizer(
            pattern="sanitize($X)", label="lbl",
            type_constraint="str",
        )
        assert san.type_constraint == "str"


# ---------------------------------------------------------------------------
# Test: _has_type_constraints
# ---------------------------------------------------------------------------


class TestHasTypeConstraints:

    def test_no_constraints(self):
        config = TaintConfig(
            sources=[TaintSource(pattern="x", label="l")],
            sinks=[TaintSink(pattern="y", label="l", message="m")],
        )
        assert _has_type_constraints(config) is False

    def test_source_has_constraint(self):
        config = TaintConfig(
            sources=[TaintSource(pattern="x", label="l", type_constraint="!int")],
            sinks=[TaintSink(pattern="y", label="l", message="m")],
        )
        assert _has_type_constraints(config) is True

    def test_sink_has_constraint(self):
        config = TaintConfig(
            sources=[TaintSource(pattern="x", label="l")],
            sinks=[TaintSink(pattern="y", label="l", message="m", type_constraint="!int")],
        )
        assert _has_type_constraints(config) is True

    def test_sanitizer_has_constraint(self):
        config = TaintConfig(
            sanitizers=[TaintSanitizer(pattern="x", label="l", type_constraint="str")],
        )
        assert _has_type_constraints(config) is True


# ---------------------------------------------------------------------------
# Test: YAML config loading with type_constraint
# ---------------------------------------------------------------------------


class TestYamlTypeConstraintLoading:

    def test_load_source_type_constraint(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "taint": {
                "labels": ["unlocked_read"],
                "sources": [{
                    "pattern": "session.query($MODEL)",
                    "label": "unlocked_read",
                    "type_constraint": "!int & !float & !bool & !str",
                }],
                "sinks": [{
                    "pattern": "dangerous($X)",
                    "label": "unlocked_read",
                    "message": "bad",
                }],
            }
        }))
        config = load_taint_config(str(config_file))
        assert len(config.sources) == 1
        assert config.sources[0].type_constraint == "!int & !float & !bool & !str"

    def test_load_sink_type_constraint(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "taint": {
                "labels": ["lbl"],
                "sources": [{"pattern": "src($X)", "label": "lbl"}],
                "sinks": [{
                    "pattern": "sink($X)",
                    "label": "lbl",
                    "message": "bad",
                    "type_constraint": "!int",
                }],
            }
        }))
        config = load_taint_config(str(config_file))
        assert config.sinks[0].type_constraint == "!int"

    def test_load_sanitizer_type_constraint(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "taint": {
                "labels": ["lbl"],
                "sources": [{"pattern": "src($X)", "label": "lbl"}],
                "sinks": [{"pattern": "sink($X)", "label": "lbl", "message": "m"}],
                "sanitizers": [{
                    "pattern": "clean($X)",
                    "label": "lbl",
                    "type_constraint": "str",
                }],
            }
        }))
        config = load_taint_config(str(config_file))
        assert config.sanitizers[0].type_constraint == "str"

    def test_missing_type_constraint_defaults_empty(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "taint": {
                "labels": ["lbl"],
                "sources": [{"pattern": "src($X)", "label": "lbl"}],
                "sinks": [{"pattern": "sink($X)", "label": "lbl", "message": "m"}],
            }
        }))
        config = load_taint_config(str(config_file))
        assert config.sources[0].type_constraint == ""
        assert config.sinks[0].type_constraint == ""


# ---------------------------------------------------------------------------
# Test: Datalog scalar_types parameter
# ---------------------------------------------------------------------------


class TestDatalogScalarTypes:
    """Tests for the scalar_types parameter on taint_propagation_datalog()."""

    def test_scalar_type_filters_source(self):
        """Source with scalar type should not propagate taint."""
        g = _build_linear_cfg(3)

        # Add symbol
        g.add_symbols_batch([SymbolFact(
            file_path="test.py", name="f", qualified_name="mod.f",
            kind="function", line=1, end_line=10, parent=None,
        )])

        # Def-use: x defined in block 0, used in block 2
        g.add_def_uses_batch([DefUseFact(
            file_path="test.py", func_qn="mod.f",
            var_name="x", kind="write",
            def_block=0, use_block=2,
            def_line=2, def_col=0, use_line=5, use_col=0,
        )])

        # Add type binding: x is int at line 2
        g.add_type(TypeFact(
            symbol_qn="x", type_str="int",
            file_path="test.py", line=2, binding_kind="definition",
        ))

        # Source at block 0, sink at block 2
        sources = [("test.py", "mod.f", "x", 0, "lbl")]
        sinks = [("test.py", "mod.f", "x", 2, "lbl")]

        # Without scalar_types: should find violation
        violations = g.taint_propagation_datalog(sources=sources, sinks=sinks)
        assert len(violations) == 1

        # With scalar_types including "int": should filter out the source
        violations = g.taint_propagation_datalog(
            sources=sources, sinks=sinks,
            scalar_types=["int", "float", "bool", "str"],
        )
        assert len(violations) == 0

    def test_non_scalar_type_not_filtered(self):
        """Source with non-scalar type should still propagate taint."""
        g = _build_linear_cfg(3)

        g.add_symbols_batch([SymbolFact(
            file_path="test.py", name="f", qualified_name="mod.f",
            kind="function", line=1, end_line=10, parent=None,
        )])

        g.add_def_uses_batch([DefUseFact(
            file_path="test.py", func_qn="mod.f",
            var_name="result", kind="write",
            def_block=0, use_block=2,
            def_line=2, def_col=0, use_line=5, use_col=0,
        )])

        # Type binding: result is Query (not a scalar)
        g.add_type(TypeFact(
            symbol_qn="result", type_str="Query",
            file_path="test.py", line=2, binding_kind="definition",
        ))

        sources = [("test.py", "mod.f", "result", 0, "lbl")]
        sinks = [("test.py", "mod.f", "result", 2, "lbl")]

        # With scalar_types: Query is NOT a scalar, should still find violation
        violations = g.taint_propagation_datalog(
            sources=sources, sinks=sinks,
            scalar_types=["int", "float", "bool", "str"],
        )
        assert len(violations) == 1

    def test_no_type_binding_not_filtered(self):
        """Source without type binding should not be filtered (conservative)."""
        g = _build_linear_cfg(3)

        g.add_symbols_batch([SymbolFact(
            file_path="test.py", name="f", qualified_name="mod.f",
            kind="function", line=1, end_line=10, parent=None,
        )])

        g.add_def_uses_batch([DefUseFact(
            file_path="test.py", func_qn="mod.f",
            var_name="x", kind="write",
            def_block=0, use_block=2,
            def_line=2, def_col=0, use_line=5, use_col=0,
        )])

        # No type binding for x

        sources = [("test.py", "mod.f", "x", 0, "lbl")]
        sinks = [("test.py", "mod.f", "x", 2, "lbl")]

        # With scalar_types but no type binding: should still find violation
        violations = g.taint_propagation_datalog(
            sources=sources, sinks=sinks,
            scalar_types=["int", "float", "bool", "str"],
        )
        assert len(violations) == 1

    def test_scalar_types_with_effect_sinks(self):
        """Scalar type filtering works with effect-based sinks too."""
        g = _build_linear_cfg(3)

        g.add_symbols_batch([SymbolFact(
            file_path="test.py", name="f", qualified_name="mod.f",
            kind="function", line=1, end_line=10, parent=None,
        )])

        # x defined in block 0, written in block 2
        g.add_def_uses_batch([
            DefUseFact(
                file_path="test.py", func_qn="mod.f",
                var_name="x", kind="write",
                def_block=0, use_block=2,
                def_line=2, def_col=0, use_line=5, use_col=0,
            ),
            DefUseFact(
                file_path="test.py", func_qn="mod.f",
                var_name="x", kind="write",
                def_block=2, use_block=2,
                def_line=5, def_col=0, use_line=5, use_col=0,
            ),
        ])

        g.add_type(TypeFact(
            symbol_qn="x", type_str="int",
            file_path="test.py", line=2, binding_kind="definition",
        ))

        sources = [("test.py", "mod.f", "x", 0, "toctou")]

        # With scalar_types and effect sinks: scalar should be filtered
        violations = g.taint_propagation_datalog(
            sources=sources,
            effect_sinks=[("toctou", "writes")],
            scalar_types=["int", "float", "bool", "str"],
        )
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Test: add_types_batch
# ---------------------------------------------------------------------------


class TestAddTypesBatch:

    def test_add_types_batch_basic(self):
        g = FactGraph()
        facts = [
            TypeFact(symbol_qn="x", type_str="int", file_path="f.py", line=1, binding_kind="definition"),
            TypeFact(symbol_qn="y", type_str="str", file_path="f.py", line=2, binding_kind="definition"),
        ]
        g.add_types_batch(facts)

        # Verify via types_for query
        result = g.types_for("x")
        assert len(result) == 1
        assert result[0].type_str == "int"

        result = g.types_for("y")
        assert len(result) == 1
        assert result[0].type_str == "str"

    def test_add_types_batch_empty(self):
        g = FactGraph()
        g.add_types_batch([])  # Should not raise


# ---------------------------------------------------------------------------
# Test: _filter_vars_by_type (Python fallback helper)
# ---------------------------------------------------------------------------


class TestFilterVarsByType:

    def _make_mock_oracle(self, bindings):
        """Create a mock type oracle returning the given bindings."""
        from emend.type_oracle import FileTypes, TypeBinding, TypeDescriptor

        ft = FileTypes(path="test.py")
        for name, line, raw_type in bindings:
            td = TypeDescriptor.named(raw_type) if raw_type != "Unknown" else TypeDescriptor.unknown()
            ft.bindings.append(TypeBinding(
                name=name, line=line, col_start=0, col_end=None,
                type_descriptor=td, raw_type=raw_type,
                binding_kind="definition",
            ))

        oracle = MagicMock()
        oracle.infer_file.return_value = ft
        return oracle

    def test_filters_scalar_vars(self):
        oracle = self._make_mock_oracle([
            ("x", 2, "int"),
            ("y", 3, "Query"),
        ])
        result = _filter_vars_by_type(
            {"x", "y"}, "!int & !float & !bool & !str",
            oracle, "test.py", 5,
        )
        assert result == {"y"}

    def test_keeps_unknown_type(self):
        """Variables with unknown types should be kept (conservative)."""
        oracle = self._make_mock_oracle([])
        result = _filter_vars_by_type(
            {"x"}, "!int", oracle, "test.py", 5,
        )
        assert result == {"x"}

    def test_oracle_failure_keeps_all(self):
        """If oracle fails, all variables should be kept."""
        oracle = MagicMock()
        oracle.infer_file.side_effect = Exception("no type checker")
        result = _filter_vars_by_type(
            {"x", "y"}, "!int", oracle, "test.py", 5,
        )
        assert result == {"x", "y"}

    def test_selects_nearest_binding(self):
        """Should select the binding closest to (at or before) the match line."""
        oracle = self._make_mock_oracle([
            ("x", 2, "str"),   # Early definition
            ("x", 8, "int"),   # Later redefinition
        ])
        # Match at line 5: should use the binding at line 2 (str)
        result = _filter_vars_by_type(
            {"x"}, "!int", oracle, "test.py", 5,
        )
        assert result == {"x"}  # str satisfies !int

        # Match at line 10: should use the binding at line 8 (int)
        result = _filter_vars_by_type(
            {"x"}, "!int", oracle, "test.py", 10,
        )
        assert result == set()  # int does NOT satisfy !int

    def test_parameterized_type_uses_top_level(self):
        """Optional[int] should have top-level name 'Optional', not 'int'."""
        oracle = self._make_mock_oracle([
            ("x", 2, "Optional[int]"),
        ])
        # !int should pass because top-level is Optional, not int
        result = _filter_vars_by_type(
            {"x"}, "!int", oracle, "test.py", 5,
        )
        assert result == {"x"}
