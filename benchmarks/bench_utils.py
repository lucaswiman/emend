"""Shared utilities for emend benchmark scripts."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TIMEOUT = 600  # seconds -- generous limit for slow operations on large codebases

# Whether to suppress progress output (for JSON mode).
_quiet = False


def _log(msg: str) -> None:
    """Print a progress message to stderr (so JSON output stays clean)."""
    if not _quiet:
        print(msg, file=sys.stderr)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, raising on failure with combined output."""
    kwargs.setdefault("timeout", TIMEOUT)
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def check_emend_available() -> list[str]:
    """Check that emend CLI is available. Returns the command to use."""
    venv_emend = str(Path(sys.executable).parent / "emend")
    candidates = [
        ["emend", "--help"],
        [venv_emend, "--help"],
        [sys.executable, "-m", "emend", "--help"],
    ]
    for cmd in candidates:
        try:
            result = _run(cmd)
        except (FileNotFoundError, OSError):
            continue
        if result.returncode == 0:
            return cmd[:-1]

    print(
        "ERROR: emend is not installed or not on PATH.\n"
        "Install it with: pip install -e . (from the emend repo root)",
        file=sys.stderr,
    )
    sys.exit(1)
