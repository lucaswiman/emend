"""Production duplicate code detection via AST canonicalization.

- Exact canonical Merkle hashes (variable-renamed, literal-preserving)
- Contiguous sibling-statement duplicate runs
- Boilerplate suppression via duplicate_heuristics

Shared backend for CLI (``emend analyze dupes``), lint, and MCP.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import blake2b
from pathlib import Path
from typing import Any, Iterator

from emend import emend_core
from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)
from emend.duplicate_heuristics import (
    is_abstract_stub,
    is_trivial_validator,
    is_property_wrapper,
    is_tiny_same_file_fragment,
    is_init_self_assignment,
    is_dunder_boilerplate,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cached payloads include the containing function/class symbol. Bump this whenever
# the payload shape changes so stale rows cannot produce different output from
# the cold (fresh-parse) path.
DUP_CACHE_VERSION = "4"

# Canonicalizer: node kinds that are candidate roots
_FUNCTION_KINDS: frozenset[str] = frozenset({
    "function_definition", "class_definition", "decorated_definition",
})
_CONTROL_FLOW_KINDS: frozenset[str] = frozenset({
    "if_statement", "for_statement", "while_statement", "try_statement",
})

# Python keywords and builtins kept as-is during canonicalization
import keyword as _keyword_mod
_PYTHON_KEYWORDS: frozenset[str] = frozenset(_keyword_mod.kwlist) | {"self", "cls"}

# Minimum contiguous run; individual shared statements are not duplicates.
MIN_SEQUENCE_STATEMENTS = 4


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateMember:
    """One member of a duplicate cluster."""
    file: str
    symbol: str
    start_line: int
    end_line: int
    node_count: int = 0
    stmt_count: int = 0


@dataclass
class DuplicateCluster:
    """A group of code locations that are duplicates of each other."""
    kind: str  # "exact" or "sequence"
    score: float
    members: list[DuplicateMember] = field(default_factory=list)
    explanation: str = ""


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------


def _count_named_statements(block) -> int:
    """Return number of named children of a block node."""
    if block is None:
        return 0
    return block.named_child_count


def _iter_candidates(tree) -> Iterator:
    """Yield candidate subtree roots for canonicalization."""
    root = tree.root

    def walk(n) -> Iterator:
        k = n.kind
        if k in _FUNCTION_KINDS:
            yield n
            body = n.child_by_field_name("body")
            if body is not None and _count_named_statements(body) >= 2:
                yield body
        elif k in _CONTROL_FLOW_KINDS:
            body = n.child_by_field_name("body") or n.child_by_field_name("consequence")
            if body is not None and _count_named_statements(body) >= 2:
                yield n
        for child in n.named_children():
            yield from walk(child)

    yield from walk(root)


# ---------------------------------------------------------------------------
# Scope-aware canonicalization (production rules)
# ---------------------------------------------------------------------------


def _build_qn_at(file_path: str, scope_resolver) -> tuple[dict[tuple[int, int], str], dict[str, tuple[int, int]]]:
    """Build position-to-QN and QN-to-def-position maps from scope resolver.

    Returns (qn_at, def_loc) where:
    - qn_at maps (row_0indexed, col) -> qualified_name
    - def_loc maps qualified_name -> (start_line_0indexed, col) of definition

    Both are derived from ``references_in_file`` which returns
    ``(qn, line_1idx, col, sb, eb, kind, annotation)`` tuples.
    """
    qn_at: dict[tuple[int, int], str] = {}
    def_loc: dict[str, tuple[int, int]] = {}

    try:
        refs = scope_resolver.references_in_file(file_path)
    except Exception:
        logger.debug("references_in_file failed for %s", file_path, exc_info=True)
        refs = []

    for ref in refs:
        qn = ref[0]
        line_1 = ref[1]
        col = ref[2]
        kind = ref[5] if len(ref) > 5 else ""
        key = (line_1 - 1, col)  # convert to 0-indexed line
        qn_at[key] = qn
        # Record the first definition/write/parameter site as the definition loc
        if qn not in def_loc and kind in ("definition", "write", "parameter"):
            def_loc[qn] = key

    return qn_at, def_loc


def _build_symbol_index(content: str, ext: str = "py") -> list[tuple[str, int, int]]:
    """Build ``(qn, start_line_0indexed, end_line_0indexed)`` from Rust symbol collector.

    Only includes class and function/method definitions (not variables or
    references), which is what ``_find_containing_symbol`` needs.
    """
    raw = emend_core.collect_symbols_from_str(content, ext=ext)
    result: list[tuple[str, int, int]] = []

    def _collect(syms: list[dict], prefix: str = "") -> None:
        for d in syms:
            kind = d.get("kind", "")
            if kind in ("variable", "reference"):
                continue
            name = d.get("name", "")
            qn = f"{prefix}.{name}" if prefix else name
            # line and end_line are 1-indexed from collect_symbols_from_str
            sl = d["line"] - 1
            el = d["end_line"] - 1
            result.append((qn, sl, el))
            _collect(d.get("children", []), prefix=qn)

    _collect(raw)
    return sorted(result, key=lambda t: (t[1], -(t[2] - t[1])))


def _is_bound_inside(qn: str, def_loc: dict[str, tuple[int, int]], subtree_start: int, subtree_end: int) -> bool:
    """Check if a qualified name's definition is inside the subtree's line range."""
    loc = def_loc.get(qn)
    if loc is None:
        return False
    return subtree_start <= loc[0] <= subtree_end


def _node_depth(node) -> int:
    """Compute the depth of an AST subtree."""
    if node.named_child_count == 0:
        return 1
    return 1 + max(_node_depth(c) for c in node.named_children())


def canonicalize_subtree(
    node,
    qn_at: dict[tuple[int, int], str],
    def_loc: dict[str, tuple[int, int]],
    *,
    binding_scope: tuple[int, int] | None = None,
    bound_map: dict[str, str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Canonicalize a subtree with production rules.

    Production canonicalization:
    - Alpha-rename bindings/usages defined inside the subtree
    - Keep free identifiers, attribute names and keyword names literal
    - Keep literal constants (strings, numbers, booleans, None)
    - Preserve operators and punctuation; ignore comments/trivia

    Returns (kind_seq, token_seq) as pre-order sequences.
    """
    subtree_start, subtree_end = binding_scope or (node.start_point[0], node.end_point[0])

    if bound_map is None:
        bound_map = {}
    class_scopes: set[str] = set()

    def record_class(n):
        if n.kind == "class_definition":
            name = n.child_by_field_name("name")
            if name is not None and (qn := qn_at.get(name.start_point)):
                class_scopes.add(qn)

    def collect_classes(n):
        record_class(n)
        for child in n.named_children():
            collect_classes(child)

    collect_classes(node)
    ancestor = node.parent()
    while ancestor is not None:
        record_class(ancestor)
        ancestor = ancestor.parent()

    kind_seq: list[str] = []
    token_seq: list[str] = []

    def walk(n, preserve_name: bool = False):
        if n.kind == "comment":
            return

        kind_seq.append(n.kind)

        if n.child_count == 0:
            text = n.text()
            if n.kind == "identifier" and not preserve_name:
                qn = qn_at.get(n.start_point)
                if (qn and qn.rpartition(".")[0] not in class_scopes
                        and _is_bound_inside(qn, def_loc, subtree_start, subtree_end)):
                    text = bound_map.setdefault(qn, f"bound_{len(bound_map)}")
            token_seq.append(text)
        else:
            # These names are labels, not variable references, even when the
            # resolver reports a same-spelled local at their position.
            label = n.child_by_field_name(
                "attribute" if n.kind == "attribute" else "name"
            ) if n.kind in ("attribute", "keyword_argument") else None
            for child in n.children():
                walk(child, label is not None and child.start_byte == label.start_byte)

    walk(node)
    return tuple(kind_seq), tuple(token_seq)


