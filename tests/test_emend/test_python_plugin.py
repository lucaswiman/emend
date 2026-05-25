"""Tests for the Python language plugin (Phase 2c/2d)."""
from __future__ import annotations

import pytest

from emend.language_plugins import (
    load_plugin,
    LanguagePlugin,
    NoOpImportHandler,
    RegexCommentHandler,
    TreeSitterPatternCompiler,
)
from emend.python_plugin import (
    PythonCommentHandler,
    PythonImportHandler,
    PythonPatternCompiler,
    create_python_plugin,
)


# ---------------------------------------------------------------------------
# PythonImportHandler
# ---------------------------------------------------------------------------

SOURCE_WITH_IMPORTS = """\
from __future__ import annotations
import os
import sys
from pathlib import Path

x = 1
"""

SOURCE_NO_IMPORTS = """\
x = 1
y = 2
"""

SOURCE_FUTURE_ONLY = """\
from __future__ import annotations

x = 1
"""


def test_python_import_handler_extract_imports():
    handler = PythonImportHandler()
    result = handler.extract_imports(SOURCE_WITH_IMPORTS)
    assert "import os" in result
    assert "import sys" in result
    assert "from pathlib import Path" in result
    assert "x = 1" not in result


def test_python_import_handler_extract_imports_empty():
    handler = PythonImportHandler()
    assert handler.extract_imports(SOURCE_NO_IMPORTS) == ""


def test_python_import_handler_extract_imports_syntax_error():
    handler = PythonImportHandler()
    assert handler.extract_imports("def (") == ""


def test_python_import_handler_add_import_prepend():
    handler = PythonImportHandler()
    # position=0 should insert after __future__
    result = handler.add_import_text("import collections", 0, SOURCE_WITH_IMPORTS)
    lines = result.splitlines()
    future_idx = next(i for i, l in enumerate(lines) if "__future__" in l)
    new_idx = next(i for i, l in enumerate(lines) if "import collections" in l)
    assert new_idx == future_idx + 1


def test_python_import_handler_add_import_prepend_no_future():
    handler = PythonImportHandler()
    result = handler.add_import_text("import collections", 0, SOURCE_WITH_IMPORTS.replace("from __future__ import annotations\n", ""))
    # Should insert at top when no __future__
    lines = result.splitlines()
    assert lines[0] == "import collections"


def test_python_import_handler_add_import_append():
    handler = PythonImportHandler()
    result = handler.add_import_text("import collections", -1, SOURCE_WITH_IMPORTS)
    lines = result.splitlines()
    path_idx = next(i for i, l in enumerate(lines) if "from pathlib" in l)
    new_idx = next(i for i, l in enumerate(lines) if "import collections" in l)
    assert new_idx == path_idx + 1


def test_python_import_handler_add_import_no_existing_imports():
    handler = PythonImportHandler()
    result = handler.add_import_text("import os", -1, SOURCE_NO_IMPORTS)
    assert result.startswith("import os\n")


def test_python_import_handler_add_import_syntax_error_graceful():
    """add_import_text should not crash on syntactically invalid source."""
    handler = PythonImportHandler()
    # With invalid Python, tree-sitter should still not crash; the import is
    # inserted at position 0 since no existing imports can be detected.
    result = handler.add_import_text("import os", -1, "def (\n")
    assert "import os" in result


def test_python_import_handler_add_import_future_only_prepend():
    handler = PythonImportHandler()
    result = handler.add_import_text("import os", 0, SOURCE_FUTURE_ONLY)
    lines = result.splitlines()
    future_idx = next(i for i, l in enumerate(lines) if "__future__" in l)
    os_idx = next(i for i, l in enumerate(lines) if "import os" in l)
    assert os_idx == future_idx + 1


# ---------------------------------------------------------------------------
# PythonImportHandler — remove_import
# ---------------------------------------------------------------------------


def test_remove_import_plain_import():
    """Remove a plain ``import foo`` statement."""
    handler = PythonImportHandler()
    source = "import os\nimport sys\n\nx = 1\n"
    result = handler.remove_import(source, "os", None)
    assert "import os" not in result
    assert "import sys" in result
    assert "x = 1" in result


def test_remove_import_from_import_whole_line():
    """Remove entire ``from foo import bar`` statement when name is None."""
    handler = PythonImportHandler()
    source = "from pathlib import Path\nimport os\n\nx = 1\n"
    result = handler.remove_import(source, "pathlib", None)
    assert "from pathlib" not in result
    assert "import os" in result
    assert "x = 1" in result


def test_remove_import_named_from_multi():
    """Remove one name from ``from foo import bar, baz`` leaving ``from foo import baz``."""
    handler = PythonImportHandler()
    source = "from os.path import join, exists\n\nx = 1\n"
    result = handler.remove_import(source, "os.path", "join")
    assert "join" not in result
    assert "from os.path import exists" in result
    assert "x = 1" in result


def test_remove_import_only_name_removes_line():
    """Remove the only name from ``from foo import bar`` removes the whole line."""
    handler = PythonImportHandler()
    source = "from pathlib import Path\n\nx = 1\n"
    result = handler.remove_import(source, "pathlib", "Path")
    assert "from pathlib" not in result
    assert "x = 1" in result


def test_remove_import_noop_when_not_found():
    """No change when the import doesn't exist."""
    handler = PythonImportHandler()
    source = "import os\n\nx = 1\n"
    result = handler.remove_import(source, "sys", None)
    assert result == source


