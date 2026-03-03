"""Tests for the emend query/transform language parser."""

import pytest
from emend.query_parser import parse_query, extract_metavars
from emend.query_ast import (
    AndMatcher,
    Bubble,
    CodeSnippet,
    ContainsPred,
    FileStmt,
    FollowsPred,
    IfCondition,
    ImportedFromPred,
    Keyword,
    MatchCondition,
    MaybeCondition,
    MetaVar,
    Multifile,
    NodeType,
    NotCondition,
    NotMatcher,
    NotWithinPred,
    OrMatcher,
    PrecedesPred,
    Regex,
    ReturnsPred,
    Rewrite,
    ScopeLocal,
    Search,
    Sequential,
    TypePred,
    WhereClause,
    WithinPred,
    WithinShorthand,
)


# ---------------------------------------------------------------------------
# Simple search patterns
# ---------------------------------------------------------------------------


class TestSimpleSearch:
    def test_code_snippet(self):
        ast = parse_query("`print($x)`")
        assert isinstance(ast, Search)
        assert isinstance(ast.pattern, CodeSnippet)
        assert ast.pattern.code == "print($x)"
        assert ast.where is None

    def test_metavar_pattern(self):
        ast = parse_query("`$fn($...args)`")
        assert isinstance(ast, Search)
        assert ast.pattern.code == "$fn($...args)"

    def test_spread_metavar(self):
        ast = parse_query("`f($...xs)`")
        assert isinstance(ast, Search)
        assert ast.pattern.code == "f($...xs)"

    def test_anonymous_wildcard(self):
        ast = parse_query("`isinstance($_, str)`")
        assert isinstance(ast, Search)
        assert "$_" in ast.pattern.code


# ---------------------------------------------------------------------------
# Rewrites
# ---------------------------------------------------------------------------


class TestRewrite:
    def test_simple_rewrite(self):
        ast = parse_query("`old($x)` => `new($x)`")
        assert isinstance(ast, Rewrite)
        assert ast.match_pattern.code == "old($x)"
        assert ast.replacement.code == "new($x)"

    def test_delete(self):
        ast = parse_query("`print($...args)` => .")
        assert isinstance(ast, Rewrite)
        assert ast.replacement is None  # . means delete

    def test_swap_args(self):
        ast = parse_query("`assertEqual($a, $b)` => `assertEqual($b, $a)`")
        assert isinstance(ast, Rewrite)

    def test_rewrite_with_where(self):
        ast = parse_query(
            '`$fn($x)` => `$fn($x, timeout=30)` where { $fn <: `requests.get` }'
        )
        assert isinstance(ast, Rewrite)
        assert ast.where is not None
        assert len(ast.where.conditions) == 1
        cond = ast.where.conditions[0]
        assert isinstance(cond, MatchCondition)
        assert isinstance(cond.expr, MetaVar)
        assert cond.expr.name == "fn"
        assert isinstance(cond.matcher, CodeSnippet)
        assert cond.matcher.code == "requests.get"


# ---------------------------------------------------------------------------
# Where clauses & conditions
# ---------------------------------------------------------------------------


class TestWhereClause:
    def test_single_condition(self):
        ast = parse_query('`$fn($...args)` where { $fn <: imported_from("requests") }')
        assert isinstance(ast, Search)
        assert ast.where is not None
        cond = ast.where.conditions[0]
        assert isinstance(cond, MatchCondition)
        assert isinstance(cond.matcher, ImportedFromPred)
        assert cond.matcher.module == "requests"

    def test_multiple_conditions(self):
        ast = parse_query(
            '`$fn($...args)` where { '
            '$fn <: imported_from("requests"), '
            "$...args <: not contains `timeout=$_`, "
            "$fn <: not `requests.head` }"
        )
        assert isinstance(ast, Search)
        assert len(ast.where.conditions) == 3

    def test_scope_local(self):
        ast = parse_query("`config` where { scope_local }")
        assert isinstance(ast, Search)
        cond = ast.where.conditions[0]
        assert isinstance(cond, ScopeLocal)

    def test_within_shorthand(self):
        ast = parse_query("`print($...args)` where { within def }")
        assert isinstance(ast, Search)
        cond = ast.where.conditions[0]
        assert isinstance(cond, WithinShorthand)
        assert isinstance(cond.target, Keyword)
        assert cond.target.name == "def"

    def test_not_within_shorthand(self):
        ast = parse_query("`global $x` where { not within class }")
        assert isinstance(ast, Search)
        cond = ast.where.conditions[0]
        # "not within X" parses as NotCondition(WithinShorthand(X))
        assert isinstance(cond, NotCondition)
        assert isinstance(cond.inner, WithinShorthand)
        assert isinstance(cond.inner.target, Keyword)
        assert cond.inner.target.name == "class"

    def test_within_pattern(self):
        ast = parse_query("`print($x)` where { within `def test_$_($...): $...` }")
        assert isinstance(ast, Search)
        cond = ast.where.conditions[0]
        assert isinstance(cond, WithinShorthand)
        assert isinstance(cond.target, CodeSnippet)


