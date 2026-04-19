"""Tests for pattern find features: negated type constraints, compound statements, decorators, lambdas."""

import pytest

from emend.transform import find_pattern, replace_pattern
from emend.pattern import parse_pattern


# ── Feature 1: Negated Type Constraints ──────────────────────────────────────


class TestNegatedTypeConstraints:
    """Tests for $X:!int, $X:!str, etc. negated type constraint syntax."""

    def test_parse_negated_type_constraint(self):
        """Parsing $X:!int extracts a negated type constraint."""
        pat = parse_pattern("range($X:!int)")
        assert len(pat.metavars) == 1
        assert pat.metavars[0].name == "X"
        assert pat.metavars[0].type_constraint == "!int"

    def test_negated_int_matches_non_integer(self, tmp_path):
        """$X:!int matches non-integer arguments."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "range(10)\n"
            "range(n)\n"
            "range(len(items))\n"
        )
        matches = find_pattern("range($X:!int)", str(test_file))
        # Should match range(n) and range(len(items)), but NOT range(10)
        assert len(matches) == 2
        captured = {m_.captures["X"] for m_ in matches}
        assert captured == {"n", "len(items)"}

    def test_negated_str_matches_non_string(self, tmp_path):
        """$X:!str matches non-string arguments."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "print('hello')\n"
            "print(42)\n"
            "print(x)\n"
        )
        matches = find_pattern("print($X:!str)", str(test_file))
        # Should match print(42) and print(x), not print('hello')
        assert len(matches) == 2
        captured = {m_.captures["X"] for m_ in matches}
        assert captured == {"42", "x"}

    def test_negated_call_matches_non_call(self, tmp_path):
        """$X:!call matches non-call expressions."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "foo(bar())\n"
            "foo(x)\n"
            "foo(42)\n"
        )
        matches = find_pattern("foo($X:!call)", str(test_file))
        # Should match foo(x) and foo(42), not foo(bar())
        assert len(matches) == 2
        captured = {m_.captures["X"] for m_ in matches}
        assert captured == {"x", "42"}

    def test_negated_identifier_matches_non_name(self, tmp_path):
        """$X:!identifier matches non-name expressions."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func(x)\n"
            "func(42)\n"
            "func('hello')\n"
        )
        matches = find_pattern("func($X:!identifier)", str(test_file))
        # Should match func(42) and func('hello'), not func(x)
        assert len(matches) == 2
        captured = {m_.captures["X"] for m_ in matches}
        assert captured == {"42", "'hello'"}


# ── Feature 2: Compound Statement Patterns ───────────────────────────────────


