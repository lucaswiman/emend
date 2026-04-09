"""Regression tests for reported bugs."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest


def test_move_helper_adds_import_to_source(tmp_path, emend_cmd):
    """Regression test for issue #137.

    When a private helper function is moved to another module, the source module
    still calls the helper by its bare name. The move command should either:
    - Add an import in the source module so the call resolves correctly, OR
    - Reject the move (since the source still has callers)

    Without the fix this test documents, the source file calls `_coerce_str()`
    but that function no longer exists there, causing a NameError at runtime.
    """
    # Create source file: a public function that calls a private helper
    src_file = tmp_path / "src.py"
    src_file.write_text(
        "def _coerce_str(value):\n"
        "    return str(value)\n"
        "\n"
        "def public_func(x):\n"
        "    return _coerce_str(x)\n"
    )

    # Create destination file with an unrelated function
    dest_file = tmp_path / "dest.py"
    dest_file.write_text(
        "def existing_func():\n"
        "    return 42\n"
    )

    # Move the helper to dest.py
    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{src_file}::_coerce_str",
            str(dest_file),
            "--project",
            str(tmp_path),
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Command failed: {result.stderr}"

    # _coerce_str should now be in dest.py
    dest_content = dest_file.read_text()
    assert "def _coerce_str(" in dest_content, (
        "_coerce_str should have been moved to dest.py"
    )

    # _coerce_str should no longer be defined in src.py
    src_content = src_file.read_text()
    src_tree = ast.parse(src_content)
    defined_names = [
        node.name
        for node in ast.walk(src_tree)
        if isinstance(node, ast.FunctionDef)
    ]
    assert "_coerce_str" not in defined_names, (
        "_coerce_str definition should have been removed from src.py"
    )

    # public_func still calls _coerce_str, so src.py must import it from dest
    # Otherwise the call would be a NameError at runtime.
    import_sources = {}
    for node in ast.walk(src_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                import_sources[alias.name] = node.module

    assert "_coerce_str" in import_sources, (
        "src.py should import _coerce_str from its new location (dest) "
        "because public_func still calls it. "
        f"Actual imports: {import_sources}\n"
        f"src.py content:\n{src_content}"
    )
    assert import_sources["_coerce_str"] in ("dest", "dest.py"), (
        f"_coerce_str should be imported from 'dest', got {import_sources['_coerce_str']!r}"
    )


def test_rename_private_symbol_updates_definition(tmp_path, monkeypatch):
    """Regression test for issue #136.

    When renaming a private symbol (prefixed with _), the defining declaration
    in the source file should also be renamed, not just references in other files.

    The bug is triggered when the project_path passed to rename_symbol is a
    relative path (e.g. '.' as typed by the user via -p .), which causes the
    Rust PyScopeResolver to produce wrong QNs for definitions in the target file.
    References in other files (b.py) are still found because their QNs come
    from import statements resolved via the absolute module name ('a._hidden'),
    but the definition in a.py gets the wrong path-based QN and is skipped.

    Without the fix: rename renames references in b.py to 'hidden' but leaves
    'def _hidden()' unchanged in a.py, causing a runtime ImportError when b.py
    tries 'from a import hidden'.
    """
    import os
    from emend.transform import rename_symbol
    from emend.component_selector import ExtendedSelector

    # a.py contains the private symbol definition
    a_file = tmp_path / "a.py"
    a_file.write_text(
        "def _hidden() -> int:\n"
        "    return 1\n"
    )

    # b.py imports and calls it
    b_file = tmp_path / "b.py"
    b_file.write_text(
        "from a import _hidden\n"
        "\n"
        "def use_it():\n"
        "    return _hidden()\n"
    )

    # Change to the project directory so that relative paths can be used,
    # which is the exact scenario from the bug report (emend rename a.py::_hidden
    # --to hidden -p .).
    monkeypatch.chdir(tmp_path)

    selector = ExtendedSelector(
        file_path="a.py",          # relative path, as typed by the user
        symbol_path=["_hidden"],
        component=None,
        accessor=None,
    )

    # project_path='.' mirrors the CLI flag '-p .' from the bug report
    diffs = rename_symbol(selector, "hidden", project_path=".", apply=True)

    # The defining declaration in a.py should be renamed
    a_content = a_file.read_text()
    assert "def hidden() -> int:" in a_content, (
        f"a.py definition should be renamed from '_hidden' to 'hidden'.\n"
        f"a.py content:\n{a_content}"
    )
    assert "_hidden" not in a_content, (
        f"a.py should no longer contain '_hidden' after rename.\n"
        f"a.py content:\n{a_content}"
    )

    # References in b.py should also be renamed
    b_content = b_file.read_text()
    assert "from a import hidden" in b_content, (
        f"b.py import should be updated to 'hidden'.\n"
        f"b.py content:\n{b_content}"
    )
    assert "return hidden()" in b_content, (
        f"b.py call should be updated to 'hidden()'.\n"
        f"b.py content:\n{b_content}"
    )
    assert "_hidden" not in b_content, (
        f"b.py should no longer contain '_hidden' after rename.\n"
        f"b.py content:\n{b_content}"
    )


# ---------------------------------------------------------------------------
# Issue #135: Module Rename Missing String-Based References
# ---------------------------------------------------------------------------


def test_rename_module_relative_import_in_init(tmp_path, run_emend_cmd):
    """Relative imports in __init__.py should be updated on module rename.

    GitHub issue #135: When renaming pkg/models.py to pkg/resolution_models.py,
    a relative import like ``from .models import VALUE`` in __init__.py is not
    updated.
    """
    # Create a package structure
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    (pkg / "__init__.py").write_text(
        "from .models import VALUE\n"
        "__all__ = ['VALUE']\n"
    )
    (pkg / "models.py").write_text(
        "VALUE = 42\n"
    )

    # Rename pkg/models.py -> pkg/resolution_models.py
    result = run_emend_cmd([
        "rename", str(pkg / "models.py"),
        "--to", "resolution_models",
        "--project", str(tmp_path),
        "--apply",
    ])
    assert result.returncode == 0

    # The new file should exist
    assert (pkg / "resolution_models.py").exists()
    assert not (pkg / "models.py").exists()

    init_content = (pkg / "__init__.py").read_text()
    # The relative import should have been updated
    assert "from .resolution_models import VALUE" in init_content, (
        f"Relative import was not updated. __init__.py content:\n{init_content}"
    )
    assert "from .models import" not in init_content, (
        f"Old relative import still present. __init__.py content:\n{init_content}"
    )


def test_rename_module_all_entry(tmp_path, run_emend_cmd):
    """__all__ string entries referencing the module should be updated on rename.

    GitHub issue #135: When renaming pkg/models.py to pkg/resolution_models.py,
    an __all__ entry like ``__all__ = ("models", "VALUE")`` still references the
    old module name after renaming.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    (pkg / "__init__.py").write_text(
        'from . import models\n'
        '__all__ = ("models", "VALUE")\n'
    )
    (pkg / "models.py").write_text(
        "VALUE = 42\n"
    )

    result = run_emend_cmd([
        "rename", str(pkg / "models.py"),
        "--to", "resolution_models",
        "--project", str(tmp_path),
        "--apply",
    ])
    assert result.returncode == 0

    assert (pkg / "resolution_models.py").exists()
    assert not (pkg / "models.py").exists()

    init_content = (pkg / "__init__.py").read_text()
    # The __all__ string entry for the module name should be updated
    assert (
        '"resolution_models"' in init_content
        or "'resolution_models'" in init_content
    ), (
        f"__all__ entry was not updated. __init__.py content:\n{init_content}"
    )
    # Old module name string should not remain in __all__
    assert '"models"' not in init_content and "'models'" not in init_content, (
        f"Old __all__ entry still present. __init__.py content:\n{init_content}"
    )


