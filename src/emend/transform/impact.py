"""Impact analysis: find what code is affected by changes."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import logging
import subprocess

if TYPE_CHECKING:
    from ..component_selector import ExtendedSelector

from emend.errors import BUG_EXCEPTIONS
from emend.git_diff import LineIntervals, read_diff, repository_root

logger = logging.getLogger(__name__)

@dataclass
class ImpactEdge:
    """A witness edge showing why a symbol is impacted."""
    source: str  # selector of the causing symbol
    target: str  # selector of the impacted symbol
    kind: str  # "calls", "references", "test"


@dataclass
class ImpactResult:
    """Result of impact analysis."""
    changed_symbols: list[str]  # selectors of directly changed symbols
    impacted_symbols: list[str]  # selectors of transitively impacted symbols
    impacted_tests: list[str]  # test file paths or test selectors
    edges: list[ImpactEdge]  # witness edges


IMPACT_OUTPUTS = frozenset(("symbols", "tests", "graph"))


def impact_projection(
    result: ImpactResult,
    output: str = "symbols",
    *,
    dsl_impacts: list[tuple[str, str, str]] | None = None,
) -> dict:
    """Return the public JSON projection for an impact result."""
    if output not in IMPACT_OUTPUTS:
        choices = ", ".join(sorted(IMPACT_OUTPUTS))
        raise ValueError(f"Unknown impact output mode {output!r}; use: {choices}")
    if output == "tests":
        return {"impacted_tests": result.impacted_tests}
    edges = [
        {"source": edge.source, "target": edge.target, "kind": edge.kind}
        for edge in result.edges
    ]
    if output == "graph":
        return {"edges": edges}
    data: dict = {
        "changed_symbols": result.changed_symbols,
        "impacted_symbols": result.impacted_symbols,
        "impacted_tests": result.impacted_tests,
        "edges": edges,
    }
    if dsl_impacts:
        data["dsl_impacts"] = [
            {"file": file, "line": line, "reason": reason}
            for file, line, reason in dsl_impacts
        ]
    return data


def _symbols_in_intervals(symbols, lines: LineIntervals):
    """Yield innermost owners in first-changed-line order, without visiting lines.

    The last matching symbol in depth-first traversal owns a line, matching
    find_symbol_by_line even when sibling spans overlap.
    """
    from heapq import heappush, heappop
    from itertools import groupby

    events = []

    def visit(symbols, lower, upper):
        for sym in symbols:
            start, stop = max(lower, sym.line_start), min(upper, sym.line_end + 1)
            if start < stop:
                priority = len(events)
                events.append((start, priority, stop, sym))
                events.append((stop, -1, stop, None))
                visit(sym.children, start, stop)

    visit(symbols, 1, float("inf"))
    active = []
    seen = set()
    previous = None
    for boundary, group in groupby(sorted(events, key=lambda event: event[0]), key=lambda event: event[0]):
        if active and lines.intersects(previous, boundary - 1):
            priority, _, sym = active[0]
            if priority not in seen:
                seen.add(priority)
                yield sym
        for _, priority, stop, sym in group:
            if sym is not None:
                heappush(active, (-priority, stop, sym))
        while active and active[0][1] <= boundary:
            heappop(active)
        previous = boundary


def _parse_diff_to_selectors(
    diff_spec: str,
    project_path: str,
    *,
    identities: dict[str, str] | None = None,
) -> list[str]:
    """Run ``git diff`` and map changed lines to symbol selectors.

    Args:
        diff_spec: Git diff specification (e.g. ``"HEAD"``, ``"abc..def"``).
        project_path: Project root directory (used as cwd for git).

    Returns:
        List of selector strings for symbols touched by the diff.
    """
    root = repository_root(project_path)
    changes = read_diff(root, diff_spec)

    from emend.ast_utils import find_nested_definitions
    from emend.language_registry import is_source_file
    from emend.project_config import module_name_for_file
    from emend.analysis_linking import _normalize_qn

    selectors: list[str] = []
    seen: set[str] = set()

    for changed in changes:
        for side, file_rel in enumerate(changed.paths):
            if file_rel is None or not changed.lines[side] or not is_source_file(file_rel):
                continue
            file_path = str(root / file_rel)
            blob = subprocess.run(
                ['git', 'cat-file', 'blob', changed.blobs[side]],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            if blob.returncode and side == 0:
                raise ValueError(f"Cannot read pre-change source: {file_rel}")
            # Git does not store worktree blobs. Committed range endpoints,
            # however, must be parsed from their blobs, not today's worktree.
            source = Path(file_path).read_text() if blob.returncode else blob.stdout
            symbols = find_nested_definitions(file_path, source_override=source)
            for sym in _symbols_in_intervals(symbols, changed.lines[side]):
                local_name = '.'.join(sym.path)
                sel = f"{file_path}::{local_name}"
                if identities is not None:
                    module = _normalize_qn(module_name_for_file(file_path, project_path))
                    identities[sel] = '.'.join(filter(None, (module, local_name)))
                if sel not in seen:
                    seen.add(sel)
                    selectors.append(sel)

    return selectors


def _is_test_file(file_path: str) -> bool:
    """Check if a file is a test file by path heuristics."""
    p = Path(file_path)
    name = p.name
    if name.startswith('test_') or p.stem.endswith('_test'):
        return True
    stem = p.stem
    if stem.endswith('.test') or stem.endswith('.spec'):
        return True
    parts = p.parts
    if 'tests' in parts or 'test' in parts or '__tests__' in parts:
        return True
    return False


def _is_test_symbol(selector: str) -> bool:
    """Check if a selector refers to a test symbol."""
    if '::' in selector:
        sym_part = selector.split('::', 1)[1]
        # e.g. test_foo or TestFoo or TestFoo.test_method
        first_name = sym_part.split('.')[0]
        if first_name.startswith('test_') or first_name.startswith('Test'):
            return True
        # TypeScript/JavaScript test framework conventions
        if first_name in ('describe', 'it', 'test'):
            return True
    return False


def _find_impact_via_fact_graph(
    changed_selectors: list[str],
    proj_root: str,
    max_depth: int = 10,
    *,
    identities: dict[str, str] | None = None,
) -> ImpactResult | None:
    """Compute impact using the owner's current project fact generation."""
    from emend.analysis_store import AnalysisStore
    from emend.component_selector import parse_extended_selector
    graph = AnalysisStore.open(proj_root).query_facts()
    fdb = graph.client

    # Resolve selectors to module-qualified names (mqn) in facts.db.
    changed_mqns: set[tuple[str, str]] = set()
    mqn_to_sel: dict[tuple[str, str], str] = {}

    for sel_str in changed_selectors:
        try:
            sel = parse_extended_selector(sel_str)
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug("unparseable selector %r, skipping", sel_str, exc_info=True)
            continue
        if not sel.symbol_path:
            continue
        name = sel.symbol_path[-1]
        qn = ".".join(sel.symbol_path)
        # Try both the raw file_path and a relative version.
        for fp in (sel.file_path, _try_relative(sel.file_path, proj_root)):
            if fp is None:
                continue
            try:
                result = fdb.run(
                    "?[mqn] := *search_symbol[fp, mqn, name, local_qn, "
                    "_, _, _, _, _, _, _, _], fp == $fp, name == $name, "
                    "local_qn == $qn",
                    {"fp": fp, "name": name, "qn": qn},
                )
            except Exception:
                logger.debug(
                    "symbol lookup failed for %s", fp, exc_info=True,
                )
                continue
            if result["rows"]:
                mqn = result["rows"][0][0]
                identity = (graph.namespace_for_file(fp), mqn)
                changed_mqns.add(identity)
                mqn_to_sel[identity] = sel_str
                break

    resolved = set(mqn_to_sel.values())
    for sel_str, mqn in (identities or {}).items():
        if sel_str not in resolved:
            identity = (graph.namespace_for_file(parse_extended_selector(sel_str).file_path), mqn)
            changed_mqns.add(identity)
            mqn_to_sel[identity] = sel_str

    if not changed_mqns:
        return ImpactResult(
            changed_symbols=changed_selectors,
            impacted_symbols=[],
            impacted_tests=[],
            edges=[],
        )

    # Build a depth-bounded reverse closure over the canonical call relation.
    rules = ["changed[ns, x] <- $changed\n"]

    rules.append(
        'call_edge[ns, caller_mqn, callee_mqn] := '
        '*call[caller_mqn, callee_mqn, fp, _, _, _, _], *file_namespace[fp, ns]\n'
    )

    # Depth-bounded transitive reverse-caller closure
    rules.append(
        "layer_0[ns, caller] := call_edge[ns, caller, callee], changed[ns, callee]\n"
    )
    for i in range(1, max_depth):
        rules.append(
            f"layer_{i}[ns, caller] := call_edge[ns, caller, mid], layer_{i - 1}[ns, mid]\n"
        )
    for i in range(max_depth):
        rules.append(f"impacted[ns, x] := layer_{i}[ns, x]\n")

    # Edges: witness pairs
    rules.append(
        "edge[ns, caller, callee] := impacted[ns, caller], call_edge[ns, caller, callee], changed[ns, callee]\n"
    )
    if max_depth > 1:
        for i in range(1, max_depth):
            rules.append(
                f"edge[ns, caller, mid] := layer_{i}[ns, caller], call_edge[ns, caller, mid], layer_{i - 1}[ns, mid]\n"
            )

    # Return impacted symbols with file paths for selector construction
    rules.append(
        "?[ns, caller_mqn, caller_fp, caller_name, callee_mqn] := "
        "edge[ns, caller_mqn, callee_mqn], not changed[ns, caller_mqn], "
        "*search_symbol[caller_fp, caller_mqn, _, caller_name, _, _, _, _, _, _, _, _], "
        "*file_namespace[caller_fp, ns]"
    )

    try:
        result = fdb.run("".join(rules), {"changed": [list(key) for key in sorted(changed_mqns)]})
    except Exception:
        logger.debug("facts.db impact query failed", exc_info=True)
        return None

    # Build the result
    impacted: list[str] = []
    all_edges: list[ImpactEdge] = []
    seen_impacted: set[str] = set()

    abs_root = str(Path(proj_root).resolve())
    # Complete the projection before rendering any edge: row ordering is not
    # graph traversal ordering, and both endpoints must use selector identities.
    for ns, mqn, fp, local_name, _ in result["rows"]:
        mqn_to_sel.setdefault((ns, mqn), f"{Path(abs_root) / fp}::{local_name}")
    for ns, caller_mqn, caller_fp, caller_name, callee_mqn in result["rows"]:
        caller_sel = mqn_to_sel[ns, caller_mqn]
        callee_sel = mqn_to_sel.get((ns, callee_mqn), callee_mqn)

        all_edges.append(ImpactEdge(
            source=callee_sel,
            target=caller_sel,
            kind="calls",
        ))

        if caller_sel not in seen_impacted and caller_sel not in changed_selectors:
            seen_impacted.add(caller_sel)
            impacted.append(caller_sel)

    # Identify impacted tests
    impacted_tests: list[str] = []
    all_impacted = changed_selectors + impacted

    # Build set of decorator-based test symbols from fact graph (e.g. Rust #[test])
    test_decorated_sels: set[str] = set()
    deco_rows: list = []
    try:
        deco_rows = fdb.run(
            '?[ns, sqn] := *decorator_on[sqn, dec, fp], '
            '*file_namespace[fp, ns], dec in ["test", "tokio::test"]'
        )["rows"]
    except Exception:
        logger.debug("decorator_on query failed", exc_info=True)
    for row in deco_rows:
        identity = tuple(row)
        if identity in mqn_to_sel:
            test_decorated_sels.add(mqn_to_sel[identity])

    test_edges: list[ImpactEdge] = []
    for sel_str in all_impacted:
        file_part = sel_str.split('::', 1)[0] if '::' in sel_str else sel_str
        if _is_test_file(file_part) or _is_test_symbol(sel_str) or sel_str in test_decorated_sels:
            if sel_str not in impacted_tests:
                impacted_tests.append(sel_str)
                for edge in all_edges:
                    if edge.target == sel_str:
                        test_edges.append(ImpactEdge(
                            source=edge.source,
                            target=sel_str,
                            kind="test",
                        ))
                        break
    all_edges.extend(test_edges)

    return ImpactResult(
        changed_symbols=changed_selectors,
        impacted_symbols=impacted,
        impacted_tests=impacted_tests,
        edges=all_edges,
    )


