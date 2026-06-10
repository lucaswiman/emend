"""Regression tests for trace line-number correctness.

Covers two verified bugs in ``emend.trace._run_trace_datalog``:

BUG 1: Effect-sink violations reported 0-indexed line numbers.
       ``_resolve_effect_sink_line`` returned ``DefUseFact``/``MethodCallFact``
       line fields raw, but those are 0-indexed (see fact_graph.py where the
       method-call populator emits ``line - 1`` and def-use facts use 0-based
       lines).  The violation therefore pointed one line above the real sink.

BUG 2: Source-line witness lookup collided when one CFG block had multiple
       sources.  The lookup dict was keyed ``(fp, fq, label, block_id)`` with
       no variable dimension, so two same-label sources in the same basic
       block (different vars / lines) overwrote each other and a violation's
       source TraceStep got the wrong line / variable.
"""

from emend.trace import (
    TraceConfig,
    TraceSink,
    TraceSource,
    run_trace_analysis,
)


# ---------------------------------------------------------------------------
# BUG 1: effect-sink line numbers are 1-indexed
# ---------------------------------------------------------------------------


def test_effect_sink_method_call_line_is_one_indexed(tmp_path):
    """A ``writes($X)`` effect sink resolved from a method call reports the
    actual 1-indexed source line, not one line above it.

    Code (1-indexed lines):
        1  def process(flag):
        2      obj = get_record()
        3      if flag:
        4          obj.save()     # effect sink: writes($OBJ) via method call

    The mutation happens on line 4; the violation and its sink TraceStep must
    point to line 4, not line 3.
    """
    f = tmp_path / "app.py"
    f.write_text(
        "def process(flag):\n"
        "    obj = get_record()\n"
        "    if flag:\n"
        "        obj.save()\n"
    )
    config = TraceConfig(
        labels=["toctou"],
        sources=[TraceSource(pattern="$X = get_record()", label="toctou")],
        sinks=[
            TraceSink(
                pattern="",
                label="toctou",
                message="mutation on tainted object",
                effect="writes($OBJ)",
            )
        ],
    )
    violations = run_trace_analysis([str(f)], config, project_path=None)
    assert len(violations) == 1
    v = violations[0]
    # ``obj.save()`` is on line 4 (1-indexed).
    assert v.line == 4, f"expected sink line 4, got {v.line}"
    sink_steps = [s for s in v.trace if s.description.startswith("sink:")]
    assert sink_steps, "expected a sink TraceStep"
    assert sink_steps[0].line == 4, (
        f"expected sink step line 4, got {sink_steps[0].line}"
    )
    # The source step is already 1-indexed (find_pattern lines) — sanity check.
    src_steps = [s for s in v.trace if s.description.startswith("source:")]
    assert src_steps and src_steps[0].line == 2


# ---------------------------------------------------------------------------
# BUG 2: per-block source witness lookup must be keyed by variable
# ---------------------------------------------------------------------------


def test_two_sources_in_one_block_keep_distinct_source_witnesses(tmp_path):
    """Two same-label sources in the same basic block must not clobber each
    other's source witness.

    Code (1-indexed lines):
        1  def process():
        2      a = tainted()      # source for label 'lbl', var a
        3      b = tainted()      # source for label 'lbl', var b
        4      sink_a(a)          # violation: a reaches sink_a
        5      sink_b(b)          # violation: b reaches sink_b

    Lines 2-5 form a single straight-line basic block, so both sources share
    the same block id.  The source-line witness lookup is keyed by
    ``(file, func, label, source_var, block_id)``; without the ``source_var``
    dimension the two sources collide and the last-recorded line clobbers the
    other.  This test pins that each source witness resolves to the line that
    actually defines its variable:
        - source var 'a' -> line 2 (a = tainted())
        - source var 'b' -> line 3 (b = tainted())
    """
    f = tmp_path / "app.py"
    f.write_text(
        "def process():\n"
        "    a = tainted()\n"
        "    b = tainted()\n"
        "    sink_a(a)\n"
        "    sink_b(b)\n"
    )
    config = TraceConfig(
        labels=["lbl"],
        sources=[TraceSource(pattern="$X = tainted()", label="lbl")],
        sinks=[
            TraceSink(pattern="sink_a($X)", label="lbl", message="a reached sink"),
            TraceSink(pattern="sink_b($X)", label="lbl", message="b reached sink"),
        ],
    )
    violations = run_trace_analysis([str(f)], config, project_path=None)
    by_sink = {v.sink_pattern: v for v in violations}
    assert "sink_a($X)" in by_sink
    assert "sink_b($X)" in by_sink

    # Expected source line per source variable.  The witness step records the
    # variable it resolved; assert that variable's line matches its definition,
    # proving the (label, var, block) key did not collide on the shared block.
    expected_line = {"a": 2, "b": 3}
    for v in violations:
        src_steps = [s for s in v.trace if s.description.startswith("source:")]
        assert src_steps, f"expected a source step for {v.sink_pattern}"
        step = src_steps[0]
        assert step.line == expected_line[step.variable], (
            f"source witness for var {step.variable!r} should point at line "
            f"{expected_line[step.variable]}, got {step.line}"
        )
