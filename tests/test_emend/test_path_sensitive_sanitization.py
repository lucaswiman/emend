"""Tests for Phase 2: Path-Sensitive Sanitization (Taint-CFG Precision).

Tests cover:
- CFG-edge unsanitized reachability in trace_propagation_datalog()
- Intra-block line-ordering guard for same-block sanitizer+sink
- `quantifier` field (`all_paths`/`some_path`) on TraceSanitizer
- `through` parameter in flow_rule_check_datalog()
- Python fallback per-block taint state
"""

import pytest

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
    TraceSanitizer,
    TraceSink,
    TraceSource,
    load_trace_config,
)


def _build_diamond_cfg():
    """Build a diamond CFG for testing path-sensitive sanitization.

    CFG structure:
        Block 0 (entry) -- source: x = tainted()
          |
        Block 1 (branch)
         / \\
    Block 2  Block 3
    (sanitize) (no sanitize)
         \\ /
        Block 4 (merge) -- sink: use(x)
          |
        Block 5 (exit)

    Def-use chain: x defined in block 0, used through blocks,
    eventually reaches sink in block 4.
    """
    g = FactGraph()

    # Blocks
    for bid in range(6):
        g.add_cfg_block(CfgBlockFact(
            "app.py", "app.f", bid,
            is_entry=(bid == 0), is_exit=(bid == 5),
        ))

    # Edges: 0->1->2->4->5, 1->3->4
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "true", 3, 4))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 3, "false", 3, 6))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 4, "fallthrough", 5, 8))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 3, 4, "fallthrough", 7, 8))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 4, 5, "fallthrough", 9, 10))

    # Def-use: x defined in block 0, used in block 4
    g.add_def_use(DefUseFact(
        "app.py", "app.f", "x", kind="write",
        def_block=0, use_block=1, def_line=1, use_line=3,
    ))
    g.add_def_use(DefUseFact(
        "app.py", "app.f", "x", kind="read",
        def_block=0, use_block=4, def_line=1, use_line=9,
    ))

    return g


# ---------------------------------------------------------------------------
# CFG-edge unsanitized reachability
# ---------------------------------------------------------------------------


class TestUnsanitizedReachability:
    """trace_propagation_datalog() uses CFG-edge reachability to block
    taint only when sanitizer dominates the sink."""

    def test_sanitizer_on_all_paths_suppresses_violation(self):
        """When a sanitizer is on ALL paths from source to sink, no violation."""
        g = FactGraph()

        # Linear CFG: 0 -> 1 -> 2 -> 3
        for bid in range(4):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 3),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 2, 3))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 3, "fallthrough", 3, 4))

        # x tainted in block 0, sanitized in block 1, sink in block 2
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 2, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 1, "lbl")],
        )
        assert len(flows) == 0

    def test_sanitizer_on_one_branch_still_fires(self):
        """When a sanitizer is only on ONE branch of a diamond CFG,
        taint reaches the sink via the unsanitized branch — violation fires."""
        g = _build_diamond_cfg()

        # Sanitizer only on block 2 (true branch), not block 3 (false branch)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 2, "lbl")],
        )
        # Violation should fire because block 3 is unsanitized
        assert len(flows) >= 1

    def test_sanitizer_on_both_branches_suppresses(self):
        """When sanitizers are on BOTH branches of a diamond CFG,
        taint cannot reach the sink — no violation."""
        g = _build_diamond_cfg()

        # Sanitizer on both branches (blocks 2 and 3)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            sanitizers=[
                ("app.py", "app.f", "x", 2, "lbl"),
                ("app.py", "app.f", "x", 3, "lbl"),
            ],
        )
        assert len(flows) == 0

    def test_sanitizer_after_sink_does_not_suppress(self):
        """A sanitizer AFTER the sink does not suppress the violation."""
        g = FactGraph()

        # Linear: 0 -> 1 -> 2 -> 3
        for bid in range(4):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 3),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 2, 3))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 3, "fallthrough", 3, 4))

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=2,
        ))

        # Sink in block 1, sanitizer in block 2 (after sink)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 1, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 2, "lbl")],
        )
        assert len(flows) >= 1

    def test_effect_sink_with_path_sensitive_sanitizer(self):
        """Effect-based sinks also respect path-sensitive sanitization."""
        g = _build_diamond_cfg()

        # Add a write (mutation) in block 4 — effect sink target
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.dirty", kind="write",
            def_block=4, use_block=5, def_line=9, use_line=10,
        ))

        # Sanitizer only on block 2
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            effect_sinks=[("lbl", "writes")],
            sanitizers=[("app.py", "app.f", "x", 2, "lbl")],
        )
        # Block 3 has no sanitizer, so taint reaches block 4 → violation
        assert len(flows) >= 1

    def test_effect_sink_all_paths_sanitized_suppresses(self):
        """Effect sink with sanitizers on both branches → no violation."""
        g = _build_diamond_cfg()

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.dirty", kind="write",
            def_block=4, use_block=5, def_line=9, use_line=10,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            effect_sinks=[("lbl", "writes")],
            sanitizers=[
                ("app.py", "app.f", "x", 2, "lbl"),
                ("app.py", "app.f", "x", 3, "lbl"),
            ],
        )
        assert len(flows) == 0

    def test_no_sanitizer_still_reports_violation(self):
        """Without sanitizers, taint reaches sink normally."""
        g = _build_diamond_cfg()

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
        )
        assert len(flows) >= 1

    def test_sanitizer_blocks_propagation_not_just_detection(self):
        """Sanitizer blocks taint propagation, not just sink detection.

        CFG: 0 -> 1 -> 2 -> 3
        Source in 0, sanitizer in 1, sink in 3.
        Even though def-use connects 0->3, the CFG path passes through
        the sanitizer in block 1.
        """
        g = FactGraph()

        for bid in range(4):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 3),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 2, 3))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 3, "fallthrough", 3, 4))

        # Long-range def-use from block 0 to block 3
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=3, def_line=1, use_line=4,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 3, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 1, "lbl")],
        )
        assert len(flows) == 0