def _try_relative(path: str, root: str) -> str | None:
    """Try to make *path* relative to *root*; return None on failure."""
    try:
        return str(Path(path).relative_to(Path(root).resolve()))
    except ValueError:
        return None


def find_impact(
    selectors: list[ExtendedSelector] | None = None,
    diff_spec: str | None = None,
    project_path: str | None = None,
    max_depth: int = 10,
) -> ImpactResult:
    """Compute the transitive set of impacted symbols from changed symbols or a diff.

    Either *selectors* or *diff_spec* must be provided.

    Args:
        selectors: Directly specified changed symbols.
        diff_spec: Git diff specification (e.g. ``"HEAD"``, ``"abc..def"``).
            Parsed to extract changed symbols automatically.
        project_path: Project root (auto-detected if None).
        max_depth: Maximum depth for transitive closure (default 10).

    Returns:
        ImpactResult with changed symbols, impacted symbols, tests, and edges.

    Raises:
        ValueError: If neither selectors nor diff_spec is provided, or on git errors.
    """
    from .project_iter import _find_project_root
    if not selectors and not diff_spec:
        raise ValueError("Either selectors or diff_spec must be provided")

    # Resolve project root
    if project_path:
        proj_root = project_path
    elif selectors:
        proj_root = _find_project_root(selectors[0].file_path)
    else:
        proj_root = _find_project_root('.')

    # Step 1: Determine changed symbols
    changed_selectors: list[str] = []
    identities: dict[str, str] = {}

    if selectors:
        for sel in selectors:
            if sel.symbol_path:
                changed_selectors.append(
                    f"{sel.file_path}::{'.'.join(sel.symbol_path)}"
                )

    if diff_spec:
        diff_sels = _parse_diff_to_selectors(diff_spec, proj_root, identities=identities)
        changed_selectors.extend(diff_sels)

    if not changed_selectors:
        return ImpactResult(
            changed_symbols=[],
            impacted_symbols=[],
            impacted_tests=[],
            edges=[],
        )

    # Datalog query on the persisted facts.db.
    dl_result = _find_impact_via_fact_graph(
        changed_selectors, proj_root, max_depth=max_depth, identities=identities,
    )
    if dl_result is not None:
        return dl_result

    return ImpactResult(
        changed_symbols=changed_selectors,
        impacted_symbols=[],
        impacted_tests=[],
        edges=[],
    )
