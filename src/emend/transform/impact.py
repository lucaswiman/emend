"""Impact analysis: find what code is affected by changes."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from ..component_selector import ExtendedSelector

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


def _parse_diff_to_changed_files(diff_text: str) -> list[tuple[str, list[int]]]:
    """Parse unified diff output to extract changed file paths and line numbers.

    Returns a list of (file_path, changed_lines) tuples where changed_lines
    are the line numbers in the *new* version of the file that were modified.
    """
    results: list[tuple[str, list[int]]] = []
    current_file: str | None = None
    changed_lines: list[int] = []

    for line in diff_text.splitlines():
        # Detect file header: +++ b/path/to/file.py
        if line.startswith('+++ b/'):
            # Save previous file if any
            if current_file is not None:
                results.append((current_file, changed_lines))
            current_file = line[6:]  # strip '+++ b/'
            changed_lines = []
        # Detect hunk header: @@ -old_start,old_count +new_start,new_count @@
        elif line.startswith('@@') and current_file is not None:
            m = re.search(r'\+(\d+)(?:,(\d+))?', line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                # Track all lines in the hunk range as potentially changed
                changed_lines.extend(range(start, start + count))

    # Don't forget the last file
    if current_file is not None:
        results.append((current_file, changed_lines))

    return results


def _parse_diff_to_selectors(
    diff_spec: str,
    project_path: str,
) -> list[str]:
    """Run ``git diff`` and map changed lines to symbol selectors.

    Args:
        diff_spec: Git diff specification (e.g. ``"HEAD"``, ``"abc..def"``).
        project_path: Project root directory (used as cwd for git).

    Returns:
        List of selector strings for symbols touched by the diff.
    """
    import subprocess

    result = subprocess.run(
        ['git', 'diff', '-U0', diff_spec],
        capture_output=True, text=True, timeout=30,
        cwd=project_path,
    )
    if result.returncode != 0:
        raise ValueError(
            f"git diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    changed_files = _parse_diff_to_changed_files(result.stdout)
    if not changed_files:
        return []

    from emend.ast_utils import find_nested_definitions, find_symbol_by_line

    selectors: list[str] = []
    seen: set[str] = set()

    for file_rel, lines in changed_files:
        file_path = str(Path(project_path) / file_rel)
        if not Path(file_path).is_file():
            continue

        # Only process source files we can parse
        from emend.language_registry import is_source_file
        if not is_source_file(file_path):
            continue

        try:
            symbols = find_nested_definitions(file_path)
        except Exception:
            continue

        for line_no in lines:
            sym = find_symbol_by_line(symbols, line_no)
            if sym is not None:
                sel = f"{file_path}::{'.'.join(sym.path)}"
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
) -> ImpactResult | None:
    """Compute impact using a Datalog query on the persisted ``facts.db``.

    Uses the CozoDB ``facts.db`` that is populated by ``emend index``.
    The query constructs a call graph from ``fact_reference`` (kind == "call")
    joined with ``fact_symbol`` (to find the enclosing function), then
    computes the transitive reverse-caller closure via recursive Datalog.

    Returns None if facts.db is unavailable (caller falls back to BFS).
    """
    from .cache import _get_facts_db
    from emend.component_selector import parse_extended_selector
    fdb = _get_facts_db(proj_root)
    if fdb is None:
        return None

    # Resolve selectors to module-qualified names (mqn) in facts.db.
    changed_mqns: set[str] = set()
    sel_to_mqn: dict[str, str] = {}
    mqn_to_sel: dict[str, str] = {}

    for sel_str in changed_selectors:
        try:
            sel = parse_extended_selector(sel_str)
        except Exception:
            continue
        if not sel.symbol_path:
            continue
        name = sel.symbol_path[-1]
        # Try both the raw file_path and a relative version.
        for fp in (sel.file_path, _try_relative(sel.file_path, proj_root)):
            if fp is None:
                continue
            try:
                result = fdb.run(
                    "?[mqn] := *fact_symbol[fp, mqn, name, _, _, _, _, _, _, _, _, _, _, _, _, _], "
                    "fp == $fp, name == $name",
                    {"fp": fp, "name": name},
                )
                if result["rows"]:
                    mqn = result["rows"][0][0]
                    changed_mqns.add(mqn)
                    sel_to_mqn[sel_str] = mqn
                    mqn_to_sel[mqn] = sel_str
                    break
            except Exception:
                continue

    if not changed_mqns:
        return ImpactResult(
            changed_symbols=changed_selectors,
            impacted_symbols=[],
            impacted_tests=[],
            edges=[],
        )

    # Build a Datalog query against the persisted facts.db schema:
    #
    #   fact_symbol: (fp, mqn) => (name, qn, kind, line, end_line, ...)
    #   fact_reference: (tqn, fp, line, col) => (kind)
    #
    # A "call" is a fact_reference where kind == "call".  To find the
    # caller, we join with fact_symbol to find which function encloses
    # the reference line.
    seed_rows = ", ".join(f'["{mqn}"]' for mqn in changed_mqns)

    rules = [f"changed[x] <- [{seed_rows}]\n"]

    # call_edge: derive (caller_mqn, callee_mqn) from references + enclosing symbols
    rules.append(
        'call_edge[caller_mqn, callee_mqn] := '
        '*fact_reference[callee_mqn, fp, ref_line, _, kind], kind == "call", '
        '*fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
        'caller_kind in ["function", "async_function", "method", "async_method"], '
        'caller_line <= ref_line, ref_line <= caller_end\n'
    )

    # Also match references by qn (short qualified name)
    rules.append(
        'call_edge[caller_mqn, callee_mqn] := '
        '*fact_symbol[_, callee_mqn, _, callee_qn, _, _, _, _, _, _, _, _, _, _, _, _], '
        'callee_qn != "", '
        '*fact_reference[callee_qn, fp, ref_line, _, kind], kind == "call", '
        '*fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
        'caller_kind in ["function", "async_function", "method", "async_method"], '
        'caller_line <= ref_line, ref_line <= caller_end\n'
    )

    # Depth-bounded transitive reverse-caller closure
    rules.append(
        "layer_0[caller] := call_edge[caller, callee], changed[callee]\n"
    )
    for i in range(1, max_depth):
        rules.append(
            f"layer_{i}[caller] := call_edge[caller, mid], layer_{i - 1}[mid]\n"
        )
    for i in range(max_depth):
        rules.append(f"impacted[x] := layer_{i}[x]\n")

    # Edges: witness pairs
    rules.append(
        "edge[caller, callee] := impacted[caller], call_edge[caller, callee], changed[callee]\n"
    )
    if max_depth > 1:
        for i in range(1, max_depth):
            rules.append(
                f"edge[caller, mid] := layer_{i}[caller], call_edge[caller, mid], layer_{i - 1}[mid]\n"
            )

    # Return impacted symbols with file paths for selector construction
    rules.append(
        "?[caller_mqn, caller_fp, caller_name, callee_mqn] := "
        "edge[caller_mqn, callee_mqn], not changed[caller_mqn], "
        "*fact_symbol[caller_fp, caller_mqn, caller_name, _, _, _, _, _, _, _, _, _, _, _, _, _]"
    )

    try:
        result = fdb.run("".join(rules))
    except Exception:
        logger.debug("facts.db impact query failed", exc_info=True)
        return None

    # Build the result
    impacted: list[str] = []
    all_edges: list[ImpactEdge] = []
    seen_impacted: set[str] = set()

    abs_root = str(Path(proj_root).resolve())
    for row in result["rows"]:
        caller_mqn, caller_fp, caller_name, callee_mqn = row[0], row[1], row[2], row[3]

        # Convert relative path back to absolute for selectors.
        if not Path(caller_fp).is_absolute():
            caller_fp = str(Path(abs_root) / caller_fp)

        # Build selector for the caller
        if caller_mqn not in mqn_to_sel:
            mqn_to_sel[caller_mqn] = f"{caller_fp}::{caller_name}"
        caller_sel = mqn_to_sel[caller_mqn]
        callee_sel = mqn_to_sel.get(callee_mqn, callee_mqn)

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
    try:
        deco_result = fdb.run(
            '?[sqn] := *decorator_on[sqn, dec], '
            'dec in ["test", "tokio::test"]'
        )
        for row in deco_result["rows"]:
            mqn = row[0]
            if mqn in mqn_to_sel:
                test_decorated_sels.add(mqn_to_sel[mqn])
    except Exception:
        pass

    for sel_str in all_impacted:
        file_part = sel_str.split('::', 1)[0] if '::' in sel_str else sel_str
        if _is_test_file(file_part) or _is_test_symbol(sel_str) or sel_str in test_decorated_sels:
            if sel_str not in impacted_tests:
                impacted_tests.append(sel_str)
                for edge in all_edges:
                    if edge.target == sel_str:
                        all_edges.append(ImpactEdge(
                            source=edge.source,
                            target=sel_str,
                            kind="test",
                        ))
                        break

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
    from .index import warm_caches
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

    if selectors:
        for sel in selectors:
            if sel.symbol_path:
                changed_selectors.append(
                    f"{sel.file_path}::{'.'.join(sel.symbol_path)}"
                )

    if diff_spec:
        diff_sels = _parse_diff_to_selectors(diff_spec, proj_root)
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
        changed_selectors, proj_root, max_depth=max_depth,
    )
    if dl_result is not None:
        return dl_result

    # facts.db unavailable — warm the index and retry once.
    try:
        warm_caches(proj_root, type_engine="none")
    except Exception:
        pass
    dl_result = _find_impact_via_fact_graph(
        changed_selectors, proj_root, max_depth=max_depth,
    )
    if dl_result is not None:
        return dl_result

    return ImpactResult(
        changed_symbols=changed_selectors,
        impacted_symbols=[],
        impacted_tests=[],
        edges=[],
    )


# ---------------------------------------------------------------------------
# Semantic context — situational awareness for code agents
# ---------------------------------------------------------------------------

# Default decorators that indicate a symbol is an external interface
_EXTERNAL_INTERFACE_DECORATORS = frozenset({
    'app.route', 'app.get', 'app.post', 'app.put', 'app.delete', 'app.patch',
    'router.get', 'router.post', 'router.put', 'router.delete', 'router.patch',
    'api_view', 'action',
    'rpc_endpoint', 'grpc_method',
    'click.command', 'click.group',
    'app.command',
    'strawberry.mutation', 'strawberry.query', 'strawberry.subscription',
    'graphene.resolve',
    'task', 'celery.task', 'shared_task',
    'webhook', 'endpoint',
    'message_handler', 'event_handler',
})

_EXTERNAL_INTERFACE_BASENAMES = frozenset({
    'route', 'get', 'post', 'put', 'delete', 'patch', 'head', 'options',
    'command', 'task', 'endpoint', 'webhook',
    'mutation', 'query', 'subscription',
    'rpc', 'grpc', 'api',
})

# Patterns in callees that indicate async side effects
_ASYNC_SIDE_EFFECT_PATTERNS = frozenset({
    'delay', 'apply_async', 'send_task',
    'submit', 'create_task', 'ensure_future',
    'run_in_executor',
})

# Patterns in callees that indicate I/O or external effects
_SIDE_EFFECT_CALLEE_PATTERNS = {
    'db_write': {'save', 'commit', 'add', 'delete', 'update', 'insert',
                 'execute', 'executemany', 'bulk_create', 'bulk_update'},
    'network': {'request', 'get', 'post', 'put', 'fetch', 'urlopen', 'send'},
    'file_io': {'write', 'open', 'unlink', 'remove', 'rename', 'mkdir'},
    'cache': {'set', 'delete', 'clear', 'invalidate'},
}

# Caching decorators that may need invalidation on mutations
_CACHE_DECORATORS = frozenset({
    'cache', 'lru_cache', 'cached_property', 'cache_page',
    'cache_control', 'memoize', 'cacheable',
})

# Regex for detecting a name inside string literals (matches dead code approach)
_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