# ---------------------------------------------------------------------------
# Intra-block line-ordering guard
# ---------------------------------------------------------------------------


class TestIntraBlockLineOrdering:
    """When sanitizer and sink are in the same block, line ordering
    determines whether the sanitizer suppresses the violation."""

    def test_sanitizer_before_sink_in_same_block_suppresses(self):
        """Sanitizer on line 5, sink on line 8, same block → suppressed."""
        g = FactGraph()

        for bid in range(3):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 2),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 3))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 9, 10))

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=5,
        ))

        # Source in block 0, sanitizer in block 1 (line 5), sink in block 1 (line 8)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 1, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 1, "lbl")],
            sanitizer_lines=[("app.py", "app.f", "lbl", 1, 5)],
            sink_lines=[("app.py", "app.f", "lbl", 1, 8)],
        )
        assert len(flows) == 0

    def test_sanitizer_after_sink_in_same_block_does_not_suppress(self):
        """Sanitizer on line 8, sink on line 5, same block → NOT suppressed."""
        g = FactGraph()

        for bid in range(3):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 2),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 3))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 9, 10))

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=5,
        ))

        # Source in block 0, sanitizer in block 1 (line 8), sink in block 1 (line 5)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 1, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 1, "lbl")],
            sanitizer_lines=[("app.py", "app.f", "lbl", 1, 8)],
            sink_lines=[("app.py", "app.f", "lbl", 1, 5)],
        )
        assert len(flows) >= 1

    def test_effect_sink_same_block_line_ordering(self):
        """Effect sink in same block as sanitizer respects line ordering."""
        g = FactGraph()

        for bid in range(3):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 2),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 3))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 9, 10))

        # Source in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=5,
        ))
        # Mutation (effect sink) in block 1 at line 8
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.dirty", kind="write",
            def_block=1, use_block=2, def_line=8, use_line=10,
        ))

        # Sanitizer at line 5 (before mutation at line 8) → suppressed
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            effect_sinks=[("lbl", "writes")],
            sanitizers=[("app.py", "app.f", "x", 1, "lbl")],
            sanitizer_lines=[("app.py", "app.f", "lbl", 1, 5)],
        )
        assert len(flows) == 0


# ---------------------------------------------------------------------------
# Quantifier field on TraceSanitizer
# ---------------------------------------------------------------------------


