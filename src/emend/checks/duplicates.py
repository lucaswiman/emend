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
        for member in cluster.members:
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
                end_line=member.end_line,
            ))
    return violations