def test_rename_module_importlib_dynamic(tmp_path, run_emend_cmd):
    """Dynamic importlib.import_module() calls should be updated on module rename.

    GitHub issue #135: When renaming pkg/models.py to pkg/resolution_models.py,
    ``importlib.import_module("pkg.models")`` is not updated.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text("VALUE = 42\n")

    loader = tmp_path / "loader.py"
    loader.write_text(
        "import importlib\n"
        'mod = importlib.import_module("pkg.models")\n'
    )

    result = run_emend_cmd([
        "rename", str(pkg / "models.py"),
        "--to", "resolution_models",
        "--project", str(tmp_path),
        "--apply",
    ])
    assert result.returncode == 0

    assert (pkg / "resolution_models.py").exists()
    assert not (pkg / "models.py").exists()

    loader_content = loader.read_text()
    assert 'importlib.import_module("pkg.resolution_models")' in loader_content, (
        f"Dynamic import was not updated. loader.py content:\n{loader_content}"
    )
    assert '"pkg.models"' not in loader_content, (
        f"Old dynamic import still present. loader.py content:\n{loader_content}"
    )


def test_rename_module_bare_relative_import_in_init(tmp_path, run_emend_cmd):
    """``from . import models`` in __init__.py should be updated on module rename.

    GitHub issue #135 (additional sub-issue): When renaming pkg/models.py to
    pkg/resolution_models.py, a bare relative import ``from . import models``
    in __init__.py is not updated.  This is different from the
    ``from .models import VALUE`` form (which was already fixed).

    Root cause: the Rust scope resolver emits QN ``..models`` (two dots) for
    ``from . import models`` in __init__.py because __init__.py IS the package.
    ``_resolve_relative_import_qn`` miscounts the levels and resolves to just
    ``models`` instead of ``pkg.models``, so the comparison against the old
    module name fails.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    (pkg / "__init__.py").write_text(
        "from . import models\n"
        "__all__ = ['models']\n"
    )
    (pkg / "models.py").write_text(
        "VALUE = 42\n"
    )

    result = run_emend_cmd([
        "rename", str(pkg / "models.py"),
        "--to", "resolution_models",
        "--project", str(tmp_path),
        "--apply",
    ])
    assert result.returncode == 0

    assert (pkg / "resolution_models.py").exists()
    assert not (pkg / "models.py").exists()

    init_content = (pkg / "__init__.py").read_text()
    # The bare relative import should have been updated
    assert "from . import resolution_models" in init_content, (
        f"Bare relative import was not updated. __init__.py content:\n{init_content}"
    )
    assert "from . import models" not in init_content, (
        f"Old bare relative import still present. __init__.py content:\n{init_content}"
    )


