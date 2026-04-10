"""Tests for Phase 1: Effect Predicates (Taint-CFG Precision).

Tests cover:
- `kind` field on DefUseFact and CozoDB def_use schema
- MethodCallFact dataclass and method_call CozoDB relation
- Effect-based sinks in trace_propagation_datalog()
- Augmented assignment detection via DefUseFact (tree-sitter backed)
- is_var_or_attr Datalog pattern (dotted-name prefix matching)
- Effect sinks replace the old attribute_mutation_sinks mechanism
"""

import pytest
import yaml

from emend.fact_graph import (
    CfgBlockFact,
    CfgEdgeFact,
    DefUseFact,
    FactGraph,
    MethodCallFact,
    SymbolFact,
)
from emend.trace import (
    TraceConfig,
    TraceSink,
    TraceSource,
    load_trace_config,
    run_trace_analysis,
)


# ---------------------------------------------------------------------------
# DefUseFact.kind field
# ---------------------------------------------------------------------------


class TestDefUseKind:
    """DefUseFact has a `kind` field stored in CozoDB."""

    def test_def_use_fact_has_kind(self):
        """DefUseFact accepts a `kind` keyword argument."""
        du = DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        )
        assert du.kind == "write"

    def test_def_use_fact_kind_default(self):
        """DefUseFact.kind defaults to 'write' for backwards compatibility."""
        du = DefUseFact("app.py", "app.main", "x", def_block=0, use_block=1)
        assert du.kind == "write"

    def test_add_and_query_def_use_with_kind(self):
        """def_use CozoDB relation stores and retrieves `kind`."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="read",
            def_block=0, use_block=1,
            use_line=5,
        ))
        results = g.def_uses(func_qn="app.main")
        kinds = {d.kind for d in results}
        assert "write" in kinds
        assert "read" in kinds

    def test_def_use_kind_aug_write(self):
        """Augmented assignment emits kind='aug_write'."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="aug_write",
            def_block=0, use_block=0,
        ))
        results = g.def_uses(func_qn="app.main")
        assert any(d.kind == "aug_write" for d in results)

    def test_def_use_kind_del(self):
        """Delete statement emits kind='del'."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="del",
            def_block=0, use_block=0,
        ))
        results = g.def_uses(func_qn="app.main")
        assert any(d.kind == "del" for d in results)

    def test_def_use_batch_with_kind(self):
        """add_def_uses_batch preserves kind."""
        g = FactGraph()
        facts = [
            DefUseFact("a.py", "a.f", "x", kind="write", def_block=0, use_block=1),
            DefUseFact("a.py", "a.f", "x", kind="read", def_block=1, use_block=2),
            DefUseFact("a.py", "a.f", "y", kind="aug_write", def_block=0, use_block=0),
        ]
        g.add_def_uses_batch(facts)
        results = g.def_uses(func_qn="a.f")
        kinds = {(d.var_name, d.kind) for d in results}
        assert ("x", "write") in kinds
        assert ("x", "read") in kinds
        assert ("y", "aug_write") in kinds

    def test_def_use_serialization_roundtrip(self):
        """JSON serialization preserves kind."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "x", kind="aug_write",
            def_block=0, use_block=1,
        ))
        json_str = g.to_json()
        g2 = FactGraph.from_json(json_str)
        results = g2.def_uses(func_qn="a.f")
        assert len(results) == 1
        assert results[0].kind == "aug_write"


# ---------------------------------------------------------------------------
# MethodCallFact
# ---------------------------------------------------------------------------


