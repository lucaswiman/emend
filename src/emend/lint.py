"""Lint command with pattern macros for emend."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
import libcst as cst

from emend.transform import find_pattern, replace_pattern


@dataclass
class LintRule:
    """A lint rule definition."""
    name: str
    find: str
    message: str
    not_inside: str | None = None
    replace: str | None = None


@dataclass
class LintViolation:
    """A lint violation found by a rule."""
    rule_name: str
    message: str
    file_path: str
    line: int
    col: int = 0
    match_text: str = ""


def expand_macros(pattern: str, macros: dict[str, str]) -> str:
    """Substitute {macro_name} references in a pattern string.

    Args:
        pattern: Pattern string possibly containing {macro_name} references
        macros: Mapping of macro names to their pattern expansions

    Returns:
        Pattern with all macro references expanded
    """
    for name, expansion in macros.items():
        pattern = pattern.replace(f"{{{name}}}", expansion)
    return pattern


def load_rules(config_path: str) -> tuple[list[LintRule], dict[str, str]]:
    """Parse a YAML rules file into LintRule objects.

    Args:
        config_path: Path to the YAML config file

    Returns:
        Tuple of (rules, macros) where rules is a list of LintRule and
        macros is a dict of macro name to pattern string

    Raises:
        FileNotFoundError: If config file does not exist
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        config = yaml.safe_load(f)

    macros = config.get("macros", {}) or {}
    raw_rules = config.get("rules", {}) or {}

    rules = []
    for name, rule_def in raw_rules.items():
        find_pattern_str = expand_macros(rule_def["find"], macros)
        rules.append(LintRule(
            name=name,
            find=find_pattern_str,
            message=rule_def.get("message", ""),
            not_inside=rule_def.get("not-inside"),
            replace=rule_def.get("replace"),
        ))

    return rules, macros


def run_lint(
    rules: list[LintRule],
    paths: list[str],
    fix: bool = False,
    rule_filter: str | None = None,
) -> list[LintViolation]:
    """Run lint rules against files and return violations.

    Args:
        rules: List of LintRule to check
        paths: List of file paths to lint
        fix: If True, apply replace rules to fix violations
        rule_filter: If set, only run the rule with this name

    Returns:
        List of LintViolation objects
    """
    if rule_filter:
        rules = [r for r in rules if r.name == rule_filter]

    violations = []

    for file_path in paths:
        for rule in rules:
            if fix and rule.replace:
                # Use replace_pattern to fix and count
                diff, count = replace_pattern(
                    rule.find,
                    rule.replace,
                    file_path,
                    not_inside=rule.not_inside,
                    apply=True,
                )
                if count > 0:
                    # Report violations for each replacement
                    # Re-read original to get line info from matches before fix
                    # Since the fix already happened, report based on count
                    # We need to find matches on the original — but replace already modified.
                    # For fix mode, we report one violation per file with replacements.
                    violations.append(LintViolation(
                        rule_name=rule.name,
                        message=rule.message,
                        file_path=file_path,
                        line=0,
                        match_text=f"{count} replacement(s) applied",
                    ))
            else:
                # Find-only mode
                try:
                    matches = find_pattern(
                        rule.find,
                        file_path,
                        not_inside=rule.not_inside,
                    )
                except Exception:
                    continue

                for match in matches:
                    match_text = cst.Module([]).code_for_node(match.node).strip()
                    violations.append(LintViolation(
                        rule_name=rule.name,
                        message=rule.message,
                        file_path=file_path,
                        line=match.line or 0,
                        match_text=match_text,
                    ))

    return violations
