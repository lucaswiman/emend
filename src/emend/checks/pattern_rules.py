"""Pattern-based lint rules: find/not-inside/replace matching."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emend.checks.rule_model import CompiledRuleDocument
    from emend.checks.rules_config import DeadCodeConfig

logger = logging.getLogger(__name__)

from emend.checks.rules_config import (  # noqa: E402
    compiled_deadcode_to_config,
    load_compiled_rules,
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
    *,
    compiled: "CompiledRuleDocument | None" = None,
) -> "tuple[list[LintRule], dict[str, str], DeadCodeConfig | None]":
    """Adapt the canonical compiled document to the established lint API."""
    document = compiled or load_compiled_rules(config_path)
    flows = {flow.name: flow for flow in document.flow_rules}
    rules: list[LintRule] = []
    for rule in document.pattern_rules:
        flow = flows.get(rule.name)
        sanitizers = [endpoint.pattern for endpoint in flow.sanitizers] if flow else []
        language: str | list[str] | None = None
        if len(rule.languages) == 1:
            language = rule.languages[0]
        elif rule.languages:
            language = list(rule.languages)
        rules.append(LintRule(
            name=rule.name, find=rule.pattern, message=rule.message,
            not_inside=rule.not_inside, replace=rule.replace,
            flows_from=flow.sources[0].pattern if flow else None,
            flows_to=flow.sinks[0].pattern if flow else None,
            not_through=(sanitizers[0] if len(sanitizers) == 1 else sanitizers or None),
            dsl=rule.dsl, files=list(rule.files) or None, language=language,
            severity=rule.severity,
        ))
    macros = {name: str(value) for name, value in document.macros.items()}
    deadcode = compiled_deadcode_to_config(document.deadcode) if document.deadcode else None
    return rules, macros, deadcode
