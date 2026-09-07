"""Rust intraprocedural trace behavioral contracts."""

import pytest

from emend.trace import run_trace_analysis
from trace_contract_helpers import run_trace_contract


@pytest.fixture
def trace_rust(tmp_path):
    def run(source):
        return run_trace_contract(
            tmp_path, run_trace_analysis, "rust", "rs", source,
            ["get_input($X)", "read_request($X)"],
            "execute_query($X)", "Potential SQL injection",
            ["sanitize($X)", "escape($X)"],
        )
    return run


@pytest.mark.parametrize(("case", "source", "cardinality"), [
    ("direct-let-binding", """\
fn handler() {
    let x = get_input("name");
    execute_query(x);
}
""", "exactly-one"),
    ("second-source-pattern", """\
fn handler() {
    let x = read_request("data");
    execute_query(x);
}
""", "exactly-one"),
    ("renamed-variable", """\
fn handler() {
    let x = get_input("name");
    let y = x;
    execute_query(y);
}
""", "exactly-one"),
    ("match-arm", """\
fn handler() {
    let input = get_input("name");
    let result = match input.len() {
        0 => get_input("default"),
        _ => input,
    };
    execute_query(result);
}
""", "at-least-one"),
    ("loop-body", """\
fn handler() {
    let data = get_input("list");
    for _i in 0..10 {
        execute_query(data);
    }
}
""", "at-least-one"),
    ("conditional-block", """\
fn handler() {
    let data = get_input("name");
    if true {
        execute_query(data);
    }
}
""", "at-least-one"),
    ("vec-push-best-effort", """\
fn handler() {
    let mut items: Vec<String> = Vec::new();
    items.push(get_input("name"));
    let x = items[0].clone();
    execute_query(x);
}
""", "smoke"),
], ids=[
    "direct-let-binding", "second-source-pattern", "renamed-variable",
    "match-arm", "loop-body", "conditional-block", "vec-push-best-effort",
])
def test_rust_trace_contracts(trace_rust, case, source, cardinality):
    path, violations = trace_rust(source)
    assert isinstance(violations, list)
    if cardinality == "exactly-one":
        assert len(violations) == 1, case
    elif cardinality == "at-least-one":
        assert violations, case
    assert all(v.label == "user_input" for v in violations)
    assert all(v.file_path == str(path) for v in violations)
    if cardinality != "smoke":
        assert "SQL injection" in violations[0].message


def test_rust_trace_shadowing_sanitizer_blocks(trace_rust):
    _, violations = trace_rust("""\
fn handler() {
    let x = get_input("name");
    let x = sanitize(x);
    execute_query(x);
}
""")
    assert violations == []