class TestCompoundStatementPatterns:
    """Tests for if/for/while/with statement header patterns."""

    def test_if_condition_pattern(self, tmp_path):
        """'if $COND:' matches if statements, capturing the condition."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "if x > 0:\n"
            "    pass\n"
            "if y == 'hello':\n"
            "    pass\n"
            "while z:\n"
            "    pass\n"
        )
        matches = find_pattern("if $COND:", str(test_file))
        assert len(matches) == 2
        captured = {m_.captures["COND"] for m_ in matches}
        assert captured == {"x > 0", "y == 'hello'"}

    def test_while_condition_pattern(self, tmp_path):
        """'while $COND:' matches while loops, capturing the condition."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "while True:\n"
            "    pass\n"
            "while x > 0:\n"
            "    x -= 1\n"
            "if something:\n"
            "    pass\n"
        )
        matches = find_pattern("while $COND:", str(test_file))
        assert len(matches) == 2
        captured = {m_.captures["COND"] for m_ in matches}
        assert captured == {"True", "x > 0"}

    def test_for_loop_pattern(self, tmp_path):
        """'for $VAR in $ITER:' matches for loops."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "for x in range(10):\n"
            "    pass\n"
            "for item in items:\n"
            "    process(item)\n"
            "while True:\n"
            "    pass\n"
        )
        matches = find_pattern("for $VAR in $ITER:", str(test_file))
        assert len(matches) == 2
        captured_vars = {m_.captures["VAR"] for m_ in matches}
        captured_iters = {m_.captures["ITER"] for m_ in matches}
        assert captured_vars == {"x", "item"}
        assert captured_iters == {"range(10)", "items"}

    def test_with_context_pattern(self, tmp_path):
        """'with $CTX as $VAR:' matches with statements."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "with open('file.txt') as f:\n"
            "    pass\n"
            "with lock as l:\n"
            "    pass\n"
            "with timer():\n"
            "    pass\n"
        )
        matches = find_pattern("with $CTX as $VAR:", str(test_file))
        # Should match only the two 'with ... as ...' forms
        assert len(matches) == 2
        captured_ctxs = {m_.captures["CTX"] for m_ in matches}
        assert captured_ctxs == {"open('file.txt')", "lock"}

    def test_with_no_as_pattern(self, tmp_path):
        """'with $CTX:' matches with statements without 'as' clause."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "with open('file.txt') as f:\n"
            "    pass\n"
            "with timer():\n"
            "    pass\n"
        )
        matches = find_pattern("with $CTX:", str(test_file))
        # Should match both: the 'as' part is ignored
        assert len(matches) == 2


# ── Feature 3: Decorator Patterns in Find ────────────────────────────────────


class TestDecoratorPatterns:
    """Tests for matching decorated function/class definitions."""

    def test_decorated_function_pattern(self, tmp_path):
        """'@$DEC\\ndef $FUNC($...ARGS):' matches decorated functions."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@property\n"
            "def name(self):\n"
            "    return self._name\n"
            "\n"
            "@staticmethod\n"
            "def create():\n"
            "    pass\n"
            "\n"
            "def plain():\n"
            "    pass\n"
        )
        matches = find_pattern("@$DEC\ndef $FUNC($...ARGS):", str(test_file))
        assert len(matches) == 2
        captured_decs = {m_.captures["DEC"] for m_ in matches}
        captured_funcs = {m_.captures["FUNC"] for m_ in matches}
        assert captured_decs == {"property", "staticmethod"}
        assert captured_funcs == {"name", "create"}

    def test_specific_decorator_pattern(self, tmp_path):
        """'@property\\ndef $FUNC($...ARGS):' matches only @property-decorated functions."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@property\n"
            "def name(self):\n"
            "    return self._name\n"
            "\n"
            "@staticmethod\n"
            "def create():\n"
            "    pass\n"
        )
        matches = find_pattern("@property\ndef $FUNC($...ARGS):", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["FUNC"] == "name"

    def test_undecorated_not_matched(self, tmp_path):
        """Decorated function pattern does not match undecorated functions."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "def plain():\n"
            "    pass\n"
        )
        matches = find_pattern("@$DEC\ndef $FUNC($...ARGS):", str(test_file))
        assert len(matches) == 0


# ── Feature 4: Lambda Body Patterns ──────────────────────────────────────────