def _canonical_hash(kind_seq: tuple[str, ...], token_seq: tuple[str, ...]) -> bytes:
    """Compute blake2b hash of the canonical form."""
    h = blake2b(digest_size=16)
    for k in kind_seq:
        h.update(k.encode())
        h.update(b"|")
    h.update(b"##")
    for t in token_seq:
        h.update(t.encode())
        h.update(b"|")
    return h.digest()


def _find_containing_symbol(
    line: int,
    symbol_index: list[tuple[str, int, int]],
) -> str:
    """Find the innermost symbol containing a given line.

    *symbol_index* is ``[(qn, start_line, end_line), ...]``.  The
    innermost symbol is the one with the smallest span (end - start).
    """
    best = ""
    best_span = 999999
    for qn, sl, el in symbol_index:
        if sl <= line <= el:
            span = el - sl
            if span < best_span:
                best = qn
                best_span = span
    return best


# ---------------------------------------------------------------------------
# Triviality filter (production thresholds)
# ---------------------------------------------------------------------------

_MIN_NODE_COUNT = 8
_MIN_DEPTH = 3
_MIN_UNIQUE_NON_KW = 4
_HALSTEAD_VOLUME_MIN = 30.0

_BLOCKED_ROOT_KINDS: frozenset[str] = frozenset(
    {"return_statement", "pass_statement", "raise_statement"}
)


