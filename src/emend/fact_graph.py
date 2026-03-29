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
    func_qn: str = ""      # containing function
    block_id: int = -1      # containing CFG block


@dataclass(frozen=True)
class ReferenceFact:
    """A reference to a symbol at a specific location."""
    symbol_qn: str
    file_path: str
    line: int
    col: int
    ref_kind: Literal["read", "write", "call", "import"]
    func_qn: str = ""      # containing function (empty for module-level)
    block_id: int = -1      # containing CFG block (-1 for module-level)


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


@dataclass(frozen=True)
class CfgEdgeFact:
    """A control flow edge within a function."""
    file_path: str
    func_qn: str
    from_block: int
    to_block: int
    edge_kind: str  # fallthrough, true_branch, false_branch, exception, finally, back_edge, jump
    from_line: int
    to_line: int


@dataclass(frozen=True)
class DefUseFact:
    """A definition-use relationship within a function."""
    file_path: str
    func_qn: str
    var_name: str
    def_block: int
    use_block: int
    def_line: int = 0    # kept for backwards compat display
    def_col: int = 0
    use_line: int = 0
    use_col: int = 0


@dataclass(frozen=True)
class CfgBlockFact:
    """A basic block in a function's control flow graph."""
    file_path: str
    func_qn: str
    block_id: int
    is_entry: bool = False
    is_exit: bool = False


@dataclass(frozen=True)
class DecoratorOnFact:
    """A decorator applied to a symbol."""
    symbol_qn: str
    decorator: str


@dataclass(frozen=True)
class SourceLocFact:
    """Display-only source location (not joined in analysis)."""
    file_path: str
    loc_kind: str  # "symbol", "reference", "call", etc.
    loc_id: str    # qualified_name or ref_id
    line: int
    col: int = 0
    end_line: int = 0
    rel_line: int = 0


@dataclass(frozen=True)
class FuncSummaryFact:
    """Interprocedural taint summary for a function parameter."""
    func_qn: str
    param_name: str
    flows_to_return: bool = False
    flows_to_sink: bool = False
    sink_label: str = ""


@dataclass(frozen=True)
class EntryPointDecoratorFact:
    """A decorator that marks a symbol as an entry point."""
    decorator: str


@dataclass(frozen=True)
class EntryPointNameFact:
    """A name pattern that marks a symbol as an entry point."""
    name: str


# Union of all fact types for generic queries.
Fact = Union[
    SymbolFact, CallFact, ReferenceFact, TaintFlowFact, TypeFact,
    ImportFact, CfgEdgeFact, DefUseFact, CfgBlockFact, DecoratorOnFact,
    SourceLocFact, FuncSummaryFact, EntryPointDecoratorFact, EntryPointNameFact,
]


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
    =>
    func_qn: String default "",
    block_id: Int default -1
}}