class TestSanitizerQuantifier:
    """TraceSanitizer supports a `quantifier` field."""

    def test_sanitizer_has_quantifier_field(self):
        """TraceSanitizer accepts a `quantifier` keyword argument."""
        san = TraceSanitizer(
            pattern="validate($X)", label="user_input",
            quantifier="all_paths",
        )
        assert san.quantifier == "all_paths"

    def test_sanitizer_quantifier_defaults_to_all_paths(self):
        """Default quantifier is 'all_paths'."""
        san = TraceSanitizer(pattern="validate($X)", label="user_input")
        assert san.quantifier == "all_paths"

    def test_some_path_quantifier_suppresses_with_any_path(self):
        """With some_path quantifier, sanitizer on ONE branch suffices."""
        g = _build_diamond_cfg()

        # With some_path, sanitizer on block 2 alone is enough
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 2, "lbl")],
            sanitizer_quantifier="some_path",
        )
        assert len(flows) == 0

    def test_all_paths_quantifier_requires_all_branches(self):
        """With all_paths quantifier (default), ONE branch is not enough."""
        g = _build_diamond_cfg()

        # With all_paths (default), sanitizer on block 2 alone doesn't suppress
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            sanitizers=[("app.py", "app.f", "x", 2, "lbl")],
            sanitizer_quantifier="all_paths",
        )
        assert len(flows) >= 1

    def test_load_trace_config_quantifier(self, tmp_path):
        """load_trace_config() reads the `quantifier` field from YAML."""
        cfg_file = tmp_path / "patterns.yaml"
        cfg_file.write_text("""\
trace:
  labels: [user_input]
  sources:
    - pattern: "request.args.get($KEY)"
      label: user_input
  sinks:
    - pattern: "execute($SQL)"
      label: user_input
      message: "SQL injection"
  sanitizers:
    - pattern: "validate($X)"
      label: user_input
      quantifier: some_path
""")
        config = load_trace_config(str(cfg_file))
        assert config.sanitizers[0].quantifier == "some_path"

    def test_load_trace_config_quantifier_default(self, tmp_path):
        """Omitted quantifier defaults to 'all_paths'."""
        cfg_file = tmp_path / "patterns.yaml"
        cfg_file.write_text("""\
trace:
  labels: [user_input]
  sources:
    - pattern: "request.args.get($KEY)"
      label: user_input
  sinks:
    - pattern: "execute($SQL)"
      label: user_input
      message: "SQL injection"
  sanitizers:
    - pattern: "validate($X)"
      label: user_input
""")
        config = load_trace_config(str(cfg_file))
        assert config.sanitizers[0].quantifier == "all_paths"


# ---------------------------------------------------------------------------
# flow_rule_check_datalog() `through` parameter
# ---------------------------------------------------------------------------


class TestFlowRuleThroughParameter:
    """flow_rule_check_datalog() `through` uses CFG-edge reachability."""

    def test_through_fires_when_required_point_avoided(self):
        """Violation when a path exists that avoids the required through-point."""
        g = _build_diamond_cfg()

        # Source in block 0, sink in block 4
        # Required through-point in block 2 (only on true branch)
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 4)],
            through=[("app.py", "app.f", "x", 2)],
        )
        # Block 3 path avoids block 2 → violation
        assert len(violations) >= 1

    def test_through_suppresses_when_all_paths_pass(self):
        """No violation when ALL paths pass through the required point."""
        g = FactGraph()

        # Linear: 0 -> 1 -> 2 -> 3
        for bid in range(4):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 3),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 2, 3))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 3, "fallthrough", 3, 4))

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=3, def_line=1, use_line=4,
        ))

        # Required through-point at block 1 (all paths pass through it)
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 3)],
            through=[("app.py", "app.f", "x", 1)],
        )
        assert len(violations) == 0

    def test_through_with_not_through_combined(self):
        """through and not_through can be combined in a single rule."""
        g = _build_diamond_cfg()

        # Must go through block 2, must not go through block 3
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 4)],
            through=[("app.py", "app.f", "x", 2)],
            not_through=[("app.py", "app.f", "x", 3)],
        )
        # Path through block 3 is blocked by not_through,
        # path through block 2 passes through → no violation for through
        # but through check says "fire if path avoids required point",
        # and the only remaining path goes through block 2, so no violation
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Python fallback per-block taint state
# ---------------------------------------------------------------------------


class TestPythonFallbackPerBlockTaint:
    """Python fallback in _analyze_function() uses per-block taint state."""

    def test_sanitizer_on_one_branch_still_fires_python(self, tmp_path):
        """Python fallback: sanitizer on one branch of if/else still fires.

        Code:
            x = source()        # block 0: source
            if cond:
                x = sanitize(x)  # true-branch: sanitized
            else:
                pass              # false-branch: not sanitized
            sink(x)              # merge: still tainted via false-branch
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def handler():
    x = get_user_input()
    if should_validate:
        x = validate(x)
    sink(x)
""")
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="get_user_input()", label="sqli")],
            sinks=[TraceSink(
                pattern="sink($X)", label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(
                pattern="validate($X)", label="sqli",
            )],
        )
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        # The sanitizer only covers the true branch; taint persists on the
        # false branch.  This test will initially fail because the current
        # Python fallback eagerly deletes taint.
        assert len(violations) >= 1

    def test_sanitizer_on_all_branches_suppresses_python(self, tmp_path):
        """Python fallback: sanitizer on all branches suppresses violation.

        Code:
            x = source()
            if cond:
                x = sanitize(x)
            else:
                x = sanitize(x)
            sink(x)
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def handler():
    x = get_user_input()
    if should_validate:
        x = validate(x)
    else:
        x = validate(x)
    sink(x)
""")
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="get_user_input()", label="sqli")],
            sinks=[TraceSink(
                pattern="sink($X)", label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(
                pattern="validate($X)", label="sqli",
            )],
        )
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Phase 5 regression tests: source-to-sink path check semantics
# ---------------------------------------------------------------------------