def test_rename_module_bare_relative_import_attribute_access(tmp_path, run_emend_cmd):
    """``models.VALUE`` should become ``resolution_models.VALUE`` after rename.

    When a sibling module uses ``from . import models`` and then accesses
    ``models.VALUE``, renaming the module should update both the import and
    all attribute-access references to the old module name.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text("VALUE = 42\n")
    (pkg / "consumer.py").write_text(
        "from . import models\n"
        "\n"
        "def use_it():\n"
        "    return models.VALUE\n"
    )

    result = run_emend_cmd([
        "rename", str(pkg / "models.py"),
        "--to", "resolution_models",
        "--project", str(tmp_path),
        "--apply",
    ])
    assert result.returncode == 0

    consumer_content = (pkg / "consumer.py").read_text()
    assert "from . import resolution_models" in consumer_content, (
        f"Import not updated. consumer.py:\n{consumer_content}"
    )
    assert "resolution_models.VALUE" in consumer_content, (
        f"Attribute access not updated. consumer.py:\n{consumer_content}"
    )
    # Check no bare "models.VALUE" remains (but "resolution_models.VALUE" is OK).
    lines = consumer_content.splitlines()
    for line_text in lines:
        if "models.VALUE" in line_text:
            # Only flag if the match is NOT part of "resolution_models.VALUE"
            stripped = line_text.replace("resolution_models.VALUE", "")
            assert "models.VALUE" not in stripped, (
                f"Old attribute access still present. consumer.py:\n{consumer_content}"
            )


def test_rename_module_bare_relative_import_in_sibling(tmp_path, run_emend_cmd):
    """``from . import models`` in a sibling module should be updated on rename.

    Same root cause as the __init__.py variant: the Rust resolver adds an extra
    separator dot for ``from . import X`` style imports, producing QN ``..models``
    instead of ``.models``.  ``_resolve_relative_import_qn`` must compensate
    regardless of whether the file is __init__.py.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text("VALUE = 42\n")
    (pkg / "consumer.py").write_text(
        "from . import models\n"
        "\n"
        "def use_it():\n"
        "    return models.VALUE\n"
    )

    result = run_emend_cmd([
        "rename", str(pkg / "models.py"),
        "--to", "resolution_models",
        "--project", str(tmp_path),
        "--apply",
    ])
    assert result.returncode == 0

    assert (pkg / "resolution_models.py").exists()
    assert not (pkg / "models.py").exists()

    consumer_content = (pkg / "consumer.py").read_text()
    assert "from . import resolution_models" in consumer_content, (
        f"Bare relative import not updated. consumer.py:\n{consumer_content}"
    )
    assert "from . import models" not in consumer_content, (
        f"Old import still present. consumer.py:\n{consumer_content}"
    )


