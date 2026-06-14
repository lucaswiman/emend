"""DSL support for embedded languages.

Detects embedded DSL regions (SQL, CSS, HTML, Jinja2, GraphQL) inside
host-language string literals and standalone DSL files, extracts symbols,
and resolves cross-language links to host-language definitions.
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

# SQL keyword pattern used to identify SQL content in string literals.
# Applied to already-extracted string content (not raw source), so regex is appropriate.
_SQL_KEYWORD_RE = re.compile(
    r'\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE\s+TABLE|WITH\s+\w+|REPLACE\s+INTO)\b',
    re.IGNORECASE,
)

# Magic comment pattern — applied to comment text (not raw source structure).
# Only used as a fallback when tree-sitter comment collection is unavailable.
_MAGIC_COMMENT_RE = re.compile(r'#\s*language\s*=\s*(\w+)', re.IGNORECASE)

# Jinja2 template patterns — detect {{ var }}, {% tag %}, {# comment #}
# Applied to already-extracted string content (not raw source).
_JINJA_EXPR_RE = re.compile(r'\{\{.*?\}\}', re.DOTALL)
_JINJA_TAG_RE = re.compile(r'\{%.*?%\}', re.DOTALL)
_JINJA_COMMENT_RE = re.compile(r'\{#.*?#\}', re.DOTALL)
_JINJA_KEYWORD_RE = re.compile(
    r'\{[%{].*?\b(extends|block|macro|include|import|from|for|if|set|call|filter)\b.*?[%}]\}',
    re.IGNORECASE | re.DOTALL,
)
# Jinja2 file extensions
_JINJA_EXTENSIONS = frozenset({'.html', '.jinja', '.jinja2', '.j2'})

# GraphQL keyword pattern
_GRAPHQL_KEYWORD_RE = re.compile(
    r'\b(type|query|mutation|subscription|schema|input|enum|interface|union|fragment|extend)\s+\w+',
    re.IGNORECASE,
)
# GraphQL file extensions
_GRAPHQL_EXTENSIONS = frozenset({'.graphql', '.gql'})

# render_template call pattern for Jinja2 context resolution
_RENDER_TEMPLATE_RE = re.compile(
    r'render_template\s*\(\s*["\']([^"\']+)["\']\s*(?:,\s*(.+?))?\s*\)',
    re.DOTALL,
)
# Keyword argument pattern for template context
_KWARG_RE = re.compile(r'(\w+)\s*=')

# GraphQL type/field patterns
_GQL_TYPE_DEF_RE = re.compile(
    r'\b(?:type|input|enum|interface|union)\s+(\w+)',
    re.IGNORECASE,
)
_GQL_FIELD_DEF_RE = re.compile(
    r'^\s+(\w+)\s*(?:\([^)]*\))?\s*:\s*',
    re.MULTILINE,
)
_GQL_QUERY_DEF_RE = re.compile(
    r'\b(?:query|mutation|subscription)\s+(\w+)',
    re.IGNORECASE,
)


def _get_string_literals(
    source: str, file_path: str
) -> list[tuple[int, int, int, int, int, int, str]]:
    """Extract all string literals from source using tree-sitter.

    Returns a list of
    ``(start_byte, end_byte, start_line, start_col, end_line, end_col, content)``
    tuples where *content* is the unquoted inner text.

    Falls back to an empty list if ``emend_core`` is unavailable.
    """
    try:
        from emend import emend_core as _rust  # type: ignore[attr-defined]
        ext = Path(file_path).suffix.lstrip(".") or "py"
        return _rust.collect_string_literals(source, ext)
    except Exception:
        return []


def _has_magic_comment(source: str, file_path: str, keyword: str) -> bool:
    """Return True if source contains a ``# language=<keyword>`` comment.

    Uses tree-sitter comment node traversal when possible, falling back to a
    simple regex over the raw source only if the Rust extension is unavailable.
    The regex fallback operates on comment text, not source structure.
    """
    try:
        from emend import emend_core as _rust  # type: ignore[attr-defined]
        ext = Path(file_path).suffix.lstrip(".") or "py"
        comments = _rust.collect_comments(source, ext)
        keyword_lower = keyword.lower()
        for _line, _col, text in comments:
            # text is e.g. "# language=sql" or "# language = sql"
            m = _MAGIC_COMMENT_RE.search(text)
            if m and m.group(1).lower() == keyword_lower:
                return True
        return False
    except Exception:
        # Fallback: regex on raw source (acceptable for comment text detection)
        return bool(re.search(
            r'#\s*language\s*=\s*' + re.escape(keyword),
            source,
            re.IGNORECASE,
        ))


def _make_region_from_ts(
    dsl: DslKind,
    content: str,
    file_path: str,
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
    trigger: str,
) -> DslRegion:
    """Build a DslRegion from tree-sitter position data."""
    return DslRegion(
        dsl=dsl,
        content=content,
        host_file=file_path,
        host_start_line=start_line,
        host_start_col=max(0, start_col),
        host_end_line=end_line,
        host_end_col=max(0, end_col),
        trigger=trigger,
    )


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

    regions: list[DslRegion] = []

    # Check file extension for standalone DSL files
    ext = Path(file_path).suffix.lower()

    if ext in _JINJA_EXTENSIONS and (dsls is None or DslKind.JINJA in dsls):
        # Entire file is a Jinja2 template
        end_line = source.count('\n') + 1
        end_col = len(source) - source.rfind('\n') - 1 if '\n' in source else len(source)
        regions.append(DslRegion(
            dsl=DslKind.JINJA,
            content=source,
            host_file=file_path,
            host_start_line=1,
            host_start_col=0,
            host_end_line=end_line,
            host_end_col=max(0, end_col),
            trigger="file_extension",
        ))
        return regions

    if ext in _GRAPHQL_EXTENSIONS and (dsls is None or DslKind.GRAPHQL in dsls):
        # Entire file is GraphQL
        end_line = source.count('\n') + 1
        end_col = len(source) - source.rfind('\n') - 1 if '\n' in source else len(source)
        regions.append(DslRegion(
            dsl=DslKind.GRAPHQL,
            content=source,
            host_file=file_path,
            host_start_line=1,
            host_start_col=0,
            host_end_line=end_line,
            host_end_col=max(0, end_col),
            trigger="file_extension",
        ))
        return regions

    # Detect DSL regions inside host-language source files
    if dsls is None or DslKind.SQL in dsls:
        regions.extend(_detect_sql_regions(file_path, source))

    if dsls is None or DslKind.JINJA in dsls:
        regions.extend(_detect_jinja_regions(file_path, source))

    if dsls is None or DslKind.GRAPHQL in dsls:
        regions.extend(_detect_graphql_regions(file_path, source))

    return regions


def _detect_sql_regions(file_path: str, source: str) -> list[DslRegion]:
    """Detect SQL regions in Python source code.

    Looks for:
    1. String literals containing SQL keywords (via tree-sitter string node traversal)
    2. Magic comments: # language=sql (via tree-sitter comment node traversal)
    """
    regions: list[DslRegion] = []
    seen_contents: set[str] = set()

    # Check for magic comment presence to determine trigger type
    trigger = "magic_comment" if _has_magic_comment(source, file_path, "sql") else "literal"

    for _sb, _eb, start_line, start_col, end_line, end_col, content in _get_string_literals(source, file_path):
        stripped = content.strip()
        if not stripped or stripped in seen_contents:
            continue
        if not _SQL_KEYWORD_RE.search(stripped):
            continue
        seen_contents.add(stripped)
        regions.append(_make_region_from_ts(
            DslKind.SQL, stripped, file_path,
            start_line, start_col, end_line, end_col, trigger,
        ))

    return regions


def _detect_jinja_regions(file_path: str, source: str) -> list[DslRegion]:
    """Detect Jinja2 template regions embedded in Python string literals.

    Looks for strings containing Jinja2 syntax: {{ expr }}, {% tag %}, {# comment #}.
    Uses tree-sitter string node traversal for reliable extraction.
    """
    regions: list[DslRegion] = []
    seen_contents: set[str] = set()

    # Check for magic comment: # language=jinja or # language=jinja2
    magic_jinja = (
        _has_magic_comment(source, file_path, "jinja")
        or _has_magic_comment(source, file_path, "jinja2")
    )
    trigger = "magic_comment" if magic_jinja else "literal"

    for _sb, _eb, start_line, start_col, end_line, end_col, content in _get_string_literals(source, file_path):
        stripped = content.strip()
        if not stripped or stripped in seen_contents:
            continue
        if not (_JINJA_EXPR_RE.search(stripped) or _JINJA_TAG_RE.search(stripped)):
            continue
        seen_contents.add(stripped)
        regions.append(_make_region_from_ts(
            DslKind.JINJA, stripped, file_path,
            start_line, start_col, end_line, end_col, trigger,
        ))

    return regions


def _detect_graphql_regions(file_path: str, source: str) -> list[DslRegion]:
    """Detect GraphQL regions embedded in Python/TypeScript string literals.

    Looks for strings containing GraphQL keywords (type, query, mutation, etc.).
    Uses tree-sitter string node traversal for reliable extraction.
    """
    regions: list[DslRegion] = []
    seen_contents: set[str] = set()

    magic_gql = _has_magic_comment(source, file_path, "graphql")
    trigger = "magic_comment" if magic_gql else "literal"

    for _sb, _eb, start_line, start_col, end_line, end_col, content in _get_string_literals(source, file_path):
        stripped = content.strip()
        if not stripped or stripped in seen_contents:
            continue
        if not _GRAPHQL_KEYWORD_RE.search(stripped):
            continue
        seen_contents.add(stripped)
        regions.append(_make_region_from_ts(
            DslKind.GRAPHQL, stripped, file_path,
            start_line, start_col, end_line, end_col, trigger,
        ))

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
# Jinja2 Symbol Extraction
# ---------------------------------------------------------------------------

# Jinja2 expression variable: {{ user.name }} -> "user"
_JINJA_VAR_RE = re.compile(r'\{\{\s*(\w+)(?:\.\w+)*\s*(?:\|[^}]*)?\}\}')
# Jinja2 block tag: {% block content %} -> "content"
_JINJA_BLOCK_RE = re.compile(r'\{%[-\s]*block\s+(\w+)')
# Jinja2 extends tag: {% extends "base.html" %} -> "base.html"
_JINJA_EXTENDS_RE = re.compile(r'\{%[-\s]*extends\s+["\']([^"\']+)["\']')
# Jinja2 macro: {% macro render_field(field) %} -> "render_field"
_JINJA_MACRO_RE = re.compile(r'\{%[-\s]*macro\s+(\w+)')
# Jinja2 include: {% include "header.html" %} -> "header.html"
_JINJA_INCLUDE_RE = re.compile(r'\{%[-\s]*include\s+["\']([^"\']+)["\']')
# Jinja2 for loop variable: {% for item in items %} -> "items" (iterable)
_JINJA_FOR_RE = re.compile(r'\{%[-\s]*for\s+\w+\s+in\s+(\w+)')

# Jinja2 built-in variables and keywords to exclude
_JINJA_BUILTINS = frozenset({
    "true", "false", "none", "loop", "range", "lipsum", "dict",
    "cycler", "joiner", "namespace", "self", "caller",
})


def extract_jinja_symbols(region: DslRegion) -> list[DslSymbol]:
    """Extract template variables, blocks, macros, and inheritance from a Jinja2 region.

    Parses Jinja2 templates using regex to identify:
    - Template variables ({{ var }}, {% for x in items %})
    - Block definitions ({% block name %})
    - Macro definitions ({% macro name %})
    - Template inheritance ({% extends "base.html" %})
    - Template includes ({% include "header.html" %})

    Args:
        region: A DslRegion containing Jinja2 content.

    Returns:
        List of DslSymbol objects with appropriate LinkHints.
    """
    symbols: list[DslSymbol] = []
    seen_vars: set[str] = set()
    seen_blocks: set[str] = set()
    content = region.content

    # Extract template variables from {{ var }} and {% for x in var %}
    for pattern in (_JINJA_VAR_RE, _JINJA_FOR_RE):
        for m in pattern.finditer(content):
            var_name = m.group(1)
            if var_name.lower() in _JINJA_BUILTINS or var_name in seen_vars:
                continue
            seen_vars.add(var_name)

            line_offset = content[:m.start()].count('\n')
            symbols.append(DslSymbol(
                name=var_name,
                kind=DslSymbolKind.TEMPLATE_VAR,
                dsl=region.dsl,
                host_file=region.host_file,
                host_line=region.host_start_line + line_offset,
                host_col=region.host_start_col,
                link_hints=[
                    LinkHint(
                        strategy="template_var",
                        target_pattern=var_name,
                        target_kind="variable",
                    ),
                ],
            ))

    # Extract block definitions
    for m in _JINJA_BLOCK_RE.finditer(content):
        block_name = m.group(1)
        if block_name in seen_blocks:
            continue
        seen_blocks.add(block_name)

        line_offset = content[:m.start()].count('\n')
        # Block inheritance hint: look for same block in parent template
        hints: list[LinkHint] = [
            LinkHint(
                strategy="template_block",
                target_pattern=block_name,
                target_kind="block",
            ),
        ]
        symbols.append(DslSymbol(
            name=block_name,
            kind=DslSymbolKind.TEMPLATE_VAR,
            dsl=region.dsl,
            host_file=region.host_file,
            host_line=region.host_start_line + line_offset,
            host_col=region.host_start_col,
            link_hints=hints,
        ))

    # Extract macro definitions
    for m in _JINJA_MACRO_RE.finditer(content):
        macro_name = m.group(1)
        line_offset = content[:m.start()].count('\n')
        symbols.append(DslSymbol(
            name=macro_name,
            kind=DslSymbolKind.TEMPLATE_VAR,
            dsl=region.dsl,
            host_file=region.host_file,
            host_line=region.host_start_line + line_offset,
            host_col=region.host_start_col,
            link_hints=[],
        ))

    return symbols


# ---------------------------------------------------------------------------
# GraphQL Symbol Extraction
# ---------------------------------------------------------------------------

# GraphQL reserved/built-in type names
_GQL_BUILTINS = frozenset({
    "string", "int", "float", "boolean", "id",
    "query", "mutation", "subscription", "schema",
})


def extract_graphql_symbols(region: DslRegion) -> list[DslSymbol]:
    """Extract type and field definitions from a GraphQL region.

    Parses GraphQL schemas/queries using regex to identify:
    - Type definitions (type User, input CreateUserInput, enum Role)
    - Field definitions (email: String!, posts: [Post!]!)
    - Query/mutation/subscription definitions

    Each type gets a graphql_type LinkHint; each field gets a graphql_field hint.

    Args:
        region: A DslRegion containing GraphQL content.

    Returns:
        List of DslSymbol objects with appropriate LinkHints.
    """
    symbols: list[DslSymbol] = []
    seen_types: set[str] = set()
    seen_fields: set[str] = set()
    content = region.content

    # Extract type definitions
    for m in _GQL_TYPE_DEF_RE.finditer(content):
        type_name = m.group(1)
        if type_name.lower() in _GQL_BUILTINS or type_name in seen_types:
            continue
        seen_types.add(type_name)

        line_offset = content[:m.start()].count('\n')
        # Link hint: look for resolver class or model class with matching name
        resolver_name = f"{type_name}Resolver"
        symbols.append(DslSymbol(
            name=type_name,
            kind=DslSymbolKind.GRAPHQL_TYPE,
            dsl=region.dsl,
            host_file=region.host_file,
            host_line=region.host_start_line + line_offset,
            host_col=region.host_start_col,
            link_hints=[
                LinkHint(
                    strategy="graphql_type",
                    target_pattern=type_name,
                    target_kind="class",
                ),
                LinkHint(
                    strategy="graphql_type",
                    target_pattern=resolver_name,
                    target_kind="class",
                ),
            ],
        ))

    # Extract field definitions (only inside type bodies)
    # Simple heuristic: lines starting with whitespace + identifier + colon
    current_type: str | None = None
    for line_offset, line in enumerate(content.split('\n')):
        type_match = _GQL_TYPE_DEF_RE.search(line)
        if type_match:
            current_type = type_match.group(1)
            continue
        if line.strip() == '}':
            current_type = None
            continue
        if current_type:
            field_match = _GQL_FIELD_DEF_RE.match(line)
            if field_match:
                field_name = field_match.group(1)
                # Note: do NOT filter field names against _GQL_BUILTINS — that
                # set is for scalar *type* names (String, Int, ID…) and must
                # not suppress perfectly-valid field names like "id".
                field_key = f"{current_type}.{field_name}"
                if field_key in seen_fields:
                    continue
                seen_fields.add(field_key)
                symbols.append(DslSymbol(
                    name=field_name,
                    kind=DslSymbolKind.GRAPHQL_FIELD,
                    dsl=region.dsl,
                    host_file=region.host_file,
                    host_line=region.host_start_line + line_offset,
                    host_col=region.host_start_col,
                    link_hints=[
                        LinkHint(
                            strategy="graphql_field",
                            target_pattern=field_name,
                            target_kind="function",
                            module_hint=current_type,
                        ),
                    ],
                ))

    # Extract query/mutation/subscription operation names
    for m in _GQL_QUERY_DEF_RE.finditer(content):
        op_name = m.group(1)
        if op_name in seen_types:
            continue
        seen_types.add(op_name)
        line_offset = content[:m.start()].count('\n')
        symbols.append(DslSymbol(
            name=op_name,
            kind=DslSymbolKind.GRAPHQL_TYPE,
            dsl=region.dsl,
            host_file=region.host_file,
            host_line=region.host_start_line + line_offset,
            host_col=region.host_start_col,
            link_hints=[
                LinkHint(
                    strategy="graphql_type",
                    target_pattern=op_name,
                    target_kind="function",
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
    # "sses"/"xes"/"zes" → plural suffix is "es"; strip both chars
    if name.endswith("sses") or name.endswith("xes") or name.endswith("zes"):
        return name[:-2]
    # "ses" (e.g. "buses") → strip "es"
    if name.endswith("ses"):
        return name[:-2]
    # "shes" (e.g. "dishes", "crashes") → strip "es"
    if name.endswith("shes"):
        return name[:-2]
    # "ches" with a consonant before (e.g. "watches", "batches") → strip "es"
    # Vowel + ches (e.g. "caches", "niches") falls through to the generic -s rule
    if name.endswith("ches") and len(name) > 4 and name[-5] not in "aeiou":
        return name[:-2]
    # Words ending in "us" or "is" are almost always already singular in English
    # (e.g. "status", "nexus", "corpus", "analysis") – do not strip the "s".
    if name.endswith("s") and not name.endswith("ss") and not name.endswith("us") and not name.endswith("is"):
        return name[:-1]
    return name


def _to_pascal_case(name: str) -> str:
    """Convert snake_case or plain name to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_"))



def _index_classes_and_tablenames(
    file_path: str,
) -> tuple[list[tuple[str, int]], dict[str, tuple[str, int]]]:
    """Index classes and ``__tablename__`` assignments in a Python file via tree-sitter.

    Uses ``emend_core.collect_symbols_from_str()`` to enumerate class definitions
    and their child variable assignments, then matches each ``__tablename__``
    variable to a string literal on the same line range to extract the table
    name. This replaces the previous regex-based scanning.

    Returns:
        ``(classes, tablename_mapping)`` where:

        * ``classes`` is a list of ``(class_name, line)`` tuples in source order.
        * ``tablename_mapping`` maps ``tablename -> (class_name, line)`` for
          string-literal ``__tablename__`` assignments inside class bodies.
    """
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        from emend import emend_core as _rust  # type: ignore[attr-defined]
        symbols = _rust.collect_symbols_from_str(source, ext="py")
        string_literals = _rust.collect_string_literals(source, "py")
    except Exception:
        return [], {}

    # Index string literals by start line so we can look up RHS values on a
    # __tablename__ variable's line. The original regex only matched
    # single-line quoted-identifier assignments; we mirror that semantics.
    strings_by_line: dict[int, list[tuple[int, str]]] = {}
    for _sb, _eb, start_line, start_col, _el, _ec, content in string_literals:
        strings_by_line.setdefault(start_line, []).append((start_col, content))

    classes: list[tuple[str, int]] = []
    tablename_mapping: dict[str, tuple[str, int]] = {}

    for sym in symbols:
        if sym.get("kind") != "class":
            continue
        class_name = sym["name"]
        classes.append((class_name, sym["line"]))

        for child in sym.get("children", []):
            if child.get("kind") != "variable" or child.get("name") != "__tablename__":
                continue
            var_line = child["line"]
            var_col = child.get("col_offset", 0)
            # Find a string literal on the assignment's start line whose
            # column is to the right of the variable name (i.e. the RHS).
            # `\w+` in the original regex required an identifier-shaped value.
            for col, content in strings_by_line.get(var_line, []):
                if col >= var_col and content and content.isidentifier():
                    tablename_mapping.setdefault(content, (class_name, var_line))
                    break

    return classes, tablename_mapping


def _find_tablename_mapping(file_path: str) -> dict[str, tuple[str, int]]:
    """Return mapping of tablename -> (class_name, line) for __tablename__ in file.

    Thin wrapper over :func:`_index_classes_and_tablenames` kept for backward
    compatibility with callers that only need the tablename mapping.
    """
    _classes, mapping = _index_classes_and_tablenames(file_path)
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
        fpath = str(py_file)
        classes, tablename_mapping = _index_classes_and_tablenames(fpath)
        for class_name, line in classes:
            if class_name not in class_index:
                class_index[class_name] = (fpath, line)
        for tablename, (class_name, line) in tablename_mapping.items():
            if tablename not in tablename_index:
                tablename_index[tablename] = (class_name, fpath, line)

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


def resolve_jinja_links(
    dsl_symbols: list[DslSymbol],
    project_root: str,
    framework: str = "flask",
) -> list[DslLink]:
    """Resolve Jinja2 template symbols to Python view function context variables.

    For template variables:
    - Find render_template() calls that render the template file
    - Extract keyword arguments to identify context variable sources

    For blocks:
    - Find the parent template ({% extends %}) and locate the same block

    Args:
        dsl_symbols: Jinja2 symbols to resolve.
        project_root: Project root directory for searching.
        framework: Web framework ("flask" or "django").

    Returns:
        List of DslLink objects.
    """
    root = Path(project_root)
    links: list[DslLink] = []

    # Collect all Python files and scan for render_template calls
    py_files = list(root.rglob("*.py"))

    # Build index: template_name -> [(file_path, line, context_vars)]
    template_contexts: dict[str, list[tuple[str, int, set[str]]]] = {}
    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _RENDER_TEMPLATE_RE.finditer(source):
            tpl_name = m.group(1)
            line = source[:m.start()].count('\n') + 1
            # Extract keyword arguments from the call
            context_args = m.group(2) or ""
            context_vars = set(_KWARG_RE.findall(context_args))
            template_contexts.setdefault(tpl_name, []).append(
                (str(py_file), line, context_vars)
            )

    # Build block index: scan template files for matching block definitions
    template_dirs = list(root.rglob("*.html")) + list(root.rglob("*.jinja2")) + list(root.rglob("*.j2"))
    block_index: dict[str, list[tuple[str, int]]] = {}  # block_name -> [(file, line)]
    extends_map: dict[str, str] = {}  # child_file -> parent_template
    for tpl_file in template_dirs:
        try:
            tpl_source = tpl_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _JINJA_EXTENDS_RE.finditer(tpl_source):
            extends_map[str(tpl_file)] = m.group(1)
        for m in _JINJA_BLOCK_RE.finditer(tpl_source):
            block_name = m.group(1)
            line = tpl_source[:m.start()].count('\n') + 1
            block_index.setdefault(block_name, []).append((str(tpl_file), line))

    for symbol in dsl_symbols:
        for hint in symbol.link_hints:
            if hint.strategy == "template_var":
                # Try to find a render_template call that passes this variable
                # Match against the symbol's host file name
                tpl_basename = Path(symbol.host_file).name
                for tpl_name, contexts in template_contexts.items():
                    # Match if template name matches the file name or ends with it
                    if tpl_basename == tpl_name or tpl_name.endswith(tpl_basename):
                        for ctx_file, ctx_line, ctx_vars in contexts:
                            if symbol.name in ctx_vars:
                                links.append(DslLink(
                                    dsl_symbol=symbol,
                                    target_qualified_name=f"{Path(ctx_file).stem}.render_template",
                                    target_file=ctx_file,
                                    target_line=ctx_line,
                                    strategy="template_var",
                                    confidence=0.85,
                                ))
                                break
                    # Also check for partial path matches
                    elif tpl_name in str(symbol.host_file):
                        for ctx_file, ctx_line, ctx_vars in contexts:
                            if symbol.name in ctx_vars:
                                links.append(DslLink(
                                    dsl_symbol=symbol,
                                    target_qualified_name=f"{Path(ctx_file).stem}.render_template",
                                    target_file=ctx_file,
                                    target_line=ctx_line,
                                    strategy="template_var",
                                    confidence=0.7,
                                ))
                                break

            elif hint.strategy == "template_block":
                # Find matching blocks in other templates (parent or child)
                block_name = hint.target_pattern
                if block_name in block_index:
                    for blk_file, blk_line in block_index[block_name]:
                        # Skip self-reference
                        if blk_file == symbol.host_file and blk_line == symbol.host_line:
                            continue
                        # Prefer parent template blocks
                        parent_name = extends_map.get(symbol.host_file)
                        is_parent_block = parent_name is not None and (
                            Path(blk_file).name == parent_name
                            or blk_file.endswith(parent_name)
                        )
                        confidence = 0.9 if is_parent_block else 0.7
                        links.append(DslLink(
                            dsl_symbol=symbol,
                            target_qualified_name=f"{Path(blk_file).stem}.block.{block_name}",
                            target_file=blk_file,
                            target_line=blk_line,
                            strategy="template_block",
                            confidence=confidence,
                        ))

    return links


def resolve_graphql_links(
    dsl_symbols: list[DslSymbol],
    project_root: str,
) -> list[DslLink]:
    """Resolve GraphQL type and field symbols to resolver class/method definitions.

    For types:
    - Find classes with matching name or name + "Resolver"
    - Also look for @Resolver-decorated classes

    For fields:
    - Find methods on the resolved type's resolver class

    Args:
        dsl_symbols: GraphQL symbols to resolve.
        project_root: Project root directory.

    Returns:
        List of DslLink objects.
    """
    root = Path(project_root)
    links: list[DslLink] = []

    # Scan Python and TypeScript files for resolver classes and methods
    code_files = list(root.rglob("*.py")) + list(root.rglob("*.ts"))

    # Build class/function index
    class_index: dict[str, tuple[str, int]] = {}  # class_name -> (file, line)
    method_index: dict[str, list[tuple[str, int, str]]] = {}  # method_name -> [(file, line, class)]

    for code_file in code_files:
        try:
            source = code_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Index classes, methods, and standalone functions via tree-sitter.
        from emend import emend_core
        ext = code_file.suffix.lstrip('.') or 'py'
        rust_syms = emend_core.collect_symbols_from_str(source, ext=ext)

        def _index_symbols(syms: list, parent_class: str | None = None) -> None:
            for sym in syms:
                kind = sym.get("kind", "")
                name = sym.get("name", "")
                line = sym.get("line", 1)
                if kind == "class":
                    if name not in class_index:
                        class_index[name] = (str(code_file), line)
                    # Recurse into children with this class as parent
                    _index_symbols(sym.get("children", []), parent_class=name)
                elif kind in ("function", "async_function", "method", "async_method"):
                    if parent_class:
                        # Method on a class — add to method_index
                        method_index.setdefault(name, []).append(
                            (str(code_file), line, parent_class)
                        )
                    else:
                        # Standalone function — add to class_index
                        if name not in class_index:
                            class_index[name] = (str(code_file), line)
                    # Recurse for nested defs (no parent_class change for nesting)
                    _index_symbols(sym.get("children", []), parent_class=parent_class)

        _index_symbols(rust_syms)

    for symbol in dsl_symbols:
        if symbol.kind == DslSymbolKind.GRAPHQL_TYPE:
            for hint in symbol.link_hints:
                if hint.strategy != "graphql_type":
                    continue
                target = hint.target_pattern
                if target in class_index:
                    found_file, found_line = class_index[target]
                    # Higher confidence for "Resolver" suffix
                    confidence = 0.9 if target.endswith("Resolver") else 0.8
                    links.append(DslLink(
                        dsl_symbol=symbol,
                        target_qualified_name=f"{Path(found_file).stem}.{target}",
                        target_file=found_file,
                        target_line=found_line,
                        strategy="graphql_type",
                        confidence=confidence,
                    ))
                    break

        elif symbol.kind == DslSymbolKind.GRAPHQL_FIELD:
            field_name = symbol.name
            parent_type = None
            for hint in symbol.link_hints:
                if hint.module_hint:
                    parent_type = hint.module_hint
                    break

            if field_name in method_index:
                for meth_file, meth_line, meth_class in method_index[field_name]:
                    # Prefer methods on the resolver class for the parent type
                    confidence = 0.7
                    if parent_type:
                        if meth_class == parent_type or meth_class == f"{parent_type}Resolver":
                            confidence = 0.9
                    links.append(DslLink(
                        dsl_symbol=symbol,
                        target_qualified_name=f"{Path(meth_file).stem}.{meth_class}.{field_name}",
                        target_file=meth_file,
                        target_line=meth_line,
                        strategy="graphql_field",
                        confidence=confidence,
                    ))
                    if confidence >= 0.9:
                        break  # Found best match

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
        elif region.dsl == DslKind.JINJA:
            all_symbols.extend(extract_jinja_symbols(region))
        elif region.dsl == DslKind.GRAPHQL:
            all_symbols.extend(extract_graphql_symbols(region))

    links: list[DslLink] = []
    if project_root and all_symbols:
        sql_symbols = [s for s in all_symbols if s.dsl == DslKind.SQL]
        jinja_symbols = [s for s in all_symbols if s.dsl == DslKind.JINJA]
        gql_symbols = [s for s in all_symbols if s.dsl == DslKind.GRAPHQL]
        if sql_symbols:
            links.extend(resolve_orm_links(sql_symbols, project_root, orm=orm))
        if jinja_symbols:
            links.extend(resolve_jinja_links(jinja_symbols, project_root))
        if gql_symbols:
            links.extend(resolve_graphql_links(gql_symbols, project_root))

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
