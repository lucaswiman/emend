"""Behavioral probes for the experimental near-clone comparison."""

import pytest
import json
from pathlib import Path
from typer.testing import CliRunner

from emend.inconsistency import find_inconsistencies
from emend.cli import app


@pytest.mark.parametrize("arguments, status", [([], 0), (["missing.py"], 2), (["--help"], 0), (["--mode", "exact"], 2)])
def test_near_cli_usage(arguments, status, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["dupes", "--near", *arguments])
    assert result.exit_code == status, result.output
    assert result.output


@pytest.mark.parametrize("change", ["guard", "operator", "literal", "rename", "parameter_order", "docstring", "parenthesized_docstring", "indentation", "fstring", "comma", "multi", "non_python"])
def test_near_clone_differences(tmp_path, change, monkeypatch):
    original = '''def process(items, fallback):
    result = []
    for item in items:
        if not allowed(item):
            continue
        value = transform(item)
        if value > 10:
            result.append(value)
    save(result)
    return result
'''
    variants = {
        "guard": original.replace("        if not allowed(item):\n            continue\n", ""),
        "operator": original.replace("value > 10", "value < 10"),
        "literal": original.replace("value > 10", "value > 11"),
        "rename": original.replace("items", "records").replace("result", "output"),
        "parameter_order": original.replace("in items", "in fallback"),
        "docstring": original.replace("    result =", '    "Different documentation."\n    result ='),
        "parenthesized_docstring": original.replace("    result =", '    ("Different documentation.")\n    result ='),
        "non_python": original.replace("value > 10", "value < 10"),
        "indentation": original.replace("    save(result)", "        save(result)"),
        "fstring": original.replace("    result =", '    f"{audit()}"\n    result ='),
        "comma": original.replace("save(result)", "save(result,)"),
        "multi": original.replace("allowed", "authorized").replace("transform", "convert").replace("save", "persist"),
    }
    files = [tmp_path / "a.py", tmp_path / "b.py"]
    if change == "non_python":
        files[1] = files[1].with_suffix(".txt")
    for path, content in zip(files, [original, variants[change]]):
        path.write_text(content)
    findings = find_inconsistencies(files)
    result = CliRunner().invoke(app, ["analyze", "dupes", str(tmp_path), "--near", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == json.loads(json.dumps(findings))
    if change == "operator":
        text = CliRunner().invoke(app, ["dupes", str(tmp_path), "--near"])
        assert text.exit_code == 0 and "not confirmed bugs" in text.output
        assert "--- " in text.output and "+        if value < 10:" in text.output
        limited = CliRunner().invoke(app, ["dupes", str(tmp_path), "--near", "--json", "--limit", "0"])
        assert limited.exit_code == 0 and json.loads(limited.output) == []
        verbose = CliRunner().invoke(app, ["dupes", str(tmp_path), "--near", "-v"])
        assert verbose.exit_code == 0 and "def process(items, fallback):" in verbose.output
        short = CliRunner().invoke(app, ["dupes", str(tmp_path), "--near", "--min-lines", "100", "--json"])
        assert short.exit_code == 0 and json.loads(short.output) == []
        assert find_inconsistencies(files, location_filter=lambda path, start, end: start <= 7 <= end)
        for file in files:
            assert find_inconsistencies(files, location_filter=lambda path, *args: Path(path) == file) == findings
        def unexpected_candidates():
            pytest.fail("unselected functions should not generate candidate pairs")
        with monkeypatch.context() as patch:
            patch.setattr("emend.inconsistency.Counter", unexpected_candidates)
            assert find_inconsistencies(files, location_filter=lambda *args: False) == []
    assert len(findings) == (0 if change in {"rename", "docstring", "parenthesized_docstring", "comma", "multi", "non_python"} else 1)
    if change == "multi":
        relaxed = find_inconsistencies(files, max_regions=3)
        assert len(relaxed) == 1 and len(relaxed[0]["changes"]) == 3
    if findings:
        finding = findings[0]
        assert finding["left"].endswith("a.py:1::process")
        assert finding["right"].endswith("b.py:1::process")
        assert finding["changes"] and finding["diff"]
        delta = [token for edit in finding["changes"] for side in ("left", "right") for token in edit[side]]
        assert {
            "guard": "allowed", "operator": "<", "literal": "11",
            "indentation": ("block", "end"), "fstring": "audit",
            "parameter_order": "bound_1",
        }[change] in delta


def test_unrelated_and_trivial_functions_are_not_candidates(tmp_path):
    path = tmp_path / "functions.py"
    path.write_text('''def first(x):
    return x + 1
def second(x):
    return x + 2
def unrelated(resource):
    with resource.acquire() as connection:
        try:
            connection.execute("SELECT * FROM records")
        finally:
            connection.close()
''')
    assert find_inconsistencies([path]) == []


# ---------------------------------------------------------------------------
# Near-clone contracts shared by the supported tree-sitter grammars
# ---------------------------------------------------------------------------


@pytest.fixture(params=["rs", "rs_method", "ts", "ts_method", "ts_arrow", "ts_bare_arrow", "ts_expression", "tsx", "js", "jsx"])
def source(request):
    kind = request.param
    if kind.startswith("rs"):
        text = '''fn process(items: Vec<i32>) -> Vec<i32> {
    let mut result = Vec::new();
    for item in items {
        if !allowed(item) { continue; }
        let value = transform(item);
        if value > 10 { result.push(value); }
    }
    save(&result);
    result
}'''
        if kind == "rs_method":
            text = "struct Worker;\nimpl Worker {\n" + text + "\n}"
    else:
        text = '''function process(items) {
    let result = [];
    for (let item of items) {
        if (!allowed(item)) { continue; }
        let value = transform(item);
        if (value > 10) { result.push(value); }
    }
    save(result);
    return result;
}'''
        if kind == "ts_method":
            text = "class Worker {\n" + text.replace("function process", "process") + "\n}"
        elif kind == "ts_arrow":
            text = text.replace("function process(items)", "const process = (items) =>")
        elif kind == "ts_bare_arrow":
            text = text.replace("function process(items)", "const process = items =>")
        elif kind == "ts_expression":
            text = text.replace("function process(items)", "const process = function(items)")
    return kind.split("_")[0], text


@pytest.mark.parametrize("change", ["operator", "rename", "comment", "member", "comma"])
def test_language_near_clones(tmp_path, source, change):
    ext, original = source
    changed = {
        "operator": original.replace("value > 10", "value < 10"),
        "rename": original.replace("items", "records").replace("result", "output"),
        "comment": original.replace("save(", "/* documentation */ save("),
        "member": original.replace(".push(", ".append("),
        "comma": original.replace("push(value)", "push(value,)"),
    }[change]
    files = [tmp_path / f"a.{ext}", tmp_path / f"b.{ext}"]
    for file, content in zip(files, [original, changed]):
        file.write_text(content)
    findings = find_inconsistencies(files)
    assert len(findings) == (change in {"operator", "member"})
    if change in {"rename", "operator"} and original.startswith(("fn process", "function process", "const process")):
        files[0].write_text(original + "\n" + changed.replace("process", "renamed"))
        assert len(find_inconsistencies([files[0]])) == (change == "operator")
    if findings:
        assert "process" in findings[0]["left"]
        assert ("value < 10" if change == "operator" else ".append(value)") in findings[0]["diff"]


def test_typescript_directives_and_mixed_languages(tmp_path):
    files = []
    for ext, header in [("ts", "function f(value)"), ("rs", "fn f(value: i32)")]:
        body = '{\n"use strict";\nstart(value);\naudit(value);\nfinish(value);\nsave(value);\nflush(value);\nclose(value);\n}'
        for name, content in [("a", body), ("b", body.replace('"use strict";', '"other";'))]:
            path = tmp_path / f"{name}.{ext}"
            path.write_text(header + content)
            files.append(path)
    findings = find_inconsistencies(files)
    assert len(findings) == 2
    result = CliRunner().invoke(app, ["dupes", str(tmp_path), "--near", "--json"])
    assert result.exit_code == 0 and len(json.loads(result.output)) == 2, result.output
    assert all(f".ts:" in f["left"] and ".ts:" in f["right"] or
               f".rs:" in f["left"] and ".rs:" in f["right"] for f in findings)
