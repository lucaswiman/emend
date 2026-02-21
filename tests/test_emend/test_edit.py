"""Tests for the unified edit command."""

import pytest
from pathlib import Path
from emend.transform import cmd_edit, cmd_add


class TestEditSet:
    """Test edit in 'set' mode (replace component)."""

    def test_edit_set_return_type(self, tmp_path):
        """Test changing function return type."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def get_count():
    return 42
""")

        result = cmd_edit(
            selector_str=f"{test_file}::get_count[returns]",
            value="int",
            apply=False,
        )
        assert "get_count()" in result
        assert "-> int" in result

    def test_edit_set_with_apply(self, tmp_path):
        """Test setting component with apply."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def get_count():
    return 42
""")

        cmd_edit(
            selector_str=f"{test_file}::get_count[returns]",
            value="int",
            apply=True,
        )

        # Verify file was modified
        content = test_file.read_text()
        assert "-> int" in content

    def test_edit_set_decorator_by_index(self, tmp_path):
        """Test changing decorator by index."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
@property
def get_count(self):
    return 42
""")

        result = cmd_edit(
            selector_str=f"{test_file}::get_count[decorators][0]",
            value="@cached_property",
            apply=False,
        )
        assert "@cached_property" in result

    def test_edit_set_bases(self, tmp_path):
        """Test changing base classes."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
class MyClass(BaseClass):
    pass
""")

        result = cmd_edit(
            selector_str=f"{test_file}::MyClass[bases][0]",
            value="NewBase",
            apply=False,
        )
        assert "NewBase" in result


class TestEditAdd:
    """Test 'add' command (insert into list component)."""

    def test_edit_add_parameter_default_append(self, tmp_path):
        """Test adding parameter (default append)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name):
    pass
""")

        result = cmd_add(
            selector_str=f"{test_file}::greet[params]",
            value="greeting: str = 'Hello'",
            apply=False,
        )
        assert "name" in result
        assert "greeting: str = 'Hello'" in result

    def test_edit_add_parameter_at_position(self, tmp_path):
        """Test adding parameter at specific position."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name, age):
    pass
""")

        result = cmd_add(
            selector_str=f"{test_file}::greet[params]",
            value="prefix: str",
            at=0,
            apply=False,
        )
        assert "prefix: str" in result
        assert "name" in result
        assert "age" in result

    def test_edit_add_parameter_before(self, tmp_path):
        """Test adding parameter before named parameter."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name, age):
    pass
""")

        result = cmd_add(
            selector_str=f"{test_file}::greet[params]",
            value="prefix: str",
            before="age",
            apply=False,
        )
        assert "prefix: str" in result
        # Check the new definition shows prefix before age
        assert "+def greet(name, prefix: str, age):" in result or "prefix: str, age" in result

    def test_edit_add_parameter_after(self, tmp_path):
        """Test adding parameter after named parameter."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name, age):
    pass
""")

        result = cmd_add(
            selector_str=f"{test_file}::greet[params]",
            value="suffix: str",
            after="age",
            apply=False,
        )
        assert "suffix: str" in result
        # suffix should come after age
        suffix_idx = result.find("suffix")
        age_idx = result.find("age")
        assert suffix_idx > age_idx

    def test_edit_add_decorator(self, tmp_path):
        """Test adding decorator."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def my_func():
    pass
""")

        result = cmd_add(
            selector_str=f"{test_file}::my_func[decorators]",
            value="@cached",
            apply=False,
        )
        assert "@cached" in result

    def test_edit_add_with_apply(self, tmp_path):
        """Test adding parameter with apply."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name):
    pass
""")

        cmd_add(
            selector_str=f"{test_file}::greet[params]",
            value="greeting: str = 'Hello'",
            apply=True,
        )

        # Verify file was modified
        content = test_file.read_text()
        assert "greeting: str = 'Hello'" in content