class TestMethodCallFact:
    """MethodCallFact captures receiver.method() calls."""

    def test_method_call_fact_fields(self):
        """MethodCallFact has all required fields."""
        mc = MethodCallFact(
            file_path="app.py", func_qn="app.main",
            receiver="obj", method="append",
            block_id=0, line=5,
        )
        assert mc.file_path == "app.py"
        assert mc.receiver == "obj"
        assert mc.method == "append"

    def test_add_and_query_method_call(self):
        """method_call CozoDB relation stores and retrieves facts."""
        g = FactGraph()
        g.add_method_call(MethodCallFact(
            "app.py", "app.main", "obj", "append", block_id=0, line=5,
        ))
        results = g.method_calls(func_qn="app.main")
        assert len(results) == 1
        assert results[0].receiver == "obj"
        assert results[0].method == "append"

    def test_method_call_batch(self):
        """Bulk insert for method_call facts."""
        g = FactGraph()
        facts = [
            MethodCallFact("a.py", "a.f", "items", "append", 0, 5),
            MethodCallFact("a.py", "a.f", "d", "update", 1, 10),
            MethodCallFact("a.py", "a.f", "obj", "save", 2, 15),
        ]
        g.add_method_calls_batch(facts)
        results = g.method_calls(func_qn="a.f")
        assert len(results) == 3
        receivers = {r.receiver for r in results}
        assert receivers == {"items", "d", "obj"}

    def test_method_call_serialization_roundtrip(self):
        """JSON serialization preserves MethodCallFact."""
        g = FactGraph()
        g.add_method_call(MethodCallFact(
            "a.py", "a.f", "obj", "save", block_id=0, line=5,
        ))
        json_str = g.to_json()
        g2 = FactGraph.from_json(json_str)
        results = g2.method_calls(func_qn="a.f")
        assert len(results) == 1
        assert results[0].method == "save"


# ---------------------------------------------------------------------------
# Taint propagation with effect-based sinks
# ---------------------------------------------------------------------------


class TestEffectBasedSinks:
    """trace_propagation_datalog() supports effect_sinks parameter."""

    def _make_graph_with_mutation(self):
        """Build a graph where tainted var is mutated via attribute write."""
        g = FactGraph()
        # x = source() in block 0 → kind=write
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        # x.dirty = val in block 1 → kind=write (dotted attr)
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x.dirty", kind="write",
            def_block=1, use_block=1,
            def_line=5,
        ))
        return g

    def test_effect_sink_writes_detects_mutation(self):
        """writes($X) effect sink fires when tainted var's attr is written."""
        g = self._make_graph_with_mutation()
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "toctou")],
            sinks=[],  # no pattern sinks
            effect_sinks=[("toctou", "writes")],
        )
        assert len(flows) >= 1
        assert any(f.label == "toctou" for f in flows)

    def test_effect_sink_writes_includes_aug_write(self):
        """writes($X) fires for augmented assignment on tainted var."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x.count", kind="aug_write",
            def_block=1, use_block=1,
            def_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "toctou")],
            sinks=[],
            effect_sinks=[("toctou", "writes")],
        )
        assert len(flows) >= 1

    def test_effect_sink_writes_includes_method_call(self):
        """writes($X) fires for method call on tainted var (e.g. x.append())."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        g.add_method_call(MethodCallFact(
            "app.py", "app.main", "x", "append", block_id=1, line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "toctou")],
            sinks=[],
            effect_sinks=[("toctou", "writes")],
        )
        assert len(flows) >= 1

    def test_effect_sink_does_not_fire_for_read(self):
        """writes($X) does NOT fire for a plain read of tainted var."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        # Only a read of x in block 1, no write/mutation
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="read",
            def_block=0, use_block=1,
            use_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "toctou")],
            sinks=[],
            effect_sinks=[("toctou", "writes")],
        )
        assert len(flows) == 0

    def test_effect_sink_does_not_fire_for_del(self):
        """writes($X) excludes del (unbinding is not mutation)."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="del",
            def_block=1, use_block=1,
            def_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "toctou")],
            sinks=[],
            effect_sinks=[("toctou", "writes")],
        )
        assert len(flows) == 0

    def test_effect_and_pattern_sinks_coexist(self):
        """Both effect_sinks and pattern sinks can fire in the same query."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        # Pattern sink: x reaches block 1
        # Effect sink: x.attr written in block 1
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x.attr", kind="write",
            def_block=1, use_block=1,
            def_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "sqli")],
            sinks=[("app.py", "app.main", "x", 1, "sqli")],
            effect_sinks=[("sqli", "writes")],
        )
        # Should find at least one violation (from pattern sink and/or effect sink)
        assert len(flows) >= 1

    def test_effect_sink_sanitizer_suppresses(self):
        """Sanitizers still suppress effect-based sink violations."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x.dirty", kind="write",
            def_block=1, use_block=1,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "toctou")],
            sinks=[],
            effect_sinks=[("toctou", "writes")],
            sanitizers=[("app.py", "app.main", "x", 0, "toctou")],
        )
        assert len(flows) == 0


