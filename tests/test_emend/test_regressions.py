"""Scope, documentation, and signature regressions."""

from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector
from emend.transform import find_references, rename_symbol


@pytest.fixture
def scoped_project(tmp_path):
    sources = {
        "file_a.py": "def process():\n    return 'A'\n",
        "file_b.py": "def process():\n    return 'B'\n\nresult = process()\n",
        "file_c.py": "from file_a import process\n\nresult = process()\n",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source)
    return sources, ExtendedSelector(str(tmp_path / "file_a.py"), ["process"])


def test_scope_aware_rename(tmp_path, scoped_project):
    sources, selector = scoped_project
    rename_symbol(selector, "handle", project_path=str(tmp_path), apply=True)
    for name, source in sources.items():
        expected = source if name == "file_b.py" else source.replace("process", "handle")
        assert (tmp_path / name).read_text() == expected


def test_scope_aware_references(tmp_path, scoped_project):
    _, selector = scoped_project
    refs = find_references(selector, project_path=str(tmp_path))
    assert {Path(ref.file_path).resolve() for ref in refs} == {
        tmp_path / "file_a.py", tmp_path / "file_c.py",
    }


def test_rename_preserves_shadowing_parameter(tmp_path):
    file = tmp_path / "module.py"
    source = (
        "def process():\n    return 42\n\n"
        "def bar(process):\n    return process + 1\n\nresult = process()\n"
    )
    file.write_text(source)
    rename_symbol(
        ExtendedSelector(str(file), ["process"]), "handle",
        project_path=str(tmp_path), apply=True,
    )
    assert file.read_text() == source.replace("process()", "handle()")


@pytest.mark.parametrize("docs", [False, True], ids=["preserve-docs", "rename-docs"])
@pytest.mark.parametrize(
    "name,new_name,source",
    [
        ("old_func", "new_func",
         'def old_func():\n    """This is old_func. Call old_func()."""\n    return 42\n'),
        ("OldClass", "NewClass",
         'class OldClass:\n    """OldClass provides functionality.\n\n'
         '    Use OldClass() to create an instance.\n    """\n    pass\n'),
    ],
    ids=["function", "class"],
)
def test_rename_docstrings(tmp_path, docs, name, new_name, source):
    file = tmp_path / "module.py"
    file.write_text(source)
    rename_symbol(
        ExtendedSelector(str(file), [name]), new_name,
        project_path=str(tmp_path), docs=docs, apply=True,
    )
    expected = source.replace(name, new_name) if docs else source.replace(name, new_name, 1)
    assert file.read_text() == expected


def test_rename_docs_in_importing_file(tmp_path):
    helpers = tmp_path / "helpers.py"
    helpers.write_text("def compute():\n    return 42\n")
    consumer = tmp_path / "main.py"
    source = (
        '"""Module that uses compute from helpers."""\n'
        "from helpers import compute\n\ndef run():\n"
        '    """Calls compute to get the answer."""\n    return compute()\n'
    )
    consumer.write_text(source)
    rename_symbol(
        ExtendedSelector(str(helpers), ["compute"]), "calculate",
        project_path=str(tmp_path), docs=True, apply=True,
    )
    assert consumer.read_text() == source.replace("compute", "calculate")


@pytest.mark.parametrize(
    "signature",
    [
        "f(a, /, b, *args, c=1, **kw)",
        "f(a, *, key=None, verbose=False)",
        "f(x, y, /)",
        "f(*args)",
        "f(**kwargs)",
        "f(a: int, *, key: str = 'default') -> bool",
    ],
    ids=["all-kinds", "keyword-only", "positional-only", "varargs", "kwargs", "annotations"],
)
def test_complete_signatures(tmp_path, run_emend_cmd, signature):
    file = tmp_path / "module.py"
    file.write_text(f"def {signature}:\n    pass\n")
    result = run_emend_cmd(["search", str(file), "--output", "summary"])
    assert signature.replace(" ", "") in result.stdout.replace(" ", "")
