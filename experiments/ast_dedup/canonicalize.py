"""Phase 2 — AST canonicalizer for near-duplicate detection experiment.

Given a ``PyTree`` (from Phase 1) and a ``PyScopeResolver`` (existing), this
module produces a canonical form of each candidate subtree where variables
are alpha-renamed to ``bound_{i}`` / ``free_{i}`` and literals are replaced
with placeholders. The canonical form is what downstream phases hash for
near-duplicate detection.

Public API:
    - ``CanonicalSubtree`` — frozen dataclass of one canonicalized subtree
    - ``CanonicalizerConfig`` — ablation knobs
    - ``canonicalize(root, qn_at, def_loc, file_path, raw_hashes, config)``
    - ``iter_candidates(tree)`` — yields candidate root ``PyNode``s
    - ``compute_raw_hashes(root)`` — Pass A raw Merkle hashes
    - ``canonicalize_file(path, scope_resolver, config)`` — tie it together

Findings on the open question (qn stability for comprehensions / walrus):
    ``PyScopeResolver.references_in_file`` reports 1-indexed line numbers,
    while ``PyScopeResolver.scopes_in_file`` uses 0-indexed line numbers.
    This module normalizes both to 0-indexed so they match tree-sitter's
    ``PyNode.start_point`` coordinates.

    Empirically (see `test_canonicalize.py::test_comprehension_bindings`),
    comprehension variables get their own qualified name nested under the
    enclosing function (e.g. ``module.f.<listcomp>.i``). Walrus bindings
    bind in an enclosing scope, so their qn is rooted at the enclosing
    function, which is the correct behaviour for alpha-renaming.
"""

from __future__ import annotations

import keyword
from collections import Counter
from dataclasses import dataclass
from hashlib import blake2b
from itertools import count
from typing import Iterable, Iterator, Optional

from emend import emend_core

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PYTHON_KEYWORDS: frozenset[str] = frozenset(keyword.kwlist) | {"self", "cls"}