def test_remove_import_noop_name_not_in_module():
    """No change when the module matches but the name doesn't."""
    handler = PythonImportHandler()
    source = "from os.path import exists\n\nx = 1\n"
    result = handler.remove_import(source, "os.path", "join")
    assert result == source


# ---------------------------------------------------------------------------
# PythonCommentHandler — find_noqa_comments
# ---------------------------------------------------------------------------

def test_python_comment_handler_find_noqa_bare():
    handler = PythonCommentHandler()
    source = "x = 1  # noqa\n"
    result = handler.find_noqa_comments(source)
    assert 1 in result
    assert result[1] is None  # bare noqa


def test_python_comment_handler_find_noqa_with_emend_tag():
    handler = PythonCommentHandler()
    source = "x = 1  # noqa: emend:deadcode\n"
    result = handler.find_noqa_comments(source)
    assert 1 in result
    assert result[1] == {"deadcode"}


def test_python_comment_handler_find_noqa_non_emend_tag():
    handler = PythonCommentHandler()
    source = "x = 1  # noqa: E501\n"
    result = handler.find_noqa_comments(source)
    # non-emend tag has no effect
    assert 1 not in result


def test_python_comment_handler_find_noqa_empty():
    handler = PythonCommentHandler()
    result = handler.find_noqa_comments("x = 1\ny = 2\n")
    assert result == {}


# ---------------------------------------------------------------------------
# PythonCommentHandler — rename_in_docstrings
# ---------------------------------------------------------------------------

SOURCE_WITH_DOCSTRINGS = '''\
def foo():
    """This references OldName in the docs."""
    pass

class Bar:
    """OldName is also here."""

    def method(self):
        """No reference here."""
        pass
'''


def test_python_comment_handler_rename_in_docstrings_found():
    handler = PythonCommentHandler()
    result = handler.rename_in_docstrings(SOURCE_WITH_DOCSTRINGS, "OldName", "NewName")
    assert result is not None
    assert "NewName" in result
    assert "OldName" not in result


def test_python_comment_handler_rename_in_docstrings_not_found():
    handler = PythonCommentHandler()
    result = handler.rename_in_docstrings(SOURCE_WITH_DOCSTRINGS, "NonExistent", "NewName")
    assert result is None


def test_python_comment_handler_rename_in_docstrings_syntax_error():
    handler = PythonCommentHandler()
    result = handler.rename_in_docstrings("def (", "old", "new")
    assert result is None


def test_python_comment_handler_find_docstrings_syntax_error():
    """find_docstrings should return an empty list on invalid source."""
    handler = PythonCommentHandler()
    result = handler.find_docstrings("def (", (0, 100))
    assert result == []


def test_python_comment_handler_find_docstrings_basic():
    """find_docstrings should find function and class docstrings."""
    handler = PythonCommentHandler()
    source = (
        'def foo():\n'
        '    """Function docstring."""\n'
        '    pass\n'
        '\n'
        'class Bar:\n'
        '    """Class docstring."""\n'
        '    pass\n'
    )
    results = handler.find_docstrings(source, (0, len(source.encode())))
    texts = [text for _, _, text in results]
    assert any("Function docstring" in t for t in texts)
    assert any("Class docstring" in t for t in texts)


# ---------------------------------------------------------------------------
# Verify no ast.parse usage (Phase 7 migration check)
# ---------------------------------------------------------------------------

def test_python_plugin_does_not_import_ast():
    """Phase 7 requirement: python_plugin.py must not import Python's ast module."""
    import inspect
    import emend.python_plugin as mod
    src = inspect.getsource(mod)
    assert "import ast" not in src, (
        "python_plugin.py must not use Python's ast module (Phase 7 migration)"
    )


# ---------------------------------------------------------------------------
# PythonPatternCompiler
# ---------------------------------------------------------------------------

def test_python_pattern_compiler():
    from emend.pattern import compile_pattern_to_rust_ir
    compiler = PythonPatternCompiler()
    pattern_str = "foo($X)"
    direct = compile_pattern_to_rust_ir(pattern_str)
    via_plugin = compiler.compile(pattern_str)
    assert direct == via_plugin


# ---------------------------------------------------------------------------
# load_plugin
# ---------------------------------------------------------------------------

def test_load_plugin_python_returns_correct_types():
    plugin = load_plugin("python")
    assert isinstance(plugin, LanguagePlugin)
    assert isinstance(plugin.import_handler, PythonImportHandler)
    assert isinstance(plugin.comment_handler, PythonCommentHandler)
    assert isinstance(plugin.pattern_compiler, PythonPatternCompiler)


def test_load_plugin_other_returns_stubs():
    from emend.language_plugins import TreeSitterPatternCompiler, TreeSitterImportHandler, DocCommentHandler
    plugin = load_plugin("typescript")
    assert isinstance(plugin, LanguagePlugin)
    assert isinstance(plugin.import_handler, TreeSitterImportHandler)
    assert isinstance(plugin.comment_handler, DocCommentHandler)
    assert isinstance(plugin.pattern_compiler, TreeSitterPatternCompiler)


def test_load_plugin_unknown_returns_stubs():
    plugin = load_plugin("cobol")
    assert isinstance(plugin, LanguagePlugin)
    assert isinstance(plugin.import_handler, NoOpImportHandler)
    assert isinstance(plugin.comment_handler, RegexCommentHandler)
    assert isinstance(plugin.pattern_compiler, TreeSitterPatternCompiler)
