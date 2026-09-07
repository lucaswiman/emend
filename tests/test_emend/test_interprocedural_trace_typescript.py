"""Tests for interprocedural taint analysis on TypeScript source files.

These tests verify that the Datalog/FactGraph interprocedural trace engine can
detect taint flows that cross function boundaries in TypeScript code.  The
engine is language-agnostic (tree-sitter + Rust backend); these tests validate
the TypeScript-specific parameter extraction and cross-function taint
propagation end-to-end.

Coverage:
- Direct cross-function sink violations
- Return-value taint propagation to caller
- Callback-style taint delegation
- Async/await function handling (best-effort)
- Sanitizer ordering (late sanitizer must not suppress earlier violation)
- Multi-hop taint chains (A -> B -> C)
"""

from emend.trace import (
    InterproceduralResult,
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    run_interprocedural_trace,
)


def _make_ts_interproc_config() -> TraceConfig:
    """Return a TraceConfig suitable for TypeScript interprocedural SQL-injection detection."""
    return TraceConfig(
        labels=["user_input"],
        sources=[
            TraceSource(pattern="req.query.get($X)", label="user_input"),
            TraceSource(pattern="req.body.get($X)", label="user_input"),
        ],
        sinks=[
            TraceSink(
                pattern="db.execute($X)",
                label="user_input",
                message="SQL injection: user input reaches db.execute()",
            ),
        ],
        sanitizers=[
            TraceSanitizer(pattern="sanitize($X)", label="user_input"),
            TraceSanitizer(pattern="escape($X)", label="user_input"),
        ],
    )


# ---------------------------------------------------------------------------
# Interprocedural violation tests
# ---------------------------------------------------------------------------

def test_ts_interproc_direct_cross_function_sink(tmp_path):
    """Helper function receives tainted value and passes it directly to a sink.

    call chain: handler() -> runQuery() -> db.execute(tainted_arg)
    """
    test_file = tmp_path / "app.ts"
    test_file.write_text(
        "function runQuery(db, query) {\n"
        "    db.execute(query);\n"
        "}\n"
        "\n"
        "function handler(req, db) {\n"
        '    let name = req.query.get("name");\n'
        "    runQuery(db, name);\n"
        "}\n"
    )

    config = _make_ts_interproc_config()
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")

    assert isinstance(result, InterproceduralResult)
    assert len(result.violations) >= 1, (
        f"Expected at least 1 violation, got 0. Summaries: {list(result.summaries.keys())}"
    )
    assert any("SQL injection" in v.message for v in result.violations)


def test_ts_interproc_returned_taint_reaches_caller(tmp_path):
    """Helper returns tainted value; caller then passes it to a sink.

    call chain: handler() calls passthrough(name), assigns result to query,
    then db.execute(query).
    """
    test_file = tmp_path / "app.ts"
    test_file.write_text(
        "function passthrough(value) {\n"
        "    return value;\n"
        "}\n"
        "\n"
        "function handler(req, db) {\n"
        '    let name = req.query.get("name");\n'
        "    let query = passthrough(name);\n"
        "    db.execute(query);\n"
        "}\n"
    )

    config = _make_ts_interproc_config()
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")

    assert isinstance(result, InterproceduralResult)
    assert len(result.violations) >= 1, (
        f"Expected at least 1 violation, got 0. Summaries: {list(result.summaries.keys())}"
    )


def test_ts_interproc_callback_taint_flow(tmp_path):
    """Value flows from caller through a helper that writes directly to a sink."""
    test_file = tmp_path / "app.ts"
    test_file.write_text(
        "function process(db, data) {\n"
        "    db.execute(data);\n"
        "}\n"
        "\n"
        "function handler(req, db) {\n"
        '    let name = req.query.get("name");\n'
        "    process(db, name);\n"
        "}\n"
    )

    config = _make_ts_interproc_config()
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")

    assert isinstance(result, InterproceduralResult)
    assert len(result.violations) >= 1, (
        f"Expected at least 1 violation, got 0. Summaries: {list(result.summaries.keys())}"
    )


