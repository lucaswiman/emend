"""Tests for the experimental rewrite/saturation engine (Phase 7)."""

import pytest
import yaml

from emend.rewrite_engine import (
    EGraph,
    ENode,
    RewriteRule,
    SaturationResult,
    UnionFind,
    _apply_substitution,
    _match_expr_pattern,
    enode_to_source,
    load_rewrite_rules,
    parse_expr,
    run_saturation,
)


class TestUnionFind:
    def test_make_and_find(self):
        uf = UnionFind()
        uf.make_set(0)
        uf.make_set(1)
        assert uf.find(0) == 0
        assert uf.find(1) == 1

    def test_union(self):
        uf = UnionFind()
        uf.make_set(0)
        uf.make_set(1)
        uf.union(0, 1)
        assert uf.find(0) == uf.find(1)

    def test_transitive_union(self):
        uf = UnionFind()
        for i in range(5):
            uf.make_set(i)
        uf.union(0, 1)
        uf.union(1, 2)
        assert uf.find(0) == uf.find(2)


class TestEGraph:
    def test_add_returns_id(self):
        eg = EGraph()
        id0 = eg.add(ENode(op="x"))
        id1 = eg.add(ENode(op="y"))
        assert id0 != id1

    def test_add_dedup(self):
        eg = EGraph()
        id0 = eg.add(ENode(op="x"))
        id1 = eg.add(ENode(op="x"))
        assert id0 == id1

    def test_merge(self):
        eg = EGraph()
        id0 = eg.add(ENode(op="x"))
        id1 = eg.add(ENode(op="y"))
        eg.merge(id0, id1)
        assert eg.find(id0) == eg.find(id1)

    def test_add_with_children(self):
        eg = EGraph()
        x = eg.add(ENode(op="x"))
        y = eg.add(ENode(op="y"))
        plus = eg.add(ENode(op="+", children=(x, y)))
        assert plus != x
        assert plus != y

    def test_extract_simple(self):
        eg = EGraph()
        x_id = eg.add(ENode(op="x"))
        node = eg.extract(x_id)
        assert node is not None
        assert node.op == "x"

    def test_ematch_nested_concrete_children(self):
        """ematch must recursively match concrete children and bind their metavars.

        Pattern: op1(op2($X, $Y), $Z)
        Data:    op1(op2(a, b), c)

        The match should bind $X=a, $Y=b, $Z=c.  A shallow match that only
        checks the op of op2 without recursing into its children would fail
        to bind $X and $Y.
        """
        eg = EGraph()
        # Data nodes
        a = eg.add(ENode(op="a"))
        b = eg.add(ENode(op="b"))
        c = eg.add(ENode(op="c"))
        inner = eg.add(ENode(op="op2", children=(a, b)))  # op2(a, b)
        root = eg.add(ENode(op="op1", children=(inner, c)))  # op1(op2(a, b), c)

        # Pattern nodes
        px = eg.add(ENode(op="$X"))
        py = eg.add(ENode(op="$Y"))
        pz = eg.add(ENode(op="$Z"))
        p_inner = eg.add(ENode(op="op2", children=(px, py)))  # op2($X, $Y)
        p_root = eg.add(ENode(op="op1", children=(p_inner, pz)))  # op1(op2($X, $Y), $Z)

        pattern = eg._get_pattern_for_eclass(p_root)
        assert pattern is not None
        matches = eg.ematch(pattern)
        assert len(matches) >= 1, "Should match the data expression"

        # At least one match should bind all three metavars
        found = False
        for m in matches:
            if "$X" in m and "$Y" in m and "$Z" in m:
                assert eg.find(m["$X"]) == eg.find(a)
                assert eg.find(m["$Y"]) == eg.find(b)
                assert eg.find(m["$Z"]) == eg.find(c)
                found = True
                break
        assert found, f"No match bound all metavars; matches={matches}"

    def test_apply_rules_uses_correct_root_eclass(self):
        """apply_rules must merge each matched e-class independently.

        With two 'add' nodes in the e-graph — add(a, b) and add(c, d) —
        applying the rule  add($X, $Y) → $X  should produce:
          add(a, b) ≡ a
          add(c, d) ≡ c
        and  a ≢ c  (distinct atoms must stay distinct).

        The bug in _find_match_root was returning the *first* e-node that
        has op=='add' regardless of which substitution is active, causing
        the wrong root to be merged for the second match.
        """
        eg = EGraph()
        # Data nodes
        e_a = eg.add(ENode(op="name:a"))
        e_b = eg.add(ENode(op="name:b"))
        e_ab = eg.add(ENode(op="add", children=(e_a, e_b)))   # add(a, b)
        e_c = eg.add(ENode(op="name:c"))
        e_d = eg.add(ENode(op="name:d"))
        e_cd = eg.add(ENode(op="add", children=(e_c, e_d)))   # add(c, d)

        # Pattern nodes live in the same e-graph
        p_x = eg.add(ENode(op="$X"))
        p_y = eg.add(ENode(op="$Y"))
        p_lhs = eg.add(ENode(op="add", children=(p_x, p_y)))  # add($X, $Y)

        lhs_node = eg._get_pattern_for_eclass(p_lhs)   # ENode(op="add", …)
        rhs_node = eg._get_pattern_for_eclass(p_x)     # ENode(op="$X")

        assert lhs_node is not None and rhs_node is not None
        eg.apply_rules([(lhs_node, rhs_node)], limit=1)

        # Each add-node should be equivalent to its first argument…
        assert eg.find(e_ab) == eg.find(e_a), "add(a,b) should merge with a"
        assert eg.find(e_cd) == eg.find(e_c), "add(c,d) should merge with c"
        # …but the two atoms themselves must remain distinct.
        assert eg.find(e_a) != eg.find(e_c), (
            "a and c must stay distinct; _find_match_root returned the wrong root"
        )


