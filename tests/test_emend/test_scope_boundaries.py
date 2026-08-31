"""Tests for Phase 3: Scope Boundaries (Taint-CFG Precision).

Tests cover:
- TraceScopeSanitizer dataclass: construction and config integration
- YAML config loading for scope_sanitizers section
- Datalog scope_kill parameter on trace_propagation_datalog()
- Effect-sink + scope_kill interaction
- Python fallback scope sanitizer behavior
- Nested scope limitation documentation
- merge_configs() concatenation of scope_sanitizers
"""

import pytest
import yaml

from emend.fact_graph import (
    CfgBlockFact,
    CfgEdgeFact,
    DefUseFact,
    FactGraph,
    SymbolFact,
)
from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceScopeSanitizer,
    TraceSink,
    TraceSource,
    load_trace_config,
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


def _commit_scope_config(message="tainted value used"):
    """TraceConfig where ``session.commit()`` is a scope sanitizer that kills
    the ``lbl`` taint produced by ``tainted()`` before it reaches ``use($X)``.
    """
    return TraceConfig(
        labels=["lbl"],
        sources=[TraceSource(pattern="tainted()", label="lbl")],
        sinks=[TraceSink(pattern="use($X)", label="lbl", message=message)],
        scope_sanitizers=[
            TraceScopeSanitizer(pattern="session.commit()", label="lbl"),
        ],
    )


# ---------------------------------------------------------------------------
# TraceScopeSanitizer dataclass tests
# ---------------------------------------------------------------------------


class TestTraceScopeSanitizerDataclass:
    """TraceScopeSanitizer has pattern and label fields."""

    def test_scope_sanitizer_dataclass(self):
        """Basic construction with pattern and label."""
        ss = TraceScopeSanitizer(pattern="session.commit()", label="unlocked_read")
        assert ss.pattern == "session.commit()"
        assert ss.label == "unlocked_read"

    def test_scope_sanitizer_in_config(self):
        """TraceConfig accepts a scope_sanitizers list."""
        ss1 = TraceScopeSanitizer(pattern="session.commit()", label="unlocked_read")
        ss2 = TraceScopeSanitizer(pattern="db.flush()", label="unlocked_read")
        cfg = TraceConfig(scope_sanitizers=[ss1, ss2])
        assert len(cfg.scope_sanitizers) == 2
        assert cfg.scope_sanitizers[0].pattern == "session.commit()"
        assert cfg.scope_sanitizers[1].pattern == "db.flush()"

    def test_scope_sanitizer_config_default_empty(self):
        """TraceConfig.scope_sanitizers defaults to empty list."""
        cfg = TraceConfig()
        assert cfg.scope_sanitizers == []

    def test_scope_sanitizer_no_quantifier(self):
        """TraceScopeSanitizer has no quantifier — always kills all taint."""
        ss = TraceScopeSanitizer(pattern="session.commit()", label="lbl")
        # No quantifier field expected; access should raise AttributeError
        assert not hasattr(ss, "quantifier")


# ---------------------------------------------------------------------------
# YAML config loading tests
# ---------------------------------------------------------------------------


