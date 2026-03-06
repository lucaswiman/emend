"""Parser for the emend query/transform language.

Parses the GritQL-inspired syntax defined in ``docs/language-spec.md``
and produces an :mod:`query_ast` IR tree.

Usage::

    from emend.query_parser import parse_query

    ast = parse_query('`print($x)` => `.`')
    # ast == Rewrite(match_pattern=CodeSnippet('print($x)'),
    #                replacement=None, where=None)
"""

from __future__ import annotations

import importlib.resources
import re
from dataclasses import dataclass
from lark import Lark, Transformer, Token

from . import query_ast as Q


# Pre-compiled regex for extract_metavars
_METAVAR_RE = re.compile(r'\$(?:\.\.\.)?(?:[a-z_][a-z0-9_]*|_)')


@dataclass(frozen=True)
class _ElseBody:
    """Internal sentinel for else-clause results during tree transformation."""
    statements: tuple[Q.Statement, ...]


def _is_dot(token: object) -> bool:
    """Test whether a Lark token is the DOT (deletion) terminal."""
    return isinstance(token, Token) and token.type == "DOT"


def _make_rewrite(
    match_pattern: Q.Pattern,
    replacement: object,
    where: Q.WhereClause | None = None,
) -> Q.Rewrite:
    """Build a Rewrite node, normalizing DOT to None (deletion)."""
    return Q.Rewrite(
        match_pattern=match_pattern,
        replacement=None if _is_dot(replacement) else replacement,
        where=where,
    )


# ---------------------------------------------------------------------------
# Lark transformer
# ---------------------------------------------------------------------------

class _QueryTransformer(Transformer):
    """Transform a Lark parse tree into query_ast nodes."""

    # -- Statements ----------------------------------------------------------

    def start(self, items):
        return items[0]

    def rewrite(self, items):
        where = items[2] if len(items) > 2 else None
        return _make_rewrite(items[0], items[1], where)

    def search(self, items):
        pattern = items[0]
        where = items[1] if len(items) > 1 else None
        return Q.Search(pattern=pattern, where=where)

    def sequential(self, items):
        return Q.Sequential(statements=tuple(items))

    def multifile(self, items):
        return Q.Multifile(file_stmts=tuple(items))

    def file_stmt(self, items):
        bubble_node = None
        idx = 0
        if isinstance(items[0], Q.Bubble):
            bubble_node = items[0]
            idx = 1
        var = items[idx]
        conditions = items[idx + 1]
        return Q.FileStmt(
            var=var,
            conditions=tuple(conditions),
            bubble=bubble_node,
        )

    def bubble(self, items):
        if items:
            # bubble(metavar_list)
            metavar_list = items[0]
            return Q.Bubble(exported_vars=tuple(metavar_list))
        return Q.Bubble()

    def metavar_list(self, items):
        return list(items)

    # -- Where clauses & conditions ------------------------------------------

    def where_clause(self, items):
        conditions = items[0]
        return Q.WhereClause(conditions=tuple(conditions))

    def conditions(self, items):
        return list(items)

    def match_condition(self, items):
        expr = items[0]
        matcher = items[1]
        return Q.MatchCondition(expr=expr, matcher=matcher)

    def not_condition(self, items):
        return Q.NotCondition(inner=items[0])

    def maybe_condition(self, items):
        return Q.MaybeCondition(statement=items[0])

    def if_condition(self, items):
        condition = items[0]
        then_stmts = []
        else_body = None
        for item in items[1:]:
            if isinstance(item, _ElseBody):
                else_body = item.statements
            else:
                then_stmts.append(item)
        return Q.IfCondition(
            condition=condition,
            then_body=tuple(then_stmts),
            else_body=else_body,
        )

    def else_clause(self, items):
        return _ElseBody(statements=tuple(items))

    def within_shorthand(self, items):
        return Q.WithinShorthand(target=items[0])

    def scope_local(self, items):
        return Q.ScopeLocal()

    # -- Matchers ------------------------------------------------------------

    def not_matcher(self, items):
        return Q.NotMatcher(inner=items[0])

    def or_matcher(self, items):
        return Q.OrMatcher(alternatives=tuple(items))

    def and_matcher(self, items):
        return Q.AndMatcher(left=items[0], right=items[1])

    # -- Predicates ----------------------------------------------------------

    def contains_pred(self, items):
        # items[0] may be a Rewrite (from inline_rewrite) or a Pattern
        return Q.ContainsPred(pattern=items[0])

    def inline_rewrite(self, items):
        return _make_rewrite(items[0], items[1])

    def within_pred(self, items):
        return Q.WithinPred(target=items[0])

    def not_within_pred(self, items):
        return Q.NotWithinPred(target=items[0])

    def imported_from_pred(self, items):
        raw = str(items[0])
        # Strip surrounding quotes
        module = raw.strip('"')
        return Q.ImportedFromPred(module=module)

    def precedes_pred(self, items):
        return Q.PrecedesPred(pattern=items[0])

    def follows_pred(self, items):
        return Q.FollowsPred(pattern=items[0])

    def type_pred(self, items):
        raw = str(items[0])
        return Q.TypePred(type_name=raw.strip('"'))

    def returns_pred(self, items):
        raw = str(items[0])
        return Q.ReturnsPred(type_name=raw.strip('"'))

    # -- Primitives ----------------------------------------------------------

    def code_snippet(self, items):
        raw = str(items[0])
        # Strip surrounding backticks
        return Q.CodeSnippet(code=raw[1:-1])

    def metavar(self, items):
        raw = str(items[0])
        # Parse: $..., $...name, $_, $name
        text = raw[1:]  # strip leading $
        if text == "...":
            return Q.MetaVar(name="...", is_spread=True)
        if text.startswith("..."):
            return Q.MetaVar(name=text[3:], is_spread=True)
        return Q.MetaVar(name=text, is_spread=False)

    def node_type(self, items):
        return Q.NodeType(name=str(items[0]))

    def keyword(self, items):
        return Q.Keyword(name=str(items[0]))

    def regex(self, items):
        raw = str(items[0])
        # Strip r" and trailing "
        return Q.Regex(pattern=raw[2:-1])


