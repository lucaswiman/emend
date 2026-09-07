"""TypeScript intraprocedural trace behavioral contracts."""

import pytest

from emend.trace import run_trace_analysis
from trace_contract_helpers import run_trace_contract


@pytest.fixture
def trace_ts(tmp_path):
    def run(source, *, sources=None):
        return run_trace_contract(
            tmp_path, run_trace_analysis, "typescript", "ts", source,
            sources or ["req.query.get($X)", "req.body.get($X)"],
            "db.execute($X)", "Potential SQL injection",
            ["sanitize($X)", "escape($X)"],
        )
    return run


@pytest.mark.parametrize(("case", "source", "cardinality"), [
    ("direct-query-source", """\
function handler(req: any, db: any): void {
    let x = req.query.get("name");
    db.execute(x);
}
""", "exactly-one"),
    ("arrow-function", """\
const handler = (req: any, db: any): void => {
    let x = req.query.get("name");
    db.execute(x);
};
""", "exactly-one"),
    ("body-method-source", """\
function handler(req: any, db: any): void {
    let x = req.body.get("data");
    db.execute(x);
}
""", "exactly-one"),
    ("renamed-variable", """\
function handler(req: any, db: any): void {
    const data = req.query.get("name");
    let y = data;
    db.execute(y);
}
""", "exactly-one"),
    ("conditional-branch", """\
function handler(req: any, db: any): void {
    let x: string | undefined;
    if (req.query.get("flag")) {
        x = req.query.get("name");
    }
    db.execute(x);
}
""", "at-least-one"),
    ("try-catch", """\
function handler(req: any, db: any): void {
    try {
        let x = req.query.get("name");
        db.execute(x);
    } catch (e) {
        console.error(e);
    }
}
""", "exactly-one"),
    ("container-mutation-best-effort", """\
function handler(req: any, db: any): void {
    let items: string[] = [];
    items.push(req.query.get("name"));
    let x = items[0];
    db.execute(x);
}
""", "smoke"),
], ids=[
    "direct-query-source", "arrow-function", "body-method-source",
    "renamed-variable", "conditional-branch", "try-catch",
    "container-mutation-best-effort",
])
def test_typescript_trace_contracts(trace_ts, case, source, cardinality):
    path, violations = trace_ts(source)
    assert isinstance(violations, list)
    if cardinality == "exactly-one":
        assert len(violations) == 1, case
    elif cardinality == "at-least-one":
        assert violations, case
    assert all(v.label == "user_input" for v in violations)
    assert all(v.file_path == str(path) for v in violations)
    if cardinality != "smoke":
        assert "SQL injection" in violations[0].message


def test_typescript_trace_sanitizer_blocks(trace_ts):
    _, violations = trace_ts("""\
function handler(req: any, db: any): void {
    let x = req.query.get("name");
    x = sanitize(x);
    db.execute(x);
}
""")
    assert violations == []


def test_typescript_subscript_source_contract(trace_ts):
    _, violations = trace_ts("""\
function handler(req: any, db: any): void {
    let x = req.body["data"];
    db.execute(x);
}
""", sources=["req.body[$X]"])
    assert len(violations) >= 1
    assert violations[0].label == "user_input"
    assert "SQL injection" in violations[0].message
