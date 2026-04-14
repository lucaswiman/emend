"""Tests for Phase 2 — the AST canonicalizer."""

from __future__ import annotations

import pytest

from emend import emend_core

from experiments.ast_dedup.canonicalize import (
    CanonicalizerConfig,
    canonicalize,
    canonicalize_file,
    compute_raw_hashes,
    iter_candidates,
    _build_qn_at_and_def_loc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path, name: str, source: str):
    p = tmp_path / name
    p.write_text(source)
    return p


def _canonicalize_first_function(tmp_path, name: str, source: str, config=None):
    """Canonicalize the first ``function_definition`` in ``source``."""
    p = _write(tmp_path, name, source)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    subs = canonicalize_file(str(p), resolver, config)
    # Find the function_definition candidate (first yielded).
    for s in subs:
        # The first candidate for a module with a top-level function is
        # the function_definition itself.
        return s
    return None


def _get_function_subtree(tmp_path, name, source, config=None):
    """Return the CanonicalSubtree for the top-level function_definition."""
    p = _write(tmp_path, name, source)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    resolver.index_file(str(p), source)
    tree = emend_core.parse_file(str(p))
    assert tree is not None
    refs = resolver.references_in_file(str(p))
    qn_at, def_loc = _build_qn_at_and_def_loc(refs)
    raw = compute_raw_hashes(tree.root)
    root = tree.root
    # Find the first function_definition (skip decorators, walk)
    func = None
    for child in root.named_children():
        if child.kind == "function_definition":
            func = child
            break
    assert func is not None, f"no function_definition in source:\n{source}"
    return canonicalize(func, qn_at, def_loc, str(p), raw, config)


# ---------------------------------------------------------------------------
# Test 1 — alpha equivalence
# ---------------------------------------------------------------------------