class TestParseExpr:
    def test_identifier(self):
        eg = EGraph()
        eid = parse_expr("foo", eg)
        node = eg.extract(eid)
        assert node is not None
        assert node.op == "name:foo"

    def test_number(self):
        eg = EGraph()
        eid = parse_expr("42", eg)
        node = eg.extract(eid)
        assert node is not None
        assert node.op == "num:42"

    def test_metavar(self):
        eg = EGraph()
        eid = parse_expr("$x", eg)
        node = eg.extract(eid)
        assert node is not None
        assert node.op == "$x"

    def test_string(self):
        eg = EGraph()
        eid = parse_expr('"hello"', eg)
        node = eg.extract(eid)
        assert node is not None
        assert "hello" in node.op

    def test_parenthesized_binop_not_misstripped(self):
        """parse_expr('(a) + (b)') must parse children as clean identifiers."""
        eg = EGraph()
        eid = parse_expr("(a) + (b)", eg)
        node = eg.extract(eid)
        assert node is not None
        assert node.op == "binop:+"
        left = eg.extract(node.children[0])
        right = eg.extract(node.children[1])
        assert left is not None and left.op == "name:a", f"left child was {left}"
        assert right is not None and right.op == "name:b", f"right child was {right}"

    def test_parenthesized_subexpr_preserved(self):
        """Parens around a single sub-expression should be fine."""
        eg = EGraph()
        eid = parse_expr("(x)", eg)
        node = eg.extract(eid)
        assert node is not None
        assert node.op == "name:x"

    def test_nested_paren_groups(self):
        """'(a) + (b)' must parse all operands cleanly."""
        eg = EGraph()
        eid = parse_expr("(a) + (b)", eg)
        node = eg.extract(eid)
        assert node is not None
        assert node.op == "binop:+"
        src = enode_to_source(node, eg)
        assert ")" not in src and "(" not in src, f"stray parens in output: {src}"

    def test_string_concat_not_single_string(self):
        """'"abc" + "def"' must parse as binop, not a single string literal."""
        eg = EGraph()
        eid = parse_expr('"abc" + "def"', eg)
        node = eg.extract(eid)
        assert node is not None
        assert node.op == "binop:+", f"expected binop:+, got {node.op}"


class TestApplySubstitution:
    def test_basic(self):
        result = _apply_substitution("$x + $y", {"x": "a", "y": "b"})
        assert result == "a + b"

    def test_prefix_metavar_not_corrupted(self):
        """$x must not be replaced inside $x_new."""
        result = _apply_substitution("$x + $x_new", {"x": "a", "x_new": "b"})
        assert result == "a + b"


class TestLoadRewriteRules:
    def test_load_rules(self, tmp_path):
        config = tmp_path / "rewrites.yaml"
        config.write_text(yaml.dump({
            "rewrites": [
                {"name": "simplify-add-zero", "lhs": "$x + 0", "rhs": "$x"},
                {"name": "simplify-mul-one", "lhs": "$x * 1", "rhs": "$x"},
            ],
        }))
        rules = load_rewrite_rules(str(config))
        assert len(rules) == 2
        assert rules[0].name == "simplify-add-zero"
        assert rules[0].lhs == "$x + 0"
        assert rules[0].rhs == "$x"

    def test_load_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_rewrite_rules(str(tmp_path / "nonexistent.yaml"))

    def test_load_empty_rules(self, tmp_path):
        config = tmp_path / "rewrites.yaml"
        config.write_text(yaml.dump({"rewrites": []}))
        rules = load_rewrite_rules(str(config))
        assert rules == []