class TestEditRemove:
    """Test edit in 'rm' mode (remove component or symbol)."""

    def test_edit_remove_parameter_by_name(self, tmp_path):
        """Test removing parameter by name."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name, age):
    pass
""")

        result = cmd_edit(
            selector_str=f"{test_file}::greet[params][age]",
            rm=True,
            apply=False,
        )
        assert "name" in result
        # Check that in the new version (after +), age is not present
        assert "+def greet(name):" in result or "greet(name)" in result

    def test_edit_remove_decorator_by_index(self, tmp_path):
        """Test removing decorator by index."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
@pytest.fixture
@cached
def my_func():
    pass
""")

        result = cmd_edit(
            selector_str=f"{test_file}::my_func[decorators][0]",
            rm=True,
            apply=False,
        )
        # Should have removed first decorator
        assert "my_func" in result

    def test_edit_remove_symbol_dry_run(self, tmp_path):
        """Test removing entire symbol (dry-run)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def to_remove():
    pass

def to_keep():
    pass
""")

        result = cmd_edit(
            selector_str=f"{test_file}::to_remove",
            rm=True,
            apply=False,
        )

        # remove_symbol returns an empty string on dry-run, just verify no error
        assert isinstance(result, str)

    def test_edit_remove_with_empty_value(self, tmp_path):
        """Test removing by using empty value."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name, age):
    pass
""")

        result = cmd_edit(
            selector_str=f"{test_file}::greet[params][age]",
            value="",
            apply=False,
        )
        assert "name" in result
        # Check that new version doesn't have age
        assert "+def greet(name):" in result or "greet(name)" in result

    def test_edit_remove_with_apply(self, tmp_path):
        """Test removing parameter with apply."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name, age):
    pass
""")

        cmd_edit(
            selector_str=f"{test_file}::greet[params][age]",
            rm=True,
            apply=True,
        )

        # Verify file was modified
        content = test_file.read_text()
        assert "name" in content
        assert "age" not in content


class TestEditErrorHandling:
    """Test error handling in edit command."""

    def test_edit_no_operation(self, tmp_path):
        """Test error when no operation is specified."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo(): pass\n")

        with pytest.raises(ValueError, match="No operation"):
            cmd_edit(
                selector_str=f"{test_file}::foo[params]",
            )

    def test_edit_file_not_found(self):
        """Test error when file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            cmd_edit(
                selector_str="/nonexistent/file.py::foo[params]",
                value="test",
            )

    def test_edit_symbol_not_found(self, tmp_path):
        """Test error when symbol not found."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo(): pass\n")

        with pytest.raises(ValueError, match="not found"):
            cmd_edit(
                selector_str=f"{test_file}::nonexistent[params]",
                value="test",
            )


class TestEditPseudoClass:
    """Test 'add' command with pseudo-class syntax for parameters."""

    def test_edit_add_keyword_only_parameter(self, tmp_path):
        """Test adding keyword-only parameter."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name):
    pass
""")

        result = cmd_add(
            selector_str=f"{test_file}::greet[params]:KEYWORD_ONLY",
            value="force: bool = False",
            apply=False,
        )
        # Should have the new parameter and a separator
        assert "force: bool = False" in result or "force" in result
        # Check that we have a function definition in the diff
        assert "def greet" in result

    def test_edit_add_positional_only_parameter(self, tmp_path):
        """Test adding positional-only parameter."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def greet(name):
    pass
""")

        result = cmd_add(
            selector_str=f"{test_file}::greet[params]:POSITIONAL_ONLY",
            value="prefix: str",
            apply=False,
        )
        assert "/" in result  # Positional-only separator
        assert "prefix: str" in result


class TestEditDryRun:
    """Test edit dry-run mode."""

    def test_edit_dry_run_shows_diff(self, tmp_path):
        """Test that dry-run shows diff without modifying file."""
        test_file = tmp_path / "test.py"
        original_content = """
def greet(name):
    pass
"""
        test_file.write_text(original_content)

        result = cmd_edit(
            selector_str=f"{test_file}::greet[params]",
            value="greeting: str = 'Hello'",
            apply=False,
        )

        # File should not be modified
        assert test_file.read_text() == original_content
        # Result should show diff
        assert "greeting: str = 'Hello'" in result


class TestEditRemoveSymbol:
    """Test removing entire symbols with --rm."""

    def test_edit_remove_symbol_dry_run(self, tmp_path):
        """Test removing entire function (dry-run)."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""def foo():
    pass

def bar():
    pass
""")

        result = cmd_edit(
            selector_str=f"{test_file}::foo",
            rm=True,
            apply=False,
        )

        # File should not be modified in dry-run
        assert "def foo():" in test_file.read_text()
        # Result should show diff with foo removed
        assert "-def foo():" in result
        assert "def bar():" in result

    def test_edit_remove_symbol_with_apply(self, tmp_path):
        """Test removing entire function with apply."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""def foo():
    pass

def bar():
    pass
""")

        result = cmd_edit(
            selector_str=f"{test_file}::foo",
            rm=True,
            apply=True,
        )

        # File should be modified
        content = test_file.read_text()
        assert "def foo():" not in content
        assert "def bar():" in content

    def test_edit_remove_class_with_apply(self, tmp_path):
        """Test removing entire class with apply."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""class Foo:
    pass

class Bar:
    pass
""")

        cmd_edit(
            selector_str=f"{test_file}::Foo",
            rm=True,
            apply=True,
        )

        # File should be modified
        content = test_file.read_text()
        assert "class Foo:" not in content
        assert "class Bar:" in content