def _is_trivial(
    kind_seq: tuple[str, ...],
    token_seq: tuple[str, ...],
    node_count: int,
    depth: int,
) -> bool:
    """Return True if the subtree should be excluded from dup detection."""
    if node_count < _MIN_NODE_COUNT:
        return True
    if depth < _MIN_DEPTH:
        return True
    unique_non_kw = len(set(token_seq) - _PYTHON_KEYWORDS)
    if unique_non_kw < _MIN_UNIQUE_NON_KW:
        return True
    vocab = len(set(kind_seq)) + len(set(token_seq))
    volume = node_count * math.log2(max(vocab, 2))
    if volume < _HALSTEAD_VOLUME_MIN:
        return True
    if kind_seq and kind_seq[0] in _BLOCKED_ROOT_KINDS:
        return True
    return False


# ---------------------------------------------------------------------------
# Statement canonicalization for sibling-sequence detection
# ---------------------------------------------------------------------------


def _stmt_canonical_hash(
    stmt,
    func_start: int,
    func_end: int,
    qn_at: dict[tuple[int, int], str],
    def_loc: dict[str, tuple[int, int]],
    bound_map: dict[str, str],
) -> bytes:
    """Hash a statement using the same semantics as exact subtree detection."""
    return _canonical_hash(*canonicalize_subtree(
        stmt, qn_at, def_loc, binding_scope=(func_start, func_end), bound_map=bound_map,
    ))


# ---------------------------------------------------------------------------
# Public API: production cache helpers
# ---------------------------------------------------------------------------


def canonicalize_file_for_cache(
    file_path: str,
    content: str,
    scope_resolver,
) -> list[dict]:
    """Compute production canonical subtree payloads for *file_path*.

    *scope_resolver* must already have *file_path* indexed
    (``scope_resolver.index_file(file_path, content)`` should have been called
    before invoking this function).

    Returns a list of dicts (one per non-trivial candidate subtree) with keys:
      ``start_line``, ``end_line``, ``root_kind``, ``node_count``,
      ``total_lines``, ``canonical_hash`` (hex str), ``score``,
      ``symbol`` (containing symbol QN, ``""`` at module level),
      ``kind_seq`` (list of str), ``token_seq`` (list of str).

    Returns an empty list if the file cannot be parsed.
    """
    tree = emend_core.parse_source(content, "py")
    if tree is None:
        return []

    qn_at, def_loc = _build_qn_at(file_path, scope_resolver)
    symbol_index = _build_symbol_index(content, ext="py")

    out: list[dict] = []
    for cand in _iter_candidates(tree):
        try:
            kind_seq, token_seq = canonicalize_subtree(cand, qn_at, def_loc)
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("canonicalize_subtree failed in %s", file_path, exc_info=True)
            continue

        node_count = len(kind_seq)
        depth = _node_depth(cand)

        if _is_trivial(kind_seq, token_seq, node_count, depth):
            continue

        start_line = cand.start_point[0]
        end_line = cand.end_point[0]
        total_lines = end_line - start_line + 1
        symbol = _find_containing_symbol(start_line, symbol_index)

        canon_hash = _canonical_hash(kind_seq, token_seq)
        vocab = len(set(kind_seq)) + len(set(token_seq))
        score = node_count * math.log2(max(vocab, 2))

        out.append(
            {
                "start_line": start_line,
                "end_line": end_line,
                "symbol": symbol,
                "root_kind": kind_seq[0] if kind_seq else "",
                "node_count": node_count,
                "total_lines": total_lines,
                "canonical_hash": canon_hash.hex(),
                "score": score,
                "kind_seq": list(kind_seq),
                "token_seq": list(token_seq),
            }
        )
    return out


