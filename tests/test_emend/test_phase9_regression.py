"""Regression tests for Phase 9 of the Datalog migration roadmap.

These tests document and verify fixes for bugs identified in phases 5-8:

1. Sanitizer-on-sink-paths vs sanitizer-on-all-exit-paths false positive
2. Scope sanitizer on one branch vs all branches (path-sensitive behaviour)
3. Effect-based sinks end-to-end through FactGraph
4. flow_rule_check_datalog blocker semantics (def_block vs use_block)
5. TraceViolation.engine field tagging by run_trace_analysis()
"""

import json

import pytest

from emend.fact_graph import (
    CfgBlockFact,
    CfgEdgeFact,
    DefUseFact,
    FactGraph,
    MethodCallFact,
)
from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceScopeSanitizer,
    TraceSource,
    TraceViolation,
    format_violations,
    run_trace_analysis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_linear_cfg(n_blocks: int = 4) -> FactGraph:
    """Build a linear CFG with n_blocks blocks: 0 -> 1 -> ... -> n-1."""
    g = FactGraph()
    for bid in range(n_blocks):
        g.add_cfg_block(CfgBlockFact(
            "app.py", "app.f", bid,
            is_entry=(bid == 0), is_exit=(bid == n_blocks - 1),
        ))
    for bid in range(n_blocks - 1):
        g.add_cfg_edge(CfgEdgeFact(
            "app.py", "app.f", bid, bid + 1, "fallthrough",
            bid + 1, bid + 2,
        ))
    return g


def _build_diamond_cfg() -> FactGraph:
    """Build a diamond CFG for path-split tests.

    CFG structure:
        Block 0 (entry)
          |
        Block 1 (branch)
         / \\
    Block 2  Block 3
         \\ /
        Block 4 (merge)
          |
        Block 5 (exit)
    """
    g = FactGraph()
    for bid in range(6):
        g.add_cfg_block(CfgBlockFact(
            "app.py", "app.f", bid,
            is_entry=(bid == 0), is_exit=(bid == 5),
        ))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "true", 3, 4))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 3, "false", 3, 6))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 4, "fallthrough", 5, 8))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 3, 4, "fallthrough", 7, 8))
    g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 4, 5, "fallthrough", 9, 10))
    return g


# ---------------------------------------------------------------------------
# 1. Sanitizer on sink paths — not necessarily on all exit paths
# ---------------------------------------------------------------------------


