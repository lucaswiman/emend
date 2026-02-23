"""Tests for --imported-from flag on the find command."""
from pathlib import Path

import pytest

from emend.transform import find_pattern


class TestImportedFromFilter:
    """find_pattern with imported_from should filter matches by module origin."""

    def test_imported_from_matches_actual_import(self, tmp_path):
        """json.loads should match when json is actually imported from json."""
        file = tmp_path / "example.py"
        file.write_text(
            "import json\n"
            "\n"
            "data = json.loads('{}')\n"
        )

        matches = find_pattern(
            "json.loads($X)", str(file), imported_from="json"
        )
        assert len(matches) == 1
        assert matches[0].line == 3

    def test_imported_from_excludes_local_variable(self, tmp_path):
        """json.loads should NOT match when json is a local variable."""
        file = tmp_path / "example.py"
        file.write_text(
            "class json:\n"
            "    @staticmethod\n"
            "    def loads(s):\n"
            "        return s\n"
            "\n"
            "data = json.loads('{}')\n"
        )

        matches = find_pattern(
            "json.loads($X)", str(file), imported_from="json"
        )
        assert len(matches) == 0

    def test_imported_from_with_from_import(self, tmp_path):
        """loads($X) should match when loads is imported from json."""
        file = tmp_path / "example.py"
        file.write_text(
            "from json import loads\n"
            "\n"
            "data = loads('{}')\n"
        )

        matches = find_pattern(
            "loads($X)", str(file), imported_from="json"
        )
        assert len(matches) == 1
        assert matches[0].line == 3

    def test_imported_from_excludes_wrong_module(self, tmp_path):
        """loads($X) should NOT match when loads is from a different module."""
        file = tmp_path / "example.py"
        file.write_text(
            "from yaml import loads\n"
            "\n"
            "data = loads('{}')\n"
        )

        matches = find_pattern(
            "loads($X)", str(file), imported_from="json"
        )
        assert len(matches) == 0

    def test_imported_from_without_flag_matches_all(self, tmp_path):
        """Without --imported-from, all matches should be returned."""
        file = tmp_path / "example.py"
        file.write_text(
            "import json\n"
            "\n"
            "data = json.loads('{}')\n"
        )

        matches = find_pattern("json.loads($X)", str(file))
        assert len(matches) == 1

    def test_imported_from_aliased_import(self, tmp_path):
        """Should match when module is imported with alias, using original module name."""
        file = tmp_path / "example.py"
        file.write_text(
            "import json as j\n"
            "\n"
            "data = j.loads('{}')\n"
        )

        matches = find_pattern(
            "j.loads($X)", str(file), imported_from="json"
        )
        assert len(matches) == 1
        assert matches[0].line == 3


class TestImportedFromCLI:
    """Test --imported-from via CLI."""

    def test_imported_from_cli_flag(self, tmp_path, run_emend_cmd):
        """CLI --imported-from flag should filter matches."""
        file = tmp_path / "example.py"
        file.write_text(
            "import json\n"
            "\n"
            "data = json.loads('{}')\n"
        )

        result = run_emend_cmd([
            "search", "json.loads($X)", str(file),
            "--imported-from", "json"
        ])
        assert str(file) in result.stdout
        assert ":3" in result.stdout

    def test_imported_from_cli_excludes_non_matching(self, tmp_path, run_emend_cmd):
        """CLI --imported-from should exclude non-matching imports."""
        file = tmp_path / "example.py"
        file.write_text(
            "class json:\n"
            "    @staticmethod\n"
            "    def loads(s):\n"
            "        return s\n"
            "\n"
            "data = json.loads('{}')\n"
        )

        result = run_emend_cmd([
            "search", "json.loads($X)", str(file),
            "--imported-from", "json", "--output", "count"
        ], check=False)
        # Should find 0 matches
        assert result.stdout.strip() == "0"
