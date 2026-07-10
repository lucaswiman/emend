"""Tests for the batch refactoring command."""

import json
import pytest
from pathlib import Path
from typer.testing import CliRunner

from emend.cli import app

runner = CliRunner()


class TestBatchJSON:
    """Tests for batch command with JSON input."""

    def test_batch_rename_dry_run(self, tmp_path):
        """Batch rename in dry-run mode shows diff without modifying."""
        test_file = tmp_path / "api.py"
        original = "def get_user():\n    return 'alice'\n"
        test_file.write_text(original)

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "edit": {
                        "selector": f"{test_file}::get_user[returns]",
                        "value": "str"
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file)])

        assert result.exit_code == 0
        # Dry-run: file not modified
        assert test_file.read_text() == original
        # Should show diff
        assert "-> str" in result.stdout or "str" in result.stdout

    def test_batch_edit_apply(self, tmp_path):
        """Batch edit with --apply modifies file."""
        test_file = tmp_path / "api.py"
        test_file.write_text("def get_user() -> int:\n    return 1\n")

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "edit": {
                        "selector": f"{test_file}::get_user[returns]",
                        "value": "str"
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "-> str:" in content

    def test_batch_add_operation(self, tmp_path):
        """Batch add a parameter."""
        test_file = tmp_path / "api.py"
        test_file.write_text("def get_user(user_id: int):\n    pass\n")

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "add": {
                        "selector": f"{test_file}::get_user[params]",
                        "value": "timeout: float = 30.0"
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "timeout: float = 30.0" in content
        assert "user_id: int" in content

    def test_batch_remove_operation(self, tmp_path):
        """Batch remove a parameter."""
        test_file = tmp_path / "api.py"
        test_file.write_text("def get_user(user_id: int, debug: bool = False):\n    pass\n")

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "remove": {
                        "selector": f"{test_file}::get_user[params][debug]"
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "debug" not in content
        assert "user_id: int" in content

    def test_batch_replace_operation(self, tmp_path):
        """Batch replace a pattern."""
        test_file = tmp_path / "api.py"
        test_file.write_text("print('hello')\nprint('world')\n")

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "replace": {
                        "pattern": "print($X)",
                        "replacement": "logger.info($X)",
                        "path": str(test_file)
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "logger.info('hello')" in content
        assert "logger.info('world')" in content

    def test_batch_multiple_operations(self, tmp_path):
        """Batch applies multiple operations in order."""
        test_file = tmp_path / "api.py"
        test_file.write_text(
            "def get_user() -> int:\n"
            "    return 1\n"
        )

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "edit": {
                        "selector": f"{test_file}::get_user[returns]",
                        "value": "str"
                    }
                },
                {
                    "add": {
                        "selector": f"{test_file}::get_user[params]",
                        "value": "user_id: int"
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "-> str:" in content
        assert "user_id: int" in content

    def test_batch_nonexistent_ops_file(self, tmp_path):
        """Error when operations file doesn't exist."""
        result = runner.invoke(app, ["batch", str(tmp_path / "nonexistent.json")])

        assert result.exit_code != 0

    def test_batch_invalid_json(self, tmp_path):
        """Error for invalid JSON."""
        ops_file = tmp_path / "ops.json"
        ops_file.write_text("not valid json{}")

        result = runner.invoke(app, ["batch", str(ops_file)])

        assert result.exit_code != 0

    def test_batch_missing_operations_key(self, tmp_path):
        """Error when 'operations' key is missing."""
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({"wrong_key": []}))

        result = runner.invoke(app, ["batch", str(ops_file)])

        assert result.exit_code != 0

    def test_batch_unknown_operation_type(self, tmp_path):
        """Error for unknown operation type."""
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {"unknown_op": {"selector": "foo.py::bar"}}
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file)])

        assert result.exit_code != 0

    def test_batch_null_operation_value(self, tmp_path):
        """A null operation value produces a clear error, not a raw AttributeError.

        Regression: ``{"edit": None}`` used to crash with
        ``AttributeError: 'NoneType' object has no attribute 'get'`` surfaced
        as an ugly generic ``Error: AttributeError(...)`` (exit 1).
        """
        ops_file = tmp_path / "ops.yaml"
        ops_file.write_text(
            "operations:\n"
            "  - edit:\n"
        )

        result = runner.invoke(app, ["batch", str(ops_file)])

        # ValueError -> exit 2 (clean validation error), not exit 1 (AttributeError).
        assert result.exit_code == 2
        assert "AttributeError" not in result.output
        assert "Operation #1" in result.output
        assert "edit" in result.output


class TestBatchYAML:
    """Tests for batch command with YAML input."""

    def test_batch_yaml_edit(self, tmp_path):
        """Batch with YAML file."""
        test_file = tmp_path / "api.py"
        test_file.write_text("def get_user() -> int:\n    return 1\n")

        ops_file = tmp_path / "ops.yaml"
        ops_file.write_text(
            "operations:\n"
            "  - edit:\n"
            f"      selector: \"{test_file}::get_user[returns]\"\n"
            "      value: \"str\"\n"
        )

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "-> str:" in content

    def test_batch_yaml_replace(self, tmp_path):
        """Batch YAML with replace operation."""
        test_file = tmp_path / "api.py"
        test_file.write_text("print('hello')\n")

        ops_file = tmp_path / "ops.yml"
        ops_file.write_text(
            "operations:\n"
            "  - replace:\n"
            "      pattern: \"print($X)\"\n"
            "      replacement: \"logger.info($X)\"\n"
            f"      path: \"{test_file}\"\n"
        )

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "logger.info('hello')" in content

    def test_batch_yaml_multiple_ops(self, tmp_path):
        """Batch YAML with multiple operations in sequence."""
        test_file = tmp_path / "api.py"
        test_file.write_text("def func(x: int):\n    pass\n")

        ops_file = tmp_path / "ops.yaml"
        ops_file.write_text(
            "operations:\n"
            "  - add:\n"
            f"      selector: \"{test_file}::func[params]\"\n"
            "      value: \"y: str\"\n"
            "  - edit:\n"
            f"      selector: \"{test_file}::func[params][x]\"\n"
            "      value: \"x: float\"\n"
        )

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "x: float" in content
        assert "y: str" in content


class TestBatchDryRun:
    """Tests for batch dry-run behavior."""

    def test_batch_dry_run_shows_diffs(self, tmp_path):
        """Dry-run shows diffs for each operation."""
        test_file = tmp_path / "api.py"
        original = "def func() -> int:\n    pass\n"
        test_file.write_text(original)

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "edit": {
                        "selector": f"{test_file}::func[returns]",
                        "value": "str"
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file)])

        assert result.exit_code == 0
        # File should not be modified
        assert test_file.read_text() == original
        # Should show some diff output
        assert result.stdout.strip() != ""

    def test_batch_dry_run_no_changes(self, tmp_path):
        """Dry-run with empty operations list."""
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({"operations": []}))

        result = runner.invoke(app, ["batch", str(ops_file)])

        assert result.exit_code == 0


class TestBatchAddWithOptions:
    """Tests for batch add with positioning options."""

    def test_batch_add_with_pseudo_class(self, tmp_path):
        """Batch add with pseudo-class selector for keyword-only param."""
        test_file = tmp_path / "api.py"
        test_file.write_text("def func(x: int, *, y: str):\n    pass\n")

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "add": {
                        "selector": f"{test_file}::func[params]:KEYWORD_ONLY",
                        "value": "force: bool = False"
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "force: bool = False" in content

    def test_batch_add_at_position(self, tmp_path):
        """Batch add at specific position."""
        test_file = tmp_path / "api.py"
        test_file.write_text("def func(x: int, z: str):\n    pass\n")

        ops_file = tmp_path / "ops.json"
        ops_file.write_text(json.dumps({
            "operations": [
                {
                    "add": {
                        "selector": f"{test_file}::func[params]",
                        "value": "y: float",
                        "at": 1
                    }
                }
            ]
        }))

        result = runner.invoke(app, ["batch", str(ops_file), "--apply"])

        assert result.exit_code == 0
        content = test_file.read_text()
        assert "y: float" in content