class TestLambdaPatterns:
    """Tests for lambda pattern matching."""

    def test_lambda_body_pattern(self, tmp_path):
        """'lambda $X: $EXPR' matches lambda expressions with one param."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda x: x + 1\n"
            "g = lambda y: y * 2\n"
            "h = len\n"
        )
        matches = find_pattern("lambda $X: $EXPR", str(test_file))
        assert len(matches) == 2
        captured_bodies = {m_.captures["EXPR"] for m_ in matches}
        assert captured_bodies == {"x + 1", "y * 2"}

    def test_lambda_specific_body_pattern(self, tmp_path):
        """'lambda $X: $X + 1' matches lambdas that add 1."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda x: x + 1\n"
            "g = lambda y: y * 2\n"
        )
        matches = find_pattern("lambda $X: $X + 1", str(test_file))
        # Should only match the first one where body is "$X + 1"
        assert len(matches) == 1
        assert matches[0].captures["X"] == "x"

    def test_lambda_no_params_pattern(self, tmp_path):
        """'lambda: $EXPR' matches parameterless lambdas."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda: 42\n"
            "g = lambda x: x\n"
        )
        matches = find_pattern("lambda: $EXPR", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["EXPR"] == "42"


# ── Feature 5: Star Expression Patterns (3.2) ────────────────────────────────


class TestStarExpressionPatterns:
    """Tests for *$X and **$X star expression patterns in function calls."""

    def test_star_arg_pattern(self, tmp_path):
        """'func(*$ARGS)' matches calls with star unpacking."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func(*args)\n"
            "func(1, 2)\n"
            "func(*items)\n"
        )
        matches = find_pattern("func(*$ARGS)", str(test_file))
        assert len(matches) == 2
        captured = {m_.captures["ARGS"] for m_ in matches}
        assert captured == {"args", "items"}

    def test_double_star_kwarg_pattern(self, tmp_path):
        """'func(**$KWARGS)' matches calls with double-star dict unpacking."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func(**kwargs)\n"
            "func(a=1)\n"
            "func(**options)\n"
        )
        matches = find_pattern("func(**$KWARGS)", str(test_file))
        assert len(matches) == 2
        captured = {m_.captures["KWARGS"] for m_ in matches}
        assert captured == {"kwargs", "options"}

    def test_star_and_double_star_pattern(self, tmp_path):
        """'func(*$ARGS, **$KWARGS)' matches calls with both star and double-star."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func(*args, **kwargs)\n"
            "func(1, 2)\n"
            "func(*items)\n"
            "func(**opts)\n"
        )
        matches = find_pattern("func(*$ARGS, **$KWARGS)", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["ARGS"] == "args"
        assert matches[0].captures["KWARGS"] == "kwargs"

    def test_star_does_not_match_regular_args(self, tmp_path):
        """'func(*$ARGS)' does NOT match regular positional args."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func(1, 2, 3)\n"
            "func(x)\n"
        )
        matches = find_pattern("func(*$ARGS)", str(test_file))
        assert len(matches) == 0

    def test_mixed_regular_and_star_args(self, tmp_path):
        """'func($X, *$ARGS)' matches calls with positional then star args."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "func(1, *rest)\n"
            "func(*all_args)\n"
            "func(1, 2)\n"
        )
        matches = find_pattern("func($X, *$ARGS)", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["X"] == "1"
        assert matches[0].captures["ARGS"] == "rest"


# ── Feature 6: Async Compound Statement Patterns (3.3) ───────────────────────


class TestAsyncCompoundStatementPatterns:
    """Tests for async for and async with statement patterns."""

    def test_async_for_pattern(self, tmp_path):
        """'async for $VAR in $ITER:' matches only async for loops."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "async for x in aiter:\n"
            "    pass\n"
            "for y in items:\n"
            "    pass\n"
        )
        matches = find_pattern("async for $VAR in $ITER:", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["VAR"] == "x"
        assert matches[0].captures["ITER"] == "aiter"

    def test_async_with_pattern(self, tmp_path):
        """'async with $CTX as $VAR:' matches only async with statements."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "async with ctx() as v:\n"
            "    pass\n"
            "with other() as w:\n"
            "    pass\n"
        )
        matches = find_pattern("async with $CTX as $VAR:", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["CTX"] == "ctx()"
        assert matches[0].captures["VAR"] == "v"

    def test_async_with_no_as_pattern(self, tmp_path):
        """'async with $CTX:' matches async with without 'as' clause."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "async with lock():\n"
            "    pass\n"
            "with timer():\n"
            "    pass\n"
        )
        matches = find_pattern("async with $CTX:", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["CTX"] == "lock()"

    def test_regular_for_matches_both_sync_and_async(self, tmp_path):
        """'for $VAR in $ITER:' matches both sync and async for loops."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "async for x in aiter:\n"
            "    pass\n"
            "for y in items:\n"
            "    pass\n"
        )
        matches = find_pattern("for $VAR in $ITER:", str(test_file))
        assert len(matches) == 2

    def test_regular_with_matches_both_sync_and_async(self, tmp_path):
        """'with $CTX:' matches both sync and async with statements."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "async with lock():\n"
            "    pass\n"
            "with timer():\n"
            "    pass\n"
        )
        matches = find_pattern("with $CTX:", str(test_file))
        assert len(matches) == 2


# ── Feature 7: Multiple Decorator Patterns (3.4) ─────────────────────────────


class TestMultipleDecoratorPatterns:
    """Tests for patterns matching functions with multiple decorators."""

    def test_two_decorators_pattern(self, tmp_path):
        """'@$DEC1\\n@$DEC2\\ndef $FUNC($...ARGS):' matches doubly-decorated functions."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@dec1\n"
            "@dec2\n"
            "def func():\n"
            "    pass\n"
            "\n"
            "@dec3\n"
            "def other():\n"
            "    pass\n"
        )
        matches = find_pattern("@$DEC1\n@$DEC2\ndef $FUNC($...ARGS):", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["DEC1"] == "dec1"
        assert matches[0].captures["DEC2"] == "dec2"
        assert matches[0].captures["FUNC"] == "func"

    def test_async_decorated_function_pattern(self, tmp_path):
        """'@$DEC\\nasync def $FUNC($...ARGS):' matches only async decorated functions."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@app.route('/test')\n"
            "async def handler():\n"
            "    pass\n"
            "\n"
            "@app.route('/other')\n"
            "def sync_handler():\n"
            "    pass\n"
        )
        matches = find_pattern("@$DEC\nasync def $FUNC($...ARGS):", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["FUNC"] == "handler"

    def test_specific_decorator_with_call_pattern(self, tmp_path):
        """'@app.route($PATH)\\ndef $FUNC($...ARGS):' matches decorator with call args."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "@app.route('/api')\n"
            "def handler():\n"
            "    pass\n"
            "\n"
            "@other_dec\n"
            "def other():\n"
            "    pass\n"
        )
        matches = find_pattern("@app.route($PATH)\ndef $FUNC($...ARGS):", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["PATH"] == "'/api'"
        assert matches[0].captures["FUNC"] == "handler"


# ── Feature 8: Lambda Star Args (3.5) ────────────────────────────────────────


class TestLambdaStarArgs:
    """Tests for lambda *$ARGS pattern matching."""

    def test_lambda_star_args_pattern(self, tmp_path):
        """'lambda *$ARGS: $EXPR' matches lambdas with star args only."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda *args: sum(args)\n"
            "g = lambda x: x\n"
            "h = lambda *a, **kw: (a, kw)\n"
        )
        matches = find_pattern("lambda *$ARGS: $EXPR", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["ARGS"] == "args"
        assert matches[0].captures["EXPR"] == "sum(args)"

    def test_lambda_star_and_double_star_pattern(self, tmp_path):
        """'lambda *$ARGS, **$KWARGS: $EXPR' matches lambdas with both."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda *args: sum(args)\n"
            "g = lambda *a, **kw: (a, kw)\n"
        )
        matches = find_pattern("lambda *$ARGS, **$KWARGS: $EXPR", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["ARGS"] == "a"
        assert matches[0].captures["KWARGS"] == "kw"

    def test_lambda_star_does_not_match_regular_params(self, tmp_path):
        """'lambda *$ARGS: $EXPR' does NOT match regular-param lambdas."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda x, y: x + y\n"
            "g = lambda: 42\n"
        )
        matches = find_pattern("lambda *$ARGS: $EXPR", str(test_file))
        assert len(matches) == 0

    def test_lambda_default_param_pattern_matches_specific_default(self, tmp_path):
        """'lambda $X=5: $EXPR' only matches lambdas where param default is 5."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda x=5: x + 1\n"
            "g = lambda y: y * 2\n"
            "h = lambda z=10: z + 3\n"
        )
        matches = find_pattern("lambda $X=5: $EXPR", str(test_file))
        assert len(matches) == 1
        assert matches[0].matched_text == "lambda x=5: x + 1"
        assert matches[0].captures["EXPR"] == "x + 1"

    def test_lambda_default_param_pattern_does_not_match_without_default(self, tmp_path):
        """'lambda $X=5: $EXPR' does NOT match lambdas without defaults."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "f = lambda y: y * 2\n"
            "g = lambda z: z\n"
        )
        matches = find_pattern("lambda $X=5: $EXPR", str(test_file))
        assert len(matches) == 0


# ── Feature 9: Dict Patterns with Literal String Keys (3.6) ──────────────────


class TestDictLiteralKeyPatterns:
    """Tests for dict patterns with literal string keys and partial matching."""

    def test_exact_dict_pattern(self, tmp_path):
        """'{'name': $NAME, 'age': $AGE}' matches exact dict structure."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "d = {'name': 'Alice', 'age': 30}\n"
            "e = {'name': 'Bob'}\n"
        )
        matches = find_pattern("{'name': $NAME, 'age': $AGE}", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["NAME"] == "'Alice'"
        assert matches[0].captures["AGE"] == "30"

    def test_partial_dict_pattern_with_spread(self, tmp_path):
        """'{'type': $TYPE, ...}' matches dicts containing 'type' key plus others."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "d = {'type': 'user', 'name': 'Alice', 'age': 30}\n"
            "e = {'type': 'admin'}\n"
            "f = {'name': 'Bob'}\n"
        )
        matches = find_pattern("{'type': $TYPE, ...}", str(test_file))
        assert len(matches) == 2
        captured = {m_.captures["TYPE"] for m_ in matches}
        assert captured == {"'user'", "'admin'"}

    def test_partial_dict_with_literal_value(self, tmp_path):
        """'{'type': 'user', ...}' matches dicts with exact key-value plus others."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "d = {'type': 'user', 'name': 'Alice'}\n"
            "e = {'type': 'admin', 'name': 'Bob'}\n"
            "f = {'name': 'Charlie'}\n"
        )
        matches = find_pattern("{'type': 'user', ...}", str(test_file))
        assert len(matches) == 1

    def test_partial_dict_does_not_match_missing_key(self, tmp_path):
        """'{'type': $TYPE, ...}' does NOT match dicts without 'type' key."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "d = {'name': 'Alice', 'age': 30}\n"
        )
        matches = find_pattern("{'type': $TYPE, ...}", str(test_file))
        assert len(matches) == 0

    def test_exact_dict_does_not_match_extra_keys(self, tmp_path):
        """Without ..., exact dict pattern does NOT match dicts with extra keys."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "d = {'name': 'Alice', 'age': 30}\n"
            "e = {'name': 'Bob'}\n"
        )
        matches = find_pattern("{'name': $NAME}", str(test_file))
        # Only matches exact single-key dict
        assert len(matches) == 1
        assert matches[0].captures["NAME"] == "'Bob'"

    def test_partial_dict_multiple_required_keys(self, tmp_path):
        """'{'type': $TYPE, 'name': $NAME, ...}' requires both keys."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "d = {'type': 'user', 'name': 'Alice', 'age': 30}\n"
            "e = {'type': 'admin'}\n"
            "f = {'name': 'Bob', 'type': 'guest', 'active': True}\n"
        )
        matches = find_pattern("{'type': $TYPE, 'name': $NAME, ...}", str(test_file))
        assert len(matches) == 2
        captured_types = {m_.captures["TYPE"] for m_ in matches}
        assert captured_types == {"'user'", "'guest'"}


# ── Feature 10: Chained Comparison Patterns (3.7) ────────────────────────────


class TestChainedComparisonPatterns:
    """Tests for chained comparison patterns like $A < $B < $C."""

    def test_chained_less_than(self, tmp_path):
        """'$A < $B < $C' matches chained less-than comparisons."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 0 < a < 10\n"
            "y = a < b\n"
            "z = 1 < x < 100\n"
        )
        matches = find_pattern("$A < $B < $C", str(test_file))
        assert len(matches) == 2
        captured_bs = {m_.captures["B"] for m_ in matches}
        assert captured_bs == {"a", "x"}

    def test_mixed_operators(self, tmp_path):
        """'$A <= $B < $C' matches mixed chained comparisons."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = 0 <= a < 10\n"
            "y = 0 < a < 10\n"
            "z = 0 <= b <= 100\n"
        )
        matches = find_pattern("$A <= $B < $C", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["A"] == "0"
        assert matches[0].captures["B"] == "a"
        assert matches[0].captures["C"] == "10"

    def test_chained_comparison_replacement(self, tmp_path):
        """Replace chained comparison pattern."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "if 0 < x < 10:\n"
            "    pass\n"
        )
        diff, count = replace_pattern("$A < $B < $C", "$A <= $B <= $C", str(test_file))
        assert count == 1
        assert "0 <= x <= 10" in diff