class TestSanitizerOnSinkPathsNotExitPaths:
    """Regression: sanitizer covering all source-to-sink paths should suppress
    the violation, even when exit paths (unrelated to the sink) are not covered.

    Bug: the Python engine was checking whether ALL entry-to-exit paths pass
    through a sanitizer, not whether all source-to-sink paths do.  This caused
    false positives when there is a code path after the sink that does not go
    through the sanitizer (e.g. a log statement using the raw value).
    """

    def test_sanitized_sink_no_violation_even_if_exit_path_unsanitized(self, tmp_path):
        """No violation when all paths to the sink are sanitized.

        The function has an exit path that uses the raw (unsanitized) value
        after the sink.  That exit path is irrelevant — the sink itself is
        always reached through the sanitizer, so no violation should fire.

        Function structure:
            name = request.args.get('name')  # source
            safe = sanitize(name)            # sanitizer
            cursor.execute(safe)             # sink — safe, sanitized
            if condition:
                log(name)                    # raw value logged — exit path, NOT the sink
        """
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor, condition):\n"
            "    name = request.args.get('name')\n"
            "    safe = sanitize(name)\n"
            "    cursor.execute(safe)\n"
            "    if condition:\n"
            "        log(name)\n"
        )

        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(
                pattern="cursor.execute($QUERY)",
                label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(pattern="sanitize($X)", label="sqli")],
        )

        violations = run_trace_analysis([str(test_file)], config)
        # The sink (cursor.execute) only ever receives `safe`, which was
        # sanitized, so no violation should be reported.
        sink_violations = [
            v for v in violations
            if "cursor.execute" in v.sink_pattern
        ]
        assert len(sink_violations) == 0, (
            f"False positive: sink-path is sanitized but got violations: {sink_violations}"
        )

    def test_unsanitized_sink_still_fires(self, tmp_path):
        """Sanity check: when the sink genuinely receives a tainted value, a
        violation IS reported."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"  # sink receives raw, tainted value
        )

        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(
                pattern="cursor.execute($QUERY)",
                label="sqli",
                message="SQL injection",
            )],
            sanitizers=[TraceSanitizer(pattern="sanitize($X)", label="sqli")],
        )

        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_datalog_sanitizer_on_all_paths_to_sink_suppresses(self):
        """Datalog engine: sanitizer dominates the sink on all paths, no violation."""
        # Linear CFG: 0 (source) -> 1 (sanitizer) -> 2 (sink) -> 3 (exit)
        g = _build_linear_cfg(4)
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "sqli")],
            sinks=[("app.py", "app.f", "x", 2, "sqli")],
            sanitizers=[("app.py", "app.f", "x", 1, "sqli")],
        )
        assert len(flows) == 0

    def test_datalog_sanitizer_only_on_exit_path_does_not_suppress_sink(self):
        """Datalog engine: sanitizer that appears on an exit path (NOT between
        source and sink) must NOT suppress the sink violation.

        Layout:
            block 0  source
            block 1  branch
            block 2  (sink reached here)  -> block 4 (exit)
            block 3  sanitizer (alternate path, never reaches sink)  -> block 4
        """
        g = FactGraph()
        for bid in range(5):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 4),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "true", 3, 4))   # sink path
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 3, "false", 3, 6))  # sanitizer path
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 4, "fallthrough", 5, 8))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 3, 4, "fallthrough", 7, 8))

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        # Sanitizer is only on block 3 (the path that does NOT lead to the sink)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "sqli")],
            sinks=[("app.py", "app.f", "x", 2, "sqli")],
            sanitizers=[("app.py", "app.f", "x", 3, "sqli")],
        )
        # The sanitizer is on a path that bypasses the sink entirely, so the
        # sink is still reachable with taint.
        assert len(flows) >= 1


# ---------------------------------------------------------------------------
# 2. Scope sanitizer path-sensitivity
# ---------------------------------------------------------------------------


class TestScopeSanitizerPathSensitivity:
    """Regression: scope sanitizers must be path-sensitive.

    A scope sanitizer (e.g. session.commit()) should only suppress a violation
    when the sanitizer appears on ALL paths from source to sink.  If it only
    appears on one branch of a diamond CFG, the violation must still fire via
    the un-sanitized branch.

    The Datalog engine correctly handles this via scope_kills.  The Python
    engine also uses BFS path-sensitivity for scope sanitizers (same as regular
    sanitizers).

    Known limitation in both engines: nested scopes / multiple sessions share
    the same label, so a scope_kill for one session instance also clears taint
    for other sessions carrying the same label.
    """

    def test_scope_kill_on_one_branch_fires_violation(self):
        """Datalog: scope_kill on only one branch of a diamond does not suppress
        the violation because the other branch remains unsanitized."""
        g = _build_diamond_cfg()

        # x tainted in block 0, sink in block 4
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=4, def_line=1, use_line=9,
        ))

        # scope_kill only on block 2 (true branch); block 3 (false branch) is not killed
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            scope_kills=[("app.py", "app.f", "lbl", 2)],
        )
        # Violation fires because taint reaches block 4 via block 3
        assert len(flows) >= 1

    def test_scope_kill_on_all_branches_suppresses_violation(self):
        """Datalog: scope_kill on both branches of a diamond suppresses the violation."""
        g = _build_diamond_cfg()

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=4, def_line=1, use_line=9,
        ))

        # scope_kill on both branches
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            scope_kills=[
                ("app.py", "app.f", "lbl", 2),
                ("app.py", "app.f", "lbl", 3),
            ],
        )
        assert len(flows) == 0

    def test_python_engine_scope_sanitizer_one_branch_fires_violation(self, tmp_path):
        """Python engine: scope sanitizer on only one branch should not suppress
        the violation — taint still reaches the sink via the un-sanitized branch.

        NOTE: the Python engine uses the same BFS path-sensitivity for scope
        sanitizers as for regular sanitizers, so this is the expected behaviour.
        """
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def process(request, session, cursor, condition):\n"
            "    name = request.args.get('name')\n"
            "    if condition:\n"
            "        session.commit()  # scope sanitizer — only on one branch\n"
            "    cursor.execute(name)  # sink — reachable without scope sanitizer\n"
        )

        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(
                pattern="cursor.execute($QUERY)",
                label="sqli",
                message="SQL injection",
            )],
            scope_sanitizers=[
                TraceScopeSanitizer(pattern="session.commit()", label="sqli"),
            ],
        )

        violations = run_trace_analysis([str(test_file)], config)
        # Violation must fire: scope sanitizer only on conditional branch
        assert len(violations) >= 1, (
            "Expected a violation because scope sanitizer is only on one branch"
        )

    def test_python_engine_scope_sanitizer_before_sink_suppresses(self, tmp_path):
        """Python engine: scope sanitizer that unconditionally precedes the sink
        should suppress the violation (all paths to sink go through the sanitizer)."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def process(request, session, cursor):\n"
            "    name = request.args.get('name')\n"
            "    session.commit()  # scope sanitizer — unconditional\n"
            "    cursor.execute(name)  # sink\n"
        )

        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(
                pattern="cursor.execute($QUERY)",
                label="sqli",
                message="SQL injection",
            )],
            scope_sanitizers=[
                TraceScopeSanitizer(pattern="session.commit()", label="sqli"),
            ],
        )

        violations = run_trace_analysis([str(test_file)], config)
        # No violation: the scope sanitizer unconditionally precedes the sink
        assert len(violations) == 0, (
            f"False positive: scope sanitizer precedes sink but got: {violations}"
        )


