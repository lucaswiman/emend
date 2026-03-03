"""Abstract syntax tree for the emend query/transform language.

This module defines the IR that the query parser produces and that
the matching/rewriting engines consume.  The design follows the
GritQL-inspired language spec in docs/language-spec.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


# ---------------------------------------------------------------------------
# Metavariables
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetaVar:
    """A metavariable like $x, $_, $...xs, or $..."""

    name: str  # "x", "_", etc.
    is_spread: bool = False  # True for $...xs and $...

    @property
    def is_anonymous(self) -> bool:
        return self.name == "_"

    @property
    def is_anonymous_spread(self) -> bool:
        return self.is_spread and self.name == "..."

    def __str__(self) -> str:
        if self.is_anonymous_spread:
            return "$..."
        prefix = "$..." if self.is_spread else "$"
        return f"{prefix}{self.name}"


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CodeSnippet:
    """A backtick-delimited code pattern.

    The ``code`` field contains the raw source text (without backticks).
    It is parsed by tree-sitter or LibCST to build the structural
    matcher.  Metavariables ($x etc.) are embedded in the code.
    """

    code: str

    def __str__(self) -> str:
        return f"`{self.code}`"


@dataclass(frozen=True)
class Regex:
    """A regex literal like r"test_.*"."""

    pattern: str

    def __str__(self) -> str:
        return f'r"{self.pattern}"'


@dataclass(frozen=True)
class NodeType:
    """A tree-sitter node type constraint like ``identifier``, ``call``."""

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Keyword:
    """A scope keyword shortcut: def, class, for, while, try, with, if."""

    name: str

    def __str__(self) -> str:
        return self.name


# Pattern is the union of things that can appear as a matchable
Pattern = Union[CodeSnippet, MetaVar]


# ---------------------------------------------------------------------------
# Matchers (right-hand side of <:)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NotMatcher:
    """``not <matcher>``."""

    inner: Matcher

    def __str__(self) -> str:
        return f"not {self.inner}"


@dataclass(frozen=True)
class OrMatcher:
    """``or { m1, m2, ... }``."""

    alternatives: tuple[Matcher, ...]

    def __str__(self) -> str:
        body = ", ".join(str(a) for a in self.alternatives)
        return f"or {{ {body} }}"


@dataclass(frozen=True)
class AndMatcher:
    """``m1 and m2``."""

    left: Matcher
    right: Matcher

    def __str__(self) -> str:
        return f"{self.left} and {self.right}"


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContainsPred:
    """``contains <pattern>`` or ``contains <pattern> => <replacement>``."""

    pattern: Union[Pattern, "Rewrite"]

    def __str__(self) -> str:
        return f"contains {self.pattern}"


@dataclass(frozen=True)
class WithinPred:
    """``within <pattern | keyword>``."""

    target: Union[Pattern, Keyword]

    def __str__(self) -> str:
        return f"within {self.target}"


@dataclass(frozen=True)
class NotWithinPred:
    """``not within <pattern | keyword>``."""

    target: Union[Pattern, Keyword]

    def __str__(self) -> str:
        return f"not within {self.target}"


@dataclass(frozen=True)
class ImportedFromPred:
    """``imported_from("module")``."""

    module: str

    def __str__(self) -> str:
        return f'imported_from("{self.module}")'


@dataclass(frozen=True)
class PrecedesPred:
    """``precedes <pattern>``."""

    pattern: Pattern

    def __str__(self) -> str:
        return f"precedes {self.pattern}"


@dataclass(frozen=True)
class FollowsPred:
    """``follows <pattern>``."""

    pattern: Pattern

    def __str__(self) -> str:
        return f"follows {self.pattern}"


@dataclass(frozen=True)
class TypePred:
    """``type("Connection")`` -- oracle type constraint."""

    type_name: str

    def __str__(self) -> str:
        return f'type("{self.type_name}")'


@dataclass(frozen=True)
class ReturnsPred:
    """``returns("str")`` -- oracle return-type constraint."""

    type_name: str

    def __str__(self) -> str:
        return f'returns("{self.type_name}")'


# Matcher is the union type for the RHS of <:
Matcher = Union[
    CodeSnippet,
    MetaVar,
    NodeType,
    Regex,
    NotMatcher,
    OrMatcher,
    AndMatcher,
    ContainsPred,
    WithinPred,
    NotWithinPred,
    ImportedFromPred,
    PrecedesPred,
    FollowsPred,
    TypePred,
    ReturnsPred,
]


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchCondition:
    """``$x <: matcher``."""

    expr: Union[MetaVar, CodeSnippet]
    matcher: Matcher

    def __str__(self) -> str:
        return f"{self.expr} <: {self.matcher}"


@dataclass(frozen=True)
class NotCondition:
    """``not <condition>``."""

    inner: Condition

    def __str__(self) -> str:
        return f"not {self.inner}"


@dataclass(frozen=True)
class ScopeLocal:
    """``scope_local`` -- restrict to locally-defined names."""

    def __str__(self) -> str:
        return "scope_local"


@dataclass(frozen=True)
class WithinShorthand:
    """``within def`` or ``within `pattern``` -- shorthand without $var <:."""

    target: Union[CodeSnippet, Keyword]

    def __str__(self) -> str:
        return f"within {self.target}"


# Forward reference: Condition includes Rewrite (defined below)
# We define Condition after all statement types.


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rewrite:
    """``pattern => replacement`` with optional ``where`` clause."""

    match_pattern: Pattern
    replacement: Union[Pattern, None]  # None means . (delete)
    where: WhereClause | None = None

    def __str__(self) -> str:
        rhs = "." if self.replacement is None else str(self.replacement)
        base = f"{self.match_pattern} => {rhs}"
        if self.where:
            return f"{base} {self.where}"
        return base


@dataclass(frozen=True)
class Search:
    """``pattern`` with optional ``where`` clause (no rewrite)."""

    pattern: Pattern
    where: WhereClause | None = None

    def __str__(self) -> str:
        base = str(self.pattern)
        if self.where:
            return f"{base} {self.where}"
        return base


@dataclass(frozen=True)
class MaybeCondition:
    """``maybe <statement>`` -- apply if possible, succeed anyway."""

    statement: Statement

    def __str__(self) -> str:
        return f"maybe {self.statement}"


@dataclass(frozen=True)
class IfCondition:
    """``if (cond) { stmts } else { stmts }``."""

    condition: Condition
    then_body: tuple[Statement, ...]
    else_body: tuple[Statement, ...] | None = None

    def __str__(self) -> str:
        then_str = "; ".join(str(s) for s in self.then_body)
        base = f"if ({self.condition}) {{ {then_str} }}"
        if self.else_body:
            else_str = "; ".join(str(s) for s in self.else_body)
            base += f" else {{ {else_str} }}"
        return base


@dataclass(frozen=True)
class Bubble:
    """``bubble`` or ``bubble($x, $y)`` scope isolation."""

    exported_vars: tuple[MetaVar, ...] = ()

    def __str__(self) -> str:
        if self.exported_vars:
            vars_str = ", ".join(str(v) for v in self.exported_vars)
            return f"bubble({vars_str})"
        return "bubble"


@dataclass(frozen=True)
class FileStmt:
    """``[bubble] file($body) where { ... }``."""

    var: MetaVar
    conditions: tuple[Condition, ...]
    bubble: Bubble | None = None

    def __str__(self) -> str:
        prefix = f"{self.bubble} " if self.bubble else ""
        conds = ", ".join(str(c) for c in self.conditions)
        return f"{prefix}file({self.var}) where {{ {conds} }}"


@dataclass(frozen=True)
class Sequential:
    """``sequential { stmt1, stmt2, ... }``."""

    statements: tuple[Statement, ...]

    def __str__(self) -> str:
        body = ", ".join(str(s) for s in self.statements)
        return f"sequential {{ {body} }}"


@dataclass(frozen=True)
class Multifile:
    """``multifile { file_stmt1, file_stmt2, ... }``."""

    file_stmts: tuple[FileStmt, ...]

    def __str__(self) -> str:
        body = ", ".join(str(f) for f in self.file_stmts)
        return f"multifile {{ {body} }}"


@dataclass(frozen=True)
class WhereClause:
    """``where { cond1, cond2, ... }``."""

    conditions: tuple[Condition, ...]

    def __str__(self) -> str:
        body = ", ".join(str(c) for c in self.conditions)
        return f"where {{ {body} }}"


# ---------------------------------------------------------------------------
# Union types
# ---------------------------------------------------------------------------

Condition = Union[
    MatchCondition,
    NotCondition,
    MaybeCondition,
    IfCondition,
    Rewrite,
    WithinShorthand,
    ScopeLocal,
]

Statement = Union[
    Rewrite,
    Search,
    Sequential,
    Multifile,
]
