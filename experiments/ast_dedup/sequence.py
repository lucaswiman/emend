"""Sibling-sequence duplicate detection.

Treats each function body as a sequence of canonical statement hashes and
finds duplicated runs across all such sequences using two complementary
methods: winnowing (Schleimer-Wilkerson-Aiken, SIGMOD 2003) and generalized
suffix array + LCP.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from hashlib import blake2b
from itertools import count
from typing import Literal, Optional

from emend import emend_core

from experiments.ast_dedup.canonicalize import (
    PYTHON_KEYWORDS,
    _build_qn_at_and_def_loc,
    _is_bound_inside,
)

# ---------------------------------------------------------------------------
# Optional pydivsufsort
# ---------------------------------------------------------------------------

try:
    import pydivsufsort  # type: ignore
    HAVE_PDS = True
except ImportError:
    HAVE_PDS = False

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementSeq:
    """A sequence of canonical statement hashes for one function body.

    ``kinds`` mirrors ``hashes``: each entry is the tree-sitter node kind
    of the corresponding top-level statement (e.g. ``"assignment"``,
    ``"for_statement"``, ``"return_statement"``).  Used by
    :func:`filter_sequence_clones` for the non-trivial statement mix rule.
    """

    file: str
    function_qn: str                            # qualified name from PyScopeResolver
    start_line: int
    end_line: int
    hashes: tuple[bytes, ...]                   # per-statement canonical hashes
    line_ranges: tuple[tuple[int, int], ...]    # per-statement (start, end) 0-indexed
    kinds: tuple[str, ...]                      # per-statement node kinds


CloneMethod = Literal["winnowing", "suffix_array"]


@dataclass(frozen=True)
class SequenceClone:
    """A run of matching statements found across two ``StatementSeq`` objects."""

    left: StatementSeq
    left_range: tuple[int, int]     # statement indices [start, end)
    right: StatementSeq
    right_range: tuple[int, int]    # statement indices [start, end)
    length: int                     # number of statements in the run
    method: CloneMethod


# ---------------------------------------------------------------------------
# Per-statement canonicalization (function-scoped alpha-renaming)
# ---------------------------------------------------------------------------
#
# Unlike ``canonicalize.canonicalize()`` — which derives the bound/free scope
# from the subtree root — here the scope is the *enclosing function* so that
# sibling statements share a consistent renaming context. We therefore need a
# walker that accepts an explicit ``(func_start_line, func_end_line)`` range
# for the bound check but walks only a single statement subtree for the hash.


def _canonicalize_statement(
    stmt,
    func_start_line: int,
    func_end_line: int,
    qn_at: dict[tuple[int, int], str],
    def_loc: dict[str, tuple[int, int]],
) -> bytes:
    """Return a 16-byte canonical hash for ``stmt`` with bound/free
    classification taken from the enclosing function's line range.
    """
    rename: dict[str, str] = {}
    bound_counter = count()
    free_counter = count()
    str_map: dict[str, str] = {}
    num_map: dict[str, str] = {}
    str_counter = count()
    num_counter = count()

    def assign(qn: str) -> str:
        tok = rename.get(qn)
        if tok is not None:
            return tok
        if _is_bound_inside(qn, def_loc, func_start_line, func_end_line):
            tok = f"bound_{next(bound_counter)}"
        else:
            tok = f"free_{next(free_counter)}"
        rename[qn] = tok
        return tok

    def lit_str(text: str) -> str:
        # Replace string literals with a single placeholder.
        existing = str_map.get(text)
        if existing is not None:
            return existing
        tok = f"str_{next(str_counter)}"
        str_map[text] = tok
        return tok

    def lit_num(text: str) -> str:
        # Replace numeric literals with a single placeholder.
        existing = num_map.get(text)
        if existing is not None:
            return existing
        tok = f"num_{next(num_counter)}"
        num_map[text] = tok
        return tok

    def leaf_token(n) -> Optional[str]:
        k = n.kind
        if k == "identifier":
            qn = qn_at.get((n.start_point[0], n.start_point[1]))
            if qn is None:
                return "free_unresolved"
            return assign(qn)
        if k == "string":
            return lit_str(n.text())
        if k in ("integer", "float"):
            return lit_num(n.text())
        if k in ("true", "false", "none", "True", "False", "None"):
            return k
        if k == "type_identifier":
            qn = qn_at.get((n.start_point[0], n.start_point[1]))
            return assign(qn) if qn is not None else "free_type"
        return n.text()

    def walk(n) -> bytes:
        # String: treat the whole node as a leaf.
        if n.kind == "string":
            tok = lit_str(n.text())
            return blake2b(b"string" + tok.encode(), digest_size=16).digest()

        # Attribute access: hash object recursively, keep attr name literal.
        if n.kind == "attribute":
            obj = n.child_by_field_name("object")
            attr = n.child_by_field_name("attribute")
            h = blake2b(digest_size=16)
            h.update(b"attribute")
            if obj is not None:
                h.update(walk(obj))
            if attr is not None:
                tok = attr.text()
                leaf_h = blake2b(
                    attr.kind.encode() + tok.encode(), digest_size=16
                ).digest()
                h.update(leaf_h)
            return h.digest()

        # True leaf (no children at all).
        if n.child_count == 0:
            tok = leaf_token(n)
            return blake2b(
                n.kind.encode() + (tok or "").encode(), digest_size=16
            ).digest()

        # Internal node: hash kind + all children (named recurse; anon raw).
        h = blake2b(digest_size=16)
        h.update(n.kind.encode())
        for i in range(n.child_count):
            c = n.child(i)
            if c is None:
                continue
            if c.is_named:
                h.update(walk(c))
            else:
                h.update(c.kind.encode())
                h.update(c.text().encode())
        return h.digest()

    return walk(stmt)


# ---------------------------------------------------------------------------
# Building statement sequences
# ---------------------------------------------------------------------------


def build_statement_seqs(path: str, scope_resolver) -> list[StatementSeq]:
    """Parse ``path`` and build a :class:`StatementSeq` for every
    function/method definition found.

    Each statement in the function body is hashed with the *enclosing
    function* as the alpha-renaming scope context, so sibling statements
    share a consistent bound/free numbering.

    Returns an empty list if the file cannot be parsed.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        source = fh.read()

    scope_resolver.index_file(path, source)
    tree = emend_core.parse_file(path)
    if tree is None:
        return []

    refs = scope_resolver.references_in_file(path)
    qn_at, def_loc = _build_qn_at_and_def_loc(refs)

    # Build a map from (start_line, end_line) to qualified name using the
    # scope resolver's definitions_in_file.  Definitions are 1-indexed.
    defs = scope_resolver.definitions_in_file(path)
    # definitions_in_file returns (qn, line, col) with 1-indexed lines.
    # We need to find the function_qn for each node by matching start lines.
    # Build a lookup: 0-indexed start_line -> qn
    def_line_to_qn: dict[int, str] = {}
    for qn, line, col in defs:
        def_line_to_qn[line - 1] = qn  # normalize to 0-indexed

    seqs: list[StatementSeq] = []

    def visit_functions(node, parent_func_qn: Optional[str] = None):
        """Recursively find function_definition nodes and build statement seqs."""
        if node.kind == "function_definition":
            name_node = node.child_by_field_name("name")
            func_start = node.start_point[0]
            func_end = node.end_point[0]

            # Derive function_qn: try to look it up from definitions_in_file.
            func_qn = def_line_to_qn.get(func_start)
            if func_qn is None and name_node is not None:
                func_qn = name_node.text()
            if func_qn is None:
                func_qn = f"<unknown>@{func_start}"

            body = node.child_by_field_name("body")
            if body is not None:
                stmts = body.named_children()
                hashes_list: list[bytes] = []
                line_ranges_list: list[tuple[int, int]] = []
                kinds_list: list[str] = []

                for stmt in stmts:
                    h = _canonicalize_statement(
                        stmt,
                        func_start,
                        func_end,
                        qn_at,
                        def_loc,
                    )
                    hashes_list.append(h)
                    line_ranges_list.append((stmt.start_point[0], stmt.end_point[0]))
                    kinds_list.append(stmt.kind)

                if hashes_list:
                    seqs.append(
                        StatementSeq(
                            file=path,
                            function_qn=func_qn,
                            start_line=func_start,
                            end_line=func_end,
                            hashes=tuple(hashes_list),
                            line_ranges=tuple(line_ranges_list),
                            kinds=tuple(kinds_list),
                        )
                    )

            # Recurse into the function body for nested functions.
            if body is not None:
                for child in body.named_children():
                    visit_functions(child, func_qn)

        elif node.kind in ("class_definition", "decorated_definition"):
            # Recurse into class bodies but don't create a seq for the class.
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.named_children():
                    visit_functions(child, parent_func_qn)
        else:
            for child in node.named_children():
                visit_functions(child, parent_func_qn)

    root = tree.root
    for child in root.named_children():
        visit_functions(child)

    return seqs


