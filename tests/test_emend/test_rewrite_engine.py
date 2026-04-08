"""Tests for the experimental rewrite/saturation engine (Phase 7)."""

import pytest
import yaml

from emend.rewrite_engine import (
    EGraph,
    ENode,
    RewriteRule,
    SaturationResult,
    UnionFind,
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
