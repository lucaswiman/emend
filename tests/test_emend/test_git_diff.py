import json
import subprocess

import pytest
from typer.testing import CliRunner

from emend.cli import app
from emend.git_diff import DiffSelection, resolve_diff


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr("emend.git_diff.shutil.which", lambda name: None)
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "a.py").write_text("def original(value):\n    total = value + 7\n    result = total * 3\n    return result\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    git("switch", "-qc", "feature")
    return tmp_path, git


def test_diff_selection_staged_default_and_explicit(repo):
    root, git = repo
    source = root / "new name.py"
    source.write_text("one = 1\ntwo = 2\n")
    git("add", ".")
    assert resolve_diff("auto", root)[1] == "--cached"
    selection = DiffSelection.load("auto", root)
    assert selection.matches(source, 2)
    assert not selection.matches(root / "a.py", 1)
    assert not selection.matches(source, 3)
    git("commit", "-qm", "new source")
    assert DiffSelection.load("auto", root).lines == selection.lines
    assert DiffSelection.load("main..HEAD", root).lines == selection.lines
    assert DiffSelection.load("HEAD..HEAD", root).lines == {}
    source.write_text("# unstaged insertion\n" + source.read_text())
    moved = DiffSelection.load("main..HEAD", root)
    assert not moved.matches(source, 1)
    assert moved.matches(source, 3)
    with pytest.raises(ValueError):
        DiffSelection.load("--output=unsafe", root)


def test_pr_base_precedes_default(repo, monkeypatch):
    root, git = repo
    git("branch", "release")
    source = root / "a.py"
    source.write_text(source.read_text() + "added = 1\n")
    git("commit", "-qam", "change")
    base_oid = git("rev-parse", "release")
    monkeypatch.setattr("emend.git_diff.shutil.which", lambda name: "/fake/gh")
    run = subprocess.run
    def gh_run(args, **kwargs):
        if args[0] == "gh":
            # A fork may not have the base branch under the same local name.
            return subprocess.CompletedProcess(args, 0, base_oid if "baseRefOid,state" in args else "not-local", "")
        return run(args, **kwargs)
    monkeypatch.setattr("emend.git_diff.subprocess.run", gh_run)
    assert resolve_diff("auto", root)[1] == git("rev-parse", "release") + "..HEAD"


def test_diff_empty_staged_deletion_does_not_fall_back(repo):
    root, git = repo
    git("rm", "a.py")
    assert resolve_diff("auto", root)[1] == "--cached"
    assert DiffSelection.load("auto", root).lines == {}


@pytest.mark.parametrize("edit,expected", [
    ("# prefix\nfirst = 1\nchanged = 2\nlast = 3\n", [3]),
    ("first = 1\nreplacement = 4\nextra = 5\nlast = 3\n", [2, 3]),
    ("first = 1\nlast = 3\n", []),
])
def test_diff_translation_uses_bounded_git_calls(repo, monkeypatch, edit, expected):
    root, git = repo
    for name in ("one.py", "two.py", "three.py"):
        (root / name).write_text("first = 1\nchanged = 0\nlast = 3\n")
    git("add", ".")
    git("commit", "-qm", "files")
    for name in ("one.py", "two.py", "three.py"):
        (root / name).write_text("first = 1\nchanged = 2\nlast = 3\n")
    git("add", ".")
    run = subprocess.run
    calls = []
    def capture(args, **kwargs):
        calls.append(args)
        return run(args, **kwargs)
    monkeypatch.setattr("emend.git_diff.subprocess.run", capture)
    for name in ("one.py", "two.py", "three.py"):
        (root / name).write_text(edit)
    scope = DiffSelection.load("auto", root)
    assert [line for line in range(1, 6) if scope.matches(root / "one.py", line)] == expected
    assert len(calls) <= 4  # independent of the number of changed files


@pytest.mark.parametrize("command", ["dupes", "refs", "graph", "deadcode", "impact", "types", "trace", "facts", "cfg", "dsl-debug", "check", "lint", "policy"])
def test_analysis_commands_expose_diff(command):
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--diff" in result.output


@pytest.mark.parametrize("diff_args", [["--diff"], ["--diff=auto"], ["--diff", "HEAD"]])
def test_dupes_diff_keeps_unchanged_partner_and_verbose_sources(repo, diff_args):
    root, git = repo
    source = (root / "a.py").read_text()
    (root / "b.py").write_text(source.replace("original", "copy"))
    git("add", ".")
    result = CliRunner().invoke(app, ["dupes", str(root), *diff_args, "--json", "--mode", "exact"])
    assert result.exit_code == 0, result.output
    clusters = json.loads(result.output)
    assert clusters
    assert {m["file"] for c in clusters for m in c["members"]} == {str(root / "a.py"), str(root / "b.py")}
    verbose = CliRunner().invoke(app, ["dupes", str(root), "--diff", "-v", "--mode", "exact"])
    assert verbose.exit_code == 0, verbose.output
    assert "2:     total = value + 7" in verbose.output
    trivial = CliRunner().invoke(app, ["dupes", str(root), "--diff", "--min-lines", "100", "--json"])
    assert trivial.exit_code == 0 and json.loads(trivial.output) == []


@pytest.mark.parametrize("command,arguments", [
    ("deadcode", ["--no-last-reference", "--json"]),
    ("facts", ["--json"]),
    ("cfg", ["--format", "json"]),
    ("impact", ["--json"]),
])
def test_analysis_bare_diff_selects_staged_symbols(repo, command, arguments):
    root, git = repo
    (root / "a.py").write_text((root / "a.py").read_text() + "\ndef added(value):\n    return value * 2\n")
    git("add", ".")
    scope = ["--project", str(root)] if command == "impact" else [str(root)]
    result = CliRunner().invoke(app, [command, *scope, "--diff", *arguments])
    assert result.exit_code == 0, result.output
    assert "added" in result.output
    assert "original" not in result.output


def test_refs_and_graph_retain_context_but_filter_reported_locations(repo):
    root, git = repo
    source = root / "a.py"
    source.write_text(source.read_text() + "\ndef caller():\n    return original(1)\n")
    git("add", ".")
    runner = CliRunner()
    refs = runner.invoke(app, ["refs", f"{source}::original", "--project", str(root), "--diff", "--json"])
    assert refs.exit_code == 0, refs.output
    assert [r["line"] for r in json.loads(refs.output)] == [7]
    graph = runner.invoke(app, ["graph", str(source), "--project", str(root), "--diff", "--format", "json"])
    assert graph.exit_code == 0, graph.output
    assert json.loads(graph.output) == {"caller": ["original"]}
    cfg = runner.invoke(app, ["cfg", str(root), "--diff=HEAD..HEAD", "--format", "json"])
    assert cfg.exit_code == 0 and json.loads(cfg.output) == []


def test_diff_import_facts_use_importing_file_location(repo):
    root, git = repo
    source = root / "a.py"
    source.write_text(source.read_text() + "\nimport os\n")
    git("add", ".")
    result = CliRunner().invoke(app, ["facts", str(root), "--type", "imports", "--file", "a.py", "--diff", "--json"])
    assert result.exit_code == 0, result.output
    assert [item["imported_module"] for item in json.loads(result.stdout)] == ["os"]


@pytest.mark.parametrize("near", [[], ["--near"]])
def test_empty_diff_skips_duplicate_analysis(repo, monkeypatch, near):
    root, _ = repo
    def unexpected(*args, **kwargs):
        pytest.fail("empty diff must not run duplicate analysis")
    monkeypatch.setattr("emend.duplicate.query_duplicates", unexpected)
    monkeypatch.setattr("emend.inconsistency.find_inconsistencies", unexpected)
    result = CliRunner().invoke(app, ["dupes", str(root), *near, "--diff=HEAD..HEAD", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


@pytest.mark.parametrize("command", ["lint", "check"])
def test_diff_checks_filter_findings_without_fixing_unselected_code(repo, command):
    root, git = repo
    source = root / "a.py"
    source.write_text("print('old')\n")
    git("commit", "-qam", "existing violation")
    source.write_text(source.read_text() + "print('new')\n")
    git("add", ".")
    config = root / "rules.yaml"
    config.write_text("rules:\n  no-print:\n    find: print($X)\n    message: avoid print\n")
    args = [command, str(root), "--config", str(config), "--diff"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert f"{source}:2:" in result.output and f"{source}:1:" not in result.output
    rejected = CliRunner().invoke(app, [*args, "--fix"])
    assert rejected.exit_code != 0 and "cannot be combined" in rejected.output
    assert source.read_text() == "print('old')\nprint('new')\n"
