"""Pattern-based lint rules: find/not-inside/replace.

Canonical home for LintRule, LintViolation, FlowWitness dataclasses and
helper utilities shared by the lint engine. The full engine (run_lint) lives
in emend.lint for now; it will migrate here in a future cleanup phase.

This module handles:
  - LintRule / LintViolation / FlowWitness data model
  - DSL pattern compilation
  - Language detection and file glob matching helpers

The flow rules live in checks/flow.py (via execute_flow_spec).
The dead code rules delegate to transform.find_dead_code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LintRule:
    """A lint rule definition."""
    name: str
    find: str
    message: str
    not_inside: str | None = None
    replace: str | None = None
    # Flow predicates
    flows_from: str | None = None  # source pattern
    flows_to: str | None = None  # sink pattern
    not_through: str | None = None  # sanitizer pattern
    # DSL mode: "sql", "css", "html", etc.  When set, the rule matches
    # inside embedded DSL regions rather than host-language code.
    dsl: str | None = None
    files: list[str] | None = None
    # Language scope: restrict this rule to files of a specific language.
    language: str | list[str] | None = None


@dataclass
class LintViolation:
    """A lint violation found by a rule."""
    rule_name: str
    message: str
    file_path: str
    line: int
    col: int = 0
    match_text: str = ""
    witness: Any = None  # FlowWitness | None


@dataclass
class FlowWitness:
    """A witness trace for a flow violation."""
    source_line: int
    source_text: str
    sink_line: int
    sink_text: str
    taint_chain: list[tuple[int, str]]  # (line, variable_name) steps from source to sink


def _rule_matches_language(rule: LintRule, file_language: str) -> bool:
    """Return True if *rule* should apply to a file with *file_language*."""
    if rule.language is None:
        return True
    if isinstance(rule.language, str):
        return rule.language == file_language
    return file_language in rule.language


def _detect_file_language(file_path: str, fallback: str = "python") -> str:
    """Detect language from file extension, falling back to *fallback*."""
    from emend.language_registry import detect_language
    return detect_language(file_path) or fallback


def _path_matches_rule_globs(file_path: str, globs: list[str] | None) -> bool:
    if not globs:
        return True
    normalized = file_path.replace("\\", "/")
    path_obj = Path(normalized)
    for pattern in globs:
        if fnmatch(normalized, pattern) or path_obj.match(pattern):
            return True
        prefixed = pattern if pattern.startswith("**/") else f"**/{pattern}"
        if fnmatch(normalized, prefixed) or path_obj.match(prefixed):
            return True
    return False


def _compile_dsl_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a DSL lint pattern to a regex.

    Supports ``$METAVAR`` placeholders that match identifiers, and
    ``$...METAVAR`` for capturing multiple tokens.  Other text is
    matched literally (case-insensitive for SQL).  Whitespace in the
    pattern matches any whitespace including newlines.
    """
    parts = re.split(r'(\$\.\.\.?\w+|\$\w+)', pattern)
    regex_parts: list[str] = []
    for part in parts:
        if part.startswith("$..."):
            regex_parts.append(r'(.+?)')
        elif part.startswith("$"):
            regex_parts.append(r'(\w+(?:\.\w+)*(?:\s*,\s*\w+(?:\.\w+)*)*)')
        else:
            escaped = re.escape(part)
            # Replace whitespace runs with \s+ for cross-line matching
            escaped = re.sub(r'(\\ )+', r'\\s+', escaped)
            regex_parts.append(escaped)
    return re.compile(''.join(regex_parts), re.IGNORECASE | re.DOTALL)
