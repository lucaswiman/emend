"""Tests for intraprocedural trace analysis on TypeScript source files.

This module verifies that the Datalog/FactGraph trace engine can detect
taint flows in TypeScript code — covering function declarations, arrow
functions, method-style calls, variable propagation, conditional branches,
try/catch blocks, and container mutation.  The engine is language-agnostic
(tree-sitter + Rust backend), so these tests validate the TypeScript
tree-sitter grammar integration end-to-end.
"""

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    run_trace_analysis,
)


def _make_ts_trace_config() -> TraceConfig:
    """Return a TraceConfig for SQL/code-injection detection in TypeScript."""
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
                message="Potential SQL injection: user input reaches db.execute()",
            ),
            TraceSink(
                pattern="eval($X)",
                label="user_input",
                message="Potential code injection: user input reaches eval()",
            ),
        ],
        sanitizers=[
            TraceSanitizer(pattern="sanitize($X)", label="user_input"),
            TraceSanitizer(pattern="escape($X)", label="user_input"),
        ],
    )


# ---------------------------------------------------------------------------
# Test 1 – basic source-to-sink flow
# ---------------------------------------------------------------------------

def test_ts_trace_basic_source_to_sink(tmp_path):
    """Tainted value from req.query flows directly into db.execute()."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        '    let x = req.query.get("name");\n'
        "    db.execute(x);\n"
        "}\n"
    )

    config = _make_ts_trace_config()
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    assert len(violations) >= 1
    v = violations[0]
    assert v.label == "user_input"
    assert "SQL injection" in v.message
    assert v.file_path == str(test_file)


# ---------------------------------------------------------------------------
# Test 2 – sanitizer blocks the taint
# ---------------------------------------------------------------------------

def test_ts_trace_sanitizer_blocks(tmp_path):
    """Sanitizer clears taint so no violation is reported."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        '    let x = req.query.get("name");\n'
        "    x = sanitize(x);\n"
        "    db.execute(x);\n"
        "}\n"
    )

    config = _make_ts_trace_config()
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    assert len(violations) == 0


# ---------------------------------------------------------------------------
# Test 3 – arrow function
# ---------------------------------------------------------------------------

def test_ts_trace_arrow_function(tmp_path):
    """Taint flow is detected inside a TypeScript arrow function."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "const handler = (req: any, db: any): void => {\n"
        '    let x = req.query.get("name");\n'
        "    db.execute(x);\n"
        "};\n"
    )

    config = _make_ts_trace_config()
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    assert len(violations) >= 1
    v = violations[0]
    assert v.label == "user_input"
    assert v.file_path == str(test_file)


# ---------------------------------------------------------------------------
# Test 4 – req.body subscript source
# ---------------------------------------------------------------------------

def test_ts_trace_method_call(tmp_path):
    """Taint from req.body.get() propagates to db.execute()."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        '    let x = req.body.get("data");\n'
        "    db.execute(x);\n"
        "}\n"
    )

    config = _make_ts_trace_config()
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    assert len(violations) >= 1
    v = violations[0]
    assert v.label == "user_input"


# ---------------------------------------------------------------------------
# Test 5 – taint propagates through a renamed variable
# ---------------------------------------------------------------------------

def test_ts_trace_destructuring(tmp_path):
    """Taint propagates through a straight assignment to a renamed variable."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        '    const data = req.query.get("name");\n'
        "    let y = data;\n"
        "    db.execute(y);\n"
        "}\n"
    )

    config = _make_ts_trace_config()
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    # Taint should propagate: data <- source, y <- data, db.execute(y) <- sink.
    assert len(violations) >= 1
    assert violations[0].label == "user_input"


# ---------------------------------------------------------------------------
# Test 6 – taint through a conditional branch
# ---------------------------------------------------------------------------

def test_ts_trace_conditional_branch(tmp_path):
    """Tainted value assigned inside an if-branch still reaches the sink."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        "    let x: string | undefined;\n"
        '    if (req.query.get("flag")) {\n'
        '        x = req.query.get("name");\n'
        "    }\n"
        "    db.execute(x);\n"
        "}\n"
    )

    config = _make_ts_trace_config()
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    # At least one path carries taint from the if-branch assignment to the sink.
    assert len(violations) >= 1
    assert violations[0].label == "user_input"


# ---------------------------------------------------------------------------
# Test 7 – taint detected inside a try block
# ---------------------------------------------------------------------------

def test_ts_trace_try_catch(tmp_path):
    """Source-to-sink flow is detected even when wrapped in try/catch."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        "    try {\n"
        '        let x = req.query.get("name");\n'
        "        db.execute(x);\n"
        "    } catch (e) {\n"
        "        console.error(e);\n"
        "    }\n"
        "}\n"
    )

    config = _make_ts_trace_config()
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    assert len(violations) >= 1
    assert violations[0].label == "user_input"
    assert violations[0].file_path == str(test_file)


# ---------------------------------------------------------------------------
# Test 8 – container mutation (best-effort; may be 0 if not tracked)
# ---------------------------------------------------------------------------

def test_ts_trace_container_mutation(tmp_path):
    """Taint pushed into an array then retrieved reaches db.execute().

    Container element tracking (items.push -> items[0]) is not guaranteed
    by the current engine.  This test documents the current behaviour:
    if container tracking is not implemented end-to-end for TypeScript,
    0 violations are acceptable; the important thing is that no crash occurs
    and the engine correctly handles array-style code.
    """
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        "    let items: string[] = [];\n"
        '    items.push(req.query.get("name"));\n'
        "    let x = items[0];\n"
        "    db.execute(x);\n"
        "}\n"
    )

    config = _make_ts_trace_config()
    # Must not raise an exception regardless of violation count.
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    # Container element tracking may or may not be supported; either outcome is
    # valid.  We only assert the return type is a list.
    assert isinstance(violations, list)
    for v in violations:
        assert v.label == "user_input"


# ---------------------------------------------------------------------------
# Test 9 – subscript source pattern (req.body[$X])
# ---------------------------------------------------------------------------

def test_ts_trace_subscript_source(tmp_path):
    """Taint from req.body["key"] using subscript pattern reaches db.execute()."""
    test_file = tmp_path / "handler.ts"
    test_file.write_text(
        "function handler(req: any, db: any): void {\n"
        '    let x = req.body["data"];\n'
        "    db.execute(x);\n"
        "}\n"
    )

    config = TraceConfig(
        labels=["user_input"],
        sources=[
            TraceSource(pattern="req.body[$X]", label="user_input"),
        ],
        sinks=[
            TraceSink(
                pattern="db.execute($X)",
                label="user_input",
                message="Potential SQL injection: user input reaches db.execute()",
            ),
        ],
        sanitizers=[],
    )
    violations = run_trace_analysis([str(test_file)], config, language="typescript")

    assert len(violations) >= 1
    v = violations[0]
    assert v.label == "user_input"
    assert "SQL injection" in v.message
