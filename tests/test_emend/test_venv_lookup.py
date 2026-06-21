"""Tests for virtual environment symbol lookup.

Tests that emend can look up symbols in .venv/venv site-packages
when they're not found in the project index, with configurable
paths via pyproject.toml [tool.emend] and .emend/config.toml.
"""

import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# project_config tests
# ---------------------------------------------------------------------------


class TestProjectConfig:
    """Test project-level configuration loading."""

    def test_default_venv_config(self, tmp_path):
        """Default config has venv lookup enabled with ['.venv', 'venv']."""
        from emend.project_config import get_environment_lookup_config, load_project_config

        load_project_config.cache_clear()
        cfg = get_environment_lookup_config(str(tmp_path))
        assert cfg.enabled is True
        assert cfg.paths == [".venv", "venv"]

    def test_pyproject_toml_override(self, tmp_path):
        """pyproject.toml [tool.emend.environment_lookup] overrides defaults."""
        from emend.project_config import load_project_config

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [tool.emend.environment_lookup]
            enabled = false
            paths = ["my_venv"]
        """))

        load_project_config.cache_clear()
        cfg = load_project_config(str(tmp_path))
        assert cfg["environment_lookup"]["enabled"] is False
        assert cfg["environment_lookup"]["paths"] == ["my_venv"]

    def test_emend_config_toml_override(self, tmp_path):
        """`.emend/config.toml` takes priority over pyproject.toml."""
        from emend.project_config import load_project_config

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [tool.emend.environment_lookup]
            paths = ["from_pyproject"]
        """))

        emend_dir = tmp_path / ".emend"
        emend_dir.mkdir()
        (emend_dir / "config.toml").write_text(textwrap.dedent("""\
            [environment_lookup]
            paths = [".custom_venv"]
        """))

        load_project_config.cache_clear()
        cfg = load_project_config(str(tmp_path))
        assert cfg["environment_lookup"]["paths"] == [".custom_venv"]

    def test_partial_override_preserves_defaults(self, tmp_path):
        """Overriding only 'paths' preserves default 'enabled'."""
        from emend.project_config import load_project_config

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [tool.emend.environment_lookup]
            paths = ["env"]
        """))

        load_project_config.cache_clear()
        cfg = load_project_config(str(tmp_path))
        assert cfg["environment_lookup"]["enabled"] is True
        assert cfg["environment_lookup"]["paths"] == ["env"]

    def test_string_paths_treated_as_single_element(self, tmp_path):
        """A bare string 'paths' value should become a single-element list, not a character list."""
        from emend.project_config import get_environment_lookup_config, load_project_config

        emend_dir = tmp_path / ".emend"
        emend_dir.mkdir()
        (emend_dir / "config.toml").write_text(textwrap.dedent("""\
            [environment_lookup]
            paths = "my_venv"
        """))

        load_project_config.cache_clear()
        cfg = get_environment_lookup_config(str(tmp_path))
        assert cfg.paths == ["my_venv"], (
            f"String paths value should become ['my_venv'], got {cfg.paths}"
        )


# ---------------------------------------------------------------------------
# resolve_venv_site_packages tests
# ---------------------------------------------------------------------------


class TestResolveVenvSitePackages:
    """Test venv site-packages directory resolution."""

    def test_finds_dot_venv(self, tmp_path):
        """Finds site-packages in .venv."""
        from emend.project_config import resolve_environment_path, load_project_config

        sp = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)

        load_project_config.cache_clear()
        result = resolve_environment_path(str(tmp_path))
        assert result == sp

    def test_finds_venv(self, tmp_path):
        """Falls back to venv/ if .venv/ doesn't exist."""
        from emend.project_config import resolve_environment_path, load_project_config

        sp = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
        sp.mkdir(parents=True)

        load_project_config.cache_clear()
        result = resolve_environment_path(str(tmp_path))
        assert result == sp

    def test_returns_none_when_disabled(self, tmp_path):
        """Returns None when venv lookup is disabled."""
        from emend.project_config import resolve_environment_path, load_project_config

        sp = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [tool.emend.environment_lookup]
            enabled = false
        """))

        load_project_config.cache_clear()
        result = resolve_environment_path(str(tmp_path))
        assert result is None

    def test_returns_none_when_no_venv(self, tmp_path):
        """Returns None when no venv directory exists."""
        from emend.project_config import resolve_environment_path, load_project_config

        load_project_config.cache_clear()
        result = resolve_environment_path(str(tmp_path))
        assert result is None

    def test_custom_venv_path(self, tmp_path):
        """Finds site-packages using custom venv path from config."""
        from emend.project_config import resolve_environment_path, load_project_config

        sp = tmp_path / "my_env" / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [tool.emend.environment_lookup]
            paths = ["my_env"]
        """))

        load_project_config.cache_clear()
        result = resolve_environment_path(str(tmp_path))
        assert result == sp


# ---------------------------------------------------------------------------
# Helper to create a fake project with venv
# ---------------------------------------------------------------------------


