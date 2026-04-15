"""Triviality filters for AST duplicate detection.

Each filter is a ``Callable[[CanonicalSubtree, FilterConfig], FilterVerdict]``
that decides whether a canonicalized subtree is worth keeping. Filters
short-circuit: the first REJECT verdict wins, but rejection counts are
tracked independently so pipeline stats can be audited.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from experiments.ast_dedup.canonicalize import CanonicalSubtree

# Control-flow statement kinds that disqualify a subtree from being a
# "stereotyped dunder" pattern (pure straight-line assignments/returns).
_CONTROL_FLOW_KINDS: frozenset[str] = frozenset(
    {
        "if_statement",
        "for_statement",
        "while_statement",
        "try_statement",
        "with_statement",
        "match_statement",
    }
)

# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterVerdict:
    """Result of running a single filter on a subtree."""

    accept: bool
    reason: str | None = None  # None means accepted


@dataclass
class FilterConfig:
    """Configurable thresholds for all triviality filters."""

    min_node_count: int = 8
    min_depth: int = 3
    min_unique_non_keyword: int = 4
    halstead_volume_min: float = 30.0
    block_root_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "return_statement",
                "pass_statement",
                "raise_statement",
            }
        )
    )
    # When True, enables the hand-written trivial-pattern checkers
    # (stereotyped_dunder and identity_pattern).
    block_trivial_patterns: bool = True


# Type alias for filter callables.
Filter = Callable[[CanonicalSubtree, FilterConfig], FilterVerdict]

# ---------------------------------------------------------------------------
# Individual filter functions
# ---------------------------------------------------------------------------

_ACCEPT = FilterVerdict(accept=True)


def size_floor(sub: CanonicalSubtree, cfg: FilterConfig) -> FilterVerdict:
    """Reject if the subtree has fewer named nodes than ``cfg.min_node_count``.

    Removes most boilerplate in one shot.
    """
    if sub.node_count >= cfg.min_node_count:
        return _ACCEPT
    return FilterVerdict(
        accept=False,
        reason=f"node_count={sub.node_count} < min={cfg.min_node_count}",
    )


def depth_floor(sub: CanonicalSubtree, cfg: FilterConfig) -> FilterVerdict:
    """Reject if the subtree's max depth is below ``cfg.min_depth``.

    Prunes flat statement lists and single-line constructs.
    """
    if sub.depth >= cfg.min_depth:
        return _ACCEPT
    return FilterVerdict(
        accept=False,
        reason=f"depth={sub.depth} < min={cfg.min_depth}",
    )


def token_diversity(sub: CanonicalSubtree, cfg: FilterConfig) -> FilterVerdict:
    """Reject if the number of distinct non-keyword tokens is too low.

    Rules out ``return None``, ``self.x = x``, ``a = b``, etc. — the direct
    answer to ``$bound1=$free1`` matches dominating the report.
    """
    unique_non_kw = sub.unique_non_keyword_tokens
    if unique_non_kw >= cfg.min_unique_non_keyword:
        return _ACCEPT
    return FilterVerdict(
        accept=False,
        reason=f"unique_non_keyword={unique_non_kw} < min={cfg.min_unique_non_keyword}",
    )


def root_kind_blocklist(sub: CanonicalSubtree, cfg: FilterConfig) -> FilterVerdict:
    """Reject if the candidate root node kind is in the blocklist.

    ``kind_seq`` is in pre-order so index 0 is the root. Most of these
    should already be filtered by ``size_floor``, but this keeps the
    stats breakdown separate.
    """
    if not sub.kind_seq:
        return _ACCEPT
    root_kind = sub.kind_seq[0]
    if root_kind in cfg.block_root_kinds:
        return FilterVerdict(
            accept=False,
            reason=f"root_kind={root_kind!r} in blocklist",
        )
    return _ACCEPT


def halstead_lite(sub: CanonicalSubtree, cfg: FilterConfig) -> FilterVerdict:
    """Cheap Halstead-proxy volume filter.

    ``volume = node_count * log2(max(|unique_kinds| + |unique_tokens|, 2))``

    Drops low-information subtrees regardless of raw size — for example, a
    loop with many repetitions of a trivial statement has high node_count but
    very low vocabulary, so volume is still small.
    """
    n = sub.node_count
    vocab = len(set(sub.kind_seq)) + len(set(sub.token_seq))
    volume = n * math.log2(max(vocab, 2))
    if volume >= cfg.halstead_volume_min:
        return _ACCEPT
    return FilterVerdict(
        accept=False,
        reason=f"halstead_volume={volume:.1f} < min={cfg.halstead_volume_min}",
    )


# ---------------------------------------------------------------------------
# Stereotyped dunder detection helpers
# ---------------------------------------------------------------------------


def _is_trivial_init(kind_seq: tuple[str, ...]) -> bool:
    """Return True if the subtree is a function whose body is entirely
    ``self.<name> = <param>``-shaped assignments (no control flow, no return).

    Detection is purely structural via ``kind_seq`` shape because ``__init__``
    is alpha-renamed away by the canonicalizer. Also fires on non-``__init__``
    methods that are pure assignment bodies, which is intentional.
    """
    if not kind_seq or kind_seq[0] != "function_definition":
        return False
    kind_set = set(kind_seq)
    if kind_set & _CONTROL_FLOW_KINDS:
        return False
    if "return_statement" in kind_set:
        return False
    assignment_count = kind_seq.count("assignment")
    if assignment_count < 2:
        return False
    attribute_count = kind_seq.count("attribute")
    return attribute_count >= assignment_count


def _is_trivial_repr(kind_seq: tuple[str, ...], token_seq: tuple[str, ...]) -> bool:
    """Return True for a __repr__ that returns a single f-string or format."""
    if "__repr__" not in token_seq:
        return False
    if set(kind_seq) & _CONTROL_FLOW_KINDS:
        return False
    return kind_seq.count("return_statement") == 1


def _is_trivial_eq_or_lt(kind_seq: tuple[str, ...], token_seq: tuple[str, ...]) -> bool:
    """Return True for a __eq__/__lt__ with a single isinstance + comparison."""
    if "__eq__" not in token_seq and "__lt__" not in token_seq:
        return False
    # if_statement is allowed here (isinstance() guard).
    if set(kind_seq) & (_CONTROL_FLOW_KINDS - {"if_statement"}):
        return False
    return kind_seq.count("return_statement") == 1


def _is_trivial_hash(kind_seq: tuple[str, ...], token_seq: tuple[str, ...]) -> bool:
    """Return True for __hash__ = return hash((self.a, self.b, ...))."""
    if "__hash__" not in token_seq:
        return False
    if set(kind_seq) & _CONTROL_FLOW_KINDS:
        return False
    return kind_seq.count("return_statement") == 1


def _is_trivial_property_getter(
    kind_seq: tuple[str, ...], token_count: int
) -> bool:
    """Return True for @property getter: ``return self._name``.

    ``self`` is canonicalized to a ``bound_`` token so detection is purely
    structural: function/decorated root, exactly one return, no assignments,
    no control flow, and a tiny canonical token count.
    """
    if not kind_seq:
        return False
    if kind_seq[0] not in ("function_definition", "decorated_definition"):
        return False
    kind_set = set(kind_seq)
    if kind_set & _CONTROL_FLOW_KINDS:
        return False
    if kind_seq.count("return_statement") != 1:
        return False
    if "assignment" in kind_set:
        return False
    return token_count <= 5


def stereotyped_dunder(sub: CanonicalSubtree, cfg: FilterConfig) -> FilterVerdict:
    """Reject trivial dunder/property implementations.

    Detects the following patterns via kind_seq / token_seq shape:
    - ``__init__`` whose body is entirely ``self.<name> = <param>`` assignments
    - ``__repr__`` returning a single f-string / format call
    - ``__eq__`` / ``__lt__`` with a single isinstance + attribute comparison
    - ``__hash__`` that is ``return hash((self.a, self.b, ...))``
    - ``@property`` getters that are ``return self._<name>``
    """
    if not cfg.block_trivial_patterns:
        return _ACCEPT

    ks = sub.kind_seq
    ts = sub.token_seq

    if _is_trivial_init(ks):
        return FilterVerdict(accept=False, reason="stereotyped_dunder:__init__")
    if _is_trivial_repr(ks, ts):
        return FilterVerdict(accept=False, reason="stereotyped_dunder:__repr__")
    if _is_trivial_eq_or_lt(ks, ts):
        return FilterVerdict(accept=False, reason="stereotyped_dunder:__eq__/__lt__")
    if _is_trivial_hash(ks, ts):
        return FilterVerdict(accept=False, reason="stereotyped_dunder:__hash__")
    if _is_trivial_property_getter(ks, len(ts)):
        return FilterVerdict(accept=False, reason="stereotyped_dunder:property_getter")
    return _ACCEPT


# ---------------------------------------------------------------------------
# Identity pattern filter
# ---------------------------------------------------------------------------

# Canonical token sequences that represent trivial identity-like patterns.
# After canonicalization:
#   - identifiers become ``bound_N`` or ``free_N``
#   - attribute names remain literal (default rename_attrs=False)
#   - keywords like ``return``, ``=``, etc. remain as-is
#
# We match against the *token_seq* (not including kind tokens) after
# filtering out any ``bound_`` / ``free_`` placeholder tokens.


def _token_shape(token_seq: tuple[str, ...]) -> tuple[str, ...]:
    """Replace all bound_N / free_N tokens with ``<id>`` for pattern matching."""
    result = []
    for t in token_seq:
        if t.startswith("bound_") or t.startswith("free_") or t == "free_unresolved":
            result.append("<id>")
        else:
            result.append(t)
    return tuple(result)


# Identity-like token shapes to reject. The ``<ident>(<ident>)`` call-shape
# collapses to ``("<id>", "<id>")`` once the anonymous parentheses are
# dropped, so it's already covered by the two-token shape.
_IDENTITY_SHAPES: frozenset[tuple[str, ...]] = frozenset(
    [
        ("<id>", "=", "<id>"),            # <ident> = <ident>
        ("<id>", "<id>"),                 # <ident>.<name>
        ("return", "<id>"),               # return <ident>
        ("return", "<id>", "<id>"),       # return <ident>.<name>
    ]
)


def identity_pattern(sub: CanonicalSubtree, cfg: FilterConfig) -> FilterVerdict:
    """Reject canonical forms that reduce to trivial identity-like patterns.

    Patterns rejected:
    - ``<ident> = <ident>``
    - ``<ident>.<name>``
    - ``return <ident>``
    - ``return <ident>.<name>``
    - ``<ident>(<ident>)``
    """
    if not cfg.block_trivial_patterns:
        return _ACCEPT

    shape = _token_shape(sub.token_seq)
    if shape in _IDENTITY_SHAPES:
        return FilterVerdict(
            accept=False,
            reason=f"identity_pattern: shape={shape}",
        )
    return _ACCEPT


# ---------------------------------------------------------------------------
# FilterPipeline
# ---------------------------------------------------------------------------


class FilterPipeline:
    """Ordered sequence of filters applied to each ``CanonicalSubtree``.

    On each call to ``run(sub)``:
    - Filters are applied in order.
    - The first REJECT verdict is returned immediately (short-circuit).
    - If all pass, ACCEPT is returned.
    - Results are recorded in ``_log`` for statistics.
    """

    def __init__(
        self,
        filters: list[tuple[str, Filter]],
        cfg: FilterConfig,
    ) -> None:
        """
        Args:
            filters: Ordered list of ``(name, callable)`` pairs.
            cfg: Shared ``FilterConfig`` passed to every filter.
        """
        self._filters = filters
        self._cfg = cfg
        self._removal_counts: dict[str, int] = {name: 0 for name, _ in filters}
        self._removal_counts["__accepted__"] = 0
        self._samples: dict[str, list[CanonicalSubtree]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Main interface
    # ------------------------------------------------------------------

    def run(self, sub: CanonicalSubtree) -> FilterVerdict:
        """Apply all filters in order; return the first REJECT or ACCEPT.

        Records the subtree's disposition for statistics.
        """
        for name, fn in self._filters:
            verdict = fn(sub, self._cfg)
            if not verdict.accept:
                self._removal_counts[name] = self._removal_counts.get(name, 0) + 1
                if len(self._samples[name]) < 5:
                    self._samples[name].append(sub)
                return verdict
        self._removal_counts["__accepted__"] = (
            self._removal_counts.get("__accepted__", 0) + 1
        )
        return _ACCEPT

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    @property
    def removal_counts(self) -> dict[str, int]:
        """Map from filter name to the number of subtrees it rejected."""
        return dict(self._removal_counts)

    @property
    def samples(self) -> dict[str, list[CanonicalSubtree]]:
        """Up to 5 rejected subtrees per filter (for auditing)."""
        return dict(self._samples)

    def format_report(self) -> str:
        """Produce a human-readable summary of filter statistics."""
        lines = ["Filter removal counts", "-" * 45]
        filter_names = [name for name, _ in self._filters]
        max_name_len = max((len(n) for n in filter_names), default=10)
        total_rejected = 0
        for name in filter_names:
            count = self._removal_counts.get(name, 0)
            total_rejected += count
            lines.append(f"{name:<{max_name_len}}  {count:>8,}")
        lines.append("-" * 45)
        accepted = self._removal_counts.get("__accepted__", 0)
        lines.append(f"{'accepted candidates':<{max_name_len}}  {accepted:>8,}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default pipeline factory
# ---------------------------------------------------------------------------


def default_pipeline(cfg: FilterConfig | None = None) -> FilterPipeline:
    """Return a ``FilterPipeline`` with the standard filters in recommended order.

    Order rationale:
    1. ``size_floor`` — fastest structural check, cuts the most.
    2. ``root_kind_blocklist`` — cheap lookup, complements size_floor.
    3. ``depth_floor`` — structural depth check.
    4. ``token_diversity`` — vocabulary-based check.
    5. ``halstead_lite`` — slightly more expensive vocab+size proxy.
    6. ``stereotyped_dunder`` — hand-written pattern matcher, last to avoid
       false positives on structurally large dunders.
    7. ``identity_pattern`` — catches minimal snippets that survived size_floor.
    """
    if cfg is None:
        cfg = FilterConfig()
    filters: list[tuple[str, Filter]] = [
        ("size_floor", size_floor),
        ("root_kind_blocklist", root_kind_blocklist),
        ("depth_floor", depth_floor),
        ("token_diversity", token_diversity),
        ("halstead_lite", halstead_lite),
        ("stereotyped_dunder", stereotyped_dunder),
        ("identity_pattern", identity_pattern),
    ]
    return FilterPipeline(filters, cfg)


__all__ = [
    "Filter",
    "FilterConfig",
    "FilterPipeline",
    "FilterVerdict",
    "default_pipeline",
    "depth_floor",
    "halstead_lite",
    "identity_pattern",
    "root_kind_blocklist",
    "size_floor",
    "stereotyped_dunder",
    "token_diversity",
]
