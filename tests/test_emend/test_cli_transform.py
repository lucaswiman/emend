"""Tests for CLI transform commands."""

import pytest
from pathlib import Path
from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()


class TestGetCommand:
    """Tests for 'search' command (lookup mode)."""

    def test_get_params_all(self, tmp_path):
        """Get all parameters from a function."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func(self, x: int, y: str = 'default', *args, **kwargs):\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[params]"])

        assert result.exit_code == 0
        assert result.stdout.strip() == "self, x: int, y: str = 'default', *args, **kwargs"

    def test_get_returns(self, tmp_path):
        """Get return annotation from a function."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func() -> str | None:\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[returns]"])

        assert result.exit_code == 0
        assert result.stdout.strip() == "str | None"

    def test_get_decorators_all(self, tmp_path):
        """Get all decorators from a function."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@property\n"
            "@lru_cache\n"
            "def func():\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[decorators]"])

        assert result.exit_code == 0
        assert result.stdout.strip() == "@property\n@lru_cache"

    def test_get_param_by_name(self, tmp_path):
        """Get specific parameter by name."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func(ctx, request, debug: bool = False):\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[params][ctx]"])

        assert result.exit_code == 0
        assert result.stdout.strip() == "ctx"

    def test_get_param_by_index(self, tmp_path):
        """Get specific parameter by index."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func(ctx, request, debug: bool = False):\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[params][0]"])

        assert result.exit_code == 0
        assert result.stdout.strip() == "ctx"

    def test_get_bases_all(self, tmp_path):
        """Get all base classes from a class."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass(BaseClass, Protocol):\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::MyClass[bases]"])

        assert result.exit_code == 0
        assert result.stdout.strip() == "BaseClass, Protocol"

    def test_get_body(self, tmp_path):
        """Get body from a function."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func():\n"
            "    x = 1\n"
            "    return x + 2\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[body]"])

        assert result.exit_code == 0
        assert "x = 1" in result.stdout
        assert "return x + 2" in result.stdout

    def test_get_nonexistent_file(self, tmp_path):
        """Error when file doesn't exist."""
        result = runner.invoke(app, ["search", f"{tmp_path}/nonexistent.py::func[returns]"])

        assert result.exit_code != 0
        assert "does not exist" in result.stderr.lower() or "cannot find" in result.stderr.lower() or "error" in result.stderr.lower()

    def test_get_nonexistent_symbol(self, tmp_path):
        """Error when symbol doesn't exist."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func():\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::nonexistent[returns]"])

        assert result.exit_code != 0
        assert "Error" in result.stdout or "Error" in result.stderr

    def test_get_no_returns(self, tmp_path):
        """Error when function has no return annotation."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func():\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[returns]"])

        assert result.exit_code != 0
        assert "no return annotation" in result.stderr.lower()

    def test_get_no_decorators(self, tmp_path):
        """Get decorators when function has no decorators."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def func():\n"
            "    pass\n"
        )

        result = runner.invoke(app, ["search", f"{test_file}::func[decorators]"])

        assert result.exit_code == 0
        assert result.stdout.strip() == ""


class TestSetCommand:
    """Tests for 'edit' command."""

    def test_set_returns_dry_run(self, tmp_path):
        """Set return annotation (dry-run shows diff)."""
        test_file = tmp_path / "test.py"
        original = "def func() -> int:\n    pass\n"
        test_file.write_text(original)

        result = runner.invoke(app, ["edit", f"{test_file}::func[returns]", "str | None"])

        assert result.exit_code == 0
        # Dry-run should show diff
        assert "-> int:" in result.stdout
        assert "-> str | None:" in result.stdout
        # File should not be modified
        assert test_file.read_text() == original

    def test_set_returns_apply(self, tmp_path):
        """Set return annotation with --apply modifies file."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func() -> int:\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[returns]", "str | None", "--apply"])

        assert result.exit_code == 0
        # File should be modified
        content = test_file.read_text()
        assert "-> str | None:" in content
        assert "-> int:" not in content

    def test_set_returns_add_new(self, tmp_path):
        """Add return annotation to function without one."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func():\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[returns]", "str", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "-> str:" in content

    def test_set_returns_remove(self, tmp_path):
        """Remove return annotation by setting to empty string."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func() -> int:\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[returns]", "", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "-> int:" not in content
        assert "def func():" in content

    def test_set_params_all(self, tmp_path):
        """Set all parameters in a function."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(old_param: int):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[params]", "x: int, y: str", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(x: int, y: str):" in content
        assert "old_param" not in content

    def test_set_param_by_name(self, tmp_path):
        """Set specific parameter by name."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[params][x]", "x: float", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "x: float" in content
        assert "y: str" in content

    def test_set_param_by_index(self, tmp_path):
        """Set specific parameter by index."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[params][1]", "y: int", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "y: int" in content

    def test_set_decorators_all(self, tmp_path):
        """Set all decorators on a function."""
        test_file = tmp_path / "test.py"
        test_file.write_text("@old_decorator\ndef func():\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[decorators]", "@property\n@cache", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "@property" in content
        assert "@cache" in content
        assert "@old_decorator" not in content

    def test_set_bases_all(self, tmp_path):
        """Set all base classes on a class."""
        test_file = tmp_path / "test.py"
        test_file.write_text("class MyClass(OldBase):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::MyClass[bases]", "NewBase, Protocol", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "class MyClass(NewBase, Protocol):" in content

    def test_set_nonexistent_file(self, tmp_path):
        """Error when file doesn't exist."""
        result = runner.invoke(app, ["edit", f"{tmp_path}/nonexistent.py::func[returns]", "str"])

        assert result.exit_code != 0
        assert "does not exist" in result.stderr.lower() or "cannot find" in result.stderr.lower() or "error" in result.stderr.lower()