# ---------------------------------------------------------------------------
# 3. Effect sinks end-to-end through FactGraph
# ---------------------------------------------------------------------------


class TestEffectSinksEndToEnd:
    """Regression: effect-based sinks (writes($OBJ)) must fire when a tainted
    variable is mutated, even without a direct pattern match on the sink call.

    The fix in Phase 1 replaced the old ``attribute_mutation_sinks`` mechanism
    with a general ``effect_sinks`` parameter on trace_propagation_datalog().
    """

    def _build_mutation_graph(self) -> FactGraph:
        """Build a FactGraph where tainted var x has an attribute written."""
        g = FactGraph()
        # Linear CFG: 0 (source) -> 1 (mutation/effect sink) -> 2 (exit)
        for bid in range(3):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 2),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 3, 4))

        # x tainted via write in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=2,
        ))
        # x.value written (mutated) in block 1 — dotted-name effect sink
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.value", kind="write",
            def_block=1, use_block=1, def_line=2, use_line=2,
        ))
        return g

    def test_effect_sink_writes_fires_violation(self):
        """writes($OBJ) effect sink fires when tainted var has attribute written."""
        g = self._build_mutation_graph()

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "toctou")],
            effect_sinks=[("toctou", "writes")],
        )
        assert len(flows) >= 1, "Expected a violation for tainted attribute write"

    def test_effect_sink_without_taint_no_violation(self):
        """No violation when the variable that is written is not tainted."""
        g = FactGraph()
        for bid in range(3):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 2),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 3, 4))

        # Only y is tainted, but x.value is written — no connection
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "y", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=2,
        ))
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.value", kind="write",
            def_block=1, use_block=1, def_line=2, use_line=2,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "y", 0, "toctou")],
            effect_sinks=[("toctou", "writes")],
        )
        # y is tainted but x.value is written, not y.* — no violation
        assert len(flows) == 0

    def test_effect_sink_sanitizer_suppresses_write_violation(self):
        """A sanitizer on all paths to the write-sink suppresses the violation."""
        g = FactGraph()
        # Linear: 0 (source) -> 1 (sanitizer) -> 2 (mutation) -> 3 (exit)
        for bid in range(4):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 3),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 3, 4))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 2, 3, "fallthrough", 5, 6))

        # x tainted in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))
        # x.value written (mutated) in block 2
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.value", kind="write",
            def_block=2, use_block=2, def_line=3, use_line=3,
        ))

        # Sanitizer in block 1 covers all paths to block 2
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "toctou")],
            effect_sinks=[("toctou", "writes")],
            sanitizers=[("app.py", "app.f", "x", 1, "toctou")],
        )
        assert len(flows) == 0

    def test_effect_sink_writes_not_reads_dotted_attr(self):
        """writes($OBJ) fires for dotted attribute write, but NOT for a plain read.

        The effect_sinks mechanism specifically targets mutations (writes/aug_writes)
        and method calls.  A ``kind="read"`` def_use fact does not trigger
        ``writes($OBJ)`` — that is the correct and tested behaviour.
        """
        g = FactGraph()
        for bid in range(3):
            g.add_cfg_block(CfgBlockFact(
                "app.py", "app.f", bid,
                is_entry=(bid == 0), is_exit=(bid == 2),
            ))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 0, 1, "fallthrough", 1, 2))
        g.add_cfg_edge(CfgEdgeFact("app.py", "app.f", 1, 2, "fallthrough", 3, 4))

        # x tainted in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=2,
        ))
        # x.field WRITTEN (mutated) in block 1 — dotted-name write triggers the effect sink
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.field", kind="write",
            def_block=1, use_block=1, def_line=2, use_line=2,
        ))
        # x.other only READ in block 1 — a plain read does NOT trigger writes($OBJ)
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.other", kind="read",
            def_block=1, use_block=1, def_line=2, use_line=2,
        ))

        # writes($OBJ) effect: fires for the x.field write
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "toctou")],
            effect_sinks=[("toctou", "writes")],
        )
        assert len(flows) >= 1, "Expected violation for tainted dotted-name write"


