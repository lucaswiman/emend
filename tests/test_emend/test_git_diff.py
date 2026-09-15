import json
import subprocess

import pytest
from click import unstyle
from typer.testing import CliRunner

from emend.cli import app
from emend.git_diff import DiffSelection, LineIntervals, resolve_diff


@pytest.fixture
def repo(tmp_path, monkeypatch, request):
    tmp_path = tmp_path / getattr(request, "param", ".")
    tmp_path.mkdir(exist_ok=True)
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


@pytest.mark.parametrize("repo", [".", "project "], indirect=True)
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
    for spec in ("main..HEAD", "HEAD^!", "HEAD^-"):
        moved = DiffSelection.load(spec, root)
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


@pytest.mark.parametrize("setting,value", [("diff.noprefix", "true"), ("diff.interHunkContext", "10")])
def test_diff_selection_ignores_presentation_config(repo, setting, value):
    root, git = repo
    git("config", setting, value)
    source = root / "a.py"
    source.write_text(source.read_text().replace("value + 7", "value + 8").replace("return result", "return result + 1"))
    git("add", ".")
    selection = DiffSelection.load("auto", root)
    assert [line for line in range(1, 5) if selection.matches(source, line)] == [2, 4]
    impact = CliRunner().invoke(app, ["impact", "--project", str(root), "--diff", "--json"])
    assert impact.exit_code == 0 and "original" in impact.output


def test_diff_matches_reuses_path_resolution(repo, monkeypatch):
    root, _ = repo
    scope = DiffSelection(root, {str(root / "a.py"): LineIntervals([(2, 3)])})
    resolve = type(root).resolve
    calls = []
    def count(path, *args, **kwargs):
        calls.append(path)
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(type(root), "resolve", count)
    assert all(scope.matches(root / "a.py", 2) for _ in range(100))
    assert len(calls) == 1


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
def test_analysis_commands_expose_diff(command, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", True)
    result = CliRunner().invoke(app, [command, "--help"], color=True)
    assert result.exit_code == 0, result.output
    assert "--diff" in unstyle(result.output)


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


@pytest.mark.parametrize("nested", [False, True])
def test_diff_fact_paths_use_owner_root_not_requested_scope(repo, monkeypatch, nested):
    root, git = repo
    source = root / "a.py"
    if nested:
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text(source.read_text())
        source = root / "src" / "a.py"
        git("add", ".")
        git("commit", "-qm", "nested source")
    source.write_text(source.read_text().replace("value + 7", "value + 8"))
    monkeypatch.chdir(root.parent)
    scope = str(source.parent) if nested else root.name
    for arguments in (["facts", scope, "--json"],
                      ["graph", str(source), "--project", scope, "--format", "json"]):
        result = CliRunner().invoke(app, [*arguments, "--diff=HEAD"])
        assert result.exit_code == 0 and "original" in result.output, result.output


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


def test_diff_intervals_bound_large_hunks_and_merge_adjacent():
    from emend.git_diff import _parse_diff, _map_lines
    changed = _parse_diff('diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n'
                          '@@ -0,0 +1,1000000 @@\n@@ -0,0 +1000001,2 @@\n')[0]
    assert changed.lines[0].intervals == []
    assert changed.lines[1].intervals == [(1, 1000003)]
    assert _map_lines(changed.lines[1], [(1, 0, 2, 1)]).intervals == [(1, 2), (3, 1000004)]


@pytest.mark.parametrize('hunks,expected', [
    ([], [(2, 5), (8, 10)]),
    ([(1, 10, 0, 0)], []),
    ([(0, 0, 1, 2)], [(4, 7), (10, 12)]),
    ([(3, 2, 2, 0)], [(2, 3), (6, 8)]),
    ([(3, 2, 3, 4)], [(2, 7), (10, 12)]),
    ([(5, 0, 6, 2)], [(2, 5), (10, 12)]),
    ([(1, 10, 1, 2)], [(1, 3)]),
    ([(5, 2, 5, 1)], [(2, 5), (7, 9)]),
])
def test_interval_translation_boundaries(hunks, expected, tmp_path):
    from emend.git_diff import LineIntervals, _map_lines
    mapped = _map_lines(LineIntervals([(2, 5), (8, 10)]), hunks)
    assert mapped.intervals == expected
    selection = DiffSelection(tmp_path, {str(tmp_path / 'a.py'): mapped})
    for start in range(1, 15):
        for end in range(start, 15):
            assert selection.matches('a.py', start, end) == any(a <= end and start < b for a, b in expected)