def build_statement_seqs_for_cache(
    file_path: str,
    content: str,
    scope_resolver,
) -> list[dict]:
    """Compute production sibling-sequence payloads for *file_path*.

    *scope_resolver* must already have *file_path* indexed.

    Returns a list of dicts (one per function with >= 2 statements) with keys:
      ``function_qn``, ``start_line``, ``end_line``,
      ``hashes`` (list of hex strings), ``line_ranges`` (list of [start, end]),
      ``kinds`` (list of str).

    Returns an empty list if the file cannot be parsed.
    """
    tree = emend_core.parse_source(content, "py")
    if tree is None:
        return []

    qn_at, def_loc = _build_qn_at(file_path, scope_resolver)

    symbol_index = _build_symbol_index(content)

    out: list[dict] = []

    def visit(node) -> None:
        if node.kind == "function_definition":
            func_start = node.start_point[0]
            func_end = node.end_point[0]
            func_qn = _find_containing_symbol(func_start, symbol_index)

            body = node.child_by_field_name("body")
            if body is not None:
                hashes_list: list[str] = []
                ranges_list: list[list[int]] = []
                kinds_list: list[str] = []
                bound_map: dict[str, str] = {}
                parameters = node.child_by_field_name("parameters")
                if parameters is not None:
                    canonicalize_subtree(parameters, qn_at, def_loc,
                                         binding_scope=(func_start, func_end), bound_map=bound_map)

                for stmt in body.named_children():
                    if stmt.kind == "comment":
                        continue
                    try:
                        h = _stmt_canonical_hash(
                            stmt, func_start, func_end, qn_at, def_loc, bound_map
                        )
                    except BUG_EXCEPTIONS:
                        raise
                    except Exception:
                        logger.debug("_stmt_canonical_hash failed in %s", file_path, exc_info=True)
                        continue
                    hashes_list.append(h.hex())
                    ranges_list.append([stmt.start_point[0], stmt.end_point[0]])
                    kinds_list.append(stmt.kind)

                if len(hashes_list) >= 2:
                    out.append(
                        {
                            "function_qn": func_qn,
                            "start_line": func_start,
                            "end_line": func_end,
                            "hashes": hashes_list,
                            "line_ranges": ranges_list,
                            "kinds": kinds_list,
                        }
                    )

            # Recurse into body for nested functions.
            if body is not None:
                for child in body.named_children():
                    visit(child)

        elif node.kind in ("class_definition", "decorated_definition"):
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.named_children():
                    visit(child)
        else:
            for child in node.named_children():
                visit(child)

    for child in tree.root.named_children():
        visit(child)

    return out


# ---------------------------------------------------------------------------
# File collection helper (production CLI / MCP)
# ---------------------------------------------------------------------------


def _collect_py_files(root_path: str) -> list[str]:
    """Collect Python source files under *root_path*."""
    from emend.file_collection import collect_source_files
    return collect_source_files(root_path, language="python")


# ---------------------------------------------------------------------------
# Shared pre-parse step (avoids double work when mode="all")
# ---------------------------------------------------------------------------


_FileData = dict[str, tuple[str, Any, dict, dict, list[tuple[str, int, int]]]]


def _preparse_files(
    py_files: list[str],
    symbol_scope: str | None,
) -> tuple[Any, _FileData]:
    """Read, parse, and index all *py_files* once.

    Returns ``(scope_resolver, file_data)`` where *file_data* maps each
    successfully parsed file to its ``(content, tree, qn_at, def_loc,
    symbol_index)`` tuple.
    """
    project_root = str(Path(py_files[0]).parent) if py_files else "."
    try:
        scope_resolver = emend_core.PyScopeResolver(project_root, "py")
    except TypeError:
        scope_resolver = emend_core.PyScopeResolver(project_root)

    file_data: _FileData = {}

    for file_path in py_files:
        if symbol_scope and symbol_scope not in file_path:
            continue
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        try:
            scope_resolver.index_file(file_path, content)
        except Exception:
            logger.debug("index_file failed for %s", file_path, exc_info=True)
        tree = emend_core.parse_source(content, "py")
        if tree is None:
            continue

        qn_at, def_loc = _build_qn_at(file_path, scope_resolver)
        ext = Path(file_path).suffix.lstrip(".") or "py"
        symbol_index = _build_symbol_index(content, ext=ext)
        file_data[file_path] = (content, tree, qn_at, def_loc, symbol_index)

    return scope_resolver, file_data


# ---------------------------------------------------------------------------
# Exact-duplicate detection (query layer)
# ---------------------------------------------------------------------------