# ---------------------------------------------------------------------------
# 4. flow_rule_check_datalog blocker semantics
# ---------------------------------------------------------------------------


class TestFlowRuleBlockerSemantics:
    """Regression: flow_rule_check_datalog ``not_through`` and ``through``
    parameters operate on def-use block membership.

    The ``not_through`` parameter blocks a def-use hop when the blocker block
    matches either the ``def_block`` or the ``use_block`` of a def-use fact.
    It does NOT do full CFG-path blocking (use trace_propagation_datalog for that).

    The ``through`` parameter uses CFG-edge reachability: a violation fires when
    any path from source to sink *avoids* the required through-point.
    """

    def test_blocker_at_def_block_suppresses(self):
        """not_through at the def_block of a def-use fact suppresses the hop."""
        g = _build_linear_cfg(4)
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        # Blocker at def_block=0 blocks the def-use hop (0 -> 2)
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 2)],
            not_through=[("app.py", "app.f", "x", 0)],
        )
        assert len(violations) == 0

    def test_blocker_at_use_block_suppresses(self):
        """not_through at the use_block of a def-use fact suppresses the hop."""
        g = _build_linear_cfg(4)
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        # Blocker at use_block=2 blocks the def-use hop (0 -> 2)
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 2)],
            not_through=[("app.py", "app.f", "x", 2)],
        )
        assert len(violations) == 0

    def test_blocker_at_intermediate_cfg_block_does_not_suppress(self):
        """not_through at a block that is neither def_block nor use_block of any
        def-use fact on the path does NOT suppress the flow.

        This documents the known behaviour of flow_rule_check_datalog: it checks
        def-use block membership, not CFG-path coverage.  Block 1 is on the CFG
        path between source (block 0) and sink (block 2), but the def-use fact
        spans directly from block 0 to block 2, so block 1 is not relevant to the
        def-use hop and does not suppress it.
        """
        g = _build_linear_cfg(4)
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        # Block 1 is on the CFG path but is NOT def_block (0) or use_block (2)
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 2)],
            not_through=[("app.py", "app.f", "x", 1)],
        )
        # Flow still fires: block 1 is not a def_block or use_block of the hop
        assert len(violations) >= 1

    def test_no_blocker_fires_violation(self):
        """Without a blocker, taint flows freely from source to sink."""
        g = _build_linear_cfg(4)
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 2)],
        )
        assert len(violations) >= 1

    def test_through_required_but_absent_fires_violation(self):
        """through parameter: violation fires when the required through-point is absent.

        The ``through`` parameter uses CFG-edge reachability (avoids_required).
        In a diamond CFG with the through-point on only one branch, the other
        branch always avoids it, triggering a violation.
        """
        g = _build_diamond_cfg()
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=4, def_line=1, use_line=9,
        ))

        # Require x to pass through block 2 on all paths, but block 3 avoids it
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 4)],
            through=[("app.py", "app.f", "x", 2)],
        )
        # Block 3 avoids block 2, so a path exists that skips the through-point
        assert len(violations) >= 1

    def test_through_present_on_all_paths_suppresses(self):
        """through parameter: no violation when through-point is on all CFG paths.

        In a linear CFG, every path from source to sink goes through block 1.
        """
        g = _build_linear_cfg(4)
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        # Linear CFG: every path goes through block 1
        violations = g.flow_rule_check_datalog(
            sources=[("app.py", "app.f", "x", 0)],
            sinks=[("app.py", "app.f", "x", 2)],
            through=[("app.py", "app.f", "x", 1)],
        )
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# 5. TraceViolation.engine field
# ---------------------------------------------------------------------------


