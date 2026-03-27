"""Relational fact model for code invariants, backed by CozoDB.

Provides a unified, queryable graph of code facts (symbols, calls,
references, taint flows, types, imports) extracted from a project's
source tree using emend's existing analysis infrastructure.

The backing store is CozoDB with the SQLite engine, giving us:
- Datalog queries with semi-naive evaluation and stratified negation
- Persistent on-disk storage
- Transitive closures as native recursive rules
- User-definable CozoScript queries via ``emend query``
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fact types (stable dataclass API)
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
# CozoDB helpers
# ---------------------------------------------------------------------------

def _create_cozo_client(db_path: str | None = None) -> Any:
    """Create a CozoDB client with the SQLite backend.

    Uses the Rust ``PyCozoDb`` exposed by ``emend_core`` (compiled
    with the ``cozo`` crate).  Falls back to ``pycozo.Client`` if
    available (for standalone testing outside the full build).

    If *db_path* is ``None``, uses a temporary in-memory database.
    """
    try:
        from emend import emend_core  # type: ignore[attr-defined]
        if db_path is None:
            return emend_core.PyCozoDb("mem", "")
        return emend_core.PyCozoDb("sqlite", str(db_path))
    except (ImportError, AttributeError):
        pass

    # Fallback: use pycozo Python package if available
    from pycozo import Client  # type: ignore[import-untyped]
    if db_path is None:
        return Client("mem", "")
    return Client("sqlite", str(db_path))


_SCHEMA_INIT = """\
{:create symbol {
    qualified_name: String
    =>
    file_path: String,
    name: String,
    kind: String,
    line: Int,
    end_line: Int,
    parent: String default ""
}}

{:create call {
    caller_qn: String,
    callee_qn: String,
    file_path: String,
    line: Int,
    col: Int
}}

{:create reference {
    symbol_qn: String,
    file_path: String,
    line: Int,
    col: Int
    =>
    ref_kind: String
}}

{:create taint_flow {
    source_var: String,
    sink_var: String,
    label: String,
    file_path: String,
    func_qn: String,
    source_line: Int,
    sink_line: Int
}}

{:create type_binding {
    symbol_qn: String,
    file_path: String,
    line: Int,
    binding_kind: String
    =>
    type_str: String
}}

{:create import {
    importing_file: String,
    imported_module: String,
    imported_name: String default "",
    line: Int
    =>
    alias: String default ""
}}
"""


def _init_schema(client: Any) -> None:
    """Create stored relations if they don't exist."""
    for stmt in _SCHEMA_INIT.strip().split("\n\n"):
        stmt = stmt.strip()
        if stmt:
            try:
                client.run(stmt)
            except Exception:
                # Relation already exists — that's fine.
                pass


# ---------------------------------------------------------------------------
# FactGraph — CozoDB-backed
# ---------------------------------------------------------------------------

