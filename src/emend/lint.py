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

from emend.transform import find_pattern, replace_pattern, prefilter_files_for_pattern, extract_pattern_literals

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

    Batches all find-only rules so each file is read and parsed only once,
    regardless of how many rules are checked.

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

    # Split rules into find-only and fix rules
    find_only_rules = [r for r in rules if not (fix and r.replace)]
    fix_rules = [r for r in rules if fix and r.replace]

    violations = []

    # Batch-read all candidate files in parallel via Rust
    import emend_core
    all_file_contents: dict[str, str] = dict(emend_core.read_and_filter_files(paths, []))

    # Pre-filter per rule using already-read content (no extra I/O)
    rule_file_sets: dict[str, set[str]] = {}
    for rule in find_only_rules:
        literals = extract_pattern_literals(rule.find)
        matching: set[str] = set()
        for fpath, content in all_file_contents.items():
            if all(lit in content for lit in literals):
                matching.add(fpath)
        rule_file_sets[rule.name] = matching

    # --- Rust fast-path: batch process compatible find-only rules ---
    libcst_rules = find_only_rules  # Will be narrowed if Rust handles some rules
    try:
        from emend.pattern import compile_pattern_to_rust_ir, compile_constraint_to_rust_ir

        rust_rules = []
        libcst_fallback = []
        for rule in find_only_rules:
            ir = compile_pattern_to_rust_ir(rule.find)
            if ir is None:
                libcst_fallback.append(rule)
                continue
            ni_ir = compile_constraint_to_rust_ir(rule.not_inside) if rule.not_inside else None
            if rule.not_inside is not None and ni_ir is None:
                # not_inside constraint didn't compile — fall back to LibCST
                libcst_fallback.append(rule)
                continue
            rust_rules.append((rule, ir, ni_ir))

        libcst_rules = libcst_fallback

        # noqa_ranges cache: lazily built per file when matches are found
        noqa_ranges_cache: dict[str, list[tuple[int, int, set[str] | None]]] = {}

        for rule, ir, ni_ir in rust_rules:
            file_pairs = [
                (fp, all_file_contents[fp])
                for fp in rule_file_sets.get(rule.name, set())
                if fp in all_file_contents
            ]
            if not file_pairs:
                continue

            raw_matches = emend_core.find_pattern_in_files(file_pairs, ir, None, ni_ir)

            for file_path_str, line, _col, _end_line, _end_col, text in raw_matches:
                # Build noqa ranges for this file on first match
                if file_path_str not in noqa_ranges_cache:
                    src = all_file_contents.get(file_path_str, "")
                    noqa_comments = parse_noqa_comments(src)
                    noqa_ranges_for_file: list[tuple[int, int, set[str] | None]] = []
                    if noqa_comments:
                        try:
                            module = cst.parse_module(src)
                            wrapper = cst.MetadataWrapper(module)
                            mapper = _StatementRangeMapper()
                            wrapper.visit(mapper)
                            noqa_ranges_for_file = build_noqa_ranges(noqa_comments, mapper.line_to_range)
                        except cst.ParserSyntaxError:
                            logger.debug(
                                "Failed to parse %s for noqa ranges", file_path_str, exc_info=True
                            )
                    noqa_ranges_cache[file_path_str] = noqa_ranges_for_file

                if is_noqa_suppressed(line, rule.name, noqa_ranges_cache[file_path_str]):
                    continue

                violations.append(LintViolation(
                    rule_name=rule.name,
                    message=rule.message,
                    file_path=file_path_str,
                    line=line,
                    match_text=text.strip(),
                ))

    except Exception:
        logger.debug("Rust fast-path failed, falling back to LibCST for all rules", exc_info=True)
        libcst_rules = find_only_rules

    # Determine files that need LibCST processing (remaining find rules + fix rules)
    files_needing_processing: set[str] = set()
    for rule in libcst_rules:
        files_needing_processing |= rule_file_sets.get(rule.name, set())

    for file_path in paths:
        # Skip files entirely if no LibCST find rules apply and no fix rules exist
        if file_path not in files_needing_processing and not fix_rules:
            continue

        source = all_file_contents.get(file_path)
        if source is None:
            continue

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

        # --- LibCST find-only rules: read source once, pass to each rule ---
        for rule in libcst_rules:
            # Skip this rule if file was pre-filtered out
            if file_path not in rule_file_sets.get(rule.name, set()):
                continue
            try:
                matches = find_pattern(
                    rule.find,
                    file_path,
                    not_inside=rule.not_inside,
                    source_override=source,
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

        # --- Fix rules: these mutate the file so must run sequentially ---
        for rule in fix_rules:
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

    return violations