class TestLoadScopesSanitizersFromYaml:
    """load_trace_config() parses scope_sanitizers from YAML."""

    def test_load_scope_sanitizers_from_yaml(self, tmp_path):
        """YAML file with scope_sanitizers section is loaded correctly."""
        cfg_file = tmp_path / "patterns.yaml"
        cfg_file.write_text("""\
trace:
  labels: [unlocked_read]
  sources:
    - pattern: "$Q.first()"
      label: unlocked_read
  sinks:
    - effect: "writes($OBJ)"
      label: unlocked_read
      message: "TOCTOU: mutation on unlocked object"
  scope_sanitizers:
    - pattern: "session.commit()"
      label: unlocked_read
    - pattern: "db.flush()"
      label: unlocked_read
""")
        config = load_trace_config(str(cfg_file))
        assert len(config.scope_sanitizers) == 2
        patterns = {s.pattern for s in config.scope_sanitizers}
        assert "session.commit()" in patterns
        assert "db.flush()" in patterns
        labels = {s.label for s in config.scope_sanitizers}
        assert labels == {"unlocked_read"}

    def test_load_empty_scope_sanitizers(self, tmp_path):
        """YAML with no scope_sanitizers key still results in empty list."""
        cfg_file = tmp_path / "patterns.yaml"
        cfg_file.write_text("""\
trace:
  labels: [sqli]
  sources:
    - pattern: "request.args.get($KEY)"
      label: sqli
  sinks:
    - pattern: "execute($SQL)"
      label: sqli
      message: "SQL injection"
""")
        config = load_trace_config(str(cfg_file))
        assert config.scope_sanitizers == []

    def test_load_scope_sanitizer_with_different_label(self, tmp_path):
        """Scope sanitizers can target labels different from other config labels."""
        cfg_file = tmp_path / "patterns.yaml"
        cfg_file.write_text("""\
trace:
  labels: [lbl_a, lbl_b]
  sources:
    - pattern: "source_a()"
      label: lbl_a
    - pattern: "source_b()"
      label: lbl_b
  sinks:
    - pattern: "sink($X)"
      label: lbl_a
      message: "sink a"
    - pattern: "sink($X)"
      label: lbl_b
      message: "sink b"
  scope_sanitizers:
    - pattern: "commit()"
      label: lbl_a
""")
        config = load_trace_config(str(cfg_file))
        assert len(config.scope_sanitizers) == 1
        assert config.scope_sanitizers[0].label == "lbl_a"


# ---------------------------------------------------------------------------
# Datalog scope_kill tests
# ---------------------------------------------------------------------------


