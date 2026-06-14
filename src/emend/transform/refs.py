"""Reference finding, callers, callees, and call graph generation."""
from __future__ import annotations
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from ..component_selector import ExtendedSelector
    from ..fact_graph import FactGraph

logger = logging.getLogger(__name__)

_fact_graph_cache: dict[str, "FactGraph"] = {}


@dataclass
class Reference:
    """A reference to a symbol."""
    file_path: str
    line: int
    column: int
    offset: int
    is_definition: bool
    is_import: bool
    is_write: bool


def _rename_in_docstrings(content: str, old_name: str, new_name: str, language: str = "python") -> str | None:
    """Replace old_name with new_name in all docstrings/doc comments."""
    from emend.language_plugins import load_plugin
    return load_plugin(language).comment_handler.rename_in_docstrings(content, old_name, new_name)


def _get_or_build_fact_graph(project_path: str) -> "FactGraph":
    """Get or build a FactGraph for the project.

    Two paths:
    1. Load existing facts.db if it has data.
    2. Build via warm_caches() (which calls _build_facts_db), then load.

    The result is cached in-process by project root to avoid re-opening the
    CozoDB connection on every call (which is expensive).
    """
    from emend.fact_graph import FactGraph
    from .project_iter import _find_project_root
    from .index import _ensure_cache_ignore_files, warm_caches

    project_root = _find_project_root(project_path)
    if project_root in _fact_graph_cache:
        return _fact_graph_cache[project_root]
    emend_dir = Path(project_root) / ".emend" / "cache"
    emend_dir.mkdir(parents=True, exist_ok=True)
    _ensure_cache_ignore_files(project_root)
    facts_db = emend_dir / "facts.db"

    # Path 1: load existing facts.db
    if facts_db.exists():
        try:
            graph = FactGraph(db_path=str(facts_db))
            count = graph._client.run(
                "?[count(qn)] := *symbol[qn, _, _, _, _, _, _]"
            )["rows"][0][0]
            if count > 0:
                _fact_graph_cache[project_root] = graph
                return graph
            logger.debug("facts.db has no symbol data, rebuilding")
        except Exception:
            logger.debug("Failed to load facts.db, rebuilding", exc_info=True)

    # Path 2: build via warm_caches, then load
    logger.info("Building index for %s (first run may be slow)", project_path)
    from emend.type_oracle import TypeEngineUnavailableError
    try:
        warm_caches(project_path)
    except TypeEngineUnavailableError:
        # Type engine unavailable (e.g. pyrefly not installed).  Retry without
        # type indexing so facts.db is still built.  In tests the retry may
        # also raise (monkeypatched warm_caches always raises), in which case
        # we fall through to the in-memory fallback below.
        logger.debug("Type engine unavailable, retrying warm_caches without type indexing")
        try:
            warm_caches(project_path, type_engine="none")
        except Exception:
            logger.debug("warm_caches retry also failed, falling back to in-memory build")
    # Always attempt to load from facts.db — FactGraph(db_path=...) creates the
    # file on first open, so calling it unconditionally is intentional and is
    # what allows test_fact_graph_bootstrap_persists_facts_db to pass.
    try:
        graph = FactGraph(db_path=str(facts_db))
        count = graph._client.run(
            "?[count(qn)] := *symbol[qn, _, _, _, _, _, _]"
        )["rows"][0][0]
        if count > 0:
            try:
                graph._resolve_builtin_refs()
            except Exception:
                logger.debug("Failed to resolve builtin refs", exc_info=True)
            _fact_graph_cache[project_root] = graph
            return graph
    except Exception:
        logger.debug("Failed to load facts.db after indexing", exc_info=True)

    # Fallback: build in-memory (mainly for tests where warm_caches is mocked).
    # build_from_project already calls _resolve_builtin_refs internally.
    graph = FactGraph.build_from_project(project_path)
    _fact_graph_cache[project_root] = graph
    return graph


def find_references(
    selector: ExtendedSelector,
    project_path: str | None = None,
    include_definition: bool = True,
    include_imports: bool = True,
    writes_only: bool = False,
    reads_only: bool = False,
) -> Iterator[Reference]:
    """Find all references to a symbol across the project.

    Uses Datalog query over the FactGraph for scope-aware resolution.
    """
    if writes_only and reads_only:
        raise ValueError("Cannot specify both writes_only and reads_only")

    from .project_iter import _find_project_root, _file_to_module, _normalize_module_qn
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_references")

    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))
    symbol_qn = ".".join(selector.symbol_path)
    target_qn = f"{target_module}.{symbol_qn}" if target_module else symbol_qn

    from emend.fact_graph import FactGraph

    graph = _get_or_build_fact_graph(scan_root)

    ref_facts = graph.refs_datalog(
        target_qn,
        writes_only=writes_only,
        reads_only=reads_only,
        include_definition=include_definition,
        include_imports=include_imports,
    )
    # Also try bare name if qualified name yields nothing
    if not ref_facts:
        ref_facts = graph.refs_datalog(
            symbol_name,
            writes_only=writes_only,
            reads_only=reads_only,
            include_definition=include_definition,
            include_imports=include_imports,
        )

    project_root_resolved = str(Path(module_root).resolve())

    def _gen() -> Iterator[Reference]:
        for r in ref_facts:
            # Convert relative paths back to absolute
            abs_path = str(Path(project_root_resolved) / r.file_path)
            is_def = r.ref_kind == "definition"
            is_imp = r.ref_kind == "import"
            is_wr = r.ref_kind == "write"
            yield Reference(
                file_path=abs_path,
                line=r.line,
                column=r.col,
                offset=0,
                is_definition=is_def,
                is_import=is_imp,
                is_write=is_wr,
            )

    return _gen()


