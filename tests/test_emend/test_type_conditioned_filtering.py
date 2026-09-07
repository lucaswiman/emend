"""Tests for Phase 4: Type-Conditioned Filtering (Taint-CFG Precision).

Tests cover:
- type_constraint field on TraceSource/TraceSink/TraceSanitizer dataclasses
- evaluate_type_constraint() boolean expression parser
- YAML config loading with type_constraint
- FactGraph type-binding storage via add_types_batch
"""

import pytest
import yaml

from emend.fact_graph import (
    FactGraph,
    TypeFact,
)
from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    evaluate_type_constraint,
    load_trace_config,
)


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
# Test: TraceSource/TraceSink/TraceSanitizer type_constraint field
# ---------------------------------------------------------------------------


class TestTypeConstraintField:
    """Tests for the type_constraint field on taint dataclasses."""

    def test_source_default_empty(self):
        src = TraceSource(pattern="foo($X)", label="lbl")
        assert src.type_constraint == ""

    def test_source_with_constraint(self):
        src = TraceSource(
            pattern="session.query($MODEL)",
            label="unlocked_read",
            type_constraint="!int & !float & !bool & !str",
        )
        assert src.type_constraint == "!int & !float & !bool & !str"

    def test_sink_default_empty(self):
        sink = TraceSink(pattern="foo($X)", label="lbl", message="msg")
        assert sink.type_constraint == ""

    def test_sink_with_constraint(self):
        sink = TraceSink(
            pattern="foo($X)", label="lbl", message="msg",
            type_constraint="!int",
        )
        assert sink.type_constraint == "!int"

    def test_sanitizer_default_empty(self):
        san = TraceSanitizer(pattern="sanitize($X)", label="lbl")
        assert san.type_constraint == ""

    def test_sanitizer_with_constraint(self):
        san = TraceSanitizer(
            pattern="sanitize($X)", label="lbl",
            type_constraint="str",
        )
        assert san.type_constraint == "str"


# ---------------------------------------------------------------------------
# Test: YAML config loading with type_constraint
# ---------------------------------------------------------------------------


class TestYamlTypeConstraintLoading:

    def test_load_source_type_constraint(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "trace": {
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
        config = load_trace_config(str(config_file))
        assert len(config.sources) == 1
        assert config.sources[0].type_constraint == "!int & !float & !bool & !str"

    def test_load_sink_type_constraint(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "trace": {
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
        config = load_trace_config(str(config_file))
        assert config.sinks[0].type_constraint == "!int"

    def test_load_sanitizer_type_constraint(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "trace": {
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
        config = load_trace_config(str(config_file))
        assert config.sanitizers[0].type_constraint == "str"

    def test_missing_type_constraint_defaults_empty(self, tmp_path):
        config_file = tmp_path / "patterns.yaml"
        config_file.write_text(yaml.dump({
            "trace": {
                "labels": ["lbl"],
                "sources": [{"pattern": "src($X)", "label": "lbl"}],
                "sinks": [{"pattern": "sink($X)", "label": "lbl", "message": "m"}],
            }
        }))
        config = load_trace_config(str(config_file))
        assert config.sources[0].type_constraint == ""
        assert config.sinks[0].type_constraint == ""


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


class TestEvaluateTypeConstraintFQNames:
    """Tests for FQ type name matching in evaluate_type_constraint."""

    def test_short_name_matches_fq_type(self):
        """'Redis' should match 'redis.client.Redis' (last component matches)."""
        assert evaluate_type_constraint("Redis", "redis.client.Redis") is True

    def test_short_name_no_match_different_suffix(self):
        """'Redis' should NOT match 'redis.client.StrictRedis' (different last component)."""
        assert evaluate_type_constraint("Redis", "redis.client.StrictRedis") is False

    def test_exact_fq_match(self):
        """Full FQ name as constraint should match exact FQ type."""
        assert evaluate_type_constraint("redis.client.Redis", "redis.client.Redis") is True

    def test_short_name_does_not_match_middle_component(self):
        """'client' should not match 'redis.client.Redis' (not the last component)."""
        assert evaluate_type_constraint("client", "redis.client.Redis") is False

    def test_negated_short_name_excludes_fq(self):
        """'!Redis' should NOT match 'redis.client.Redis'."""
        assert evaluate_type_constraint("!Redis", "redis.client.Redis") is False

    def test_negated_short_name_allows_other_fq(self):
        """'!Redis' should match 'redis.client.StrictRedis'."""
        assert evaluate_type_constraint("!Redis", "redis.client.StrictRedis") is True

    def test_simple_name_still_exact_matches_simple_type(self):
        """Simple name still matches simple type (no dots in type)."""
        assert evaluate_type_constraint("int", "int") is True
        assert evaluate_type_constraint("int", "float") is False
