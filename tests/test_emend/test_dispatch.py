"""Tests for emend.transform.dispatch module."""

import pytest
from emend.transform import cmd_lookup


class TestCmdLookupLineBasedMetadata:
    """Tests for cmd_lookup with line-based selectors and metadata=True."""

    def test_line_with_no_symbol_raises_value_error(self, tmp_path):
        """When a line-based selector points to an empty line (no symbol),
        cmd_lookup should raise ValueError, not SystemExit."""
        source = tmp_path / "example.py"
        source.write_text(
            "def foo():\n"
            "    pass\n"
            "\n"  # line 3: empty line, no symbol
            "def bar():\n"
            "    pass\n"
        )
        # Line 3 is an empty line with no symbol.
        selector_str = f"{source}:3"
        with pytest.raises(ValueError, match="No symbol found at line 3"):
            cmd_lookup(str(source), selector_str=selector_str, metadata=True)
