"""Relational fact model for code invariants, backed by CozoDB.

Provides a unified, queryable graph of code facts (symbols, calls,
references, trace flows, types, imports) extracted from a project's
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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fact types (stable dataclass API)
# ---------------------------------------------------------------------------


@dataclass
class TraceDatalogConfig:
    """Grouped parameters for :meth:`FactGraph.trace_propagation_datalog`.

    Collects sources, sinks, and analysis options into a single config
    object to reduce the method's parameter count.
    """
    sources: list[tuple[str, str, str, int, str]]  # (file_path, func_qn, var_name, block_id, label)
    sinks: list[tuple[str, str, str, int, str]] = field(default_factory=list)
    effect_sinks: list[tuple[str, str]] = field(default_factory=list)  # (label, effect_kind)
    sanitizers: list[tuple[str, str, str, int, str]] = field(default_factory=list)
    sanitizer_quantifier: str = "all_paths"  # "all_paths" or "some_path"
    sanitizer_lines: list[tuple[str, str, str, int, int]] = field(default_factory=list)
    sink_lines: list[tuple[str, str, str, int, int]] = field(default_factory=list)
    scope_kills: list[tuple[str, str, str, int]] = field(default_factory=list)
    scalar_types: list[str] = field(default_factory=list)

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
class TraceFlowFact:
    """A trace flow edge from source to sink within a function."""
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
    kind: str = "write"  # "read", "write", "aug_write", "del"
    def_block: int = 0
    use_block: int = 0
    def_line: int = 0    # kept for backwards compat display
    def_col: int = 0
    use_line: int = 0
    use_col: int = 0


@dataclass(frozen=True)
class MethodCallFact:
    """A method call on a receiver object (e.g. obj.append())."""
    file_path: str
    func_qn: str
    receiver: str
    method: str
    block_id: int = 0
    line: int = 0


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
    SymbolFact, CallFact, ReferenceFact, TraceFlowFact, TypeFact,
    ImportFact, CfgEdgeFact, DefUseFact, MethodCallFact, CfgBlockFact,
    DecoratorOnFact, SourceLocFact, FuncSummaryFact,
    EntryPointDecoratorFact, EntryPointNameFact,
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

{:create call_by_callee {
    callee_qn: String,
    caller_qn: String,
    file_path: String,
    line: Int,
    col: Int
    =>
    func_qn: String default "",
    block_id: Int default -1
}}

{:create call_by_file {
    file_path: String,
    caller_qn: String,
    callee_qn: String,
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

{:create trace_flow {
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
    kind: String default "write",
    def_block: Int,
    use_block: Int,
    def_line: Int default 0,
    use_line: Int default 0
    =>
    def_col: Int default 0,
    use_col: Int default 0
}}

{:create method_call {
    file_path: String,
    func_qn: String,
    receiver: String,
    method: String,
    block_id: Int,
    line: Int
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

{:create entry_point_prefix { prefix: String }}

{:create exported_symbol { qualified_name: String }}

{:create ref_by_block {
    file_path: String,
    func_qn: String,
    block_id: Int,
    symbol_qn: String
}}

{:create reachable_block {
    file_path: String,
    func_qn: String,
    block_id: Int
}}

{:create module_level_ref {
    symbol_qn: String,
    file_path: String,
    line: Int
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
        params = {
            "caller": fact.caller_qn,
            "callee": fact.callee_qn,
            "fp": fact.file_path,
            "line": fact.line,
            "col": fact.col,
            "fq": fact.func_qn,
            "bid": fact.block_id,
        }
        self._client.run(
            "?[caller_qn, callee_qn, file_path, line, col, func_qn, block_id] <- "
            "[[$caller, $callee, $fp, $line, $col, $fq, $bid]] "
            ":put call {caller_qn, callee_qn, file_path, line, col => func_qn, block_id}",
            params,
        )
        self._client.run(
            "?[callee_qn, caller_qn, file_path, line, col, func_qn, block_id] <- "
            "[[$callee, $caller, $fp, $line, $col, $fq, $bid]] "
            ":put call_by_callee {callee_qn, caller_qn, file_path, line, col => func_qn, block_id}",
            params,
        )
        self._client.run(
            "?[file_path, caller_qn, callee_qn, line, col, func_qn, block_id] <- "
            "[[$fp, $caller, $callee, $line, $col, $fq, $bid]] "
            ":put call_by_file {file_path, caller_qn, callee_qn, line, col => func_qn, block_id}",
            params,
        )

    def add_reference(self, fact: ReferenceFact) -> None:
        """Add a reference fact."""
        params = {
            "qn": fact.symbol_qn,
            "fp": fact.file_path,
            "line": fact.line,
            "col": fact.col,
            "kind": fact.ref_kind,
            "fq": fact.func_qn,
            "bid": fact.block_id,
        }
        self._client.run(
            "?[symbol_qn, file_path, line, col, ref_kind, func_qn, block_id] <- "
            "[[$qn, $fp, $line, $col, $kind, $fq, $bid]] "
            ":put reference {symbol_qn, file_path, line, col => ref_kind, func_qn, block_id}",
            params,
        )
        if fact.func_qn == "" and fact.block_id == -1:
            self._client.run(
                "?[symbol_qn, file_path, line] <- [[$qn, $fp, $line]] "
                ":put module_level_ref {symbol_qn, file_path, line}",
                params,
            )

    def add_trace_flow(self, fact: TraceFlowFact) -> None:
        """Add a taint flow fact."""
        self._client.run(
            "?[source_var, sink_var, label, file_path, func_qn, source_line, sink_line] <- "
            "[[$sv, $skv, $lbl, $fp, $fq, $sl, $skl]] "
            ":put trace_flow {source_var, sink_var, label, file_path, func_qn, source_line, sink_line}",
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

    def add_types_batch(self, facts: list[TypeFact]) -> None:
        """Bulk-insert type binding facts."""
        if not facts:
            return
        rows = [
            [f.symbol_qn, f.file_path, f.line, f.binding_kind, f.type_str]
            for f in facts
        ]
        self._client.run(
            "?[symbol_qn, file_path, line, binding_kind, type_str] <- $rows "
            ":put type_binding {symbol_qn, file_path, line, binding_kind => type_str}",
            {"rows": rows},
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
            "?[file_path, func_qn, var_name, kind, def_block, use_block, def_line, def_col, use_line, use_col] <- "
            "[[$fp, $fq, $vn, $k, $db, $ub, $dl, $dc, $ul, $uc]] "
            ":put def_use {file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line => def_col, use_col}",
            {
                "fp": fact.file_path,
                "fq": fact.func_qn,
                "vn": fact.var_name,
                "k": fact.kind,
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
        reverse_rows = [[f.callee_qn, f.caller_qn, f.file_path, f.line, f.col, f.func_qn, f.block_id] for f in facts]
        file_rows = [[f.file_path, f.caller_qn, f.callee_qn, f.line, f.col, f.func_qn, f.block_id] for f in facts]
        self._client.run(
            "?[caller_qn, callee_qn, file_path, line, col, func_qn, block_id] <- $rows "
            ":put call {caller_qn, callee_qn, file_path, line, col => func_qn, block_id}",
            {"rows": rows},
        )
        self._client.run(
            "?[callee_qn, caller_qn, file_path, line, col, func_qn, block_id] <- $rows "
            ":put call_by_callee {callee_qn, caller_qn, file_path, line, col => func_qn, block_id}",
            {"rows": reverse_rows},
        )
        self._client.run(
            "?[file_path, caller_qn, callee_qn, line, col, func_qn, block_id] <- $rows "
            ":put call_by_file {file_path, caller_qn, callee_qn, line, col => func_qn, block_id}",
            {"rows": file_rows},
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
        module_rows = [[f.symbol_qn, f.file_path, f.line] for f in facts if f.func_qn == "" and f.block_id == -1]
        if module_rows:
            self._client.run(
                "?[symbol_qn, file_path, line] <- $rows "
                ":put module_level_ref {symbol_qn, file_path, line}",
                {"rows": module_rows},
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
            [f.file_path, f.func_qn, f.var_name, f.kind, f.def_block, f.use_block, f.def_line, f.def_col, f.use_line, f.use_col]
            for f in facts
        ]
        self._client.run(
            "?[file_path, func_qn, var_name, kind, def_block, use_block, def_line, def_col, use_line, use_col] <- $rows "
            ":put def_use {file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line => def_col, use_col}",
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

    def add_method_call(self, fact: MethodCallFact) -> None:
        """Add a method call fact."""
        self._client.run(
            "?[file_path, func_qn, receiver, method, block_id, line] <- "
            "[[$fp, $fq, $rcv, $meth, $bid, $ln]] "
            ":put method_call {file_path, func_qn, receiver, method, block_id, line}",
            {
                "fp": fact.file_path, "fq": fact.func_qn,
                "rcv": fact.receiver, "meth": fact.method,
                "bid": fact.block_id, "ln": fact.line,
            },
        )

    def add_method_calls_batch(self, facts: list[MethodCallFact]) -> None:
        """Bulk-insert method call facts."""
        if not facts:
            return
        rows = [[f.file_path, f.func_qn, f.receiver, f.method, f.block_id, f.line] for f in facts]
        self._client.run(
            "?[file_path, func_qn, receiver, method, block_id, line] <- $rows "
            ":put method_call {file_path, func_qn, receiver, method, block_id, line}",
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

    def add_exported_symbols_batch(self, qns: list[str]) -> None:
        """Bulk-insert exported symbol qualified names."""
        if not qns:
            return
        rows = [[qn] for qn in qns]
        self._client.run(
            "?[qualified_name] <- $rows "
            ":put exported_symbol {qualified_name}",
            {"rows": rows},
        )

    # -- Post-processing ---------------------------------------------------

    def _resolve_builtin_refs(
        self,
        file_to_module_fn: Callable[[str], str],
    ) -> None:
        """Resolve ``builtins.*`` references using import facts.

        When a scope resolver can't resolve a cross-file import (typical
        for TypeScript/Rust), it reports the callee as ``builtins.X``.
        This method finds such references, matches them against import
        facts, and adds corrected reference/call facts with the real
        qualified name.

        ``file_to_module_fn`` maps a relative file path to its module
        name (language-specific, e.g. ``target`` or ``utils.helper``).
        """
        # 1. Find all call facts with builtins.* callee
        try:
            builtin_calls = self._client.run(
                "?[caller_qn, callee_qn, file_path, line, col, func_qn, block_id] := "
                "*call[caller_qn, callee_qn, file_path, line, col, func_qn, block_id], "
                "starts_with(callee_qn, 'builtins.')"
            )["rows"]
        except Exception:
            return

        if not builtin_calls:
            return

        # 2. Build import map: (file_path, name) -> imported_module
        try:
            imports = self._client.run(
                "?[importing_file, imported_name, imported_module] := "
                "*import[importing_file, imported_module, imported_name, _, _], "
                "imported_name != ''"
            )["rows"]
        except Exception:
            return

        import_map: dict[tuple[str, str], str] = {}
        for imp_file, imp_name, imp_module in imports:
            import_map[(imp_file, imp_name)] = imp_module

        # 3. Build file→module QN map for all known files
        try:
            file_modules = self._client.run(
                "?[fp] := *symbol[_, fp, _, _, _, _, _]"
            )["rows"]
        except Exception:
            file_modules = []

        module_qn_cache: dict[str, str] = {}
        for (fp,) in file_modules:
            if fp not in module_qn_cache:
                try:
                    module_qn_cache[fp] = _normalize_qn(file_to_module_fn(fp))
                except Exception:
                    pass

        # 4. Resolve and add corrected facts
        new_calls: list[CallFact] = []
        new_refs: list[ReferenceFact] = []

        for caller_qn, callee_qn, file_path, line, col, func_qn, block_id in builtin_calls:
            bare_name = callee_qn.split(".", 1)[-1] if "." in callee_qn else callee_qn
            imp_module = import_map.get((file_path, bare_name))
            if not imp_module:
                continue

            # Resolve import module path to a file module QN.
            # Handle relative imports (./target, ../utils) by normalizing
            # against the importing file's directory.
            resolved_qn = self._resolve_import_to_qn(
                imp_module, file_path, bare_name, module_qn_cache,
            )
            if not resolved_qn:
                continue

            new_calls.append(CallFact(
                caller_qn=caller_qn, callee_qn=resolved_qn,
                file_path=file_path, line=line, col=col,
                func_qn=func_qn, block_id=block_id,
            ))
            new_refs.append(ReferenceFact(
                symbol_qn=resolved_qn, file_path=file_path,
                line=line, col=col, ref_kind="call",
                func_qn=func_qn, block_id=block_id,
            ))

        if new_calls:
            self.add_calls_batch(new_calls)
        if new_refs:
            self.add_references_batch(new_refs)

    def _resolve_import_to_qn(
        self,
        import_source: str,
        importing_file: str,
        symbol_name: str,
        module_qn_cache: dict[str, str],
    ) -> str | None:
        """Resolve an import source path to a symbol QN.

        For relative imports like ``./target`` or ``../utils/helper``,
        resolves relative to the importing file's directory.
        """
        import posixpath

        # Strip leading ./ and resolve relative paths
        source = import_source
        if source.startswith("./") or source.startswith("../"):
            # Resolve relative to the importing file's directory
            imp_dir = posixpath.dirname(importing_file)
            source = posixpath.normpath(posixpath.join(imp_dir, source))

        # Normalize separators to dots
        normalized = _normalize_qn(source)

        # Try to match against known module QNs
        for _fp, mod_qn in module_qn_cache.items():
            if mod_qn == normalized:
                return f"{mod_qn}.{symbol_name}"

        # Fallback: construct the QN directly
        return f"{normalized}.{symbol_name}"

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
            "caller = $qn, *call[$qn, callee, fp, line, col, fq, bid]",
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
            "callee = $qn, *call_by_callee[$qn, caller, fp, line, col, fq, bid]",
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
            "qn = $qn, *reference[$qn, fp, line, col, kind, fq, bid]",
            {"qn": symbol_qn},
        )
        return [
            ReferenceFact(symbol_qn=r[0], file_path=r[1], line=r[2], col=r[3],
                          ref_kind=r[4], func_qn=r[5], block_id=r[6])
            for r in result["rows"]
        ]

    def trace_flows(
        self,
        label: str | None = None,
        file_path: str | None = None,
    ) -> list[TraceFlowFact]:
        """Query taint flow facts with optional filters."""
        clauses = ["*trace_flow[sv, skv, lbl, fp, fq, sl, skl]"]
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
            TraceFlowFact(
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
        clauses = ["*def_use[fp, fq, vn, k, db, ub, dl, ul, dc, uc]"]
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
        query = "?[fp, fq, vn, k, db, ub, dl, dc, ul, uc] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            DefUseFact(
                file_path=r[0], func_qn=r[1], var_name=r[2],
                kind=r[3], def_block=r[4], use_block=r[5],
                def_line=r[6], def_col=r[7], use_line=r[8], use_col=r[9],
            )
            for r in result["rows"]
        ]

    def method_calls(
        self,
        func_qn: str | None = None,
        file_path: str | None = None,
    ) -> list[MethodCallFact]:
        """Query method call facts with optional filters."""
        clauses = ["*method_call[fp, fq, rcv, meth, bid, ln]"]
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses.append("fq == $func_qn")
            params["func_qn"] = func_qn
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path
        query = "?[fp, fq, rcv, meth, bid, ln] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            MethodCallFact(
                file_path=r[0], func_qn=r[1], receiver=r[2],
                method=r[3], block_id=r[4], line=r[5],
            )
            for r in result["rows"]
        ]

    def method_call_types(
        self,
        file_path: str | None = None,
        func_qn: str | None = None,
    ) -> list[tuple[str, str, str, str, str]]:
        """Resolve receiver types for method calls via type_binding join.

        Returns (file_path, func_qn, receiver, method, receiver_type) tuples
        for method calls where the receiver has a known type binding.
        """
        clauses = [
            "*method_call[fp, fq, rcv, meth, bid, ln]",
            "*type_binding[_, fp, def_line, _, type_str]",
            "*def_use[fp, fq, rcv, _, _, bid, def_line, _, _, _]",
        ]
        params: dict[str, Any] = {}
        if file_path is not None:
            clauses.append("fp == $fp")
            params["fp"] = file_path
        if func_qn is not None:
            clauses.append("fq == $fq")
            params["fq"] = func_qn
        query = "?[fp, fq, rcv, meth, type_str] := " + ", ".join(clauses)
        try:
            result = self._client.run(query, params)
            return [(r[0], r[1], r[2], r[3], r[4]) for r in result["rows"]]
        except Exception:
            return []

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

    def resolve_location(self, file_path: str, line: int) -> tuple[str, int]:
        """Resolve a line number to ``(func_qn, block_id)`` using stored facts.

        Uses symbol facts for function ranges and source_loc/cfg_block facts
        for block ranges.

        Returns ``(MODULE_LEVEL_FUNC, MODULE_LEVEL_BLOCK)`` for module-level
        code (i.e. when the line does not fall inside any known function).
        """
        from emend.location_resolver import MODULE_LEVEL_BLOCK, MODULE_LEVEL_FUNC, LocationResolver

        resolver = LocationResolver.from_fact_graph(self, file_path=file_path)
        loc = resolver.resolve(file_path, line)
        return loc.func_qn, loc.block_id

    # -- Transitive closures (Datalog! No more Python BFS) ---------------

    def transitive_callers(self, symbol_qn: str) -> set[str]:
        """Compute the transitive set of callers of *symbol_qn* via Datalog."""
        result = self._client.run(
            "reaches[a] := *call_by_callee[$qn, a, _, _, _, _, _]\n"
            "reaches[a] := reaches[mid], *call_by_callee[mid, a, _, _, _, _, _]\n"
            "?[a] := reaches[a]",
            {"qn": symbol_qn},
        )
        return {r[0] for r in result["rows"]} - {symbol_qn}

    def transitive_callees(self, symbol_qn: str) -> set[str]:
        """Compute the transitive set of callees of *symbol_qn* via Datalog."""
        result = self._client.run(
            "reaches[b] := *call[$qn, b, _, _, _, _, _]\n"
            "reaches[b] := reaches[mid], *call[mid, b, _, _, _, _, _]\n"
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
        exclude_reference_paths: list[str] | None = None,
        exclude_reference_segments: list[str] | None = None,
        entry_point_prefixes: list[str] | None = None,
    ) -> tuple[list[SymbolFact], list[CfgBlockFact]]:
        """Unified dead code detection via Datalog.

        Combines unreachable-block analysis with unreferenced-symbol detection
        in a single Datalog program:

        1. Computes reachable blocks via transitive closure from CFG entry blocks
        2. Only counts references from reachable code as "live"
        3. Applies entry point heuristics (dunders, test_, decorators) as Datalog rules
        4. Returns a tuple of (dead symbols, unreachable blocks).

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
        if entry_point_prefixes:
            pfx_rows = ", ".join(f'["{p}"]' for p in entry_point_prefixes)
            setup_rules.append(f"?[prefix] <- [{pfx_rows}] :put entry_point_prefix {{prefix}}")

        # Run setup rules if any
        for rule in setup_rules:
            try:
                self._client.run(rule)
            except Exception:
                pass

        # Build excluded-path filter clauses for CozoDB.
        # We need two variants: one using the variable "fp" (for the
        # ref_by_block rule) and one using "ref_fp" (for the module_level_ref
        # rule).  Building them separately avoids the brittle
        # `.replace("fp", "ref_fp")` which would mangle path strings that
        # happen to contain the substring "fp".
        excl_clauses = ""
        excl_clauses_ref = ""
        excl_parts: list[str] = []
        excl_parts_ref: list[str] = []
        if exclude_reference_paths:
            for ep in exclude_reference_paths:
                excl_parts.append(f'not starts_with(fp, "{ep}")')
                excl_parts_ref.append(f'not starts_with(ref_fp, "{ep}")')
        if exclude_reference_segments:
            for seg in exclude_reference_segments:
                # Match paths containing this directory segment
                excl_parts.append(f'not str_includes(fp, "{seg}/")')
                excl_parts.append(f'not str_includes(fp, "{seg}\\\\")')
                excl_parts_ref.append(f'not str_includes(ref_fp, "{seg}/")')
                excl_parts_ref.append(f'not str_includes(ref_fp, "{seg}\\\\")')
        if excl_parts:
            excl_clauses = ", " + ", ".join(excl_parts)
        if excl_parts_ref:
            excl_clauses_ref = ", " + ", ".join(excl_parts_ref)

        query = (
            # Live references: from reachable code via pre-computed relations
            # (ref_by_block keyed on (fp, fq, bid, sq) joins efficiently with
            # reachable_block keyed on (fp, fq, bid))
            "live_ref[sq] := "
            "*ref_by_block[fp, fq, bid, sq], "
            "*reachable_block[fp, fq, bid], "
            f"sq != fq{excl_clauses}\n"

            # Live references: from module level (no function context)
            # Exclude self-references where the reference is the symbol's own definition
            'live_ref[sq] := '
            '*module_level_ref[sq, ref_fp, ref_line], '
            '*symbol[sq, sym_fp, _, _, sym_line, _, _], '
            f'not (ref_fp == sym_fp, ref_line == sym_line){excl_clauses_ref}\n'

            # Entry points: dunder methods
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            'starts_with(name, "__"), ends_with(name, "__")\n'

            # Entry points: dynamic prefix rules (test_, Test, describe_, etc.)
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            '*entry_point_prefix[pfx], '
            'starts_with(name, pfx)\n'

            # Entry points: decorated symbols (case-insensitive for TS PascalCase)
            'entry_point[qn] := '
            '*decorator_on[qn, dec], '
            '*entry_point_decorator[ep_dec], '
            'lowercase(dec) == lowercase(ep_dec)\n'

            # Entry points: named symbols
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            '*entry_point_name[name]\n'

            # Entry points: explicitly exported symbols
            'entry_point[qn] := '
            '*exported_symbol[qn]\n'

            # Dead symbols: top-level only (parent == ""), no live reference, not entry point
            "dead[qn, fp, name, kind, line, end_line, parent] := "
            "*symbol[qn, fp, name, kind, line, end_line, parent], "
            'parent == "", '
            "not live_ref[qn], "
            "not entry_point[qn]\n"

            "?[fp, name, qn, kind, line, end_line, parent] := "
            "dead[qn, fp, name, kind, line, end_line, parent]"
        )

        result = self._client.run(query)
        dead_symbols = [
            SymbolFact(
                file_path=r[0], name=r[1], qualified_name=r[2],
                kind=r[3], line=r[4], end_line=r[5],
                parent=r[6] if r[6] else None,
            )
            for r in result["rows"]
        ]

        # Query for unreachable blocks (non-exit blocks not reachable from entry)
        unreachable_query = (
            "unreachable[fp, fq, bid] := "
            "*cfg_block[fp, fq, bid, _, is_exit], "
            "is_exit == false, "
            "not *reachable_block[fp, fq, bid]\n"

            "?[fp, fq, bid, ie, ix] := "
            "unreachable[fp, fq, bid], "
            "*cfg_block[fp, fq, bid, ie, ix]"
        )

        try:
            unreachable_result = self._client.run(unreachable_query)
            unreachable_blocks = [
                CfgBlockFact(
                    file_path=r[0], func_qn=r[1], block_id=r[2],
                    is_entry=r[3], is_exit=r[4],
                )
                for r in unreachable_result["rows"]
            ]
        except Exception:
            unreachable_blocks = []

        return dead_symbols, unreachable_blocks

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
            "layer_0[caller] := changed[callee], *call_by_callee[callee, caller, _, _, _, _, _]\n"
        )
        for i in range(1, max_depth):
            rules.append(
                f"layer_{i}[caller] := layer_{i - 1}[mid], "
                f"*call_by_callee[mid, caller, _, _, _, _, _]\n"
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
            "has_external_caller[qn] := *call_by_callee[qn, caller, _, _, _, _, _], "
            "not to_delete[caller]\n"
            # Cascade targets: callees of deleted symbols with no external callers,
            # excluding the initial deletes themselves.
            "callee_of_deleted[qn] := *call_by_callee[qn, caller, _, _, _, _, _], to_delete[caller]\n"
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
                "alive[qn] := *call_by_callee[qn, caller, _, _, _, _, _], not excluded[caller]\n"
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
        clauses = ["*reference[$qn, fp, line, col, kind, fq, bid]"]
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
            "callee_qn = $qn, "
            "*call_by_callee[$qn, caller_qn, fp, line, col, fq, bid]",
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

        Uses caller_qn on call facts to find what a function calls.
        Replaces Python line-range filtering in find_callees().
        """
        result = self._client.run(
            "?[caller_qn, callee_qn, fp, line, col, fq, bid] := "
            "caller_qn = $fqn, "
            "*call[$fqn, callee_qn, fp, line, col, fq, bid]",
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
                "*call_by_file[$fp, caller_qn, callee_qn, _, _, _, _]",
                {"fp": file_path},
            )
        else:
            result = self._client.run(
                "?[caller_qn, callee_qn] := "
                "*call[caller_qn, callee_qn, _, _, _, _, _]"
            )
        return [(r[0], r[1]) for r in result["rows"]]

    # -- Phase 5: Trace (data-flow) analysis via Datalog --------------------------------

    @staticmethod
    def _cozo_quote(v: str | int) -> str:
        """Escape *v* for a CozoScript single-quoted string literal.

        Subscript expressions like ``request.POST["id"]`` break double-quoted
        CozoScript strings, so we use single-quoted literals throughout.
        """
        if isinstance(v, str):
            escaped = v.replace("\\", "\\\\").replace("'", "\\'")
            return f"'{escaped}'"
        return str(v)

    @staticmethod
    def _inline_relation(
        name: str,
        cols: list[str],
        rows: list[tuple[str | int, ...]],
    ) -> str:
        """Build a CozoScript inline-relation rule.

        Returns a string like ``name[c1, c2] <- [[v1, v2], [v3, v4]]\\n``
        or ``name[c1, c2] <- []\\n`` when *rows* is empty.
        """
        col_str = ", ".join(cols)
        if not rows:
            return f"{name}[{col_str}] <- []\n"
        _q = FactGraph._cozo_quote
        formatted = ", ".join(
            "[" + ", ".join(_q(v) for v in row) + "]"
            for row in rows
        )
        return f"{name}[{col_str}] <- [{formatted}]\n"

    def trace_propagation_datalog(
        self,
        sources: list[tuple[str, str, str, int, str]],  # (file_path, func_qn, var_name, block_id, label)
        sinks: list[tuple[str, str, str, int, str]] | None = None,  # (file_path, func_qn, var_name, block_id, label)
        effect_sinks: list[tuple[str, str]] | None = None,  # (label, effect_kind) e.g. [("toctou", "writes")]
        sanitizers: list[tuple[str, str, str, int, str]] | None = None,  # same as sources
        sanitizer_quantifier: str = "all_paths",  # "all_paths" or "some_path"
        source_lines: list[tuple[str, str, str, int, int]] | None = None,  # (fp, fq, lbl, block_id, line)
        sanitizer_lines: list[tuple[str, str, str, str, int, int]] | None = None,  # (fp, fq, var, lbl, block_id, line)
        sink_lines: list[tuple[str, str, str, int, int]] | None = None,  # (fp, fq, lbl, block_id, line)
        scope_kills: list[tuple[str, str, str, int]] | None = None,  # (file_path, func_qn, label, block_id)
        scope_kill_lines: list[tuple[str, str, str, int, int]] | None = None,  # (fp, fq, lbl, block_id, line)
        scalar_types: list[str] | None = None,  # type names to filter out from sources (e.g. ["int", "float"])
    ) -> list[TraceFlowFact]:
        """Intraprocedural taint propagation via Datalog over def_use facts.

        Pattern matching (identifying sources/sinks/sanitizers) stays in Python.
        This method handles propagation: given pre-computed source/sink locations,
        it traces taint through CFG-edge reachability and def-use chains.

        **Path-sensitive sanitization**: Taint only propagates to blocks that are
        *unsanitized-reachable* from the source via CFG edges.  With the default
        ``all_paths`` quantifier, a sanitizer must appear on **every** CFG path
        from source to sink to suppress the violation.  With ``some_path``, a
        sanitizer on **any** path suffices (the old behaviour).

        **Intra-block line ordering**: When a sanitizer and sink co-occur in the
        same basic block, the violation is suppressed only if the sanitizer line
        precedes the sink line.  Pass ``sanitizer_lines`` and ``sink_lines`` to
        enable this guard.

        When ``effect_sinks`` is provided, violations are also detected when a
        tainted variable (or its attributes) is written/mutated in a reachable
        block.  This replaces the old ``attribute_mutation_sinks`` mechanism.

        Returns TraceFlowFact entries for each source-to-sink violation found.
        """
        if not sources:
            return []
        if not sinks and not effect_sinks:
            return []

        _ir = self._inline_relation
        _5cols = ["fp", "fq", "var", "bid", "lbl"]

        # Insert source/sink/sanitizer matches as inline relations
        src_rule = _ir("trace_source", _5cols, sources)
        sink_rule = _ir("trace_sink", _5cols, sinks or [])
        # Per-variable sanitizer relation: includes the variable name so that
        # sanitizing `a` does not clear taint on `b` in the same block.
        sanitizer_var_rule = _ir(
            "sanitizer_var", _5cols,
            sanitizers or [],
        )
        # Block-level sanitizer (derived): a block has ANY sanitizer for a label.
        # Used for scope-kill-style checks and some_path quantifier.
        sanitizer_block_rule = (
            "sanitizer_block[fp, fq, lbl, bid] := "
            "sanitizer_var[fp, fq, _, bid, lbl]\n"
        )

        # Effect sink rules
        effect_rules = ""
        if effect_sinks:
            effect_rules += _ir("effect_sink_label", ["lbl"],
                                [(lbl,) for lbl, _ in effect_sinks])
            effect_rules += 'mutate_kind[k] <- [["write"], ["aug_write"]]\n'

        # Intra-block line-ordering
        _5line = ["fp", "fq", "lbl", "bid", "line"]
        _6line = ["fp", "fq", "var", "lbl", "bid", "line"]
        source_line_rule = _ir("source_in_block", _5line, source_lines or [])
        san_line_rule = _ir("sanitizer_in_block", _6line, sanitizer_lines or [])
        sink_line_rule = _ir("sink_in_block", _5line, sink_lines or [])

        # Scope kills
        scope_kill_rule = _ir("scope_kill", ["fp", "fq", "lbl", "bid"],
                              scope_kills or [])
        scope_kill_line_rule = _ir("scope_kill_in_block", _5line,
                                   scope_kill_lines or [])

        # -- Phase 4: type-conditioned filtering --
        if scalar_types:
            type_filter_rules = (
                _ir("scalar_type", ["t"], [(t,) for t in scalar_types])
                + "scalar_typed[fp, fq, var, block] := "
                "trace_source[fp, fq, var, block, _], "
                "*type_binding[_, fp, line, _, type_str], "
                "*def_use[fp, fq, var, _, block, _, line, _, _, _], "
                "scalar_type[type_str]\n"
                "effective_source[fp, fq, var, block, lbl] := "
                "trace_source[fp, fq, var, block, lbl], "
                "not scalar_typed[fp, fq, var, block]\n"
            )
            source_relation = "effective_source"
        else:
            type_filter_rules = ""
            source_relation = "trace_source"

        # -- Build the Datalog query --

        if sanitizer_quantifier == "some_path":
            # some_path: sanitizer on ANY path suppresses.
            # If source can reach a sanitizer block, and that sanitizer block
            # can reach the sink, the violation is suppressed.
            # Per-variable: only suppresses taint for the specific sanitized variable.
            query = (
                f"{src_rule}"
                f"{sink_rule}"
                f"{sanitizer_var_rule}"
                f"{sanitizer_block_rule}"
                f"{effect_rules}"
                f"{source_line_rule}"
                f"{san_line_rule}"
                f"{sink_line_rule}"
                f"{scope_kill_rule}"
                f"{type_filter_rules}"

                # CFG reachability (for some_path sanitizer check)
                "cfg_reaches[fp, fq, block, block] := "
                "*cfg_block[fp, fq, block, _, _]\n"

                "cfg_reaches[fp, fq, from_b, to_b] := "
                "cfg_reaches[fp, fq, from_b, mid], "
                "*cfg_edge[fp, fq, mid, to_b, _, _, _]\n"

                # Per-variable: a sink var is sanitized if source→sanitizer(var)→sink via CFG
                "sink_sanitized[fp, fq, var, lbl, sink_block] := "
                f"{source_relation}[fp, fq, var, src_block, lbl], "
                "sanitizer_var[fp, fq, var, san_block, lbl], "
                "cfg_reaches[fp, fq, src_block, san_block], "
                "cfg_reaches[fp, fq, san_block, sink_block]\n"

                # Taint sources are tainted
                "tainted[fp, fq, var, block, lbl] := "
                f"{source_relation}[fp, fq, var, block, lbl]\n"

                # Propagation through def-use chains (no blocking — sanitizer
                # suppression happens at violation level for some_path)
                "tainted[fp, fq, var, use_block, lbl] := "
                "tainted[fp, fq, var, def_block, lbl], "
                "*def_use[fp, fq, var, _kind, def_block, use_block, _, _, _, _], "
                "not scope_kill[fp, fq, lbl, def_block]\n"

                # Cross-variable taint via assignment (some_path variant)
                "tainted[fp, fq, def_var, block, lbl] := "
                "tainted[fp, fq, use_var, block, lbl], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, _], "
                "*def_use[fp, fq, def_var, _, block, _, def_line, _, _, _], "
                "use_var != def_var, "
                "use_line == def_line, "
                "not str_includes(def_var, \".\"), "
                "not str_includes(def_var, \"[\"), "
                "not scope_kill[fp, fq, lbl, block]\n"

                # Field/subscript assignment taint (some_path variant):
                # ``obj.field = tainted_var`` where def_col < use_col ensures
                # genuine LHS=RHS assignment rather than coincidental same-line.
                "tainted[fp, fq, def_var, block, lbl] := "
                "tainted[fp, fq, use_var, block, lbl], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, use_col], "
                "*def_use[fp, fq, def_var, _, block, _, def_line, _, def_col, _], "
                "use_var != def_var, "
                "use_line == def_line, "
                "def_col < use_col, "
                "not scope_kill[fp, fq, lbl, block]\n"

                # Container mutation taint (some_path variant)
                "tainted[fp, fq, receiver, block, lbl] := "
                "tainted[fp, fq, use_var, block, lbl], "
                "*method_call[fp, fq, receiver, _method, block, call_line], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, _], "
                "use_line == call_line, "
                "receiver != use_var, "
                "not scope_kill[fp, fq, lbl, block]\n"

                # Pattern-based violations: taint reaches sink, not sanitized
                # Per-variable: only suppress if THIS sink_var was sanitized
                "violation[fp, fq, src_var, sink_var, lbl, src_block, sink_block] := "
                "tainted[fp, fq, sink_var, sink_block, lbl], "
                "trace_sink[fp, fq, sink_var, sink_block, lbl], "
                f"{source_relation}[fp, fq, src_var, src_block, lbl], "
                "not sink_sanitized[fp, fq, sink_var, lbl, sink_block]\n"
            )
        else:
            # all_paths (default): sanitizer must be on EVERY path.
            # Per-variable: unsanitized reachability is tracked per (var, label)
            # so that sanitizing variable `a` does not clear taint on variable `b`.
            query = (
                f"{src_rule}"
                f"{sink_rule}"
                f"{sanitizer_var_rule}"
                f"{sanitizer_block_rule}"
                f"{effect_rules}"
                f"{source_line_rule}"
                f"{san_line_rule}"
                f"{sink_line_rule}"
                f"{scope_kill_rule}"
                f"{type_filter_rules}"

                # Check if any CFG edges exist for functions with taint sources
                "has_cfg[fp, fq] := "
                f"{source_relation}[fp, fq, _, _, _], "
                "*cfg_edge[fp, fq, _, _, _, _, _]\n"

                # Per-variable unsanitized reachability.
                # Base case: source variable is unsanitized at its source block
                "unsanitized[fp, fq, var, lbl, block] := "
                f"{source_relation}[fp, fq, var, block, lbl]\n"

                # With CFG: propagate along CFG edges, blocked by sanitizer for
                # this specific variable (not all variables in the block)
                "unsanitized[fp, fq, var, lbl, to_block] := "
                "unsanitized[fp, fq, var, lbl, from_block], "
                "has_cfg[fp, fq], "
                "*cfg_edge[fp, fq, from_block, to_block, _, _, _], "
                "not sanitizer_var[fp, fq, var, from_block, lbl], "
                "not scope_kill[fp, fq, lbl, from_block]\n"

                # Without CFG (fallback): propagate unsanitized via def-use
                "unsanitized[fp, fq, var, lbl, use_block] := "
                "unsanitized[fp, fq, var, lbl, def_block], "
                "not has_cfg[fp, fq], "
                "*def_use[fp, fq, _, _, def_block, use_block, _, _, _, _], "
                "not sanitizer_var[fp, fq, var, def_block, lbl], "
                "not scope_kill[fp, fq, lbl, def_block]\n"

                # A variable is tainted in a block if:
                #   (a) it's a source in that block, OR
                #   (b) taint propagates via def-use AND var is unsanitized at use block
                #   (c) taint flows across variable names via assignment
                "tainted[fp, fq, var, block, lbl] := "
                f"{source_relation}[fp, fq, var, block, lbl]\n"

                "tainted[fp, fq, var, use_block, lbl] := "
                "tainted[fp, fq, var, def_block, lbl], "
                "*def_use[fp, fq, var, _kind, def_block, use_block, _, _, _, _], "
                "unsanitized[fp, fq, var, lbl, use_block]\n"

                # Cross-variable taint via assignment: ``y = f(x)`` where x is
                # tainted means y is tainted.  Uses use_var's unsanitized state
                # to determine whether its taint should propagate.
                "tainted[fp, fq, def_var, block, lbl] := "
                "tainted[fp, fq, use_var, block, lbl], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, _], "
                "*def_use[fp, fq, def_var, _, block, _, def_line, _, _, _], "
                "use_var != def_var, "
                "use_line == def_line, "
                "not str_includes(def_var, \".\"), "
                "not str_includes(def_var, \"[\"), "
                "unsanitized[fp, fq, use_var, lbl, block]\n"

                # Field/subscript assignment taint (all_paths variant):
                # ``obj.field = tainted_var`` with column ordering guard.
                "tainted[fp, fq, def_var, block, lbl] := "
                "tainted[fp, fq, use_var, block, lbl], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, use_col], "
                "*def_use[fp, fq, def_var, _, block, _, def_line, _, def_col, _], "
                "use_var != def_var, "
                "use_line == def_line, "
                "def_col < use_col, "
                "unsanitized[fp, fq, use_var, lbl, block]\n"

                # Inherit unsanitized status for cross-variable taint:
                # when def_var gets tainted from use_var, def_var is also
                # unsanitized (unless it has its own sanitizer).
                "unsanitized[fp, fq, def_var, lbl, block] := "
                "unsanitized[fp, fq, use_var, lbl, block], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, _], "
                "*def_use[fp, fq, def_var, _, block, _, def_line, _, _, _], "
                "use_var != def_var, "
                "use_line == def_line, "
                "not str_includes(def_var, \".\"), "
                "not str_includes(def_var, \"[\"), "
                "not sanitizer_var[fp, fq, def_var, block, lbl]\n"

                # Inherit unsanitized for field/subscript assignments.
                "unsanitized[fp, fq, def_var, lbl, block] := "
                "unsanitized[fp, fq, use_var, lbl, block], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, use_col], "
                "*def_use[fp, fq, def_var, _, block, _, def_line, _, def_col, _], "
                "use_var != def_var, "
                "use_line == def_line, "
                "def_col < use_col, "
                "not sanitizer_var[fp, fq, def_var, block, lbl]\n"

                # Container mutation taint: tainted var passed to method call
                "tainted[fp, fq, receiver, block, lbl] := "
                "tainted[fp, fq, use_var, block, lbl], "
                "*method_call[fp, fq, receiver, _method, block, call_line], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, _], "
                "use_line == call_line, "
                "receiver != use_var, "
                "unsanitized[fp, fq, use_var, lbl, block]\n"

                # Inherit unsanitized for container mutation
                "unsanitized[fp, fq, receiver, lbl, block] := "
                "unsanitized[fp, fq, use_var, lbl, block], "
                "*method_call[fp, fq, receiver, _method, block, call_line], "
                "*def_use[fp, fq, use_var, _, _, block, _, use_line, _, _], "
                "use_line == call_line, "
                "receiver != use_var, "
                "not sanitizer_var[fp, fq, receiver, block, lbl]\n"

                # Pattern-based violations: taint reaches sink
                "violation[fp, fq, src_var, sink_var, lbl, src_block, sink_block] := "
                "tainted[fp, fq, sink_var, sink_block, lbl], "
                "trace_sink[fp, fq, sink_var, sink_block, lbl], "
                f"{source_relation}[fp, fq, src_var, src_block, lbl]\n"
            )

        # Effect-based violations: tainted var is written/mutated
        # Three rule variants implement is_var_or_attr matching.
        # Each excludes the source block to avoid self-triggering (the source
        # definition itself is a "write" but should not count as a violation).
        if effect_sinks:
            # Variant 1: exact var match — write to the tainted var itself
            query += (
                "violation[fp, fq, src_var, sink_var, lbl, src_block, sink_block] := "
                "tainted[fp, fq, sink_var, sink_block, lbl], "
                "effect_sink_label[lbl], "
                "*def_use[fp, fq, sink_var, kind, sink_block, _, _, _, _, _], "
                "mutate_kind[kind], "
                f"{source_relation}[fp, fq, src_var, src_block, lbl], "
                "sink_block != src_block\n"
            )
            # Variant 2: dotted attribute — write to sink_var.field
            query += (
                "violation[fp, fq, src_var, sink_var, lbl, src_block, sink_block] := "
                "tainted[fp, fq, sink_var, sink_block, lbl], "
                "effect_sink_label[lbl], "
                "*def_use[fp, fq, var_name, kind, sink_block, _, _, _, _, _], "
                "mutate_kind[kind], "
                'starts_with(var_name, concat(sink_var, ".")), '
                f"{source_relation}[fp, fq, src_var, src_block, lbl], "
                "sink_block != src_block\n"
            )
            # Variant 3: method call on tainted var (e.g. sink_var.append())
            query += (
                "violation[fp, fq, src_var, sink_var, lbl, src_block, sink_block] := "
                "tainted[fp, fq, sink_var, sink_block, lbl], "
                "effect_sink_label[lbl], "
                "*method_call[fp, fq, sink_var, _, sink_block, _], "
                f"{source_relation}[fp, fq, src_var, src_block, lbl], "
                "sink_block != src_block\n"
            )

        # Same-block suppression: if sanitizer precedes sink in the same block,
        # filter out those violations.  Per-variable: only suppress when the
        # sanitized variable matches the sink variable.
        query += (
            f"{scope_kill_line_rule}"
            "same_block_sanitized[fp, fq, var, lbl, block] := "
            "sanitizer_in_block[fp, fq, var, lbl, block, san_line], "
            "sink_in_block[fp, fq, lbl, block, sink_line], "
            "san_line < sink_line\n"
        )
        # Scope kill same-block suppression: if scope sanitizer appears
        # BETWEEN a source and a sink in the same block, suppress.
        # The kill must be after the source (otherwise it kills taint
        # that doesn't exist yet) and before the sink.
        # All three line values use 1-based pattern-match line numbers.
        # Scope kill same-block: applies to ALL variables for the label
        # (scope kills are not per-variable), so we need a sink_var wildcard.
        query += (
            "same_block_sanitized[fp, fq, sink_var, lbl, block] := "
            "scope_kill_in_block[fp, fq, lbl, block, kill_line], "
            "sink_in_block[fp, fq, lbl, block, sink_line], "
            "source_in_block[fp, fq, lbl, block, src_line], "
            "trace_sink[fp, fq, sink_var, block, lbl], "
            "kill_line > src_line, "
            "kill_line < sink_line\n"
        )
        # For effect-based sinks: the mutation line is the def_line in def_use.
        if effect_sinks:
            # The sanitizer variable (e.g. "x") must match or be a prefix of
            # the written variable (e.g. "x" or "x.dirty").  The violation's
            # sink_var comes from the tainted relation and is the base variable.
            query += (
                "same_block_sanitized[fp, fq, san_var, lbl, block] := "
                "sanitizer_in_block[fp, fq, san_var, lbl, block, san_line], "
                "effect_sink_label[lbl], "
                "*def_use[fp, fq, _, kind, block, _, write_line, _, _, _], "
                "mutate_kind[kind], "
                "san_line < write_line\n"
            )

        query += (
            "?[fp, fq, src_var, sink_var, lbl, src_block, sink_block] := "
            "violation[fp, fq, src_var, sink_var, lbl, src_block, sink_block], "
            "not same_block_sanitized[fp, fq, sink_var, lbl, sink_block]"
        )

        result = self._client.run(query)
        return [
            TraceFlowFact(
                source_var=r[2], sink_var=r[3], label=r[4],
                file_path=r[0], func_qn=r[1],
                source_line=r[5], sink_line=r[6],
            )
            for r in result["rows"]
        ]

    def interprocedural_trace_datalog(
        self,
        sources: list[tuple[str, str, str, int, str]] | None = None,
        sinks: list[tuple[str, str, str, int, str]] | None = None,
        max_iterations: int = 10,
    ) -> list[TraceFlowFact]:
        """Interprocedural taint analysis via recursive Datalog.

        Replaces the Python fixed-point loop in run_interprocedural_taint_analysis().
        Uses func_summary and call facts to propagate taint across function boundaries.

        When *sources* and *sinks* are provided (as ``(file_path, func_qn, var_name,
        block_id, label)`` tuples), they are used to seed the query via inline
        relations so that only relevant label/param combinations are followed.
        When omitted, all stored ``func_summary`` facts are considered.
        """
        _ir = self._inline_relation
        _5cols = ["fp", "fq", "var", "bid", "lbl"]

        # Seed inline relations when config-driven sources/sinks are provided.
        # The interprocedural summary relation does not preserve exact match
        # vars/blocks across call boundaries, so these seeds constrain the
        # caller/callee functions and labels rather than pretending to track
        # the original per-match tuples end-to-end.
        config_seed = ""
        if sources is not None:
            config_seed += _ir("cfg_source", _5cols, sources)
        if sinks is not None:
            config_seed += _ir("cfg_sink", _5cols, sinks)

        source_seed_rule = ""
        source_violation_guard = ""
        if sources is not None:
            source_seed_rule = (
                "seed_source[fp, fq, lbl] := cfg_source[fp, fq, _, _, lbl]\n"
            )
            source_violation_guard = ", seed_source[fp, caller_fq, lbl]"

        sink_seed_rule = ""
        sink_summary_guard = ""
        if sinks is not None:
            sink_seed_rule = (
                "seed_sink[fq, lbl] := cfg_sink[_, fq, _, _, lbl]\n"
            )
            sink_summary_guard = ", seed_sink[fq, lbl]"

        query = config_seed + source_seed_rule + sink_seed_rule + (
            # Direct summaries (from intraprocedural analysis)
            "param_flows_to_return[fq, param] := "
            "*func_summary[fq, param, ftr, _, _], ftr == true\n"

            # Direct summaries: param flows to sink
            "param_flows_to_sink[fq, param, lbl] := "
            f"*func_summary[fq, param, _, fts, lbl], fts == true{sink_summary_guard}\n"

            # Transitive: if callee's param flows to return, propagate through call
            "param_flows_to_return[caller_fq, caller_param] := "
            "*call[caller_fq, callee_fq, _, _, _, _, _], "
            "param_flows_to_return[callee_fq, callee_param], "
            "*def_use[_, caller_fq, caller_param, _, _, _, _, _, _, _]\n"

            # Violations: tainted param flows to sink through call chain
            "violation[caller_fq, callee_fq, fp, param, lbl] := "
            "*call[caller_fq, callee_fq, fp, _, _, _, _], "
            f"param_flows_to_sink[callee_fq, param, lbl]{source_violation_guard}\n"

            "?[caller_fq, callee_fq, fp, param, lbl] := "
            "violation[caller_fq, callee_fq, fp, param, lbl]"
        )

        result = self._client.run(query)
        return [
            TraceFlowFact(
                source_var=r[3], sink_var=r[3], label=r[4],
                file_path=r[2], func_qn=r[0],
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
        source_lines: dict[tuple[str, str, int], int] | None = None,
        sink_lines: dict[tuple[str, str, int], int] | None = None,
        blocker_lines: dict[tuple[str, str, int], int] | None = None,
        include_locations: bool = False,
    ) -> list[tuple]:
        """Check flow-based lint rules via Datalog.

        Replaces _check_flow_rule() in lint.py for flows-from/flows-to/not-through rules.

        ``through`` uses CFG-edge reachability: a violation fires if any path
        from source to sink *avoids* the required through-point (i.e., the
        complement of ``all_paths`` — the through-point must appear on every
        path to suppress the violation).

        ``not_through`` blocks propagation through the specified points.

        ``source_lines``, ``sink_lines``, ``blocker_lines`` are optional dicts
        keyed by ``(file_path, func_qn, block_id)`` mapping to line numbers.
        When provided, same-block results are post-filtered in Python:
        source_line < sink_line, and source_line < blocker_line < sink_line.

        Returns list of ``(file_path, func_qn, source_var, sink_var)`` tuples.
        When ``include_locations`` is true, the result also includes
        ``(source_block, sink_block)``.
        """
        if not sources or not sinks:
            return []

        _ir = self._inline_relation
        _4cols = ["fp", "fq", "var", "bid"]
        src_rule = _ir("flow_source", _4cols, sources)
        sink_ir = _ir("flow_sink", _4cols, sinks)
        not_through_rule = _ir("blocked", _4cols, not_through or [])

        if through:
            required_rule = _ir("required", ["fp", "fq", "bid"],
                                [(fp, fq, bid) for fp, fq, _var, bid in through])

            # CFG-edge reachability that avoids required points
            through_rules = (
                f"{required_rule}"

                # blocked_block: union of required (for through) and not_through blocks
                "blocked_cfg[fp, fq, bid] := required[fp, fq, bid]\n"
                "blocked_cfg[fp, fq, bid] := blocked[fp, fq, _, bid]\n"

                # Base case: source blocks can avoid required points
                "avoids_required[fp, fq, block] := "
                "flow_source[fp, fq, _, block]\n"

                # Recursive: propagate along CFG edges, skipping blocked blocks
                "avoids_required[fp, fq, to_block] := "
                "avoids_required[fp, fq, from_block], "
                "*cfg_edge[fp, fq, from_block, to_block, _, _, _], "
                "not blocked_cfg[fp, fq, from_block]\n"

                # through-violation: sink reachable while avoiding required point
                "through_violation[fp, fq, src_var, sink_var] := "
                "avoids_required[fp, fq, sink_block], "
                "flow_sink[fp, fq, sink_var, sink_block], "
                "flow_source[fp, fq, src_var, _]\n"
            )
        else:
            through_rules = ""

        # Standard def-use reachability (for not_through and basic flow)
        query = (
            f"{src_rule}"
            f"{sink_ir}"
            f"{not_through_rule}"
            f"{through_rules}"

            "reaches[fp, fq, var, block] := "
            "flow_source[fp, fq, var, block]\n"

            "reaches[fp, fq, target, use_block] := "
            "reaches[fp, fq, source, def_block], "
            "*def_use[fp, fq, source, _kind, def_block, use_block, _, _, _, _], "
            "target = source, "
            "not blocked[fp, fq, source, def_block], "
            "not blocked[fp, fq, source, use_block]\n"
        )

        if through:
            # Violation requires BOTH: flow reaches sink AND path avoids required
            query += (
                "?[fp, fq, src_var, sink_var, src_block, sink_block] := "
                "reaches[fp, fq, sink_var, sink_block], "
                "flow_sink[fp, fq, sink_var, sink_block], "
                "through_violation[fp, fq, src_var, sink_var], "
                "flow_source[fp, fq, src_var, src_block]"
            )
        else:
            query += (
                "?[fp, fq, src_var, sink_var, src_block, sink_block] := "
                "reaches[fp, fq, sink_var, sink_block], "
                "flow_sink[fp, fq, sink_var, sink_block], "
                "flow_source[fp, fq, src_var, src_block]"
            )

        result = self._client.run(query)
        raw = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in result["rows"]]

        # Post-filter: same-block line ordering
        if source_lines or sink_lines or blocker_lines:
            filtered: list[tuple] = []
            _src_lines = source_lines or {}
            _sink_lines = sink_lines or {}
            _blk_lines = blocker_lines or {}
            for fp, fq, src_var, sink_var, src_block, sink_block in raw:
                src_key = (fp, fq, src_block)
                sink_key = (fp, fq, sink_block)
                src_ln = _src_lines.get(src_key)
                sink_ln = _sink_lines.get(sink_key)

                # If source and sink share a block, require source_line < sink_line
                if src_block == sink_block and src_ln is not None and sink_ln is not None:
                    if src_ln >= sink_ln:
                        continue

                # If blocker shares a block with source/sink, require ordering
                skip = False
                for blk_key, blk_ln in _blk_lines.items():
                    blk_fp, blk_fq, blk_block = blk_key
                    if blk_fp != fp or blk_fq != fq:
                        continue
                    # Blocker in same block as source: must be after source
                    if blk_block == src_block and src_ln is not None:
                        if blk_ln <= src_ln:
                            continue  # blocker before source, doesn't count
                        # Blocker after source in same block — should block
                        # but only if also before sink
                        if sink_ln is not None and blk_block == sink_block:
                            if src_ln < blk_ln < sink_ln:
                                skip = True
                                break
                        elif blk_block != sink_block:
                            # blocker in source block, sink in different block
                            skip = True
                            break
                    # Blocker in same block as sink: must be before sink
                    elif blk_block == sink_block and sink_ln is not None:
                        if blk_ln < sink_ln:
                            # Check blocker is after source (if in different block, it always is)
                            if src_block != sink_block:
                                skip = True
                                break
                            elif src_ln is not None and src_ln < blk_ln:
                                skip = True
                                break
                if skip:
                    continue
                if include_locations:
                    filtered.append((fp, fq, src_var, sink_var, src_block, sink_block))
                else:
                    filtered.append((fp, fq, src_var, sink_var))
            return filtered

        if include_locations:
            return raw
        return [(fp, fq, src_var, sink_var) for fp, fq, src_var, sink_var, _sb, _skb in raw]

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
        for fact in self.trace_flows():
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
            "?[fp, fq, vn, k, db, ub, dl, dc, ul, uc] := *def_use[fp, fq, vn, k, db, ub, dl, ul, dc, uc]"
        )
        return [
            DefUseFact(
                file_path=r[0], func_qn=r[1], var_name=r[2],
                kind=r[3], def_block=r[4], use_block=r[5],
                def_line=r[6], def_col=r[7], use_line=r[8], use_col=r[9],
            )
            for r in result["rows"]
        ]

    def _all_method_calls(self) -> list[MethodCallFact]:
        result = self._client.run(
            "?[fp, fq, rcv, meth, bid, ln] := *method_call[fp, fq, rcv, meth, bid, ln]"
        )
        return [
            MethodCallFact(
                file_path=r[0], func_qn=r[1], receiver=r[2],
                method=r[3], block_id=r[4], line=r[5],
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
        for fact in self.trace_flows():
            data.append(_tag(fact))
        for fact in self._all_types():
            data.append(_tag(fact))
        for fact in self._all_imports():
            data.append(_tag(fact))
        for fact in self._all_cfg_edges():
            data.append(_tag(fact))
        for fact in self._all_def_uses():
            data.append(_tag(fact))
        for fact in self._all_method_calls():
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
            "TraceFlowFact": (TraceFlowFact, graph.add_trace_flow),
            "TypeFact": (TypeFact, graph.add_type),
            "ImportFact": (ImportFact, graph.add_import),
            "CfgEdgeFact": (CfgEdgeFact, graph.add_cfg_edge),
            "DefUseFact": (DefUseFact, graph.add_def_use),
            "MethodCallFact": (MethodCallFact, graph.add_method_call),
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

    # -- Incremental update / removal -------------------------------------

    def remove_files(self, file_paths: list[str]) -> None:
        """Delete all facts for the given files.

        Removes rows from every stored relation that references any of the
        supplied file paths.  Relations that key on ``file_path`` directly
        use a simple filter; relations keyed on ``symbol_qn`` or
        ``func_qn`` join through the ``symbol`` relation first.
        """
        if not file_paths:
            return

        # CozoDB `:rm` requires that the query variable names in `?[...]`
        # and `:rm relation { }` match the actual column names in the
        # stored relation schema.
        for fp in file_paths:
            for query in (
                # decorator_on/func_summary join through symbol — remove first
                "?[symbol_qn, decorator] := *decorator_on[symbol_qn, decorator], "
                "*symbol[symbol_qn, file_path, _, _, _, _, _], "
                "file_path == $fp  :rm decorator_on {symbol_qn, decorator}",
                "?[func_qn, param_name] := *func_summary[func_qn, param_name, _, _, _], "
                "*symbol[func_qn, file_path, _, _, _, _, _], "
                "file_path == $fp  :rm func_summary {func_qn, param_name => }",
                # symbol
                "?[qualified_name] := *symbol[qualified_name, file_path, _, _, _, _, _], "
                "file_path == $fp  :rm symbol {qualified_name => }",
                # call
                "?[caller_qn, callee_qn, file_path, line, col] := "
                "*call[caller_qn, callee_qn, file_path, line, col, _, _], "
                "file_path == $fp  :rm call {caller_qn, callee_qn, file_path, line, col => }",
                # call_by_callee
                "?[callee_qn, caller_qn, file_path, line, col] := "
                "*call_by_callee[callee_qn, caller_qn, file_path, line, col, _, _], "
                "file_path == $fp  :rm call_by_callee {callee_qn, caller_qn, file_path, line, col => }",
                # call_by_file
                "?[file_path, caller_qn, callee_qn, line, col] := "
                "*call_by_file[file_path, caller_qn, callee_qn, line, col, _, _], "
                "file_path == $fp  :rm call_by_file {file_path, caller_qn, callee_qn, line, col => }",
                # reference
                "?[symbol_qn, file_path, line, col] := "
                "*reference[symbol_qn, file_path, line, col, _, _, _], "
                "file_path == $fp  :rm reference {symbol_qn, file_path, line, col => }",
                # trace_flow (all keys)
                "?[source_var, sink_var, label, file_path, func_qn, source_line, sink_line] := "
                "*trace_flow[source_var, sink_var, label, file_path, func_qn, source_line, sink_line], "
                "file_path == $fp  :rm trace_flow "
                "{source_var, sink_var, label, file_path, func_qn, source_line, sink_line}",
                # type_binding
                "?[symbol_qn, file_path, line, binding_kind] := "
                "*type_binding[symbol_qn, file_path, line, binding_kind, _], "
                "file_path == $fp  :rm type_binding {symbol_qn, file_path, line, binding_kind => }",
                # cfg_edge (all keys)
                "?[file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line] := "
                "*cfg_edge[file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line], "
                "file_path == $fp  :rm cfg_edge "
                "{file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line}",
                # def_use
                "?[file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line] := "
                "*def_use[file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line, _, _], "
                "file_path == $fp  :rm def_use "
                "{file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line => }",
                # method_call (all keys)
                "?[file_path, func_qn, receiver, method, block_id, line] := "
                "*method_call[file_path, func_qn, receiver, method, block_id, line], "
                "file_path == $fp  :rm method_call "
                "{file_path, func_qn, receiver, method, block_id, line}",
                # cfg_block
                "?[file_path, func_qn, block_id] := "
                "*cfg_block[file_path, func_qn, block_id, _, _], "
                "file_path == $fp  :rm cfg_block {file_path, func_qn, block_id => }",
                # source_loc
                "?[file_path, loc_kind, loc_id] := "
                "*source_loc[file_path, loc_kind, loc_id, _, _, _, _], "
                "file_path == $fp  :rm source_loc {file_path, loc_kind, loc_id => }",
                # ref_by_block (all keys)
                "?[file_path, func_qn, block_id, symbol_qn] := "
                "*ref_by_block[file_path, func_qn, block_id, symbol_qn], "
                "file_path == $fp  :rm ref_by_block "
                "{file_path, func_qn, block_id, symbol_qn}",
                # reachable_block (all keys)
                "?[file_path, func_qn, block_id] := "
                "*reachable_block[file_path, func_qn, block_id], "
                "file_path == $fp  :rm reachable_block {file_path, func_qn, block_id}",
                # module_level_ref
                "?[symbol_qn, file_path, line] := "
                "*module_level_ref[symbol_qn, file_path, line], "
                "file_path == $fp  :rm module_level_ref {symbol_qn, file_path, line}",
                # import (uses importing_file, not file_path)
                "?[importing_file, imported_module, imported_name, line] := "
                "*import[importing_file, imported_module, imported_name, line, _], "
                "importing_file == $fp  :rm import "
                "{importing_file, imported_module, imported_name, line => }",
            ):
                try:
                    self._client.run(query, {"fp": fp})
                except Exception:
                    pass

    def update_files(
        self,
        file_list: list[tuple[str, str]],
        language: str = "python",
    ) -> None:
        """Incrementally update facts for a set of files.

        Takes a list of ``(file_path, source_content)`` pairs.  For each
        file, all existing facts are deleted and fresh facts are extracted
        from the provided content.  Files not in *file_list* are untouched.

        This is the single primitive on which ``build_from_files()``,
        ``build_from_project()``, and ``_build_facts_db()`` should be
        built.
        """
        from emend import emend_core as _rust
        from emend.cfg import build_cfgs_for_source

        # 1. Delete existing facts for all files in the batch.
        self.remove_files([str(Path(fp).resolve()) for fp, _ in file_list])

        # 2. Extract and insert new facts per file.
        for abs_file_path, content in file_list:
            rel_path = str(Path(abs_file_path).resolve())
            module_name = Path(abs_file_path).stem

            # -- Symbol facts -----------------------------------------------
            ext = Path(abs_file_path).suffix.lstrip(".") or "py"
            try:
                raw_symbols = _rust.collect_symbols_from_str(content, ext=ext)
            except Exception:
                logger.debug("Could not parse %s for symbols", abs_file_path, exc_info=True)
                raw_symbols = []

            sym_facts: list[SymbolFact] = []
            dec_facts: list[DecoratorOnFact] = []
            _walk_symbols(sym_facts, dec_facts, raw_symbols, rel_path, module_name, parent_qn=None)
            self.add_symbols_batch(sym_facts)
            self.add_decorator_on_batch(dec_facts)

            # -- Source location facts for symbols --------------------------
            source_loc_facts: list[SourceLocFact] = []
            for sf in sym_facts:
                source_loc_facts.append(SourceLocFact(
                    file_path=sf.file_path,
                    loc_kind="symbol",
                    loc_id=sf.qualified_name,
                    line=sf.line,
                    end_line=sf.end_line,
                ))
            self.add_source_locs_batch(source_loc_facts)

            # -- CFG blocks and edges ---------------------------------------
            try:
                cfgs = build_cfgs_for_source(content, ext=ext)
            except Exception:
                logger.debug("Could not build CFGs for %s", abs_file_path, exc_info=True)
                cfgs = []

            cfg_block_facts, cfg_edge_facts, block_loc_facts, block_ranges = (
                _build_cfg_facts(cfgs, sym_facts, rel_path, module_name)
            )
            self.add_cfg_blocks_batch(cfg_block_facts)
            self.add_cfg_edges_batch(cfg_edge_facts)
            self.add_source_locs_batch(block_loc_facts)

            # -- Reference and call facts (scope resolver) ------------------
            resolver = None
            refs: list = []
            try:
                resolver_root = str(Path(abs_file_path).parent.resolve())
                resolver = _rust.PyScopeResolver(resolver_root, ext)
                resolver.index_file(abs_file_path, content)
            except Exception:
                logger.debug("Could not build scope resolver for %s", abs_file_path, exc_info=True)

            if resolver is not None:
                symbol_ranges = _build_symbol_line_index(sym_facts, rel_path)
                try:
                    refs = resolver.references_in_file(abs_file_path)
                except Exception:
                    logger.debug("references_in_file failed for %s", abs_file_path, exc_info=True)
                    refs = []

                ref_facts: list[ReferenceFact] = []
                call_facts: list[CallFact] = []
                for qn, line, col, _offset, _end_offset, kind, _ann in refs:
                    ref_kind = _map_ref_kind(kind)
                    fq, bid = _find_containing_block(block_ranges, line)
                    ref_facts.append(ReferenceFact(
                        symbol_qn=qn, file_path=rel_path,
                        line=line, col=col, ref_kind=ref_kind,
                        func_qn=fq, block_id=bid,
                    ))
                    if ref_kind == "call":
                        caller = _enclosing_symbol(symbol_ranges, line)
                        caller_qn = caller if caller is not None else module_name
                        call_facts.append(CallFact(
                            caller_qn=caller_qn, callee_qn=qn,
                            file_path=rel_path, line=line, col=col,
                            func_qn=fq, block_id=bid,
                        ))
                self.add_references_batch(ref_facts)
                self.add_calls_batch(call_facts)

            # -- Def-use facts ----------------------------------------------
            def_use_facts: list[DefUseFact] = _build_def_use_facts(
                cfgs, sym_facts, rel_path, module_name
            )
            method_call_facts: list[MethodCallFact] = []

            # -- Module-level def-use facts ---------------------------------
            # The Rust CFG builder only produces CFGs for functions, so
            # module-level code has no def-use facts.  Synthesise them from
            # scope-resolver references that fall outside any function block.
            if resolver is not None:
                from emend.location_resolver import MODULE_LEVEL_BLOCK, MODULE_LEVEL_FUNC
                # Scope resolver uses 1-based lines; CFG uses 0-based.
                # Convert to 0-based for consistency with CFG def-use facts.
                mod_defs: dict[str, list[tuple[int, int]]] = {}  # var -> [(line, col)]
                mod_uses: dict[str, list[tuple[int, int]]] = {}
                for qn, line, col, _offset, _end_offset, kind, _ann in refs:
                    fq_check, bid_check = _find_containing_block(block_ranges, line)
                    if fq_check != "" or bid_check != -1:
                        continue  # inside a function — already handled
                    # Extract simple variable name (last segment of qn)
                    var_name = qn.rsplit(".", 1)[-1] if "." in qn else qn
                    rk = _map_ref_kind(kind)
                    line0 = line - 1  # convert to 0-based
                    if rk == "write":
                        mod_defs.setdefault(var_name, []).append((line0, col))
                    elif rk in ("read", "call"):
                        mod_uses.setdefault(var_name, []).append((line0, col))

                for var_name, use_entries in mod_uses.items():
                    if var_name in mod_defs:
                        for dl, dc in mod_defs[var_name]:
                            for ul, uc in use_entries:
                                def_use_facts.append(DefUseFact(
                                    file_path=rel_path,
                                    func_qn=MODULE_LEVEL_FUNC,
                                    var_name=var_name,
                                    kind="write",
                                    def_block=MODULE_LEVEL_BLOCK,
                                    use_block=MODULE_LEVEL_BLOCK,
                                    def_line=dl,
                                    def_col=dc,
                                    use_line=ul,
                                    use_col=uc,
                                ))

            # Method call facts from dotted-name call references.
            # The scope resolver uses 1-based line numbers, but CFG
            # def-use facts use 0-based line numbers.  Convert method
            # call lines to 0-based to match def-use for same-line
            # comparisons in Datalog taint rules.
            if resolver is not None:
                from emend.location_resolver import MODULE_LEVEL_BLOCK as _MLB
                from emend.location_resolver import MODULE_LEVEL_FUNC as _MLF
                for qn, line, col, _offset, _end_offset, kind, _ann in refs:
                    if _map_ref_kind(kind) == "call" and "." in qn:
                        parts = qn.rsplit(".", 1)
                        if len(parts) == 2:
                            fq, bid = _find_containing_block(block_ranges, line)
                            if fq == "" and bid == -1:
                                fq, bid = _MLF, _MLB
                            method_call_facts.append(MethodCallFact(
                                file_path=rel_path,
                                func_qn=fq,
                                receiver=parts[0].rsplit(".", 1)[-1],
                                method=parts[1],
                                block_id=bid,
                                line=line - 1,
                            ))

            self.add_def_uses_batch(def_use_facts)
            self.add_method_calls_batch(method_call_facts)

            # -- Import facts -----------------------------------------------
            import_facts = _extract_imports(rel_path, content)
            self.add_imports_batch(import_facts)

    # -- File-list builder ------------------------------------------------

    @classmethod
    def build_from_files(
        cls,
        file_paths: list[str],
        language: str = "python",
        db_path: str | None = None,
    ) -> FactGraph:
        """Populate a fact graph from an explicit list of source files.

        Unlike ``build_from_project`` this does not require a project
        directory — it builds symbol, CFG, def-use, and import facts
        directly from the given files.  Delegates to ``update_files()``.
        """
        graph = cls(db_path=db_path)

        file_list: list[tuple[str, str]] = []
        for abs_file_path in file_paths:
            try:
                content = Path(abs_file_path).read_text(encoding="utf-8")
            except Exception:
                logger.debug("Could not read %s", abs_file_path, exc_info=True)
                continue
            file_list.append((abs_file_path, content))

        if file_list:
            graph.update_files(file_list, language=language)

        return graph

    # -- Project builder --------------------------------------------------

    @classmethod
    def build_from_project(
        cls,
        project_path: str,
        language: str | None = None,
        db_path: str | None = None,
        languages: list[str] | None = None,
    ) -> FactGraph:
        """Populate a fact graph by visiting all files in a project.

        Uses emend's tree-sitter infrastructure (``emend_core``) to
        extract symbols, references, calls, and imports from every
        source file in the project.

        ``language`` (singular) keeps backward compatibility.  Pass
        ``languages`` (a list) or leave both as ``None`` to auto-detect
        from the project directory.

        **Note**: This is a deliberate full-rebuild path used by tests and
        one-off analysis.  In steady-state indexing, ``_build_facts_db()``
        in ``transform.py`` is the canonical path that populates the
        persisted ``facts.db`` directly from source files.
        """
        from emend import emend_core as _rust
        from emend.transform import (
            _collect_all_source_files,
            _collect_source_files,
            _file_to_module,
            _find_project_root,
            detect_project_languages,
        )

        graph = cls(db_path=db_path)
        project_root = _find_project_root(project_path)

        # Resolve which languages to collect.
        # Priority: explicit ``languages`` list > singular ``language`` > auto-detect.
        if languages is not None:
            effective_languages = list(languages)
        elif language is not None:
            effective_languages = [language]
        else:
            effective_languages = detect_project_languages(project_root)

        source_files = _collect_all_source_files(project_root, languages=effective_languages)

        # The scope resolver may fail if pointed at a repo root with
        # incompatible config.  Use the user-supplied project_path as
        # the resolver root (typically ``src/pkg``), falling back to
        # the detected project_root.
        resolver_root = str(Path(project_path).resolve())

        from emend.language_registry import detect_language as _detect_lang
        from emend.language_registry import detect_exported_names

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

            # -- Exported symbol facts (for TS/Rust visibility) ----------
            try:
                _lang = _detect_lang(abs_file_path) or "python"
                exported_names = detect_exported_names(content, _lang)
                if exported_names:
                    exported_qns = [
                        sf.qualified_name for sf in sym_facts
                        if sf.name in exported_names
                    ]
                    graph.add_exported_symbols_batch(exported_qns)
            except Exception:
                logger.debug("Could not detect exported symbols for %s", abs_file_path, exc_info=True)

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
            cfg_block_facts, cfg_edge_facts, block_loc_facts, block_ranges = (
                _build_cfg_facts(cfgs, sym_facts, rel_path, module_name)
            )
            graph.add_cfg_blocks_batch(cfg_block_facts)
            graph.add_cfg_edges_batch(cfg_edge_facts)
            graph.add_source_locs_batch(block_loc_facts)

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
                for qn, line, col, _offset, _end_offset, kind, _ann in refs:
                    qn = _normalize_qn(qn)
                    ref_kind = _map_ref_kind(kind)
                    fq, bid = _find_containing_block(block_ranges, line)
                    ref_facts.append(ReferenceFact(
                        symbol_qn=qn, file_path=rel_path,
                        line=line, col=col, ref_kind=ref_kind,
                        func_qn=fq, block_id=bid,
                    ))

                    if ref_kind == "call":
                        caller = _enclosing_symbol(symbol_ranges, line)
                        # Use module name as caller for module-level calls
                        caller_qn = caller if caller is not None else module_name
                        call_facts.append(CallFact(
                            caller_qn=caller_qn, callee_qn=qn,
                            file_path=rel_path, line=line, col=col,
                            func_qn=fq, block_id=bid,
                        ))

                graph.add_references_batch(ref_facts)
                graph.add_calls_batch(call_facts)

            # -- Def-use facts with block IDs ----------------------------
            def_use_facts: list[DefUseFact] = _build_def_use_facts(
                cfgs, sym_facts, rel_path, module_name
            )
            method_call_facts: list[MethodCallFact] = []

            # -- Method call facts (from call references with dotted names) --
            if resolver is not None:
                from emend.location_resolver import MODULE_LEVEL_BLOCK as _MLB
                from emend.location_resolver import MODULE_LEVEL_FUNC as _MLF
                for qn, line, col, _offset, _end_offset, kind, _ann in refs:
                    qn = _normalize_qn(qn)
                    if _map_ref_kind(kind) == "call" and "." in qn:
                        parts = qn.rsplit(".", 1)
                        if len(parts) == 2:
                            fq, bid = _find_containing_block(block_ranges, line)
                            if fq == "" and bid == -1:
                                fq, bid = _MLF, _MLB
                            method_call_facts.append(MethodCallFact(
                                file_path=rel_path,
                                func_qn=fq,
                                receiver=parts[0].rsplit(".", 1)[-1],
                                method=parts[1],
                                block_id=bid,
                                line=line - 1,
                            ))

            graph.add_def_uses_batch(def_use_facts)
            graph.add_method_calls_batch(method_call_facts)

            # -- Import facts (via stdlib ast) ----------------------------
            import_facts = _extract_imports(rel_path, content)
            graph.add_imports_batch(import_facts)

        # -- Resolve builtins.* references using import facts -----------
        # When a scope resolver can't resolve a cross-file import (common
        # for TypeScript/Rust), references end up as "builtins.X".  We
        # resolve these using the import facts to find the real callee QN.
        graph._resolve_builtin_refs(
            lambda fp: _file_to_module(
                str((Path(project_root).resolve() / fp)),
                project_root,
            ),
        )

        # -- Type binding facts (via type oracle) ----------------------
        # Populate after all files are processed so the type oracle can
        # see the full project.  Gracefully skips when no type checker is
        # available.
        try:
            from emend.type_oracle import create_type_oracle, parse_type_string

            oracle = create_type_oracle(engine="auto")
            if oracle.is_available():
                project_root_path = Path(project_root).resolve()
                for abs_file_path in source_files:
                    try:
                        rel_path = str(Path(abs_file_path).relative_to(project_root_path))
                    except ValueError:
                        rel_path = abs_file_path
                    try:
                        file_types = oracle.infer_file(Path(abs_file_path), project_root=project_root_path)
                    except Exception:
                        logger.debug("Type oracle failed for %s", abs_file_path, exc_info=True)
                        continue
                    type_facts: list[TypeFact] = []
                    for binding in file_types.bindings:
                        td = parse_type_string(binding.raw_type)
                        type_facts.append(TypeFact(
                            symbol_qn=binding.name,
                            type_str=td.name,  # top-level constructor
                            file_path=rel_path,
                            line=binding.line,
                            binding_kind=binding.binding_kind,
                        ))
                    graph.add_types_batch(type_facts)
        except Exception:
            logger.debug("Could not populate type bindings", exc_info=True)

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
    # Normalize the module name to use dots so that symbol QNs are
    # consistent with reference QNs (which go through _normalize_qn()).
    normalized_module = _normalize_qn(module_name)
    for d in raw_symbols:
        kind = d.get("kind", "")
        if kind in ("variable", "reference"):
            continue

        name = d["name"]
        path_parts = list(d.get("path", []))
        if path_parts:
            qn = f"{normalized_module}.{'.'.join(path_parts)}"
        else:
            qn = f"{normalized_module}.{name}"

        out.append(SymbolFact(
            file_path=file_path,
            name=name,
            qualified_name=qn,
            kind=kind,
            line=d["line"],
            end_line=d["end_line"],
            parent=parent_qn,
        ))

        # Extract decorators — strip @ prefix and arguments so that
        # ``@router.get('/users')`` becomes ``router.get`` and also
        # stores the basename ``get`` for broader matching.
        for dec_name in (d.get("decorators", []) or []):
            cleaned = dec_name
            if cleaned.startswith("@"):
                cleaned = cleaned[1:]
            if "(" in cleaned:
                cleaned = cleaned[:cleaned.index("(")]
            cleaned = cleaned.strip()
            dec_out.append(DecoratorOnFact(symbol_qn=qn, decorator=cleaned))
            # Also store the basename for broader matching
            basename = cleaned.rsplit(".", 1)[-1] if "." in cleaned else None
            if basename and basename != cleaned:
                dec_out.append(DecoratorOnFact(symbol_qn=qn, decorator=basename))

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


def _normalize_qn(qn: str) -> str:
    """Normalize language-specific QN separators to dots.

    The Rust scope resolver uses ``::`` (Rust) and ``/`` (TypeScript) as
    separators, but ``_walk_symbols`` always uses ``.``.

    Also strips quotes and normalizes relative import paths (e.g.
    ``'./target'.process`` → ``target.process``).
    """
    # Strip quotes from TS import source paths
    qn = qn.replace("'", "").replace('"', "")
    qn = qn.replace("::", ".").replace("/", ".")
    # Normalize relative path segments: ./target -> target, ../utils -> utils
    while ".." in qn:
        qn = qn.replace("..", ".")
    # Strip leading dots
    qn = qn.lstrip(".")
    return qn


def _find_containing_block(
    block_ranges: list[tuple],
    line: int,
) -> tuple[str, int]:
    """Find the (func_qn, block_id) containing a given line.

    Returns ``("", -1)`` for module-level code.

    *block_ranges* must be sorted by ``(start_line, -(end_line - start_line))``
    — the default ordering produced by ``_build_facts_db``.  Uses binary search
    to find the insertion point, then scans backwards through candidates whose
    start_line <= line, picking the tightest (smallest span) enclosing block.
    """
    import bisect

    if not block_ranges:
        return ("", -1)

    # bisect on start_line: find rightmost block whose start_line <= line.
    # block_ranges[i] = (func_qn, block_id, start_line, end_line, ...)
    # sorted by start_line ascending.
    lo, hi = 0, len(block_ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        if block_ranges[mid][2] <= line:
            lo = mid + 1
        else:
            hi = mid
    # lo is the first index where start_line > line.
    # Scan backwards from lo-1 to find all blocks containing the line.
    best_func_qn = ""
    best_block_id = -1
    best_span = float("inf")

    for i in range(lo - 1, -1, -1):
        start_line_i = block_ranges[i][2]
        # Once start_line is far enough before line that no block starting
        # here could still be the tightest, stop.  But blocks can be large,
        # so we must check end_line.  We can stop when
        # start_line < line - best_span (any block starting here with
        # span < best_span would end before line).
        if best_span < float("inf") and start_line_i < line - best_span:
            break
        end_line_i = block_ranges[i][3]
        if end_line_i >= line:
            span = end_line_i - start_line_i
            if span < best_span:
                best_span = span
                best_func_qn = block_ranges[i][0]
                best_block_id = block_ranges[i][1]

    return best_func_qn, best_block_id


def _extract_imports_python(file_path: str, content: str) -> list[ImportFact]:
    """Extract imports from Python source using tree-sitter via ``emend_core``.

    Falls back to ``stdlib ast`` when the Rust extension is unavailable (e.g.
    during development without a compiled extension).
    """
    facts: list[ImportFact] = []
    try:
        from emend import emend_core as ec  # type: ignore[attr-defined]
        resolver = getattr(_extract_imports_python, "_resolver", None)
        if resolver is None:
            resolver = ec.PyScopeResolver(".", extension="py")
            _extract_imports_python._resolver = resolver  # type: ignore[attr-defined]
        structured = resolver.collect_structured_imports_from_source(content, ext="py")
        for imp in structured:
            line: int = imp["start_line"]
            if imp["is_plain"]:
                # ``import X`` or ``import X as Y`` — module is in names
                for name, alias in imp["names"]:
                    facts.append(ImportFact(
                        importing_file=file_path,
                        imported_module=name,
                        imported_name=None,
                        alias=alias,
                        line=line,
                    ))
            else:
                # ``from M import X`` — module field + names
                level: int = imp["level"]
                raw_module: str = imp["module"]
                # Reconstruct leading dots for relative imports so that
                # callers can distinguish absolute from relative paths.
                module: str = ("." * level) + raw_module if level else raw_module
                for name, alias in imp["names"]:
                    facts.append(ImportFact(
                        importing_file=file_path,
                        imported_module=module,
                        imported_name=name,
                        alias=alias,
                        line=line,
                    ))
        return facts
    except Exception:
        logger.debug(
            "emend_core structured import extraction failed for %s, falling back to ast.parse",
            file_path,
            exc_info=True,
        )

    # Fallback: stdlib ast (Python only, but always available)
    import ast as stdlib_ast
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
# TypeScript / JavaScript import extraction
# ---------------------------------------------------------------------------

import re as _re
from bisect import bisect_right as _bisect_right


def _offset_to_line(content: str, offset: int, _newline_cache: dict[int, list[int]] = {}) -> int:
    """Return 1-based line number for a character *offset* in *content*.

    Uses a cached newline-position index keyed by ``id(content)`` so that
    repeated calls on the same string are O(log n) instead of O(n).
    """
    cid = id(content)
    positions = _newline_cache.get(cid)
    if positions is None:
        positions = [i for i, ch in enumerate(content) if ch == "\n"]
        _newline_cache[cid] = positions
        # Keep cache bounded — evict oldest if too large.
        if len(_newline_cache) > 64:
            oldest = next(iter(_newline_cache))
            del _newline_cache[oldest]
    return _bisect_right(positions, offset) + 1


# Matches: import ... from "module" / import ... from 'module'
# Groups: (import_body, module_path)
_TS_IMPORT_FROM_RE = _re.compile(
    r'^\s*(?:import\s+type\s+|import\s+)'   # import [type]
    r'(.*?)'                                  # import clause (greedy captured below)
    r'\s+from\s+'
    r'["\']([^"\']+)["\']'                   # "module" or 'module'
    r'\s*;?',
    _re.MULTILINE | _re.DOTALL,
)

# Matches: import "module"; (side-effect import)
_TS_SIDE_EFFECT_RE = _re.compile(
    r'^\s*import\s+["\']([^"\']+)["\']\s*;?',
    _re.MULTILINE,
)

# Matches: const/let/var X = require("module") or const { X } = require("module")
_TS_REQUIRE_RE = _re.compile(
    r'^\s*(?:const|let|var)\s+'
    r'(\{[^}]*\}|\w+)'                       # binding (destructured or simple name)
    r'\s*=\s*require\s*\(\s*["\']([^"\']+)["\']\s*\)',
    _re.MULTILINE,
)

# Matches: export { X } from "module";
_TS_EXPORT_FROM_RE = _re.compile(
    r'^\s*export\s+\{([^}]*)\}\s+from\s+["\']([^"\']+)["\']\s*;?',
    _re.MULTILINE,
)

# Full import-from pattern (multiline-safe)
_TS_FULL_IMPORT_RE = _re.compile(
    r'\bimport\s+(?:type\s+)?([\s\S]*?)\s+from\s+["\']([^"\']+)["\']',
    _re.MULTILINE,
)


def _ts_parse_import_clause(
    clause: str,
    module: str,
    file_path: str,
    line: int,
) -> list[ImportFact]:
    """Convert a TypeScript import clause string into ``ImportFact`` objects.

    Handles:
    - ``{ X, Y as Z }``        — named imports
    - ``* as X``               — namespace import
    - ``X``                    — default import
    - ``X, { Y, Z }``         — default + named
    """
    facts: list[ImportFact] = []
    clause = clause.strip()

    # Namespace import: * as X
    ns_m = _re.match(r'^\*\s+as\s+(\w+)$', clause)
    if ns_m:
        facts.append(ImportFact(
            importing_file=file_path,
            imported_module=module,
            imported_name="*",
            alias=ns_m.group(1),
            line=line,
        ))
        return facts

    # Split default import from named block if both present (X, { Y })
    brace_start = clause.find("{")
    if brace_start != -1:
        pre_brace = clause[:brace_start].strip().rstrip(",").strip()
        brace_end = clause.rfind("}")
        named_part = clause[brace_start + 1:brace_end] if brace_end != -1 else ""

        # Default import before the brace
        if pre_brace and not pre_brace.startswith("{"):
            facts.append(ImportFact(
                importing_file=file_path,
                imported_module=module,
                imported_name="default",
                alias=pre_brace,
                line=line,
            ))

        # Named imports inside braces
        for raw_item in named_part.split(","):
            item = raw_item.strip()
            if not item:
                continue
            alias_m = _re.match(r'^(\w+)\s+as\s+(\w+)$', item)
            if alias_m:
                facts.append(ImportFact(
                    importing_file=file_path,
                    imported_module=module,
                    imported_name=alias_m.group(1),
                    alias=alias_m.group(2),
                    line=line,
                ))
            else:
                ident = item.split()[0] if item.split() else item
                facts.append(ImportFact(
                    importing_file=file_path,
                    imported_module=module,
                    imported_name=ident,
                    alias=None,
                    line=line,
                ))
        return facts

    # Pure default import: import X from "module"
    if clause and not clause.startswith("{") and not clause.startswith("*"):
        # Could be a plain identifier
        ident = clause.split()[0]
        facts.append(ImportFact(
            importing_file=file_path,
            imported_module=module,
            imported_name="default",
            alias=ident,
            line=line,
        ))
        return facts

    return facts


def _extract_imports_typescript(file_path: str, content: str) -> list[ImportFact]:
    """Extract imports from TypeScript/JavaScript source using regex.

    Handles all standard ES module import forms plus CommonJS ``require()``.
    """
    facts: list[ImportFact] = []

    def _line(offset: int) -> int:
        return _offset_to_line(content, offset)

    # Side-effect imports: import "module";
    for m in _TS_SIDE_EFFECT_RE.finditer(content):
        facts.append(ImportFact(
            importing_file=file_path,
            imported_module=m.group(1),
            imported_name=None,
            alias=None,
            line=_line(m.start()),
        ))

    # Re-exports: export { X } from "module";
    for m in _TS_EXPORT_FROM_RE.finditer(content):
        module = m.group(2)
        line = _line(m.start())
        facts.extend(
            _ts_parse_import_clause("{" + m.group(1) + "}", module, file_path, line)
        )

    # import [...] from "module" — multiline-safe scanner
    seen_offsets: set[int] = set()
    for m in _TS_FULL_IMPORT_RE.finditer(content):
        start = m.start()
        if start in seen_offsets:
            continue
        clause = m.group(1).strip()
        module = m.group(2)
        line = _line(start)
        facts.extend(_ts_parse_import_clause(clause, module, file_path, line))
        seen_offsets.add(start)

    # CommonJS require: const X = require("module")
    for m in _TS_REQUIRE_RE.finditer(content):
        binding = m.group(1).strip()
        module = m.group(2)
        line = _line(m.start())
        if binding.startswith("{"):
            # Destructuring: const { A, B } = require('mod')
            inner = binding[1:binding.rfind("}")].strip()
            for raw_item in inner.split(","):
                item = raw_item.strip()
                if not item:
                    continue
                facts.append(ImportFact(
                    importing_file=file_path,
                    imported_module=module,
                    imported_name=item,
                    alias=None,
                    line=line,
                ))
        else:
            facts.append(ImportFact(
                importing_file=file_path,
                imported_module=module,
                imported_name=None,
                alias=binding,
                line=line,
            ))

    return facts


# ---------------------------------------------------------------------------
# Rust import extraction — helpers (module-level for performance)
# ---------------------------------------------------------------------------

_RUST_MOD_RE = _re.compile(
    r'^\s*(?:pub\s*(?:\([^)]*\)\s*)?)?\bmod\s+(\w+)\s*;', _re.MULTILINE
)
_RUST_USE_START_RE = _re.compile(r'\buse\b', _re.MULTILINE)


def _rust_find_use_body(text: str, start_offset: int) -> tuple[str, int] | None:
    """Return (body_str, end_offset) for a ``use`` statement."""
    depth = 0
    i = start_offset
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ";" and depth == 0:
            return text[start_offset:i].strip(), i
        i += 1
    return None


def _rust_matching_brace(text: str, open_idx: int) -> int:
    """Return the index of the ``}`` matching ``{`` at *open_idx*."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def _rust_split_top_level(text: str) -> list[str]:
    """Split *text* by top-level commas (not inside braces)."""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "{":
            depth += 1
            current.append(ch)
        elif ch == "}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        items.append("".join(current))
    return items


def _rust_join_path(prefix: str, suffix: str) -> str:
    """Join two Rust path segments with ``::``."""
    if prefix and suffix:
        return prefix + "::" + suffix
    return prefix or suffix


def _rust_parse_use_tree(
    prefix: str,
    body: str,
    file_path: str,
    line: int,
    result: list[ImportFact],
) -> None:
    """Recursively parse a Rust use-tree fragment into ``ImportFact``s."""
    body = body.strip()
    if not body:
        return

    brace_idx: int | None = None
    for idx, ch in enumerate(body):
        if ch == "{":
            brace_idx = idx
            break

    if brace_idx is not None:
        pre_raw = body[:brace_idx].rstrip(":").strip()
        new_prefix = _rust_join_path(prefix, pre_raw)

        brace_end = _rust_matching_brace(body, brace_idx)
        inner = body[brace_idx + 1:brace_end]

        for item in _rust_split_top_level(inner):
            item = item.strip()
            if not item:
                continue
            if item == "self":
                last_sep = new_prefix.rfind("::")
                if last_sep != -1:
                    result.append(ImportFact(
                        importing_file=file_path,
                        imported_module=new_prefix[:last_sep],
                        imported_name=new_prefix[last_sep + 2:],
                        alias=None,
                        line=line,
                    ))
                else:
                    result.append(ImportFact(
                        importing_file=file_path,
                        imported_module=new_prefix,
                        imported_name=None,
                        alias=None,
                        line=line,
                    ))
            else:
                _rust_parse_use_tree(new_prefix, item, file_path, line, result)
        return

    as_m = _re.match(r'^(.*?)\s+as\s+(\w+)$', body)
    if as_m:
        path_part = _rust_join_path(prefix, as_m.group(1).strip())
        alias = as_m.group(2)
    else:
        path_part = _rust_join_path(prefix, body)
        alias = None

    if path_part.endswith("::*"):
        result.append(ImportFact(
            importing_file=file_path,
            imported_module=path_part[:-3],
            imported_name="*",
            alias=alias,
            line=line,
        ))
        return

    last_sep = path_part.rfind("::")
    if last_sep == -1:
        result.append(ImportFact(
            importing_file=file_path,
            imported_module=path_part,
            imported_name=None,
            alias=alias,
            line=line,
        ))
    else:
        result.append(ImportFact(
            importing_file=file_path,
            imported_module=path_part[:last_sep],
            imported_name=path_part[last_sep + 2:],
            alias=alias,
            line=line,
        ))


def _extract_imports_rust(file_path: str, content: str) -> list[ImportFact]:
    """Extract imports from Rust source.

    Handles ``use`` declarations (plain, aliased, glob, grouped/nested),
    ``pub use``, ``pub(crate) use``, and ``mod name;`` declarations.
    """
    facts: list[ImportFact] = []

    def _line(offset: int) -> int:
        return _offset_to_line(content, offset)

    for m in _RUST_MOD_RE.finditer(content):
        facts.append(ImportFact(
            importing_file=file_path,
            imported_module=m.group(1),
            imported_name=None,
            alias=None,
            line=_line(m.start()),
        ))

    processed: set[int] = set()
    for m in _RUST_USE_START_RE.finditer(content):
        use_end = m.end()
        while use_end < len(content) and content[use_end] == " ":
            use_end += 1
        if use_end in processed:
            continue
        processed.add(use_end)
        result = _rust_find_use_body(content, use_end)
        if result is None:
            continue
        body_str, _end = result
        _rust_parse_use_tree("", body_str, file_path, _line(m.start()), facts)

    return facts


def _extract_imports(file_path: str, content: str) -> list[ImportFact]:
    """Extract import facts from *content*, dispatching by language.

    - Python files: tree-sitter via ``emend_core`` (falls back to ``ast.parse``)
    - TypeScript / JavaScript files: regex-based extraction
    - Rust files: regex-based extraction for ``use`` / ``mod`` declarations
    - All others: treated as Python (best-effort)
    """
    from emend.language_registry import detect_language
    lang = detect_language(file_path)
    if lang == "typescript":
        return _extract_imports_typescript(file_path, content)
    elif lang == "rust":
        return _extract_imports_rust(file_path, content)
    else:
        return _extract_imports_python(file_path, content)


def _build_cfg_facts(
    cfgs: list[Any],
    sym_facts: list[SymbolFact],
    rel_path: str,
    module_name: str,
) -> tuple[
    list[CfgBlockFact],
    list[CfgEdgeFact],
    list[SourceLocFact],
    list[tuple[str, int, int, int, bool]],  # block_ranges
]:
    """Build CFG block/edge/source-loc facts from a list of PyCfg objects.

    Returns ``(cfg_block_facts, cfg_edge_facts, block_loc_facts, block_ranges)``
    where *block_ranges* is sorted for ``_find_containing_block`` lookups.
    """
    block_ranges: list[tuple[str, int, int, int, bool]] = []
    cfg_block_facts: list[CfgBlockFact] = []
    cfg_edge_facts: list[CfgEdgeFact] = []

    for cfg in cfgs:
        func_name = cfg.func_name
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
            has_content = bool(
                block.get("statements")
                or block.get("defs")
                or block.get("uses")
            )
            block_ranges.append((func_qn, bid, block["start_line"] + 1, block["end_line"] + 1, has_content))

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

    block_loc_facts: list[SourceLocFact] = []
    for func_qn_br, bid_br, start_line_br, end_line_br, has_content_br in block_ranges:
        if start_line_br > 0 and has_content_br:
            block_loc_facts.append(SourceLocFact(
                file_path=rel_path,
                loc_kind="block",
                loc_id=f"{func_qn_br}:{bid_br}",
                line=start_line_br,
                end_line=end_line_br,
            ))

    block_ranges.sort(key=lambda x: (x[2], -(x[3] - x[2])))
    return cfg_block_facts, cfg_edge_facts, block_loc_facts, block_ranges


def _build_def_use_facts(
    cfgs: list[Any],
    sym_facts: list[SymbolFact],
    rel_path: str,
    module_name: str,
) -> list[DefUseFact]:
    """Extract def-use facts from CFG blocks.

    Covers only intra-function code; module-level def-use facts must be
    synthesised separately from scope-resolver references.
    """
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

        defs_map: dict[str, list[tuple[int, int, int, str]]] = {}
        for block in cfg.get_blocks():
            bid = block["id"]
            for d in block.get("defs", []) or []:
                var_name = d[0] if isinstance(d, (list, tuple)) else d
                dline = d[1] if isinstance(d, (list, tuple)) and len(d) > 1 else 0
                dcol = d[2] if isinstance(d, (list, tuple)) and len(d) > 2 else 0
                dkind = d[3] if isinstance(d, (list, tuple)) and len(d) > 3 else "write"
                defs_map.setdefault(var_name, []).append((bid, dline, dcol, dkind))

        for block in cfg.get_blocks():
            bid = block["id"]
            for u in block.get("uses", []) or []:
                var_name = u[0] if isinstance(u, (list, tuple)) else u
                uline = u[1] if isinstance(u, (list, tuple)) and len(u) > 1 else 0
                ucol = u[2] if isinstance(u, (list, tuple)) and len(u) > 2 else 0
                if var_name in defs_map:
                    for def_bid, dl, dc, dk in defs_map[var_name]:
                        def_use_facts.append(DefUseFact(
                            file_path=rel_path,
                            func_qn=func_qn,
                            var_name=var_name,
                            kind=dk,
                            def_block=def_bid,
                            use_block=bid,
                            def_line=dl,
                            def_col=dc,
                            use_line=uline,
                            use_col=ucol,
                        ))

    return def_use_facts


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------

def flows_from(source_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching taint flows whose source_var matches *source_pattern*."""
    compiled = re.compile(source_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TraceFlowFact) and compiled.search(fact.source_var) is not None

    return _predicate


def flows_to(sink_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching taint flows whose sink_var matches *sink_pattern*."""
    compiled = re.compile(sink_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TraceFlowFact) and compiled.search(fact.sink_var) is not None

    return _predicate


def _parse_effect(effect_str: str) -> tuple[str, str] | None:
    """Parse an effect predicate like ``writes($OBJ)`` into ``(kind, metavar)``.

    Returns ``None`` if the string is not a recognised effect form.
    """
    m = re.match(r"(writes|reads)\(\$(\w+)\)", effect_str)
    if m:
        return m.group(1), m.group(2)
    return None


def _compile_sequence_query(
    check: "SequenceCheck",
    step_locations: dict[str, list[tuple[str, str, int, int, dict[str, str]]]],
    blocker_locations: dict[tuple[str, str], dict[str, list[tuple[str, str, int]]]] | None = None,
) -> str | None:
    """Compile a sequence rule to a CozoScript query given pre-resolved step locations.

    Args:
        check: The ``SequenceCheck`` definition.
        step_locations: Mapping from step bind-name to a list of resolved
            location tuples ``(file_path, func_qn, block_id, line, bindings)``.
            For effect-based steps, the list may be empty (resolved in Datalog).
        blocker_locations: Optional mapping from ``(from_step, to_step)`` to
            dicts with ``"not_through"`` and ``"not_through_scope"`` keys, each
            a list of ``(file_path, func_qn, block_id)`` blocker locations.

    Returns:
        A CozoScript query string, or ``None`` if no step locations were resolved.
    """
    from emend.policy import SequenceCheck, SequenceStep

    steps = check.sequence
    if not steps or len(steps) < 2:
        return None

    # Build a path-constraint index keyed by (from_step, to_step)
    path_index: dict[tuple[str, str], Any] = {}
    for pc in check.path_constraints:
        path_index[(pc.from_step, pc.to_step)] = pc

    rules: list[str] = []

    # --- Step 0: Build inline relations for resolved step locations ---
    any_locations = False
    for step in steps:
        locs = step_locations.get(step.bind, [])
        if step.effect:
            # Effect-based steps are resolved in Datalog; we still need
            # the earlier step's bindings to know which variable to track.
            continue
        if not locs:
            continue
        any_locations = True
        rules.append(FactGraph._inline_relation(
            f'step_{step.bind}', ["fp", "fq", "block", "line"],
            [(fp, fq, bid, line) for fp, fq, bid, line, _bindings in locs],
        ).rstrip("\n"))

    if not any_locations:
        return None

    # --- Step 1: Resolve effect-based steps ---
    # For each effect step, look up the effect kind and the bound metavar
    # from the preceding step's bindings.
    # Track which metavar each effect step references (for liveness filtering).
    effect_bound_vars: dict[int, str] = {}  # step_idx -> bound variable name

    for i, step in enumerate(steps):
        if not step.effect:
            continue
        parsed = _parse_effect(step.effect)
        if parsed is None:
            continue
        effect_kind, metavar_name = parsed

        # Find the binding for this metavar from earlier steps
        bound_var = None
        for j in range(i - 1, -1, -1):
            prev_step = steps[j]
            prev_locs = step_locations.get(prev_step.bind, [])
            for _fp, _fq, _bid, _line, bindings in prev_locs:
                if metavar_name in bindings:
                    bound_var = bindings[metavar_name]
                    break
            if bound_var:
                break

        if bound_var is None:
            continue

        effect_bound_vars[i] = bound_var

        # Collect source step blocks to exclude self-triggering.
        # The preceding step's block should not count as a mutation site.
        prev_step = steps[i - 1]
        prev_locs = step_locations.get(prev_step.bind, [])
        excl_name = f"excl_src_{i}"
        rules.append(FactGraph._inline_relation(
            excl_name, ["fp", "fq", "block"],
            [(fp, fq, bid) for fp, fq, bid, _line, _bindings in prev_locs],
        ).rstrip("\n"))

        # Generate Datalog rules to resolve writes/reads for the bound variable
        if effect_kind == "writes":
            rules.append('mutate_kind[k] <- [["write"], ["aug_write"]]')

            # Variant 1: exact var match (exclude source blocks)
            rules.append(
                f'step_{step.bind}[fp, fq, block, line] := '
                f'*def_use[fp, fq, "{bound_var}", kind, block, _, line, _, _, _], '
                f'mutate_kind[kind], '
                f'not {excl_name}[fp, fq, block]'
            )
            # Variant 2: dotted attribute match (e.g. obj.name = ...)
            rules.append(
                f'step_{step.bind}[fp, fq, block, line] := '
                f'*def_use[fp, fq, var_name, kind, block, _, line, _, _, _], '
                f'mutate_kind[kind], '
                f'starts_with(var_name, "{bound_var}."), '
                f'not {excl_name}[fp, fq, block]'
            )
            # Variant 3: method call on the variable (e.g. obj.append())
            rules.append(
                f'step_{step.bind}[fp, fq, block, line] := '
                f'*method_call[fp, fq, "{bound_var}", _, block, line], '
                f'not {excl_name}[fp, fq, block]'
            )
        elif effect_kind == "reads":
            rules.append(
                f'step_{step.bind}[fp, fq, block, line] := '
                f'*def_use[fp, fq, "{bound_var}", "read", block, _, line, _, _, _], '
                f'not {excl_name}[fp, fq, block]'
            )

    # --- Step 2: CFG reachability between consecutive steps ---
    for i in range(len(steps) - 1):
        from_step = steps[i]
        to_step = steps[i + 1]
        pair_key = (from_step.bind, to_step.bind)
        reach_name = f"reachable_{i}"

        # Blocker relations for this pair
        nt_blocks: list[tuple[str, str, int]] = []
        scope_kills: list[tuple[str, str, int]] = []
        if blocker_locations and pair_key in blocker_locations:
            bl = blocker_locations[pair_key]
            nt_blocks = bl.get("not_through", [])
            scope_kills = bl.get("not_through_scope", [])

        # Build blocker and scope kill inline relations
        blocker_name = f"blocker_{i}"
        sk_name = f"scope_kill_{i}"
        _3cols = ["fp", "fq", "block"]
        rules.append(FactGraph._inline_relation(blocker_name, _3cols, nt_blocks).rstrip("\n"))
        rules.append(FactGraph._inline_relation(sk_name, _3cols, scope_kills).rstrip("\n"))

        # Base case: reachable from the "from" step's block
        rules.append(
            f'{reach_name}[fp, fq, block] := '
            f'step_{from_step.bind}[fp, fq, block, _]'
        )

        # Recursive case: propagate along CFG edges, blocked by not_through and scope_kill
        rules.append(
            f'{reach_name}[fp, fq, to_block] := '
            f'{reach_name}[fp, fq, from_block], '
            f'*cfg_edge[fp, fq, from_block, to_block, _, _, _], '
            f'not {blocker_name}[fp, fq, from_block], '
            f'not {sk_name}[fp, fq, from_block], '
            f'not {blocker_name}[fp, fq, to_block], '
            f'not {sk_name}[fp, fq, to_block]'
        )

    # --- Step 3: Def-use liveness for bound variables ---
    # For each pair of consecutive steps, check liveness ONLY for variables
    # that are actually referenced in the later step (via effect or shared
    # metavar in pattern).  This avoids requiring liveness for incidental
    # captures like model names.
    #
    # IMPORTANT: liveness is checked from the ORIGINAL definition site (the
    # step that first bound the variable) to the current step, not from the
    # immediately preceding step.  For a 3-step sequence A→B→C where var
    # is bound at A, we need def_use(A→B) and def_use(A→C).
    liveness_vars: dict[int, set[str]] = {}  # pair_index -> set of var names
    # Map variable name → step index where it was first bound
    var_origin_step: dict[str, int] = {}

    for i in range(len(steps) - 1):
        from_step = steps[i]
        to_step = steps[i + 1]
        from_locs = step_locations.get(from_step.bind, [])

        # Track which step originally defines each variable
        if from_locs:
            for _fp, _fq, _bid, _line, bindings in from_locs:
                for mvar, val in bindings.items():
                    if val not in var_origin_step:
                        var_origin_step[val] = i

        vars_for_pair: set[str] = set()

        # If the to_step has an effect, the effect's metavar binding is
        # the variable that must be live.
        if to_step.effect and (i + 1) in effect_bound_vars:
            vars_for_pair.add(effect_bound_vars[i + 1])

        # If the to_step is pattern-based and shares metavar names with
        # any earlier step, those shared bindings must be live.
        if to_step.pattern:
            to_locs = step_locations.get(to_step.bind, [])
            # Collect all metavar bindings from all previous steps
            all_prev_metavars: dict[str, str] = {}
            for j in range(i + 1):
                for _fp, _fq, _bid, _line, bindings in step_locations.get(steps[j].bind, []):
                    all_prev_metavars.update(bindings)
            to_metavars: set[str] = set()
            for _fp, _fq, _bid, _line, bindings in to_locs:
                to_metavars.update(bindings.keys())
            for mvar in all_prev_metavars:
                if mvar in to_metavars:
                    vars_for_pair.add(all_prev_metavars[mvar])

        if not vars_for_pair:
            liveness_vars[i] = set()
            continue

        liveness_vars[i] = vars_for_pair

        # For each bound variable, generate a liveness check from its
        # original definition step to the current target step.
        for var in vars_for_pair:
            origin_idx = var_origin_step.get(var, i)
            origin_step = steps[origin_idx]
            live_name = f"live_{i}_{var}"
            rules.append(
                f'{live_name}[fp, fq, origin_block, to_block] := '
                f'step_{origin_step.bind}[fp, fq, origin_block, _], '
                f'*def_use[fp, fq, "{var}", _, origin_block, to_block, _, _, _, _]'
            )

    # --- Step 4: Final violation query ---
    # Join all step locations with reachability and liveness constraints.
    join_clauses: list[str] = []
    output_cols: list[str] = ["fp", "fq"]

    # First step
    first_step = steps[0]
    join_clauses.append(f'step_{first_step.bind}[fp, fq, block_0, line_0]')
    output_cols.append("line_0")

    for i in range(1, len(steps)):
        step = steps[i]
        reach_name = f"reachable_{i - 1}"
        block_var = f"block_{i}"
        line_var = f"line_{i}"

        # Step location
        join_clauses.append(f'step_{step.bind}[fp, fq, {block_var}, {line_var}]')
        output_cols.append(line_var)

        # Reachability from previous step
        join_clauses.append(f'{reach_name}[fp, fq, {block_var}]')

        # Same-function constraint is implicit: all step relations share fp, fq

        # Liveness constraints for variables referenced in this step
        pair_idx = i - 1
        vars_to_check = liveness_vars.get(pair_idx, set())
        for var in vars_to_check:
            origin_idx = var_origin_step.get(var, pair_idx)
            live_name = f"live_{pair_idx}_{var}"
            join_clauses.append(
                f'{live_name}[fp, fq, block_{origin_idx}, {block_var}]'
            )

    # Ensure temporal ordering: each step's line must be >= the previous
    for i in range(1, len(steps)):
        join_clauses.append(f'line_{i} >= line_{i - 1}')

    # Rename first_line / last_line for the output
    first_line = "line_0"
    last_line = f"line_{len(steps) - 1}"

    # Build the final query
    all_output = ", ".join(output_cols)
    # Alias first/last line in the output header
    query_output = f"fp, fq, {first_line} as first_line, {last_line} as last_line"

    # CozoScript doesn't support "as" aliases in the output — use positional
    # We'll just output all step lines plus fp and fq
    rules.append(
        f'?[fp, fq, first_line, last_line] := '
        + ", ".join(join_clauses)
        + f', first_line = {first_line}'
        + f', last_line = {last_line}'
    )

    return "\n".join(rules)


def compile_sequence_rule(
    graph: "FactGraph",
    check: "SequenceCheck",
    project_path: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Compile a temporal sequence rule to a CozoScript query.

    Resolves each step via pattern matching (Python side), then compiles
    to a CozoScript Datalog query for CFG-reachability and def-use liveness
    checks.

    Args:
        graph: The populated ``FactGraph`` for the project.
        check: The ``SequenceCheck`` definition.
        project_path: Project root directory for reading source files.
            If ``None``, attempts to resolve from the graph.

    Returns:
        Tuple of ``(cozoscript_query, step_data)`` where *step_data* is
        metadata about resolved steps, or ``None`` if no matches found.
    """
    from emend.policy import SequenceCheck

    steps = check.sequence
    if not steps or len(steps) < 2:
        return None

    # Collect all file paths from the graph's symbol table
    sym_result = graph.run_query(
        "?[fp] := *symbol[_, fp, _, _, _, _, _]"
    )
    file_paths = sorted({r[0] for r in sym_result["rows"]})
    if not file_paths:
        return None

    # Resolve the project root for reading files
    resolve_root: Path | None = None
    if project_path:
        resolve_root = Path(project_path).resolve()

    # --- Step resolution: match patterns against project files ---
    step_locations: dict[str, list[tuple[str, str, int, int, dict[str, str]]]] = {
        step.bind: [] for step in steps
    }
    blocker_locations: dict[tuple[str, str], dict[str, list[tuple[str, str, int]]]] = {}

    # Collect CFG block ranges from the graph for block-id resolution
    block_ranges_result = graph.run_query(
        "?[fp, fq, bid, start_line, end_line] := "
        "*source_loc[fp, lk, lid, start_line, _, end_line, _], "
        'lk == "symbol", '
        "*cfg_block[fp2, fq, bid, _, _], "
        "fp == fp2"
    )

    # Build block ranges from cfg_block + source_loc for line→block resolution
    # Fallback: use def_use facts to infer block line ranges
    _block_line_ranges: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
    # Gather block info from def_use facts (which have line info)
    du_result = graph.run_query(
        "?[fp, fq, bid, line] := *def_use[fp, fq, _, _, bid, _, line, _, _, _]"
    )
    for r in du_result["rows"]:
        key = (r[0], r[1])
        if key not in _block_line_ranges:
            _block_line_ranges[key] = []
        _block_line_ranges[key].append((r[2], r[3], r[3]))

    # Also from cfg_edge (which has from_line/to_line)
    edge_result = graph.run_query(
        "?[fp, fq, fb, fl, tb, tl] := *cfg_edge[fp, fq, fb, tb, _, fl, tl]"
    )
    for r in edge_result["rows"]:
        key = (r[0], r[1])
        if key not in _block_line_ranges:
            _block_line_ranges[key] = []
        if r[3] > 0:  # from_line
            _block_line_ranges[key].append((r[2], r[3], r[3]))
        if r[5] > 0:  # to_line
            _block_line_ranges[key].append((r[4], r[5], r[5]))

    # Build func line ranges from symbol facts
    func_ranges: dict[str, list[tuple[str, str, int, int]]] = {}
    sym_all = graph.run_query(
        "?[fp, qn, kind, line, end_line] := "
        "*symbol[qn, fp, _, kind, line, end_line, _]"
    )
    for r in sym_all["rows"]:
        fp, qn, kind, line, end_line = r
        if kind in ("function", "method", "async_function"):
            if fp not in func_ranges:
                func_ranges[fp] = []
            func_ranges[fp].append((fp, qn, line, end_line))

    def _find_func_for_line(fp: str, line: int) -> str:
        """Find the innermost function containing a given line."""
        candidates = func_ranges.get(fp, [])
        best_qn = ""
        best_span = float("inf")
        for _, qn, start, end in candidates:
            if start <= line <= end:
                span = end - start
                if span < best_span:
                    best_qn = qn
                    best_span = span
        return best_qn

    def _find_block_for_line(fp: str, fq: str, line: int) -> int:
        """Find the CFG block containing a given line."""
        key = (fp, fq)
        ranges = _block_line_ranges.get(key, [])
        best_bid = -1
        best_dist = float("inf")
        for bid, bline, _ in ranges:
            dist = abs(bline - line)
            if dist < best_dist:
                best_dist = dist
                best_bid = bid
        return best_bid

    # Track metavar bindings across steps for substitution
    accumulated_bindings: dict[str, str] = {}

    for step_idx, step in enumerate(steps):
        if step.effect:
            # Effect-based steps are resolved in Datalog — skip pattern matching.
            # But we still need to record which metavar they reference.
            continue

        if not step.pattern:
            continue

        # Substitute accumulated bindings into the pattern
        resolved_pattern = step.pattern
        for mvar, val in accumulated_bindings.items():
            resolved_pattern = resolved_pattern.replace(f"${mvar}", val)

        try:
            from emend.transform import find_pattern
        except ImportError:
            logger.debug("Cannot import find_pattern for sequence step resolution")
            continue

        for fp in file_paths:
            # Resolve the absolute file path for reading
            abs_path = fp
            if resolve_root:
                candidate = resolve_root / fp
                if candidate.exists():
                    abs_path = str(candidate)

            try:
                source = Path(abs_path).read_text(encoding="utf-8")
            except Exception:
                continue

            try:
                matches = find_pattern(
                    resolved_pattern,
                    abs_path,
                    source_override=source,
                    language="python",
                )
            except Exception:
                logger.debug("Pattern match failed for %s in %s", resolved_pattern, fp, exc_info=True)
                continue

            for m in matches:
                if m.line is None:
                    continue
                fq = _find_func_for_line(fp, m.line)
                if not fq:
                    continue  # Skip module-level matches
                bid = _find_block_for_line(fp, fq, m.line)

                bindings = dict(m.captures) if m.captures else {}
                step_locations[step.bind].append(
                    (fp, fq, bid, m.line, bindings)
                )

                # Accumulate bindings for later steps
                for k, v in bindings.items():
                    if k not in accumulated_bindings:
                        accumulated_bindings[k] = v

    # --- Blocker resolution ---
    def _resolve_blockers(
        patterns: list[str],
        target: list[tuple[str, str, int]],
    ) -> None:
        """Resolve blocker patterns to (file, func, block) locations."""
        from emend.transform import find_pattern
        for pattern in patterns:
            resolved = pattern
            for mvar, val in accumulated_bindings.items():
                resolved = resolved.replace(f"${mvar}", val)
            try:
                for fp in file_paths:
                    abs_path = fp
                    if resolve_root:
                        candidate = resolve_root / fp
                        if candidate.exists():
                            abs_path = str(candidate)
                    try:
                        source = Path(abs_path).read_text(encoding="utf-8")
                    except Exception:
                        continue
                    matches = find_pattern(resolved, abs_path, source_override=source, language="python")
                    for m in matches:
                        if m.line is None:
                            continue
                        fq = _find_func_for_line(fp, m.line)
                        if fq:
                            bid = _find_block_for_line(fp, fq, m.line)
                            target.append((fp, fq, bid))
            except Exception:
                pass

    for pc in check.path_constraints:
        pair_key = (pc.from_step, pc.to_step)
        bl: dict[str, list[tuple[str, str, int]]] = {
            "not_through": [],
            "not_through_scope": [],
        }
        _resolve_blockers(pc.not_through, bl["not_through"])
        _resolve_blockers(pc.not_through_scope, bl["not_through_scope"])
        blocker_locations[pair_key] = bl

    # --- Compile to CozoScript ---
    query = _compile_sequence_query(check, step_locations, blocker_locations)
    if query is None:
        return None

    step_data = {
        "step_locations": {
            k: [(fp, fq, bid, line) for fp, fq, bid, line, _ in locs]
            for k, locs in step_locations.items()
        },
        "blocker_locations": {
            f"{k[0]}->{k[1]}": v for k, v in blocker_locations.items()
        },
        "bindings": accumulated_bindings,
    }
    return query, step_data


def symbol_has_type(type_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching TypeFacts whose type_str matches *type_pattern*."""
    compiled = re.compile(type_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TypeFact) and compiled.search(fact.type_str) is not None

    return _predicate