class TestSourceToSinkPathSemantics:
    """Regression tests for Phase 5 Bug 1 fix: source-to-sink (not entry-to-exit)
    path check for sanitizer coverage."""

    def test_sanitizer_on_branch_not_reaching_source_reports_violation(self, tmp_path):
        """Sanitizer is only on the branch that does NOT reach the sink from
        the same source.  Violation should still be reported.

        Code pattern:
            x = source()       # source
            if flag:
                y = sanitize(x)  # sanitizer on true branch, but y is not x
            sink(x)            # sink: x is still tainted
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def handler():
    x = get_user_input()
    if flag:
        y = validate(x)
    sink(x)
""")
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="get_user_input()", label="sqli")],
            sinks=[TraceSink(
                pattern="sink($X)", label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(
                pattern="validate($X)", label="sqli",
            )],
        )
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        # validate only sanitizes y (via capture), not x.
        # x still reaches sink — violation expected.
        assert len(violations) >= 1

    def test_sanitizer_after_sink_same_block_reports_violation(self, tmp_path):
        """Sanitizer appears after the sink in the same basic block.
        The violation should still be reported.

        Code:
            x = source()      # line 2
            sink(x)           # line 3 — SINK (sanitizer not yet applied)
            x = sanitize(x)   # line 4 — sanitizer comes AFTER sink
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def handler():
    x = get_user_input()
    execute(x)
    x = sanitize(x)
""")
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="get_user_input()", label="sqli")],
            sinks=[TraceSink(
                pattern="execute($X)", label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(
                pattern="sanitize($X)", label="sqli",
            )],
        )
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        # Sanitizer is after the sink; violation should fire.
        assert len(violations) >= 1

    def test_sanitizer_before_sink_same_block_suppresses(self, tmp_path):
        """Sanitizer appears before the sink in the same basic block.
        The violation should be suppressed.

        Code:
            x = source()      # line 2
            x = sanitize(x)   # line 3 — sanitizer before sink
            sink(x)           # line 4
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def handler():
    x = get_user_input()
    x = sanitize(x)
    execute(x)
""")
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="get_user_input()", label="sqli")],
            sinks=[TraceSink(
                pattern="execute($X)", label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(
                pattern="sanitize($X)", label="sqli",
            )],
        )
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        # Sanitizer is before the sink in the same block — no violation.
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# Phase 5 regression tests: CFG build failure behavior (Bug 2)
# ---------------------------------------------------------------------------


class TestCfgBuildFailureBehavior:
    """When CFG construction fails, violations should still be reported.

    Previously, CFG failure returned True (all paths sanitized), which
    silently suppressed violations.  The fix returns False (fail-closed),
    ensuring violations are always reported when the CFG is unavailable.
    """

    def test_violation_reported_without_cfg(self, tmp_path, monkeypatch):
        """Violations are reported even when CFG construction fails.

        Monkeypatching build_cfgs_for_source to raise an exception simulates
        a CFG build failure and verifies the fail-closed behavior.
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def handler():
    x = get_user_input()
    x = sanitize(x)
    execute(x)
""")
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="get_user_input()", label="sqli")],
            sinks=[TraceSink(
                pattern="execute($X)", label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(
                pattern="sanitize($X)", label="sqli",
            )],
        )
        # Patch build_cfgs_for_source to simulate CFG construction failure
        import emend.trace as trace_mod
        original = getattr(trace_mod, "_build_cfgs_for_source_orig", None)

        import emend.cfg as cfg_mod
        original_build = cfg_mod.build_cfgs_for_source

        def failing_build(*args, **kwargs):
            raise RuntimeError("Simulated CFG build failure")

        monkeypatch.setattr(cfg_mod, "build_cfgs_for_source", failing_build)

        from emend.trace import run_trace_analysis
        # With CFG unavailable and a sanitizer present, the fail-closed
        # behavior means we still report the violation (can't prove sanitized).
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        # Fail-closed: violation should be reported even though it would be
        # suppressed if CFG were available and showed the sanitizer covers
        # all paths.  This is intentional (prefer false positives over
        # false negatives when CFG is unavailable).
        assert len(violations) >= 1

    def test_taint_without_sanitizer_always_reports(self, tmp_path):
        """Without any sanitizer, violations are always reported regardless
        of CFG availability."""
        f = tmp_path / "app.py"
        f.write_text("""\
def handler():
    x = get_user_input()
    execute(x)
""")
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="get_user_input()", label="sqli")],
            sinks=[TraceSink(
                pattern="execute($X)", label="sqli",
                message="SQL injection",
            )],
        )
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        assert len(violations) >= 1
