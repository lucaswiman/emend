"""DSL support for embedded languages.

Detects embedded DSL regions (SQL, CSS, HTML) inside host-language
string literals, extracts symbols, and resolves cross-language links
to host-language definitions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class DslKind(str, Enum):
    """Supported embedded DSL types."""
    SQL = "sql"
    CSS = "css"
    HTML = "html"
    GRAPHQL = "graphql"
    JINJA = "jinja"


class DslSymbolKind(str, Enum):
    """Kinds of symbols extracted from DSL regions."""
    TABLE = "table"
    COLUMN = "column"
    CSS_CLASS = "css_class"
    CSS_ID = "css_id"
    COMPONENT = "component"
    TEMPLATE_VAR = "template_var"
    GRAPHQL_TYPE = "graphql_type"
    GRAPHQL_FIELD = "graphql_field"


@dataclass
class DslRegion:
    """A detected embedded DSL region within a host file."""
    dsl: DslKind
    content: str                    # the DSL source text
    host_file: str                  # path to the host file
    host_start_line: int            # 1-based start line in host file
    host_start_col: int             # 0-based start column
    host_end_line: int              # 1-based end line
    host_end_col: int               # 0-based end column
    trigger: str                    # how it was detected: "call", "magic_comment", "file_extension"


@dataclass
class LinkHint:
    """A hint for resolving a DSL symbol to a host-language definition."""
    strategy: str          # "orm_model", "orm_column", "component_export", etc.
    target_pattern: str    # e.g., class name "User", component "UserCard"
    target_kind: str       # "class", "function", "variable"
    module_hint: str = ""  # optional: expected module path pattern


@dataclass
class DslSymbol:
    """A symbol extracted from an embedded DSL region."""
    name: str                       # e.g. "users", "email", "UserCard"
    kind: DslSymbolKind
    dsl: DslKind
    host_file: str
    host_line: int                  # 1-based line in host file
    host_col: int                   # 0-based column
    link_hints: list[LinkHint] = field(default_factory=list)


@dataclass
class DslLink:
    """A resolved link from a DSL symbol to a host-language definition."""
    dsl_symbol: DslSymbol
    target_qualified_name: str      # e.g. "models.User"
    target_file: str
    target_line: int
    strategy: str                   # resolution strategy used
    confidence: float               # 0.0-1.0


# ---------------------------------------------------------------------------
# Injection detection
# ---------------------------------------------------------------------------

# SQL keyword pattern used to identify SQL content in string literals
_SQL_KEYWORD_RE = re.compile(
    r'\b(SELECT|INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE\s+TABLE|WITH\s+\w|REPLACE\s+INTO)\b',
    re.IGNORECASE,
)

# Magic comment pattern: # language=sql
_MAGIC_COMMENT_RE = re.compile(r'#\s*language\s*=\s*(\w+)', re.IGNORECASE)

# Triple-quoted string patterns
_TRIPLE_DOUBLE_STRING_RE = re.compile(r'"""(.*?)"""', re.DOTALL)
_TRIPLE_SINGLE_STRING_RE = re.compile(r"'''(.*?)'''", re.DOTALL)

# Single-line string patterns (double or single quoted)
_SINGLE_DOUBLE_STRING_RE = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')
_SINGLE_SINGLE_STRING_RE = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'")


def _count_newlines_before(text: str, pos: int) -> int:
    """Count newlines in text[:pos], returning 0-based line offset."""
    return text[:pos].count('\n')


def detect_dsl_regions(
    file_path: str,
    source: str | None = None,
    dsls: list[DslKind] | None = None,
) -> list[DslRegion]:
    """Detect embedded DSL regions in a source file.

    Scans string literals for SQL patterns (SELECT, INSERT, UPDATE, DELETE,
    CREATE, ALTER, DROP) and magic comments (# language=sql).

    Args:
        file_path: Path to the source file.
        source: Source text (read from file if None).
        dsls: Which DSLs to detect (default: all).

    Returns:
        List of DslRegion objects.
    """
    if source is None:
        try:
            source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("Could not read %s: %s", file_path, e)
            return []

    if dsls is None or DslKind.SQL in dsls:
        return _detect_sql_regions(file_path, source)

    return []


def _detect_sql_regions(file_path: str, source: str) -> list[DslRegion]:
    """Detect SQL regions in Python source code.

    Looks for:
    1. Triple-quoted string literals containing SQL keywords
    2. Single-quoted string literals containing SQL keywords
    3. Magic comments: # language=sql
    """
    regions: list[DslRegion] = []
    seen_contents: set[str] = set()

    def _add_region(content: str, match_start: int, match_end: int, trigger: str) -> None:
        stripped = content.strip()
        if not stripped or stripped in seen_contents:
            return
        if not _SQL_KEYWORD_RE.search(stripped):
            return
        seen_contents.add(stripped)

        start_line = _count_newlines_before(source, match_start) + 1
        start_col = match_start - source.rfind('\n', 0, match_start) - 1
        end_line = _count_newlines_before(source, match_end) + 1
        end_col = match_end - source.rfind('\n', 0, match_end) - 1

        regions.append(DslRegion(
            dsl=DslKind.SQL,
            content=stripped,
            host_file=file_path,
            host_start_line=start_line,
            host_start_col=max(0, start_col),
            host_end_line=end_line,
            host_end_col=max(0, end_col),
            trigger=trigger,
        ))

    # Check for magic comment presence to determine trigger type
    magic_comment_active = bool(_MAGIC_COMMENT_RE.search(source))
    trigger = "magic_comment" if magic_comment_active else "literal"

    # Scan triple-quoted strings first (they have priority)
    for pattern in (_TRIPLE_DOUBLE_STRING_RE, _TRIPLE_SINGLE_STRING_RE):
        for m in pattern.finditer(source):
            content = m.group(1)
            _add_region(content, m.start(), m.end(), trigger)

    # Scan single-line strings
    for pattern in (_SINGLE_DOUBLE_STRING_RE, _SINGLE_SINGLE_STRING_RE):
        for m in pattern.finditer(source):
            content = m.group(1)
            # Skip if already captured as part of triple-quoted
            _add_region(content, m.start(), m.end(), trigger)

    return regions


# ---------------------------------------------------------------------------
# SQL Symbol Extraction
# ---------------------------------------------------------------------------

# SQL keyword patterns for table/column extraction
_SQL_TABLE_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE|TRUNCATE)\s+'
    r'(?:IF\s+(?:NOT\s+)?EXISTS\s+)?'
    r'`?(\w+)`?',
    re.IGNORECASE,
)

_SQL_COLUMN_LIST_RE = re.compile(
    r'\bSELECT\s+(.*?)\s+FROM\b',
    re.IGNORECASE | re.DOTALL,
)

_SQL_WHERE_COLUMN_RE = re.compile(
    r'\bWHERE\s+.*?`?(\w+)`?\s*(?:=|<|>|LIKE|IN|IS)',
    re.IGNORECASE,
)

# SQL reserved words to skip when extracting column names
_SQL_RESERVED = frozenset({
    "select", "from", "where", "join", "on", "as", "and", "or", "not",
    "in", "is", "null", "true", "false", "like", "between", "exists",
    "case", "when", "then", "else", "end", "distinct", "all", "any",
    "union", "intersect", "except", "order", "by", "group", "having",
    "limit", "offset", "into", "values", "set", "update", "delete",
    "insert", "create", "drop", "alter", "table", "index", "view",
    "count", "sum", "avg", "min", "max", "coalesce", "ifnull",
    "inner", "outer", "left", "right", "full", "cross", "natural",
    "asc", "desc", "primary", "key", "foreign", "references",
    "constraint", "unique", "default", "check", "truncate",
    "with", "recursive", "returning", "using",
})


def extract_sql_symbols(region: DslRegion) -> list[DslSymbol]:
    """Extract table and column names from a SQL region.

    Parses SQL statements using regex to identify:
    - Table names (FROM, JOIN, INTO, UPDATE, TABLE clauses)
    - Column names (SELECT list, WHERE conditions)

    Each table gets an orm_model LinkHint; each column gets an orm_column hint.

    Args:
        region: A DslRegion containing SQL content.

    Returns:
        List of DslSymbol objects with appropriate LinkHints.
    """
    symbols: list[DslSymbol] = []
    seen_tables: set[str] = set()
    seen_columns: set[str] = set()
    content = region.content

    # Extract table names
    for m in _SQL_TABLE_RE.finditer(content):
        table_name = m.group(1).lower()
        if table_name in _SQL_RESERVED or table_name in seen_tables:
            continue
        seen_tables.add(table_name)

        # Build ORM link hint: singularize + PascalCase
        class_name = _to_pascal_case(_singularize(table_name))
        symbols.append(DslSymbol(
            name=table_name,
            kind=DslSymbolKind.TABLE,
            dsl=region.dsl,
            host_file=region.host_file,
            host_line=region.host_start_line,
            host_col=region.host_start_col,
            link_hints=[
                LinkHint(
                    strategy="orm_model",
                    target_pattern=class_name,
                    target_kind="class",
                ),
            ],
        ))

    # Extract column names from SELECT list
    col_match = _SQL_COLUMN_LIST_RE.search(content)
    if col_match:
        col_list = col_match.group(1)
        for col_expr in col_list.split(","):
            col_expr = col_expr.strip()
            # Handle "table.column" or "table.column AS alias" or just "column"
            # Strip AS alias
            col_expr = re.sub(r'\s+AS\s+\w+', '', col_expr, flags=re.IGNORECASE).strip()
            # Get the last part after dot (if qualified)
            if '.' in col_expr:
                col_expr = col_expr.rsplit('.', 1)[-1]
            # Strip backtick quoting
            col_name = col_expr.strip('`').strip()
            if not col_name or col_name == '*':
                continue
            col_name_lower = col_name.lower()
            if col_name_lower in _SQL_RESERVED or col_name_lower in seen_columns:
                continue
            # Skip if it looks like a function call
            if '(' in col_name:
                continue
            seen_columns.add(col_name_lower)
            symbols.append(DslSymbol(
                name=col_name_lower,
                kind=DslSymbolKind.COLUMN,
                dsl=region.dsl,
                host_file=region.host_file,
                host_line=region.host_start_line,
                host_col=region.host_start_col,
                link_hints=[
                    LinkHint(
                        strategy="orm_column",
                        target_pattern=col_name_lower,
                        target_kind="variable",
                    ),
                ],
            ))

    return symbols


# ---------------------------------------------------------------------------
# ORM Link Resolution
# ---------------------------------------------------------------------------

def _singularize(name: str) -> str:
    """Naive singularization for table-to-class mapping."""
    if name.endswith("ies"):
        return name[:-3] + "y"
    # "sses"/"xes"/"zes" → strip only the trailing "s"
    if name.endswith("sses") or name.endswith("xes") or name.endswith("zes"):
        return name[:-1]
    # "ses" (e.g. "buses") → strip "es"
    if name.endswith("ses"):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def _to_pascal_case(name: str) -> str:
    """Convert snake_case or plain name to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_"))


# Regex to find class definitions: "class ClassName(..."
_CLASS_DEF_RE = re.compile(r'^class\s+(\w+)\s*[\(:]', re.MULTILINE)
# Regex to find __tablename__ assignments
_TABLENAME_RE = re.compile(r'__tablename__\s*=\s*["\'](\w+)["\']')


def _find_classes_in_file(file_path: str) -> list[tuple[str, int, str]]:
    """Return list of (class_name, line_number, file_path) for all classes in file."""
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    results = []
    for m in _CLASS_DEF_RE.finditer(source):
        class_name = m.group(1)
        line = source[:m.start()].count('\n') + 1
        results.append((class_name, line, file_path))
    return results


def _find_tablename_mapping(file_path: str) -> dict[str, tuple[str, int]]:
    """Return mapping of tablename -> (class_name, line) for __tablename__ in file."""
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    mapping: dict[str, tuple[str, int]] = {}

    # Find all class defs and scan their bodies for __tablename__
    lines = source.split('\n')
    current_class: str | None = None
    for i, line in enumerate(lines, start=1):
        class_match = re.match(r'^class\s+(\w+)\s*[\(:]', line)
        if class_match:
            current_class = class_match.group(1)
        if current_class:
            tn_match = _TABLENAME_RE.search(line)
            if tn_match:
                tablename = tn_match.group(1)
                mapping[tablename] = (current_class, i)

    return mapping


def resolve_orm_links(
    dsl_symbols: list[DslSymbol],
    project_root: str,
    orm: str = "sqlalchemy",
) -> list[DslLink]:
    """Resolve DSL symbols to ORM model definitions.

    For table symbols:
    - Singularize + PascalCase the table name
    - Search for class definitions matching that name
    - Also check __tablename__ assignments for exact matches

    For column symbols:
    - Find the resolved table class, then look for attributes with the column name

    Args:
        dsl_symbols: Symbols to resolve.
        project_root: Project root directory for searching.
        orm: ORM framework ("sqlalchemy" or "django").

    Returns:
        List of DslLink objects.
    """
    root = Path(project_root)

    # Collect all Python files in project
    py_files = list(root.rglob("*.py"))

    # Build index: class_name -> (file_path, line)
    class_index: dict[str, tuple[str, int]] = {}
    # Build tablename index: tablename -> (class_name, file_path, line)
    tablename_index: dict[str, tuple[str, str, int]] = {}

    for py_file in py_files:
        for class_name, line, fpath in _find_classes_in_file(str(py_file)):
            if class_name not in class_index:
                class_index[class_name] = (fpath, line)

        for tablename, (class_name, line) in _find_tablename_mapping(str(py_file)).items():
            if tablename not in tablename_index:
                tablename_index[tablename] = (class_name, str(py_file), line)

    links: list[DslLink] = []

    for symbol in dsl_symbols:
        if symbol.kind != DslSymbolKind.TABLE:
            continue

        for hint in symbol.link_hints:
            if hint.strategy != "orm_model":
                continue

            target_class = hint.target_pattern
            resolved_file: str | None = None
            resolved_line: int = 0
            resolved_qname: str | None = None
            confidence: float = 0.0

            # 1) Try __tablename__ exact match (highest confidence)
            if symbol.name in tablename_index:
                found_class, found_file, found_line = tablename_index[symbol.name]
                resolved_qname = f"{Path(found_file).stem}.{found_class}"
                resolved_file = found_file
                resolved_line = found_line
                confidence = 0.95

            # 2) Try PascalCase class name match
            elif target_class in class_index:
                found_file, found_line = class_index[target_class]
                resolved_qname = f"{Path(found_file).stem}.{target_class}"
                resolved_file = found_file
                resolved_line = found_line
                confidence = 0.8

            if resolved_qname and resolved_file:
                links.append(DslLink(
                    dsl_symbol=symbol,
                    target_qualified_name=resolved_qname,
                    target_file=resolved_file,
                    target_line=resolved_line,
                    strategy=hint.strategy,
                    confidence=confidence,
                ))
                break  # Only first matching hint

    return links


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_file(
    file_path: str,
    source: str | None = None,
    project_root: str | None = None,
    orm: str = "sqlalchemy",
) -> tuple[list[DslSymbol], list[DslLink]]:
    """Analyze a file for embedded DSL symbols and resolve links.

    Args:
        file_path: Path to the source file.
        source: Source text (read from file if None).
        project_root: Project root for link resolution.
        orm: ORM framework for link resolution.

    Returns:
        Tuple of (symbols, links).
    """
    regions = detect_dsl_regions(file_path, source=source)

    all_symbols: list[DslSymbol] = []
    for region in regions:
        if region.dsl == DslKind.SQL:
            all_symbols.extend(extract_sql_symbols(region))

    links: list[DslLink] = []
    if project_root and all_symbols:
        links = resolve_orm_links(all_symbols, project_root, orm=orm)

    return all_symbols, links


def format_symbols(
    symbols: list[DslSymbol],
    links: list[DslLink] | None = None,
    json_output: bool = False,
) -> str:
    """Format DSL symbols for display.

    Args:
        symbols: DSL symbols to format.
        links: Optional resolved links to include.
        json_output: If True, return JSON; otherwise human-readable text.

    Returns:
        Formatted string output.
    """
    if json_output:
        items = []
        for sym in symbols:
            item: dict = {
                "name": sym.name,
                "kind": sym.kind.value,
                "dsl": sym.dsl.value,
                "host_file": sym.host_file,
                "host_line": sym.host_line,
                "host_col": sym.host_col,
            }
            if sym.link_hints:
                item["link_hints"] = [
                    {
                        "strategy": h.strategy,
                        "target_pattern": h.target_pattern,
                        "target_kind": h.target_kind,
                        "module_hint": h.module_hint,
                    }
                    for h in sym.link_hints
                ]
            items.append(item)
        # Attach resolved links
        if links:
            link_map: dict[str, list[dict]] = {}
            for lnk in links:
                key = f"{lnk.dsl_symbol.host_file}:{lnk.dsl_symbol.name}"
                link_map.setdefault(key, []).append({
                    "target_qualified_name": lnk.target_qualified_name,
                    "target_file": lnk.target_file,
                    "target_line": lnk.target_line,
                    "strategy": lnk.strategy,
                    "confidence": lnk.confidence,
                })
            for item in items:
                key = f"{item['host_file']}:{item['name']}"
                if key in link_map:
                    item["links"] = link_map[key]
        return json.dumps(items, indent=2)

    # Human-readable text
    if not symbols:
        return ""

    lines: list[str] = []
    # Build link lookup
    link_by_name: dict[str, DslLink] = {}
    if links:
        for lnk in links:
            link_by_name[lnk.dsl_symbol.name] = lnk

    for sym in symbols:
        prefix = f"{sym.host_file}:{sym.host_line}:{sym.host_col}"
        kind_str = sym.kind.value
        dsl_str = sym.dsl.value
        line = f"{prefix}  [{dsl_str}:{kind_str}]  {sym.name}"
        if sym.name in link_by_name:
            lnk = link_by_name[sym.name]
            line += f"  -> {lnk.target_qualified_name} ({lnk.target_file}:{lnk.target_line})"
        lines.append(line)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Find inside DSL regions
# ---------------------------------------------------------------------------

@dataclass
class DslMatch:
    """A pattern match inside a DSL region."""
    matched_text: str
    host_file: str
    host_line: int
    host_col: int
    dsl: DslKind
    captures: dict[str, str] = field(default_factory=dict)


def _compile_dsl_find_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a find pattern for DSL regions.

    Supports $METAVAR placeholders that match identifiers or expressions.
    Whitespace in the pattern matches any whitespace (including newlines).
    """
    parts = re.split(r'(\$\.\.\.?\w+|\$\w+)', pattern)
    regex_parts: list[str] = []
    group_names: list[str] = []
    for part in parts:
        if part.startswith("$..."):
            name = part[4:]
            group_names.append(name)
            regex_parts.append(f'(?P<{name}>.+?)')
        elif part.startswith("$"):
            name = part[1:]
            group_names.append(name)
            regex_parts.append(f'(?P<{name}>\\w+(?:\\.\\w+)*(?:\\s*,\\s*\\w+(?:\\.\\w+)*)*)')
        else:
            # Replace runs of whitespace with \s+ so patterns match across newlines
            escaped = re.escape(part)
            escaped = re.sub(r'(\\ )+', r'\\s+', escaped)
            regex_parts.append(escaped)
    return re.compile(''.join(regex_parts), re.IGNORECASE | re.DOTALL)


def find_in_dsl(
    pattern: str,
    file_path: str,
    source: str | None = None,
    dsl_type: str = "sql",
) -> list[DslMatch]:
    """Find pattern matches inside embedded DSL regions.

    Args:
        pattern: Pattern string with optional $METAVAR placeholders.
        file_path: Path to the source file.
        source: Source text (read from file if None).
        dsl_type: DSL type to search in ("sql", "css", "html").

    Returns:
        List of DslMatch objects.
    """
    regions = detect_dsl_regions(file_path, source=source)
    compiled = _compile_dsl_find_pattern(pattern)
    matches: list[DslMatch] = []

    for region in regions:
        if region.dsl.value != dsl_type:
            continue
        for m in compiled.finditer(region.content):
            match_offset = m.start()
            lines_before = region.content[:match_offset].count('\n')
            match_line = region.host_start_line + lines_before

            # Compute column: find last newline before match in region content
            last_nl = region.content.rfind('\n', 0, match_offset)
            if last_nl == -1:
                match_col = region.host_start_col + match_offset
            else:
                match_col = match_offset - last_nl - 1

            captures = {
                k: v for k, v in m.groupdict().items() if v is not None
            }
            matches.append(DslMatch(
                matched_text=m.group(0),
                host_file=file_path,
                host_line=match_line,
                host_col=match_col,
                dsl=region.dsl,
                captures=captures,
            ))

    return matches


# ---------------------------------------------------------------------------
# Regex named group navigation
# ---------------------------------------------------------------------------

_NAMED_GROUP_RE = re.compile(r'\(\?P<(\w+)>')
_GROUP_CALL_RE = re.compile(r'\.group\(\s*["\'](\w+)["\']\s*\)')


@dataclass
class RegexNamedGroup:
    """A named group in a regex pattern and its usage sites."""
    name: str
    definition_file: str
    definition_line: int
    definition_col: int
    usages: list[tuple[str, int, int]] = field(default_factory=list)  # (file, line, col)


def extract_regex_named_groups(
    file_path: str,
    source: str | None = None,
) -> list[RegexNamedGroup]:
    """Extract regex named groups and their .group() call sites from a file.

    Finds ``(?P<name>...)`` definitions and ``.group("name")`` call sites,
    linking them by group name.

    Args:
        file_path: Path to the source file.
        source: Source text (read from file if None).

    Returns:
        List of RegexNamedGroup objects.
    """
    if source is None:
        try:
            source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

    # Find all named group definitions
    groups: dict[str, RegexNamedGroup] = {}
    for m in _NAMED_GROUP_RE.finditer(source):
        name = m.group(1)
        line = source[:m.start()].count('\n') + 1
        last_nl = source.rfind('\n', 0, m.start())
        col = m.start() - last_nl - 1 if last_nl >= 0 else m.start()
        if name not in groups:
            groups[name] = RegexNamedGroup(
                name=name,
                definition_file=file_path,
                definition_line=line,
                definition_col=col,
            )

    # Find all .group("name") call sites
    for m in _GROUP_CALL_RE.finditer(source):
        name = m.group(1)
        line = source[:m.start()].count('\n') + 1
        last_nl = source.rfind('\n', 0, m.start())
        col = m.start() - last_nl - 1 if last_nl >= 0 else m.start()
        if name in groups:
            groups[name].usages.append((file_path, line, col))

    return list(groups.values())


def find_regex_group_references(
    group_name: str,
    project_root: str,
) -> list[tuple[str, int, int]]:
    """Find all .group("name") call sites for a named regex group across a project.

    Args:
        group_name: The regex group name to find references for.
        project_root: Project root directory.

    Returns:
        List of (file_path, line, col) tuples.
    """
    root = Path(project_root)
    refs: list[tuple[str, int, int]] = []
    pattern = re.compile(
        rf'\.group\(\s*["\']' + re.escape(group_name) + r'["\']\s*\)'
    )
    for py_file in root.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in pattern.finditer(source):
            line = source[:m.start()].count('\n') + 1
            last_nl = source.rfind('\n', 0, m.start())
            col = m.start() - last_nl - 1 if last_nl >= 0 else m.start()
            refs.append((str(py_file), line, col))
    return refs


# ---------------------------------------------------------------------------
# Impact DSL integration
# ---------------------------------------------------------------------------

def find_dsl_impact(
    changed_symbols: list[str],
    project_root: str,
    orm: str = "sqlalchemy",
) -> list[tuple[str, str, str]]:
    """Find DSL regions impacted by changes to host-language symbols.

    When an ORM model class changes, find all SQL queries that reference
    the corresponding table.

    Args:
        changed_symbols: List of changed symbol selectors (e.g. "models.py::User").
        project_root: Project root directory.
        orm: ORM framework.

    Returns:
        List of (dsl_file, dsl_line, reason) tuples describing impacted DSL regions.
    """
    root = Path(project_root)
    impacted: list[tuple[str, str, str]] = []

    # Extract class names from changed selectors
    changed_classes: set[str] = set()
    for sel in changed_symbols:
        parts = sel.split("::")
        if len(parts) >= 2:
            changed_classes.add(parts[-1].split(".")[-1])

    if not changed_classes:
        return impacted

    # Build reverse mapping: class -> possible table names
    class_to_tables: dict[str, set[str]] = {}
    for cls in changed_classes:
        # Convention: PascalCase -> snake_case plural
        snake = re.sub(r'(?<!^)(?=[A-Z])', '_', cls).lower()
        tables = {snake, snake + "s", snake + "es"}
        # Also check __tablename__ in project files
        for py_file in root.rglob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for tn_name, (tn_class, _line) in _find_tablename_mapping(str(py_file)).items():
                if tn_class == cls:
                    tables.add(tn_name)
        class_to_tables[cls] = tables

    all_tables = set()
    for tables in class_to_tables.values():
        all_tables |= tables

    if not all_tables:
        return impacted

    # Scan all files for SQL regions referencing those tables
    for py_file in root.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        regions = detect_dsl_regions(str(py_file), source=source)
        for region in regions:
            if region.dsl != DslKind.SQL:
                continue
            for sym in extract_sql_symbols(region):
                if sym.kind == DslSymbolKind.TABLE and sym.name in all_tables:
                    # Find which class this table belongs to
                    for cls, tables in class_to_tables.items():
                        if sym.name in tables:
                            impacted.append((
                                str(py_file),
                                str(region.host_start_line),
                                f"SQL query references table '{sym.name}' "
                                f"linked to changed class '{cls}'",
                            ))
                            break

    return impacted