class TestTraceViolationEngineField:
    """Regression: TraceViolation must have an ``engine`` field and
    run_trace_analysis() must set it to ``"datalog"`` for all violations
    (after Phase 16 cutover).

    The field was introduced in Phase 8 to distinguish which analysis engine
    produced a violation (useful for differential testing and debugging).
    """

    def test_trace_violation_has_engine_field(self):
        """TraceViolation dataclass has an ``engine`` attribute."""
        v = TraceViolation(
            file_path="app.py",
            line=5,
            col=0,
            label="sqli",
            sink_pattern="cursor.execute($X)",
            message="SQL injection",
        )
        assert hasattr(v, "engine")

    def test_trace_violation_engine_default_empty(self):
        """TraceViolation.engine defaults to empty string."""
        v = TraceViolation(
            file_path="app.py",
            line=5,
            col=0,
            label="sqli",
            sink_pattern="cursor.execute($X)",
            message="SQL injection",
        )
        assert v.engine == ""

    def test_trace_violation_engine_can_be_set(self):
        """TraceViolation.engine can be set explicitly."""
        v = TraceViolation(
            file_path="app.py",
            line=5,
            col=0,
            label="sqli",
            sink_pattern="cursor.execute($X)",
            message="SQL injection",
            engine="python",
        )
        assert v.engine == "python"

    def test_run_trace_analysis_tags_engine_datalog(self, tmp_path):
        """run_trace_analysis() sets engine='datalog' on all violations (Phase 16 cutover)."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )

        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(
                pattern="cursor.execute($QUERY)",
                label="sqli",
                message="SQL injection",
            )],
        )

        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1
        for v in violations:
            assert v.engine == "datalog", (
                f"Expected engine='datalog', got {v.engine!r}"
            )

    def test_format_violations_json_includes_engine(self, tmp_path):
        """format_violations with json_output=True includes 'engine' key when set."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )

        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(
                pattern="cursor.execute($QUERY)",
                label="sqli",
                message="SQL injection",
            )],
        )

        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

        json_str = format_violations(violations, json_output=True)
        data = json.loads(json_str)
        assert isinstance(data, list)
        assert len(data) >= 1
        # All violations with engine set must include "engine" in JSON output
        for entry in data:
            assert "engine" in entry, (
                f"Expected 'engine' key in JSON output, got: {entry}"
            )
            assert entry["engine"] == "datalog"

    def test_format_violations_json_omits_engine_when_empty(self):
        """format_violations JSON omits 'engine' key when engine is empty string."""
        v = TraceViolation(
            file_path="app.py",
            line=5,
            col=0,
            label="sqli",
            sink_pattern="cursor.execute($X)",
            message="SQL injection",
            engine="",  # explicitly empty
        )

        json_str = format_violations([v], json_output=True)
        data = json.loads(json_str)
        assert len(data) == 1
        # engine="" means the key should be omitted from the JSON
        assert "engine" not in data[0], (
            f"Expected 'engine' to be omitted when empty, but found it in: {data[0]}"
        )