# ---------------------------------------------------------------------------
# Method A: Winnowing
# ---------------------------------------------------------------------------


def winnowing(seq: list[bytes], w: int = 4) -> list[tuple[int, bytes]]:
    """Schleimer-Wilkerson-Aiken winnowing fingerprinting.

    For each window of size ``w``, pick the rightmost minimum hash value
    (ties broken by rightmost).  Deduplicate consecutive identical
    selections.

    Returns a list of ``(position, hash)`` fingerprints.
    """
    n = len(seq)
    if n < w:
        # Fewer elements than window size: just return one fingerprint
        # per element (degenerate case), selecting the minimum.
        if not seq:
            return []
        min_h = min(seq)
        min_pos = max(i for i, h in enumerate(seq) if h == min_h)
        return [(min_pos, min_h)]

    fingerprints: list[tuple[int, bytes]] = []
    last_selected: Optional[tuple[int, bytes]] = None

    for i in range(n - w + 1):
        window = seq[i : i + w]
        # Rightmost minimum: find min hash, then take the rightmost position.
        min_h = min(window)
        # Rightmost occurrence in window.
        pos_in_window = max(j for j, h in enumerate(window) if h == min_h)
        global_pos = i + pos_in_window
        selected = (global_pos, min_h)
        if selected != last_selected:
            fingerprints.append(selected)
            last_selected = selected

    return fingerprints


