"""Tests for the safe delete command (emend delete --cascade)."""
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector, parse_extended_selector


class TestSafeDeleteBasic:
    """Tests for basic (non-cascade) safe delete."""

    def test_delete_single_function(self, tmp_path):
        """Deleting a single function produces the expected diff."""
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text(
            "def keep():\n    return 1\n\n"
            "def remove_me():\n    return 2\n\n"
            "x = keep()\n"
        )

        sel = parse_extended_selector(f"{src}::remove_me")
        plan = safe_delete(sel, cascade=False, apply=False)

        assert len(plan.deletions) == 1
        assert plan.deletions[0]["name"] == "remove_me"
        assert plan.deletions[0]["reason"] == "target of delete"
        assert str(src) in plan.diffs or src.name in "".join(plan.diffs.values())
        # File should not be modified (dry-run)
        assert "remove_me" in src.read_text()

    def test_delete_single_function_apply(self, tmp_path):
        """With apply=True, the function is actually removed from the file."""
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text(
            "def keep():\n    return 1\n\n"
            "def remove_me():\n    return 2\n\n"
            "x = keep()\n"
        )

        sel = parse_extended_selector(f"{src}::remove_me")
        plan = safe_delete(sel, cascade=False, apply=True)

        assert len(plan.deletions) == 1
        content = src.read_text()
        assert "remove_me" not in content
        assert "keep" in content

    def test_delete_class(self, tmp_path):
        """Can delete a class."""
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text(
            "class Keep:\n    pass\n\n"
            "class Remove:\n    x = 1\n    def method(self):\n        pass\n\n"
            "k = Keep()\n"
        )

        sel = parse_extended_selector(f"{src}::Remove")
        plan = safe_delete(sel, cascade=False, apply=True)

        assert len(plan.deletions) == 1
        assert plan.deletions[0]["name"] == "Remove"
        content = src.read_text()
        assert "Remove" not in content
        assert "Keep" in content

    def test_delete_decorated_function(self, tmp_path):
        """Deleting a decorated function removes the decorator too."""
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text(
            "def decorator(f):\n    return f\n\n"
            "@decorator\n"
            "def remove_me():\n    return 1\n"
        )

        sel = parse_extended_selector(f"{src}::remove_me")
        plan = safe_delete(sel, cascade=False, apply=True)

        content = src.read_text()
        assert "@decorator" not in content
        assert "remove_me" not in content
        assert "def decorator" in content

    def test_delete_nonexistent_raises(self, tmp_path):
        """Deleting a symbol that doesn't exist raises ValueError."""
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text("def keep():\n    return 1\n")

        sel = parse_extended_selector(f"{src}::nonexistent")
        with pytest.raises(ValueError, match="not found"):
            safe_delete(sel)

    def test_delete_multiple_in_same_file(self, tmp_path):
        """Deleting from bottom up preserves correct lines."""
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text(
            "def a():\n    return 1\n\n"
            "def b():\n    return 2\n\n"
            "def c():\n    return 3\n"
        )

        # Delete 'b' first
        sel = parse_extended_selector(f"{src}::b")
        plan = safe_delete(sel, cascade=False, apply=True)

        content = src.read_text()
        assert "def a" in content
        assert "def b" not in content
        assert "def c" in content


