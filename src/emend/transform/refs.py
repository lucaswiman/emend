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

from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)

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
    """Compatibility delegate to the project-scoped analysis owner."""
    from emend.analysis_store import AnalysisStore

    return AnalysisStore.open(project_path).query_facts()


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

    # Keep identities until presentation so same-named methods cannot merge.
    edges: dict[str, list[str]] = {}
    for caller_qn, callee_qn in edge_pairs:
        edges.setdefault(caller_qn, [])
        if callee_qn not in edges[caller_qn]:
            edges[caller_qn].append(callee_qn)

    # Also include functions and classes with no calls
    syms = graph.symbols(file_path=rel_path)
    for s in syms:
        if s.kind in ("function", "async_function", "method", "async_method", "class"):
            edges.setdefault(s.qualified_name, [])

    from collections import Counter

    names = {qn: qn.rsplit('.', 1)[-1]
             for qn in set(edges) | {callee for callees in edges.values() for callee in callees}}
    counts = Counter(names.values())
    labels = {qn: name if counts[name] == 1 else qn for qn, name in names.items()}
    edges = {labels[qn]: [labels[callee] for callee in callees]
             for qn, callees in edges.items()}

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