def find_clones_winnowing(
    seqs: list[StatementSeq],
    w: int = 4,
    min_run: int = 4,
) -> list[SequenceClone]:
    """Find duplicated statement runs using the winnowing fingerprint index.

    For any fingerprint hit by >= 2 different sequences, extend the match
    leftward and rightward as far as hashes agree, then filter by
    ``min_run``.
    """
    # Build fingerprint index: fingerprint_hash -> list[(seq_index, stmt_position)]
    index: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
    fingerprint_lists: list[list[tuple[int, bytes]]] = []

    for si, seq in enumerate(seqs):
        fps = winnowing(list(seq.hashes), w)
        fingerprint_lists.append(fps)
        for pos, fp_hash in fps:
            index[fp_hash].append((si, pos))

    clones: list[SequenceClone] = []
    seen: set[tuple[int, int, int, int]] = set()  # dedup

    for fp_hash, hits in index.items():
        # Only care about fingerprints appearing in >= 2 different sequences.
        seqs_hit = set(si for si, _ in hits)
        if len(seqs_hit) < 2:
            continue

        # Group hits by sequence index.
        by_seq: dict[int, list[int]] = defaultdict(list)
        for si, pos in hits:
            by_seq[si].append(pos)

        seq_indices = list(by_seq.keys())
        # Compare each pair of sequences.
        for a in range(len(seq_indices)):
            for b in range(a + 1, len(seq_indices)):
                si_a = seq_indices[a]
                si_b = seq_indices[b]
                seq_a = seqs[si_a]
                seq_b = seqs[si_b]

                for pa in by_seq[si_a]:
                    for pb in by_seq[si_b]:
                        # Extend leftward.
                        la, lb = pa, pb
                        while la > 0 and lb > 0 and seq_a.hashes[la - 1] == seq_b.hashes[lb - 1]:
                            la -= 1
                            lb -= 1

                        # Extend rightward from the original positions.
                        ra, rb = pa + 1, pb + 1
                        while (
                            ra < len(seq_a.hashes)
                            and rb < len(seq_b.hashes)
                            and seq_a.hashes[ra] == seq_b.hashes[rb]
                        ):
                            ra += 1
                            rb += 1

                        run_len = ra - la
                        if run_len < min_run:
                            continue

                        key = (si_a, la, si_b, lb)
                        if key in seen:
                            continue
                        seen.add(key)

                        clones.append(
                            SequenceClone(
                                left=seq_a,
                                left_range=(la, ra),
                                right=seq_b,
                                right_range=(lb, rb),
                                length=run_len,
                                method="winnowing",
                            )
                        )

    return clones