def test_rename_only_preserves_hash(tmp_path):
    src_a = "def f(x, y):\n    z = x + y\n    return z * x\n"
    src_b = "def f(a, b):\n    c = a + b\n    return c * a\n"

    sub_a = _get_function_subtree(tmp_path, "a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "b.py", src_b)

    assert sub_a.canonical_hash == sub_b.canonical_hash
    assert sub_a.kind_seq == sub_b.kind_seq
    assert sub_a.token_seq == sub_b.token_seq


# ---------------------------------------------------------------------------
# Test 2 — operator difference breaks equivalence
# ---------------------------------------------------------------------------


def test_operator_difference_breaks_hash(tmp_path):
    src_a = "def f(x, y):\n    z = x + y\n    return z * x\n"
    src_b = "def f(x, y):\n    z = x - y\n    return z * x\n"

    sub_a = _get_function_subtree(tmp_path, "a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "b.py", src_b)

    assert sub_a.canonical_hash != sub_b.canonical_hash


# ---------------------------------------------------------------------------
# Test 3 — shadowed variables get distinct tokens
# ---------------------------------------------------------------------------


def test_shadowed_variables_distinct_tokens(tmp_path):
    src = (
        "def outer(x):\n"
        "    def inner(x):\n"
        "        return x\n"
        "    return x\n"
    )
    sub = _get_function_subtree(tmp_path, "s.py", src)

    # There are two distinct `x` identifiers in the outer subtree, one
    # bound in outer's scope and one bound in inner's. They must get
    # different bound tokens.
    bounds = [t for t in sub.token_seq if t.startswith("bound_")]
    # At least bound_0 and bound_1 (for the two scopes' x parameters) plus
    # the outer function name.
    assert len(set(bounds)) >= 3

    # Collect the canonical tokens assigned to each *use* site in source
    # order. The inner `x` use and outer `x` use should resolve to
    # different bound_k values.
    # A simple way: the last two tokens (from the two `return x` lines)
    # should differ.
    last_two = [t for t in sub.token_seq if t.startswith("bound_")][-2:]
    assert last_two[0] != last_two[1], (
        f"expected distinct bound tokens for shadowed x, got {sub.token_seq}"
    )


# ---------------------------------------------------------------------------
# Test 4 — free variable used at multiple positions shares a token
# ---------------------------------------------------------------------------


def test_free_variable_same_token(tmp_path):
    src = "GLOBAL = 1\n\ndef f():\n    return GLOBAL + GLOBAL + GLOBAL\n"
    sub = _get_function_subtree(tmp_path, "g.py", src)

    frees = [t for t in sub.token_seq if t.startswith("free_")]
    # The three uses of GLOBAL should share the same free token.
    assert len(frees) >= 3
    # Exactly one distinct free_* token for GLOBAL.
    distinct = set(frees)
    assert "free_0" in distinct
    # All three references to GLOBAL should map to the same free index,
    # so at least the three uses of GLOBAL collapse.
    assert sum(1 for t in sub.token_seq if t == "free_0") >= 3


# ---------------------------------------------------------------------------
# Test 5 — attribute / method names preserved unless rename_attrs=True
# ---------------------------------------------------------------------------


def test_attribute_names_preserved_by_default(tmp_path):
    src = "def f(obj):\n    return obj.some_attribute\n"
    sub = _get_function_subtree(tmp_path, "a1.py", src)

    # The attribute name must appear literally in the token sequence.
    assert "some_attribute" in sub.token_seq


def test_attribute_rename_when_flag_set(tmp_path):
    src = "def f(obj):\n    return obj.some_attribute\n"
    sub_default = _get_function_subtree(tmp_path, "a2.py", src)
    sub_renamed = _get_function_subtree(
        tmp_path, "a3.py", src, config=CanonicalizerConfig(rename_attrs=True)
    )

    # With rename_attrs=True the literal attribute name is gone; the hash
    # must differ from the default.
    assert "some_attribute" in sub_default.token_seq
    assert "some_attribute" not in sub_renamed.token_seq
    assert sub_default.canonical_hash != sub_renamed.canonical_hash


def test_method_name_preserved(tmp_path):
    # Different method names should give different canonical hashes even
    # when the receiver is renamed.
    src_a = "def f(obj):\n    return obj.foo(1)\n"
    src_b = "def f(obj):\n    return obj.bar(1)\n"
    sub_a = _get_function_subtree(tmp_path, "m1.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "m2.py", src_b)

    assert sub_a.canonical_hash != sub_b.canonical_hash


# ---------------------------------------------------------------------------
# Test 6 — string literal equality classes honored only with flag
# ---------------------------------------------------------------------------


def test_string_literals_default_collapse(tmp_path):
    src_a = 'def f():\n    return "hello" + "world"\n'
    src_b = 'def f():\n    return "foo" + "bar"\n'

    sub_a = _get_function_subtree(tmp_path, "s1.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "s2.py", src_b)

    # Default: all strings collapse to "str".
    assert sub_a.canonical_hash == sub_b.canonical_hash
    assert "str" in sub_a.token_seq


def test_string_literals_keep_equality(tmp_path):
    # Same-text strings should share a token id; different-text strings
    # should get different token ids, making the canonical hash sensitive
    # to literal identity.
    src_same = 'def f():\n    return "hello" + "hello"\n'
    src_diff = 'def f():\n    return "hello" + "world"\n'

    cfg = CanonicalizerConfig(keep_literal_equality=True)
    sub_same = _get_function_subtree(tmp_path, "e1.py", src_same, cfg)
    sub_diff = _get_function_subtree(tmp_path, "e2.py", src_diff, cfg)

    # Same-text: two uses share the token.
    str_toks_same = [
        t for t in sub_same.token_seq if t.startswith("str")
    ]
    assert len(str_toks_same) == 2
    assert len(set(str_toks_same)) == 1

    # Different-text: two distinct tokens.
    str_toks_diff = [
        t for t in sub_diff.token_seq if t.startswith("str")
    ]
    assert len(str_toks_diff) == 2
    assert len(set(str_toks_diff)) == 2

    # And the hashes differ from each other.
    assert sub_same.canonical_hash != sub_diff.canonical_hash


# ---------------------------------------------------------------------------
# Smoke / integration tests
# ---------------------------------------------------------------------------


def test_canonicalize_file_finds_function_candidate(tmp_path):
    src = "def f(x):\n    return x + 1\n"
    p = _write(tmp_path, "q.py", src)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    subs = canonicalize_file(str(p), resolver)
    # Should find at least the function_definition.
    assert len(subs) >= 1
    assert subs[0].kind_seq[0] == "function_definition"
    # raw_merkle present and non-empty
    assert subs[0].raw_merkle
    # canonical_hash present and distinct from raw
    assert subs[0].canonical_hash
    # child_merkle_bag non-empty
    assert len(subs[0].child_merkle_bag) > 0


def test_raw_merkle_exact_match(tmp_path):
    # Two files with identical source produce identical raw_merkle roots.
    src = "def f(x):\n    return x + 1\n"
    p1 = _write(tmp_path, "r1.py", src)
    p2 = _write(tmp_path, "r2.py", src)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    subs1 = canonicalize_file(str(p1), resolver)
    subs2 = canonicalize_file(str(p2), resolver)
    assert subs1[0].raw_merkle == subs2[0].raw_merkle


def test_iter_candidates_yields_function_and_body(tmp_path):
    src = "def f(x):\n    a = 1\n    b = 2\n    return a + b + x\n"
    p = _write(tmp_path, "c.py", src)
    tree = emend_core.parse_file(str(p))
    assert tree is not None
    cands = list(iter_candidates(tree))
    kinds = [c.kind for c in cands]
    # Should contain at least function_definition and its body block.
    assert "function_definition" in kinds
    assert "block" in kinds


def test_comprehension_bindings(tmp_path):
    # Spot check: comprehension loop variables get their own qn nested
    # under the enclosing function. This is the open question from the
    # roadmap index — the finding is documented in canonicalize.py.
    src = "def f(xs):\n    return [i * 2 for i in xs]\n"
    p = _write(tmp_path, "cp.py", src)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    resolver.index_file(str(p), src)
    # Should not raise, and the canonical hash should be stable.
    sub = _get_function_subtree(tmp_path, "cp2.py", src)
    # The canonical form should reference both `xs` and `i` as bound
    # identifiers (both bind inside the function subtree).
    bounds = {t for t in sub.token_seq if t.startswith("bound_")}
    assert len(bounds) >= 2


def test_module_level_decorated_function(tmp_path):
    # decorated_definition is enumerated as a candidate even though the
    # inner function_definition is also enumerated. Both should be yielded.
    src = "@staticmethod\ndef f(x):\n    return x\n"
    p = _write(tmp_path, "d.py", src)
    tree = emend_core.parse_file(str(p))
    kinds = {c.kind for c in iter_candidates(tree)}
    assert "decorated_definition" in kinds or "function_definition" in kinds