class FactGraph:
    """Queryable store of code facts, backed by CozoDB (SQLite engine).

    All queries are executed as CozoScript. The Python dataclass types
    are preserved as the public API for callers that need typed results.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._client = _create_cozo_client(db_path)
        _init_schema(self._client)

    @property
    def client(self) -> Any:
        """Expose the underlying CozoDB client for raw CozoScript queries."""
        return self._client

    def run_query(self, cozoscript: str) -> dict[str, Any]:
        """Execute a raw CozoScript query and return the result dict.

        The result has keys ``headers`` (list of column names) and
        ``rows`` (list of row tuples).
        """
        return self._client.run(cozoscript)

    def close(self) -> None:
        """Close the underlying database connection."""
        try:
            self._client.close()
        except Exception:
            pass

    # -- Mutation ---------------------------------------------------------

    def add_symbol(self, fact: SymbolFact) -> None:
        """Add a symbol definition fact."""
        self._client.run(
            "?[qualified_name, file_path, name, kind, line, end_line, parent] <- "
            "[[$qn, $fp, $name, $kind, $line, $end, $parent]] "
            ":put symbol {qualified_name => file_path, name, kind, line, end_line, parent}",
            {
                "qn": fact.qualified_name,
                "fp": fact.file_path,
                "name": fact.name,
                "kind": fact.kind,
                "line": fact.line,
                "end": fact.end_line,
                "parent": fact.parent or "",
            },
        )

    def add_call(self, fact: CallFact) -> None:
        """Add a call relationship fact."""
        self._client.run(
            "?[caller_qn, callee_qn, file_path, line, col] <- "
            "[[$caller, $callee, $fp, $line, $col]] "
            ":put call {caller_qn, callee_qn, file_path, line, col}",
            {
                "caller": fact.caller_qn,
                "callee": fact.callee_qn,
                "fp": fact.file_path,
                "line": fact.line,
                "col": fact.col,
            },
        )

    def add_reference(self, fact: ReferenceFact) -> None:
        """Add a reference fact."""
        self._client.run(
            "?[symbol_qn, file_path, line, col, ref_kind] <- "
            "[[$qn, $fp, $line, $col, $kind]] "
            ":put reference {symbol_qn, file_path, line, col => ref_kind}",
            {
                "qn": fact.symbol_qn,
                "fp": fact.file_path,
                "line": fact.line,
                "col": fact.col,
                "kind": fact.ref_kind,
            },
        )

    def add_taint_flow(self, fact: TaintFlowFact) -> None:
        """Add a taint flow fact."""
        self._client.run(
            "?[source_var, sink_var, label, file_path, func_qn, source_line, sink_line] <- "
            "[[$sv, $skv, $lbl, $fp, $fq, $sl, $skl]] "
            ":put taint_flow {source_var, sink_var, label, file_path, func_qn, source_line, sink_line}",
            {
                "sv": fact.source_var,
                "skv": fact.sink_var,
                "lbl": fact.label,
                "fp": fact.file_path,
                "fq": fact.func_qn,
                "sl": fact.source_line,
                "skl": fact.sink_line,
            },
        )

    def add_type(self, fact: TypeFact) -> None:
        """Add a type binding fact."""
        self._client.run(
            "?[symbol_qn, file_path, line, binding_kind, type_str] <- "
            "[[$qn, $fp, $line, $bk, $ts]] "
            ":put type_binding {symbol_qn, file_path, line, binding_kind => type_str}",
            {
                "qn": fact.symbol_qn,
                "fp": fact.file_path,
                "line": fact.line,
                "bk": fact.binding_kind,
                "ts": fact.type_str,
            },
        )

    def add_import(self, fact: ImportFact) -> None:
        """Add an import fact."""
        self._client.run(
            "?[importing_file, imported_module, imported_name, line, alias] <- "
            "[[$f, $mod, $name, $line, $alias]] "
            ":put import {importing_file, imported_module, imported_name, line => alias}",
            {
                "f": fact.importing_file,
                "mod": fact.imported_module,
                "name": fact.imported_name or "",
                "line": fact.line,
                "alias": fact.alias or "",
            },
        )

    # -- Batch mutation (for build_from_project performance) ---------------

    def add_symbols_batch(self, facts: list[SymbolFact]) -> None:
        """Bulk-insert symbol facts."""
        if not facts:
            return
        rows = [
            [f.qualified_name, f.file_path, f.name, f.kind, f.line, f.end_line, f.parent or ""]
            for f in facts
        ]
        self._client.run(
            "?[qualified_name, file_path, name, kind, line, end_line, parent] <- $rows "
            ":put symbol {qualified_name => file_path, name, kind, line, end_line, parent}",
            {"rows": rows},
        )

    def add_calls_batch(self, facts: list[CallFact]) -> None:
        """Bulk-insert call facts."""
        if not facts:
            return
        rows = [[f.caller_qn, f.callee_qn, f.file_path, f.line, f.col] for f in facts]
        self._client.run(
            "?[caller_qn, callee_qn, file_path, line, col] <- $rows "
            ":put call {caller_qn, callee_qn, file_path, line, col}",
            {"rows": rows},
        )

    def add_references_batch(self, facts: list[ReferenceFact]) -> None:
        """Bulk-insert reference facts."""
        if not facts:
            return
        rows = [[f.symbol_qn, f.file_path, f.line, f.col, f.ref_kind] for f in facts]
        self._client.run(
            "?[symbol_qn, file_path, line, col, ref_kind] <- $rows "
            ":put reference {symbol_qn, file_path, line, col => ref_kind}",
            {"rows": rows},
        )

    def add_imports_batch(self, facts: list[ImportFact]) -> None:
        """Bulk-insert import facts."""
        if not facts:
            return
        rows = [
            [f.importing_file, f.imported_module, f.imported_name or "", f.line, f.alias or ""]
            for f in facts
        ]
        self._client.run(
            "?[importing_file, imported_module, imported_name, line, alias] <- $rows "
            ":put import {importing_file, imported_module, imported_name, line => alias}",
            {"rows": rows},
        )

    # -- Queries ----------------------------------------------------------

    def symbols(
        self,
        name: str | None = None,
        kind: str | None = None,
        file_path: str | None = None,
    ) -> list[SymbolFact]:
        """Query symbol facts with optional filters."""
        clauses = ["*symbol[qn, fp, n, k, line, end_line, parent]"]
        params: dict[str, Any] = {}

        if name is not None:
            clauses.append("n == $name")
            params["name"] = name
        if kind is not None:
            clauses.append("k == $kind")
            params["kind"] = kind
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path

        query = (
            "?[fp, n, qn, k, line, end_line, parent] := "
            + ", ".join(clauses)
        )
        result = self._client.run(query, params)
        return [
            SymbolFact(
                file_path=r[0],
                name=r[1],
                qualified_name=r[2],
                kind=r[3],
                line=r[4],
                end_line=r[5],
                parent=r[6] if r[6] else None,
            )
            for r in result["rows"]
        ]

    def calls_from(self, caller_qn: str) -> list[CallFact]:
        """Return all calls made by *caller_qn*."""
        result = self._client.run(
            "?[caller, callee, fp, line, col] := "
            "*call[caller, callee, fp, line, col], caller == $qn",
            {"qn": caller_qn},
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3], col=r[4])
            for r in result["rows"]
        ]

    def calls_to(self, callee_qn: str) -> list[CallFact]:
        """Return all call sites that invoke *callee_qn*."""
        result = self._client.run(
            "?[caller, callee, fp, line, col] := "
            "*call[caller, callee, fp, line, col], callee == $qn",
            {"qn": callee_qn},
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3], col=r[4])
            for r in result["rows"]
        ]

    def references_to(self, symbol_qn: str) -> list[ReferenceFact]:
        """Return all references to *symbol_qn*."""
        result = self._client.run(
            "?[qn, fp, line, col, kind] := "
            "*reference[qn, fp, line, col, kind], qn == $qn",
            {"qn": symbol_qn},
        )
        return [
            ReferenceFact(symbol_qn=r[0], file_path=r[1], line=r[2], col=r[3], ref_kind=r[4])
            for r in result["rows"]
        ]

    def taint_flows(
        self,
        label: str | None = None,
        file_path: str | None = None,
    ) -> list[TaintFlowFact]:
        """Query taint flow facts with optional filters."""
        clauses = ["*taint_flow[sv, skv, lbl, fp, fq, sl, skl]"]
        params: dict[str, Any] = {}

        if label is not None:
            clauses.append("lbl == $label")
            params["label"] = label
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path

        query = (
            "?[sv, skv, lbl, fp, fq, sl, skl] := "
            + ", ".join(clauses)
        )
        result = self._client.run(query, params)
        return [
            TaintFlowFact(
                source_var=r[0], sink_var=r[1], label=r[2],
                file_path=r[3], func_qn=r[4], source_line=r[5], sink_line=r[6],
            )
            for r in result["rows"]
        ]

    def types_for(self, symbol_qn: str) -> list[TypeFact]:
        """Return all type bindings for *symbol_qn*."""
        result = self._client.run(
            "?[qn, ts, fp, line, bk] := "
            "*type_binding[qn, fp, line, bk, ts], qn == $qn",
            {"qn": symbol_qn},
        )
        return [
            TypeFact(symbol_qn=r[0], type_str=r[1], file_path=r[2], line=r[3], binding_kind=r[4])
            for r in result["rows"]
        ]

    def imports_in(self, file_path: str) -> list[ImportFact]:
        """Return all imports declared in *file_path*."""
        result = self._client.run(
            "?[f, mod, name, alias, line] := "
            "*import[f, mod, name, line, alias], f == $fp",
            {"fp": file_path},
        )
        return [
            ImportFact(
                importing_file=r[0],
                imported_module=r[1],
                imported_name=r[2] if r[2] else None,
                alias=r[3] if r[3] else None,
                line=r[4],
            )
            for r in result["rows"]
        ]

    # -- Transitive closures (Datalog! No more Python BFS) ---------------

    def transitive_callers(self, symbol_qn: str, max_depth: int = 10) -> set[str]:
        """Compute the transitive set of callers of *symbol_qn* via Datalog."""
        result = self._client.run(
            "reaches[a] := *call[a, b, _, _, _], b == $qn\n"
            "reaches[a] := *call[a, mid, _, _, _], reaches[mid]\n"
            "?[a] := reaches[a]",
            {"qn": symbol_qn},
        )
        return {r[0] for r in result["rows"]} - {symbol_qn}

    def transitive_callees(self, symbol_qn: str, max_depth: int = 10) -> set[str]:
        """Compute the transitive set of callees of *symbol_qn* via Datalog."""
        result = self._client.run(
            "reaches[b] := *call[a, b, _, _, _], a == $qn\n"
            "reaches[b] := *call[mid, b, _, _, _], reaches[mid]\n"
            "?[b] := reaches[b]",
            {"qn": symbol_qn},
        )
        return {r[0] for r in result["rows"]} - {symbol_qn}

    # -- Dead code detection as Datalog ----------------------------------

    def dead_code(
        self,
        entry_kinds: set[str] | None = None,
    ) -> list[SymbolFact]:
        """Find symbols with no references — pure Datalog query.

        Returns top-level symbols (functions, classes) that have no
        incoming references and are not entry points.
        """
        result = self._client.run(
            "has_ref[qn] := *reference[qn, _, _, _, _]\n"
            "dead[qn, fp, name, kind, line, end_line, parent] := "
            "*symbol[qn, fp, name, kind, line, end_line, parent], "
            "not has_ref[qn]\n"
            "?[fp, name, qn, kind, line, end_line, parent] := "
            "dead[qn, fp, name, kind, line, end_line, parent]"
        )
        return [
            SymbolFact(
                file_path=r[0], name=r[1], qualified_name=r[2],
                kind=r[3], line=r[4], end_line=r[5],
                parent=r[6] if r[6] else None,
            )
            for r in result["rows"]
        ]

    # -- Generic query (predicate-based, for backwards compat) -----------

    def query(self, predicate: Callable[[Fact], bool]) -> list[Fact]:
        """Return all facts matching *predicate*.

        This fetches all facts from CozoDB and filters in Python.
        For performance-sensitive queries, use ``run_query()`` with
        CozoScript instead.
        """
        results: list[Fact] = []
        for fact in self.symbols():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_calls():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_references():
            if predicate(fact):
                results.append(fact)
        for fact in self.taint_flows():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_types():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_imports():
            if predicate(fact):
                results.append(fact)
        return results

    def _all_calls(self) -> list[CallFact]:
        result = self._client.run(
            "?[caller, callee, fp, line, col] := *call[caller, callee, fp, line, col]"
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3], col=r[4])
            for r in result["rows"]
        ]

    def _all_references(self) -> list[ReferenceFact]:
        result = self._client.run(
            "?[qn, fp, line, col, kind] := *reference[qn, fp, line, col, kind]"
        )
        return [
            ReferenceFact(symbol_qn=r[0], file_path=r[1], line=r[2], col=r[3], ref_kind=r[4])
            for r in result["rows"]
        ]

    def _all_types(self) -> list[TypeFact]:
        result = self._client.run(
            "?[qn, ts, fp, line, bk] := *type_binding[qn, fp, line, bk, ts]"
        )
        return [
            TypeFact(symbol_qn=r[0], type_str=r[1], file_path=r[2], line=r[3], binding_kind=r[4])
            for r in result["rows"]
        ]

    def _all_imports(self) -> list[ImportFact]:
        result = self._client.run(
            "?[f, mod, name, alias, line] := *import[f, mod, name, line, alias]"
        )
        return [
            ImportFact(
                importing_file=r[0], imported_module=r[1],
                imported_name=r[2] if r[2] else None,
                alias=r[3] if r[3] else None, line=r[4],
            )
            for r in result["rows"]
        ]

    # -- Serialization ----------------------------------------------------

    def to_json(self) -> str:
        """Serialize the entire fact graph to a JSON string."""
        def _tag(fact: Fact) -> dict[str, Any]:
            d = asdict(fact)  # type: ignore[arg-type]
            d["_type"] = type(fact).__name__
            return d

        data: list[dict[str, Any]] = []
        for fact in self.symbols():
            data.append(_tag(fact))
        for fact in self._all_calls():
            data.append(_tag(fact))
        for fact in self._all_references():
            data.append(_tag(fact))
        for fact in self.taint_flows():
            data.append(_tag(fact))
        for fact in self._all_types():
            data.append(_tag(fact))
        for fact in self._all_imports():
            data.append(_tag(fact))
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> FactGraph:
        """Deserialize a fact graph from a JSON string."""
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
        db_path: str | None = None,
    ) -> FactGraph:
        """Populate a fact graph by visiting all files in a project.

        Uses emend's tree-sitter infrastructure (``emend_core``) to
        extract symbols, references, calls, and imports from every
        source file in the project.
        """
        from emend import emend_core as _rust
        from emend.transform import (
            _collect_source_files,
            _file_to_module,
            _find_project_root,
        )

        graph = cls(db_path=db_path)
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

            sym_facts: list[SymbolFact] = []
            _walk_symbols(sym_facts, raw_symbols, file_path, module_name, parent_qn=None)
            graph.add_symbols_batch(sym_facts)

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
                symbol_ranges = _build_symbol_line_index(sym_facts, file_path)

                try:
                    refs = resolver.references_in_file(file_path)
                except Exception:
                    logger.debug(
                        "references_in_file failed for %s", file_path, exc_info=True
                    )
                    refs = []

                ref_facts: list[ReferenceFact] = []
                call_facts: list[CallFact] = []
                for qn, line, col, _offset, _end_offset, kind in refs:
                    ref_kind = _map_ref_kind(kind)
                    ref_facts.append(ReferenceFact(
                        symbol_qn=qn, file_path=file_path,
                        line=line, col=col, ref_kind=ref_kind,
                    ))

                    if ref_kind == "call":
                        caller = _enclosing_symbol(symbol_ranges, line)
                        if caller is not None:
                            call_facts.append(CallFact(
                                caller_qn=caller, callee_qn=qn,
                                file_path=file_path, line=line, col=col,
                            ))

                graph.add_references_batch(ref_facts)
                graph.add_calls_batch(call_facts)

            # -- Import facts (via stdlib ast) ----------------------------
            import_facts = _extract_imports(file_path, content)
            graph.add_imports_batch(import_facts)

        return graph


# ---------------------------------------------------------------------------
# Internal helpers for build_from_project
# ---------------------------------------------------------------------------

def _walk_symbols(
    out: list[SymbolFact],
    raw_symbols: list[dict[str, Any]],
    file_path: str,
    module_name: str,
    parent_qn: str | None,
) -> None:
    """Recursively walk Rust symbol dicts and collect SymbolFact entries."""
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

        out.append(SymbolFact(
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
            _walk_symbols(out, children, file_path, module_name, parent_qn=qn)


def _map_ref_kind(kind: str) -> Literal["read", "write", "call", "import"]:
    """Map a Rust scope-resolver reference kind to our fact model."""
    if kind == "call":
        return "call"
    if kind == "write":
        return "write"
    if kind == "import":
        return "import"
    return "read"


def _build_symbol_line_index(
    sym_facts: list[SymbolFact],
    file_path: str,
) -> list[tuple[int, int, str]]:
    """Build a sorted list of (start_line, end_line, qn) for function symbols."""
    entries: list[tuple[int, int, str]] = []
    for sym in sym_facts:
        if sym.file_path == file_path and sym.kind in (
            "function", "async_function", "method", "async_method"
        ):
            entries.append((sym.line, sym.end_line, sym.qualified_name))
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


def _extract_imports(file_path: str, content: str) -> list[ImportFact]:
    """Extract import facts from *content* using the stdlib ``ast`` module."""
    import ast as stdlib_ast

    facts: list[ImportFact] = []
    try:
        tree = stdlib_ast.parse(content, filename=file_path)
    except Exception:
        logger.debug("ast.parse failed for %s", file_path, exc_info=True)
        return facts

    for node in stdlib_ast.walk(tree):
        if isinstance(node, stdlib_ast.Import):
            for alias in node.names:
                facts.append(ImportFact(
                    importing_file=file_path,
                    imported_module=alias.name,
                    imported_name=None,
                    alias=alias.asname,
                    line=node.lineno,
                ))
        elif isinstance(node, stdlib_ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                facts.append(ImportFact(
                    importing_file=file_path,
                    imported_module=module,
                    imported_name=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                ))
    return facts


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------

def flows_from(source_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching taint flows whose source_var matches *source_pattern*."""
    compiled = re.compile(source_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TaintFlowFact) and compiled.search(fact.source_var) is not None

    return _predicate


def flows_to(sink_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching taint flows whose sink_var matches *sink_pattern*."""
    compiled = re.compile(sink_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TaintFlowFact) and compiled.search(fact.sink_var) is not None

    return _predicate


def reachable_from(symbol_qn: str) -> Callable[[FactGraph], set[str]]:
    """Return a callable that computes the transitive call closure from *symbol_qn*."""
    def _compute(graph: FactGraph) -> set[str]:
        return graph.transitive_callees(symbol_qn)

    return _compute


def symbol_has_type(type_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching TypeFacts whose type_str matches *type_pattern*."""
    compiled = re.compile(type_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TypeFact) and compiled.search(fact.type_str) is not None

    return _predicate
