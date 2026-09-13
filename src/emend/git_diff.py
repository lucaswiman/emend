"""Shared Git change selection for analysis reports, not analysis inputs."""

from dataclasses import dataclass
from bisect import bisect_left
from pathlib import Path
import shutil
import subprocess
from typing import Annotated, Optional

import typer
from typer.core import TyperCommand


DiffOption = Annotated[Optional[str], typer.Option(
    "--diff", metavar="[RANGE]",
    help="Report changes: staged if present, otherwise PR/default-base...HEAD; optionally supply a Git range.",
)]


class DiffCommand(TyperCommand):
    """Allow a bare --diff while retaining Typer's ordinary string option."""

    def parse_args(self, ctx, args):
        normalized = []
        for index, arg in enumerate(args):
            if arg == "--":
                normalized.extend(args[index:])
                break
            if arg == "--diff" and (index + 1 == len(args) or args[index + 1].startswith("-")):
                arg = "--diff=auto"
            normalized.append(arg)
        return super().parse_args(ctx, normalized)


def _run(root, *args, required=True):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)
    if required and result.returncode:
        raise ValueError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip() if result.returncode == 0 else None


def _gh(root, *args):
    if shutil.which("gh"):
        try:
            result = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass


def resolve_diff(spec, path="."):
    """Return repository root and an explicit Git diff spec, without fetching."""
    candidate = Path(str(path).split("::", 1)[0]).resolve()
    while not candidate.is_dir():
        candidate = candidate.parent
    root = Path(_run(candidate, "rev-parse", "--show-toplevel"))
    if spec != "auto":
        if spec.startswith("-"):
            raise ValueError("Expected a Git revision or range, not an option")
        return root, spec
    if _run(root, "diff", "--cached", "--name-only", "--"):
        return root, "--cached"
    base = (_gh(root, "pr", "view", "--json", "baseRefOid,state", "--jq", 'select(.state == "OPEN") | .baseRefOid')
            or _run(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", required=False)
            or _gh(root, "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"))
    candidates = [f"origin/{base}", base] if base else ["origin/main", "origin/master", "main", "master"]
    for candidate in candidates:
        if candidate and _run(root, "rev-parse", "--verify", f"{candidate}^{{commit}}", required=False):
            ancestor = _run(root, "merge-base", "HEAD", candidate)
            return root, f"{ancestor}..HEAD"
    raise ValueError("Cannot resolve the PR/default base locally; provide --diff RANGE")


def _map_lines(lines, hunks):
    """Translate selected 1-based lines through zero-context Git hunks."""
    lines = sorted(lines)
    mapped, cursor, offset = [], 0, 0
    for old, old_count, new, new_count in hunks:
        # Empty hunks name the preceding line, unlike nonempty hunks.
        old -= bool(old_count)
        new -= bool(new_count)
        start, stop = bisect_left(lines, old + 1), bisect_left(lines, old + old_count + 1)
        mapped.extend(line + offset for line in lines[cursor:start])
        if start < stop:
            mapped.extend(range(new + 1, new + new_count + 1))
        cursor, offset = stop, new + new_count - old - old_count
    mapped.extend(line + offset for line in lines[cursor:])
    return mapped


@dataclass
class DiffSelection:
    root: Path
    lines: dict[str, list[int]]

    @classmethod
    def load(cls, spec, path="."):
        if spec is None:
            return None
        from emend.transform.impact import _parse_diff

        root, revision = resolve_diff(spec, path)
        def patch(*revisions):
            return _parse_diff(_run(root, "-c", "core.quotepath=false", "diff", "--no-ext-diff",
                                   "--no-textconv", "--no-renames", "--full-index", "-U0", *revisions, "--"))

        selected = patch(revision)
        # A single revision already compares against the working tree. Staged
        # diffs and ranges need one batched translation from their right side.
        edits = {}
        if selected and (revision == "--cached" or ".." in revision):
            target = [] if revision == "--cached" else [revision.rsplit("..", 1)[1] or "HEAD"]
            edits = {item.paths[0]: item.hunks for item in patch(*target)}
        lines = {}
        for item in selected:
            if item.paths[1] is None:
                continue
            source = root / item.paths[1]
            if not source.is_file():
                continue
            lines[str(source.resolve())] = _map_lines(item.lines[1], edits.get(item.paths[1], ()))
        return cls(root, lines)

    def matches(self, path, line=None, end_line=None):
        path = Path(path)
        absolute = str((path if path.is_absolute() else self.root / path).resolve())
        changed = self.lines.get(absolute)
        if changed is None:
            return False
        if line is None or line <= 0:
            return True
        index = bisect_left(changed, line)
        return index < len(changed) and changed[index] <= (end_line or line)

    def filter(self, values, *, relative_to=None):
        selected = []
        for value in values:
            path = getattr(value, "file_path", getattr(value, "importing_file", ""))
            if relative_to is not None:
                path = Path(relative_to) / path
            if self.matches(path, getattr(value, "line", getattr(value, "start_line", None)),
                            getattr(value, "end_line", None)):
                selected.append(value)
        return selected