def _subtree_cands_from_file_data(
    file_data: _FileData,
    min_lines: int,
) -> dict[bytes, list[dict]]:
    """Group canonicalized subtree candidates by canonical hash (fresh parse)."""
    hash_to_cands: dict[bytes, list[dict]] = {}
    for file_path, (content, tree, qn_at, def_loc, symbol_index) in file_data.items():
        for cand in _iter_candidates(tree):
            start_0 = cand.start_point[0]
            end_0 = cand.end_point[0]
            if end_0 - start_0 + 1 < min_lines:
                continue

            try:
                kind_seq, token_seq = canonicalize_subtree(cand, qn_at, def_loc)
            except BUG_EXCEPTIONS:
                raise
            except Exception:
                logger.debug("canonicalize_subtree failed in %s", file_path, exc_info=True)
                continue

            nc = len(kind_seq)
            depth = _node_depth(cand)
            if _is_trivial(kind_seq, token_seq, nc, depth):
                continue

            ch = _canonical_hash(kind_seq, token_seq)
            symbol = _find_containing_symbol(start_0, symbol_index)
            unique_non_kw = len(set(token_seq) - _PYTHON_KEYWORDS)

            hash_to_cands.setdefault(ch, []).append({
                "file": file_path,
                "symbol": symbol,
                "start_line": start_0 + 1,
                "end_line": end_0 + 1,
                "node_count": nc,
                "unique_non_kw": unique_non_kw,
                "kind_seq": kind_seq,
                "token_seq": token_seq,
            })
    return hash_to_cands


def _subtree_cands_from_cached(
    cached: dict[str, dict],
    min_lines: int,
) -> dict[str, list[dict]]:
    """Group canonicalized subtree candidates by canonical hash (cached payloads)."""
    hash_to_cands: dict[str, list[dict]] = {}
    for file_path, payload in cached.items():
        for s in payload.get("subtrees", []):
            total_lines = s.get("total_lines", s["end_line"] - s["start_line"] + 1)
            if total_lines < min_lines:
                continue
            ks = tuple(s.get("kind_seq", ()))
            ts = tuple(s.get("token_seq", ()))
            unique_non_kw = len(set(ts) - _PYTHON_KEYWORDS)
            hash_to_cands.setdefault(s["canonical_hash"], []).append({
                "file": file_path,
                # Cache payloads generated by version 2 include the same
                # containing symbol as the fresh-parse path.
                "symbol": s.get("symbol", ""),
                "start_line": s["start_line"] + 1,
                "end_line": s["end_line"] + 1,
                "node_count": s["node_count"],
                "unique_non_kw": unique_non_kw,
                "kind_seq": ks,
                "token_seq": ts,
            })
    return hash_to_cands


def _exact_clusters_from_cands(
    hash_to_cands: dict,
    cross_file: bool | None,
) -> list[DuplicateCluster]:
    """Build exact-duplicate clusters from a hash→candidate-list mapping."""
    clusters: list[DuplicateCluster] = []
    for _ch, cands in hash_to_cands.items():
        if len(cands) < 2:
            continue
        files = {c["file"] for c in cands}
        if cross_file is True and len(files) < 2:
            continue
        if cross_file is False and len(files) > 1:
            continue

        members = [
            DuplicateMember(
                file=c["file"], symbol=c["symbol"],
                start_line=c["start_line"], end_line=c["end_line"],
                node_count=c["node_count"],
            )
            for c in cands
        ]
        avg_nc = sum(c["node_count"] for c in cands) / len(cands)
        avg_uniq = sum(c["unique_non_kw"] for c in cands) / len(cands)
        score = avg_nc * math.log2(max(avg_uniq + 1, 2))
        if len(files) > 1:
            score += 20.0

        rep = cands[0]
        ks = tuple(rep.get("kind_seq", ()))
        ts = tuple(rep.get("token_seq", ()))
        penalty = (
            is_abstract_stub(ks, ts)
            + is_trivial_validator(int(avg_nc), ks, ts)
            + is_property_wrapper(ks, ts)
            + is_tiny_same_file_fragment(members, int(avg_nc))
            + is_init_self_assignment(ks, ts)
            + is_dunder_boilerplate(rep.get("symbol", ""), ks)
        )
        score = max(0.0, score - penalty)
        if score <= 0.0:
            continue

        clusters.append(DuplicateCluster(
            kind="exact", score=score, members=members,
            explanation="same canonical subtree (alpha-renamed AST)",
        ))
    return clusters


