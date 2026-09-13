"""Relocation preserves imported targets and literal values."""
import subprocess
import sys
import pytest

from emend.component_selector import ExtendedSelector
from emend.transform.patterns import copy_symbol
from emend.transform.rename_move import move_module, rename_module


@pytest.mark.parametrize("literal", ['"""first\n        payload\n        """', 'r"""first\n        payload\n        """'])
@pytest.mark.parametrize("by_lines", [False, True])
def test_copy_preserves_multiline_literal_value(tmp_path, literal, by_lines):
    source, target = tmp_path / "source.py", tmp_path / "target.py"
    source.write_text(f'class C:\n    def f():\n        return {literal}\n')
    selector = (ExtendedSelector(str(source), [], line_start=2, line_end=5) if by_lines
                else ExtendedSelector(str(source), ["C", "f"]))
    copy_symbol(selector, str(target), dedent=True, apply=True)
    original, copied = {}, {}
    exec(source.read_text(), original)
    exec(target.read_text(), copied)
    assert copied["f"]() == original["C"].f()


@pytest.mark.parametrize("destination", ["newpkg", ""])
@pytest.mark.parametrize("relative", [False, True])
def test_module_move_preserves_relative_dependency(tmp_path, destination, relative, monkeypatch):
    for package, value in [("oldpkg", 1), ("newpkg", 2)]:
        directory = tmp_path / package
        directory.mkdir()
        (directory / "__init__.py").write_text("")
        (directory / "helper.py").write_text(f"VALUE = {value}\n")
    source = tmp_path / "oldpkg" / "source.py"
    source.write_text("from .helper import VALUE\nimport oldpkg.source\ndef run(): return VALUE\n")
    (source.parent / "consumer.py").write_text(
        "from .source import run\nfrom . import source as other, helper\nimport oldpkg.source\n"
        "assert run() == other.run() == helper.VALUE == oldpkg.source.run() == 1\n"
    )
    monkeypatch.chdir(tmp_path)
    move_module(str(source.relative_to(tmp_path) if relative else source), str(tmp_path / destination), project_path=str(tmp_path), apply=True)
    result = subprocess.run([sys.executable, "-c", "import oldpkg.consumer"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("statement", [
    "import { value } from './helper';", "export { value } from './helper';",
    "export const value = import('./helper');", "export const value = require('./helper');",
])
def test_module_move_preserves_node_relative_dependency(tmp_path, statement):
    (tmp_path / "oldpkg").mkdir()
    source = tmp_path / "oldpkg" / "source.ts"
    source.write_text(statement + "\n")
    move_module(str(source), str(tmp_path / "newpkg"), project_path=str(tmp_path), apply=True)
    assert (tmp_path / "newpkg" / "source.ts").read_text() == statement.replace("'./helper'", "'../oldpkg/helper'") + "\n"


@pytest.mark.parametrize("operation", [move_module, rename_module])
def test_package_file_relocation_rejects_before_writes(tmp_path, operation):
    source = tmp_path / "__init__.py"
    source.write_text("VALUE = 1\n")
    with pytest.raises(ValueError, match="package"):
        operation(str(source), "destination", project_path=str(tmp_path), apply=True)
    assert source.read_text() == "VALUE = 1\n"


def test_computed_module_import_rejects_before_move(tmp_path):
    source = tmp_path / "source.ts"
    source.write_text("export const value = import(moduleName);\n")
    with pytest.raises(ValueError, match="computed"):
        move_module(str(source), str(tmp_path / "newpkg"), project_path=str(tmp_path), apply=True)
    assert source.exists() and not (tmp_path / "newpkg").exists()