# ── Feature 11: Walrus Operator in Complex Contexts (3.8) ────────────────────


class TestWalrusPatterns:
    """Tests for walrus operator (:=) patterns in comprehensions and if statements."""

    def test_walrus_in_if_condition(self, tmp_path):
        """'if ($VAR := $EXPR):' matches walrus in if statements."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "if (x := func()):\n"
            "    pass\n"
            "if True:\n"
            "    pass\n"
            "if (result := compute(data)):\n"
            "    use(result)\n"
        )
        matches = find_pattern("if ($VAR := $EXPR):", str(test_file))
        assert len(matches) == 2
        captured_vars = {m_.captures["VAR"] for m_ in matches}
        captured_exprs = {m_.captures["EXPR"] for m_ in matches}
        assert captured_vars == {"x", "result"}
        assert captured_exprs == {"func()", "compute(data)"}

    def test_walrus_in_list_comprehension(self, tmp_path):
        """Walrus in list comprehension if-clause matches correctly."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "result = [x for y in items if (z := f(y))]\n"
            "other = [a for a in items]\n"
        )
        matches = find_pattern(
            "[$X for $VAR in $ITER if ($TARGET := $EXPR)]", str(test_file)
        )
        assert len(matches) == 1
        assert matches[0].captures["X"] == "x"
        assert matches[0].captures["TARGET"] == "z"
        assert matches[0].captures["EXPR"] == "f(y)"

    def test_walrus_in_while(self, tmp_path):
        """Walrus in while condition matches correctly."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "while (chunk := read()):\n"
            "    process(chunk)\n"
            "while True:\n"
            "    pass\n"
        )
        matches = find_pattern("while ($VAR := $EXPR):", str(test_file))
        assert len(matches) == 1
        assert matches[0].captures["VAR"] == "chunk"
        assert matches[0].captures["EXPR"] == "read()"

    def test_standalone_walrus_pattern(self, tmp_path):
        """'($VAR := $EXPR)' matches walrus operator expressions anywhere."""
        test_file = tmp_path / "test.py"
        test_file.write_text(
            "x = (val := compute())\n"
            "if (result := check()):\n"
            "    pass\n"
        )
        matches = find_pattern("($VAR := $EXPR)", str(test_file))
        assert len(matches) == 2
        captured_vars = {m_.captures["VAR"] for m_ in matches}
        assert captured_vars == {"val", "result"}
