"""Lint command with pattern macros for emend."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

logger = logging.getLogger(__name__)

from emend.transform import find_pattern, replace_pattern, extract_pattern_literals
from emend.trace import _extract_identifiers
from emend.rules_config import (
    LEGACY_PATTERNS_PATH,
    load_rules_document,
    yaml_key,
    as_list,
    expand_macros,
    expand_not_through,
)


@dataclass
class FlowWitness:
    """A witness trace for a flow violation."""
    source_line: int
    source_text: str
    sink_line: int
    sink_text: str
    taint_chain: list[tuple[int, str]]  # (line, variable_name) steps from source to sink


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
    # Can be a single string ("typescript"), a list (["python", "typescript"]),
    # or None (applies to all languages where the pattern parses).
    language: str | list[str] | None = None


@dataclass
class DeadCodeConfig:
    """Configuration for the deadcode lint rule."""
    enabled: bool = False
    rule_name: str = "deadcode"
    kind: str | None = None
    include_private: bool = False
    exclude_references_from: list[str] | None = None
    strings_count_as_references: bool = True
    message: str = "Symbol appears to be unused"
    entry_point_decorators: list[str] | None = None
    entry_point_names: list[str] | None = None
    exclude_paths: list[str] | None = None


@dataclass
class LintViolation:
    """A lint violation found by a rule."""
    rule_name: str
    message: str
    file_path: str
    line: int
    col: int = 0
    match_text: str = ""
    witness: FlowWitness | None = None


def parse_noqa_comments(source: str, language: str = "python") -> dict[int, set[str] | None]:
    """Find real ``# noqa`` comments via the tokenizer.

    Returns a mapping of line number to either ``None`` (bare noqa, suppresses
    all emend rules) or a set of emend rule names extracted from
    ``emend:<rule>`` entries.
    """
    from emend.language_plugins import load_plugin
    return load_plugin(language).comment_handler.find_noqa_comments(source)


def _build_statement_line_map(source: str, ext: str = "py") -> dict[int, tuple[int, int]]:
    """Build a mapping from line -> (stmt_start, stmt_end) using Rust tree-sitter.

    Uses emend_core.get_statement_ranges() for statement range mapping.
    """
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
            # Match both bare name and emend:-prefixed variant
            if rule_name in rules or f"emend:{rule_name}" in rules:
                return True
    return False


def _coerce_optional_str_list(value: object) -> list[str] | None:
    if value is None:
        return None
    values = [str(v) for v in as_list(value)]
    return values or None


def _parse_deadcode_config(raw_dc: object, *, rule_name: str = "deadcode") -> DeadCodeConfig | None:
    if raw_dc is None:
        return None
    if isinstance(raw_dc, bool):
        return DeadCodeConfig(enabled=raw_dc)
    if not isinstance(raw_dc, dict):
        return None

    entry_points = raw_dc.get("entry-points")
    ep_decorators = yaml_key(raw_dc, "entry_point_decorators")
    ep_names = yaml_key(raw_dc, "entry_point_names")
    if isinstance(entry_points, dict):
        ep_decorators = ep_decorators or entry_points.get("decorators")
        ep_names = ep_names or entry_points.get("names")

    return DeadCodeConfig(
        enabled=raw_dc.get("enabled", True),
        rule_name=rule_name,
        kind=raw_dc.get("kind"),
        include_private=raw_dc.get("include-private", False),
        exclude_references_from=_coerce_optional_str_list(
            yaml_key(raw_dc, "exclude_references_from")
        ),
        strings_count_as_references=raw_dc.get("strings-count-as-references", True),
        message=raw_dc.get("message", "Symbol appears to be unused"),
        entry_point_decorators=_coerce_optional_str_list(ep_decorators),
        entry_point_names=_coerce_optional_str_list(ep_names),
        exclude_paths=_coerce_optional_str_list(yaml_key(raw_dc, "exclude_paths")),
    )


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


def load_rules(
    config_path: str | None = None,
) -> tuple[list[LintRule], dict[str, str], DeadCodeConfig | None]:
    """Parse a YAML rules file into LintRule objects.

    Args:
        config_path: Path to the YAML config file

    Returns:
        Tuple of (rules, macros, deadcode_config) where rules is a list
        of LintRule, macros is a dict of macro name to pattern string,
        and deadcode_config is a DeadCodeConfig if the ``deadcode``
        section is present.

    Raises:
        FileNotFoundError: If config file does not exist
    """
    config, _path = load_rules_document(
        config_path,
        fallbacks=(LEGACY_PATTERNS_PATH,),
    )

    macros = config.get("macros", {}) or {}
    raw_rules = config.get("rules", {}) or {}

    rules = []
    deadcode_config = _parse_deadcode_config(config.get("deadcode"))
    for name, rule_def in raw_rules.items():
        if not isinstance(rule_def, dict):
            continue

        if "deadcode" in rule_def:
            parsed_deadcode = _parse_deadcode_config(rule_def.get("deadcode"), rule_name=name)
            if parsed_deadcode is not None and deadcode_config is None:
                if rule_def.get("message"):
                    parsed_deadcode.message = rule_def["message"]
                deadcode_config = parsed_deadcode
            continue

        flow_def = rule_def.get("flow")
        flows_from = yaml_key(rule_def, "flows_from")
        flows_to = yaml_key(rule_def, "flows_to")
        not_through = yaml_key(rule_def, "not_through")
        if isinstance(flow_def, dict):
            flows_from = flows_from or flow_def.get("from")
            flows_to = flows_to or flow_def.get("to")
            not_through = not_through or yaml_key(flow_def, "not_through")

        # Dict-form flow definitions (e.g. {pattern: ..., type_constraint: ...})
        # need the pattern string extracted before macro expansion.
        if isinstance(flows_from, dict):
            flows_from = flows_from.get("pattern")
        if isinstance(flows_to, dict):
            flows_to = flows_to.get("pattern")

        if flows_from and flows_to:
            # Flow rule: find is not required
            find_pattern_str = rule_def.get("find", "")
            flows_from = expand_macros(flows_from, macros)
            flows_to = expand_macros(flows_to, macros)
            not_through = expand_not_through(not_through, macros)
        else:
            # Pattern rule: find is required
            match_pattern = rule_def.get("match", rule_def.get("find"))
            find_pattern_str = expand_macros(match_pattern, macros)

        rule_files = _coerce_optional_str_list(rule_def.get("files"))

        raw_language = rule_def.get("language")
        if isinstance(raw_language, str):
            rule_language: str | list[str] | None = raw_language
        elif isinstance(raw_language, list):
            rule_language = [str(l) for l in raw_language]
        else:
            rule_language = None

        rules.append(LintRule(
            name=name,
            find=find_pattern_str,
            message=rule_def.get("message", ""),
            not_inside=yaml_key(rule_def, "not_within", "not_inside"),
            replace=rule_def.get("fix", rule_def.get("replace")),
            flows_from=flows_from if flows_from else None,
            flows_to=flows_to if flows_to else None,
            not_through=not_through if not_through else None,
            dsl=rule_def.get("dsl"),
            files=rule_files,
            language=rule_language,
        ))

    return rules, macros, deadcode_config


# Reuse shared helpers from taint module
_extract_names_from_text = _extract_identifiers


def _assignments_from_cfgs(
    source: str,
    file_path: str,
    func_start: int,
    func_end: int,
) -> list[tuple[int, str, str]]:
    """Find assignments via tree-sitter CFG block defs.

    Returns list of ``(abs_line, target_name, rhs_text)`` for writes/aug_writes
    within the given function's line range.
    """
    from emend.trace import _defs_from_cfgs, _extract_rhs_from_line

    ext = Path(file_path).suffix.lstrip('.') or 'py'
    source_lines = source.splitlines()
    assignments: list[tuple[int, str, str]] = []

    for abs_line, var_name in _defs_from_cfgs(source, func_start, func_end, ext=ext):
        rhs = _extract_rhs_from_line(source_lines, abs_line)
        if rhs is not None:
            assignments.append((abs_line, var_name, rhs))

    return sorted(assignments, key=lambda a: a[0])


def _check_flow_rule(
    rule: LintRule,
    file_path: str,
    source: str,
    language: str,
    fact_graph: "Any | None" = None,  # unused; retained for API compatibility
) -> list[LintViolation]:
    """Check a flow-based lint rule within each function in the file.

    For each function body, finds source and sink pattern matches, then
    propagates taint using the intraprocedural flow analysis engine.
    """
    from emend import emend_core
    from emend.ast_utils import _rust_dict_to_nested_symbol

    assert rule.flows_from is not None
    assert rule.flows_to is not None

    violations: list[LintViolation] = []

    # Get function definitions from source
    ext = Path(file_path).suffix.lstrip('.') or 'py'
    rust_syms = emend_core.collect_symbols_from_str(source, ext=ext)
    symbols = [
        _rust_dict_to_nested_symbol(d) for d in rust_syms
        if d.get("kind") not in ("variable", "reference")
    ]

    # Flatten to get all functions (including nested methods)
    def _all_functions(syms):
        for sym in syms:
            if sym.kind in ('function', 'async_function', 'method', 'async_method'):
                yield sym
            yield from _all_functions(sym.children)

    # Hoist pattern matching and line splitting out of the per-function loop
    all_source_matches = find_pattern(
        rule.flows_from, file_path, source_override=source, language=language
    )
    all_sink_matches = find_pattern(
        rule.flows_to, file_path, source_override=source, language=language
    )
    all_sanitizer_matches = None
    if rule.not_through:
        all_sanitizer_matches = find_pattern(
            rule.not_through, file_path,
            source_override=source, language=language
        )

    # Intraprocedural flow analysis
    all_lines = source.splitlines()
    total_lines = len(all_lines)

    # Build the list of scopes to analyze.  When collect_symbols_from_str
    # doesn't detect any functions (e.g. TypeScript arrow functions or
    # top-level Rust code), fall back to analyzing the entire file as one
    # scope so that source→sink pairs are still checked.
    class _FakeScope:
        def __init__(self, start: int, end: int):
            self.line_start = start
            self.line_end = end

    function_scopes = list(_all_functions(symbols))
    if not function_scopes and all_source_matches and all_sink_matches:
        function_scopes = [_FakeScope(1, total_lines)]

    for sym in function_scopes:
        # Filter to matches within this function's line range
        func_sources = [
            m for m in all_source_matches
            if m.line is not None and sym.line_start <= m.line <= sym.line_end
        ]
        func_sinks = [
            m for m in all_sink_matches
            if m.line is not None and sym.line_start <= m.line <= sym.line_end
        ]

        if not func_sources or not func_sinks:
            continue

        # Find assignments within the function via tree-sitter CFG defs.
        assignments = _assignments_from_cfgs(
            source, file_path,
            func_start=sym.line_start,
            func_end=sym.line_end,
        )

        for src_match in func_sources:
            src_line = src_match.line or 0
            tainted: dict[str, int] = {}  # name -> line where it became tainted

            for cap_name, cap_text in src_match.captures.items():
                for name in _extract_names_from_text(cap_text):
                    tainted[name] = src_line

            if src_match.matched_text:
                matched_line_text = all_lines[src_line - 1].strip() if src_line <= len(all_lines) else ""
                # Handle declaration keywords: const/let/var (TS), let/let mut (Rust)
                assign_match = re.match(
                    r'^(?:(?:const|let|var|let\s+mut)\s+)?([a-zA-Z_]\w*)(?:\s*:\s*[^=]+?)?\s*=\s*',
                    matched_line_text,
                )
                if assign_match:
                    tainted[assign_match.group(1)] = src_line

            taint_chain: list[tuple[int, str]] = [
                (src_line, ', '.join(sorted(tainted.keys())))
            ]

            for assign_line, target, rhs in sorted(assignments, key=lambda a: a[0]):
                if assign_line <= src_line:
                    continue
                rhs_names = _extract_names_from_text(rhs)
                if rhs_names & set(tainted.keys()):
                    tainted[target] = assign_line
                    taint_chain.append((assign_line, target))

            for sink_match in func_sinks:
                sink_line = sink_match.line or 0
                if sink_line <= src_line:
                    continue

                sink_names: set[str] = set()
                for cap_name, cap_text in sink_match.captures.items():
                    sink_names |= _extract_names_from_text(cap_text)
                if sink_match.matched_text:
                    sink_names |= _extract_names_from_text(sink_match.matched_text or "")

                tainted_at_sink = {
                    name for name, line in tainted.items()
                    if line <= sink_line
                }

                if not (sink_names & tainted_at_sink):
                    continue

                sanitized = False
                if all_sanitizer_matches:
                    for san_match in all_sanitizer_matches:
                        san_line = san_match.line or 0
                        if src_line <= san_line < sink_line:
                            san_names: set[str] = set()
                            for cap_name, cap_text in san_match.captures.items():
                                san_names |= _extract_names_from_text(cap_text)
                            if san_match.matched_text:
                                san_names |= _extract_names_from_text(san_match.matched_text or "")
                            if san_names & tainted_at_sink:
                                sanitized = True
                                break
                            if san_line <= len(all_lines):
                                san_line_text = all_lines[san_line - 1].strip()
                                san_assign = re.match(
                                    r'^(?:(?:const|let|var|let\s+mut)\s+)?([a-zA-Z_]\w*)(?:\s*:\s*[^=]+?)?\s*=\s*',
                                    san_line_text,
                                )
                                if san_assign and san_assign.group(1) in tainted_at_sink:
                                    sanitized = True
                                    break

                if sanitized:
                    continue

                src_text = (src_match.matched_text or "").strip()
                sink_text = (sink_match.matched_text or "").strip()
                witness = FlowWitness(
                    source_line=src_line,
                    source_text=src_text,
                    sink_line=sink_line,
                    sink_text=sink_text,
                    taint_chain=[
                        step for step in taint_chain if step[0] <= sink_line
                    ],
                )

                violations.append(LintViolation(
                    rule_name=rule.name,
                    message=rule.message,
                    file_path=file_path,
                    line=sink_line,
                    match_text=f"flow: {src_text} -> {sink_text}",
                    witness=witness,
                ))

    return violations


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


def run_lint(
    rules: list[LintRule],
    paths: list[str],
    fix: bool = False,
    rule_filter: str | None = None,
    deadcode_config: DeadCodeConfig | None = None,
    project_path: str | None = None,
    language: str = "python",
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

    # Build per-file language map for auto-detection
    file_languages: dict[str, str] = {}
    for fp in paths:
        file_languages[fp] = _detect_file_language(fp, fallback=language)

    # Separate flow rules, DSL rules, and pattern rules
    flow_rules = [r for r in rules if r.flows_from and r.flows_to]
    dsl_rules = [r for r in rules if r.dsl and not (r.flows_from and r.flows_to)]
    pattern_rules = [r for r in rules if not (r.flows_from and r.flows_to) and not r.dsl]

    # Split pattern rules into find-only and fix rules
    find_only_rules = [r for r in pattern_rules if not (fix and r.replace)]
    fix_rules = [r for r in pattern_rules if fix and r.replace]

    violations = []

    # Batch-read all candidate files in parallel via Rust
    from emend import emend_core
    all_file_contents: dict[str, str] = dict(emend_core.read_and_filter_files(paths, []))

    # Update file_languages for files resolved by the Rust reader (may
    # differ from the input paths when symlinks or canonical paths are
    # involved).
    for fp in all_file_contents:
        if fp not in file_languages:
            file_languages[fp] = _detect_file_language(fp, fallback=language)

    # Pre-filter per rule using already-read content (no extra I/O).
    # Also respects per-rule language scope.
    rule_file_sets: dict[str, set[str]] = {}
    for rule in find_only_rules:
        literals = extract_pattern_literals(rule.find)
        matching: set[str] = set()
        for fpath, content in all_file_contents.items():
            if not _path_matches_rule_globs(fpath, rule.files):
                continue
            if not _rule_matches_language(rule, file_languages.get(fpath, language)):
                continue
            if all(lit in content for lit in literals):
                matching.add(fpath)
        rule_file_sets[rule.name] = matching

    # --- Rust fast-path: batch process compatible find-only rules ---
    # Group files by detected language so pattern compilation uses the
    # correct tree-sitter grammar.
    from emend.pattern import compile_pattern_to_rust_ir, compile_constraint_to_rust_ir

    # Discover which languages are present among candidate files
    langs_present: set[str] = set()
    for rule in find_only_rules:
        for fp in rule_file_sets.get(rule.name, set()):
            langs_present.add(file_languages.get(fp, language))

    # noqa_ranges cache: lazily built per file when matches are found
    noqa_ranges_cache: dict[str, list[tuple[int, int, set[str] | None]]] = {}

    # Track fallback rules per language
    all_fallback_rules_by_lang: dict[str, list[LintRule]] = {}

    for lang in langs_present:
        lang_files = {fp for fp in all_file_contents
                      if file_languages.get(fp, language) == lang}

        rust_rules = []
        fallback_rules = []
        for rule in find_only_rules:
            if not _rule_matches_language(rule, lang):
                continue
            # Only consider this rule if it has candidate files in this language
            rule_lang_files = rule_file_sets.get(rule.name, set()) & lang_files
            if not rule_lang_files:
                continue
            # The Rust batch scanner (find_multi_patterns_in_files) only
            # supports Python files.  Route non-Python languages to the
            # single-file fallback path which uses find_pattern().
            if lang != "python":
                fallback_rules.append(rule)
                continue
            ir = compile_pattern_to_rust_ir(rule.find, language=lang)
            if ir is None:
                fallback_rules.append(rule)
                continue
            ni_ir = compile_constraint_to_rust_ir(rule.not_inside, language=lang) if rule.not_inside else None
            if rule.not_inside is not None and ni_ir is None:
                fallback_rules.append(rule)
                continue
            rust_rules.append((rule, ir, ni_ir))

        all_fallback_rules_by_lang[lang] = fallback_rules

        # Batched scan for this language group
        all_rust_files = set()
        for rule, _ir, _ni_ir in rust_rules:
            all_rust_files |= (rule_file_sets.get(rule.name, set()) & lang_files)
        rust_file_pairs = [
            (fp, all_file_contents[fp])
            for fp in all_rust_files
            if fp in all_file_contents
        ]

        if rust_file_pairs and rust_rules:
            patterns_for_batch = [(ir, ni_ir) for _rule, ir, ni_ir in rust_rules]
            batch_matches = emend_core.find_multi_patterns_in_files(
                rust_file_pairs, patterns_for_batch
            )
            for rule_idx, file_path_str, line, _col, _end_line, _end_col, text in batch_matches:
                rule = rust_rules[rule_idx][0]
                if file_path_str not in rule_file_sets.get(rule.name, set()):
                    continue

                if file_path_str not in noqa_ranges_cache:
                    src = all_file_contents.get(file_path_str, "")
                    file_lang = file_languages.get(file_path_str, language)
                    noqa_comments = parse_noqa_comments(src, language=file_lang)
                    noqa_ranges_for_file: list[tuple[int, int, set[str] | None]] = []
                    if noqa_comments:
                        line_map = _build_statement_line_map(src)
                        noqa_ranges_for_file = build_noqa_ranges(
                            noqa_comments, line_map
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

    # Determine files that need single-file processing (remaining find rules)
    files_needing_processing: set[str] = set()
    for lang, fb_rules in all_fallback_rules_by_lang.items():
        lang_files = {fp for fp in all_file_contents
                      if file_languages.get(fp, language) == lang}
        for rule in fb_rules:
            files_needing_processing |= (rule_file_sets.get(rule.name, set()) & lang_files)

    # --- Single-file find rules ---
    def _process_file_fallback(file_path: str) -> list[LintViolation]:
        source = all_file_contents.get(file_path)
        if source is None:
            return []
        file_lang = file_languages.get(file_path, language)
        fb_rules = all_fallback_rules_by_lang.get(file_lang, [])
        file_violations: list[LintViolation] = []
        # Build noqa ranges lazily: only when a rule actually produces matches.
        noqa_ranges: list[tuple[int, int, set[str] | None]] | None = None
        # Build line-offset table lazily for extracting match text from source.
        line_starts: list[int] | None = None
        for rule in fb_rules:
            if file_path not in rule_file_sets.get(rule.name, set()):
                continue
            try:
                matches = find_pattern(
                    rule.find,
                    file_path,
                    not_inside=rule.not_inside,
                    source_override=source,
                    language=file_lang,
                )
            except Exception:
                logger.debug(
                    "find_pattern failed for rule %s on %s", rule.name, file_path, exc_info=True
                )
                continue
            if not matches:
                continue
            # First match for this file: build noqa ranges now
            if noqa_ranges is None:
                noqa_ranges = []
                noqa_comments = parse_noqa_comments(source, language=file_lang)
                if noqa_comments:
                    line_map = _build_statement_line_map(source)
                    noqa_ranges = build_noqa_ranges(noqa_comments, line_map)
            # Build line-offset table on first match (once per file)
            if line_starts is None:
                line_starts = [0]
                for i, ch in enumerate(source):
                    if ch == '\n':
                        line_starts.append(i + 1)
            for match in matches:
                if is_noqa_suppressed(match.line or 0, rule.name, noqa_ranges):
                    continue
                # Extract match text from source using line/col positions
                if match.matched_text is not None:
                    match_text = match.matched_text.strip()
                elif (match.line and match.col is not None
                      and match.end_line and match.end_col is not None):
                    start = line_starts[match.line - 1] + match.col
                    end = line_starts[match.end_line - 1] + match.end_col
                    match_text = source[start:end].strip()
                else:
                    match_text = ""
                file_violations.append(LintViolation(
                    rule_name=rule.name,
                    message=rule.message,
                    file_path=file_path,
                    line=match.line or 0,
                    match_text=match_text,
                ))
        return file_violations

    for fp in paths:
        if fp in files_needing_processing:
            violations.extend(_process_file_fallback(fp))

    # --- Fix rules: these mutate the file so must run sequentially ---
    for file_path in paths:
        if not fix_rules:
            break
        source = all_file_contents.get(file_path)
        if source is None:
            continue
        file_lang = file_languages.get(file_path, language)
        noqa_comments = parse_noqa_comments(source, language=file_lang)
        noqa_ranges: list[tuple[int, int, set[str] | None]] = []
        if noqa_comments:
            line_map = _build_statement_line_map(source)
            noqa_ranges = build_noqa_ranges(noqa_comments, line_map)

        for rule in fix_rules:
            if not _path_matches_rule_globs(file_path, rule.files):
                continue
            if not _rule_matches_language(rule, file_lang):
                continue
            try:
                matches = find_pattern(
                    rule.find,
                    file_path,
                    not_inside=rule.not_inside,
                    language=file_lang,
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
                language=file_lang,
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

    # --- Flow rules: intraprocedural taint analysis ---
    if flow_rules:
        from emend.flow_ir import from_lint_rule, execute_flow_spec

        for file_path in paths:
            source = all_file_contents.get(file_path)
            if source is None:
                continue
            file_lang = file_languages.get(file_path, language)

            # Build noqa ranges for this file (reuse cache if available)
            if file_path not in noqa_ranges_cache:
                noqa_comments = parse_noqa_comments(source, language=file_lang)
                noqa_ranges_for_file_flow: list[tuple[int, int, set[str] | None]] = []
                if noqa_comments:
                    line_map = _build_statement_line_map(source)
                    noqa_ranges_for_file_flow = build_noqa_ranges(
                        noqa_comments, line_map
                    )
                noqa_ranges_cache[file_path] = noqa_ranges_for_file_flow

            for rule in flow_rules:
                if not _path_matches_rule_globs(file_path, rule.files):
                    continue
                if not _rule_matches_language(rule, file_lang):
                    continue
                # Pre-filter: check if source and sink literals exist in file
                from_literals = extract_pattern_literals(rule.flows_from or "")
                to_literals = extract_pattern_literals(rule.flows_to or "")
                if not all(lit in source for lit in from_literals):
                    continue
                if not all(lit in source for lit in to_literals):
                    continue

                try:
                    spec = from_lint_rule(rule)
                    flow_results = execute_flow_spec(
                        spec, file_path, source, file_lang, fact_graph=None
                    )
                except Exception:
                    logger.debug(
                        "Flow rule %s failed on %s",
                        rule.name, file_path, exc_info=True,
                    )
                    continue

                for fv in flow_results:
                    # Convert FlowViolation back to LintViolation for compat
                    witness = None
                    if fv.source_text or fv.sink_text:
                        witness = FlowWitness(
                            source_line=fv.source_line,
                            source_text=fv.source_text,
                            sink_line=fv.line,
                            sink_text=fv.sink_text,
                            taint_chain=[
                                (s.line, s.var_name)
                                for s in fv.witness
                                if s.kind in ("source", "propagation")
                            ],
                        )
                    v = LintViolation(
                        rule_name=spec.name,
                        message=fv.message,
                        file_path=fv.file_path,
                        line=fv.line,
                        col=fv.col,
                        match_text=f"flow: {fv.source_text} -> {fv.sink_text}",
                        witness=witness,
                    )
                    if is_noqa_suppressed(
                        v.line, v.rule_name,
                        noqa_ranges_cache.get(file_path, []),
                    ):
                        continue
                    violations.append(v)

    # --- DSL-aware lint rules ---
    if dsl_rules:
        from emend.dsl import detect_dsl_regions, extract_sql_symbols, DslKind

        for file_path in paths:
            source = all_file_contents.get(file_path)
            if source is None:
                continue
            file_lang = file_languages.get(file_path, language)

            # Build noqa ranges for this file
            if file_path not in noqa_ranges_cache:
                noqa_comments = parse_noqa_comments(source, language=file_lang)
                noqa_ranges_for_dsl: list[tuple[int, int, set[str] | None]] = []
                if noqa_comments:
                    line_map = _build_statement_line_map(source)
                    noqa_ranges_for_dsl = build_noqa_ranges(
                        noqa_comments, line_map
                    )
                noqa_ranges_cache[file_path] = noqa_ranges_for_dsl

            regions = detect_dsl_regions(file_path, source=source)
            if not regions:
                continue

            for rule in dsl_rules:
                if not _path_matches_rule_globs(file_path, rule.files):
                    continue
                rule_dsl = rule.dsl.lower() if rule.dsl else ""
                find_re = _compile_dsl_pattern(rule.find)

                for region in regions:
                    if region.dsl.value != rule_dsl:
                        continue
                    for m in find_re.finditer(region.content):
                        # Compute host-file line from region offset + match position
                        match_offset = m.start()
                        lines_before = region.content[:match_offset].count('\n')
                        match_line = region.host_start_line + lines_before
                        match_text = m.group(0).strip()

                        if is_noqa_suppressed(
                            match_line, rule.name,
                            noqa_ranges_cache.get(file_path, []),
                        ):
                            continue

                        violations.append(LintViolation(
                            rule_name=rule.name,
                            message=rule.message,
                            file_path=file_path,
                            line=match_line,
                            match_text=match_text,
                        ))

    # --- Dead code analysis (if configured) ---
    if (deadcode_config is not None
            and deadcode_config.enabled
            and (
                rule_filter is None
                or rule_filter == deadcode_config.rule_name
                or rule_filter in {"deadcode", "dead-code", "dead_code"}
            )):
        from emend.transform import find_dead_code
        dc_project = project_path or "."
        try:
            dead = find_dead_code(
                project_path=dc_project,
                kind=deadcode_config.kind,
                include_private=deadcode_config.include_private,
                exclude_references_from=deadcode_config.exclude_references_from,
                strings_count_as_references=deadcode_config.strings_count_as_references,
                show_last_reference=False,
                entry_point_decorators=deadcode_config.entry_point_decorators,
                entry_point_names=deadcode_config.entry_point_names,
                exclude_paths=deadcode_config.exclude_paths,
            )
            for d in dead:
                violations.append(LintViolation(
                    rule_name=deadcode_config.rule_name,
                    message=f"{deadcode_config.message}: {d.name}",
                    file_path=d.file_path,
                    line=d.line,
                    match_text=d.selector,
                ))
        except Exception:
            logger.debug("Dead code analysis failed", exc_info=True)

    return violations
