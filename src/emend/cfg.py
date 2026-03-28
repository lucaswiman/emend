"""Per-function control flow graph construction and querying.

Wraps the Rust ``emend_core.PyCfg`` / ``build_cfgs`` interface and provides
convenience functions for file-level and project-level CFG extraction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thin wrappers around the Rust PyCfg
# ---------------------------------------------------------------------------


def build_cfgs_for_source(source: str, ext: str = "py") -> list[Any]:
    """Build CFGs for all functions in *source*.

    Returns a list of ``emend_core.PyCfg`` objects.
    """
    from emend import emend_core  # heavy / compiled extension

    return emend_core.build_cfgs(source, ext=ext)


def build_cfgs_for_file(file_path: str, *, ext: str | None = None) -> list[Any]:
    """Build CFGs for all functions in the file at *file_path*.

    Returns a list of ``PyCfg`` objects.
    """
    path = Path(file_path)
    source = path.read_text()
    extension = ext or path.suffix.lstrip(".") or "py"
    return build_cfgs_for_source(source, ext=extension)


# ---------------------------------------------------------------------------
# Text / JSON / DOT formatters
# ---------------------------------------------------------------------------


def format_cfg_text(cfg) -> str:
    """Human-readable text representation of a single CFG."""
    lines: list[str] = []
    lines.append(
        f"function {cfg.func_name} "
        f"(lines {cfg.func_start_line}-{cfg.func_end_line}, "
        f"{cfg.block_count()} blocks, {cfg.edge_count()} edges)"
    )

    for block in cfg.get_blocks():
        bid = block["id"]
        tag = ""
        if bid == cfg.entry:
            tag = " [entry]"
        elif bid == cfg.exit:
            tag = " [exit]"

        lines.append(
            f"  B{bid}{tag}  lines {block['start_line']}-{block['end_line']}"
        )
        if block["defs"]:
            defs_str = ", ".join(d[0] for d in block["defs"])
            lines.append(f"    defs: {defs_str}")
        if block["uses"]:
            uses_str = ", ".join(u[0] for u in block["uses"])
            lines.append(f"    uses: {uses_str}")

    for edge in cfg.get_edges():
        cond = ""
        if edge.get("condition_bytes"):
            cond = " (conditional)"
        lines.append(
            f"  B{edge['from']} -> B{edge['to']}  [{edge['kind']}{cond}]"
        )

    return "\n".join(lines)


def format_cfgs_json(cfgs, file_path: str | None = None) -> str:
    """JSON representation of a list of CFGs."""
    import json

    data = []
    for cfg in cfgs:
        # Use Rust-side to_json() to avoid PyO3 round-trip for blocks/edges
        entry = json.loads(cfg.to_json())
        if file_path:
            entry["file"] = file_path
        data.append(entry)
    return json.dumps(data, indent=2)


def format_cfgs_dot(cfgs) -> str:
    """Graphviz DOT representation of a list of CFGs."""
    parts: list[str] = []
    for cfg in cfgs:
        parts.append(cfg.to_dot())
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Unreachable-block detection
# ---------------------------------------------------------------------------


def find_unreachable_blocks(cfg) -> list[dict]:
    """Return blocks that are unreachable from the entry block.

    Each result is a dict from ``get_blocks()`` for the unreachable block.
    The synthetic exit block is excluded.
    """
    # Build adjacency from edges once to avoid per-block PyO3 round-trips
    edges = cfg.get_edges()
    succ: dict[int, list[int]] = {}
    for e in edges:
        succ.setdefault(e["from"], []).append(e["to"])

    # BFS from entry
    reachable: set[int] = set()
    queue = [cfg.entry]
    while queue:
        bid = queue.pop()
        if bid in reachable:
            continue
        reachable.add(bid)
        queue.extend(succ.get(bid, []))

    unreachable = []
    for block in cfg.get_blocks():
        bid = block["id"]
        if bid not in reachable and bid != cfg.exit:
            unreachable.append(block)
    return unreachable
