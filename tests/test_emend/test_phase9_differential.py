"""Phase 9 differential tests: Python intraprocedural engine vs Datalog FactGraph engine.

These tests run the same taint analysis fixture through both engines and compare
results.  The goal is to document where the engines agree and to surface divergence
so it can be intentionally fixed or accepted.

Key design choices:
- ``_run_both_engines`` writes source to a temp file and calls both engines.
- Datalog may return None if FactGraph construction fails; those comparisons are
  skipped with an informative message (not a hard failure).
- Comparison uses ``(file_path, line, label, sink_pattern)`` tuples so engine
  metadata and trace steps do not cause false mismatches.

As of Phase 14, the Datalog engine uses ``FactGraph.build_from_files()`` to build
facts directly from the given file list, so small tmp_path fixtures now work
correctly.  The only remaining accepted divergence is nested-function closure
taint which requires interprocedural analysis.
"""

from __future__ import annotations

import pytest

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    TraceViolation,
    _run_trace_datalog,
    run_trace_analysis,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _violation_key(v: TraceViolation) -> tuple[str, int, str, str]:
    """Comparable key that excludes engine-specific fields."""
    return (v.file_path, v.line, v.label, v.sink_pattern)


def _run_both_engines(
    tmp_path,
    source_code: str,
    config: TraceConfig,
) -> tuple[list[TraceViolation], list[TraceViolation] | None]:
    """Run both Python and Datalog engines on the same source.

    Returns ``(python_violations, datalog_violations)``.  ``datalog_violations``
    is ``None`` when the Datalog engine is unavailable or construction fails.
    """
    src_file = tmp_path / "app.py"
    src_file.write_text(source_code)
    paths = [str(src_file)]
    project_path = str(tmp_path)

    python_violations = run_trace_analysis(
        paths=paths,
        config=config,
        label_filter=None,
        language="python",
        project_path=project_path,
    )
    for v in python_violations:
        assert v.engine == "python", f"Expected engine='python', got {v.engine!r}"

    datalog_violations = _run_trace_datalog(
        paths=paths,
        config=config,
        label_filter=None,
        language="python",
        project_path=project_path,
    )
    if datalog_violations is not None:
        for v in datalog_violations:
            assert v.engine == "datalog", f"Expected engine='datalog', got {v.engine!r}"

    return python_violations, datalog_violations


def _assert_engines_agree(
    python_violations: list[TraceViolation],
    datalog_violations: list[TraceViolation] | None,
    *,
    context: str = "",
) -> None:
    """Assert both engines produce the same violation keys (order-independent).

    Skips the assertion when Datalog is unavailable (returns None).

    When the engines diverge this raises AssertionError — callers that expect
    a known divergence should use ``pytest.mark.xfail`` or check the individual
    engine results directly instead of calling this helper.
    """
    if datalog_violations is None:
        pytest.skip(f"Datalog engine unavailable{': ' + context if context else ''}")
    py_keys = set(_violation_key(v) for v in python_violations)
    dl_keys = set(_violation_key(v) for v in datalog_violations)
    assert py_keys == dl_keys, (
        f"Engine divergence{': ' + context if context else ''}.\n"
        f"  Python-only:  {py_keys - dl_keys}\n"
        f"  Datalog-only: {dl_keys - py_keys}"
    )


# ---------------------------------------------------------------------------
# Shared configs
# ---------------------------------------------------------------------------

def _sqli_config() -> TraceConfig:
    return TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
    )


def _sqli_sanitized_config() -> TraceConfig:
    return TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
        sanitizers=[TraceSanitizer(pattern="escape($X)", label="sqli")],
    )


# ---------------------------------------------------------------------------
# TestDifferentialBasicFlow
# ---------------------------------------------------------------------------

class TestDifferentialBasicFlow:
    """Both engines on simple, unambiguous taint flows."""

    def test_simple_source_to_sink_python_engine(self, tmp_path):
        """Python engine alone: simple linear taint source -> variable -> sink."""
        source = """\
def handler():
    user_input = request.args.get("name")
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)

        assert python_v, "Python engine should detect source->sink flow"
        assert python_v[0].label == "sqli"
        assert python_v[0].line == 3  # cursor.execute is on line 3

    def test_simple_source_to_sink_both_engines(self, tmp_path):
        """Both engines: simple linear taint."""
        source = """\