# ---------------------------------------------------------------------------
# Parser setup
# ---------------------------------------------------------------------------

_grammar_text = importlib.resources.read_text("emend.grammars", "query.lark")
_parser = Lark(_grammar_text, parser="earley", ambiguity="resolve")


def parse_query(source: str) -> Q.Statement:
    """Parse a query/transform expression and return an AST node.

    Args:
        source: A query string like ``\\`print($x)\\` => \\`log($x)\\```

    Returns:
        A :class:`query_ast.Statement` (Rewrite, Search, Sequential, or
        Multifile).

    Raises:
        lark.exceptions.LarkError: If the source cannot be parsed.

    Examples::

        >>> from emend.query_parser import parse_query
        >>> ast = parse_query('`print($x)`')
        >>> ast
        Search(pattern=CodeSnippet(code='print($x)'), where=None)

        >>> ast = parse_query('`old($x)` => `new($x)`')
        >>> ast.replacement.code
        'new($x)'
    """
    tree = _parser.parse(source)
    return _QueryTransformer().transform(tree)


def extract_metavars(pattern: Q.CodeSnippet) -> list[Q.MetaVar]:
    """Extract metavariables from a code snippet's text.

    Scans the code string for $name, $_, $...name, $... tokens and
    returns a list of MetaVar objects.

    This is useful for building the metavar map needed by the
    tree-sitter pattern compiler.
    """
    metavars: list[Q.MetaVar] = []
    seen: set[str] = set()
    for match in _METAVAR_RE.finditer(pattern.code):
        text = match.group()
        if text in seen:
            continue
        seen.add(text)
        inner = text[1:]  # strip $
        if inner == "...":
            metavars.append(Q.MetaVar(name="...", is_spread=True))
        elif inner.startswith("..."):
            metavars.append(Q.MetaVar(name=inner[3:], is_spread=True))
        else:
            metavars.append(Q.MetaVar(name=inner, is_spread=False))
    return metavars
