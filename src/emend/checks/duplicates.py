"""Duplicate code detection as a checks module."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emend.lint import LintViolation

logger = logging.getLogger(__name__)


@dataclass
class DuplicateCodeConfig:
    """Configuration for the duplicate-code rule."""
    enabled: bool = False
    rule_name: str = "duplicate-code"
    mode: str = "all"
    min_lines: int = 5
    min_score: float = 50.0
    cross_file_only: bool = True
    exclude_tests: bool = True
    exclude_generated: bool = True
    message: str = "Duplicate code detected"


def parse_duplicate_code_config(raw: object, *, rule_name: str = "duplicate-code") -> DuplicateCodeConfig | None:
    """Parse the ``duplicate`` section from a YAML rules document."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return DuplicateCodeConfig(enabled=raw, rule_name=rule_name)
    if not isinstance(raw, dict):
        return None
    return DuplicateCodeConfig(
        enabled=raw.get("enabled", True),
        rule_name=rule_name,
        mode=raw.get("mode", "all"),
        min_lines=int(raw.get("min-lines", raw.get("min_lines", 5))),
        min_score=float(raw.get("min-score", raw.get("min_score", 50.0))),
        cross_file_only=raw.get("cross-file-only", raw.get("cross_file_only", True)),
        exclude_tests=raw.get("exclude-tests", raw.get("exclude_tests", True)),
        exclude_generated=raw.get("exclude-generated", raw.get("exclude_generated", True)),
        message=raw.get("message", "Duplicate code detected"),
    )


def run_duplicate_code_check(
    file_paths: list[str],
    config: DuplicateCodeConfig,
    project_path: str,
) -> "list[LintViolation]":
    """Run duplicate code detection as part of lint."""
    if not config.enabled:
        return []

    from emend.duplicate import query_duplicates
    from emend.lint import LintViolation

    violations = []
    clusters = query_duplicates(
        project_path=project_path,
        mode=config.mode,
        min_lines=config.min_lines,
        min_score=config.min_score,
        cross_file=True if config.cross_file_only else None,
    )

    file_path_set = set(file_paths)
    for cluster in clusters:
        for member in cluster.members[:1]:
            if member.file not in file_path_set:
                continue
            others = [m for m in cluster.members if m != member]
            if not others:
                continue
            other = others[0]
            msg = (
                f"{config.message}: {cluster.explanation} "
                f"(also at {other.file}:{other.start_line})"
            )
            violations.append(LintViolation(
                rule_name=config.rule_name,
                message=msg,
                file_path=member.file,
                line=member.start_line,
            ))
    return violations