# ---------------------------------------------------------------------------
# Method B: Generalized suffix array + LCP
# ---------------------------------------------------------------------------


def _build_int_sequence(seqs: list[StatementSeq]) -> tuple[list[int], list[tuple[int, int]], list[int]]:
    """Concatenate all statement hashes into an integer sequence.

    Each unique 16-byte hash maps to a positive integer starting from 1.
    Separators between sequences use *distinct* values so the suffix array
    never spuriously matches across them.  Separator values are allocated
    after all real hashes have been collected, ensuring they exceed the
    maximum hash integer and are unique per sequence.

    Returns:
        - ``int_seq``:  the concatenated integer sequence
        - ``offsets``:  list of (seq_index, stmt_index) for each position;
          separator positions use the sentinel ``(-1, -1)``
        - ``sep_positions``: positions of separator values in int_seq
    """
    hash_to_int: dict[bytes, int] = {}
    next_hash_int = count(1)

    def get_int(h: bytes) -> int:
        v = hash_to_int.get(h)
        if v is None:
            v = next(next_hash_int)
            hash_to_int[h] = v
        return v

    # First pass: build the integer sequence for real hashes, with -1
    # placeholders for separators.
    int_seq: list[int] = []
    offsets: list[tuple[int, int]] = []
    sep_positions: list[int] = []

    for si, seq in enumerate(seqs):
        for stmt_i, h in enumerate(seq.hashes):
            int_seq.append(get_int(h))
            offsets.append((si, stmt_i))
        sep_positions.append(len(int_seq))
        int_seq.append(-1)  # placeholder; filled in below
        offsets.append((-1, -1))

    # Second pass: assign unique separator values strictly greater than any
    # real hash integer. ``next(next_hash_int)`` gives the next unused value.
    first_sep_value = next(next_hash_int)
    for i, pos in enumerate(sep_positions):
        int_seq[pos] = first_sep_value + i

    return int_seq, offsets, sep_positions


def _build_suffix_array_naive(seq: list[int]) -> list[int]:
    """O(n² log n) suffix array construction (for small n)."""
    n = len(seq)
    return sorted(range(n), key=lambda i: seq[i:])


def _compute_lcp_naive(seq: list[int], sa: list[int]) -> list[int]:
    """Compute LCP array naively from sorted suffix array."""
    n = len(sa)
    lcp = [0] * n
    for i in range(1, n):
        s1 = sa[i - 1]
        s2 = sa[i]
        l = 0
        while s1 + l < n and s2 + l < n and seq[s1 + l] == seq[s2 + l]:
            l += 1
        lcp[i] = l
    return lcp