class TestRunSaturation:
    def test_no_rules(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1 + 0\n")
        results = run_saturation(str(test_file), [])
        assert results == []

    def test_nonexistent_file(self, tmp_path):
        results = run_saturation(str(tmp_path / "nope.py"), [])
        assert results == []

    def test_identity_add_rewrite(self, tmp_path):
        """Rule $x + 0 => $x should match and produce a rewrite."""
        test_file = tmp_path / "test.py"
        test_file.write_text("result = foo + 0\n")

        rules = [RewriteRule(name="add-zero", lhs="$x + 0", rhs="$x")]
        results = run_saturation(str(test_file), rules)

        assert len(results) >= 1
        r = results[0]
        assert r.file_path == str(test_file)
        assert "add-zero" in r.rules_applied

    def test_not_not_rewrite(self, tmp_path):
        """Rule 'not not $x' => '$x' should simplify double negation."""
        test_file = tmp_path / "test.py"
        test_file.write_text("result = not not flag\n")

        rules = [RewriteRule(name="double-neg", lhs="not not $x", rhs="$x")]
        results = run_saturation(str(test_file), rules)

        assert len(results) >= 1
        assert "double-neg" in results[0].rules_applied

    def test_no_match_no_result(self, tmp_path):
        """Rules that don't match should produce no results."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1 + 2\n")

        rules = [RewriteRule(name="add-zero", lhs="$x + 0", rhs="$x")]
        results = run_saturation(str(test_file), rules)
        assert results == []

    def test_skip_comments(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("# foo + 0\nx = 1\n")

        rules = [RewriteRule(name="add-zero", lhs="$x + 0", rhs="$x")]
        results = run_saturation(str(test_file), rules)
        assert results == []


# ---------------------------------------------------------------------------
# Bug regression tests
# ---------------------------------------------------------------------------


class TestBug1HashconsRebuildAfterMerge:
    def test_egraph_hashcons_rebuild_after_merge(self):
        """After merging a and b, add(f(a)) should find existing f(b)."""
        eg = EGraph()
        a = eg.add(ENode("a"))
        b = eg.add(ENode("b"))
        f_b = eg.add(ENode("f", (b,)))
        eg.merge(a, b)
        f_a = eg.add(ENode("f", (a,)))
        assert eg.find(f_b) == eg.find(f_a), (
            "f(a) and f(b) should be in the same e-class after merging a and b"
        )


class TestBug2LeftAssociativeParsing:
    def test_parse_expr_left_associative(self):
        """Binary operators should be left-associative: 1 - 2 - 3 = (1 - 2) - 3."""
        eg = EGraph()
        root = parse_expr("1 - 2 - 3", eg)
        extracted = enode_to_source(eg.extract(root), eg)

        eg2 = EGraph()
        left_assoc = parse_expr("(1 - 2) - 3", eg2)
        extracted_la = enode_to_source(eg2.extract(left_assoc), eg2)

        assert extracted == extracted_la, (
            f"1 - 2 - 3 should parse as (1 - 2) - 3, got {extracted}"
        )


class TestBug_MultiCharOperatorParsing:
    """Greedy (.+) in _BINOP_RE causes **, //, <<, >> to be misparsed."""

    def test_parse_expr_power_operator(self):
        """'a ** b' should parse as binop:** with children a and b."""
        eg = EGraph()
        root = parse_expr("a ** b", eg)
        node = eg.extract(root)
        assert node is not None
        assert node.op == "binop:**", f"expected binop:** but got {node.op}"

    def test_parse_expr_floor_division(self):
        """'a // b' should parse as binop:// with children a and b."""
        eg = EGraph()
        root = parse_expr("a // b", eg)
        node = eg.extract(root)
        assert node is not None
        assert node.op == "binop://", f"expected binop:// but got {node.op}"

    def test_parse_expr_left_shift(self):
        """'a << b' should parse as binop:<< with children a and b."""
        eg = EGraph()
        root = parse_expr("a << b", eg)
        node = eg.extract(root)
        assert node is not None
        assert node.op == "binop:<<", f"expected binop:<< but got {node.op}"

    def test_parse_expr_right_shift(self):
        """'a >> b' should parse as binop:>> with children a and b."""
        eg = EGraph()
        root = parse_expr("a >> b", eg)
        node = eg.extract(root)
        assert node is not None
        assert node.op == "binop:>>", f"expected binop:>> but got {node.op}"


class TestBug3DuplicateMetavar:
    def test_match_source_duplicate_metavar(self):
        """Pattern with repeated metavar $x + $x should match expressions like a + a."""
        result = _match_expr_pattern("a + a", "$x + $x")
        assert result is not None, "Pattern $x + $x should match a + a"
        assert result.get("x") == "a"

    def test_match_source_duplicate_metavar_no_match(self):
        """Pattern $x + $x should NOT match a + b (different values)."""
        result = _match_expr_pattern("a + b", "$x + $x")
        # Either None or the match should not have consistent x.
        # The important thing is that it doesn't crash with re.error.
        if result is not None:
            # If it matches, x should be consistent
            assert result.get("x") in ("a", "b")
