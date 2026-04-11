"""Tests for interprocedural taint analysis."""

import pytest

from emend.trace import (
    FunctionSummary,
    InterproceduralResult,
    TraceConfig,
    TraceSanitizer,
    TraceSink,
    TraceSource,
    _collect_function_params,
    _compute_function_summary,
    run_interprocedural_trace,
)


def _make_sql_config():
    """Reusable SQL injection taint config."""
    return TraceConfig(
        labels=["user_input"],
        sources=[
            TraceSource(pattern="request.args.get($X)", label="user_input"),
        ],
        sinks=[
            TraceSink(
                pattern="cursor.execute($X)",
                label="user_input",
                message="SQL injection: user input reaches cursor.execute()",
            ),
        ],
        sanitizers=[
            TraceSanitizer(pattern="escape($X)", label="user_input"),
        ],
    )


class TestCollectFunctionParams:
    def test_simple_params(self, tmp_path):
        source = "def foo(a, b, c):\n    pass\n"
        params = _collect_function_params(source, 1, 2)
        assert params == ["a", "b", "c"]

    def test_params_with_defaults(self, tmp_path):
        source = "def foo(a, b=1, c='x'):\n    pass\n"
        params = _collect_function_params(source, 1, 2)
        assert params == ["a", "b", "c"]

    def test_params_with_annotations(self, tmp_path):
        source = "def foo(a: int, b: str = 'x'):\n    pass\n"
        params = _collect_function_params(source, 1, 2)
        assert params == ["a", "b"]

    def test_skip_self_cls(self, tmp_path):
        source = "def method(self, a, b):\n    pass\n"
        params = _collect_function_params(source, 1, 2)
        assert params == ["a", "b"]

    def test_star_args(self, tmp_path):
        source = "def foo(a, *args, **kwargs):\n    pass\n"
        params = _collect_function_params(source, 1, 2)
        assert "a" in params
        assert "args" in params
        assert "kwargs" in params

    def test_no_params(self, tmp_path):
        source = "def foo():\n    pass\n"
        params = _collect_function_params(source, 1, 2)
        assert params == []

    def test_async_def(self, tmp_path):
        source = "async def foo(a, b):\n    pass\n"
        params = _collect_function_params(source, 1, 2)
        assert params == ["a", "b"]


class TestComputeFunctionSummary:
    def test_param_to_return(self, tmp_path):
        """Parameter that flows to return value via assignment."""
        test_file = tmp_path / "test.py"
        source = "def identity(x):\n    result = x\n    return result\n"
        test_file.write_text(source)

        config = _make_sql_config()
        summary = _compute_function_summary(
            file_path=str(test_file),
            source=source,
            func_start=1,
            func_end=3,
            config=config,
            func_qn="test::identity",
            param_names=["x"],
        )
        assert "x" in summary.param_to_return
        assert "user_input" in summary.param_to_return["x"]

    def test_param_to_sink(self, tmp_path):
        """Parameter that flows to a sink."""
        test_file = tmp_path / "test.py"
        source = (
            "def run_query(cursor, query):\n"
            "    cursor.execute(query)\n"
        )
        test_file.write_text(source)

        config = _make_sql_config()
        summary = _compute_function_summary(
            file_path=str(test_file),
            source=source,
            func_start=1,
            func_end=2,
            config=config,
            func_qn="test::run_query",
            param_names=["cursor", "query"],
        )
        assert "query" in summary.param_to_sink
        assert any(s[0] == "user_input" for s in summary.param_to_sink["query"])

    def test_no_flow(self, tmp_path):
        """Parameter that doesn't flow to return or sink."""
        test_file = tmp_path / "test.py"
        source = "def foo(x):\n    return 42\n"
        test_file.write_text(source)

        config = _make_sql_config()
        summary = _compute_function_summary(
            file_path=str(test_file),
            source=source,
            func_start=1,
            func_end=2,
            config=config,
            func_qn="test::foo",
            param_names=["x"],
        )
        assert "x" not in summary.param_to_return

    def test_param_to_sink_respects_statement_order(self, tmp_path):
        """A later assignment must not taint an earlier sink in the summary."""
        test_file = tmp_path / "test.py"
        source = (
            "def run_query(cursor, query):\n"
            "    cursor.execute(sql)\n"
            "    sql = query\n"
        )
        test_file.write_text(source)

        config = _make_sql_config()
        summary = _compute_function_summary(
            file_path=str(test_file),
            source=source,
            func_start=1,
            func_end=3,
            config=config,
            func_qn="test::run_query",
            param_names=["cursor", "query"],
        )
        assert "query" not in summary.param_to_sink