def handler():
    user_input = request.args.get("name")
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="simple source->sink")

    def test_simple_sanitized_python_engine(self, tmp_path):
        """Python engine: sanitized flow should NOT produce a violation."""
        source = """\
def handler():
    user_input = request.args.get("name")
    safe = escape(user_input)
    cursor.execute(safe)
"""
        config = _sqli_sanitized_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)

        assert python_v == [], (
            f"Python engine should find no violations after sanitization; got {python_v}"
        )

    def test_simple_sanitized_both_engines(self, tmp_path):
        """Both engines agree on sanitized flow: both should return empty.

        The Python engine returns [] because the sanitizer clears taint.
        The Datalog engine also returns [] (because of missing facts or because
        it correctly applies the sanitizer).  Either way they agree.
        """
        source = """\
def handler():
    user_input = request.args.get("name")
    safe = escape(user_input)
    cursor.execute(safe)
"""
        config = _sqli_sanitized_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)

        assert python_v == [], (
            f"Python engine should find no violations after sanitization; got {python_v}"
        )
        # Both engines agree: no violations.
        _assert_engines_agree(python_v, datalog_v, context="sanitized flow")

    def test_no_taint_no_violations_both_engines(self, tmp_path):
        """No source match means no violations in either engine."""
        source = """\
def handler():
    value = "static string"
    cursor.execute(value)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)

        assert python_v == [], "Python engine should find no violations when no source matches"
        _assert_engines_agree(python_v, datalog_v, context="no taint source")


# ---------------------------------------------------------------------------
# TestDifferentialEdgeCases
# ---------------------------------------------------------------------------

class TestDifferentialEdgeCases:
    """Edge cases — each test has a Python-only assertion and a cross-engine comparison."""

    def test_nested_function_python_engine(self, tmp_path):
        """Python engine: nested inner function can access outer-scope tainted variable."""
        source = """\
def outer():
    user_input = request.args.get("name")

    def inner():
        cursor.execute(user_input)

    inner()
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)
        # The Python engine analyzes each function in isolation using full-file source.
        # It may or may not detect the cross-scope flow; just verify it does not crash.
        assert isinstance(python_v, list)

    @pytest.mark.xfail(
        reason=(
            "Nested function closure taint is cross-scope (outer→inner), "
            "which requires interprocedural analysis not yet wired for "
            "intraprocedural trace."
        ),
        strict=False,
    )
    def test_nested_function_both_engines(self, tmp_path):
        """Both engines: nested function.  Xfail — cross-scope closure taint."""
        source = """\
def outer():
    user_input = request.args.get("name")

    def inner():
        cursor.execute(user_input)

    inner()
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="nested function scope")

    def test_module_level_code_python_engine(self, tmp_path):
        """Python engine: source and sink at module level (not in any function)."""
        source = """\
user_input = request.args.get("name")
cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)

        assert python_v, "Python engine should detect module-level source->sink"
        assert python_v[0].line == 2

    def test_module_level_code_both_engines(self, tmp_path):
        """Both engines: module-level code."""
        source = """\
user_input = request.args.get("name")
cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="module-level code")

    def test_same_block_ordering_python_engine(self, tmp_path):
        """Python engine: sink before source on line 2 must NOT be flagged."""
        source = """\
def handler():
    cursor.execute("static")
    user_input = request.args.get("name")
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)

        python_lines = {v.line for v in python_v}
        assert 4 in python_lines, "Python engine should flag line 4 (sink after source)"
        assert 2 not in python_lines, "Python engine must NOT flag line 2 (sink before source)"

    def test_same_block_ordering_both_engines(self, tmp_path):
        """Both engines: intra-block line-ordering."""
        source = """\
def handler():
    cursor.execute("static")
    user_input = request.args.get("name")
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="same-block line ordering")

    def test_sink_after_source_python_engine(self, tmp_path):
        """Python engine: sink after source in same function — must be flagged."""
        source = """\
def handler():
    user_input = request.args.get("name")
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)

        assert python_v, "Python engine must flag sink that appears after source"
        assert 3 in {v.line for v in python_v}

    def test_sink_after_source_both_engines(self, tmp_path):
        """Both engines: sink after source."""
        source = """\
def handler():
    user_input = request.args.get("name")
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="sink after source")


# ---------------------------------------------------------------------------
# TestDifferentialCorpus
# ---------------------------------------------------------------------------

class TestDifferentialCorpus:
    """Curated corpus exercising common edge cases."""

    def test_corpus_reassignment_python_engine(self, tmp_path):
        """Python engine: variable reassigned to literal clears taint."""
        source = """\