def _query_exact_clusters(
    file_data: _FileData,
    min_lines: int,
    cross_file,
) -> list[DuplicateCluster]:
    """Find exact structural duplicates by grouping on canonical hash."""
    return _exact_clusters_from_cands(
        _subtree_cands_from_file_data(file_data, min_lines),
        cross_file,
    )


# ---------------------------------------------------------------------------
# Sequence-duplicate detection (query layer)
# ---------------------------------------------------------------------------


def _repeated_statement_runs(all_seqs: list[dict], covered):
    """Yield repeated substrings longest first using suffix-array LCP groups.

    Unique sequence terminators prevent matches crossing function boundaries.
    Prefix doubling and disjoint sets avoid materializing every substring.
    """
    tokens, locations = [], []
    token_ids = {}
    for i, seq in enumerate(all_seqs):
        for pos, token in enumerate(seq["hashes"]):
            tokens.append(token_ids.setdefault(token, len(token_ids)))
            locations.append((i, pos))
        tokens.append(-i - 1)
        locations.append((i, len(seq["hashes"])))
    size = len(tokens)
    suffixes, ranks = list(range(size)), tokens[:]
    width = 1
    while width < size:
        key = lambda i: (ranks[i], ranks[i + width] if i + width < size else -size - 1)
        suffixes.sort(key=key)
        updated = [0] * size
        for a, b in zip(suffixes, suffixes[1:]):
            updated[b] = updated[a] + (key(a) != key(b))
        ranks = updated
        if ranks[suffixes[-1]] == size - 1:
            break
        width *= 2
    for rank, start in enumerate(suffixes):
        ranks[start] = rank

    # Kasai's algorithm computes adjacent longest-common-prefix lengths.
    edges, length = [], 0
    for start in range(size):
        rank = ranks[start]
        if rank == 0:
            length = 0
            continue
        other = suffixes[rank - 1]
        while start + length < size and other + length < size and tokens[start + length] == tokens[other + length]:
            length += 1
        if length >= MIN_SEQUENCE_STATEMENTS:
            edges.append((length, rank - 1, rank))
        length = max(0, length - 1)

    parent = list(range(size))
    members = [{pos} for pos in suffixes]
    bounds = [{locations[pos][0]: (locations[pos][1], locations[pos][1])} for pos in suffixes]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    from itertools import groupby
    for length, equal_edges in groupby(sorted(edges, reverse=True), key=lambda edge: edge[0]):
        touched = set()
        for _, a, b in equal_edges:
            a, b = find(a), find(b)
            if a != b:
                if len(members[a]) < len(members[b]):
                    a, b = b, a
                parent[b] = a
                members[a].update(members[b])
                members[b].clear()
                for i, (start, end) in bounds[b].items():
                    lo, hi = bounds[a].get(i, (start, end))
                    bounds[a][i] = (min(start, lo), max(end, hi))
                bounds[b].clear()
            touched.add(a)
        for root in {find(i) for i in touched}:
            # A single-function group cannot contain two non-overlapping
            # copies if even its outermost occurrences overlap.
            if len(bounds[root]) == 1 and any(end - start < length for start, end in bounds[root].values()):
                continue
            # Prove containment from per-function occurrence bounds before
            # sorting or constructing potentially thousands of clone members.
            if all(any(left <= start and end + length <= right for left, right in covered[i])
                   for i, (start, end) in bounds[root].items()):
                continue
            yield length, sorted(locations[pos] for pos in members[root])