# ---------------------------------------------------------------------------
# Match operator <:
# ---------------------------------------------------------------------------


class TestMatchOperator:
    def test_code_snippet_matcher(self):
        ast = parse_query("`$x` where { $x <: `foo` }")
        cond = ast.where.conditions[0]
        assert isinstance(cond, MatchCondition)
        assert isinstance(cond.matcher, CodeSnippet)
        assert cond.matcher.code == "foo"

    def test_node_type_matcher(self):
        ast = parse_query("`$x` where { $x <: identifier }")
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, NodeType)
        assert cond.matcher.name == "identifier"

    def test_not_matcher(self):
        ast = parse_query("`$x` where { $x <: not `None` }")
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, NotMatcher)
        assert isinstance(cond.matcher.inner, CodeSnippet)

    def test_or_matcher(self):
        ast = parse_query("`$fn($x)` where { $fn <: or { `old_api`, `legacy_api` } }")
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, OrMatcher)
        assert len(cond.matcher.alternatives) == 2
        assert cond.matcher.alternatives[0].code == "old_api"
        assert cond.matcher.alternatives[1].code == "legacy_api"

    def test_regex_matcher(self):
        ast = parse_query('`$x` where { $x <: r"test_.*" }')
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, Regex)
        assert cond.matcher.pattern == "test_.*"


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_contains(self):
        ast = parse_query(
            "`class $name($...bases): $...body` where { $bases <: contains `BaseModel` }"
        )
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, ContainsPred)
        assert isinstance(cond.matcher.pattern, CodeSnippet)
        assert cond.matcher.pattern.code == "BaseModel"

    def test_contains_with_rewrite(self):
        ast = parse_query(
            "`$body` where { $body <: contains `$target()` => `new_$target()` }"
        )
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, ContainsPred)
        assert isinstance(cond.matcher.pattern, Rewrite)

    def test_within_pred(self):
        ast = parse_query(
            "`$x` where { $x <: within `def test_$_($...): $...` }"
        )
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, WithinPred)

    def test_not_within_pred(self):
        ast = parse_query("`$x` where { $x <: not within `class $_: $...` }")
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, NotWithinPred)

    def test_imported_from(self):
        ast = parse_query('`loads($data)` where { $_ <: imported_from("json") }')
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, ImportedFromPred)
        assert cond.matcher.module == "json"

    def test_type_pred(self):
        ast = parse_query('`$x` where { $x <: type("Connection") }')
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, TypePred)
        assert cond.matcher.type_name == "Connection"

    def test_returns_pred(self):
        ast = parse_query('`$fn()` where { $fn <: returns("str") }')
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, ReturnsPred)
        assert cond.matcher.type_name == "str"

    def test_precedes(self):
        ast = parse_query("`$stmt` where { $stmt <: precedes `return $_` }")
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, PrecedesPred)

    def test_follows(self):
        ast = parse_query("`$stmt` where { $stmt <: follows `if $_:` }")
        cond = ast.where.conditions[0]
        assert isinstance(cond.matcher, FollowsPred)


# ---------------------------------------------------------------------------
# maybe / if-else
# ---------------------------------------------------------------------------


