"""Tests for module-level refactoring commands.

This module tests module refactoring commands:
- move (module mode): Move module to another package
"""

import tempfile
from pathlib import Path


class TestMoveModule:
    """Tests for move command (module mode)."""

    def test_move_module_to_subpackage(self, run_emend_cmd):
        """Move module from root to a subpackage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Setup source module
            utils = tmpdir / "utils.py"
            utils.write_text("def helper():\n    return 42\n")

            # Setup destination package
            pkg = tmpdir / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")

            # Run move command with --apply
            result = run_emend_cmd([
                "move",
                str(utils),
                "pkg",
                "--project", str(tmpdir),
                "--apply",
            ])

            # Verify module moved to package
            assert result.returncode == 0
            new_location = pkg / "utils.py"
            assert new_location.exists()
            assert "def helper():" in new_location.read_text()

            # Verify original file removed
            assert not utils.exists()

    def test_move_module_updates_imports(self, run_emend_cmd):
        """Moving module updates all import statements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Setup source module
            utils = tmpdir / "utils.py"
            utils.write_text("def helper():\n    return 42\n")

            # Setup destination package
            pkg = tmpdir / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")

            # Setup file that imports the module
            main = tmpdir / "main.py"
            main.write_text(
                "from utils import helper\n\n"
                "result = helper()\n"
            )

            # Run move command with --apply
            result = run_emend_cmd([
                "move",
                str(utils),
                "pkg",
                "--project", str(tmpdir),
                "--apply",
            ])

            # Verify import updated in main.py
            assert result.returncode == 0
            main_content = main.read_text()
            assert "from pkg.utils import helper" in main_content
            assert "from utils import helper" not in main_content

    def test_move_module_dotted_destination(self, run_emend_cmd):
        """Destination can be pkg.subpkg format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Setup source module
            utils = tmpdir / "utils.py"
            utils.write_text("def helper():\n    return 42\n")

            # Setup nested package structure
            pkg = tmpdir / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            subpkg = pkg / "subpkg"
            subpkg.mkdir()
            (subpkg / "__init__.py").write_text("")

            # Run move command with dotted destination
            result = run_emend_cmd([
                "move",
                str(utils),
                "pkg.subpkg",
                "--project", str(tmpdir),
                "--apply",
            ])

            # Verify module moved to nested package
            assert result.returncode == 0
            new_location = subpkg / "utils.py"
            assert new_location.exists()
            assert "def helper():" in new_location.read_text()

            # Verify original file removed
            assert not utils.exists()

    def test_move_module_dry_run(self, run_emend_cmd):
        """Dry run shows preview without moving."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Setup source module
            utils = tmpdir / "utils.py"
            utils.write_text("def helper():\n    return 42\n")

            # Setup destination package
            pkg = tmpdir / "pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")

            # Run move command in dry-run mode (default)
            result = run_emend_cmd([
                "move",
                str(utils),
                "pkg",
                "--project", str(tmpdir),
            ])

            # Verify files unchanged
            assert result.returncode == 0
            assert utils.exists()
            assert not (pkg / "utils.py").exists()

            # Verify preview shown in output
            assert "CHANGES PREVIEW" in result.stdout