def _make_project_with_venv(tmp_path: Path, venv_name: str = ".venv") -> Path:
    """Create a project root with a pyproject.toml and fake venv."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    sp = tmp_path / venv_name / "lib" / "python3.12" / "site-packages"
    sp.mkdir(parents=True)
    return sp


def _add_package(site_packages: Path, pkg_name: str, source: str) -> Path:
    """Add a fake package to site-packages."""
    pkg_dir = site_packages / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    init = pkg_dir / "__init__.py"
    init.write_text(source)
    return pkg_dir


# ---------------------------------------------------------------------------
# lookup_venv_symbol tests
# ---------------------------------------------------------------------------


class TestLookupVenvSymbol:
    """Test lookup_venv_symbol function."""

    def test_lookup_by_qualified_name(self, tmp_path):
        """Look up a symbol by qualified name in venv."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        _add_package(sp, "mypkg", textwrap.dedent("""\
            def hello():
                return "world"

            class Widget:
                pass
        """))

        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            qualified_name="mypkg.hello",
        )
        assert len(results) >= 1
        hello_results = [r for r in results if r["name"] == "hello"]
        assert len(hello_results) == 1
        assert hello_results[0]["kind"] == "function"

    def test_lookup_by_qualified_name_class(self, tmp_path):
        """Look up a class by qualified name."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        _add_package(sp, "mypkg", textwrap.dedent("""\
            def hello():
                return "world"

            class Widget:
                pass
        """))

        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            qualified_name="mypkg.Widget",
        )
        widget_results = [r for r in results if r["name"] == "Widget"]
        assert len(widget_results) == 1
        assert widget_results[0]["kind"] == "class"

    def test_lookup_by_name_pattern(self, tmp_path):
        """Look up by name pattern."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        _add_package(sp, "mypkg", textwrap.dedent("""\
            def hello():
                pass

            def help_me():
                pass
        """))

        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            name_pattern="hello",
        )
        assert len(results) >= 1
        assert results[0]["name"] == "hello"

    def test_lookup_submodule(self, tmp_path):
        """Look up a symbol in a submodule."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        pkg = _add_package(sp, "mypkg", "")
        sub = pkg / "sub.py"
        sub.write_text(textwrap.dedent("""\
            def deep_func():
                pass
        """))

        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            qualified_name="mypkg.sub.deep_func",
        )
        assert len(results) >= 1
        deep_results = [r for r in results if r["name"] == "deep_func"]
        assert len(deep_results) == 1

    def test_lookup_pyi_stubs(self, tmp_path):
        """Finds symbols from .pyi stub files."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        pkg_dir = sp / "stubpkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("def real(): pass\n")
        (pkg_dir / "__init__.pyi").write_text("def stub_only() -> str: ...\n")

        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            name_pattern="stub_only",
        )
        assert len(results) >= 1
        assert results[0]["name"] == "stub_only"

    def test_lookup_disabled(self, tmp_path):
        """Returns empty when venv lookup is disabled."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        _add_package(sp, "mypkg", "def hello(): pass\n")

        (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "test"

            [tool.emend.environment_lookup]
            enabled = false
        """))
        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            qualified_name="mypkg.hello",
        )
        assert results == []

    def test_lookup_with_limit(self, tmp_path):
        """Respects limit parameter."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        _add_package(sp, "mypkg", textwrap.dedent("""\
            def a(): pass
            def b(): pass
            def c(): pass
        """))

        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            name_pattern="a",
            limit=1,
        )
        assert len(results) <= 1

    def test_lookup_nonexistent_package(self, tmp_path):
        """Returns empty for packages not in venv."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        _make_project_with_venv(tmp_path)
        load_project_config.cache_clear()

        results = lookup_venv_symbol(
            str(tmp_path),
            qualified_name="nonexistent.foo",
        )
        assert results == []

    def test_venv_index_is_cached(self, tmp_path):
        """Second lookup uses cached index (no rebuild)."""
        from emend.transform import lookup_venv_symbol
        from emend.project_config import load_project_config

        sp = _make_project_with_venv(tmp_path)
        _add_package(sp, "mypkg", "def hello(): pass\n")

        load_project_config.cache_clear()

        # First lookup builds the index
        results1 = lookup_venv_symbol(str(tmp_path), qualified_name="mypkg.hello")
        # Second lookup uses cached index
        results2 = lookup_venv_symbol(str(tmp_path), qualified_name="mypkg.hello")

        assert len(results1) >= 1
        assert len(results2) >= 1

    def test_separate_db_from_project(self, tmp_path):
        """Venv index uses parse_venv.db, not parse.db."""
        from emend.transform import _venv_db_path, _find_project_root

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        project_root = _find_project_root(str(tmp_path))
        venv_db = _venv_db_path(project_root)

        assert venv_db.name == "parse_venv.db"
        assert "parse.db" not in str(venv_db) or "parse_venv.db" in str(venv_db)