class TestControlFlow:
    def test_maybe_in_where(self):
        ast = parse_query(
            "`$x` where { maybe `from old import $name` => `from new import $name` }"
        )
        cond = ast.where.conditions[0]
        assert isinstance(cond, MaybeCondition)
        assert isinstance(cond.statement, Rewrite)

    def test_if_condition(self):
        ast = parse_query(
            "`$x` where { if ($x <: int_literal) { `$x` => `str($x)` } }"
        )
        cond = ast.where.conditions[0]
        assert isinstance(cond, IfCondition)
        assert isinstance(cond.condition, MatchCondition)
        assert len(cond.then_body) == 1
        assert isinstance(cond.then_body[0], Rewrite)
        assert cond.else_body is None

    def test_if_else_condition(self):
        ast = parse_query(
            "`$x` where { if ($x <: int_literal) { `$x` => `str($x)` } "
            "else { `$x` => `repr($x)` } }"
        )
        cond = ast.where.conditions[0]
        assert isinstance(cond, IfCondition)
        assert cond.else_body is not None
        assert len(cond.else_body) == 1


# ---------------------------------------------------------------------------
# Sequential
# ---------------------------------------------------------------------------


class TestSequential:
    def test_two_rewrites(self):
        ast = parse_query(
            "sequential { "
            "`from old_module import $name` => `from new_module import $name`, "
            "`old_module.$attr` => `new_module.$attr` }"
        )
        assert isinstance(ast, Sequential)
        assert len(ast.statements) == 2
        assert all(isinstance(s, Rewrite) for s in ast.statements)


# ---------------------------------------------------------------------------
# Multifile
# ---------------------------------------------------------------------------


class TestMultifile:
    def test_multifile_with_bubble(self):
        ast = parse_query(
            "multifile { "
            "bubble($target) file($body) where { "
            "  $body <: contains `class $target: $...` "
            "}, "
            "bubble($target) file($body) where { "
            "  $body <: contains `$target()` => `new_$target()` "
            "} }"
        )
        assert isinstance(ast, Multifile)
        assert len(ast.file_stmts) == 2
        fs0 = ast.file_stmts[0]
        assert isinstance(fs0, FileStmt)
        assert isinstance(fs0.bubble, Bubble)
        assert len(fs0.bubble.exported_vars) == 1
        assert fs0.bubble.exported_vars[0].name == "target"
        assert fs0.var.name == "body"

    def test_multifile_without_bubble(self):
        ast = parse_query(
            "multifile { "
            "file($body) where { $body <: contains `print($x)` } }"
        )
        assert isinstance(ast, Multifile)
        assert ast.file_stmts[0].bubble is None


# ---------------------------------------------------------------------------
# extract_metavars
# ---------------------------------------------------------------------------


class TestExtractMetavars:
    def test_simple(self):
        mvs = extract_metavars(CodeSnippet("print($x)"))
        assert len(mvs) == 1
        assert mvs[0].name == "x"
        assert not mvs[0].is_spread

    def test_spread(self):
        mvs = extract_metavars(CodeSnippet("f($...args)"))
        assert len(mvs) == 1
        assert mvs[0].name == "args"
        assert mvs[0].is_spread

    def test_anonymous(self):
        mvs = extract_metavars(CodeSnippet("isinstance($_, str)"))
        assert len(mvs) == 1
        assert mvs[0].name == "_"
        assert not mvs[0].is_spread

    def test_multiple(self):
        mvs = extract_metavars(CodeSnippet("$fn($x, $...rest)"))
        names = {mv.name for mv in mvs}
        assert names == {"fn", "x", "rest"}

    def test_deduplication(self):
        mvs = extract_metavars(CodeSnippet("$x == $x"))
        assert len(mvs) == 1
        assert mvs[0].name == "x"


# ---------------------------------------------------------------------------
# Round-trip: parse -> str
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Verify that str(parse_query(s)) produces a recognizable representation."""

    def test_search_roundtrip(self):
        ast = parse_query("`print($x)`")
        assert "`print($x)`" in str(ast)

    def test_rewrite_roundtrip(self):
        ast = parse_query("`old($x)` => `new($x)`")
        s = str(ast)
        assert "=>" in s
        assert "`old($x)`" in s
        assert "`new($x)`" in s

    def test_delete_roundtrip(self):
        ast = parse_query("`print($x)` => .")
        s = str(ast)
        assert "=> ." in s

    def test_where_roundtrip(self):
        ast = parse_query('`$fn($x)` where { $fn <: imported_from("json") }')
        s = str(ast)
        assert "where" in s
        assert "imported_from" in s

    def test_sequential_roundtrip(self):
        ast = parse_query(
            "sequential { `a` => `b`, `c` => `d` }"
        )
        s = str(ast)
        assert "sequential" in s