def test_ts_interproc_async_await_taint(tmp_path):
    """Async function returning tainted value; caller assigns and passes to sink.

    The interprocedural engine may or may not unwrap the Promise wrapper added
    by async semantics.  We require no crash and a valid result object; the
    violation count is allowed to be 0 if async wrapping is not tracked.
    """
    test_file = tmp_path / "app.ts"
    test_file.write_text(
        "async function getData(req) {\n"
        '    let data = req.query.get("name");\n'
        "    return data;\n"
        "}\n"
        "\n"
        "async function handler(req, db) {\n"
        "    let result = getData(req);\n"
        "    db.execute(result);\n"
        "}\n"
    )

    config = _make_ts_interproc_config()
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")

    # Minimum: no crash and a valid result object.
    assert isinstance(result, InterproceduralResult)
    # All violations that do appear must carry the correct label.
    for v in result.violations:
        assert v.label == "user_input"


def test_ts_interproc_late_sanitizer_ordering(tmp_path):
    """Sanitizer AFTER the sink call must NOT retroactively suppress the violation.

    The taint flows unsanitized into runQuery(); the escape() call on the
    *next* line comes too late and must not erase the interprocedural violation.
    """
    test_file = tmp_path / "app.ts"
    test_file.write_text(
        "function runQuery(db, query) {\n"
        "    db.execute(query);\n"
        "}\n"
        "\n"
        "function handler(req, db) {\n"
        '    let name = req.query.get("name");\n'
        "    runQuery(db, name);\n"
        "    name = escape(name);\n"
        "}\n"
    )

    config = _make_ts_interproc_config()
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")

    assert isinstance(result, InterproceduralResult)
    assert len(result.violations) >= 1, (
        "Late sanitizer must not suppress the earlier interprocedural violation"
    )


def test_ts_interproc_multi_hop_chain(tmp_path):
    """Taint declared in handler flows through step1 -> step2 -> db.execute().

    The transitive param_to_sink Datalog closure propagates sink reachability
    through call chains of arbitrary depth, so this 3-hop chain is detected.
    """
    test_file = tmp_path / "app.ts"
    test_file.write_text(
        "function step2(db, data) {\n"
        "    db.execute(data);\n"
        "}\n"
        "\n"
        "function step1(db, value) {\n"
        "    step2(db, value);\n"
        "}\n"
        "\n"
        "function handler(req, db) {\n"
        '    let name = req.query.get("name");\n'
        "    step1(db, name);\n"
        "}\n"
    )

    config = _make_ts_interproc_config()
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")

    # No crash, valid result object.
    assert isinstance(result, InterproceduralResult)

    # All three functions should have been summarised.
    qns = list(result.summaries.keys())
    assert any("step2" in qn for qn in qns), f"step2 not summarised; got: {qns}"
    assert any("step1" in qn for qn in qns), f"step1 not summarised; got: {qns}"
    assert any("handler" in qn for qn in qns), f"handler not summarised; got: {qns}"

    # The leaf function (step2) should record 'data' flows to the sink.
    step2_summary = next(s for qn, s in result.summaries.items() if qn.endswith("::step2"))
    assert "data" in step2_summary.param_to_sink, (
        f"Expected step2 'data' param to flow to sink, got: {step2_summary.param_to_sink}"
    )

    # step1 should gain transitive param_to_sink via step2.
    step1_summary = next(s for qn, s in result.summaries.items() if qn.endswith("::step1"))
    assert "value" in step1_summary.param_to_sink, (
        f"Expected step1 'value' to have transitive param_to_sink, got: {step1_summary.param_to_sink}"
    )

    # The 3-hop chain should now be detected.
    assert len(result.violations) >= 1, (
        f"Expected at least 1 violation for 3-hop chain, got {len(result.violations)}"
    )
    assert all(v.label == "user_input" for v in result.violations)