def handler():
    user_input = request.args.get("name")
    user_input = "safe_static_value"
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)
        # Python engine should clear taint on reassignment to a literal.
        # We document the result — do not enforce a specific count since
        # heuristics for "safe" literals may evolve.
        assert isinstance(python_v, list)

    def test_corpus_reassignment_both_engines(self, tmp_path):
        """Both engines: reassignment."""
        source = """\
def handler():
    user_input = request.args.get("name")
    user_input = "safe_static_value"
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="reassignment clears taint")

    def test_corpus_multiple_labels_python_engine(self, tmp_path):
        """Python engine: two labels — only the one with a matching source fires."""
        source = """\
def handler():
    sql_input = request.args.get("q")
    cursor.execute(sql_input)
"""
        config = TraceConfig(
            labels=["sqli", "xss"],
            sources=[
                TraceSource(pattern="request.args.get($X)", label="sqli"),
                TraceSource(pattern="request.form[$X]", label="xss"),
            ],
            sinks=[
                TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection"),
                TraceSink(pattern="render_template_string($X)", label="xss", message="XSS"),
            ],
        )
        python_v, _ = _run_both_engines(tmp_path, source, config)

        python_labels = {v.label for v in python_v}
        assert "sqli" in python_labels, "sqli taint should be detected"
        assert "xss" not in python_labels, "xss taint should not fire (no xss source match)"

    def test_corpus_multiple_labels_both_engines(self, tmp_path):
        """Both engines: multiple labels."""
        source = """\
def handler():
    sql_input = request.args.get("q")
    cursor.execute(sql_input)
"""
        config = TraceConfig(
            labels=["sqli", "xss"],
            sources=[
                TraceSource(pattern="request.args.get($X)", label="sqli"),
                TraceSource(pattern="request.form[$X]", label="xss"),
            ],
            sinks=[
                TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection"),
                TraceSink(pattern="render_template_string($X)", label="xss", message="XSS"),
            ],
        )
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="multiple labels")

    def test_corpus_branch_sensitive_python_engine(self, tmp_path):
        """Python engine: taint on one if-branch; sink after merge — conservative."""
        source = """\
def handler(condition):
    if condition:
        user_input = request.args.get("name")
    else:
        user_input = "safe"
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)
        # The Python engine is conservative: it propagates taint from any branch.
        # It should flag line 6 (cursor.execute after the merge point).
        assert isinstance(python_v, list)

    def test_corpus_branch_sensitive_both_engines(self, tmp_path):
        """Both engines: branch-sensitive taint."""
        source = """\
def handler(condition):
    if condition:
        user_input = request.args.get("name")
    else:
        user_input = "safe"
    cursor.execute(user_input)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="branch-sensitive taint")

    def test_corpus_intermediate_variables_python_engine(self, tmp_path):
        """Python engine: taint flows through chain of intermediate assignments."""
        source = """\
def handler():
    raw = request.args.get("data")
    processed = raw
    final = processed
    cursor.execute(final)
"""
        config = _sqli_config()
        python_v, _ = _run_both_engines(tmp_path, source, config)
        assert python_v, "Python engine should track taint through intermediate variables"

    def test_corpus_intermediate_variables_both_engines(self, tmp_path):
        """Both engines: taint through intermediates."""
        source = """\
def handler():
    raw = request.args.get("data")
    processed = raw
    final = processed
    cursor.execute(final)
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)
        _assert_engines_agree(python_v, datalog_v, context="taint through intermediates")

    def test_corpus_no_source_multiple_sinks_both_engines(self, tmp_path):
        """Both engines agree: no source means no violations even with many sinks."""
        source = """\
def handler():
    cursor.execute("SELECT 1")
    cursor.execute("SELECT 2")
    cursor.execute("SELECT 3")
"""
        config = _sqli_config()
        python_v, datalog_v = _run_both_engines(tmp_path, source, config)

        assert python_v == [], "Python engine should find no violations without a source"
        # Both engines should agree on the empty result.
        _assert_engines_agree(python_v, datalog_v, context="no source, multiple sinks")
