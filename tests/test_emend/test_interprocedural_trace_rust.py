"""Rust interprocedural trace behavioral contracts."""

import pytest

from emend.trace import InterproceduralResult, run_interprocedural_trace
from trace_contract_helpers import run_trace_contract


@pytest.fixture
def interproc_rust(tmp_path):
    def run(source):
        _, result = run_trace_contract(
            tmp_path, run_interprocedural_trace, "rust", "rs", source,
            ["get_input($X)", "read_request($X)"],
            "execute_query($X)", "Potential SQL injection",
            ["sanitize($X)", "escape($X)"],
        )
        return result
    return run


@pytest.mark.parametrize(("case", "source"), [
    ("direct-helper-sink", """\
fn run_query(query: String) {
    execute_query(query);
}

fn handler() {
    let name = get_input("name");
    run_query(name);
}
"""),
    ("returned-taint", """\
fn passthrough(value: String) -> String {
    return value;
}

fn handler() {
    let name = get_input("name");
    let query = passthrough(name);
    execute_query(query);
}
"""),
    ("delegation-match-analogue", """\
fn process(input: String) {
    execute_query(input);
}

fn handler() {
    let data = get_input("name");
    process(data);
}
"""),
    ("closure-like-wrapper", """\
fn apply(value: String) {
    execute_query(value);
}

fn handler() {
    let data = get_input("name");
    apply(data);
}
"""),
    ("method-like-return-wrapper", """\
fn process(x: String) -> String {
    return x;
}

fn handler() {
    let name = get_input("name");
    let result = process(name);
    execute_query(result);
}
"""),
    ("two-function-chain", """\
fn sink_helper(value: String) {
    execute_query(value);
}

fn helper2(name: String) {
    sink_helper(name);
}

fn handler() {
    let name = get_input("name");
    sink_helper(name);
}
"""),
    ("three-hop-chain", """\
fn leaf(value: String) {
    execute_query(value);
}

fn mid(data: String) {
    leaf(data);
}

fn handler() {
    let name = get_input("name");
    mid(name);
}
"""),
], ids=[
    "direct-helper-sink", "returned-taint", "delegation-match-analogue",
    "closure-like-wrapper", "method-like-return-wrapper",
    "two-function-chain", "three-hop-chain",
])
def test_rust_interprocedural_contracts(interproc_rust, case, source):
    result = interproc_rust(source)
    assert isinstance(result, InterproceduralResult)
    assert result.violations, f"No violation for {case}; summaries: {list(result.summaries)}"
    assert all(v.label == "user_input" for v in result.violations)
    assert any("SQL injection" in v.message for v in result.violations)
