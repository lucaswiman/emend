"""Project discovery must reflect changes within a long-running process."""

import subprocess
from pathlib import Path

import pytest

from emend.file_collection import collect_source_files


def test_scanner_follows_symlinks_without_directory_cycles(tmp_path):
    from emend import emend_core

    project, dependency = tmp_path / "project", tmp_path / "dependency"
    project.mkdir()
    dependency.mkdir()
    (project / "source.py").touch()
    (dependency / "dep.py").touch()
    (project / "pkg").mkdir()
    (project / "pkg" / "local.py").touch()
    for name, target in (("cycle", project), ("external", dependency),
                         ("alias", dependency), ("alias.py", project / "source.py"),
                         ("pkg-alias", project / "pkg"),
                         ("broken.py", tmp_path / "missing")):
        (project / name).symlink_to(target, target_is_directory=target.is_dir())
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(project, target_is_directory=True)
    for root in (project, linked_root):
        files = emend_core.collect_files(str(root), ["py"])
        assert {str(Path(path).relative_to(root)) for path in files} == {
            "source.py", "alias.py", "external/dep.py", "alias/dep.py",
            "pkg/local.py", "pkg-alias/local.py",
        }


@pytest.mark.parametrize("existing", [None, "a.py", "readme.txt"])
def test_collect_source_files_reflects_nested_changes(tmp_path, existing):
    sub = tmp_path / "pkg"
    sub.mkdir()
    if existing:
        (sub / existing).write_text("x = 1\n")
    expected = {str(sub / "a.py")} if existing == "a.py" else set()
    assert set(collect_source_files(str(tmp_path))) == expected

    added = sub / "b.py"
    added.write_text("y = 2\n")
    assert set(collect_source_files(str(tmp_path))) == expected | {str(added)}

    added.unlink()
    assert set(collect_source_files(str(tmp_path))) == expected


def test_git_tracked_collection_preserves_filenames(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert collect_source_files(str(tmp_path), git_tracked_only=True) == []
    names = [" spaced.py", "café.py"]
    for name in [*names, "untracked.py"]:
        (tmp_path / name).touch()
    subprocess.run(["git", "add", "--", *names], cwd=tmp_path, check=True)
    assert set(collect_source_files(str(tmp_path), git_tracked_only=True)) == {
        str(tmp_path / name) for name in names
    }
