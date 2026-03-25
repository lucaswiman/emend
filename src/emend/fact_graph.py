"""Stable relational fact model for code invariants.

Provides a unified, queryable graph of code facts (symbols, calls,
references, taint flows, types, imports) extracted from a project's
source tree using emend's existing analysis infrastructure.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fact types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SymbolFact:
    """A symbol definition in the project."""
    file_path: str
    name: str
    qualified_name: str
    kind: str  # e.g. "class", "function", "method", "async_function"
    line: int
    end_line: int
    parent: str | None = None  # qualified name of containing symbol


@dataclass(frozen=True)
class CallFact:
    """A call relationship between two symbols."""
    caller_qn: str
    callee_qn: str
    file_path: str
    line: int
    col: int


@dataclass(frozen=True)
class ReferenceFact:
    """A reference to a symbol at a specific location."""
    symbol_qn: str
    file_path: str
    line: int
    col: int
    ref_kind: Literal["read", "write", "call", "import"]


@dataclass(frozen=True)
class TaintFlowFact:
    """A taint flow edge from source to sink within a function."""
    source_var: str
    sink_var: str
    label: str
    file_path: str
    func_qn: str
    source_line: int
    sink_line: int


@dataclass(frozen=True)
class TypeFact:
    """A type binding for a symbol."""
    symbol_qn: str
    type_str: str
    file_path: str
    line: int
    binding_kind: str  # e.g. "annotation", "inferred", "return"


@dataclass(frozen=True)
class ImportFact:
    """An import relationship in a file."""
    importing_file: str
    imported_module: str
    imported_name: str | None
    alias: str | None
    line: int


# Union of all fact types for generic queries.
Fact = Union[SymbolFact, CallFact, ReferenceFact, TaintFlowFact, TypeFact, ImportFact]


# ---------------------------------------------------------------------------
# FactGraph
# ---------------------------------------------------------------------------

class FactGraph:
    """Queryable store of code facts extracted from a project.

    Facts are stored in typed lists with secondary indexes for fast
    lookup by the most common query keys.
    """

    def __init__(self) -> None:
        # Primary storage
        self._symbols: list[SymbolFact] = []
        self._calls: list[CallFact] = []
        self._references: list[ReferenceFact] = []
        self._taint_flows: list[TaintFlowFact] = []
        self._types: list[TypeFact] = []
        self._imports: list[ImportFact] = []

        # Secondary indexes (lazily rebuilt)
        self._idx_symbols_by_name: dict[str, list[SymbolFact]] = defaultdict(list)
        self._idx_symbols_by_kind: dict[str, list[SymbolFact]] = defaultdict(list)
        self._idx_symbols_by_file: dict[str, list[SymbolFact]] = defaultdict(list)
        self._idx_symbols_by_qn: dict[str, SymbolFact] = {}
        self._idx_calls_from: dict[str, list[CallFact]] = defaultdict(list)
        self._idx_calls_to: dict[str, list[CallFact]] = defaultdict(list)
        self._idx_refs_to: dict[str, list[ReferenceFact]] = defaultdict(list)
        self._idx_taint_by_label: dict[str, list[TaintFlowFact]] = defaultdict(list)
        self._idx_taint_by_file: dict[str, list[TaintFlowFact]] = defaultdict(list)
        self._idx_types_by_qn: dict[str, list[TypeFact]] = defaultdict(list)
        self._idx_imports_by_file: dict[str, list[ImportFact]] = defaultdict(list)

    # -- Mutation ---------------------------------------------------------

    def add_symbol(self, fact: SymbolFact) -> None:
        """Add a symbol definition fact."""
        self._symbols.append(fact)
        self._idx_symbols_by_name[fact.name].append(fact)
        self._idx_symbols_by_kind[fact.kind].append(fact)
        self._idx_symbols_by_file[fact.file_path].append(fact)
        self._idx_symbols_by_qn[fact.qualified_name] = fact

    def add_call(self, fact: CallFact) -> None:
        """Add a call relationship fact."""
        self._calls.append(fact)
        self._idx_calls_from[fact.caller_qn].append(fact)
        self._idx_calls_to[fact.callee_qn].append(fact)

    def add_reference(self, fact: ReferenceFact) -> None:
        """Add a reference fact."""
        self._references.append(fact)
        self._idx_refs_to[fact.symbol_qn].append(fact)

    def add_taint_flow(self, fact: TaintFlowFact) -> None:
        """Add a taint flow fact."""
        self._taint_flows.append(fact)
        self._idx_taint_by_label[fact.label].append(fact)
        self._idx_taint_by_file[fact.file_path].append(fact)

    def add_type(self, fact: TypeFact) -> None:
        """Add a type binding fact."""
        self._types.append(fact)
        self._idx_types_by_qn[fact.symbol_qn].append(fact)

    def add_import(self, fact: ImportFact) -> None:
        """Add an import fact."""
        self._imports.append(fact)
        self._idx_imports_by_file[fact.importing_file].append(fact)

    # -- Queries ----------------------------------------------------------

    def symbols(
        self,
        name: str | None = None,
        kind: str | None = None,
        file_path: str | None = None,
    ) -> list[SymbolFact]:
        """Query symbol facts with optional filters.

        When a single filter is specified, the index is used for O(1) lookup.
        Multiple filters intersect the results.
        """
        if name is not None and kind is None and file_path is None:
            return list(self._idx_symbols_by_name.get(name, []))
        if kind is not None and name is None and file_path is None:
            return list(self._idx_symbols_by_kind.get(kind, []))
        if file_path is not None and name is None and kind is None:
            return list(self._idx_symbols_by_file.get(file_path, []))

        # Multi-filter: start from the narrowest index, then filter.
        if file_path is not None:
            candidates = self._idx_symbols_by_file.get(file_path, [])
        elif name is not None:
            candidates = self._idx_symbols_by_name.get(name, [])
        elif kind is not None:
            candidates = self._idx_symbols_by_kind.get(kind, [])
        else:
            candidates = self._symbols

        result: list[SymbolFact] = []
        for s in candidates:
            if name is not None and s.name != name:
                continue
            if kind is not None and s.kind != kind:
                continue
            if file_path is not None and s.file_path != file_path:
                continue
            result.append(s)
        return result

    def calls_from(self, caller_qn: str) -> list[CallFact]:
        """Return all calls made by *caller_qn*."""
        return list(self._idx_calls_from.get(caller_qn, []))

    def calls_to(self, callee_qn: str) -> list[CallFact]:
        """Return all call sites that invoke *callee_qn*."""
        return list(self._idx_calls_to.get(callee_qn, []))

    def references_to(self, symbol_qn: str) -> list[ReferenceFact]:
        """Return all references to *symbol_qn*."""
        return list(self._idx_refs_to.get(symbol_qn, []))

    def taint_flows(
        self,
        label: str | None = None,
        file_path: str | None = None,
    ) -> list[TaintFlowFact]:
        """Query taint flow facts with optional filters."""
        if label is not None and file_path is None:
            return list(self._idx_taint_by_label.get(label, []))
        if file_path is not None and label is None:
            return list(self._idx_taint_by_file.get(file_path, []))

        if label is not None and file_path is not None:
            return [
                f for f in self._idx_taint_by_file.get(file_path, [])
                if f.label == label
            ]
        return list(self._taint_flows)

    def types_for(self, symbol_qn: str) -> list[TypeFact]:
        """Return all type bindings for *symbol_qn*."""
        return list(self._idx_types_by_qn.get(symbol_qn, []))

    def imports_in(self, file_path: str) -> list[ImportFact]:
        """Return all imports declared in *file_path*."""
        return list(self._idx_imports_by_file.get(file_path, []))

    def transitive_callers(self, symbol_qn: str, max_depth: int = 10) -> set[str]:
        """Compute the transitive set of callers of *symbol_qn* via BFS.

        Returns a set of qualified names (excluding the start symbol).
        """
        visited: set[str] = set()
        frontier = {symbol_qn}
        depth = 0
        while frontier and depth < max_depth:
            next_frontier: set[str] = set()
            for qn in frontier:
                for call in self._idx_calls_to.get(qn, []):
                    if call.caller_qn not in visited and call.caller_qn != symbol_qn:
                        visited.add(call.caller_qn)
                        next_frontier.add(call.caller_qn)
            frontier = next_frontier
            depth += 1
        return visited

    def transitive_callees(self, symbol_qn: str, max_depth: int = 10) -> set[str]:
        """Compute the transitive set of callees of *symbol_qn* via BFS.

        Returns a set of qualified names (excluding the start symbol).
        """
        visited: set[str] = set()
        frontier = {symbol_qn}
        depth = 0
        while frontier and depth < max_depth:
            next_frontier: set[str] = set()
            for qn in frontier:
                for call in self._idx_calls_from.get(qn, []):
                    if call.callee_qn not in visited and call.callee_qn != symbol_qn:
                        visited.add(call.callee_qn)
                        next_frontier.add(call.callee_qn)
            frontier = next_frontier
            depth += 1
        return visited

    def query(self, predicate: Callable[[Fact], bool]) -> list[Fact]:
        """Return all facts matching *predicate*.

        Scans every fact collection. For targeted queries prefer the
        typed methods which use indexes.
        """
        results: list[Fact] = []
        for collection in (
            self._symbols,
            self._calls,
            self._references,
            self._taint_flows,
            self._types,
            self._imports,
        ):
            for fact in collection:
                if predicate(fact):
                    results.append(fact)
        return results

    # -- Serialization ----------------------------------------------------

    def to_json(self) -> str:
        """Serialize the entire fact graph to a JSON string."""
        def _tag(fact: Fact) -> dict[str, Any]:
            d = asdict(fact)  # type: ignore[arg-type]
            d["_type"] = type(fact).__name__
            return d

        data: list[dict[str, Any]] = []
        for collection in (
            self._symbols,
            self._calls,
            self._references,
            self._taint_flows,
            self._types,
            self._imports,
        ):
            for fact in collection:
                data.append(_tag(fact))
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> FactGraph:
        """Deserialize a fact graph from a JSON string.

        Args:
            json_str: JSON produced by ``to_json()``.

        Returns:
            A new FactGraph populated with the deserialized facts.
        """
        graph = cls()
        _TYPE_MAP: dict[str, tuple[type, Callable[..., None]]] = {
            "SymbolFact": (SymbolFact, graph.add_symbol),
            "CallFact": (CallFact, graph.add_call),
            "ReferenceFact": (ReferenceFact, graph.add_reference),
            "TaintFlowFact": (TaintFlowFact, graph.add_taint_flow),
            "TypeFact": (TypeFact, graph.add_type),
            "ImportFact": (ImportFact, graph.add_import),
        }

        for entry in json.loads(json_str):
            type_name = entry.pop("_type", None)
            if type_name not in _TYPE_MAP:
                logger.warning("Unknown fact type in JSON: %s", type_name)
                continue
            fact_cls, adder = _TYPE_MAP[type_name]
            adder(fact_cls(**entry))
        return graph

    # -- Project builder --------------------------------------------------

    @classmethod
    def build_from_project(
        cls,
        project_path: str,
        language: str = "python",
    ) -> FactGraph:
        """Populate a fact graph by visiting all files in a project.

        Uses emend's tree-sitter infrastructure (``emend_core``) to
        extract symbols, references, calls, and imports from every
        source file in the project.

        Args:
            project_path: Root directory of the project.
            language: Source language to analyze (default ``"python"``).

        Returns:
            A fully populated FactGraph.
        """
        # Lazy imports to avoid circular dependencies.
        from emend import emend_core as _rust
        from emend.transform import (
            _collect_source_files,
            _file_to_module,
            _find_project_root,
        )

        graph = cls()
        project_root = _find_project_root(project_path)
        source_files = _collect_source_files(project_root, language=language)

        for file_path in source_files:
            try:
                content = Path(file_path).read_text(encoding="utf-8")
            except Exception:
                logger.debug("Could not read %s", file_path, exc_info=True)
                continue

            module_name = _file_to_module(file_path, project_root)

            # -- Symbol facts (via Rust symbol collection) ----------------
            try:
                ext = Path(file_path).suffix.lstrip(".") or "py"
                raw_symbols = _rust.collect_symbols_from_str(content, ext=ext)
            except Exception:
                logger.debug("Could not parse %s for symbols", file_path, exc_info=True)
                raw_symbols = []

            _walk_symbols(graph, raw_symbols, file_path, module_name, parent_qn=None)

            # -- Reference and call facts (via scope resolver) ------------
            try:
                ext = Path(file_path).suffix.lstrip(".") or "py"
                resolver = _rust.PyScopeResolver(project_root, ext)
                resolver.index_file(file_path, content)
            except Exception:
                logger.debug(
                    "Could not build scope resolver for %s", file_path, exc_info=True
                )
                resolver = None

            if resolver is not None:
                # Build a map from (line, col) -> enclosing symbol QN for call facts.
                symbol_ranges = _build_symbol_line_index(graph, file_path)

                try:
                    refs = resolver.references_in_file(file_path)
                except Exception:
                    logger.debug(
                        "references_in_file failed for %s", file_path, exc_info=True
                    )
                    refs = []

                for qn, line, col, _offset, _end_offset, kind in refs:
                    ref_kind = _map_ref_kind(kind)
                    graph.add_reference(ReferenceFact(
                        symbol_qn=qn,
                        file_path=file_path,
                        line=line,
                        col=col,
                        ref_kind=ref_kind,
                    ))

                    # For call references, also record a CallFact if we can
                    # determine the enclosing function.
                    if ref_kind == "call":
                        caller = _enclosing_symbol(symbol_ranges, line)
                        if caller is not None:
                            graph.add_call(CallFact(
                                caller_qn=caller,
                                callee_qn=qn,
                                file_path=file_path,
                                line=line,
                                col=col,
                            ))

            # -- Import facts (via stdlib ast) ----------------------------
            _extract_imports(graph, file_path, content)

        return graph


# ---------------------------------------------------------------------------
# Internal helpers for build_from_project
# ---------------------------------------------------------------------------

def _walk_symbols(
    graph: FactGraph,
    raw_symbols: list[dict[str, Any]],
    file_path: str,
    module_name: str,
    parent_qn: str | None,
) -> None:
    """Recursively walk Rust symbol dicts and add SymbolFact entries."""
    for d in raw_symbols:
        kind = d.get("kind", "")
        if kind in ("variable", "reference"):
            continue

        name = d["name"]
        path_parts = list(d.get("path", []))
        if path_parts:
            qn = f"{module_name}.{'.'.join(path_parts)}"
        else:
            qn = f"{module_name}.{name}"

        graph.add_symbol(SymbolFact(
            file_path=file_path,
            name=name,
            qualified_name=qn,
            kind=kind,
            line=d["line"],
            end_line=d["end_line"],
            parent=parent_qn,
        ))

        children = d.get("children", [])
        if children:
            _walk_symbols(graph, children, file_path, module_name, parent_qn=qn)


def _map_ref_kind(kind: str) -> Literal["read", "write", "call", "import"]:
    """Map a Rust scope-resolver reference kind to our fact model."""
    if kind == "call":
        return "call"
    if kind == "write":
        return "write"
    if kind == "import":
        return "import"
    # "definition", "read", or anything else -> "read"
    return "read"


def _build_symbol_line_index(
    graph: FactGraph,
    file_path: str,
) -> list[tuple[int, int, str]]:
    """Build a sorted list of (start_line, end_line, qn) for symbols in *file_path*.

    Used to resolve which function encloses a given line number.
    """
    entries: list[tuple[int, int, str]] = []
    for sym in graph._idx_symbols_by_file.get(file_path, []):
        if sym.kind in ("function", "async_function", "method", "async_method"):
            entries.append((sym.line, sym.end_line, sym.qualified_name))
    # Sort by start line descending so the innermost (most-nested) function
    # is checked first in _enclosing_symbol.
    entries.sort(key=lambda e: e[0], reverse=True)
    return entries


def _enclosing_symbol(
    symbol_ranges: list[tuple[int, int, str]],
    line: int,
) -> str | None:
    """Return the qualified name of the innermost function containing *line*."""
    for start, end, qn in symbol_ranges:
        if start <= line <= end:
            return qn
    return None


def _extract_imports(graph: FactGraph, file_path: str, content: str) -> None:
    """Extract import facts from *content* using the stdlib ``ast`` module."""
    import ast as stdlib_ast

    try:
        tree = stdlib_ast.parse(content, filename=file_path)
    except Exception:
        logger.debug("ast.parse failed for %s", file_path, exc_info=True)
        return

    for node in stdlib_ast.walk(tree):
        if isinstance(node, stdlib_ast.Import):
            for alias in node.names:
                graph.add_import(ImportFact(
                    importing_file=file_path,
                    imported_module=alias.name,
                    imported_name=None,
                    alias=alias.asname,
                    line=node.lineno,
                ))
        elif isinstance(node, stdlib_ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                graph.add_import(ImportFact(
                    importing_file=file_path,
                    imported_module=module,
                    imported_name=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                ))


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------

def flows_from(source_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching taint flows whose source_var matches *source_pattern*.

    The pattern is matched as a regex against ``TaintFlowFact.source_var``.
    """
    compiled = re.compile(source_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TaintFlowFact) and compiled.search(fact.source_var) is not None

    return _predicate


def flows_to(sink_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching taint flows whose sink_var matches *sink_pattern*.

    The pattern is matched as a regex against ``TaintFlowFact.sink_var``.
    """
    compiled = re.compile(sink_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TaintFlowFact) and compiled.search(fact.sink_var) is not None

    return _predicate


def reachable_from(symbol_qn: str) -> Callable[[FactGraph], set[str]]:
    """Return a callable that computes the transitive call closure from *symbol_qn*.

    Usage::

        closure = reachable_from("mymod.my_func")(graph)
    """
    def _compute(graph: FactGraph) -> set[str]:
        return graph.transitive_callees(symbol_qn)

    return _compute


def symbol_has_type(type_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching TypeFacts whose type_str matches *type_pattern*.

    The pattern is matched as a regex against ``TypeFact.type_str``.
    """
    compiled = re.compile(type_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TypeFact) and compiled.search(fact.type_str) is not None

    return _predicate
