"""Boilerplate suppression heuristics for duplicate code detection.

Each function returns a penalty (float). 0.0 = not applicable.
Large values (>= 500) effectively suppress the cluster when subtracted
from the base score.
"""

from __future__ import annotations

from typing import Any, Sequence

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
    token_seq: tuple[str, ...] = (),
) -> float:
    """Detect tiny ``isinstance``/raise/return validators.

    These three-line guards appear constantly in Python library code and are
    never interesting duplicates.  Criteria:

      - node_count < 12
      - root kind is ``if_statement``
      - ``isinstance`` appears in the token sequence
    """
    if node_count >= 12:
        return 0.0

    is_if_root = kind_seq and kind_seq[0] == "if_statement"
    has_isinstance = "isinstance" in token_seq or "isinstance" in kind_seq
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
