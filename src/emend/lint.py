"""Lint command with pattern macros for emend."""

from __future__ import annotations

import io
import logging
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

import yaml
import libcst as cst

from emend.transform import find_pattern, replace_pattern

_NOQA_RE = re.compile(r"#\s*noqa\b(?:\s*:\s*(.+))?", re.IGNORECASE)


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


def parse_noqa_comments(source: str) -> dict[int, set[str] | None]:
    """Find real ``# noqa`` comments via the tokenizer.

    Returns a mapping of line number to either ``None`` (bare noqa, suppresses
    all emend rules) or a set of emend rule names extracted from
    ``emend:<rule>`` entries.
    """
    result: dict[int, set[str] | None] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok_type, tok_string, (srow, _), _, _ in tokens:
            if tok_type == tokenize.COMMENT:
                m = _NOQA_RE.search(tok_string)
                if m:
                    rules_str = m.group(1)
                    if rules_str:
                        rules = set()
                        for r in rules_str.split(","):
                            r = r.strip()
                            if r.startswith("emend:"):
                                rules.add(r[len("emend:"):])
                        if rules:
                            result[srow] = rules
                        # e.g. "# noqa: E501" with no emend: prefix → no effect
                    else:
                        result[srow] = None  # bare noqa suppresses all
    except tokenize.TokenError:
        pass
    return result


class _StatementRangeMapper(cst.CSTVisitor):
    """Map each line to the (start, end) range of its enclosing simple statement."""

    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider,)

    def __init__(self) -> None:
        self.line_to_range: dict[int, tuple[int, int]] = {}

    def visit_SimpleStatementLine(self, node: cst.SimpleStatementLine) -> bool:
        pos = self.get_metadata(cst.metadata.PositionProvider, node)
        for line in range(pos.start.line, pos.end.line + 1):
            self.line_to_range[line] = (pos.start.line, pos.end.line)
        return True


def build_noqa_ranges(
    noqa_comments: dict[int, set[str] | None],
    line_to_range: dict[int, tuple[int, int]],
) -> list[tuple[int, int, set[str] | None]]:
    """Expand noqa comments to cover their enclosing statement's line range."""
    ranges: list[tuple[int, int, set[str] | None]] = []
    for line, rules in noqa_comments.items():
        if line in line_to_range:
            start, end = line_to_range[line]
        else:
            start, end = line, line
        ranges.append((start, end, rules))
    return ranges


def is_noqa_suppressed(
    line: int,
    rule_name: str,
    noqa_ranges: list[tuple[int, int, set[str] | None]],
) -> bool:
    """Check whether a violation at *line* for *rule_name* is suppressed."""
    for start, end, rules in noqa_ranges:
        if start <= line <= end:
            if rules is None or rule_name in rules:
                return True
    return False


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
        source = Path(file_path).read_text()
        noqa_comments = parse_noqa_comments(source)
        noqa_ranges: list[tuple[int, int, set[str] | None]] = []
        if noqa_comments:
            try:
                module = cst.parse_module(source)
                wrapper = cst.MetadataWrapper(module)
                mapper = _StatementRangeMapper()
                wrapper.visit(mapper)
                noqa_ranges = build_noqa_ranges(noqa_comments, mapper.line_to_range)
            except cst.ParserSyntaxError:
                logger.debug("Failed to parse %s for noqa ranges", file_path, exc_info=True)

        for rule in rules:
            if fix and rule.replace:
                # Pre-find matches to check for noqa suppression
                try:
                    matches = find_pattern(
                        rule.find,
                        file_path,
                        not_inside=rule.not_inside,
                    )
                except Exception:
                    logger.debug("find_pattern failed for rule %s on %s", rule.name, file_path, exc_info=True)
                    continue

                suppressed_lines: set[int] = set()
                active_count = 0
                for match in matches:
                    line = match.line or 0
                    if is_noqa_suppressed(line, rule.name, noqa_ranges):
                        suppressed_lines.add(line)
                    else:
                        active_count += 1

                if active_count == 0:
                    continue

                original_lines = source.splitlines(keepends=True)
                diff, count = replace_pattern(
                    rule.find,
                    rule.replace,
                    file_path,
                    not_inside=rule.not_inside,
                    apply=True,
                )
                if count > 0 and suppressed_lines:
                    # Restore suppressed lines from original source
                    fixed_lines = Path(file_path).read_text().splitlines(keepends=True)
                    if len(fixed_lines) == len(original_lines):
                        for suppressed_line in suppressed_lines:
                            for start, end, _rules in noqa_ranges:
                                if start <= suppressed_line <= end:
                                    for idx in range(start - 1, min(end, len(original_lines))):
                                        fixed_lines[idx] = original_lines[idx]
                                    break
                        Path(file_path).write_text("".join(fixed_lines))
                if count > 0:
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
                    logger.debug("find_pattern failed for rule %s on %s", rule.name, file_path, exc_info=True)
                    continue

                for match in matches:
                    if is_noqa_suppressed(match.line or 0, rule.name, noqa_ranges):
                        continue
                    match_text = cst.Module([]).code_for_node(match.node).strip()
                    violations.append(LintViolation(
                        rule_name=rule.name,
                        message=rule.message,
                        file_path=file_path,
                        line=match.line or 0,
                        match_text=match_text,
                    ))

    return violations