# ---------------------------------------------------------------------------
# Augmented assignment via DefUseFact (tree-sitter backed)
# ---------------------------------------------------------------------------


class TestAugmentedAssignment:
    """DefUseFact detects augmented assignments as aug_write."""

    def test_augmented_assignment_detected(self, tmp_path):
        """x += 1 is detected as an aug_write DefUseFact."""
        source = "def f():\n    x = 0\n    x += 1\n"
        test_file = tmp_path / "test.py"
        test_file.write_text(source)
        graph = FactGraph.build_from_files([str(test_file)], language="python")
        facts = graph.def_uses(file_path=str(test_file))
        write_facts = [f for f in facts if f.kind in ("write", "aug_write")]
        targets = [f.var_name for f in write_facts]
        assert "x" in targets
        aug_facts = [f for f in write_facts if f.kind == "aug_write"]
        assert len(aug_facts) >= 1

    def test_dotted_augmented_assignment(self, tmp_path):
        """obj.count += 1 is detected."""
        source = "def f():\n    obj.count += 1\n"
        test_file = tmp_path / "test.py"
        test_file.write_text(source)
        graph = FactGraph.build_from_files([str(test_file)], language="python")
        facts = graph.def_uses(file_path=str(test_file))
        targets = [f.var_name for f in facts if f.kind in ("write", "aug_write")]
        assert any("obj" in t or "count" in t for t in targets)

    def test_various_aug_operators(self, tmp_path):
        """All augmented operators are detected: -=, *=, /=, etc."""
        source = "def f():\n    a -= 1\n    b *= 2\n    c //= 3\n    d **= 2\n    e &= 0xff\n"
        test_file = tmp_path / "test.py"
        test_file.write_text(source)
        graph = FactGraph.build_from_files([str(test_file)], language="python")
        facts = graph.def_uses(file_path=str(test_file))
        write_facts = [f for f in facts if f.kind in ("write", "aug_write")]
        assert len(write_facts) >= 5


# ---------------------------------------------------------------------------
# Effect key on TraceSink config
# ---------------------------------------------------------------------------


class TestEffectSinkConfig:
    """TraceSink supports an `effect` key as alternative to `pattern`."""

    def test_taint_sink_with_effect(self):
        """TraceSink can be created with effect instead of pattern."""
        sink = TraceSink(
            pattern="",
            label="toctou",
            message="Mutation on unlocked object",
            effect="writes($OBJ)",
        )
        assert sink.effect == "writes($OBJ)"

    def test_load_config_effect_sinks(self, tmp_path):
        """load_trace_config parses sinks with `effect` key."""
        config_dict = {
            "trace": {
                "labels": ["toctou"],
                "sources": [{"pattern": "$Q.first()", "label": "toctou"}],
                "sinks": [
                    {
                        "effect": "writes($OBJ)",
                        "label": "toctou",
                        "message": "TOCTOU: mutation on unlocked ORM object",
                    },
                ],
            },
        }
        config_file = tmp_path / ".emend" / "patterns.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(yaml.dump(config_dict))
        config = load_trace_config(str(config_file))
        effect_sinks = [s for s in config.sinks if s.effect]
        assert len(effect_sinks) == 1
        assert effect_sinks[0].effect == "writes($OBJ)"

    def test_effect_sinks_in_config(self, tmp_path):
        """Effect sinks are loaded from YAML config."""
        config_dict = {
            "trace": {
                "labels": ["unlocked_read"],
                "sources": [{"pattern": "$Q.first()", "label": "unlocked_read"}],
                "sinks": [
                    {
                        "effect": "writes($OBJ)",
                        "label": "unlocked_read",
                        "message": "TOCTOU: mutation on unlocked ORM object",
                    },
                ],
            },
        }
        config_file = tmp_path / ".emend" / "patterns.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(yaml.dump(config_dict))
        config = load_trace_config(str(config_file))
        assert len(config.sinks) == 1
        assert config.sinks[0].effect == "writes($OBJ)"