{:create reference {
    symbol_qn: String,
    file_path: String,
    line: Int,
    col: Int
    =>
    ref_kind: String,
    func_qn: String default "",
    block_id: Int default -1
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

{:create cfg_edge {
    file_path: String,
    func_qn: String,
    from_block: Int,
    to_block: Int,
    edge_kind: String,
    from_line: Int,
    to_line: Int
}}

{:create def_use {
    file_path: String,
    func_qn: String,
    var_name: String,
    def_block: Int,
    use_block: Int
    =>
    def_line: Int default 0,
    def_col: Int default 0,
    use_line: Int default 0,
    use_col: Int default 0
}}

{:create cfg_block {
    file_path: String,
    func_qn: String,
    block_id: Int
    =>
    is_entry: Bool default false,
    is_exit: Bool default false
}}

{:create decorator_on {
    symbol_qn: String,
    decorator: String
}}

{:create source_loc {
    file_path: String,
    loc_kind: String,
    loc_id: String
    =>
    line: Int,
    col: Int default 0,
    end_line: Int default 0,
    rel_line: Int default 0
}}

{:create func_summary {
    func_qn: String,
    param_name: String
    =>
    flows_to_return: Bool default false,
    flows_to_sink: Bool default false,
    sink_label: String default ""
}}

{:create entry_point_decorator { decorator: String }}

{:create entry_point_name { name: String }}
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
            "?[caller_qn, callee_qn, file_path, line, col, func_qn, block_id] <- "
            "[[$caller, $callee, $fp, $line, $col, $fq, $bid]] "
            ":put call {caller_qn, callee_qn, file_path, line, col => func_qn, block_id}",
            {
                "caller": fact.caller_qn,
                "callee": fact.callee_qn,
                "fp": fact.file_path,
                "line": fact.line,
                "col": fact.col,
                "fq": fact.func_qn,
                "bid": fact.block_id,
            },
        )

    def add_reference(self, fact: ReferenceFact) -> None:
        """Add a reference fact."""
        self._client.run(
            "?[symbol_qn, file_path, line, col, ref_kind, func_qn, block_id] <- "
            "[[$qn, $fp, $line, $col, $kind, $fq, $bid]] "
            ":put reference {symbol_qn, file_path, line, col => ref_kind, func_qn, block_id}",
            {
                "qn": fact.symbol_qn,
                "fp": fact.file_path,
                "line": fact.line,
                "col": fact.col,
                "kind": fact.ref_kind,
                "fq": fact.func_qn,
                "bid": fact.block_id,
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

    def add_cfg_edge(self, fact: CfgEdgeFact) -> None:
        """Add a control flow edge fact."""
        self._client.run(
            "?[file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line] <- "
            "[[$fp, $fq, $fb, $tb, $ek, $fl, $tl]] "
            ":put cfg_edge {file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line}",
            {
                "fp": fact.file_path,
                "fq": fact.func_qn,
                "fb": fact.from_block,
                "tb": fact.to_block,
                "ek": fact.edge_kind,
                "fl": fact.from_line,
                "tl": fact.to_line,
            },
        )

    def add_def_use(self, fact: DefUseFact) -> None:
        """Add a definition-use fact."""
        self._client.run(
            "?[file_path, func_qn, var_name, def_block, use_block, def_line, def_col, use_line, use_col] <- "
            "[[$fp, $fq, $vn, $db, $ub, $dl, $dc, $ul, $uc]] "
            ":put def_use {file_path, func_qn, var_name, def_block, use_block => def_line, def_col, use_line, use_col}",
            {
                "fp": fact.file_path,
                "fq": fact.func_qn,
                "vn": fact.var_name,
                "db": fact.def_block,
                "ub": fact.use_block,
                "dl": fact.def_line,
                "dc": fact.def_col,
                "ul": fact.use_line,
                "uc": fact.use_col,
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
        rows = [[f.caller_qn, f.callee_qn, f.file_path, f.line, f.col, f.func_qn, f.block_id] for f in facts]
        self._client.run(
            "?[caller_qn, callee_qn, file_path, line, col, func_qn, block_id] <- $rows "
            ":put call {caller_qn, callee_qn, file_path, line, col => func_qn, block_id}",
            {"rows": rows},
        )

    def add_references_batch(self, facts: list[ReferenceFact]) -> None:
        """Bulk-insert reference facts."""
        if not facts:
            return
        rows = [[f.symbol_qn, f.file_path, f.line, f.col, f.ref_kind, f.func_qn, f.block_id] for f in facts]
        self._client.run(
            "?[symbol_qn, file_path, line, col, ref_kind, func_qn, block_id] <- $rows "
            ":put reference {symbol_qn, file_path, line, col => ref_kind, func_qn, block_id}",
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

    def add_cfg_edges_batch(self, facts: list[CfgEdgeFact]) -> None:
        """Bulk-insert CFG edge facts."""
        if not facts:
            return
        rows = [
            [f.file_path, f.func_qn, f.from_block, f.to_block, f.edge_kind, f.from_line, f.to_line]
            for f in facts
        ]
        self._client.run(
            "?[file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line] <- $rows "
            ":put cfg_edge {file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line}",
            {"rows": rows},
        )

    def add_def_uses_batch(self, facts: list[DefUseFact]) -> None:
        """Bulk-insert def-use facts."""
        if not facts:
            return
        rows = [
            [f.file_path, f.func_qn, f.var_name, f.def_block, f.use_block, f.def_line, f.def_col, f.use_line, f.use_col]
            for f in facts
        ]
        self._client.run(
            "?[file_path, func_qn, var_name, def_block, use_block, def_line, def_col, use_line, use_col] <- $rows "
            ":put def_use {file_path, func_qn, var_name, def_block, use_block => def_line, def_col, use_line, use_col}",
            {"rows": rows},
        )

    def add_cfg_block(self, fact: CfgBlockFact) -> None:
        """Add a CFG block fact."""
        self._client.run(
            "?[file_path, func_qn, block_id, is_entry, is_exit] <- "
            "[[$fp, $fq, $bid, $ie, $ix]] "
            ":put cfg_block {file_path, func_qn, block_id => is_entry, is_exit}",
            {"fp": fact.file_path, "fq": fact.func_qn, "bid": fact.block_id,
             "ie": fact.is_entry, "ix": fact.is_exit},
        )

    def add_cfg_blocks_batch(self, facts: list[CfgBlockFact]) -> None:
        """Bulk-insert CFG block facts."""
        if not facts:
            return
        rows = [[f.file_path, f.func_qn, f.block_id, f.is_entry, f.is_exit] for f in facts]
        self._client.run(
            "?[file_path, func_qn, block_id, is_entry, is_exit] <- $rows "
            ":put cfg_block {file_path, func_qn, block_id => is_entry, is_exit}",
            {"rows": rows},
        )

    def add_decorator_on(self, fact: DecoratorOnFact) -> None:
        """Add a decorator-on fact."""
        self._client.run(
            "?[symbol_qn, decorator] <- [[$sqn, $dec]] "
            ":put decorator_on {symbol_qn, decorator}",
            {"sqn": fact.symbol_qn, "dec": fact.decorator},
        )

    def add_decorator_on_batch(self, facts: list[DecoratorOnFact]) -> None:
        """Bulk-insert decorator-on facts."""
        if not facts:
            return
        rows = [[f.symbol_qn, f.decorator] for f in facts]
        self._client.run(
            "?[symbol_qn, decorator] <- $rows "
            ":put decorator_on {symbol_qn, decorator}",
            {"rows": rows},
        )

    def add_source_loc(self, fact: SourceLocFact) -> None:
        """Add a source location fact."""
        self._client.run(
            "?[file_path, loc_kind, loc_id, line, col, end_line, rel_line] <- "
            "[[$fp, $lk, $lid, $line, $col, $el, $rl]] "
            ":put source_loc {file_path, loc_kind, loc_id => line, col, end_line, rel_line}",
            {"fp": fact.file_path, "lk": fact.loc_kind, "lid": fact.loc_id,
             "line": fact.line, "col": fact.col, "el": fact.end_line, "rl": fact.rel_line},
        )

    def add_source_locs_batch(self, facts: list[SourceLocFact]) -> None:
        """Bulk-insert source location facts."""
        if not facts:
            return
        rows = [[f.file_path, f.loc_kind, f.loc_id, f.line, f.col, f.end_line, f.rel_line] for f in facts]
        self._client.run(
            "?[file_path, loc_kind, loc_id, line, col, end_line, rel_line] <- $rows "
            ":put source_loc {file_path, loc_kind, loc_id => line, col, end_line, rel_line}",
            {"rows": rows},
        )

    def add_func_summary(self, fact: FuncSummaryFact) -> None:
        """Add a function summary fact."""
        self._client.run(
            "?[func_qn, param_name, flows_to_return, flows_to_sink, sink_label] <- "
            "[[$fq, $pn, $ftr, $fts, $sl]] "
            ":put func_summary {func_qn, param_name => flows_to_return, flows_to_sink, sink_label}",
            {"fq": fact.func_qn, "pn": fact.param_name, "ftr": fact.flows_to_return,
             "fts": fact.flows_to_sink, "sl": fact.sink_label},
        )

    def add_func_summaries_batch(self, facts: list[FuncSummaryFact]) -> None:
        """Bulk-insert function summary facts."""
        if not facts:
            return
        rows = [[f.func_qn, f.param_name, f.flows_to_return, f.flows_to_sink, f.sink_label] for f in facts]
        self._client.run(
            "?[func_qn, param_name, flows_to_return, flows_to_sink, sink_label] <- $rows "
            ":put func_summary {func_qn, param_name => flows_to_return, flows_to_sink, sink_label}",
            {"rows": rows},
        )

    def add_entry_point_decorator(self, fact: EntryPointDecoratorFact) -> None:
        """Add an entry point decorator fact."""
        self._client.run(
            "?[decorator] <- [[$dec]] "
            ":put entry_point_decorator {decorator}",
            {"dec": fact.decorator},
        )

    def add_entry_point_decorators_batch(self, facts: list[EntryPointDecoratorFact]) -> None:
        """Bulk-insert entry point decorator facts."""
        if not facts:
            return
        rows = [[f.decorator] for f in facts]
        self._client.run(
            "?[decorator] <- $rows "
            ":put entry_point_decorator {decorator}",
            {"rows": rows},
        )

    def add_entry_point_name(self, fact: EntryPointNameFact) -> None:
        """Add an entry point name fact."""
        self._client.run(
            "?[name] <- [[$name]] "
            ":put entry_point_name {name}",
            {"name": fact.name},
        )

    def add_entry_point_names_batch(self, facts: list[EntryPointNameFact]) -> None:
        """Bulk-insert entry point name facts."""
        if not facts:
            return
        rows = [[f.name] for f in facts]
        self._client.run(
            "?[name] <- $rows "
            ":put entry_point_name {name}",
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
            "?[caller, callee, fp, line, col, fq, bid] := "
            "*call[caller, callee, fp, line, col, fq, bid], caller == $qn",
            {"qn": caller_qn},
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3],
                     col=r[4], func_qn=r[5], block_id=r[6])
            for r in result["rows"]
        ]

    def calls_to(self, callee_qn: str) -> list[CallFact]:
        """Return all call sites that invoke *callee_qn*."""
        result = self._client.run(
            "?[caller, callee, fp, line, col, fq, bid] := "
            "*call[caller, callee, fp, line, col, fq, bid], callee == $qn",
            {"qn": callee_qn},
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3],
                     col=r[4], func_qn=r[5], block_id=r[6])
            for r in result["rows"]
        ]

    def references_to(self, symbol_qn: str) -> list[ReferenceFact]:
        """Return all references to *symbol_qn*."""
        result = self._client.run(
            "?[qn, fp, line, col, kind, fq, bid] := "
            "*reference[qn, fp, line, col, kind, fq, bid], qn == $qn",
            {"qn": symbol_qn},
        )
        return [
            ReferenceFact(symbol_qn=r[0], file_path=r[1], line=r[2], col=r[3],
                          ref_kind=r[4], func_qn=r[5], block_id=r[6])
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

    def cfg_edges(
        self,
        func_qn: str | None = None,
        file_path: str | None = None,
    ) -> list[CfgEdgeFact]:
        """Query CFG edge facts with optional filters."""
        clauses = ["*cfg_edge[fp, fq, fb, tb, ek, fl, tl]"]
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses.append("fq == $func_qn")
            params["func_qn"] = func_qn
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path
        query = "?[fp, fq, fb, tb, ek, fl, tl] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            CfgEdgeFact(
                file_path=r[0], func_qn=r[1], from_block=r[2],
                to_block=r[3], edge_kind=r[4], from_line=r[5], to_line=r[6],
            )
            for r in result["rows"]
        ]

    def def_uses(
        self,
        func_qn: str | None = None,
        var_name: str | None = None,
        file_path: str | None = None,
    ) -> list[DefUseFact]:
        """Query def-use facts with optional filters."""
        clauses = ["*def_use[fp, fq, vn, db, ub, dl, dc, ul, uc]"]
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses.append("fq == $func_qn")
            params["func_qn"] = func_qn
        if var_name is not None:
            clauses.append("vn == $var_name")
            params["var_name"] = var_name
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path
        query = "?[fp, fq, vn, db, ub, dl, dc, ul, uc] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            DefUseFact(
                file_path=r[0], func_qn=r[1], var_name=r[2],
                def_block=r[3], use_block=r[4],
                def_line=r[5], def_col=r[6], use_line=r[7], use_col=r[8],
            )
            for r in result["rows"]
        ]

    def cfg_blocks(self, func_qn: str | None = None, file_path: str | None = None) -> list[CfgBlockFact]:
        """Query CFG block facts."""
        clauses = ["*cfg_block[fp, fq, bid, ie, ix]"]
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses.append("fq == $func_qn")
            params["func_qn"] = func_qn
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path
        query = "?[fp, fq, bid, ie, ix] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            CfgBlockFact(file_path=r[0], func_qn=r[1], block_id=r[2],
                         is_entry=r[3], is_exit=r[4])
            for r in result["rows"]
        ]

    def decorators_on(self, symbol_qn: str) -> list[DecoratorOnFact]:
        """Return all decorators on a symbol."""
        result = self._client.run(
            "?[sqn, dec] := *decorator_on[sqn, dec], sqn == $qn",
            {"qn": symbol_qn},
        )
        return [DecoratorOnFact(symbol_qn=r[0], decorator=r[1]) for r in result["rows"]]

    def source_locs(self, loc_id: str | None = None, loc_kind: str | None = None) -> list[SourceLocFact]:
        """Query source locations."""
        clauses = ["*source_loc[fp, lk, lid, line, col, el, rl]"]
        params: dict[str, Any] = {}
        if loc_id is not None:
            clauses.append("lid == $loc_id")
            params["loc_id"] = loc_id
        if loc_kind is not None:
            clauses.append("lk == $loc_kind")
            params["loc_kind"] = loc_kind
        query = "?[fp, lk, lid, line, col, el, rl] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            SourceLocFact(file_path=r[0], loc_kind=r[1], loc_id=r[2],
                         line=r[3], col=r[4], end_line=r[5], rel_line=r[6])
            for r in result["rows"]
        ]

    def func_summaries(self, func_qn: str | None = None) -> list[FuncSummaryFact]:
        """Query function summary facts."""
        clauses = ["*func_summary[fq, pn, ftr, fts, sl]"]
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses.append("fq == $func_qn")
            params["func_qn"] = func_qn
        query = "?[fq, pn, ftr, fts, sl] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            FuncSummaryFact(func_qn=r[0], param_name=r[1], flows_to_return=r[2],
                           flows_to_sink=r[3], sink_label=r[4])
            for r in result["rows"]
        ]

    # -- Transitive closures (Datalog! No more Python BFS) ---------------

    def transitive_callers(self, symbol_qn: str, max_depth: int = 10) -> set[str]:
        """Compute the transitive set of callers of *symbol_qn* via Datalog."""
        result = self._client.run(
            "reaches[a] := *call[a, b, _, _, _, _, _], b == $qn\n"
            "reaches[a] := *call[a, mid, _, _, _, _, _], reaches[mid]\n"
            "?[a] := reaches[a]",
            {"qn": symbol_qn},
        )
        return {r[0] for r in result["rows"]} - {symbol_qn}

    def transitive_callees(self, symbol_qn: str, max_depth: int = 10) -> set[str]:
        """Compute the transitive set of callees of *symbol_qn* via Datalog."""
        result = self._client.run(
            "reaches[b] := *call[a, b, _, _, _, _, _], a == $qn\n"
            "reaches[b] := *call[mid, b, _, _, _, _, _], reaches[mid]\n"
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
            "has_ref[qn] := *reference[qn, _, _, _, _, _, _]\n"
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

    def dead_code_unified(
        self,
        entry_point_decorators: list[str] | None = None,
        entry_point_names: list[str] | None = None,
    ) -> list[SymbolFact]:
        """Unified dead code detection via Datalog.

        Combines unreachable-block analysis with unreferenced-symbol detection
        in a single Datalog program:

        1. Computes reachable blocks via transitive closure from CFG entry blocks
        2. Only counts references from reachable code as "live"
        3. Applies entry point heuristics (dunders, test_, decorators) as Datalog rules
        4. Returns symbols with no live references that are not entry points

        String literal filtering stays as a Python post-filter (caller's responsibility).
        """
        # Seed entry point facts if provided
        setup_rules = []
        if entry_point_decorators:
            dec_rows = ", ".join(f'["{d}"]' for d in entry_point_decorators)
            setup_rules.append(f"?[decorator] <- [{dec_rows}] :put entry_point_decorator {{decorator}}")
        if entry_point_names:
            name_rows = ", ".join(f'["{n}"]' for n in entry_point_names)
            setup_rules.append(f"?[name] <- [{name_rows}] :put entry_point_name {{name}}")

        # Run setup rules if any
        for rule in setup_rules:
            try:
                self._client.run(rule)
            except Exception:
                pass

        query = (
            # Reachable blocks (transitive closure from entry)
            "reachable[fp, fq, bid] := "
            "*cfg_block[fp, fq, bid, is_entry, _], is_entry == true\n"

            "reachable[fp, fq, tb] := "
            "reachable[fp, fq, fb], "
            "*cfg_edge[fp, fq, fb, tb, _, _, _]\n"

            # Live references: from reachable code
            "live_ref[sq] := "
            "*reference[sq, fp, _, _, _, fq, bid], "
            "reachable[fp, fq, bid]\n"

            # Live references: from module level (no function context)
            'live_ref[sq] := '
            '*reference[sq, _, _, _, _, fq, bid], '
            'fq == "", bid == -1\n'

            # Entry points: dunder methods
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            'starts_with(name, "__"), ends_with(name, "__")\n'

            # Entry points: test functions
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            'starts_with(name, "test_")\n'

            # Entry points: decorated symbols
            'entry_point[qn] := '
            '*decorator_on[qn, dec], '
            '*entry_point_decorator[dec]\n'

            # Entry points: named symbols
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            '*entry_point_name[name]\n'

            # Dead symbols: no live reference and not an entry point
            "dead[qn, fp, name, kind, line, end_line, parent] := "
            "*symbol[qn, fp, name, kind, line, end_line, parent], "
            "not live_ref[qn], "
            "not entry_point[qn]\n"

            "?[fp, name, qn, kind, line, end_line, parent] := "
            "dead[qn, fp, name, kind, line, end_line, parent]"
        )

        result = self._client.run(query)
        return [
            SymbolFact(
                file_path=r[0], name=r[1], qualified_name=r[2],
                kind=r[3], line=r[4], end_line=r[5],
                parent=r[6] if r[6] else None,
            )
            for r in result["rows"]
        ]

    def unreachable_blocks_datalog(self, func_qn: str | None = None) -> list[CfgBlockFact]:
        """Find unreachable CFG blocks via Datalog.

        Replaces find_unreachable_blocks() in cfg.py with a Datalog query
        over the fact graph.
        """
        clauses_filter = ""
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses_filter = ", fq == $fqn"
            params["fqn"] = func_qn

        query = (
            "reachable[fp, fq, bid] := "
            "*cfg_block[fp, fq, bid, is_entry, _], is_entry == true\n"

            "reachable[fp, fq, tb] := "
            "reachable[fp, fq, fb], "
            "*cfg_edge[fp, fq, fb, tb, _, _, _]\n"

            "unreachable[fp, fq, bid] := "
            "*cfg_block[fp, fq, bid, _, is_exit], "
            "is_exit == false, "
            f"not reachable[fp, fq, bid]{clauses_filter}\n"

            "?[fp, fq, bid, ie, ix] := "
            "unreachable[fp, fq, bid], "
            "*cfg_block[fp, fq, bid, ie, ix]"
        )

        result = self._client.run(query, params)
        return [
            CfgBlockFact(
                file_path=r[0], func_qn=r[1], block_id=r[2],
                is_entry=r[3], is_exit=r[4],
            )
            for r in result["rows"]
        ]

    # -- Impact closure (Datalog transitive reverse-caller with edges) ----

    def impact_closure(
        self,
        changed_qns: set[str],
        max_depth: int = 10,
    ) -> dict[str, Any]:
        """Compute the transitive set of impacted symbols from a set of changes.

        Uses a Datalog recursive rule to find all transitive callers of the
        changed symbols, returning both the impacted set and witness edges.

        Returns:
            Dict with keys:
              - ``impacted``: set of qualified names transitively impacted
              - ``edges``: list of (source_qn, caller_qn) witness edges
        """
        if not changed_qns:
            return {"impacted": set(), "edges": []}

        # Build a Datalog rule: seed the "changed" relation, then compute
        # transitive callers.  CozoDB inline relations use the syntax:
        #   changed[x] <- [["val1"], ["val2"]]
        seed_rows = ", ".join(f'["{qn}"]' for qn in changed_qns)

        if max_depth <= 0:
            return {"impacted": set(), "edges": []}

        # Build depth-bounded rules by unrolling the recursion.
        # layer_0 = direct callers of changed; layer_N = callers of layer_(N-1).
        # This avoids unbounded recursion and respects max_depth exactly.
        rules = [f"changed[x] <- [{seed_rows}]\n"]
        rules.append(
            "layer_0[caller] := *call[caller, callee, _, _, _, _, _], changed[callee]\n"
        )
        for i in range(1, max_depth):
            rules.append(
                f"layer_{i}[caller] := *call[caller, mid, _, _, _, _, _], "
                f"layer_{i - 1}[mid]\n"
            )
        # Union all layers into impacted_node
        for i in range(max_depth):
            rules.append(f"impacted_node[x] := layer_{i}[x]\n")
        # Edges: witness pairs — only between impacted nodes and their
        # callees that are either changed or themselves impacted.
        rules.append(
            "edge[caller, callee] := impacted_node[caller], "
            "*call[caller, callee, _, _, _, _, _], changed[callee]\n"
        )
        if max_depth > 1:
            # Inner edges: caller in layer_N calls mid in layer_(N-1)
            for i in range(1, max_depth):
                rules.append(
                    f"edge[caller, mid] := layer_{i}[caller], "
                    f"*call[caller, mid, _, _, _, _, _], layer_{i - 1}[mid]\n"
                )
        rules.append(
            "?[caller, callee] := edge[caller, callee], not changed[caller]"
        )
        query = "".join(rules)
        result = self._client.run(query)
        edges = [(r[0], r[1]) for r in result["rows"]]
        impacted = {r[0] for r in result["rows"]}
        return {"impacted": impacted, "edges": edges}

    # -- Cascade dead code (Datalog negation) -------------------------------

    def cascade_dead(
        self,
        initial_deletes: set[str],
        exclude_entry_points: bool = True,
    ) -> list[SymbolFact]:
        """Find symbols that become dead after deleting *initial_deletes*.

        Uses Datalog with stratified negation:
        1. Mark initial deletes
        2. Find symbols whose *only* references come from the delete set
        3. Transitively add those to the delete set

        This is a fixed-point computation expressed as recursive Datalog
        rules — CozoDB's semi-naive evaluation handles convergence.

        Returns:
            List of SymbolFacts that would become dead (excluding the
            initial deletes themselves).
        """
        if not initial_deletes:
            return []

        # Seed the delete set using CozoDB inline relation syntax
        seed_rows = ", ".join(f'["{qn}"]' for qn in initial_deletes)
        query = (
            f"to_delete[x] <- [{seed_rows}]\n"
            # A symbol has an external caller if called by something NOT
            # in the delete set.
            "has_external_caller[qn] := *call[caller, qn, _, _, _, _, _], "
            "not to_delete[caller]\n"
            # Cascade targets: callees of deleted symbols with no external callers,
            # excluding the initial deletes themselves.
            "callee_of_deleted[qn] := *call[caller, qn, _, _, _, _, _], to_delete[caller]\n"
            "cascade[qn] := callee_of_deleted[qn], "
            "not has_external_caller[qn], "
            "not to_delete[qn]\n"
            # Return full symbol info for cascade targets
            "?[fp, name, qn, kind, line, end_line, parent] := "
            "cascade[qn], "
            "*symbol[qn, fp, name, kind, line, end_line, parent]"
        )
        result = self._client.run(query)
        return [
            SymbolFact(
                file_path=r[0], name=r[1], qualified_name=r[2],
                kind=r[3], line=r[4], end_line=r[5],
                parent=r[6] if r[6] else None,
            )
            for r in result["rows"]
        ]

    # -- Symbols with no external references (parameterised dead code) ------

    def unreferenced_symbols(
        self,
        exclude_qns: set[str] | None = None,
        kinds: set[str] | None = None,
    ) -> list[SymbolFact]:
        """Find symbols with no references, optionally excluding refs from *exclude_qns*.

        This generalises ``dead_code()`` by allowing a set of qualified
        names to be excluded from the reference check — useful for
        "what would become dead if we removed these symbols?"

        Args:
            exclude_qns: If provided, references from these symbols are
                ignored when checking liveness.
            kinds: If provided, only return symbols of these kinds.
        """
        if exclude_qns:
            seed_rows = ", ".join(f'["{qn}"]' for qn in exclude_qns)
            # When excluding callers, we only consider a symbol alive if
            # it has a call-site caller NOT in the excluded set.  We use
            # call facts (which carry the caller QN) rather than bare
            # reference facts (which don't).
            query = (
                f"excluded[x] <- [{seed_rows}]\n"
                "alive[qn] := *call[caller, qn, _, _, _, _, _], not excluded[caller]\n"
                "dead[qn, fp, name, kind, line, end_line, parent] := "
                "*symbol[qn, fp, name, kind, line, end_line, parent], "
                "not alive[qn]\n"
                "?[fp, name, qn, kind, line, end_line, parent] := "
                "dead[qn, fp, name, kind, line, end_line, parent]"
            )
        else:
            query = (
                'has_ref[qn] := *reference[qn, _, _, _, _, _, _]\n'
                'dead[qn, fp, name, kind, line, end_line, parent] := '
                '*symbol[qn, fp, name, kind, line, end_line, parent], '
                'not has_ref[qn]\n'
                '?[fp, name, qn, kind, line, end_line, parent] := '
                'dead[qn, fp, name, kind, line, end_line, parent]'
            )
        result = self._client.run(query)
        facts = [
            SymbolFact(
                file_path=r[0], name=r[1], qualified_name=r[2],
                kind=r[3], line=r[4], end_line=r[5],
                parent=r[6] if r[6] else None,
            )
            for r in result["rows"]
        ]
        if kinds:
            facts = [f for f in facts if f.kind in kinds]
        return facts

    # -- Phase 3: Direct relation queries via Datalog --------------------

    def refs_datalog(
        self,
        symbol_qn: str,
        writes_only: bool = False,
        reads_only: bool = False,
        calls_only: bool = False,
        include_definition: bool = True,
        include_imports: bool = True,
    ) -> list[ReferenceFact]:
        """Find all references to *symbol_qn* via Datalog query.

        Replaces Python file traversal in find_references().
        """
        clauses = ["*reference[sqn, fp, line, col, kind, fq, bid]"]
        clauses.append("sqn == $qn")
        params: dict[str, Any] = {"qn": symbol_qn}

        # Kind filtering
        if writes_only:
            clauses.append('kind == "write"')
        elif reads_only:
            clauses.append('kind == "read"')
        elif calls_only:
            clauses.append('kind == "call"')

        if not include_definition:
            clauses.append('kind != "definition"')
        if not include_imports:
            clauses.append('kind != "import"')

        query = "?[fp, line, col, kind, fq, bid] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            ReferenceFact(
                symbol_qn=symbol_qn, file_path=r[0], line=r[1], col=r[2],
                ref_kind=r[3], func_qn=r[4], block_id=r[5],
            )
            for r in result["rows"]
        ]

    def callers_datalog(self, symbol_qn: str) -> list[CallFact]:
        """Find all callers of *symbol_qn* via Datalog query.

        Replaces Python file traversal in find_callers().
        """
        result = self._client.run(
            "?[caller_qn, callee_qn, fp, line, col, fq, bid] := "
            "*call[caller_qn, callee_qn, fp, line, col, fq, bid], "
            "callee_qn == $qn",
            {"qn": symbol_qn},
        )
        return [
            CallFact(
                caller_qn=r[0], callee_qn=r[1], file_path=r[2],
                line=r[3], col=r[4], func_qn=r[5], block_id=r[6],
            )
            for r in result["rows"]
        ]

    def callees_datalog(self, func_qn: str) -> list[CallFact]:
        """Find all callees of *func_qn* via Datalog query.

        Uses func_qn tag on call facts -- no line-range filtering needed.
        Replaces Python line-range filtering in find_callees().
        """
        result = self._client.run(
            "?[caller_qn, callee_qn, fp, line, col, fq, bid] := "
            "*call[caller_qn, callee_qn, fp, line, col, fq, bid], "
            "fq == $fqn",
            {"fqn": func_qn},
        )
        return [
            CallFact(
                caller_qn=r[0], callee_qn=r[1], file_path=r[2],
                line=r[3], col=r[4], func_qn=r[5], block_id=r[6],
            )
            for r in result["rows"]
        ]

    def graph_datalog(self, file_path: str | None = None) -> list[tuple[str, str]]:
        """Generate call graph edges via Datalog query.

        Returns list of (caller_qn, callee_qn) pairs.
        Replaces Rust collect_callees() in generate_graph().
        """
        if file_path is not None:
            result = self._client.run(
                "?[caller_qn, callee_qn] := "
                "*call[caller_qn, callee_qn, fp, _, _, _, _], "
                "fp == $fp",
                {"fp": file_path},
            )
        else:
            result = self._client.run(
                "?[caller_qn, callee_qn] := "
                "*call[caller_qn, callee_qn, _, _, _, _, _]"
            )
        return [(r[0], r[1]) for r in result["rows"]]

    # -- Phase 5: Taint analysis via Datalog --------------------------------

    def taint_propagation_datalog(
        self,
        sources: list[tuple[str, str, str, int, str]],  # (file_path, func_qn, var_name, block_id, label)
        sinks: list[tuple[str, str, str, int, str]],     # (file_path, func_qn, var_name, block_id, label)
        sanitizers: list[tuple[str, str, str, int, str]] | None = None,  # same format
    ) -> list[TaintFlowFact]:
        """Intraprocedural taint propagation via Datalog over def_use facts.

        Pattern matching (identifying sources/sinks/sanitizers) stays in Python.
        This method handles propagation: given pre-computed source/sink locations,
        it traces taint through def-use chains using Datalog recursion.

        Returns TaintFlowFact entries for each source-to-sink violation found.
        """
        if not sources or not sinks:
            return []

        # Insert source matches as temporary inline relations
        src_rows = ", ".join(
            f'["{fp}", "{fq}", "{var}", {bid}, "{lbl}"]'
            for fp, fq, var, bid, lbl in sources
        )
        sink_rows = ", ".join(
            f'["{fp}", "{fq}", "{var}", {bid}, "{lbl}"]'
            for fp, fq, var, bid, lbl in sinks
        )

        if sanitizers:
            san_rows = ", ".join(
                f'["{fp}", "{fq}", "{var}", {bid}, "{lbl}"]'
                for fp, fq, var, bid, lbl in sanitizers
            )
            sanitizer_rule = f"sanitizer[fp, fq, var, bid, lbl] <- [{san_rows}]\n"
        else:
            # Empty sanitizer relation
            sanitizer_rule = "sanitizer[fp, fq, var, bid, lbl] <- []\n"

        query = (
            f"taint_source[fp, fq, var, bid, lbl] <- [{src_rows}]\n"
            f"taint_sink[fp, fq, var, bid, lbl] <- [{sink_rows}]\n"
            f"{sanitizer_rule}"

            # Taint sources are tainted
            "tainted[fp, fq, var, block, lbl] := "
            "taint_source[fp, fq, var, block, lbl]\n"

            # Propagation through def-use chains (same function)
            "tainted[fp, fq, target, use_block, lbl] := "
            "tainted[fp, fq, source, def_block, lbl], "
            "*def_use[fp, fq, source, def_block, use_block, _, _, _, _], "
            "target = source, "
            "not sanitizer[fp, fq, source, def_block, lbl]\n"

            # Violations: taint reaches sink
            "?[fp, fq, src_var, sink_var, lbl, src_block, sink_block] := "
            "tainted[fp, fq, sink_var, sink_block, lbl], "
            "taint_sink[fp, fq, sink_var, sink_block, lbl], "
            "taint_source[fp, fq, src_var, src_block, lbl]"
        )

        result = self._client.run(query)
        return [
            TaintFlowFact(
                source_var=r[2], sink_var=r[3], label=r[4],
                file_path=r[0], func_qn=r[1],
                source_line=r[5], sink_line=r[6],
            )
            for r in result["rows"]
        ]

    def interprocedural_taint_datalog(
        self,
        max_iterations: int = 10,
    ) -> list[TaintFlowFact]:
        """Interprocedural taint analysis via recursive Datalog.

        Replaces the Python fixed-point loop in run_interprocedural_taint_analysis().
        Uses func_summary and call facts to propagate taint across function boundaries.
        """
        query = (
            # Direct summaries (from intraprocedural analysis)
            "param_flows_to_return[fq, param] := "
            "*func_summary[fq, param, ftr, _, _], ftr == true\n"

            # Direct summaries: param flows to sink
            "param_flows_to_sink[fq, param, lbl] := "
            "*func_summary[fq, param, _, fts, lbl], fts == true\n"

            # Transitive: if callee's param flows to return, propagate through call
            "param_flows_to_return[caller_fq, caller_param] := "
            "*call[caller_fq, callee_fq, _, _, _, _, _], "
            "param_flows_to_return[callee_fq, callee_param], "
            "*def_use[_, caller_fq, caller_param, _, _, _, _, _, _]\n"

            # Violations: tainted param flows to sink through call chain
            "violation[caller_fq, callee_fq, param, lbl] := "
            "*call[caller_fq, callee_fq, fp, line, _, _, _], "
            "param_flows_to_sink[callee_fq, param, lbl]\n"

            "?[caller_fq, callee_fq, param, lbl] := "
            "violation[caller_fq, callee_fq, param, lbl]"
        )

        result = self._client.run(query)
        return [
            TaintFlowFact(
                source_var=r[2], sink_var=r[2], label=r[3],
                file_path="", func_qn=r[0],
                source_line=0, sink_line=0,
            )
            for r in result["rows"]
        ]

    def flow_rule_check_datalog(
        self,
        sources: list[tuple[str, str, str, int]],  # (file_path, func_qn, var_name, block_id)
        sinks: list[tuple[str, str, str, int]],     # same format
        through: list[tuple[str, str, str, int]] | None = None,  # must-pass-through points
        not_through: list[tuple[str, str, str, int]] | None = None,  # must-not-pass-through points
    ) -> list[tuple[str, str, str, str]]:
        """Check flow-based lint rules via Datalog.

        Replaces _check_flow_rule() in lint.py for flows-from/flows-to/not-through rules.
        Returns list of (file_path, func_qn, source_var, sink_var) violations.
        """
        if not sources or not sinks:
            return []

        src_rows = ", ".join(
            f'["{fp}", "{fq}", "{var}", {bid}]'
            for fp, fq, var, bid in sources
        )
        sink_rows = ", ".join(
            f'["{fp}", "{fq}", "{var}", {bid}]'
            for fp, fq, var, bid in sinks
        )

        if not_through:
            nt_rows = ", ".join(
                f'["{fp}", "{fq}", "{var}", {bid}]'
                for fp, fq, var, bid in not_through
            )
            not_through_rule = f"blocked[fp, fq, var, bid] <- [{nt_rows}]\n"
        else:
            not_through_rule = "blocked[fp, fq, var, bid] <- []\n"

        query = (
            f"flow_source[fp, fq, var, bid] <- [{src_rows}]\n"
            f"flow_sink[fp, fq, var, bid] <- [{sink_rows}]\n"
            f"{not_through_rule}"

            "reaches[fp, fq, var, block] := "
            "flow_source[fp, fq, var, block]\n"

            "reaches[fp, fq, target, use_block] := "
            "reaches[fp, fq, source, def_block], "
            "*def_use[fp, fq, source, def_block, use_block, _, _, _, _], "
            "target = source, "
            "not blocked[fp, fq, source, def_block]\n"

            "?[fp, fq, src_var, sink_var] := "
            "reaches[fp, fq, sink_var, sink_block], "
            "flow_sink[fp, fq, sink_var, sink_block], "
            "flow_source[fp, fq, src_var, _]"
        )

        result = self._client.run(query)
        return [(r[0], r[1], r[2], r[3]) for r in result["rows"]]

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
        for fact in self._all_cfg_edges():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_def_uses():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_cfg_blocks():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_decorator_on():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_source_locs():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_func_summaries():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_entry_point_decorators():
            if predicate(fact):
                results.append(fact)
        for fact in self._all_entry_point_names():
            if predicate(fact):
                results.append(fact)
        return results

    def _all_calls(self) -> list[CallFact]:
        result = self._client.run(
            "?[caller, callee, fp, line, col, fq, bid] := *call[caller, callee, fp, line, col, fq, bid]"
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3],
                     col=r[4], func_qn=r[5], block_id=r[6])
            for r in result["rows"]
        ]

    def _all_references(self) -> list[ReferenceFact]:
        result = self._client.run(
            "?[qn, fp, line, col, kind, fq, bid] := *reference[qn, fp, line, col, kind, fq, bid]"
        )
        return [
            ReferenceFact(symbol_qn=r[0], file_path=r[1], line=r[2], col=r[3],
                          ref_kind=r[4], func_qn=r[5], block_id=r[6])
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

    def _all_cfg_edges(self) -> list[CfgEdgeFact]:
        result = self._client.run(
            "?[fp, fq, fb, tb, ek, fl, tl] := *cfg_edge[fp, fq, fb, tb, ek, fl, tl]"
        )
        return [
            CfgEdgeFact(
                file_path=r[0], func_qn=r[1], from_block=r[2],
                to_block=r[3], edge_kind=r[4], from_line=r[5], to_line=r[6],
            )
            for r in result["rows"]
        ]

    def _all_def_uses(self) -> list[DefUseFact]:
        result = self._client.run(
            "?[fp, fq, vn, db, ub, dl, dc, ul, uc] := *def_use[fp, fq, vn, db, ub, dl, dc, ul, uc]"
        )
        return [
            DefUseFact(
                file_path=r[0], func_qn=r[1], var_name=r[2],
                def_block=r[3], use_block=r[4],
                def_line=r[5], def_col=r[6], use_line=r[7], use_col=r[8],
            )
            for r in result["rows"]
        ]

    def _all_cfg_blocks(self) -> list[CfgBlockFact]:
        result = self._client.run(
            "?[fp, fq, bid, ie, ix] := *cfg_block[fp, fq, bid, ie, ix]"
        )
        return [
            CfgBlockFact(file_path=r[0], func_qn=r[1], block_id=r[2],
                         is_entry=r[3], is_exit=r[4])
            for r in result["rows"]
        ]

    def _all_decorator_on(self) -> list[DecoratorOnFact]:
        result = self._client.run(
            "?[sqn, dec] := *decorator_on[sqn, dec]"
        )
        return [DecoratorOnFact(symbol_qn=r[0], decorator=r[1]) for r in result["rows"]]

    def _all_source_locs(self) -> list[SourceLocFact]:
        result = self._client.run(
            "?[fp, lk, lid, line, col, el, rl] := *source_loc[fp, lk, lid, line, col, el, rl]"
        )
        return [
            SourceLocFact(file_path=r[0], loc_kind=r[1], loc_id=r[2],
                         line=r[3], col=r[4], end_line=r[5], rel_line=r[6])
            for r in result["rows"]
        ]

    def _all_func_summaries(self) -> list[FuncSummaryFact]:
        result = self._client.run(
            "?[fq, pn, ftr, fts, sl] := *func_summary[fq, pn, ftr, fts, sl]"
        )
        return [
            FuncSummaryFact(func_qn=r[0], param_name=r[1], flows_to_return=r[2],
                           flows_to_sink=r[3], sink_label=r[4])
            for r in result["rows"]
        ]

    def _all_entry_point_decorators(self) -> list[EntryPointDecoratorFact]:
        result = self._client.run(
            "?[dec] := *entry_point_decorator[dec]"
        )
        return [EntryPointDecoratorFact(decorator=r[0]) for r in result["rows"]]

    def _all_entry_point_names(self) -> list[EntryPointNameFact]:
        result = self._client.run(
            "?[name] := *entry_point_name[name]"
        )
        return [EntryPointNameFact(name=r[0]) for r in result["rows"]]

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
        for fact in self._all_cfg_edges():
            data.append(_tag(fact))
        for fact in self._all_def_uses():
            data.append(_tag(fact))
        for fact in self._all_cfg_blocks():
            data.append(_tag(fact))
        for fact in self._all_decorator_on():
            data.append(_tag(fact))
        for fact in self._all_source_locs():
            data.append(_tag(fact))
        for fact in self._all_func_summaries():
            data.append(_tag(fact))
        for fact in self._all_entry_point_decorators():
            data.append(_tag(fact))
        for fact in self._all_entry_point_names():
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
            "CfgEdgeFact": (CfgEdgeFact, graph.add_cfg_edge),
            "DefUseFact": (DefUseFact, graph.add_def_use),
            "CfgBlockFact": (CfgBlockFact, graph.add_cfg_block),
            "DecoratorOnFact": (DecoratorOnFact, graph.add_decorator_on),
            "SourceLocFact": (SourceLocFact, graph.add_source_loc),
            "FuncSummaryFact": (FuncSummaryFact, graph.add_func_summary),
            "EntryPointDecoratorFact": (EntryPointDecoratorFact, graph.add_entry_point_decorator),
            "EntryPointNameFact": (EntryPointNameFact, graph.add_entry_point_name),
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

        # The scope resolver may fail if pointed at a repo root with
        # incompatible config.  Use the user-supplied project_path as
        # the resolver root (typically ``src/pkg``), falling back to
        # the detected project_root.
        resolver_root = str(Path(project_path).resolve())

        for abs_file_path in source_files:
            try:
                content = Path(abs_file_path).read_text(encoding="utf-8")
            except Exception:
                logger.debug("Could not read %s", abs_file_path, exc_info=True)
                continue

            # Store relative paths in the fact graph so they match
            # selector paths regardless of working directory.
            try:
                rel_path = str(Path(abs_file_path).relative_to(Path(project_root).resolve()))
            except ValueError:
                rel_path = abs_file_path

            module_name = _file_to_module(abs_file_path, project_root)

            # -- Symbol facts (via Rust symbol collection) ----------------
            try:
                ext = Path(abs_file_path).suffix.lstrip(".") or "py"
                raw_symbols = _rust.collect_symbols_from_str(content, ext=ext)
            except Exception:
                logger.debug("Could not parse %s for symbols", abs_file_path, exc_info=True)
                raw_symbols = []

            sym_facts: list[SymbolFact] = []
            dec_facts: list[DecoratorOnFact] = []
            _walk_symbols(sym_facts, dec_facts, raw_symbols, rel_path, module_name, parent_qn=None)
            graph.add_symbols_batch(sym_facts)
            graph.add_decorator_on_batch(dec_facts)

            # -- Source location facts for symbols -----------------------
            source_loc_facts: list[SourceLocFact] = []
            for sf in sym_facts:
                source_loc_facts.append(SourceLocFact(
                    file_path=sf.file_path,
                    loc_kind="symbol",
                    loc_id=sf.qualified_name,
                    line=sf.line,
                    end_line=sf.end_line,
                ))
            graph.add_source_locs_batch(source_loc_facts)

            # -- CFG blocks and edges (via Rust CFG builder) -------------
            try:
                from emend.cfg import build_cfgs_for_source
                ext = Path(abs_file_path).suffix.lstrip(".") or "py"
                cfgs = build_cfgs_for_source(content, ext=ext)
            except Exception:
                logger.debug("Could not build CFGs for %s", abs_file_path, exc_info=True)
                cfgs = []

            # Build block line ranges for block-tagging references
            block_ranges: list[tuple[str, int, int, int]] = []  # (func_qn, block_id, start_line, end_line)
            cfg_block_facts: list[CfgBlockFact] = []
            cfg_edge_facts: list[CfgEdgeFact] = []

            for cfg in cfgs:
                func_name = cfg.func_name
                # Find the matching symbol QN
                func_qn = ""
                for sf in sym_facts:
                    if sf.name == func_name and sf.file_path == rel_path:
                        func_qn = sf.qualified_name
                        break
                if not func_qn:
                    func_qn = f"{module_name}.{func_name}"

                for block in cfg.get_blocks():
                    bid = block["id"]
                    cfg_block_facts.append(CfgBlockFact(
                        file_path=rel_path,
                        func_qn=func_qn,
                        block_id=bid,
                        is_entry=(bid == cfg.entry),
                        is_exit=(bid == cfg.exit),
                    ))
                    block_ranges.append((func_qn, bid, block["start_line"], block["end_line"]))

                for edge in cfg.get_edges():
                    cfg_edge_facts.append(CfgEdgeFact(
                        file_path=rel_path,
                        func_qn=func_qn,
                        from_block=edge["from"],
                        to_block=edge["to"],
                        edge_kind=edge["kind"],
                        from_line=0,
                        to_line=0,
                    ))

            graph.add_cfg_blocks_batch(cfg_block_facts)
            graph.add_cfg_edges_batch(cfg_edge_facts)

            # Sort block_ranges for lookup: innermost (smallest range) first
            block_ranges.sort(key=lambda x: (x[2], -(x[3] - x[2])))

            # -- Reference and call facts (via scope resolver) ------------
            try:
                ext = Path(abs_file_path).suffix.lstrip(".") or "py"
                resolver = _rust.PyScopeResolver(resolver_root, ext)
                resolver.index_file(abs_file_path, content)
            except Exception:
                logger.debug(
                    "Could not build scope resolver for %s", abs_file_path, exc_info=True
                )
                resolver = None

            if resolver is not None:
                symbol_ranges = _build_symbol_line_index(sym_facts, rel_path)

                try:
                    refs = resolver.references_in_file(abs_file_path)
                except Exception:
                    logger.debug(
                        "references_in_file failed for %s", abs_file_path, exc_info=True
                    )
                    refs = []

                ref_facts: list[ReferenceFact] = []
                call_facts: list[CallFact] = []
                for qn, line, col, _offset, _end_offset, kind in refs:
                    ref_kind = _map_ref_kind(kind)
                    fq, bid = _find_containing_block(block_ranges, line)
                    ref_facts.append(ReferenceFact(
                        symbol_qn=qn, file_path=rel_path,
                        line=line, col=col, ref_kind=ref_kind,
                        func_qn=fq, block_id=bid,
                    ))

                    if ref_kind == "call":
                        caller = _enclosing_symbol(symbol_ranges, line)
                        if caller is not None:
                            call_facts.append(CallFact(
                                caller_qn=caller, callee_qn=qn,
                                file_path=rel_path, line=line, col=col,
                                func_qn=fq, block_id=bid,
                            ))

                graph.add_references_batch(ref_facts)
                graph.add_calls_batch(call_facts)

            # -- Def-use facts with block IDs ----------------------------
            def_use_facts: list[DefUseFact] = []
            for cfg in cfgs:
                func_name = cfg.func_name
                func_qn = ""
                for sf in sym_facts:
                    if sf.name == func_name and sf.file_path == rel_path:
                        func_qn = sf.qualified_name
                        break
                if not func_qn:
                    func_qn = f"{module_name}.{func_name}"

                # Build def map: var_name -> [(block_id, line, col)]
                defs_map: dict[str, list[tuple[int, int, int]]] = {}
                for block in cfg.get_blocks():
                    bid = block["id"]
                    for d in block.get("defs", []) or []:
                        var_name = d[0] if isinstance(d, (list, tuple)) else d
                        dline = d[1] if isinstance(d, (list, tuple)) and len(d) > 1 else 0
                        dcol = d[2] if isinstance(d, (list, tuple)) and len(d) > 2 else 0
                        defs_map.setdefault(var_name, []).append((bid, dline, dcol))

                # Build use map and create def-use pairs
                for block in cfg.get_blocks():
                    bid = block["id"]
                    for u in block.get("uses", []) or []:
                        var_name = u[0] if isinstance(u, (list, tuple)) else u
                        uline = u[1] if isinstance(u, (list, tuple)) and len(u) > 1 else 0
                        ucol = u[2] if isinstance(u, (list, tuple)) and len(u) > 2 else 0
                        if var_name in defs_map:
                            for def_bid, dl, dc in defs_map[var_name]:
                                def_use_facts.append(DefUseFact(
                                    file_path=rel_path,
                                    func_qn=func_qn,
                                    var_name=var_name,
                                    def_block=def_bid,
                                    use_block=bid,
                                    def_line=dl,
                                    def_col=dc,
                                    use_line=uline,
                                    use_col=ucol,
                                ))

            graph.add_def_uses_batch(def_use_facts)

            # -- Import facts (via stdlib ast) ----------------------------
            import_facts = _extract_imports(rel_path, content)
            graph.add_imports_batch(import_facts)

        return graph


# ---------------------------------------------------------------------------
# Internal helpers for build_from_project
# ---------------------------------------------------------------------------

def _walk_symbols(
    out: list[SymbolFact],
    dec_out: list[DecoratorOnFact],
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

        # Extract decorators
        for dec_name in (d.get("decorators", []) or []):
            dec_out.append(DecoratorOnFact(symbol_qn=qn, decorator=dec_name))

        children = d.get("children", [])
        if children:
            _walk_symbols(out, dec_out, children, file_path, module_name, parent_qn=qn)


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


def _find_containing_block(
    block_ranges: list[tuple[str, int, int, int]],
    line: int,
) -> tuple[str, int]:
    """Find the (func_qn, block_id) containing a given line.

    Returns ("", -1) for module-level code.
    """
    best_func_qn = ""
    best_block_id = -1
    best_span = float("inf")

    for func_qn, block_id, start_line, end_line in block_ranges:
        if start_line <= line <= end_line:
            span = end_line - start_line
            if span < best_span:
                best_span = span
                best_func_qn = func_qn
                best_block_id = block_id

    return best_func_qn, best_block_id


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


def symbol_has_type(type_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching TypeFacts whose type_str matches *type_pattern*."""
    compiled = re.compile(type_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TypeFact) and compiled.search(fact.type_str) is not None

    return _predicate
