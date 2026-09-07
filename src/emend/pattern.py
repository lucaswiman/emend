"""Pattern parsing with metavariables."""
from __future__ import annotations
from dataclasses import dataclass
from lark import Lark, Transformer, Token
import importlib.resources
import re as _re


@dataclass
class MetaVar:
    name: str
    ellipsis: bool = False
    type_constraint: str | None = None


@dataclass
class Pattern:
    raw: str
    metavars: list[MetaVar]


class PatternTransformer(Transformer):
    """Transform parse tree to Pattern with extracted metavars."""

    def __init__(self):
        super().__init__()
        self.metavars = []

    def start(self, items):
        return items[0]

    def pattern(self, items):
        # Items are code chunks and metavars - we just need to extract metavars
        return None

    def type_constraint(self, items):
        """Handle the type_constraint rule wrapping either terminal."""
        return str(items[0])

    def metavar(self, items):
        # Separate type constraint strings from other items
        type_constraint_str = None
        other_items = []

        for item in items:
            if isinstance(item, Token):
                if item.type in ("SIMPLE_TYPE_CONSTRAINT", "ORACLE_TYPE_CONSTRAINT"):
                    type_constraint_str = str(item)
                # Skip DOLLAR, UNDERSCORE, etc.
            elif isinstance(item, str) and item.startswith(":"):
                # Result of type_constraint rule
                type_constraint_str = item
            else:
                other_items.append(item)

        if not other_items:
            # This is $_ (anonymous metavar)
            metavar = MetaVar(name="_")
        else:
            # Check if first item is ellipsis marker
            if other_items[0] == "...":
                name = other_items[1]
                ellipsis = True
            else:
                name = other_items[0]
                ellipsis = False

            # Extract type constraint (remove leading ':')
            type_constraint = type_constraint_str[1:] if type_constraint_str else None

            metavar = MetaVar(
                name=name,
                ellipsis=ellipsis,
                type_constraint=type_constraint
            )

        self.metavars.append(metavar)
        return metavar

    def code_chunk(self, items):
        # Handle both regular code chunks and standalone colons
        # items can be empty if there's a standalone colon at the boundary
        if not items:
            return ""
        return str(items[0])

    def ELLIPSIS(self, token):
        return "..."

    def METAVAR_NAME(self, token):
        return str(token)

    def TYPE_NAME(self, token):
        return str(token)


# Load grammar from package
_grammar_text = importlib.resources.read_text("emend.grammars", "pattern.lark")
_parser = Lark(_grammar_text, parser="lalr")


def parse_pattern(pattern_str: str) -> Pattern:
    """Parse pattern with metavariables.

    Args:
        pattern_str: Pattern string with metavariables like "print($MSG)"

    Returns:
        Pattern object with raw string and extracted metavariables
    """
    tree = _parser.parse(pattern_str)
    transformer = PatternTransformer()
    transformer.transform(tree)

    return Pattern(
        raw=pattern_str,
        metavars=transformer.metavars
    )


def is_oracle_type_constraint(constraint: str | None) -> bool:
    """Check if a type constraint requires the TypeOracle (type[X] or returns[X])."""
    if constraint is None:
        return False
    return constraint.startswith("type[") or constraint.startswith("returns[")


def parse_oracle_type_constraint(constraint: str) -> tuple[str, str]:
    """Parse an oracle type constraint like 'type[Connection]' or 'returns[Optional[str]]'."""
    bracket_pos = constraint.index("[")
    kind = constraint[:bracket_pos]
    # Extract the inner type string, handling nested brackets
    inner = constraint[bracket_pos + 1:-1]
    return kind, inner



_COMPOUND_HEADER_RE = _re.compile(
    r"^\s*(?:if|elif|while|for|with|async\s+for|async\s+with)\s+.*:\s*$"
)

# ---------------------------------------------------------------------------
# Rust IR compiler (for tree-sitter fast path)
# ---------------------------------------------------------------------------

