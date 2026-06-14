"""Resolve pattern matches to exact CFG-backed locations.

Provides a single, reusable resolver that maps pattern match line numbers
to ``(file_path, func_qn, block_id, line)`` tuples using FactGraph facts
(``source_loc``, ``cfg_block``) as the source of truth, falling back to
on-the-fly CFG construction when facts are unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emend.fact_graph import FactGraph

logger = logging.getLogger(__name__)

# Module-level code sentinel: use a well-defined constant instead of ("", -1)
MODULE_LEVEL_FUNC = "<module>"
MODULE_LEVEL_BLOCK = 0


@dataclass(frozen=True)
class ResolvedLocation:
    """Exact CFG-backed location for a pattern match."""
    file_path: str
    func_qn: str        # MODULE_LEVEL_FUNC for module-level code
    block_id: int       # MODULE_LEVEL_BLOCK for module-level code
    line: int
    col: int = 0
    captures: dict[str, str] = field(default_factory=dict)

    @property
    def is_module_level(self) -> bool:
        return self.func_qn == MODULE_LEVEL_FUNC


# ---------------------------------------------------------------------------
# Internal index types
# ---------------------------------------------------------------------------

# (func_qn, start_line, end_line)
_FuncRange = tuple[str, int, int]
# (func_qn, block_id, start_line, end_line)
_BlockRange = tuple[str, int, int, int]


def _find_innermost_func(func_ranges: list[_FuncRange], line: int) -> str:
    """Return the qualified name of the innermost function containing *line*.

    Prefers the narrowest span when multiple functions contain the line
    (e.g. nested functions).  Returns MODULE_LEVEL_FUNC when no function
    contains *line*.
    """
    best_qn = MODULE_LEVEL_FUNC
    best_span = float("inf")
    for qn, start, end in func_ranges:
        if start <= line <= end:
            span = end - start
            if span < best_span:
                best_span = span
                best_qn = qn
    return best_qn


def _find_most_specific_block(block_ranges: list[_BlockRange], func_qn: str, line: int) -> int:
    """Return the most specific block_id whose range contains *line*.

    Among all blocks in *func_qn* that contain *line*, the one with the
    narrowest span wins.  Returns MODULE_LEVEL_BLOCK when none match.
    """
    best_bid = MODULE_LEVEL_BLOCK
    best_span = float("inf")
    for bfunc_qn, bid, start, end in block_ranges:
        if bfunc_qn != func_qn:
            continue
        if start <= line <= end:
            span = end - start
            if span < best_span or (span == best_span and bid > best_bid):
                best_span = span
                best_bid = bid
    return best_bid


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


class LocationResolver:
    """Resolves pattern match lines to exact (file, func, block) locations.

    Uses two strategies:

    1. **FactGraph-backed**: queries ``source_loc`` and ``cfg_block`` facts
       from an already-populated :class:`~emend.fact_graph.FactGraph`
       (preferred when available).
    2. **On-the-fly**: builds function and CFG block ranges from source text
       when no FactGraph is available.

    Create instances via the class-method factories:
    :meth:`from_fact_graph` and :meth:`from_source`.
    """

    def __init__(
        self,
        func_ranges: list[_FuncRange],
        block_ranges: list[_BlockRange],
    ) -> None:
        # Both lists are pre-built by the factory methods.
        self._func_ranges = func_ranges
        self._block_ranges = block_ranges

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_fact_graph(cls, graph: "FactGraph", file_path: str) -> "LocationResolver":
        """Build a resolver using pre-populated FactGraph facts.

        Queries ``symbol`` facts for function ranges and ``source_loc`` facts
        (with ``loc_kind == "block"``) for block ranges.

        Args:
            graph: Populated :class:`~emend.fact_graph.FactGraph`.
            file_path: The file whose locations should be resolvable.
        """
        _FUNC_KINDS = {"function", "async_function", "method", "async_method"}

        func_ranges: list[_FuncRange] = []
        for sym in graph.symbols(file_path=file_path):
            if sym.kind in _FUNC_KINDS and sym.line > 0 and sym.end_line >= sym.line:
                func_ranges.append((sym.qualified_name, sym.line, sym.end_line))

        # Sort narrowest-last so we can always find innermost.  (Not strictly
        # necessary since _find_innermost_func iterates all; kept for clarity.)
        func_ranges.sort(key=lambda t: t[2] - t[1])

        # Build block ranges from cfg_block + source_loc join.
        # source_loc rows with loc_kind == "block" have loc_id == "func_qn:block_id".
        block_ranges: list[_BlockRange] = []

        # Get all blocks for the file
        cfg_blocks = graph.cfg_blocks(file_path=file_path)
        if cfg_blocks:
            # Fetch source_loc facts for all blocks in one query
            block_locs = graph.source_locs(loc_kind="block")
            # Index by loc_id -> (start_line, end_line)
            loc_index: dict[str, tuple[int, int]] = {}
            for loc in block_locs:
                if loc.file_path == file_path and loc.end_line >= loc.line > 0:
                    loc_index[loc.loc_id] = (loc.line, loc.end_line)

            for blk in cfg_blocks:
                loc_id = f"{blk.func_qn}:{blk.block_id}"
                if loc_id in loc_index:
                    start, end = loc_index[loc_id]
                    block_ranges.append((blk.func_qn, blk.block_id, start, end))

        return cls(func_ranges, block_ranges)

    @classmethod
    def from_source(cls, file_path: str, source: str) -> "LocationResolver":
        """Build a resolver by analysing *source* on-the-fly.

        Uses ``emend_core.collect_symbols_from_str`` for function ranges and
        :func:`~emend.cfg.build_cfgs_for_source` for block ranges.

        Args:
            file_path: Path used as the file identifier in resolved locations.
            source: Full source text of the file.
        """
        ext = Path(file_path).suffix.lstrip(".") or "py"

        # -- Function ranges via tree-sitter symbol collector --
        func_ranges: list[_FuncRange] = []
        try:
            from emend import emend_core
            raw_syms = emend_core.collect_symbols_from_str(source, ext=ext)
            _collect_func_ranges_from_raw(raw_syms, func_ranges)
        except Exception:
            logger.debug(
                "collect_symbols_from_str failed for %s; skipping function index",
                file_path,
                exc_info=True,
            )

        # -- Block ranges via CFG builder --
        block_ranges: list[_BlockRange] = []
        try:
            from emend.cfg import build_cfgs_for_source
            cfgs = build_cfgs_for_source(source, ext=ext)
            for cfg in cfgs:
                # CFG lines from Rust are 0-based tree-sitter rows;
                # convert to 1-based to match symbol / FactGraph conventions.
                func_start_1b = cfg.func_start_line + 1
                func_qn = _find_innermost_func(func_ranges, func_start_1b)
                for block in cfg.get_blocks():
                    abs_start = block["start_line"] + 1
                    abs_end = block["end_line"] + 1
                    block_ranges.append((func_qn, block["id"], abs_start, abs_end))
        except Exception:
            logger.debug(
                "build_cfgs_for_source failed for %s; block index unavailable",
                file_path,
                exc_info=True,
            )

        return cls(func_ranges, block_ranges)

    # ------------------------------------------------------------------
    # Resolution API
    # ------------------------------------------------------------------

    def resolve(
        self,
        file_path: str,
        line: int,
        col: int = 0,
        captures: dict[str, str] | None = None,
    ) -> ResolvedLocation:
        """Resolve a single line to a :class:`ResolvedLocation`.

        Args:
            file_path: The source file path (used verbatim in the result).
            line: 1-based line number to resolve.
            col: 1-based column number (defaults to 0 = unknown).
            captures: Optional metavariable captures from a pattern match.
        """
        func_qn = _find_innermost_func(self._func_ranges, line)
        block_id = _find_most_specific_block(self._block_ranges, func_qn, line)
        return ResolvedLocation(
            file_path=file_path,
            func_qn=func_qn,
            block_id=block_id,
            line=line,
            col=col,
            captures=captures or {},
        )

    def resolve_batch(
        self,
        file_path: str,
        lines: list[int],
        col: int = 0,
    ) -> list[ResolvedLocation]:
        """Resolve a list of line numbers to :class:`ResolvedLocation` objects.

        All results share the same *file_path* and *col* (useful when batch-
        resolving matches from a single file that all have the same column, or
        where column precision is not needed).

        Args:
            file_path: Source file path.
            lines: 1-based line numbers to resolve.
            col: Column number applied to all results (defaults to 0).
        """
        return [self.resolve(file_path, ln, col) for ln in lines]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_func_ranges(
    syms: list,  # list[NestedSymbol]
    out: list[_FuncRange],
    prefix: str = "",
) -> None:
    """Recursively collect (qualified_name, start_line, end_line) for functions."""
    _FUNC_KINDS = {"function", "async_function", "method", "async_method"}
    for sym in syms:
        qn = ".".join(sym.path) if sym.path else sym.name
        if sym.kind in _FUNC_KINDS:
            out.append((qn, sym.line_start, sym.line_end))
        # Always recurse to pick up nested functions inside classes, etc.
        if sym.children:
            _collect_func_ranges(sym.children, out, qn)


def _collect_func_ranges_from_raw(
    raw_syms: list[dict],
    out: list[_FuncRange],
    prefix: str = "",
) -> None:
    """Collect function ranges from raw dicts returned by
    ``emend_core.collect_symbols_from_str()``.
    """
    _FUNC_KINDS = {"function", "async_function", "method", "async_method"}
    for d in raw_syms:
        kind = d.get("kind", "")
        if kind in ("variable", "reference"):
            continue
        path = d.get("path", [])
        name = d.get("name", "")
        qn = ".".join(path) if path else name
        if kind in _FUNC_KINDS:
            out.append((qn, d["line"], d["end_line"]))
        children = d.get("children", [])
        if children:
            _collect_func_ranges_from_raw(children, out, qn)