class TestAddCommand:
    """Tests for 'add' command."""

    def test_add_param_at_end(self, tmp_path):
        """Add parameter at end (default position)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params]:POSITIONAL_OR_KEYWORD", "debug: bool = False", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(x: int, y: str, debug: bool = False):" in content

    def test_add_param_at_start(self, tmp_path):
        """Add parameter at beginning with --at 0."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params]", "self", "--at", "0", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(self, x: int, y: str):" in content

    def test_add_param_at_index(self, tmp_path):
        """Add parameter at specific index."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, z: str):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params]", "y: float", "--at", "1", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(x: int, y: float, z: str):" in content

    def test_add_param_to_empty(self, tmp_path):
        """Add parameter to function with no params."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func():\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params]:POSITIONAL_OR_KEYWORD", "x: int", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(x: int):" in content

    def test_add_decorator_at_end(self, tmp_path):
        """Add decorator at end (default position)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("@property\ndef func():\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[decorators]", "@cache", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "@property" in content
        assert "@cache" in content

    def test_add_decorator_at_start(self, tmp_path):
        """Add decorator at beginning with --at 0."""
        test_file = tmp_path / "test.py"
        test_file.write_text("@cache\ndef func():\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[decorators]", "@staticmethod", "--at", "0", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "@staticmethod" in content
        assert "@cache" in content

    def test_add_decorator_to_empty(self, tmp_path):
        """Add decorator to function with no decorators."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func():\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[decorators]", "@property", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "@property" in content

    def test_add_base_at_end(self, tmp_path):
        """Add base class at end."""
        test_file = tmp_path / "test.py"
        test_file.write_text("class MyClass(BaseClass):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::MyClass[bases]", "Protocol", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "class MyClass(BaseClass, Protocol):" in content

    def test_add_base_at_start(self, tmp_path):
        """Add base class at beginning."""
        test_file = tmp_path / "test.py"
        test_file.write_text("class MyClass(Protocol):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::MyClass[bases]", "Generic[T]", "--at", "0", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "class MyClass(Generic[T], Protocol):" in content

    def test_add_base_to_empty(self, tmp_path):
        """Add base class to class with no bases."""
        test_file = tmp_path / "test.py"
        test_file.write_text("class MyClass:\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::MyClass[bases]", "Protocol", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "class MyClass(Protocol):" in content

    def test_add_dry_run(self, tmp_path):
        """Add without --apply shows diff without modifying file."""
        test_file = tmp_path / "test.py"
        original = "def func(x: int):\n    pass\n"
        test_file.write_text(original)

        result = runner.invoke(app, ["add", f"{test_file}::func[params]", "y: str"])

        assert result.exit_code == 0
        # Should show diff
        assert "def func(x: int):" in result.stdout
        assert "def func(x: int, y: str):" in result.stdout
        # File should not be modified
        assert test_file.read_text() == original

    def test_add_with_accessor_error(self, tmp_path):
        """Error when selector has accessor (add only works on lists, not items)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params][x]", "y: str"])

        assert result.exit_code != 0
        assert "cannot add to item" in result.stderr.lower() or "add only works on" in result.stderr.lower() or "error" in result.stderr.lower()

    def test_add_to_returns_error(self, tmp_path):
        """Error when adding to returns (not a list component)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func() -> int:\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[returns]", "str"])

        assert result.exit_code != 0
        assert "not a list component" in result.stderr.lower()

    def test_add_param_before_name(self, tmp_path):
        """Add parameter before a named parameter using --before."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(self, y: str):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params]", "x: int", "--before", "y", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(self, x: int, y: str):" in content

    def test_add_param_after_name(self, tmp_path):
        """Add parameter after a named parameter using --after."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(self, x: int):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params]", "y: str", "--after", "self", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(self, y: str, x: int):" in content

    def test_add_decorator_before_name(self, tmp_path):
        """Add decorator before a named decorator using --before."""
        test_file = tmp_path / "test.py"
        test_file.write_text("@property\ndef func():\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[decorators]", "@cache", "--before", "property", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        lines = content.split('\n')
        # @cache should come before @property
        cache_line = next(i for i, line in enumerate(lines) if '@cache' in line)
        property_line = next(i for i, line in enumerate(lines) if '@property' in line)
        assert cache_line < property_line

    def test_add_base_after_name(self, tmp_path):
        """Add base class after a named base using --after."""
        test_file = tmp_path / "test.py"
        test_file.write_text("class MyClass(ABC):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::MyClass[bases]", "Protocol", "--after", "ABC", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "class MyClass(ABC, Protocol):" in content

    def test_add_before_after_mutual_exclusion(self, tmp_path):
        """Error when both --before and --after are provided."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, z: str):\n    pass\n")

        result = runner.invoke(app, ["add", f"{test_file}::func[params]", "y: float", "--before", "z", "--after", "x"])

        assert result.exit_code != 0
        assert "Error" in result.stdout or "Error" in result.stderr


class TestRmCommand:
    """Tests for 'edit' command."""

    def test_rm_param_by_name(self, tmp_path):
        """Remove parameter by name."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str, z: bool):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[params][y]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(x: int, z: bool):" in content
        assert "y: str" not in content

    def test_rm_param_by_index(self, tmp_path):
        """Remove parameter by index."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str, z: bool):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[params][1]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(x: int, z: bool):" in content

    def test_rm_param_last_index(self, tmp_path):
        """Remove last parameter using its index (grammar doesn't support negative indices)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str, z: bool):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[params][2]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func(x: int, y: str):" in content

    def test_rm_decorator_by_name(self, tmp_path):
        """Remove decorator by name."""
        test_file = tmp_path / "test.py"
        test_file.write_text("@property\n@cache\n@lru_cache\ndef func():\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[decorators][cache]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "@property" in content
        assert "@lru_cache" in content
        assert "@cache" not in content

    def test_rm_decorator_by_index(self, tmp_path):
        """Remove decorator by index."""
        test_file = tmp_path / "test.py"
        test_file.write_text("@property\n@cache\ndef func():\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[decorators][0]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "@property" not in content
        assert "@cache" in content

    def test_rm_base_by_name(self, tmp_path):
        """Remove base class by name."""
        test_file = tmp_path / "test.py"
        test_file.write_text("class MyClass(BaseClass, Protocol, Generic[T]):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::MyClass[bases][Protocol]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "class MyClass(BaseClass, Generic[T]):" in content
        assert "Protocol" not in content

    def test_rm_all_decorators(self, tmp_path):
        """Remove all decorators when no accessor."""
        test_file = tmp_path / "test.py"
        test_file.write_text("@property\n@cache\ndef func():\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[decorators]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "@property" not in content
        assert "@cache" not in content
        assert "def func():" in content

    def test_rm_all_bases(self, tmp_path):
        """Remove all base classes when no accessor."""
        test_file = tmp_path / "test.py"
        test_file.write_text("class MyClass(BaseClass, Protocol):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::MyClass[bases]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "class MyClass:" in content
        assert "BaseClass" not in content
        assert "Protocol" not in content

    def test_rm_all_params(self, tmp_path):
        """Remove all parameters when no accessor."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func(x: int, y: str):\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[params]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func():" in content
        assert "x: int" not in content

    def test_rm_returns(self, tmp_path):
        """Remove return annotation."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func() -> int:\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[returns]", "--rm", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "def func():" in content
        assert "-> int:" not in content

    def test_rm_dry_run(self, tmp_path):
        """Remove without --apply shows diff without modifying file."""
        test_file = tmp_path / "test.py"
        original = "def func(x: int, y: str):\n    pass\n"
        test_file.write_text(original)

        result = runner.invoke(app, ["edit", f"{test_file}::func[params][y]", "--rm"])

        assert result.exit_code == 0
        # Should show diff
        assert "def func(x: int, y: str):" in result.stdout
        assert "def func(x: int):" in result.stdout
        # File should not be modified
        assert test_file.read_text() == original

    def test_rm_body_error(self, tmp_path):
        """Error when removing body (not allowed)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def func():\n    pass\n")

        result = runner.invoke(app, ["edit", f"{test_file}::func[body]", "--rm"])

        assert result.exit_code != 0
        assert "Error" in result.stdout or "Error" in result.stderr


class TestFindCommand:
    """Tests for 'search' command (pattern mode)."""

    def test_find_simple_pattern(self, tmp_path):
        """Find a pattern with metavariable."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        # Should show file:line format
        assert f"{str(test_file)}:1" in result.stdout

    def test_find_no_matches(self, tmp_path):
        """Find pattern that doesn't match anything."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 5\n"
            "y = 10\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        # Should indicate no matches (empty output)
        assert result.stdout.strip() == ""

    def test_find_multiple_matches(self, tmp_path):
        """Find pattern that matches multiple times."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
            "print('test')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file)])

        assert result.exit_code == 0
        # Should show 3 matches (3 lines of output)
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 3
        # Each line should reference the file in file:line format
        for i, line in enumerate(lines):
            assert f"{str(test_file)}:" in line

    def test_find_count(self, tmp_path):
        """Find with --count shows only count."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--output", "count"])

        assert result.exit_code == 0
        assert "2" in result.stdout

    def test_find_nonexistent_file(self, tmp_path):
        """Error when file doesn't exist."""
        result = runner.invoke(app, ["search", "print($X)", f"{tmp_path}/nonexistent.py"])

        assert result.exit_code != 0
        assert "does not exist" in result.stderr.lower() or "cannot find" in result.stderr.lower() or "error" in result.stderr.lower()

    def test_find_json_includes_matches(self, tmp_path):
        """Find with --json should include match details with line numbers and captures."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "x = 5\n"
            "print('world')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--output", "json"])

        assert result.exit_code == 0
        # Parse JSON output
        import json
        data = json.loads(result.stdout)
        # Should have matches array
        assert "matches" in data
        assert len(data["matches"]) == 2
        # Each match should have line, code, and captures
        for match in data["matches"]:
            assert "line" in match
            assert match["line"] is not None
            assert "code" in match
            assert "captures" in match
        # Verify specific matches
        assert data["matches"][0]["line"] == 1
        assert "print" in data["matches"][0]["code"]
        assert data["matches"][1]["line"] == 3

    def test_find_with_scope(self, tmp_path):
        """Find only inside a named function using --in scope."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('global')\n"
            "def my_func():\n"
            "    print('inside')\n"
            "def other_func():\n"
            "    print('other')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "my_func"])

        assert result.exit_code == 0
        # Should find only the print inside my_func (line 3)
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 1
        assert ":3" in lines[0]

    def test_find_with_dotted_scope(self, tmp_path):
        """Find only inside a nested scope using --in with dotted path."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('inside method')\n"
            "    def other_method(self):\n"
            "        print('other')\n"
            "def func():\n"
            "    print('in func')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "MyClass.method"])

        assert result.exit_code == 0
        # Should find only the print inside MyClass.method (line 3)
        lines = result.stdout.strip().split('\n')
        assert len(lines) == 1
        assert ":3" in lines[0]


class TestReplaceCommand:
    """Tests for 'replace' command."""

    def test_replace_dry_run(self, tmp_path):
        """Replace without --apply shows diff without modifying file."""
        test_file = tmp_path / "test.py"
        original = "print('hello')\nx = 5\n"
        test_file.write_text(original)

        result = runner.invoke(app, ["replace", "print('hello')", "logger.info('hello')", str(test_file)])

        assert result.exit_code == 0
        # Should show diff
        assert "print('hello')" in result.stdout
        assert "logger.info('hello')" in result.stdout
        # File should not be modified
        assert test_file.read_text() == original

    def test_replace_apply(self, tmp_path):
        """Replace with --apply modifies file."""
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')\nprint('world')\n")

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "logger.info('hello')" in content
        assert "logger.info('world')" in content
        assert "print(" not in content

    def test_replace_with_metavar(self, tmp_path):
        """Replace pattern with metavariable."""
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')\nprint('world')\n")

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "logger.info('hello')" in content
        assert "logger.info('world')" in content

    def test_replace_multiple_metavars(self, tmp_path):
        """Replace pattern with multiple metavariables."""
        test_file = tmp_path / "test.py"
        test_file.write_text("assertEqual(x, 5)\nassertEqual(y, 10)\n")

        result = runner.invoke(app, ["replace", "assertEqual($A, $B)", "assert $A == $B", str(test_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "assert x == 5" in content
        assert "assert y == 10" in content

    def test_replace_no_matches(self, tmp_path):
        """Replace pattern that doesn't match returns empty diff."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 5\ny = 10\n")

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file)])

        assert result.exit_code == 0
        # Empty output when no matches
        assert result.stdout.strip() == ""

    def test_replace_nonexistent_file(self, tmp_path):
        """Error when file doesn't exist."""
        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", f"{tmp_path}/nonexistent.py"])

        assert result.exit_code != 0
        assert "Error" in result.stdout or "Error" in result.stderr

    def test_replace_dry_run_output_format(self, tmp_path):
        """Replace dry-run should output diff format, not tuple repr."""
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')\n")

        result = runner.invoke(app, ["replace", "print('hello')", "logger.info('hello')", str(test_file)])

        assert result.exit_code == 0
        # Should contain diff markers
        assert "---" in result.stdout or "+++" in result.stdout
        # Should NOT start with tuple opening paren or contain tuple closing with count
        assert not result.stdout.startswith("(")
        assert not result.stdout.endswith(", 1)")
        assert not result.stdout.endswith(", 1)\n")

    def test_replace_with_scope(self, tmp_path):
        """Replace only inside a named function using --in scope."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('global')\n"
            "def my_func():\n"
            "    print('inside')\n"
            "def other_func():\n"
            "    print('other')\n"
        )

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--where", "my_func", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only inside my_func
        assert "print('global')" in content  # Global not replaced
        assert "logger.info('inside')" in content  # Inside my_func replaced
        assert "print('other')" in content  # Inside other_func not replaced

    def test_replace_with_dotted_scope(self, tmp_path):
        """Replace only inside a nested scope using --in with dotted path."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('inside method')\n"
            "    def other_method(self):\n"
            "        print('other')\n"
            "def func():\n"
            "    print('in func')\n"
        )

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--where", "MyClass.method", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only inside MyClass.method
        assert "logger.info('inside method')" in content  # Inside MyClass.method replaced
        assert "print('other')" in content  # Inside other_method not replaced
        assert "print('in func')" in content  # Inside func not replaced


class TestFindReplaceConstraints:
    """Tests for search/replace with --inside/--not-inside constraints."""

    def test_find_inside_cli(self, tmp_path):
        """Test find with --inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('module')\n"
            "def func():\n"
            "    print('inside func')\n"
            "class MyClass:\n"
            "    def method(self):\n"
            "        print('inside method')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "def"])

        assert result.exit_code == 0
        # Should find prints inside functions only (2 matches)
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2
        # Each match should be in file:line format
        assert all(f"{str(test_file)}:" in line for line in lines)

    def test_find_not_inside_cli(self, tmp_path):
        """Test find with --not-inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('before')\n"
            "if condition:\n"
            "    print('inside if')\n"
            "print('after')\n"
        )

        result = runner.invoke(app, ["search", "print($X)", str(test_file), "--where", "not if"])

        assert result.exit_code == 0
        # Should find prints outside if blocks only (2 matches)
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2

    def test_replace_inside_cli(self, tmp_path):
        """Test replace with --inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('module')\n"
            "def func():\n"
            "    print('inside func')\n"
        )

        result = runner.invoke(app, ["replace", "print($X)", "logger.info($X)", str(test_file), "--where", "def", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only inside function
        assert "print('module')" in content  # Module level not replaced
        assert "logger.info('inside func')" in content  # Inside func replaced

    def test_replace_not_inside_cli(self, tmp_path):
        """Test replace with --not-inside constraint."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 1\n"
            "class MyClass:\n"
            "    y = 2\n"
            "z = 3\n"
        )

        result = runner.invoke(app, ["replace", "$NAME = $VALUE", "$NAME: int = $VALUE", str(test_file), "--where", "not class", "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        # Should replace only outside class
        assert "x: int = 1" in content  # Outside class replaced
        assert "z: int = 3" in content  # Outside class replaced
        assert "y = 2" in content  # Inside class not replaced

    def test_find_inside_and_not_inside_conflict(self, tmp_path):
        """Test that --inside and --not-inside cannot be used together."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        result = runner.invoke(app, ["search", "$X = $Y", str(test_file), "--where", "def", "--where", "not class"])

        assert result.exit_code == 2
        assert "Cannot specify both" in result.stdout or "Cannot specify both" in result.stderr


class TestFindReplaceMultiFile:
    """Tests for search/replace across multiple files using globs."""

    def test_find_glob_pattern(self, tmp_path):
        """Test find with glob pattern matching multiple files."""
        # Create multiple test files
        file1 = tmp_path / "test1.py"
        file1.write_text("print('file1')\n")
        file2 = tmp_path / "test2.py"
        file2.write_text("print('file2')\n")
        file3 = tmp_path / "other.py"
        file3.write_text("print('other')\n")

        # Use glob pattern to match test*.py
        result = runner.invoke(app, ["search", "print($X)", str(tmp_path / "test*.py")])

        assert result.exit_code == 0
        output = result.stdout
        # Should find matches in both test1.py and test2.py
        assert "test1.py" in output
        assert "test2.py" in output
        # Should NOT find match in other.py (doesn't match pattern)
        assert "other.py" not in output

    def test_find_directory(self, tmp_path):
        """Test find with directory path searches all .py files recursively."""
        # Create nested directory structure
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file1 = tmp_path / "file1.py"
        file1.write_text("print('top')\n")
        file2 = subdir / "file2.py"
        file2.write_text("print('nested')\n")
        non_py = tmp_path / "file3.txt"
        non_py.write_text("print('not py')\n")

        result = runner.invoke(app, ["search", "print($X)", str(tmp_path)])

        assert result.exit_code == 0
        output = result.stdout
        # Should find matches in both .py files
        assert "file1.py" in output
        assert "file2.py" in output
        # Should NOT search non-.py files
        assert "file3.txt" not in output

    def test_replace_glob_pattern(self, tmp_path):
        """Test replace with glob pattern across multiple files."""
        file1 = tmp_path / "test1.py"
        file1.write_text("old('file1')\n")
        file2 = tmp_path / "test2.py"
        file2.write_text("old('file2')\n")
        file3 = tmp_path / "other.py"
        file3.write_text("old('other')\n")

        result = runner.invoke(app, ["replace", "old($X)", "new($X)", str(tmp_path / "test*.py"), "--apply"])

        assert result.exit_code == 0
        # Check files were modified
        assert "new('file1')" in file1.read_text()
        assert "new('file2')" in file2.read_text()
        # other.py should NOT be modified (doesn't match glob)
        assert "old('other')" in file3.read_text()

    def test_replace_directory(self, tmp_path):
        """Test replace with directory path modifies all .py files."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        file1 = tmp_path / "file1.py"
        file1.write_text("x = 1\n")
        file2 = subdir / "file2.py"
        file2.write_text("x = 2\n")

        result = runner.invoke(app, ["replace", "$NAME = $VALUE", "$NAME: int = $VALUE", str(tmp_path), "--apply"])

        assert result.exit_code == 0
        # Both files should be modified
        assert "x: int = 1" in file1.read_text()
        assert "x: int = 2" in file2.read_text()