def _sequence_clusters_from_seqs(
    all_seqs: list[dict],
    min_lines: int,
    cross_file: bool | None,
) -> list[DuplicateCluster]:
    """Report maximal, non-overlapping copies of contiguous statement runs.

    Sharing one statement (or different runs with a bridging function) does
    not imply that whole function bodies are duplicates. Keep occurrences
    attached to their shared run and report only its source ranges.
    """
    clusters: list[DuplicateCluster] = []
    covered: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for length, occurrences in _repeated_statement_runs(all_seqs, covered):
        nonoverlapping = []
        last_end = {}
        for i, pos in occurrences:
            ranges = all_seqs[i]["line_ranges"]
            if ranges[pos + length - 1][1] - ranges[pos][0] + 1 < min_lines:
                continue
            if pos >= last_end.get(i, 0):
                nonoverlapping.append((i, pos))
                last_end[i] = pos + length
        if len(nonoverlapping) < 2:
            continue
        if all(any(start <= pos and pos + length <= end for start, end in covered[i])
               for i, pos in nonoverlapping):
            continue

        members = []
        kinds = set()
        for i, pos in nonoverlapping:
            s = all_seqs[i]
            start_1 = s["line_ranges"][pos][0] + 1
            end_1 = s["line_ranges"][pos + length - 1][1] + 1
            kinds.update(s["kinds"][pos:pos + length])
            members.append(DuplicateMember(
                file=s["file"], symbol=s["function_qn"],
                start_line=start_1, end_line=end_1, stmt_count=length,
            ))
        files = {m.file for m in members}
        if cross_file is True and len(files) < 2:
            continue
        if cross_file is False and len(files) > 1:
            continue
        score = length * 10.0 + min(len(kinds) * 3.0, 15.0)
        if len(files) > 1:
            score += 20.0

        clusters.append(DuplicateCluster(
            kind="sequence", score=score, members=members,
            explanation=f"shared contiguous statement run ({length} stmts)",
        ))
        for i, pos in nonoverlapping:
            merged = []
            for start, end in sorted([*covered[i], (pos, pos + length)]):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            covered[i] = merged
    return clusters


def _query_sequence_clusters(
    file_data: _FileData,
    scope_resolver,
    min_lines: int,
    cross_file,
) -> list[DuplicateCluster]:
    """Find contiguous sibling-sequence duplicates."""
    all_seqs: list[dict] = []
    for file_path, (content, _tree, _qn_at, _def_loc, _symbol_index) in file_data.items():
        try:
            seqs = build_statement_seqs_for_cache(file_path, content, scope_resolver)
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("build_statement_seqs_for_cache failed in %s", file_path, exc_info=True)
            continue
        for seq in seqs:
            all_seqs.append({"file": file_path, **seq})
    return _sequence_clusters_from_seqs(all_seqs, min_lines, cross_file)


# ---------------------------------------------------------------------------
# Public query / format API
# ---------------------------------------------------------------------------


def _load_cached_payloads(
    project_path: str,
    py_files: list[str],
) -> dict[str, dict] | None:
    """Try to load per-file subtree/sequence payloads from dup_cache.

    Returns ``{file_path: {"subtrees": [...], "sequences": [...]}}`` for
    files that have a cache hit, or ``None`` if the cache is unavailable.
    """
    import hashlib
    import pickle
    import sqlite3
    import zlib

    from emend.transform import _cache_db_dir

    try:
        db_path = _cache_db_dir(project_path) / "parse.db"
        if not db_path.exists():
            return None
    except OSError:
        logger.debug("Could not stat dup_cache db for %s", project_path, exc_info=True)
        return None

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        cached_rows: dict[str, bytes] = {}
        for row in conn.execute(
            "SELECT hash, data FROM dup_cache WHERE version = ?",
            (DUP_CACHE_VERSION,),
        ):
            cached_rows[row[0]] = row[1]
        conn.close()
    except sqlite3.Error:
        logger.debug("dup_cache query failed for %s", db_path, exc_info=True)
        return None

    if not cached_rows:
        return None

    result: dict[str, dict] = {}
    for file_path in py_files:
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        content_hash = hashlib.md5(
            content.encode(), usedforsecurity=False,
        ).hexdigest()
        blob = cached_rows.get(content_hash)
        if blob is not None:
            try:
                result[file_path] = pickle.loads(zlib.decompress(blob))
            except Exception:
                logger.debug("Corrupt dup_cache entry for %s", file_path, exc_info=True)
    return result if result else None


def _clusters_from_cached_subtrees(
    cached: dict[str, dict],
    min_lines: int,
    cross_file: bool | None,
) -> list[DuplicateCluster]:
    """Build exact-duplicate clusters from cached subtree payloads."""
    return _exact_clusters_from_cands(
        _subtree_cands_from_cached(cached, min_lines),
        cross_file,
    )


def _clusters_from_cached_sequences(
    cached: dict[str, dict],
    min_lines: int,
    cross_file: bool | None,
) -> list[DuplicateCluster]:
    """Build sequence-duplicate clusters from cached sequence payloads."""
    all_seqs: list[dict] = []
    for file_path, payload in cached.items():
        for seq in payload.get("sequences", []):
            all_seqs.append({"file": file_path, **seq})
    return _sequence_clusters_from_seqs(all_seqs, min_lines, cross_file)