class TestInterproceduralAnalysis:
    def test_nested_same_named_helpers_are_scoped_to_their_owner(self, tmp_path):
        """Sibling nested helpers should not share summaries by short name."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def outer_b(request):\n"
            "    def helper(value):\n"
            "        return value\n"
            "    name = request.args.get('name')\n"
            "    helper(name)\n"
            "\n"
            "def outer_a(request):\n"
            "    def helper(value):\n"
            "        sink(value)\n"
            "    return request.args.get('name')\n"
        )

        config = TraceConfig(
            labels=["user_input"],
            sources=[
                TraceSource(pattern="request.args.get($X)", label="user_input"),
            ],
            sinks=[
                TraceSink(
                    pattern="sink($X)",
                    label="user_input",
                    message="Nested helper sink reached",
                ),
            ],
        )

        result = run_interprocedural_trace([str(test_file)], config)

        assert result.violations == []
        assert f"{test_file}::outer_a::helper" in result.summaries
        assert f"{test_file}::outer_b::helper" in result.summaries

    def test_unrelated_functions_do_not_report_violation(self, tmp_path):
        """A source in one function should not taint an unrelated sink."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def read_name(request):\n"
            "    name = request.args.get('name')\n"
            "    return name\n"
            "\n"
            "def run_query(cursor):\n"
            "    name = 'SELECT 1'\n"
            "    cursor.execute(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        assert result.violations == []

    def test_callee_return_taint_reaches_caller_sink(self, tmp_path):
        """Taint returned from a helper should remain tainted in the caller."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def passthrough(value):\n"
            "    return value\n"
            "\n"
            "def handle_request(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    query = passthrough(name)\n"
            "    cursor.execute(query)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        assert len(result.violations) >= 1
        assert any("SQL injection" in v.message for v in result.violations)

    def test_cross_function_violation(self, tmp_path):
        """Taint flows from source through a helper function to a sink."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def run_query(cursor, query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def handle_request(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    run_query(cursor, name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        assert isinstance(result, InterproceduralResult)
        assert len(result.summaries) > 0

        assert len(result.violations) >= 1
        messages = [v.message for v in result.violations]
        assert any("SQL injection" in m or "cursor.execute" in m.lower() for m in messages)
        assert all(v.engine == "datalog" for v in result.violations)

    def test_intraprocedural_still_found(self, tmp_path):
        """Interprocedural mode still finds direct (intraprocedural) violations."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle_request(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        assert len(result.violations) >= 1
        assert result.violations[0].label == "user_input"

    def test_sanitizer_blocks_interprocedural(self, tmp_path):
        """Sanitizer in caller prevents interprocedural violation."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def run_query(cursor, query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def handle_request(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    name = escape(name)\n"
            "    run_query(cursor, name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        # After sanitization, the interprocedural violation should not appear
        interprocedural_violations = [
            v for v in result.violations
            if "via" in v.message.lower() or "function call" in v.message.lower()
        ]
        assert len(interprocedural_violations) == 0

    def test_late_sanitizer_does_not_erase_earlier_interprocedural_violation(self, tmp_path):
        """A sanitizer after the call site must not retroactively suppress it."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def run_query(cursor, query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def handle_request(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    run_query(cursor, name)\n"
            "    name = escape(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        interprocedural_violations = [
            v for v in result.violations
            if "via" in v.message.lower() or "function call" in v.message.lower()
        ]
        assert len(interprocedural_violations) >= 1

    def test_empty_files(self, tmp_path):
        """Handles empty file list gracefully."""
        config = _make_sql_config()
        result = run_interprocedural_trace([], config)
        assert result.violations == []
        assert result.summaries == {}

    def test_no_sources(self, tmp_path):
        """Returns empty result when no sources configured."""
        config = TraceConfig(labels=["x"], sources=[], sinks=[
            TraceSink(pattern="sink($X)", label="x", message="test"),
        ])
        result = run_interprocedural_trace(["/nonexistent"], config)
        assert result.violations == []

    def test_label_filter(self, tmp_path):
        """Label filter restricts analysis to specific label."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace(
            [str(test_file)], config, label_filter="user_input",
        )
        assert len(result.violations) >= 1
        assert all(v.engine == "datalog" for v in result.violations)

        result_no_match = run_interprocedural_trace(
            [str(test_file)], config, label_filter="nonexistent",
        )
        assert len(result_no_match.violations) == 0


