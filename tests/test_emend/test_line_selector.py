"""Test line selector functionality for emend.

Line selectors allow targeting code by line numbers:
- file.py:42 - single line
- file.py:42-100 - line range

Use show --metadata to display location metadata for line selectors.
"""

import tempfile
from pathlib import Path


def test_show_location_single_line(run_emend_cmd):
    """Test show --metadata with single line selector (file.py:42)."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
class Builder:
    def __init__(self):
        pass

    def build(self):
        # This is line 7
        result = self.process()
        return result

    def process(self):
        return "done"
""")
        f.flush()
        temp_file = f.name

    try:
        # show --metadata for line 7 should find the build method
        result = run_emend_cmd(["search", "--output", "metadata", f"{temp_file}:7"])

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        output = result.stdout

        # Should show the build method since line 7 is inside it
        assert "build" in output, f"Expected 'build' in output, got: {output}"
        assert "Lines:" in output, f"Expected line info in output, got: {output}"
    finally:
        Path(temp_file).unlink()


def test_show_location_line_range(run_emend_cmd):
    """Test show --metadata with line range selector (file.py:42-100)."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
class Builder:
    def __init__(self):
        pass

    def build(self):
        result = self.process()
        return result

    def process(self):
        # Line 11
        step1 = "start"
        step2 = "middle"
        step3 = "end"
        return step3
""")
        f.flush()
        temp_file = f.name

    try:
        # show --metadata for lines 11-14 should find the process method
        result = run_emend_cmd(["search", "--output", "metadata", f"{temp_file}:11-14"])

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        output = result.stdout

        # Should show the process method since lines 11-14 are inside it
        assert "process" in output, f"Expected 'process' in output, got: {output}"
        assert "Lines:" in output, f"Expected line info in output, got: {output}"
    finally:
        Path(temp_file).unlink()


def test_copy_to_single_line(run_emend_cmd):
    """Test copy-to with single line selector."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
def outer():
    def inner():
        # Line 4
        return "inner"
    return inner()
""")
        f.flush()
        temp_file = f.name

    dest_file = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
    dest_file.close()

    try:
        # copy-to for line 4 should extract the inner function
        result = run_emend_cmd([
            "copy-to",
            f"{temp_file}:4",
            dest_file.name,
            "--dedent"
        ])

        assert result.returncode == 0, f"Command failed: {result.stderr}"

        # Preview should show the symbol name being copied
        output = result.stdout
        assert "inner" in output, \
            f"Expected symbol name 'inner' in output, got: {output}"
    finally:
        Path(temp_file).unlink()
        Path(dest_file.name).unlink()



def test_line_selector_outside_any_function_error(run_emend_cmd):
    """Test line selector on module-level code returns error when no symbol found."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
# Line 2: module-level code
x = 42

def func():
    return x
""")
        f.flush()
        temp_file = f.name

    try:
        # show --metadata for line 3 (module-level assignment) should fail
        result = run_emend_cmd(["search", "--output", "metadata", f"{temp_file}:3"], check=False)

        # Should exit with error code for module-level code
        assert result.returncode == 1, \
            f"Expected exit code 1, got {result.returncode}"
        assert "no symbol found" in result.stdout.lower(), \
            f"Expected 'no symbol found' message in stdout, got: {result.stdout}"
    finally:
        Path(temp_file).unlink()


def test_line_selector_inside_function_succeeds(run_emend_cmd):
    """Test line selector on code inside a function returns symbol info."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("""
def func():
    x = 42
    return x
""")
        f.flush()
        temp_file = f.name

    try:
        # show --metadata for line 3 (inside func) should succeed
        result = run_emend_cmd(["search", "--output", "metadata", f"{temp_file}:3"], check=False)

        # Should succeed and show symbol info
        assert result.returncode == 0, \
            f"Expected exit code 0, got {result.returncode}. stderr: {result.stderr}"
        assert "func" in result.stdout, \
            f"Expected 'func' in output, got: {result.stdout}"
        assert "Lines:" in result.stdout, \
            f"Expected 'Lines:' in output, got: {result.stdout}"
    finally:
        Path(temp_file).unlink()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