_FUNCTION_DEFINITION_KINDS: frozenset[str] = frozenset(
    {"function_definition", "class_definition", "decorated_definition"}
)
_CONTROL_FLOW_KINDS: frozenset[str] = frozenset(
    {"if_statement", "for_statement", "while_statement", "try_statement"}
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CanonicalizerConfig:
    """Ablation knobs for :func:`canonicalize`."""

    rename_attrs: bool = False
    rename_string_literals: bool = True
    rename_numeric_literals: bool = True
    keep_literal_equality: bool = False
    min_candidate_nodes: int = 8
    min_candidate_depth: int = 3


@dataclass(frozen=True)
class CanonicalSubtree:
    """A canonicalized subtree: structural shape + alpha-renamed hash.

    Phase 3 consumes ``canonical_hash`` (for exact dedup), ``kind_seq`` /
    ``token_seq`` (for shingled / simhash strategies), and
    ``child_merkle_bag`` (for bag-of-subtrees MinHash).
    """

    file: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int

    kind_seq: tuple[str, ...]
    token_seq: tuple[str, ...]
    depth: int
    node_count: int

    raw_merkle: bytes
    canonical_hash: bytes

    unique_tokens: int
    unique_non_keyword_tokens: int
    kind_histogram: tuple[tuple[str, int], ...]

    # Multiset of child Merkle hashes collected during the canonicalization
    # walk (depth <= 2). Phase 3's ``BagOfSubtreesMinHash`` consumes this.
    child_merkle_bag: tuple[bytes, ...]


# ---------------------------------------------------------------------------
# Pass A: raw Merkle hashes
# ---------------------------------------------------------------------------


def compute_raw_hashes(root) -> dict[tuple[int, int], bytes]:
    """Post-order walk producing a raw Merkle hash for every node.

    The hash incorporates:
      - ``node.kind``
      - for internal named nodes, the field-tagged hashes of each named child
      - for leaves (both named like ``identifier`` and anonymous like ``+``),
        the raw source text — so two otherwise-identical trees that differ
        only in an operator or identifier hash differently.

    Returns a dict keyed by ``(start_byte, end_byte)`` for direct lookup
    at candidate roots.
    """
    hashes: dict[tuple[int, int], bytes] = {}

    def walk(n) -> bytes:
        h = blake2b(digest_size=16)
        h.update(n.kind.encode())
        if n.named_child_count > 0:
            for field_name, child in n.named_children_with_fields():
                h.update((field_name or "").encode("utf-8", errors="ignore"))
                h.update(walk(child))
        else:
            # Leaf (named or anonymous): include text so distinct identifiers
            # and operators hash differently.
            h.update(n.text().encode("utf-8", errors="ignore"))
        digest = h.digest()
        hashes[(n.start_byte, n.end_byte)] = digest
        return digest

    walk(root)
    return hashes


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------


def _count_named_statements(block) -> int:
    """Return the number of named children of a ``block`` node."""
    if block is None:
        return 0
    return block.named_child_count


def iter_candidates(tree) -> Iterator:
    """Yield candidate roots per the Phase 2 spec.

    Candidates are:
      1. every ``function_definition`` / ``class_definition`` /
         ``decorated_definition`` node;
      2. the ``body`` block of a function/class whose body has >= 2
         statements;
      3. every ``if_statement`` / ``for_statement`` / ``while_statement`` /
         ``try_statement`` whose body has >= 2 statements.

    TODO(Phase 5): also yield maximal runs of >= 3 sibling statements inside
    a ``block`` for sibling-sequence clone detection.
    """
    root = tree.root

    def walk(n) -> Iterator:
        k = n.kind
        if k in _FUNCTION_DEFINITION_KINDS:
            yield n
            body = n.child_by_field_name("body")
            if body is not None and _count_named_statements(body) >= 2:
                yield body
        elif k in _CONTROL_FLOW_KINDS:
            body = n.child_by_field_name("body") or n.child_by_field_name(
                "consequence"
            )
            if body is not None and _count_named_statements(body) >= 2:
                yield n
        for child in n.named_children():
            yield from walk(child)

    yield from walk(root)


# ---------------------------------------------------------------------------
# Pass B: alpha-renamed canonicalization
# ---------------------------------------------------------------------------


def _compute_depth(n) -> int:
    """Max depth of the subtree rooted at ``n``, counting named children."""
    if n.named_child_count == 0:
        return 1
    best = 0
    for c in n.named_children():
        d = _compute_depth(c)
        if d > best:
            best = d
    return 1 + best


def _count_named_nodes(n) -> int:
    """Count all named descendants including ``n`` itself."""
    total = 1
    for c in n.named_children():
        total += _count_named_nodes(c)
    return total


def _is_bound_inside(
    qn: str,
    def_loc: dict[str, tuple[int, int]],
    start_line: int,
    end_line: int,
) -> bool:
    """Return True if qn's definition site falls within [start_line, end_line]."""
    loc = def_loc.get(qn)
    if loc is None:
        return False
    return start_line <= loc[0] <= end_line


def canonicalize(
    R,
    qn_at: dict[tuple[int, int], str],
    def_loc: dict[str, tuple[int, int]],
    file_path: str,
    raw_hashes: dict[tuple[int, int], bytes],
    config: Optional[CanonicalizerConfig] = None,
) -> CanonicalSubtree:
    """Canonicalize a single subtree rooted at ``R``.

    Parameters:
      - ``R``: a ``PyNode`` candidate root
      - ``qn_at``: map from 0-indexed ``(line, col)`` of a reference site to
        its target qualified name
      - ``def_loc``: map from qualified name to the 0-indexed ``(line, col)``
        of its definition (the first write/definition reference)
      - ``file_path``: absolute path of the source file (for reporting)
      - ``raw_hashes``: output of :func:`compute_raw_hashes` for the whole file
      - ``config``: optional :class:`CanonicalizerConfig`
    """
    cfg = config or CanonicalizerConfig()
    rename: dict[str, str] = {}
    bound_counter = count()
    free_counter = count()
    str_map: dict[str, str] = {}
    num_map: dict[str, str] = {}
    str_counter = count()
    num_counter = count()
    kind_seq: list[str] = []
    token_seq: list[str] = []
    child_merkle_bag: list[bytes] = []

    r_start_line = R.start_point[0]
    r_end_line = R.end_point[0]

    def assign(qn: str) -> str:
        tok = rename.get(qn)
        if tok is not None:
            return tok
        if _is_bound_inside(qn, def_loc, r_start_line, r_end_line):
            tok = f"bound_{next(bound_counter)}"
        else:
            tok = f"free_{next(free_counter)}"
        rename[qn] = tok
        return tok

    def lit_token(prefix: str, text: str, ctr, mapping: dict[str, str]) -> str:
        if not cfg.keep_literal_equality:
            return prefix
        existing = mapping.get(text)
        if existing is not None:
            return existing
        tok = f"{prefix}_{next(ctr)}"
        mapping[text] = tok
        return tok

    def leaf_token(n) -> Optional[str]:
        k = n.kind
        if k == "identifier":
            qn = qn_at.get((n.start_point[0], n.start_point[1]))
            if qn is None:
                return "free_unresolved"
            return assign(qn)
        if k == "string":
            if not cfg.rename_string_literals:
                return n.text()
            return lit_token("str", n.text(), str_counter, str_map)
        if k == "integer" or k == "float":
            if not cfg.rename_numeric_literals:
                return n.text()
            return lit_token("num", n.text(), num_counter, num_map)
        if k in ("true", "false", "none", "True", "False", "None"):
            return k
        if k == "type_identifier":
            qn = qn_at.get((n.start_point[0], n.start_point[1]))
            return assign(qn) if qn is not None else "free_type"
        # Fallback: anonymous keyword leaves (``return``, ``if``, ...)
        return n.text()

    def walk(n, depth: int = 0) -> bytes:
        if n.is_named:
            kind_seq.append(n.kind)

        # String literals: tree-sitter parses a string as ``string`` with
        # child nodes (``string_start``, ``string_content``, ``string_end``).
        # Treat the whole thing as a leaf so ``rename_string_literals``
        # correctly collapses distinct strings to the same token.
        if n.kind == "string":
            text = n.text()
            if not cfg.rename_string_literals:
                tok = text
            else:
                tok = lit_token("str", text, str_counter, str_map)
            token_seq.append(tok)
            leaf_h = blake2b(
                b"string" + tok.encode(), digest_size=16
            ).digest()
            if depth <= 2:
                child_merkle_bag.append(leaf_h)
            return leaf_h

        # Special-case attribute access: recurse into the object, but keep
        # the attribute / method name literal unless rename_attrs is set.
        if n.kind == "attribute":
            obj = n.child_by_field_name("object")
            attr = n.child_by_field_name("attribute")
            h = blake2b(digest_size=16)
            h.update(n.kind.encode())
            if obj is not None:
                oh = walk(obj, depth + 1)
                h.update(oh)
            if attr is not None:
                if cfg.rename_attrs:
                    qn = qn_at.get(
                        (attr.start_point[0], attr.start_point[1])
                    )
                    if qn is not None:
                        tok = assign(qn)
                    else:
                        tok = "free_attr"
                else:
                    tok = attr.text()
                kind_seq.append(attr.kind)
                token_seq.append(tok)
                leaf_h = blake2b(
                    attr.kind.encode() + tok.encode(), digest_size=16
                ).digest()
                h.update(leaf_h)
            digest = h.digest()
            if depth <= 2:
                child_merkle_bag.append(digest)
            return digest

        # True leaf (no children of any kind).
        if n.child_count == 0:
            tok = leaf_token(n)
            if tok is not None:
                token_seq.append(tok)
            leaf_h = blake2b(
                n.kind.encode() + (tok or "").encode(), digest_size=16
            ).digest()
            if depth <= 2:
                child_merkle_bag.append(leaf_h)
            return leaf_h

        h = blake2b(digest_size=16)
        h.update(n.kind.encode())
        # Walk all children (named and anonymous) so operators and
        # punctuation contribute to the structural hash. Anonymous children
        # hash as their raw text; named children recurse.
        for i in range(n.child_count):
            c = n.child(i)
            if c is None:
                continue
            if c.is_named:
                ch = walk(c, depth + 1)
                h.update(ch)
            else:
                h.update(c.kind.encode())
                h.update(c.text().encode())
        digest = h.digest()
        if depth <= 2:
            child_merkle_bag.append(digest)
        return digest

    canonical = walk(R)
    raw = raw_hashes.get((R.start_byte, R.end_byte), b"")
    node_count = _count_named_nodes(R)
    depth = _compute_depth(R)
    kind_hist = tuple(sorted(Counter(kind_seq).items()))
    unique = len(set(token_seq))
    unique_non_kw = len(set(token_seq) - PYTHON_KEYWORDS)

    return CanonicalSubtree(
        file=file_path,
        start_byte=R.start_byte,
        end_byte=R.end_byte,
        start_line=R.start_point[0],
        end_line=R.end_point[0],
        kind_seq=tuple(kind_seq),
        token_seq=tuple(token_seq),
        depth=depth,
        node_count=node_count,
        raw_merkle=raw,
        canonical_hash=canonical,
        unique_tokens=unique,
        unique_non_keyword_tokens=unique_non_kw,
        kind_histogram=kind_hist,
        child_merkle_bag=tuple(child_merkle_bag),
    )


# ---------------------------------------------------------------------------
# High-level driver
# ---------------------------------------------------------------------------


def _build_qn_at_and_def_loc(
    refs: Iterable[tuple],
) -> tuple[dict[tuple[int, int], str], dict[str, tuple[int, int]]]:
    """Normalize ``references_in_file`` to 0-indexed line coordinates and
    derive the per-qn definition location (first write/definition/parameter
    reference).
    """
    qn_at: dict[tuple[int, int], str] = {}
    def_loc: dict[str, tuple[int, int]] = {}
    for qn, line, col, _sb, _eb, kind, _ann in refs:
        # Scope resolver references are 1-indexed for line, 0-indexed for col.
        key = (line - 1, col)
        qn_at[key] = qn
        if qn not in def_loc and kind in ("definition", "write", "parameter"):
            def_loc[qn] = key
    return qn_at, def_loc


def canonicalize_file(
    path: str,
    scope_resolver,
    config: Optional[CanonicalizerConfig] = None,
) -> list[CanonicalSubtree]:
    """Parse ``path``, index it in ``scope_resolver``, and canonicalize every
    candidate subtree.

    Returns an empty list if the file cannot be parsed.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()
    scope_resolver.index_file(path, source)

    tree = emend_core.parse_file(path)
    if tree is None:
        return []

    refs = scope_resolver.references_in_file(path)
    qn_at, def_loc = _build_qn_at_and_def_loc(refs)

    raw_hashes = compute_raw_hashes(tree.root)

    out: list[CanonicalSubtree] = []
    for cand in iter_candidates(tree):
        sub = canonicalize(cand, qn_at, def_loc, path, raw_hashes, config)
        out.append(sub)
    return out


__all__ = [
    "CanonicalSubtree",
    "CanonicalizerConfig",
    "PYTHON_KEYWORDS",
    "canonicalize",
    "canonicalize_file",
    "compute_raw_hashes",
    "iter_candidates",
]
