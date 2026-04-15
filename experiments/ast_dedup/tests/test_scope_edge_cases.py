"""Scope edge-case micro-corpus for Phase 2's open question.

Tests whether ``PyScopeResolver`` emits consistent qualified names (qns) for:
  1. List comprehension variables (bound names inside comprehension scope)
  2. Walrus operator bindings  (:=)
  3. Nested function definitions with inner locals

For each alpha-equivalent pair the canonical hashes MUST match.
For each intentionally-different pair the canonical hashes MUST differ.

If any equality assertion fails it means PyScopeResolver does NOT produce
stable/consistent qns for that construct — a real finding for Phase 2.
"""

from __future__ import annotations

import pytest

from emend import emend_core

from experiments.ast_dedup.canonicalize import (
    CanonicalizerConfig,
    canonicalize,
    compute_raw_hashes,
    _build_qn_at_and_def_loc,
)


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_canonicalize.py — not imported to avoid
# coupling to their internal structure)
# ---------------------------------------------------------------------------


def _write(tmp_path, name: str, source: str):
    p = tmp_path / name
    p.write_text(source)
    return p


def _get_function_subtree(tmp_path, name: str, source: str, config=None):
    """Canonicalize the first top-level ``function_definition`` in *source*."""
    p = _write(tmp_path, name, source)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    resolver.index_file(str(p), source)
    tree = emend_core.parse_file(str(p))
    assert tree is not None, f"parse failed for {name}"
    refs = resolver.references_in_file(str(p))
    qn_at, def_loc = _build_qn_at_and_def_loc(refs)
    raw = compute_raw_hashes(tree.root)
    func = None
    for child in tree.root.named_children():
        if child.kind == "function_definition":
            func = child
            break
    assert func is not None, f"no function_definition in:\n{source}"
    return canonicalize(func, qn_at, def_loc, str(p), raw, config)


# ---------------------------------------------------------------------------
# 1. List comprehension variables
# ---------------------------------------------------------------------------


