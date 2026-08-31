"""Pattern-based lint rules: find/not-inside/replace matching."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emend.checks.rules_config import DeadCodeConfig

logger = logging.getLogger(__name__)

from emend.checks.rules_config import (  # noqa: E402
    load_rules_document,
    yaml_key,
    coerce_optional_str_list,
    parse_deadcode_config,
    expand_macros,
    expand_pattern_macros,
    normalize_flow_definition,
    path_matches_glob,
)


@dataclass
class LintRule:
    """A lint rule definition."""
    name: str
    find: str
    message: str
    not_inside: str | None = None
    replace: str | None = None
    flows_from: str | None = None
    flows_to: str | None = None
    not_through: list[str] | str | None = None
    dsl: str | None = None
    files: list[str] | None = None
    language: str | list[str] | None = None
    severity: str = "warning"


@dataclass
class FlowWitness:
    """A witness trace for a flow violation."""
    source_line: int
    source_text: str
    sink_line: int
    sink_text: str
    taint_chain: list[tuple[int, str]]


def parse_noqa_comments(source: str, language: str = "python") -> dict[int, set[str] | None]:
    """Find real ``# noqa`` comments via the tokenizer."""
    from emend.language_plugins import load_plugin
    return load_plugin(language).comment_handler.find_noqa_comments(source)


def build_statement_line_map(source: str, ext: str = "py") -> dict[int, tuple[int, int]]:
    """Build a mapping from line -> (stmt_start, stmt_end) using tree-sitter."""
    from emend import emend_core
    line_to_range: dict[int, tuple[int, int]] = {}
    for start, end in emend_core.get_statement_ranges(source, ext=ext):
        for line in range(start, end + 1):
            line_to_range[line] = (start, end)
    return line_to_range


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
            if rules is None:
                return True
            if rule_name in rules or f"emend:{rule_name}" in rules:
                return True
    return False


def rule_matches_language(rule: LintRule, file_language: str) -> bool:
    """Return True if *rule* should apply to a file with *file_language*."""
    if rule.language is None:
        return True
    if isinstance(rule.language, str):
        return rule.language == file_language
    return file_language in rule.language


def detect_file_language(file_path: str, fallback: str = "python") -> str:
    """Detect language from file extension."""
    from emend.language_registry import detect_language
    return detect_language(file_path) or fallback


def path_matches_rule_globs(
    file_path: str,
    globs: list[str] | None,
    *,
    project_root: str | Path | None = None,
) -> bool:
    if not globs:
        return True
    for pattern in globs:
        if path_matches_glob(file_path, pattern, project_root=project_root):
            return True
    return False


def load_rules(
    config_path: str | None = None,
) -> "tuple[list[LintRule], dict[str, str], DeadCodeConfig | None]":
    """Parse a YAML rules file into LintRule objects."""
    config, _path = load_rules_document(config_path)

    macros = config.get("macros", {}) or {}
    raw_rules = config.get("rules", {}) or {}

    rules = []
    deadcode_config = parse_deadcode_config(config.get("deadcode"))
    for name, rule_def in raw_rules.items():
        if not isinstance(rule_def, dict):
            continue

        if "deadcode" in rule_def:
            parsed_deadcode = parse_deadcode_config(rule_def.get("deadcode"), rule_name=name)
            if parsed_deadcode is not None and deadcode_config is None:
                if rule_def.get("message"):
                    parsed_deadcode.message = rule_def["message"]
                deadcode_config = parsed_deadcode
            continue

        flow = normalize_flow_definition(rule_def, macros)
        flows_from = flow["from"]
        flows_to = flow["to"]
        not_through = flow["not_through"]

        if flows_from and flows_to:
            find_pattern_str = rule_def.get("find", "")
        else:
            match_pattern = rule_def.get("match", rule_def.get("find"))
            find_pattern_str = expand_macros(match_pattern, macros)

        rule_files = coerce_optional_str_list(rule_def.get("files"))

        raw_language = rule_def.get("language")
        if isinstance(raw_language, str):
            rule_language: str | list[str] | None = raw_language
        elif isinstance(raw_language, list):
            rule_language = [str(language) for language in raw_language]
        else:
            rule_language = None

        rules.append(LintRule(
            name=name,
            find=find_pattern_str,
            message=rule_def.get("message", ""),
            not_inside=expand_pattern_macros(yaml_key(rule_def, "not_within", "not_inside"), macros),
            replace=expand_pattern_macros(rule_def.get("fix", rule_def.get("replace")), macros),
            flows_from=flows_from if flows_from else None,
            flows_to=flows_to if flows_to else None,
            not_through=not_through if not_through else None,
            dsl=rule_def.get("dsl"),
            files=rule_files,
            language=rule_language,
            severity=str(rule_def.get("severity", "warning")),
        ))

    return rules, macros, deadcode_config