class TestScopeKillDatalog:
    """trace_propagation_datalog() scope_kills parameter kills all taint for a label."""

    def test_scope_kill_blocks_all_taint_for_label(self):
        """scope_kill blocks taint for ALL variables carrying the label."""
        g = _build_linear_cfg(4)

        # x and y both tainted in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "y", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        # scope_kill in block 1 kills ALL "lbl" taint
        flows = g.trace_propagation_datalog(
            sources=[
                ("app.py", "app.f", "x", 0, "lbl"),
                ("app.py", "app.f", "y", 0, "lbl"),
            ],
            sinks=[
                ("app.py", "app.f", "x", 2, "lbl"),
                ("app.py", "app.f", "y", 2, "lbl"),
            ],
            scope_kills=[("app.py", "app.f", "lbl", 1)],
        )
        assert len(flows) == 0

    def test_scope_kill_does_not_affect_other_labels(self):
        """scope_kill only kills taint for its label, not other labels."""
        g = _build_linear_cfg(4)

        # Both lbl_a and lbl_b tainted
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        # scope_kill only kills lbl_a; lbl_b taint should still reach sink
        flows = g.trace_propagation_datalog(
            sources=[
                ("app.py", "app.f", "x", 0, "lbl_a"),
                ("app.py", "app.f", "x", 0, "lbl_b"),
            ],
            sinks=[
                ("app.py", "app.f", "x", 2, "lbl_a"),
                ("app.py", "app.f", "x", 2, "lbl_b"),
            ],
            scope_kills=[("app.py", "app.f", "lbl_a", 1)],
        )
        # lbl_a is killed; lbl_b should still fire
        fired_labels = {f.label for f in flows}
        assert "lbl_a" not in fired_labels
        assert "lbl_b" in fired_labels

    def test_scope_kill_on_one_branch_still_fires(self):
        """scope_kill on only one branch of a diamond still allows violation via other branch."""
        g = _build_diamond_cfg()

        # x tainted in block 0, used in block 4
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=4, def_line=1, use_line=9,
        ))

        # scope_kill only on block 2 (true branch), not block 3 (false branch)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            scope_kills=[("app.py", "app.f", "lbl", 2)],
        )
        # Violation still fires because block 3 has no scope_kill
        assert len(flows) >= 1

    def test_scope_kill_on_all_branches_suppresses(self):
        """scope_kill on both branches of a diamond suppresses violation."""
        g = _build_diamond_cfg()

        # x tainted in block 0, used in block 4
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=4, def_line=1, use_line=9,
        ))

        # scope_kill on both branches (blocks 2 and 3)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 4, "lbl")],
            scope_kills=[
                ("app.py", "app.f", "lbl", 2),
                ("app.py", "app.f", "lbl", 3),
            ],
        )
        assert len(flows) == 0

    def test_scope_kill_combined_with_regular_sanitizer(self):
        """Both scope_kill and regular sanitizer independently block taint."""
        g = _build_linear_cfg(5)

        # Two variables tainted in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=3, def_line=1, use_line=4,
        ))
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "y", kind="write",
            def_block=0, use_block=3, def_line=1, use_line=4,
        ))

        # scope_kill for x's label in block 1; regular sanitizer for y in block 2
        flows = g.trace_propagation_datalog(
            sources=[
                ("app.py", "app.f", "x", 0, "lbl"),
                ("app.py", "app.f", "y", 0, "lbl"),
            ],
            sinks=[
                ("app.py", "app.f", "x", 3, "lbl"),
                ("app.py", "app.f", "y", 3, "lbl"),
            ],
            sanitizers=[("app.py", "app.f", "y", 2, "lbl")],
            scope_kills=[("app.py", "app.f", "lbl", 1)],
        )
        # Both x (via scope_kill) and y (via sanitizer) are blocked
        assert len(flows) == 0

    def test_scope_kill_after_sink_does_not_suppress(self):
        """scope_kill in a block AFTER the sink does not suppress the violation."""
        g = _build_linear_cfg(4)

        # x tainted in block 0, sink in block 1, scope_kill in block 2
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=1, def_line=1, use_line=2,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 1, "lbl")],
            scope_kills=[("app.py", "app.f", "lbl", 2)],
        )
        # scope_kill is too late; violation should still fire
        assert len(flows) >= 1

    def test_scope_kill_no_sources_no_violation(self):
        """With no sources, scope_kill produces no violations."""
        g = _build_linear_cfg(4)

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        flows = g.trace_propagation_datalog(
            sources=[],
            sinks=[("app.py", "app.f", "x", 2, "lbl")],
            scope_kills=[("app.py", "app.f", "lbl", 1)],
        )
        assert len(flows) == 0

    def test_no_scope_kill_still_reports_violation(self):
        """Without scope_kills, taint reaches sink normally."""
        g = _build_linear_cfg(4)

        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[("app.py", "app.f", "x", 2, "lbl")],
        )
        assert len(flows) >= 1


# ---------------------------------------------------------------------------
# Effect-sink + scope_kill interaction
# ---------------------------------------------------------------------------


class TestScopeKillWithEffectSinks:
    """scope_kill also suppresses effect-based (writes($X)) violations."""

    def test_scope_kill_blocks_effect_sinks(self):
        """scope_kill in block 1 prevents writes($X) effect sink from firing in block 2."""
        g = _build_linear_cfg(4)

        # x tainted in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=2, def_line=1, use_line=3,
        ))
        # x.attr written (mutated) in block 2 — this is the effect sink
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.attr", kind="write",
            def_block=2, use_block=3, def_line=3, use_line=4,
        ))

        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[],
            effect_sinks=[("lbl", "writes")],
            scope_kills=[("app.py", "app.f", "lbl", 1)],
        )
        # scope_kill in block 1 should prevent the effect sink from firing
        assert len(flows) == 0

    def test_scope_kill_blocks_effect_sink_on_all_branches(self):
        """Effect sink suppressed when scope_kill covers all paths."""
        g = _build_diamond_cfg()

        # x tainted in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=4, def_line=1, use_line=9,
        ))
        # x.dirty written in block 4 — effect sink
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.dirty", kind="write",
            def_block=4, use_block=5, def_line=9, use_line=10,
        ))

        # scope_kill on both branches
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[],
            effect_sinks=[("lbl", "writes")],
            scope_kills=[
                ("app.py", "app.f", "lbl", 2),
                ("app.py", "app.f", "lbl", 3),
            ],
        )
        assert len(flows) == 0

    def test_scope_kill_on_one_branch_effect_sink_still_fires(self):
        """Effect sink still fires when scope_kill covers only one branch."""
        g = _build_diamond_cfg()

        # x tainted in block 0
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x", kind="write",
            def_block=0, use_block=4, def_line=1, use_line=9,
        ))
        # x.dirty written in block 4 — effect sink
        g.add_def_use(DefUseFact(
            "app.py", "app.f", "x.dirty", kind="write",
            def_block=4, use_block=5, def_line=9, use_line=10,
        ))

        # scope_kill only on block 2 (true branch)
        flows = g.trace_propagation_datalog(
            sources=[("app.py", "app.f", "x", 0, "lbl")],
            sinks=[],
            effect_sinks=[("lbl", "writes")],
            scope_kills=[("app.py", "app.f", "lbl", 2)],
        )
        # Block 3 path has no scope_kill → effect sink fires
        assert len(flows) >= 1