def query_duplicates(
    project_path: str,
    mode: str = "all",
    file_scope: str | None = None,
    symbol_scope: str | None = None,
    limit: int = 50,
    min_lines: int = 3,
    min_score: float = 0.0,
    cross_file: bool | None = None,
    involves_file: str | None = None,
) -> list[DuplicateCluster]:
    """Find duplicate code clusters in a project.

    Uses cached payloads from ``emend index`` when available, falling
    back to on-the-fly parsing otherwise.

    ``involves_file`` keeps only clusters with at least one member in the
    given file. Useful for post-write hooks that want "did this edit
    introduce duplication?" — filtering happens before ``limit`` truncation.
    """
    root = Path(project_path)
    if not root.exists():
        return []

    if root.is_file():
        py_files = [str(root)]
    else:
        resolved = str(root.resolve())
        if file_scope:
            scope_path = Path(file_scope)
            if scope_path.is_file():
                py_files = [str(scope_path)]
            elif scope_path.is_dir():
                py_files = _collect_py_files(str(scope_path))
            else:
                py_files = [p for p in _collect_py_files(resolved) if file_scope in p]
        else:
            py_files = _collect_py_files(resolved)

    if not py_files:
        return []

    if symbol_scope:
        py_files = [p for p in py_files if symbol_scope in p]

    cached = _load_cached_payloads(project_path, py_files)
    if cached is not None and len(cached) == len(py_files):
        logger.debug("query_duplicates: using cached payloads for %d files", len(cached))
        clusters: list[DuplicateCluster] = []
        if mode in ("exact", "all"):
            clusters.extend(_clusters_from_cached_subtrees(cached, min_lines, cross_file))
        if mode in ("sequence", "all"):
            clusters.extend(_clusters_from_cached_sequences(cached, min_lines, cross_file))
    else:
        scope_resolver, file_data = _preparse_files(py_files, symbol_scope=None)
        if not file_data:
            return []
        clusters = []
        if mode in ("exact", "all"):
            clusters.extend(_query_exact_clusters(file_data, min_lines, cross_file))
        if mode in ("sequence", "all"):
            clusters.extend(_query_sequence_clusters(
                file_data, scope_resolver, min_lines, cross_file,
            ))

    if involves_file:
        target = str(Path(involves_file).resolve())
        clusters = [
            c for c in clusters
            if any(str(Path(m.file).resolve()) == target for m in c.members)
        ]

    if min_score > 0.0:
        clusters = [c for c in clusters if c.score >= min_score]

    clusters.sort(key=lambda c: (-c.score, -len(c.members)))
    return clusters[:limit]


def format_duplicates_text(clusters: list[DuplicateCluster]) -> str:
    """Format duplicate clusters as human-readable text."""
    if not clusters:
        return ""
    lines: list[str] = []
    for i, cluster in enumerate(clusters, 1):
        lines.append(
            f"[{i}] {cluster.kind.upper()}  score={cluster.score:.1f}  {cluster.explanation}"
        )
        for j, member in enumerate(cluster.members):
            prefix = "  primary:" if j == 0 else "  also:   "
            sym = f"  ({member.symbol})" if member.symbol else ""
            size = (
                f"  [{member.node_count} nodes]" if member.node_count
                else (f"  [{member.stmt_count} stmts]" if member.stmt_count else "")
            )
            lines.append(
                f"{prefix} {member.file}:{member.start_line}-{member.end_line}{sym}{size}"
            )
        lines.append("")
    return "\n".join(lines)


def format_duplicates_json(clusters: list[DuplicateCluster]) -> str:
    """Format duplicate clusters as JSON."""
    import json as _json
    data = [
        {
            "kind": c.kind,
            "score": c.score,
            "explanation": c.explanation,
            "members": [
                {
                    "file": m.file,
                    "symbol": m.symbol,
                    "start_line": m.start_line,
                    "end_line": m.end_line,
                    "node_count": m.node_count,
                    "stmt_count": m.stmt_count,
                }
                for m in c.members
            ],
        }
        for c in clusters
    ]
    return _json.dumps(data, indent=2)


__all__ = [
    "DuplicateMember",
    "DuplicateCluster",
    "DUP_CACHE_VERSION",
    "canonicalize_file_for_cache",
    "build_statement_seqs_for_cache",
    "query_duplicates",
    "format_duplicates_text",
    "format_duplicates_json",
]