class TestInterproceduralCozoEscaping:
    """Bug #2: Interprocedural trace crashes with CozoDB parse error when variable
    names contain special characters (e.g. subscript captures like GET["id"])."""

    def test_subscript_in_sink_does_not_crash(self, tmp_path):
        """Crash when a sink pattern captures a subscript expression with double quotes."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def process(request):\n"
            '    execute(request.GET["id"])\n'
        )

        config = TraceConfig(
            labels=["user_input"],
            sources=[
                TraceSource(pattern="request.GET[$X]", label="user_input"),
            ],
            sinks=[
                TraceSink(
                    pattern="execute($X)",
                    label="user_input",
                    message="Untrusted data reaches execute()",
                ),
            ],
        )

        result = run_interprocedural_trace([str(test_file)], config)
        assert isinstance(result, InterproceduralResult)

    def test_multi_function_subscript_in_sink_does_not_crash(self, tmp_path):
        """Multi-function file with subscript notation doesn't crash."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def get_data(request):\n"
            '    data = request.GET["id"]\n'
            "    return data\n"
            "\n"
            "def process(request):\n"
            "    val = get_data(request)\n"
            "    execute(val)\n"
        )

        config = TraceConfig(
            labels=["user_input"],
            sources=[
                TraceSource(pattern="request.GET[$X]", label="user_input"),
            ],
            sinks=[
                TraceSink(
                    pattern="execute($X)",
                    label="user_input",
                    message="Untrusted data reaches execute()",
                ),
            ],
        )

        result = run_interprocedural_trace([str(test_file)], config)
        assert isinstance(result, InterproceduralResult)

    def test_single_quoted_subscript_in_sink_does_not_crash(self, tmp_path):
        """Single-quoted subscript keys also don't crash."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def process(request, db):\n"
            "    db.execute(request.POST['username'])\n"
        )

        config = TraceConfig(
            labels=["user_input"],
            sources=[
                TraceSource(pattern="request.POST[$X]", label="user_input"),
            ],
            sinks=[
                TraceSink(
                    pattern="db.execute($X)",
                    label="user_input",
                    message="Untrusted data reaches db.execute()",
                ),
            ],
        )

        result = run_interprocedural_trace([str(test_file)], config)
        assert isinstance(result, InterproceduralResult)


class TestMultiHopChainPropagation:
    """Tests for transitive param_to_sink propagation across 3+ function hops."""

    def test_three_hop_chain_detects_violation(self, tmp_path):
        """handler() -> step1() -> step2() -> cursor.execute() is detected.

        Previously this was a known limitation: param_to_sink was only
        computed per-function and never composed transitively.  The engine
        now uses a recursive Datalog rule to propagate param_to_sink through
        call chains of arbitrary depth.
        """
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def step2(query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def step1(data):\n"
            "    step2(data)\n"
            "\n"
            "def handler():\n"
            "    name = request.args.get('name')\n"
            "    step1(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)
        assert isinstance(result, InterproceduralResult)
        assert len(result.violations) >= 1, (
            f"Expected at least 1 violation for 3-hop chain, got {len(result.violations)}"
        )
        assert any(v.label == "user_input" for v in result.violations)

    def test_four_hop_chain_detects_violation(self, tmp_path):
        """handler() -> a() -> b() -> c() -> cursor.execute() is detected."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def c(query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def b(data):\n"
            "    c(data)\n"
            "\n"
            "def a(val):\n"
            "    b(val)\n"
            "\n"
            "def handler():\n"
            "    name = request.args.get('name')\n"
            "    a(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)
        assert isinstance(result, InterproceduralResult)
        assert len(result.violations) >= 1, (
            f"Expected at least 1 violation for 4-hop chain, got {len(result.violations)}"
        )

    def test_three_hop_chain_with_sanitizer(self, tmp_path):
        """A sanitizer in the middle of a 3-hop chain should suppress the violation."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def step2(query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def step1(data):\n"
            "    safe = escape(data)\n"
            "    step2(safe)\n"
            "\n"
            "def handler():\n"
            "    name = request.args.get('name')\n"
            "    step1(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)
        assert isinstance(result, InterproceduralResult)
        # The sanitizer in step1 should prevent taint from reaching step2's sink.
        # step1's summary should NOT record param_to_sink for 'data' since
        # the taint is sanitized before being passed to step2.
        unsanitized = [v for v in result.violations if "step1" in v.message or "step2" in v.message]
        assert len(unsanitized) == 0, (
            f"Expected sanitizer to suppress violation, but got: {unsanitized}"
        )

    def test_step1_gains_transitive_param_to_sink(self, tmp_path):
        """step1 should gain a transitive param_to_sink entry via step2."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def leaf(query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def mid(data):\n"
            "    leaf(data)\n"
            "\n"
            "def top():\n"
            "    name = request.args.get('name')\n"
            "    mid(name)\n"
        )

        config = _make_sql_config()
        result = run_interprocedural_trace([str(test_file)], config)

        # leaf has direct param_to_sink
        leaf_summary = next(
            s for qn, s in result.summaries.items() if qn.endswith("::leaf")
        )
        assert "query" in leaf_summary.param_to_sink

        # mid should now also have transitive param_to_sink
        mid_summary = next(
            s for qn, s in result.summaries.items() if qn.endswith("::mid")
        )
        assert "data" in mid_summary.param_to_sink, (
            f"Expected mid 'data' to have transitive param_to_sink, "
            f"got: {mid_summary.param_to_sink}"
        )

    def test_max_chain_depth_limits_propagation(self, tmp_path):
        """max_chain_depth=1 should only detect direct callee sinks, not transitive."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def step2(query):\n"
            "    cursor.execute(query)\n"
            "\n"
            "def step1(data):\n"
            "    step2(data)\n"
            "\n"
            "def handler():\n"
            "    name = request.args.get('name')\n"
            "    step1(name)\n"
        )

        config = _make_sql_config()
        # With depth=1, step1's transitive sink through step2 should NOT
        # be propagated, so handler -> step1 should not produce a violation
        # (but handler directly calling step2 would still work).
        result = run_interprocedural_trace(
            [str(test_file)], config, max_chain_depth=1,
        )
        assert isinstance(result, InterproceduralResult)
        # Only violations through direct callee sinks (depth 1) should appear.
        # handler -> step1 is depth 2 (step1 -> step2 -> sink), so no violation.
        chain_violations = [
            v for v in result.violations
            if "step1" in (v.message or "") or "step2" in (v.message or "")
        ]
        assert len(chain_violations) == 0, (
            f"Expected no chain violations at depth 1, got: {chain_violations}"
        )