# ---------------------------------------------------------------------------
# Python fallback tests
# ---------------------------------------------------------------------------


class TestScopeSanitizerIntegration:
    """Scope sanitizers work in the Datalog trace engine."""

    def test_scope_sanitizer_clears_taint(self, tmp_path):
        """Scope sanitizer clears ALL taint for a label.

        Code:
            def process():
                x = tainted()
                y = tainted()
                session.commit()   # scope sanitizer: kills all 'lbl' taint
                use(x)             # would be sink if tainted
                use(y)             # would be sink if tainted

        Both x and y should be clear after session.commit(); no violations.
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def process():
    x = tainted()
    y = tainted()
    session.commit()
    use(x)
    use(y)
""")
        config = _commit_scope_config()
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        assert len(violations) == 0

    def test_scope_sanitizer_only_kills_matching_label(self, tmp_path):
        """Scope sanitizer kills only the targeted label, not other labels.

        Code:
            def process():
                x = source_a()     # tainted with lbl_a
                y = source_b()     # tainted with lbl_b
                scope_kill()       # scope sanitizer kills lbl_a only
                sink(x)            # no violation: lbl_a killed
                sink(y)            # violation: lbl_b still active
        """
        f = tmp_path / "app.py"
        f.write_text("""\
def process():
    x = source_a()
    y = source_b()
    scope_kill()
    sink(x)
    sink(y)
""")
        config = TraceConfig(
            labels=["lbl_a", "lbl_b"],
            sources=[
                TraceSource(pattern="source_a()", label="lbl_a"),
                TraceSource(pattern="source_b()", label="lbl_b"),
            ],
            sinks=[
                TraceSink(pattern="sink($X)", label="lbl_a", message="lbl_a reached sink"),
                TraceSink(pattern="sink($X)", label="lbl_b", message="lbl_b reached sink"),
            ],
            scope_sanitizers=[
                TraceScopeSanitizer(pattern="scope_kill()", label="lbl_a"),
            ],
        )
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        # Only lbl_b should fire; lbl_a is killed by scope sanitizer
        fired_labels = {v.label for v in violations}
        assert "lbl_a" not in fired_labels
        assert "lbl_b" in fired_labels

    def test_scope_sanitizer_without_matching_pattern(self, tmp_path):
        """When scope sanitizer pattern does NOT appear, taint is NOT killed."""
        f = tmp_path / "app.py"
        f.write_text("""\
def process():
    x = tainted()
    use(x)
""")
        config = _commit_scope_config()
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis(
            [str(f)], config, project_path=None,
        )
        # No scope kill occurs; violation should still fire
        assert len(violations) >= 1


# ---------------------------------------------------------------------------
# merge_configs test
# ---------------------------------------------------------------------------