# ---------------------------------------------------------------------------
# Datalog: is_var_or_attr pattern (dotted-name prefix matching)
# ---------------------------------------------------------------------------


class TestDottedNameMatching:
    """Effect predicates match both `var` and `var.attr` via is_var_or_attr."""

    def test_writes_matches_exact_var(self):
        """writes(obj) fires for direct write to `obj`."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "obj", kind="write",
            def_block=0, use_block=1,
        ))
        # A write to 'obj' itself in block 1
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "obj", kind="write",
            def_block=1, use_block=1,
            def_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("a.py", "a.f", "obj", 0, "lbl")],
            sinks=[],
            effect_sinks=[("lbl", "writes")],
        )
        assert len(flows) >= 1

    def test_writes_matches_dotted_attr(self):
        """writes(obj) fires for write to `obj.field`."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "obj", kind="write",
            def_block=0, use_block=1,
        ))
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "obj.field", kind="write",
            def_block=1, use_block=1,
            def_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("a.py", "a.f", "obj", 0, "lbl")],
            sinks=[],
            effect_sinks=[("lbl", "writes")],
        )
        assert len(flows) >= 1

    def test_writes_no_false_positive_on_prefix(self):
        """writes(obj) does NOT match `objection.field` (must be dot boundary)."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "obj", kind="write",
            def_block=0, use_block=1,
        ))
        # 'objection.field' is NOT an attribute of 'obj'
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "objection.field", kind="write",
            def_block=1, use_block=1,
            def_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("a.py", "a.f", "obj", 0, "lbl")],
            sinks=[],
            effect_sinks=[("lbl", "writes")],
        )
        # Should NOT fire — 'objection' is not an attribute of 'obj'
        assert len(flows) == 0

    def test_writes_matches_nested_dotted_attr(self):
        """writes(obj) fires for write to `obj.field.sub`."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "obj", kind="write",
            def_block=0, use_block=1,
        ))
        g.add_def_use(DefUseFact(
            "a.py", "a.f", "obj.field.sub", kind="write",
            def_block=1, use_block=1,
            def_line=5,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("a.py", "a.f", "obj", 0, "lbl")],
            sinks=[],
            effect_sinks=[("lbl", "writes")],
        )
        assert len(flows) >= 1


# ---------------------------------------------------------------------------
# Taint propagation Datalog queries still work with kind (backwards compat)
# ---------------------------------------------------------------------------


class TestTaintPropagationWithKind:
    """Existing trace_propagation_datalog works with new kind column."""

    def test_basic_propagation_still_works(self):
        """Standard taint propagation through def-use chains still works."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.main", "x", 0, "sqli")],
            sinks=[("app.py", "app.main", "x", 1, "sqli")],
        )
        assert len(flows) >= 1

    def test_flow_rule_check_still_works(self):
        """flow_rule_check_datalog works with new kind column."""
        g = FactGraph()
        g.add_def_use(DefUseFact(
            "app.py", "app.main", "x", kind="write",
            def_block=0, use_block=1,
        ))
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.main", "x", 0)],
            sinks=[("app.py", "app.main", "x", 1)],
        )
        assert len(violations) >= 1

    def test_interprocedural_taint_still_works(self):
        """interprocedural_trace_datalog works with new kind column."""
        from emend.fact_graph import CallFact, FuncSummaryFact
        g = FactGraph()
        g.add_symbol(SymbolFact("a.py", "foo", "a.foo", "function", 1, 5, None))
        g.add_symbol(SymbolFact("b.py", "bar", "b.bar", "function", 1, 5, None))
        g.add_call(CallFact("a.foo", "b.bar", "a.py", 2, 0))
        g.add_def_use(DefUseFact(
            "a.py", "a.foo", "x", kind="write", def_block=0, use_block=1,
        ))
        g.add_func_summary(FuncSummaryFact("b.bar", "x", flows_to_sink=True, sink_label="sqli"))
        violations = g.interprocedural_trace_datalog()
        assert len(violations) >= 1