def test_rename_module_parent_relative_import(tmp_path, run_emend_cmd):
    """``from .. import models`` in a sub-package should be updated on rename.

    The Rust resolver produces QN ``...models`` (three dots) for
    ``from .. import models``.  Resolution must handle this correctly.
    """
    pkg = tmp_path / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True)

    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text("VALUE = 42\n")
    (sub / "__init__.py").write_text("")
    (sub / "consumer.py").write_text(
        "from .. import models\n"
        "\n"
        "def use_it():\n"
        "    return models.VALUE\n"
    )

    result = run_emend_cmd([
        "rename", str(pkg / "models.py"),
        "--to", "resolution_models",
        "--project", str(tmp_path),
        "--apply",
    ])
    assert result.returncode == 0

    assert (pkg / "resolution_models.py").exists()
    assert not (pkg / "models.py").exists()

    consumer_content = (sub / "consumer.py").read_text()
    assert "from .. import resolution_models" in consumer_content, (
        f"Parent relative import not updated. consumer.py:\n{consumer_content}"
    )
    assert "from .. import models" not in consumer_content, (
        f"Old import still present. consumer.py:\n{consumer_content}"
    )


def test_move_symbol_retains_import_used_by_remaining_code(tmp_path, emend_cmd):
    """Moving a symbol should not remove imports still needed by other code.

    When source.py has ``from utils import helper`` and two functions that both
    use ``helper``, moving only one function should leave the import in place
    for the remaining function.
    """
    (tmp_path / "utils.py").write_text(
        "def helper(x):\n"
        "    return x + 1\n"
    )

    source = tmp_path / "source.py"
    source.write_text(
        "from utils import helper\n"
        "\n"
        "def func_a():\n"
        "    return helper(1)\n"
        "\n"
        "def func_b():\n"
        "    return helper(2)\n"
    )

    dest = tmp_path / "dest.py"
    dest.write_text(
        "def existing():\n"
        "    return 0\n"
    )

    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{source}::func_a",
            str(dest),
            "--project", str(tmp_path),
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"Command failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    # func_a should be in dest.py now
    dest_content = dest.read_text()
    assert "def func_a" in dest_content, (
        f"func_a should have been moved to dest.py:\n{dest_content}"
    )

    # source.py should still have func_b AND the helper import
    src_content = source.read_text()
    assert "def func_b" in src_content, (
        f"func_b should remain in source.py:\n{src_content}"
    )
    assert "from utils import helper" in src_content, (
        f"'from utils import helper' should be retained — func_b still uses it.\n"
        f"source.py content:\n{src_content}"
    )


# ---------------------------------------------------------------------------
# Issue #138 - Bug 1: sibling imports must NOT be retargeted
# Issue #138 - Bug 2: moved symbol must carry its dependencies
# ---------------------------------------------------------------------------

_SOURCE_MOD_138 = """\
from dataclasses import dataclass, asdict


@dataclass
class Helper:
    value: int


@dataclass
class Bundle:
    name: str
    helper: Helper

    def to_dict(self):
        return asdict(self)


def load_bundle(name: str) -> "Bundle":
    return Bundle(name=name, helper=Helper(value=0))
"""

_DEST_MOD_138 = """\
def existing():
    return "I was here first"
"""

_CONSUMER_138 = """\
from source_mod import Bundle, load_bundle


def use_them():
    b = load_bundle("test")
    return b
"""