class TestMergeConfigsWithScopeSanitizers:
    """merge_configs() properly concatenates scope_sanitizers lists."""

    def test_merge_configs_includes_scope_sanitizers(self):
        """merge_configs concatenates scope_sanitizers from all configs."""
        from emend.trace_presets import merge_configs

        c1 = TraceConfig(scope_sanitizers=[TraceScopeSanitizer("a()", "lbl1")])
        c2 = TraceConfig(scope_sanitizers=[TraceScopeSanitizer("b()", "lbl2")])
        merged = merge_configs(c1, c2)
        assert len(merged.scope_sanitizers) == 2
        patterns = {s.pattern for s in merged.scope_sanitizers}
        assert "a()" in patterns
        assert "b()" in patterns

    def test_merge_configs_empty_scope_sanitizers(self):
        """merge_configs handles configs with no scope_sanitizers."""
        from emend.trace_presets import merge_configs

        c1 = TraceConfig(
            labels=["lbl"],
            sources=[TraceSource("src()", "lbl")],
        )
        c2 = TraceConfig(
            scope_sanitizers=[TraceScopeSanitizer("kill()", "lbl")],
        )
        merged = merge_configs(c1, c2)
        assert len(merged.scope_sanitizers) == 1
        assert merged.scope_sanitizers[0].pattern == "kill()"

    def test_merge_configs_multiple_scope_sanitizers(self):
        """merge_configs accumulates scope_sanitizers from multiple configs."""
        from emend.trace_presets import merge_configs

        configs = [
            TraceConfig(scope_sanitizers=[
                TraceScopeSanitizer(f"fn_{i}()", f"lbl_{i}"),
            ])
            for i in range(5)
        ]
        merged = merge_configs(*configs)
        assert len(merged.scope_sanitizers) == 5
        patterns = {s.pattern for s in merged.scope_sanitizers}
        for i in range(5):
            assert f"fn_{i}()" in patterns

    def test_merge_configs_preserves_other_fields(self):
        """merge_configs with scope_sanitizers still merges sources/sinks/sanitizers."""
        from emend.trace_presets import merge_configs

        c1 = TraceConfig(
            labels=["lbl"],
            sources=[TraceSource("src()", "lbl")],
            scope_sanitizers=[TraceScopeSanitizer("kill_a()", "lbl")],
        )
        c2 = TraceConfig(
            labels=["lbl"],
            sinks=[TraceSink("sink($X)", "lbl", "reached sink")],
            scope_sanitizers=[TraceScopeSanitizer("kill_b()", "lbl")],
        )
        merged = merge_configs(c1, c2)
        # Labels deduplicated
        assert merged.labels.count("lbl") == 1
        assert len(merged.sources) == 1
        assert len(merged.sinks) == 1
        # scope_sanitizers concatenated (both kept)
        assert len(merged.scope_sanitizers) == 2


# ---------------------------------------------------------------------------
# Phase 5 regression tests: path-sensitive scope sanitizers (Bug 3)
# ---------------------------------------------------------------------------


class TestScopeSanitizerPathSensitive:
    """Regression tests for Phase 5 Bug 3 fix: scope sanitizers should be
    path-sensitive (only kill taint when they cover all paths from source to
    sink, not when they appear anywhere in the function).
    """

    @pytest.mark.parametrize("handler_body, expect_violation", [
        # scope kill only on the true branch; the false branch carries taint
        # to the sink after the merge.
        (
            "    x = tainted()\n"
            "    if flag:\n"
            "        session.commit()\n"
            "    use(x)\n",
            True,
        ),
        # scope kill covers the only path to the sink → suppressed.
        (
            "    x = tainted()\n"
            "    session.commit()\n"
            "    use(x)\n",
            False,
        ),
        # scope kill comes after the sink → too late, violation still fires.
        (
            "    x = tainted()\n"
            "    use(x)\n"
            "    session.commit()\n",
            True,
        ),
    ])
    def test_path_sensitive_scope_sanitizer_python(
        self, tmp_path, handler_body, expect_violation
    ):
        """Scope sanitizers only kill taint when they cover every path from
        source to sink; otherwise the violation is still reported."""
        f = tmp_path / "app.py"
        f.write_text("def handler():\n" + handler_body)
        config = _commit_scope_config(message="tainted value reached sink")
        from emend.trace import run_trace_analysis
        violations = run_trace_analysis([str(f)], config, project_path=None)
        assert bool(violations) is expect_violation
