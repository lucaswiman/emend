"""CLI integration tests for interprocedural trace analysis with TypeScript and Rust.

These tests verify that ``run_interprocedural_trace()`` correctly routes taint
analysis through the language parameter for TypeScript (``.ts``) and Rust
(``.rs``) source files.  They serve as lightweight smoke tests confirming the
end-to-end path — source detection, function summary computation, and
cross-function violation reporting — works for non-Python languages.
"""

from emend.trace import (
    InterproceduralResult,
    TraceConfig,
    TraceSink,
    TraceSource,
    run_interprocedural_trace,
)


def test_interprocedural_ts_via_language_param(tmp_path):
    """Cross-function SQL injection taint flow detected in TypeScript via language param.

    ``handler`` reads user input via ``req.query.get()``, then passes it to
    ``runQuery`` which calls ``db.execute()``.  The interprocedural engine must
    propagate taint across the function boundary and report a violation.
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
    config = TraceConfig(
        labels=["user_input"],
        sources=[TraceSource(pattern="req.query.get($X)", label="user_input")],
        sinks=[TraceSink(pattern="db.execute($X)", label="user_input", message="SQL injection")],
        sanitizers=[],
    )
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")
    assert isinstance(result, InterproceduralResult)
    assert len(result.violations) >= 1, (
        f"Expected at least 1 violation for TypeScript interprocedural flow, "
        f"got 0. Summaries: {list(result.summaries.keys())}"
    )


def test_interprocedural_rust_via_language_param(tmp_path):
    """Cross-function SQL injection taint flow detected in Rust via language param.

    ``handler`` reads user input via ``get_input()``, then passes it to
    ``run_query`` which calls ``execute_query()``.  The interprocedural engine
    must propagate taint across the function boundary and report a violation.
    """
    test_file = tmp_path / "app.rs"
    test_file.write_text(
        "fn run_query(query: String) {\n"
        "    execute_query(query);\n"
        "}\n"
        "\n"
        "fn handler() {\n"
        '    let name = get_input("name");\n'
        "    run_query(name);\n"
        "}\n"
    )
    config = TraceConfig(
        labels=["user_input"],
        sources=[TraceSource(pattern="get_input($X)", label="user_input")],
        sinks=[TraceSink(pattern="execute_query($X)", label="user_input", message="SQL injection")],
        sanitizers=[],
    )
    result = run_interprocedural_trace([str(test_file)], config, language="rust")
    assert isinstance(result, InterproceduralResult)
    assert len(result.violations) >= 1, (
        f"Expected at least 1 violation for Rust interprocedural flow, "
        f"got 0. Summaries: {list(result.summaries.keys())}"
    )


def test_interprocedural_json_output_has_summaries_ts(tmp_path):
    """Function summaries are populated for both helper and handler in TypeScript.

    Verifies that ``InterproceduralResult.summaries`` contains entries for
    each named function discovered in the file, which is required for correct
    interprocedural analysis and JSON output.
    """
    test_file = tmp_path / "app.ts"
    test_file.write_text(
        "function helper(value) {\n"
        "    return value;\n"
        "}\n"
        "\n"
        "function handler(req, db) {\n"
        '    let name = req.query.get("name");\n'
        "    let q = helper(name);\n"
        "    db.execute(q);\n"
        "}\n"
    )
    config = TraceConfig(
        labels=["user_input"],
        sources=[TraceSource(pattern="req.query.get($X)", label="user_input")],
        sinks=[TraceSink(pattern="db.execute($X)", label="user_input", message="SQL injection")],
        sanitizers=[],
    )
    result = run_interprocedural_trace([str(test_file)], config, language="typescript")
    assert isinstance(result, InterproceduralResult)
    assert len(result.summaries) > 0, "Expected at least one function summary"
    summary_keys = list(result.summaries.keys())
    assert any("helper" in k for k in summary_keys), (
        f"Expected 'helper' in summary keys, got: {summary_keys}"
    )
    assert any("handler" in k for k in summary_keys), (
        f"Expected 'handler' in summary keys, got: {summary_keys}"
    )
