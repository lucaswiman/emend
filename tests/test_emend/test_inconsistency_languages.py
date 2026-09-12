"""Near-clone contracts shared by the supported tree-sitter grammars."""

import pytest
import json
from typer.testing import CliRunner

from emend.inconsistency import find_inconsistencies
from emend.cli import app


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