def _crosses_separator(
    pos: int,
    length: int,
    sorted_sep_positions: list[int],
) -> bool:
    """Return True if [pos, pos+length) contains any separator.

    ``sorted_sep_positions`` must be sorted ascending; this uses binary
    search so the check is O(log n) instead of O(length).
    """
    idx = bisect_left(sorted_sep_positions, pos)
    return (
        idx < len(sorted_sep_positions)
        and sorted_sep_positions[idx] < pos + length
    )


def find_clones_suffix_array(
    seqs: list[StatementSeq],
    min_run: int = 4,
) -> list[SequenceClone]:
    """Find duplicated statement runs using a generalized suffix array + LCP.

    Uses ``pydivsufsort`` if available; falls back to a pure-Python O(n²
    log n) implementation that is correct but slow for large inputs.
    """
    if not seqs:
        return []

    int_seq, offsets, sep_positions_list = _build_int_sequence(seqs)

    n = len(int_seq)

    if HAVE_PDS:
        sa = pydivsufsort.divsufsort(int_seq)
    else:
        sa = _build_suffix_array_naive(int_seq)

    lcp = _compute_lcp_naive(int_seq, sa)

    clones: list[SequenceClone] = []
    seen: set[tuple[int, int, int, int]] = set()

    for i in range(1, n):
        run_len = lcp[i]
        if run_len < min_run:
            continue

        pos_a = sa[i - 1]
        pos_b = sa[i]

        # Get the sequence/position identifiers.
        off_a = offsets[pos_a]
        off_b = offsets[pos_b]

        si_a, stmt_a = off_a
        si_b, stmt_b = off_b

        # Skip separators and self-matches.
        if si_a < 0 or si_b < 0:
            continue
        if si_a == si_b:
            continue

        # Filter out runs that straddle a separator.
        if _crosses_separator(pos_a, run_len, sep_positions_list):
            continue
        if _crosses_separator(pos_b, run_len, sep_positions_list):
            continue

        # Clamp run_len to available statements.
        max_a = len(seqs[si_a].hashes) - stmt_a
        max_b = len(seqs[si_b].hashes) - stmt_b
        actual_run = min(run_len, max_a, max_b)

        if actual_run < min_run:
            continue

        # Canonicalize order so (si_a, stmt_a) <= (si_b, stmt_b).
        if (si_a, stmt_a) > (si_b, stmt_b):
            si_a, si_b = si_b, si_a
            stmt_a, stmt_b = stmt_b, stmt_a

        key = (si_a, stmt_a, si_b, stmt_b)
        if key in seen:
            continue
        seen.add(key)

        clones.append(
            SequenceClone(
                left=seqs[si_a],
                left_range=(stmt_a, stmt_a + actual_run),
                right=seqs[si_b],
                right_range=(stmt_b, stmt_b + actual_run),
                length=actual_run,
                method="suffix_array",
            )
        )

    return clones


# ---------------------------------------------------------------------------
# Triviality filter
# ---------------------------------------------------------------------------


def filter_sequence_clones(
    clones: list[SequenceClone],
    min_run: int = 4,
) -> list[SequenceClone]:
    """Filter trivial clones.

    Rules applied:
    1. Minimum run length: ``clone.length >= min_run``.
    2. Non-trivial statement mix: the run must include at least 2 distinct
       statement kinds.  A run of nothing but ``assignment`` or nothing but
       ``expression_statement`` is considered trivial.
    """
    result: list[SequenceClone] = []
    for clone in clones:
        if clone.length < min_run:
            continue

        # Collect statement kinds for the left range.
        left_kinds = clone.left.kinds[clone.left_range[0] : clone.left_range[1]]
        if len(set(left_kinds)) < 2:
            continue

        result.append(clone)
    return result


__all__ = [
    "StatementSeq",
    "SequenceClone",
    "build_statement_seqs",
    "winnowing",
    "find_clones_winnowing",
    "find_clones_suffix_array",
    "filter_sequence_clones",
]