class TestSafeDeleteCascade:
    """Tests for cascading safe delete."""

    def _setup_project(self, tmp_path):
        """Create a project directory with a .git marker for root detection."""
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".git" / "HEAD").touch()  # marker for _find_project_root
        return project

    def _build_index(self, project_path: str):
        """Helper: build the parse.db index for a project."""
        from emend.transform import warm_caches
        warm_caches(project_path, type_engine="none")

    @pytest.mark.parametrize("owner", ["class C:", "def C():"])
    def test_cascade_uses_full_symbol_identity(self, tmp_path, owner):
        from emend.transform import safe_delete
        project = self._setup_project(tmp_path)
        src = project / "main.py"
        src.write_text("def helper():\n    return 1\ndef remove():\n    return helper()\n"
                       "def own_helper():\n    return 2\n"
                       f"{owner}\n    def remove():\n        return own_helper()\n    keep = 3\n"
                       "print(remove())\n")
        plan = safe_delete(parse_extended_selector(f"{src}::C.remove"),
                           cascade=True, project_path=str(project))
        assert {d["selector"] for d in plan.deletions} == {f"{src}::C.remove", f"{src}::own_helper"}

    @pytest.mark.parametrize("protection", [None, "script", "noqa", "test_file", "string"])
    def test_cascade_respects_retained_helpers(self, tmp_path, protection):
        from emend.transform import safe_delete
        project = self._setup_project(tmp_path)
        (project / "pyproject.toml").write_text(
            '[project]\nname="demo"\nversion="0.0.0"\n' +
            ('[project.scripts]\nmycli="main:helper"\n' if protection == "script" else ''))
        src = project / ("test_main.py" if protection == "test_file" else "main.py")
        comment = "  # noqa: emend:deadcode" if protection == "noqa" else ""
        src.write_text(f"def helper():{comment}\n    return 1\ndef remove():\n    return helper()\n")
        if protection == "string":
            (project / "consumer.py").write_text('import main\ngetattr(main, "helper")()\n')
        plan = safe_delete(parse_extended_selector(f"{src}::remove"),
                           cascade=True, project_path=str(project))
        assert {d["name"] for d in plan.deletions} == ({"remove"} if protection else {"remove", "helper"})

    @pytest.mark.parametrize("cascade", [False, True])
    def test_delete_rejects_empty_suite_before_writing(self, tmp_path, cascade):
        from emend.transform import safe_delete
        project = self._setup_project(tmp_path)
        src = project / "main.py"
        source = "class C:\n    def remove(self):\n        return 1\n"
        src.write_text(source)
        with pytest.raises(ValueError, match="invalid syntax"):
            safe_delete(parse_extended_selector(f"{src}::C.remove"),
                        cascade=cascade, project_path=str(project), apply=True)
        assert src.read_text() == source

    def test_cascade_validates_all_files_before_writing(self, tmp_path):
        from emend.transform import safe_delete
        project = self._setup_project(tmp_path)
        files = {"main.py": "from helpers import helper\ndef remove():\n    return helper()\n",
                 "helpers.py": "def helper():\n    return 1\nclass Unfinished:\n"}
        for name, source in files.items():
            (project / name).write_text(source)
        with pytest.raises(ValueError, match="invalid syntax"):
            safe_delete(parse_extended_selector(f"{project / 'main.py'}::remove"),
                        cascade=True, project_path=str(project), apply=True)
        assert {name: (project / name).read_text() for name in files} == files

    def test_cascade_removes_only_caller(self, tmp_path):
        """When a helper is only called by the deleted function, cascade removes it too."""
        from emend.transform import safe_delete

        project = self._setup_project(tmp_path)

        (project / "main.py").write_text(
            "from helpers import helper\n\n"
            "def target():\n    return helper()\n\n"
            "def other():\n    return 42\n\n"
            "x = other()\n"
        )
        (project / "helpers.py").write_text(
            "def helper():\n    return 'help'\n\n"
            "def shared():\n    return 'shared'\n"
        )

        self._build_index(str(project))

        sel = parse_extended_selector(f"{project / 'main.py'}::target")
        plan = safe_delete(sel, cascade=True, project_path=str(project), apply=False)

        deleted_names = {d["name"] for d in plan.deletions}
        assert "target" in deleted_names
        # helper should be cascade-deleted since only target calls it
        assert "helper" in deleted_names
        # shared should NOT be deleted (not called by target)
        assert "shared" not in deleted_names

    def test_cascade_preserves_shared_dependency(self, tmp_path):
        """A function called by both the target and another function survives cascade."""
        from emend.transform import safe_delete

        project = self._setup_project(tmp_path)

        (project / "main.py").write_text(
            "from helpers import shared_helper\n\n"
            "def target():\n    return shared_helper()\n\n"
            "def other():\n    return shared_helper()\n\n"
            "x = other()\n"
        )
        (project / "helpers.py").write_text(
            "def shared_helper():\n    return 'help'\n"
        )

        self._build_index(str(project))

        sel = parse_extended_selector(f"{project / 'main.py'}::target")
        plan = safe_delete(sel, cascade=True, project_path=str(project), apply=False)

        deleted_names = {d["name"] for d in plan.deletions}
        assert "target" in deleted_names
        # shared_helper should NOT be deleted (also called by other)
        assert "shared_helper" not in deleted_names

    def test_cascade_preserves_noncall_reference_and_public_export(self, tmp_path):
        """Cascade respects value references and the module's public API."""
        from emend.transform import safe_delete

        project = self._setup_project(tmp_path)
        (project / "main.py").write_text(
            "from helpers import fixture_helper, main, public_helper, referenced_helper\n\n"
            "def target():\n"
            "    return fixture_helper() + main() + public_helper() + referenced_helper()\n\n"
            "def expose():\n    return referenced_helper\n"
        )
        (project / "helpers.py").write_text(
            "import pytest\n\n"
            "__all__ = ['public_helper']\n\n"
            "def main():\n    return 0\n\n"
            "@pytest.fixture\n"
            "def fixture_helper():\n    return 0\n\n"
            "def public_helper():\n    return 1\n\n"
            "def referenced_helper():\n    return 2\n"
        )
        self._build_index(str(project))

        sel = parse_extended_selector(f"{project / 'main.py'}::target")
        plan = safe_delete(sel, cascade=True, project_path=str(project), apply=False)

        assert {"fixture_helper", "main", "public_helper", "referenced_helper"}.isdisjoint(
            deletion["name"] for deletion in plan.deletions
        )

    def test_cascade_no_effect_without_flag(self, tmp_path):
        """Without --cascade, only the target is deleted even if callees would be dead."""
        from emend.transform import safe_delete

        project = self._setup_project(tmp_path)

        (project / "main.py").write_text(
            "from helpers import helper\n\n"
            "def target():\n    return helper()\n\n"
            "x = 1\n"
        )
        (project / "helpers.py").write_text(
            "def helper():\n    return 'help'\n"
        )

        self._build_index(str(project))

        sel = parse_extended_selector(f"{project / 'main.py'}::target")
        plan = safe_delete(sel, cascade=False, project_path=str(project), apply=False)

        assert len(plan.deletions) == 1
        assert plan.deletions[0]["name"] == "target"

    def test_cascade_apply_modifies_files(self, tmp_path):
        """Cascade with apply=True actually modifies both files."""
        from emend.transform import safe_delete

        project = self._setup_project(tmp_path)

        (project / "main.py").write_text(
            "from helpers import helper\n\n"
            "def target():\n    return helper()\n\n"
            "def other():\n    return 42\n\n"
            "x = other()\n"
        )
        (project / "helpers.py").write_text(
            "def helper():\n    return 'help'\n\n"
            "def shared():\n    return 'shared'\n"
        )

        self._build_index(str(project))

        sel = parse_extended_selector(f"{project / 'main.py'}::target")
        plan = safe_delete(sel, cascade=True, project_path=str(project), apply=True)

        main_content = (project / "main.py").read_text()
        assert "target" not in main_content
        assert "other" in main_content

        helpers_content = (project / "helpers.py").read_text()
        assert "shared" in helpers_content

    def test_delete_plan_json_structure(self, tmp_path):
        """DeletePlan has the expected structure."""
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text("def remove_me():\n    return 1\n")

        sel = parse_extended_selector(f"{src}::remove_me")
        plan = safe_delete(sel, cascade=False, apply=False)

        assert plan.target is not None
        assert isinstance(plan.deletions, list)
        assert isinstance(plan.diffs, dict)
        assert plan.deletions[0]["name"] == "remove_me"
        assert plan.deletions[0]["kind"] == "function"


class TestSafeDeleteNestedSymbol:
    """Deleting a nested symbol (e.g. a method) must not crash while
    re-parsing the internally-built selector.  The selector was assembled by
    joining the symbol path with ``::`` instead of ``.``, producing an
    unparseable string like ``mod.py::Foo::method_a`` that raised
    ``UnexpectedToken`` in phase 2.
    """

    def test_delete_method(self, tmp_path):
        from emend.transform import safe_delete

        src = tmp_path / "mod.py"
        src.write_text(
            "class Foo:\n"
            "    def keep(self):\n"
            "        return 1\n"
            "    def method_a(self):\n"
            "        return 2\n"
        )

        sel = parse_extended_selector(f"{src}::Foo.method_a")
        plan = safe_delete(sel, cascade=False, apply=True)

        assert plan.deletions[0]["name"] == "method_a"
        content = src.read_text()
        assert "method_a" not in content
        assert "def keep" in content
