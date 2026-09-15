"""Shared Git change selection for analysis reports, not analysis inputs."""

from dataclasses import dataclass, field
from bisect import bisect_left
from pathlib import Path
import shutil
import subprocess
import json
import re


def _run(root, *args, required=True):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                            errors="surrogateescape", timeout=30)
    if required and result.returncode:
        raise ValueError(result.stderr.strip() or "Git command failed")
    return result.stdout.removesuffix("\n") if result.returncode == 0 else None


@dataclass
class LineIntervals:
    """Sorted, disjoint half-open intervals of 1-based lines."""

    intervals: list[tuple[int, int]] = field(default_factory=list)

    def add(self, start, stop):
        """Append in source order, coalescing adjacent or overlapping spans."""
        if start >= stop:
            return
        if self.intervals and start <= self.intervals[-1][1]:
            previous, end = self.intervals[-1]
            self.intervals[-1] = (previous, max(end, stop))
        else:
            self.intervals.append((start, stop))

    def intersects(self, start, end):
        """Whether an inclusive finding span touches selected lines."""
        if not self.intervals or end < self.intervals[0][0] or start >= self.intervals[-1][1]:
            return False
        index = bisect_left(self.intervals, (end + 1,)) - 1
        return index >= 0 and self.intervals[index][1] > start

    def __contains__(self, line):
        return self.intersects(line, line)

    def __bool__(self):
        return bool(self.intervals)


@dataclass
class _DiffFile:
    paths: list[str | None] = field(default_factory=lambda: [None, None])
    blobs: list[str] = field(default_factory=lambda: ["", ""])
    lines: list[LineIntervals] = field(default_factory=lambda: [LineIntervals(), LineIntervals()])
    hunks: list[tuple[int, int, int, int]] = field(default_factory=list)


def _parse_diff(diff_text: str) -> list[_DiffFile]:
    """Keep both coordinate spaces and blob identities of a Git patch."""
    files: list[_DiffFile] = []
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            files.append(_DiffFile())
            in_hunk = False
        elif files:
            current = files[-1]
            if match := re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line):
                in_hunk = True
                hunk = tuple(int(value) if value is not None else 1 for value in match.groups())
                current.hunks.append(hunk)
                for side in (0, 1):
                    start, count = hunk[side * 2:side * 2 + 2]
                    current.lines[side].add(start, start + count)
            elif in_hunk:
                continue
            elif line.startswith("index "):
                current.blobs = line.split()[1].split("..")
            elif line.startswith(("--- ", "+++ ")):
                path = line[4:].removesuffix("\t")
                if path.startswith('"'):
                    path = json.loads(path)
                current.paths[line.startswith("+++")] = None if path == "/dev/null" else path[2:]
    return files


def read_diff(root, *revisions):
    """Read machine-format hunks independently of Git presentation settings."""
    return _parse_diff(_run(root, "-c", "core.quotepath=false", "diff", "--no-ext-diff",
                           "--no-textconv", "--no-renames", "--full-index", "--no-color", "-U0",
                           "--src-prefix=a/", "--dst-prefix=b/", "--inter-hunk-context=0", *revisions, "--"))


def _gh(root, *args):
    if shutil.which("gh"):
        try:
            result = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass


def repository_root(path="."):
    """Resolve the coordinate origin of Git paths, independently of analysis scope."""
    candidate = Path(str(path).split("::", 1)[0]).resolve()
    while not candidate.is_dir():
        candidate = candidate.parent
    return Path(_run(candidate, "rev-parse", "--show-toplevel"))


def resolve_diff(spec, path="."):
    """Return repository root and an explicit Git diff spec, without fetching."""
    root = repository_root(path)
    if spec != "auto":
        if spec.startswith("-"):
            raise ValueError("Expected a Git revision or range, not an option")
        if ".." not in spec:
            revisions = _run(root, "rev-parse", "--revs-only", "--no-flags", spec).splitlines()
            if len(revisions) > 2:
                raise ValueError("Combined merge diffs are unsupported; provide a two-commit range")
            if len(revisions) == 2:
                left, right = revisions
                spec = f"{right[1:]}..{left}" if right.startswith("^") else f"{left}..{right}"
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


def _map_lines(lines: LineIntervals, hunks) -> LineIntervals:
    """Translate intervals through zero-context Git hunks in one sweep.

    Insertions shift surviving lines but are not selected; a replacement is
    selected in full if any of its old lines were selected.
    """
    mapped = LineIntervals()
    spans = iter(lines.intervals)
    start, stop = next(spans, (None, None))
    offset = 0
    for old, old_count, new, new_count in hunks:
        # Empty hunks name the preceding line, unlike nonempty hunks.
        old += not old_count
        new += not new_count
        old_stop = old + old_count
        while start is not None and start < old:
            end = min(stop, old)
            mapped.add(start + offset, end + offset)
            start = end
            if start == stop:
                start, stop = next(spans, (None, None))
        if start is not None and start < old_stop:
            mapped.add(new, new + new_count)
            while start is not None and start < old_stop:
                start = min(stop, old_stop)
                if start == stop:
                    start, stop = next(spans, (None, None))
        offset = new + new_count - old_stop
    if start is not None:
        mapped.add(start + offset, stop + offset)
        for start, stop in spans:
            mapped.add(start + offset, stop + offset)
    return mapped


@dataclass
class DiffSelection:
    root: Path
    lines: dict[str, LineIntervals]
    _paths: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    @classmethod
    def load(cls, spec, path="."):
        if spec is None:
            return None
        root, revision = resolve_diff(spec, path)
        selected = read_diff(root, revision)
        # A single revision already compares against the working tree. Staged
        # diffs and ranges need one batched translation from their right side.
        edits = {}
        if selected and (revision == "--cached" or ".." in revision):
            target = [] if revision == "--cached" else [revision.rsplit("..", 1)[1] or "HEAD"]
            edits = {item.paths[0]: item.hunks for item in read_diff(root, *target)}
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
        path = str(path)
        if path not in self._paths:
            self._paths[path] = str((self.root / path).resolve())
        changed = self.lines.get(self._paths[path])
        if changed is None:
            return False
        if line is None or line <= 0:
            return True
        return changed.intersects(line, end_line or line)

    def filter(self, values, *, relative_to=None):
        if relative_to is not None:
            relative_to = Path(relative_to).resolve()
        selected = []
        for value in values:
            path = getattr(value, "file_path", getattr(value, "importing_file", ""))
            if relative_to is not None:
                path = Path(relative_to) / path
            if self.matches(path, getattr(value, "line", getattr(value, "start_line", None)),
                            getattr(value, "end_line", None)):
                selected.append(value)
        return selected
