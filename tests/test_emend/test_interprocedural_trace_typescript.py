"""TypeScript interprocedural trace behavioral contracts."""

import pytest

from emend.trace import InterproceduralResult, run_interprocedural_trace
from trace_contract_helpers import run_trace_contract


@pytest.fixture
def interproc_ts(tmp_path):
    def run(source):
        _, result = run_trace_contract(
            tmp_path, run_interprocedural_trace, "typescript", "ts", source,
            ["req.query.get($X)", "req.body.get($X)"],
            "db.execute($X)", "SQL injection",
            ["sanitize($X)", "escape($X)"],
        )
        return result
    return run


@pytest.mark.parametrize(("case", "source", "requires_violation"), [
    ("direct-helper-sink", """\
function runQuery(db, query) {
    db.execute(query);
}

function handler(req, db) {
    let name = req.query.get("name");
    runQuery(db, name);
}
""", True),
    ("returned-taint", """\
function passthrough(value) {
    return value;
}

function handler(req, db) {
    let name = req.query.get("name");
    let query = passthrough(name);
    db.execute(query);
}
""", True),
    ("callback-style-delegation", """\
function process(db, data) {
    db.execute(data);
}

function handler(req, db) {
    let name = req.query.get("name");
    process(db, name);
}
""", True),
    ("async-best-effort", """\
async function getData(req) {
    let data = req.query.get("name");
    return data;
}
async function handler(req, db) {
    let result = getData(req);
    db.execute(result);
}
""", False),
    ("late-sanitizer", """\
function runQuery(db, query) {
    db.execute(query);
}

function handler(req, db) {
    let name = req.query.get("name");
    runQuery(db, name);
    name = escape(name);
}
""", True),
], ids=[
    "direct-helper-sink", "returned-taint", "callback-style-delegation",
    "async-best-effort", "late-sanitizer",
])
def test_typescript_interprocedural_contracts(interproc_ts, case, source, requires_violation):
    result = interproc_ts(source)
    assert isinstance(result, InterproceduralResult)
    assert all(v.label == "user_input" for v in result.violations)
    if requires_violation:
        assert result.violations, f"{case}; summaries: {list(result.summaries)}"
        assert "SQL injection" in result.violations[0].message


def test_typescript_transitive_param_to_sink_contract(interproc_ts):
    result = interproc_ts("""\
function step2(db, data) {
    db.execute(data);
}

function step1(db, value) {
    step2(db, value);
}

function handler(req, db) {
    let name = req.query.get("name");
    step1(db, name);
}
""")
    assert isinstance(result, InterproceduralResult)
    summaries = result.summaries
    assert {qn.rsplit("::", 1)[-1] for qn in summaries} >= {"step2", "step1", "handler"}
    assert "data" in next(s for qn, s in summaries.items() if qn.endswith("::step2")).param_to_sink
    assert "value" in next(s for qn, s in summaries.items() if qn.endswith("::step1")).param_to_sink
    assert result.violations
    assert all(v.label == "user_input" for v in result.violations)