def test_listcomp_alpha_equiv_x_vs_y(tmp_path):
    """[x*2 for x in items] and [y*2 for y in items] should be alpha-equivalent."""
    src_a = "def f(items):\n    return [x * 2 for x in items]\n"
    src_b = "def f(items):\n    return [y * 2 for y in items]\n"
    sub_a = _get_function_subtree(tmp_path, "lc_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "lc_b.py", src_b)
    assert sub_a.canonical_hash == sub_b.canonical_hash, (
        "PyScopeResolver does not produce stable qns for comprehension variables: "
        f"token_seq_a={sub_a.token_seq!r} token_seq_b={sub_b.token_seq!r}"
    )


def test_listcomp_alpha_equiv_with_condition(tmp_path):
    """[i for i in seq if i > 0] vs [k for k in seq if k > 0] — alpha-equivalent."""
    src_a = "def f(seq):\n    return [i for i in seq if i > 0]\n"
    src_b = "def f(seq):\n    return [k for k in seq if k > 0]\n"
    sub_a = _get_function_subtree(tmp_path, "lc_cond_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "lc_cond_b.py", src_b)
    assert sub_a.canonical_hash == sub_b.canonical_hash, (
        "Filtered comprehension qns are unstable: "
        f"a={sub_a.token_seq!r} b={sub_b.token_seq!r}"
    )


def test_listcomp_negative_different_body(tmp_path):
    """[x*2 for x in items] vs [x+x for x in items] — different operator, NOT alpha-equivalent."""
    src_a = "def f(items):\n    return [x * 2 for x in items]\n"
    src_b = "def f(items):\n    return [x + x for x in items]\n"
    sub_a = _get_function_subtree(tmp_path, "lc_neg_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "lc_neg_b.py", src_b)
    assert sub_a.canonical_hash != sub_b.canonical_hash, (
        "Different operators/structure should produce different hashes"
    )


# ---------------------------------------------------------------------------
# 2. Walrus operator bindings
# ---------------------------------------------------------------------------


def test_walrus_alpha_equiv_n_vs_m(tmp_path):
    """if (n := len(a)) > 10: print(n) vs if (m := len(a)) > 10: print(m)."""
    src_a = (
        "def f(a):\n"
        "    if (n := len(a)) > 10:\n"
        "        print(n)\n"
        "    return n\n"
    )
    src_b = (
        "def f(a):\n"
        "    if (m := len(a)) > 10:\n"
        "        print(m)\n"
        "    return m\n"
    )
    sub_a = _get_function_subtree(tmp_path, "walrus_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "walrus_b.py", src_b)
    assert sub_a.canonical_hash == sub_b.canonical_hash, (
        "Walrus-bound variables produce unstable qns: "
        f"a={sub_a.token_seq!r} b={sub_b.token_seq!r}"
    )


def test_walrus_alpha_equiv_in_while(tmp_path):
    """while chunk := f.read(8192): … with renamed walrus var."""
    src_a = (
        "def f(fh):\n"
        "    results = []\n"
        "    while chunk := fh.read(8192):\n"
        "        results.append(chunk)\n"
        "    return results\n"
    )
    src_b = (
        "def f(fh):\n"
        "    results = []\n"
        "    while data := fh.read(8192):\n"
        "        results.append(data)\n"
        "    return results\n"
    )
    sub_a = _get_function_subtree(tmp_path, "walrus_while_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "walrus_while_b.py", src_b)
    assert sub_a.canonical_hash == sub_b.canonical_hash, (
        "Walrus-while qns unstable: "
        f"a={sub_a.token_seq!r} b={sub_b.token_seq!r}"
    )


def test_walrus_negative_extra_statement(tmp_path):
    """Walrus with extra logging statement — structurally different, NOT alpha-equivalent."""
    src_a = (
        "def f(a):\n"
        "    if (n := len(a)) > 10:\n"
        "        print(n)\n"
        "    return n\n"
    )
    # Extra statement inside the if-body makes this structurally different.
    src_b = (
        "def f(a):\n"
        "    if (m := len(a)) > 10:\n"
        "        log(m)\n"
        "        print(m)\n"
        "    return m\n"
    )
    sub_a = _get_function_subtree(tmp_path, "walrus_neg_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "walrus_neg_b.py", src_b)
    assert sub_a.canonical_hash != sub_b.canonical_hash, (
        "Extra statement in if-body should produce a different canonical hash"
    )


# ---------------------------------------------------------------------------
# 3. Nested function definitions with inner locals
# ---------------------------------------------------------------------------


def test_nested_func_alpha_equiv_inner_locals(tmp_path):
    """Outer function identical except inner function's local variable name."""
    src_a = (
        "def outer(x):\n"
        "    def inner(y):\n"
        "        result = y + x\n"
        "        return result\n"
        "    return inner\n"
    )
    src_b = (
        "def outer(x):\n"
        "    def inner(z):\n"
        "        tmp = z + x\n"
        "        return tmp\n"
        "    return inner\n"
    )
    sub_a = _get_function_subtree(tmp_path, "nested_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "nested_b.py", src_b)
    assert sub_a.canonical_hash == sub_b.canonical_hash, (
        "Nested function inner-local renaming produces unstable qns: "
        f"a={sub_a.token_seq!r} b={sub_b.token_seq!r}"
    )


def test_nested_func_alpha_equiv_both_renamed(tmp_path):
    """Outer param AND inner locals all renamed — still alpha-equivalent."""
    src_a = (
        "def process(data):\n"
        "    def helper(item):\n"
        "        val = item * 2\n"
        "        return val\n"
        "    return [helper(d) for d in data]\n"
    )
    src_b = (
        "def process(xs):\n"
        "    def helper(el):\n"
        "        out = el * 2\n"
        "        return out\n"
        "    return [helper(x) for x in xs]\n"
    )
    sub_a = _get_function_subtree(tmp_path, "nested_both_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "nested_both_b.py", src_b)
    assert sub_a.canonical_hash == sub_b.canonical_hash, (
        "Fully-renamed nested+comprehension qns are unstable: "
        f"a={sub_a.token_seq!r} b={sub_b.token_seq!r}"
    )


def test_nested_func_negative_structural_difference(tmp_path):
    """Inner function with extra statement — NOT alpha-equivalent."""
    src_a = (
        "def outer(x):\n"
        "    def inner(y):\n"
        "        return y + x\n"
        "    return inner\n"
    )
    src_b = (
        "def outer(x):\n"
        "    def inner(y):\n"
        "        z = y + x\n"
        "        return z\n"
        "    return inner\n"
    )
    sub_a = _get_function_subtree(tmp_path, "nested_neg_a.py", src_a)
    sub_b = _get_function_subtree(tmp_path, "nested_neg_b.py", src_b)
    assert sub_a.canonical_hash != sub_b.canonical_hash, (
        "Structurally different inner functions should have different hashes"
    )