def test_move_symbol_no_sibling_retarget(tmp_path, emend_cmd):
    """Moving Bundle from source_mod must NOT retarget load_bundle's import.

    Issue #138 Bug 1: consumer.py has::

        from source_mod import Bundle, load_bundle

    After ``emend move source_mod.py::Bundle dest_mod.py --apply``:
      - Bundle import should point to dest_mod
      - load_bundle import must STILL point to source_mod (not dest_mod)

    The broken behaviour: the entire ``from source_mod import ...`` line is
    rewritten to ``from dest_mod import Bundle, load_bundle`` even though
    load_bundle was not moved.
    """
    (tmp_path / "source_mod.py").write_text(_SOURCE_MOD_138)
    (tmp_path / "dest_mod.py").write_text(_DEST_MOD_138)
    consumer = tmp_path / "consumer.py"
    consumer.write_text(_CONSUMER_138)

    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{tmp_path / 'source_mod.py'}::Bundle",
            str(tmp_path / "dest_mod.py"),
            "--project", str(tmp_path),
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"Command failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    consumer_content = consumer.read_text()

    # The critical failure: load_bundle must NOT appear to come from dest_mod.
    # A naive implementation replaces the whole ``from source_mod import ...``
    # line's module name, dragging load_bundle along for the ride.
    assert "from dest_mod import Bundle, load_bundle" not in consumer_content, (
        "Bug 1: load_bundle was incorrectly retargeted to dest_mod alongside Bundle.\n"
        f"consumer.py:\n{consumer_content}"
    )
    assert "from dest_mod import load_bundle" not in consumer_content, (
        "Bug 1: load_bundle was incorrectly retargeted to dest_mod.\n"
        f"consumer.py:\n{consumer_content}"
    )

    # Bundle should now be imported from dest_mod
    assert "from dest_mod import Bundle" in consumer_content, (
        f"Bundle should be imported from dest_mod.\nconsumer.py:\n{consumer_content}"
    )

    # load_bundle must still be reachable from source_mod in consumer.py
    assert "source_mod" in consumer_content and "load_bundle" in consumer_content, (
        f"load_bundle should still be importable from source_mod.\n"
        f"consumer.py:\n{consumer_content}"
    )


def test_move_symbol_carries_dependencies(tmp_path, emend_cmd):
    """Moving Bundle to dest_mod should bring along all needed dependencies.

    Issue #138 Bug 2: Bundle depends on:
      - ``@dataclass`` decorator  => needs ``from dataclasses import dataclass``
      - ``asdict()``              => needs ``from dataclasses import asdict``
      - ``Helper`` class          => needs the Helper definition or an import

    After the move, dest_mod.py must be a valid, importable module.  Without
    the fix, the moved class body references names that are not defined in
    dest_mod.py, raising NameError / TypeError at runtime.
    """
    (tmp_path / "source_mod.py").write_text(_SOURCE_MOD_138)
    dest_mod = tmp_path / "dest_mod.py"
    dest_mod.write_text(_DEST_MOD_138)
    (tmp_path / "consumer.py").write_text(_CONSUMER_138)

    result = subprocess.run(
        [
            emend_cmd,
            "move",
            f"{tmp_path / 'source_mod.py'}::Bundle",
            str(tmp_path / "dest_mod.py"),
            "--project", str(tmp_path),
            "--apply",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"Command failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

    dest_content = dest_mod.read_text()

    # Bundle class must be present in dest_mod
    assert "class Bundle" in dest_content, (
        f"Bundle class should be in dest_mod.py:\n{dest_content}"
    )

    # @dataclass must be applied and importable
    assert "@dataclass" in dest_content, (
        f"Bug 2: @dataclass decorator missing from dest_mod.py:\n{dest_content}"
    )
    assert "dataclass" in dest_content, (
        f"Bug 2: 'dataclass' import missing from dest_mod.py:\n{dest_content}"
    )

    # asdict must be available (used inside to_dict())
    assert "asdict" in dest_content, (
        f"Bug 2: asdict dependency missing from dest_mod.py:\n{dest_content}"
    )

    # Helper must be available (Bundle uses it as a field type annotation)
    assert "Helper" in dest_content, (
        f"Bug 2: Helper dependency missing from dest_mod.py:\n{dest_content}"
    )

    # dest_mod.py must be syntactically valid Python
    try:
        ast.parse(dest_content)
    except SyntaxError as exc:
        pytest.fail(
            f"dest_mod.py is not valid Python after move:\n{dest_content}\nError: {exc}"
        )

    # The most meaningful check: actually import and instantiate Bundle
    check_script = (
        "import sys; sys.path.insert(0, r'" + str(tmp_path) + "'); "
        "from dest_mod import Bundle; "
        "from source_mod import Helper; "
        "b = Bundle(name='x', helper=Helper(value=1)); "
        "d = b.to_dict(); "
        "print('ok')"
    )
    run = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, (
        f"Bug 2: dest_mod.py not importable after move.\n"
        f"dest_mod.py content:\n{dest_content}\n"
        f"Import error:\n{run.stderr}"
    )