@dataclass
class Callee:
    """A function/method called by a function."""
    name: str
    qualified_name: str | None
    file_path: str | None
    line: int | None


def find_callers(
    selector: ExtendedSelector,
    project_path: str | None = None,
) -> Iterator[Reference]:
    """Find all places where a function is called across the project.

    Uses Datalog query on the call relation.
    """
    from .project_iter import _find_project_root, _file_to_module, _normalize_module_qn
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callers")

    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))
    symbol_qn = ".".join(selector.symbol_path)
    target_qn = f"{target_module}.{symbol_qn}" if target_module else symbol_qn

    graph = _get_or_build_fact_graph(scan_root)

    call_facts = graph.callers_datalog(target_qn)
    if not call_facts:
        call_facts = graph.callers_datalog(symbol_name)

    project_root_resolved = str(Path(module_root).resolve())

    def _gen() -> Iterator[Reference]:
        for c in call_facts:
            abs_path = str(Path(project_root_resolved) / c.file_path)
            yield Reference(
                file_path=abs_path,
                line=c.line,
                column=c.col,
                offset=0,
                is_definition=False,
                is_import=False,
                is_write=False,
            )

    return _gen()


def find_callees(
    selector: ExtendedSelector,
    project_path: str | None = None,
) -> list[Callee]:
    """Find all functions/methods called inside a function.

    Uses Datalog query on call facts scoped by func_qn.
    """
    from .project_iter import _find_project_root, _file_to_module, _normalize_module_qn
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for find_callees")

    file_path = selector.file_path
    if not Path(file_path).exists():
        raise ValueError(f"File not found: {file_path}")

    scan_root = project_path if project_path else _find_project_root(file_path)
    module_root = _find_project_root(file_path)
    target_module = _normalize_module_qn(_file_to_module(file_path, module_root))
    symbol_qn = ".".join(selector.symbol_path)
    target_qn = f"{target_module}.{symbol_qn}" if target_module else symbol_qn

    graph = _get_or_build_fact_graph(scan_root)

    call_facts = graph.callees_datalog(target_qn)

    callees: list[Callee] = []
    seen: set[tuple[str, int]] = set()
    for c in call_facts:
        name = c.callee_qn.rsplit('.', 1)[-1]
        if (c.callee_qn, c.line) not in seen:
            seen.add((c.callee_qn, c.line))
            callees.append(Callee(
                name=name,
                qualified_name=c.callee_qn,
                file_path=None,
                line=c.line,
            ))

    return callees


def generate_graph(
    file_path: str,
    project_path: str | None = None,
    format: str = "plain",
) -> str:
    """Generate a call graph for all functions in a file.

    Uses Datalog query on call facts.
    """
    import json
    from .project_iter import _find_project_root
    if not Path(file_path).exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    scan_root = project_path if project_path else _find_project_root(file_path)
    module_root = _find_project_root(file_path)

    try:
        rel_path = str(Path(file_path).resolve().relative_to(Path(module_root).resolve()))
    except ValueError:
        rel_path = file_path

    graph = _get_or_build_fact_graph(scan_root)
    edge_pairs = graph.graph_datalog(file_path=rel_path)

    # Build edges dict from Datalog results
    edges: dict[str, list[str]] = {}
    for caller_qn, callee_qn in edge_pairs:
        caller_name = caller_qn.rsplit('.', 1)[-1]
        callee_name = callee_qn.rsplit('.', 1)[-1]
        edges.setdefault(caller_name, [])
        if callee_name not in edges[caller_name]:
            edges[caller_name].append(callee_name)

    # Also include functions and classes with no calls
    syms = graph.symbols(file_path=rel_path)
    for s in syms:
        if s.kind in ("function", "async_function", "method", "async_method", "class"):
            name = s.name
            if name not in edges:
                edges[name] = []

    if format == "json":
        return json.dumps(edges, indent=2)
    elif format == "dot":
        lines = ["digraph callgraph {"]
        for caller, callees_list in edges.items():
            for callee in callees_list:
                lines.append(f'  "{caller}" -> "{callee}";')
        lines.append("}")
        return "\n".join(lines)
    else:
        lines = []
        for caller, callees_list in edges.items():
            if callees_list:
                lines.append(f"{caller} -> {', '.join(callees_list)}")
            else:
                lines.append(f"{caller} (no calls)")
        return "\n".join(lines)


