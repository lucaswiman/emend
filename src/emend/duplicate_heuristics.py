"""
duplicate_heuristics.py — Production scoring model and false-positive suppressions
for duplicate code detection.

This module is self-contained (stdlib only) and is imported by the duplicate
detection pipeline. It does NOT import from emend.duplicate.

Scoring model
-------------
Two cluster kinds are supported:

  exact   — subtree clusters found by canonical Merkle hashing
  sequence — sibling-statement run clusters found by winnowing / suffix arrays

Each produces a :class:`DuplicateScore` with four components:

  raw_score        base numeric signal (size proxy)
  diversity_bonus  reward for structural variety inside the fragment
  cross_bonus      reward for cross-file or cross-function occurrences
  penalty          accumulated suppression weight (boilerplate filters)
  final_score      max(0, raw_score + diversity_bonus + cross_bonus - penalty)

A final_score > 0 passes the filter.  The CLI/lint layer imposes an additional
``min_score`` threshold (default 0.0) so callers can tighten the bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DuplicateScore:
    """Composite score for a single duplicate cluster."""

    raw_score: float
    """Base signal derived from size (node count × log-diversity or stmt×10)."""

    diversity_bonus: float
    """Bonus for token or statement-kind variety; capped to avoid over-rewarding."""

    cross_bonus: float
    """Bonus for cross-file (+20) or cross-function-same-file (+10) occurrences."""

    penalty: float
    """Accumulated boilerplate penalty from suppression heuristics."""

    final_score: float
    """max(0, raw_score + diversity_bonus + cross_bonus - penalty)."""

    @property
    def is_suppressed(self) -> bool:
        """True when the cluster should be hidden from production output."""
        return self.final_score <= 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cross_bonus(members: Sequence[Any]) -> float:
    """Return cross-file or cross-function bonus for a cluster.

    Members are expected to expose a ``.file`` attribute (str) and optionally
    a ``.function`` attribute (str | None).  Plain dicts with those keys are
    also accepted for test convenience.
    """
    def _get(m: Any, key: str, default: Any = None) -> Any:
        if isinstance(m, dict):
            return m.get(key, default)
        return getattr(m, key, default)

    files = {_get(m, "file", "") for m in members}
    if len(files) > 1:
        return 20.0

    # All same file — check cross-function
    funcs = {_get(m, "function", None) for m in members}
    if len(funcs) > 1:
        return 10.0

    return 0.0


# ---------------------------------------------------------------------------
# Suppression heuristics
# Each function returns a *penalty* (float).
# 0.0 = not applicable.  Large values (≥ 500) effectively suppress the cluster.
# ---------------------------------------------------------------------------

def is_abstract_stub(
    kind_seq: tuple[str, ...],
    token_seq: tuple[str, ...],
) -> float:
    """Detect ``raise NotImplementedError`` stubs.

    Returns 1000.0 when the fragment is (or contains only) a raise of
    ``NotImplementedError`` / ``NotImplemented``, which is a ubiquitous
    abstract-interface pattern and almost never an actionable duplicate.
    """
    has_not_implemented = any(
        t in ("NotImplementedError", "NotImplemented") for t in token_seq
    )
    if not has_not_implemented:
        return 0.0

    # Only suppress when the body is *tiny* (just the raise), not when
    # NotImplementedError appears as part of a larger meaningful body.
    raise_kinds = sum(1 for k in kind_seq if k == "raise_statement")
    non_trivial_kinds = {
        "if_statement",
        "for_statement",
        "while_statement",
        "with_statement",
        "try_statement",
        "match_statement",
        "assignment",
        "augmented_assignment",
        "return_statement",
    }
    if raise_kinds >= 1 and not any(k in non_trivial_kinds for k in kind_seq):
        return 1000.0

    return 0.0


def is_trivial_validator(
    node_count: int,
    kind_seq: tuple[str, ...],
) -> float:
    """Detect tiny ``isinstance``/raise/return validators.

    These three-line guards appear constantly in Python library code and are
    never interesting duplicates.  Criteria:

      - node_count < 12
      - root kind is ``if_statement``
      - ``isinstance`` is present in the kind/token context (caller supplies
        that in the kind_seq as a pseudo-token, or it will appear in token_seq
        for callers who pass tokens via kind_seq — the heuristic is lenient)
    """
    if node_count >= 12:
        return 0.0

    is_if_root = kind_seq and kind_seq[0] == "if_statement"
    has_isinstance = "isinstance" in kind_seq or "call" in kind_seq
    if is_if_root and has_isinstance:
        return 500.0

    return 0.0


def is_property_wrapper(
    kind_seq: tuple[str, ...],
    token_seq: tuple[str, ...],
) -> float:
    """Detect trivial ``@property`` getters that just return ``self.attr``.

    Pattern: function body is a single ``return_statement`` whose expression
    is an attribute access on ``self``.  These are identical across every
    class that wraps a private attribute.
    """
    has_return = "return_statement" in kind_seq
    if not has_return:
        return 0.0

    # Must be tiny: function_def + return + attribute = very few kinds
    meaningful_kinds = {
        "if_statement", "for_statement", "while_statement",
        "with_statement", "try_statement", "assignment",
        "augmented_assignment", "call",
    }
    if any(k in meaningful_kinds for k in kind_seq):
        return 0.0

    # Token evidence: "self" present and token count is very small.
    # Threshold is 8 to capture "def name(self): return self.attr"
    # which yields tokens like (def, name, self, return, self, ., attr).
    has_self = "self" in token_seq
    if has_self and len(token_seq) <= 8:
        return 500.0

    return 0.0


def is_tiny_same_file_fragment(
    members: Sequence[Any],
    node_count: int,
) -> float:
    """Penalise small fragments that all live in the same file.

    These typically arise from boilerplate that was copy-pasted within a
    single module and is not interesting to surface.  Cross-file copies are
    separately rewarded by the cross_bonus; this function only fires when
    ALL members share one file.
    """
    if node_count >= 15:
        return 0.0

    def _get_file(m: Any) -> str:
        if isinstance(m, dict):
            return m.get("file", "")
        return getattr(m, "file", "")

    files = {_get_file(m) for m in members}
    if len(files) == 1:
        return 300.0

    return 0.0


def is_init_self_assignment(
    kind_seq: tuple[str, ...],
    token_seq: tuple[str, ...],
) -> float:
    """Detect ``__init__`` bodies that are entirely ``self.x = x`` assignments.

    These are identical across every data-class-like ``__init__``, yet they
    carry zero refactoring signal — the duplication is intentional and
    unavoidable without a macro system.

    Heuristic: the fragment contains assignments and ``self`` but no branching,
    looping, or calls (other than super()).
    """
    has_assignment = "assignment" in kind_seq or "augmented_assignment" in kind_seq
    has_self = "self" in token_seq
    if not (has_assignment and has_self):
        return 0.0

    branching_kinds = {
        "if_statement", "for_statement", "while_statement",
        "with_statement", "try_statement", "match_statement",
    }
    if any(k in branching_kinds for k in kind_seq):
        return 0.0

    # Allow super() calls but penalise if the only calls are self-assignments
    call_tokens = [t for t in token_seq if t == "call"]
    # If kind_seq has calls but no branching, it's likely super() + assignments
    # Use a generous threshold: if >80% of kinds are assignments, it's boilerplate
    assignment_count = kind_seq.count("assignment") + kind_seq.count("augmented_assignment")
    total_kinds = len(kind_seq)
    if total_kinds > 0 and assignment_count / total_kinds >= 0.6:
        return 500.0

    return 0.0


def is_dunder_boilerplate(
    symbol: str,
    kind_seq: tuple[str, ...],
) -> float:
    """Detect trivial ``__repr__``, ``__eq__``, ``__hash__`` implementations.

    These dunder methods have well-known identical implementations across
    many classes and are seldom actionable refactoring targets.
    """
    trivial_dunders = {"__repr__", "__str__", "__eq__", "__hash__", "__bool__",
                       "__len__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__"}
    # symbol may be a qualified name like "MyClass.__repr__"
    base = symbol.rsplit(".", 1)[-1] if "." in symbol else symbol
    if base not in trivial_dunders:
        return 0.0

    # Only penalise *small* dunder implementations — large ones may be interesting
    meaningful_kinds = {
        "if_statement", "for_statement", "while_statement",
        "with_statement", "try_statement",
    }
    if any(k in meaningful_kinds for k in kind_seq):
        return 0.0

    return 400.0


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

# Root kinds that are high-priority candidates for subtree duplicate analysis.
_FUNCTION_ROOT_KINDS = frozenset({
    "function_definition",
    "method_definition",
    "arrow_function",
    "function_declaration",
    "function_item",          # Rust
    "impl_item",
    "trait_item",
    "lambda",
})

_BLOCK_ROOT_KINDS = frozenset({
    "block",
    "if_statement",
    "for_statement",
    "while_statement",
    "with_statement",
    "try_statement",
    "match_statement",
    "class_definition",
    "class_declaration",
    "struct_item",
    "enum_item",
})


def should_analyze_subtree(
    root_kind: str,
    node_count: int,
    depth: int,
) -> bool:
    """Whether a subtree should be considered for duplicate detection.

    Production rules:

    - Function/method roots: ``node_count >= 8`` and ``depth >= 3``
    - Block/class roots: ``node_count >= 15`` and ``depth >= 3``
    - All other root kinds: skipped (too noisy)

    The depth guard avoids trivially shallow fragments (single expressions).
    """
    if depth < 3:
        return False

    if root_kind in _FUNCTION_ROOT_KINDS:
        return node_count >= 8

    if root_kind in _BLOCK_ROOT_KINDS:
        return node_count >= 15

    return False


def should_analyze_sequence(
    stmt_count: int,
    distinct_kinds: int,
) -> bool:
    """Whether a statement sequence should be considered for duplicate detection.

    Production rules:

    - At least 3 statements (``stmt_count >= 3``)
    - At least 2 distinct statement kinds, so we do not flag all-assignment
      initialisation blocks where every line is the same kind

    Sequences that are purely one kind (e.g. all ``assignment``) are often
    data definitions or field lists rather than logic worth extracting.
    """
    return stmt_count >= 3 and distinct_kinds >= 2


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def score_subtree_cluster(
    members: Sequence[Any],
    unique_tokens_per_member: Sequence[int],
) -> DuplicateScore:
    """Score an exact-duplicate cluster of subtrees.

    Parameters
    ----------
    members:
        Cluster members.  Each must expose ``.file`` (str), ``.node_count``
        (int), and optionally ``.function`` (str | None).  Plain dicts are
        accepted for testing.
    unique_tokens_per_member:
        Number of unique (non-keyword) tokens in each member's canonical
        token sequence.  Must parallel ``members``.
    """
    def _get(m: Any, key: str, default: Any = 0) -> Any:
        if isinstance(m, dict):
            return m.get(key, default)
        return getattr(m, key, default)

    if not members:
        return DuplicateScore(
            raw_score=0.0,
            diversity_bonus=0.0,
            cross_bonus=0.0,
            penalty=0.0,
            final_score=0.0,
        )

    node_counts = [_get(m, "node_count", 0) for m in members]
    avg_node_count = sum(node_counts) / len(node_counts)
    avg_unique = (
        sum(unique_tokens_per_member) / len(unique_tokens_per_member)
        if unique_tokens_per_member
        else 0
    )

    raw_score = avg_node_count * math.log2(avg_unique + 1)
    diversity_bonus = min(avg_unique * 0.5, 20.0)
    cross_bonus = _cross_bonus(members)

    return DuplicateScore(
        raw_score=raw_score,
        diversity_bonus=diversity_bonus,
        cross_bonus=cross_bonus,
        penalty=0.0,
        final_score=max(0.0, raw_score + diversity_bonus + cross_bonus),
    )


def score_sequence_cluster(
    members: Sequence[Any],
    stmt_count: int,
    distinct_stmt_kinds: int = 0,
) -> DuplicateScore:
    """Score a sibling-sequence duplicate cluster.

    Parameters
    ----------
    members:
        Cluster members (same interface as :func:`score_subtree_cluster`).
    stmt_count:
        Number of statements in the shared run.
    distinct_stmt_kinds:
        Number of distinct statement kinds in the run.  Used for the diversity
        bonus.  Defaults to 0 (no bonus) when callers do not supply it.
    """
    raw_score = stmt_count * 10.0
    diversity_bonus = min(distinct_stmt_kinds * 3.0, 15.0)
    cross_bonus = _cross_bonus(members)

    return DuplicateScore(
        raw_score=raw_score,
        diversity_bonus=diversity_bonus,
        cross_bonus=cross_bonus,
        penalty=0.0,
        final_score=max(0.0, raw_score + diversity_bonus + cross_bonus),
    )


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def compute_production_score(
    kind: str,
    members: Sequence[Any],
    *,
    node_count: int = 0,
    stmt_count: int = 0,
    unique_tokens: int = 0,
    distinct_stmt_kinds: int = 0,
    kind_seq: tuple[str, ...] = (),
    token_seq: tuple[str, ...] = (),
    symbol: str = "",
) -> DuplicateScore:
    """Compute the production score with all suppression heuristics applied.

    Parameters
    ----------
    kind:
        ``"exact"`` for subtree clusters or ``"sequence"`` for sibling-sequence
        clusters.
    members:
        Cluster members (see :func:`score_subtree_cluster`).
    node_count:
        Representative node count for the cluster (used by suppressions).
    stmt_count:
        Statement count for sequence clusters.
    unique_tokens:
        Unique token count for exact clusters.
    distinct_stmt_kinds:
        Distinct statement kinds for sequence diversity bonus.
    kind_seq:
        Pre-order sequence of node kinds from the canonical subtree.
    token_seq:
        Pre-order sequence of canonical leaf tokens from the canonical subtree.
    symbol:
        Qualified name of the containing symbol, used for dunder detection.

    Returns
    -------
    DuplicateScore
        Composite score with all penalties applied.
    """
    # 1. Base scores (no penalties yet)
    if kind == "sequence":
        base = score_sequence_cluster(members, stmt_count, distinct_stmt_kinds)
    else:
        unique_tokens_per = [unique_tokens] * len(members) if members else []
        base = score_subtree_cluster(members, unique_tokens_per)

    # 2. Accumulate suppression penalties
    penalty = 0.0
    penalty += is_abstract_stub(kind_seq, token_seq)
    penalty += is_trivial_validator(node_count, kind_seq)
    penalty += is_property_wrapper(kind_seq, token_seq)
    penalty += is_tiny_same_file_fragment(members, node_count)
    penalty += is_init_self_assignment(kind_seq, token_seq)
    penalty += is_dunder_boilerplate(symbol, kind_seq)

    # 3. Build final score
    final = max(0.0, base.raw_score + base.diversity_bonus + base.cross_bonus - penalty)

    return DuplicateScore(
        raw_score=base.raw_score,
        diversity_bonus=base.diversity_bonus,
        cross_bonus=base.cross_bonus,
        penalty=penalty,
        final_score=final,
    )


# ---------------------------------------------------------------------------
# Filtering / ranking
# ---------------------------------------------------------------------------

def filter_findings(
    clusters: list[Any],
    *,
    min_score: float = 0.0,
    max_results: int = 50,
    score_attr: str = "score",
) -> list[Any]:
    """Filter and rank duplicate clusters by production score.

    Parameters
    ----------
    clusters:
        Raw list of cluster objects.  Each must expose a score via
        ``score_attr`` (default ``"score"``), which should be a
        :class:`DuplicateScore` instance.  Plain dicts are accepted.
    min_score:
        Minimum ``final_score`` to retain (inclusive).
    max_results:
        Maximum number of clusters in the output.
    score_attr:
        Attribute / dict key that holds the :class:`DuplicateScore`.

    Returns
    -------
    list
        Clusters with ``final_score > 0`` and ``>= min_score``, sorted by
        ``final_score`` descending, capped at ``max_results``.
    """
    def _score(c: Any) -> float:
        if isinstance(c, dict):
            s = c.get(score_attr)
        else:
            s = getattr(c, score_attr, None)
        if isinstance(s, DuplicateScore):
            return s.final_score
        if isinstance(s, (int, float)):
            return float(s)
        return 0.0

    kept = [c for c in clusters if _score(c) > 0 and _score(c) >= min_score]
    kept.sort(key=_score, reverse=True)
    return kept[:max_results]