def _build_metavar_map_and_replace(
    pattern: Pattern, language: str = "python"
) -> tuple[str, dict[str, MetaVar]]:
    """Shared step 1 of pattern compilation: replace metavars with placeholders.

    Returns (temp_code, metavar_map) where metavar_map maps placeholder names
    to MetaVar objects.
    """
    temp_code = pattern.raw
    metavar_map: dict[str, MetaVar] = {}

    def _metavar_pattern_str(mv: MetaVar) -> str:
        """Build the literal pattern string for a metavar (e.g. '$...X:int')."""
        prefix = "$..." if mv.ellipsis else "$"
        suffix = f":{mv.type_constraint}" if mv.type_constraint else ""
        return f"{prefix}{mv.name}{suffix}"

    # Sort longest first so that more specific patterns are replaced before
    # shorter ones that are a prefix of them (e.g. '$X' before '$XY').
    sorted_metavars = sorted(
        pattern.metavars, key=lambda mv: -len(_metavar_pattern_str(mv))
    )

    for mv in sorted_metavars:
        placeholder = f"__META_{mv.name}__"
        metavar_map[placeholder] = mv
        temp_code = temp_code.replace(_metavar_pattern_str(mv), placeholder)

    if language != "python":
        return temp_code, metavar_map

    # Fix ellipsis metavars in dict context by appending `: None`.
    #
    # A single ellipsis metavar may appear multiple times in one pattern
    # (e.g. `f({"k": 1, $...A}, {"k": 2, $...A})`), and each occurrence was
    # replaced by the *same* placeholder above. We must therefore process
    # every occurrence of every ellipsis placeholder, not just the first.
    # Dedupe the metavars first so each placeholder is handled once, then
    # walk all occurrences left-to-right. Inserting `: None` shifts later
    # text, so we advance the search offset past the inserted text.
    seen_placeholders: set[str] = set()
    for mv in sorted_metavars:
        if not mv.ellipsis:
            continue
        placeholder = f"__META_{mv.name}__"
        if placeholder in seen_placeholders:
            continue
        seen_placeholders.add(placeholder)

        search_from = 0
        while True:
            idx = temp_code.find(placeholder, search_from)
            if idx == -1:
                break
            end = idx + len(placeholder)

            after_placeholder = temp_code[end:].lstrip()
            if after_placeholder.startswith(':'):
                # Already a key:value pair (or a type constraint); skip it.
                search_from = end
                continue

            inserted = False
            brace_depth = 0
            for i in range(idx - 1, -1, -1):
                c = temp_code[i]
                if c == '}':
                    brace_depth += 1
                elif c == '{':
                    if brace_depth == 0:
                        found_colon = False
                        inner_depth = 0
                        for j in range(i + 1, len(temp_code)):
                            cj = temp_code[j]
                            if cj in '{[(':
                                inner_depth += 1
                            elif cj in '}])':
                                if inner_depth == 0:
                                    break
                                inner_depth -= 1
                            elif cj == ':' and inner_depth == 0:
                                found_colon = True
                                break

                        if found_colon:
                            temp_code = (
                                temp_code[:end]
                                + ': None'
                                + temp_code[end:]
                            )
                            inserted = True
                        break
                    brace_depth -= 1

            # Advance past this occurrence (and any inserted `: None`) so the
            # next iteration finds the following occurrence.
            search_from = end + (len(': None') if inserted else 0)

    # Replace literal `...` in dict context with `**__EMEND_SPREAD__`
    temp_code = _re.sub(
        r'\.\.\.\s*}',
        '**__EMEND_SPREAD__}',
        temp_code
    )

    return temp_code, metavar_map


def compile_pattern_to_rust_ir(pattern_str: str, language: str = "python") -> dict | None:
    """Compile a pattern through its language plugin using tree-sitter."""
    from emend.language_plugins import load_plugin

    compiler = load_plugin(language).pattern_compiler
    return compiler.compile(pattern_str) if compiler is not None else None


def compile_constraint_to_rust_ir(
    constraint: str | None, language: str = "python"
) -> dict | None:
    """Compile an inside/not_inside constraint string to Rust IR dict."""
    if constraint is None:
        return None

    if constraint == "def":
        return {
            "type": "funcdef",
            "name": {"type": "any_expr"},
            "params": [{"type": "ellipsis"}],
            "decorators": [{"type": "ellipsis"}],
        }

    if constraint == "async def":
        return {
            "type": "funcdef",
            "name": {"type": "any_expr"},
            "params": [{"type": "ellipsis"}],
            "decorators": [{"type": "ellipsis"}],
            "is_async": True,
        }

    if constraint == "class":
        return {
            "type": "classdef",
            "name": {"type": "any_expr"},
            "bases": [{"type": "ellipsis"}],
            "decorators": [{"type": "ellipsis"}],
        }

    for keyword in ("async def", "def", "class"):
        if constraint.startswith(keyword + " "):
            name_pattern = constraint[len(keyword) + 1:].strip()
            name_pattern = name_pattern.rstrip(":").strip()
            name_ir = (
                {"type": "name_glob", "value": name_pattern}
                if "*" in name_pattern
                else {"type": "name", "value": name_pattern}
            )
            if keyword == "class":
                return {
                    "type": "classdef",
                    "name": name_ir,
                    "bases": [{"type": "ellipsis"}],
                    "decorators": [{"type": "ellipsis"}],
                }
            return {
                "type": "funcdef",
                "name": name_ir,
                "params": [{"type": "ellipsis"}],
                "decorators": [{"type": "ellipsis"}],
                "is_async": keyword == "async def",
            }

    # Simple keyword constraints for compound statements
    # Use NodeKindMatch to match related tree-sitter node types
    if constraint == "if":
        return {
            "type": "node_kind_match",
            "kinds": ["if_statement", "conditional_expression"],
        }

    if constraint == "for":
        return {
            "type": "node_kind_match",
            "kinds": [
                "for_statement",
                "list_comprehension",
                "set_comprehension",
                "dictionary_comprehension",
                "generator_expression",
            ],
        }

    if constraint == "while":
        return {
            "type": "node_kind_match",
            "kinds": ["while_statement"],
        }

    if constraint == "try":
        return {
            "type": "node_kind_match",
            "kinds": ["try_statement"],
        }

    if constraint == "with":
        return {
            "type": "node_kind_match",
            "kinds": ["with_statement"],
        }

    stripped = constraint.rstrip()
    if stripped.endswith(":"):
        ir = compile_pattern_to_rust_ir(stripped, language=language)
        if ir is not None:
            return ir

    # Users often omit the trailing colon on compound statement headers;
    # normalise before trying the pattern compiler.
    if not stripped.endswith(":") and _COMPOUND_HEADER_RE.match(stripped + ":"):
        ir = compile_pattern_to_rust_ir(stripped + ":", language=language)
        if ir is not None:
            return ir

    return None
