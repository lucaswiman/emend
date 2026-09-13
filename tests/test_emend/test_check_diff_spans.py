"""Report locations retain their spans through check normalization."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from emend.cli import app


@pytest.mark.parametrize("rule", ["deadcode", "duplicate-code", "pattern", "structural"])
def test_diff_checks_select_changed_body_not_only_declaration(tmp_path, monkeypatch, rule):
    monkeypatch.chdir(tmp_path)
    source = "def original(value):\n    total = value + 7\n    result = total * 3\n    return result\n"
    (tmp_path / "a.py").write_text(source)
    (tmp_path / "b.py").write_text(source.replace("original", "copy"))
    config = tmp_path / "rules.yaml"
    config.write_text({
        "deadcode": "deadcode:\n  enabled: true\n",
        "duplicate-code": "duplicate-code:\n  min-lines: 3\n  min-score: 0\n",
        "pattern": "rules:\n  function:\n    find: 'def $F($...ARGS):'\n    message: found\n",
        "structural": "policies:\n  - name: function\n    description: found\n    checks:\n      - type: structural\n        pattern: 'def $F($...ARGS):'\n",
    }[rule])
    for args in [("init", "-q"), ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Test"), ("add", "."), ("commit", "-qm", "initial")]:
        subprocess.run(["git", *args], check=True, capture_output=True)
    (tmp_path / "a.py").write_text(source.replace("value + 7", "value  + 7"))
    runner = CliRunner()
    args = ["check", ".", "--config", str(config), "--json"]
    full = runner.invoke(app, args)
    changed = runner.invoke(app, [*args, "--diff=HEAD"])
    assert full.exit_code == changed.exit_code == 1, (full.output, changed.output)
    findings = json.loads(changed.output)
    assert findings and all(v["file"] == str(tmp_path / "a.py") for v in findings)
    assert any(v["line"] == 1 for v in findings)
    if rule == "deadcode":
        direct = runner.invoke(app, ["deadcode", ".", "--diff=HEAD", "--no-last-reference", "--json"])
        assert any(v.get("name") == "original" for v in json.loads(direct.output))
