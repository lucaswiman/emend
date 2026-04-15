"""Tests for Phase 4 — triviality filters.

Six test cases per the spec:
  1. size_floor drops a 3-node subtree.
  2. token_diversity drops a low-diversity token subtree.
  3. halstead_lite drops a low-vocab subtree (documents behavior).
  4. stereotyped_dunder drops a trivial __init__ assigning 5 params.
  5. identity_pattern drops a trivial return-single-identifier pattern.
  6. A real non-trivial function from src/emend/ is NOT dropped.
"""

from __future__ import annotations

import math
import os
import tempfile

import pytest

from emend import emend_core
from experiments.ast_dedup.canonicalize import (
    PYTHON_KEYWORDS,
    CanonicalSubtree,
    canonicalize_file,
)
from experiments.ast_dedup.filter import (
    FilterConfig,
    FilterPipeline,
    FilterVerdict,
    default_pipeline,
    depth_floor,
    halstead_lite,
    identity_pattern,
    root_kind_blocklist,
    size_floor,
    stereotyped_dunder,
    token_diversity,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal CanonicalSubtree directly (no tree-sitter needed)
# ---------------------------------------------------------------------------

def _make_sub(
    *,
    kind_seq: tuple[str, ...] = (),
    token_seq: tuple[str, ...] = (),
    depth: int = 1,
    node_count: int | None = None,
) -> CanonicalSubtree:
    """Build a minimal CanonicalSubtree for unit tests."""
    nc = node_count if node_count is not None else len(kind_seq)
    unique_toks = len(set(token_seq))
    unique_non_kw = len(set(token_seq) - PYTHON_KEYWORDS)
    hist = tuple(
        sorted((k, kind_seq.count(k)) for k in set(kind_seq))
    )
    return CanonicalSubtree(
        file="test.py",
        start_byte=0,
        end_byte=100,
        start_line=0,
        end_line=depth,
        kind_seq=kind_seq,
        token_seq=token_seq,
        depth=depth,
        node_count=nc,
        raw_merkle=b"",
        canonical_hash=b"",
        unique_tokens=unique_toks,
        unique_non_keyword_tokens=unique_non_kw,
        kind_histogram=hist,
        child_merkle_bag=(),
    )


def _write(tmp_path: str, name: str, source: str) -> str:
    p = os.path.join(tmp_path, name)
    with open(p, "w") as fh:
        fh.write(source)
    return p


# ---------------------------------------------------------------------------
# Test 1 — size_floor drops a 3-node subtree
# ---------------------------------------------------------------------------


def test_size_floor_rejects_tiny_subtree():
    """size_floor rejects a subtree with node_count=3 (< min=8)."""
    sub = _make_sub(
        kind_seq=("return_statement", "identifier"),
        token_seq=("return", "bound_0"),
        depth=2,
        node_count=3,
    )
    cfg = FilterConfig()
    verdict = size_floor(sub, cfg)
    assert not verdict.accept
    assert "node_count=3" in verdict.reason


def test_size_floor_accepts_large_enough_subtree():
    """size_floor accepts a subtree at exactly the minimum threshold."""
    sub = _make_sub(
        kind_seq=("block",) + ("expression_statement",) * 7,
        token_seq=("bound_0",) * 4,
        depth=3,
        node_count=8,
    )
    cfg = FilterConfig()
    verdict = size_floor(sub, cfg)
    assert verdict.accept


# ---------------------------------------------------------------------------
# Test 2 — token_diversity drops a low-diversity subtree
# ---------------------------------------------------------------------------


def test_token_diversity_rejects_low_diversity():
    """token_diversity rejects a subtree with only 'self', 'x', and a bound var."""
    # 'self' is in PYTHON_KEYWORDS, 'x' and 'bound_0' are the only non-keywords.
    # unique_non_kw = {'x', 'bound_0'} = 2, below min=4.
    sub = _make_sub(
        kind_seq=("assignment", "attribute", "identifier", "identifier") * 3,
        token_seq=("self", "x", "bound_0") * 4,
        depth=3,
        node_count=12,
    )
    cfg = FilterConfig()
    verdict = token_diversity(sub, cfg)
    assert not verdict.accept
    assert "unique_non_keyword=" in verdict.reason
    # Confirm the computation: unique non-keyword tokens are 'x' and 'bound_0'
    unique_non_kw = len({"self", "x", "bound_0"} - PYTHON_KEYWORDS)
    assert unique_non_kw < cfg.min_unique_non_keyword


def test_token_diversity_accepts_diverse_tokens():
    """token_diversity accepts a subtree with sufficient token variety."""
    sub = _make_sub(
        kind_seq=("function_definition", "block", "if_statement", "assignment") * 3,
        token_seq=("bound_0", "bound_1", "free_0", "free_1", "alpha", "beta") * 2,
        depth=5,
        node_count=20,
    )
    cfg = FilterConfig()
    assert token_diversity(sub, cfg).accept


# ---------------------------------------------------------------------------
# Test 3 — halstead_lite drops a long but low-vocab subtree
# ---------------------------------------------------------------------------


def test_halstead_lite_rejects_low_vocab():
    """halstead_lite rejects a subtree with high node_count but tiny vocabulary.

    NOTE: This test documents the filter's behavior, not a design prescription.
    A repetitive block (e.g. 10 identical assignments) has high node_count but
    very low token/kind vocabulary, so the Halstead-proxy volume falls below
    the threshold. Whether such a block is *truly* uninteresting is debatable;
    the filter errs on the side of discarding it.
    """
    # 10 nodes, kind_vocab=1 ("block"), token_vocab=1 → vocab=2
    # volume = 10 * log2(2) = 10.0 < 30.0
    sub = _make_sub(
        kind_seq=("block",) * 10,
        token_seq=("bound_0",) * 5,
        depth=3,
        node_count=10,
    )
    cfg = FilterConfig()
    n = sub.node_count
    vocab = len(set(sub.kind_seq)) + len(set(sub.token_seq))
    volume = n * math.log2(max(vocab, 2))
    assert volume < cfg.halstead_volume_min, (
        f"Expected volume {volume:.1f} < {cfg.halstead_volume_min}"
    )
    verdict = halstead_lite(sub, cfg)
    assert not verdict.accept
    assert "halstead_volume=" in verdict.reason


def test_halstead_lite_accepts_high_vocab():
    """halstead_lite accepts a subtree with sufficient vocabulary diversity."""
    # Many distinct kinds and tokens → high vocabulary → high volume.
    sub = _make_sub(
        kind_seq=(
            "function_definition", "block", "if_statement",
            "assignment", "attribute", "identifier",
            "call", "argument_list", "return_statement",
            "comparison_operator",
        ),
        token_seq=(
            "bound_0", "bound_1", "free_0", "free_1",
            "alpha", "beta", "gamma", "delta",
        ),
        depth=6,
        node_count=20,
    )
    cfg = FilterConfig()
    assert halstead_lite(sub, cfg).accept


# ---------------------------------------------------------------------------
# Test 4 — stereotyped_dunder drops a trivial __init__ assigning 5 params
# ---------------------------------------------------------------------------


def test_stereotyped_dunder_rejects_trivial_init(tmp_path):
    """stereotyped_dunder rejects an __init__ that only does self.x = param."""
    source = (
        "class MyClass:\n"
        "    def __init__(self, a, b, c, d, e):\n"
        "        self.a = a\n"
        "        self.b = b\n"
        "        self.c = c\n"
        "        self.d = d\n"
        "        self.e = e\n"
    )
    p = _write(str(tmp_path), "init5.py", source)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    subs = canonicalize_file(p, resolver)

    cfg = FilterConfig()
    # Find the function_definition subtree (the __init__ itself).
    func_subs = [s for s in subs if s.kind_seq[0] == "function_definition"]
    assert func_subs, "Expected a function_definition candidate"

    func_sub = func_subs[0]
    verdict = stereotyped_dunder(func_sub, cfg)
    assert not verdict.accept, (
        f"Expected __init__ to be rejected; got accept=True. "
        f"kind_seq={func_sub.kind_seq}, token_seq={func_sub.token_seq}"
    )
    assert "stereotyped_dunder:__init__" in verdict.reason


def test_stereotyped_dunder_accepts_init_with_logic(tmp_path):
    """stereotyped_dunder does NOT reject an __init__ with conditional logic."""
    source = (
        "class MyClass:\n"
        "    def __init__(self, a, b):\n"
        "        self.a = a\n"
        "        if a > 0:\n"
        "            self.b = b\n"
        "        else:\n"
        "            self.b = None\n"
    )
    p = _write(str(tmp_path), "init_logic.py", source)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    subs = canonicalize_file(p, resolver)

    cfg = FilterConfig()
    func_subs = [s for s in subs if s.kind_seq[0] == "function_definition"]
    assert func_subs

    # None of the function_definition subtrees should be rejected as trivial init.
    for func_sub in func_subs:
        v = stereotyped_dunder(func_sub, cfg)
        assert v.accept, (
            f"Non-trivial __init__ was incorrectly rejected: {v.reason}"
        )


# ---------------------------------------------------------------------------
# Test 5 — identity_pattern drops trivial identity-like patterns
# ---------------------------------------------------------------------------


def test_identity_pattern_rejects_return_ident():
    """identity_pattern rejects a subtree whose token shape is ('return', '<id>')."""
    # Construct a subtree whose token_seq canonicalizes to the shape
    # ("return", "<id>"), which matches the _IDENTITY_SHAPES set.
    sub = _make_sub(
        kind_seq=("return_statement", "identifier"),
        token_seq=("return", "bound_0"),
        depth=2,
        node_count=8,  # above size_floor so we're testing identity_pattern alone
    )
    cfg = FilterConfig()
    verdict = identity_pattern(sub, cfg)
    assert not verdict.accept
    assert "identity_pattern" in verdict.reason


def test_identity_pattern_rejects_assignment():
    """identity_pattern rejects token shape ('<id>', '=', '<id>')."""
    sub = _make_sub(
        kind_seq=("assignment", "identifier", "identifier"),
        token_seq=("bound_0", "=", "free_0"),
        depth=3,
        node_count=8,
    )
    cfg = FilterConfig()
    verdict = identity_pattern(sub, cfg)
    assert not verdict.accept


def test_identity_pattern_rejects_two_ident_shape():
    """identity_pattern rejects token shape ('<id>', '<id>') — trivial call/access.

    The shape ``("<id>", "<id>")`` matches when both token_seq elements are
    canonicalized identifiers (bound_N or free_N). This covers patterns like
    ``a(b)`` (a one-argument call) where the function name and argument are both
    scope-resolved. Attribute names are preserved literally and do NOT map to
    ``<id>``, so ``a.name`` gives shape ``("<id>", "name")``, which is not in
    the identity shape set.

    Note: in real canonicalized subtrees, the ``return`` keyword is an anonymous
    node and does NOT appear in token_seq. The ``("return", "<id>")`` shape only
    matches when constructing a CanonicalSubtree directly (as in
    test_identity_pattern_rejects_return_ident above).
    """
    sub = _make_sub(
        kind_seq=("call", "identifier", "argument_list", "identifier"),
        token_seq=("bound_0", "free_0"),  # f(x): both identifiers are canonicalized
        depth=3,
        node_count=8,
    )
    cfg = FilterConfig()
    verdict = identity_pattern(sub, cfg)
    assert not verdict.accept


def test_identity_pattern_disabled_when_flag_off():
    """identity_pattern passes everything when block_trivial_patterns=False."""
    sub = _make_sub(
        kind_seq=("return_statement", "identifier"),
        token_seq=("return", "bound_0"),
        depth=2,
        node_count=8,
    )
    cfg = FilterConfig(block_trivial_patterns=False)
    verdict = identity_pattern(sub, cfg)
    assert verdict.accept


def test_pipeline_rejects_trivial_function(tmp_path):
    """The default pipeline rejects 'def f(x): return x' (via size_floor)."""
    source = "def f(x):\n    return x\n"
    p = _write(str(tmp_path), "identity.py", source)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    subs = canonicalize_file(p, resolver)

    cfg = FilterConfig()
    pipe = default_pipeline(cfg)
    assert subs, "Expected at least one candidate"
    # The function_definition has node_count=7 < min=8, so size_floor catches it.
    for sub in subs:
        verdict = pipe.run(sub)
        assert not verdict.accept, (
            f"Expected 'def f(x): return x' to be rejected; "
            f"kind={sub.kind_seq[0]!r}, node_count={sub.node_count}"
        )


# ---------------------------------------------------------------------------
# Test 6 — a real non-trivial function from src/emend/ is NOT dropped
# ---------------------------------------------------------------------------


def test_real_nontrivial_function_is_accepted():
    """_resolve_cache_root from transform.py passes all filters.

    This function has complex logic (if/else, try/except, string operations)
    and a large node_count, so it should not be filtered by any of the
    triviality checks.

    Pinning this test ensures our filters don't accidentally drop real
    refactoring candidates.
    """
    src_path = os.path.join(
        os.path.dirname(__file__), "../../../src/emend/transform.py"
    )
    src_path = os.path.normpath(src_path)
    assert os.path.exists(src_path), f"Could not find {src_path}"

    project_root = os.path.dirname(src_path)
    resolver = emend_core.PyScopeResolver(project_root, "py")
    subs = canonicalize_file(src_path, resolver)

    cfg = FilterConfig()
    pipe = default_pipeline(cfg)

    # Find the _resolve_cache_root function_definition subtree.
    # It starts at line 43 (0-indexed: 42) in the file.
    target_subs = [
        s for s in subs
        if s.kind_seq[0] == "function_definition" and s.start_line == 42
    ]
    assert target_subs, (
        "Could not find _resolve_cache_root function_definition "
        "(expected at 0-indexed line 42). "
        "Available function_definition start lines: "
        + str([s.start_line for s in subs if s.kind_seq[0] == "function_definition"][:10])
    )

    target = target_subs[0]
    verdict = pipe.run(target)
    assert verdict.accept, (
        f"Non-trivial function _resolve_cache_root was unexpectedly rejected "
        f"by filter: {verdict.reason!r}. "
        f"node_count={target.node_count}, depth={target.depth}"
    )
    # Sanity checks: confirm it's the right function (large and deep).
    assert target.node_count > 50, (
        f"Expected node_count > 50 for _resolve_cache_root, got {target.node_count}"
    )
    assert target.depth > 10, (
        f"Expected depth > 10, got {target.depth}"
    )


# ---------------------------------------------------------------------------
# FilterPipeline statistics tests
# ---------------------------------------------------------------------------


def test_pipeline_removal_counts():
    """FilterPipeline tracks rejection counts per filter."""
    cfg = FilterConfig()
    pipe = default_pipeline(cfg)

    # Tiny subtree rejected by size_floor.
    tiny = _make_sub(kind_seq=("identifier",), token_seq=("bound_0",), depth=1, node_count=3)
    pipe.run(tiny)
    pipe.run(tiny)

    counts = pipe.removal_counts
    assert counts["size_floor"] == 2
    # Other filters should not have been triggered (short-circuit after size_floor).
    assert counts.get("depth_floor", 0) == 0
    assert counts.get("__accepted__", 0) == 0


def test_pipeline_samples():
    """FilterPipeline collects up to 5 rejected samples per filter."""
    cfg = FilterConfig()
    pipe = default_pipeline(cfg)

    tiny = _make_sub(kind_seq=("identifier",), token_seq=("x",), depth=1, node_count=2)
    for _ in range(10):
        pipe.run(tiny)

    samples = pipe.samples
    assert "size_floor" in samples
    assert len(samples["size_floor"]) == 5  # capped at 5


def test_pipeline_format_report():
    """format_report produces a readable multi-line summary."""
    cfg = FilterConfig()
    pipe = default_pipeline(cfg)

    tiny = _make_sub(node_count=3, kind_seq=("identifier",), token_seq=("x",))
    pipe.run(tiny)

    report = pipe.format_report()
    assert "Filter removal counts" in report
    assert "size_floor" in report
    assert "accepted candidates" in report


def test_pipeline_accept_increments_accepted_count():
    """Accepted subtrees increment the __accepted__ counter."""
    cfg = FilterConfig()
    pipe = default_pipeline(cfg)

    # Build something that passes all filters.
    big_kinds = (
        "function_definition", "block", "if_statement",
        "assignment", "attribute", "identifier",
        "call", "argument_list", "return_statement",
        "comparison_operator", "for_statement", "while_statement",
    ) * 3
    big_tokens = (
        "bound_0", "bound_1", "free_0", "free_1",
        "alpha", "beta", "gamma", "delta", "epsilon",
    ) * 3
    big_sub = _make_sub(
        kind_seq=big_kinds,
        token_seq=big_tokens,
        depth=8,
        node_count=50,
    )
    v = pipe.run(big_sub)
    assert v.accept
    assert pipe.removal_counts.get("__accepted__", 0) == 1
