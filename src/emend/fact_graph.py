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

import hashlib
import json
import logging
import posixpath
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from emend.errors import BUG_EXCEPTIONS
from emend.analysis_extraction import (
    _bfs_reachable_blocks,
    _build_method_call_facts,
    _build_symbol_line_index,
    _enclosing_symbol,
    _extract_file_facts,
    _extract_imports,
    _extract_imports_python,
    _extract_imports_rust,
    _extract_imports_typescript,
    _find_containing_block,
    _map_ref_kind,
    _normalize_qn,
    _resolve_cfg_func_qn,
    _walk_symbols,
    build_def_use_facts,
)
from emend.analysis_snapshot import (
    AnalysisSnapshot,
    CallFact,
    CfgBlockFact,
    CfgEdgeFact,
    DecoratorOnFact,
    DefUseFact,
    EntryPointDecoratorFact,
    EntryPointNameFact,
    ExtractedFile,
    ExportedSymbolFact,
    Fact,
    FileRevision,
    FlowEdgeFact,
    FlowEventFact,
    FuncSummaryFact,
    ImportFact,
    MethodCallFact,
    ReferenceFact,
    SourceLocFact,
    SymbolFact,
    TraceFlowFact,
    TypeFact,
)

if TYPE_CHECKING:
    from emend.policy import SequenceCheck

logger = logging.getLogger(__name__)

FACT_GRAPH_SCHEMA_VERSION = "10"


@dataclass(frozen=True)
class DeadSymbolFact(SymbolFact):
    """A query result, not a stored fact; empty causes identify a direct root."""

    root_causes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------


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

{:create search_symbol {
    file_path: String,
    module_qualified_name: String
    =>
    name: String,
    qualified_name: String,
    kind: String,
    line: Int,
    end_line: Int,
    depth: Int,
    parent: String default "",
    signature: String default "",
    returns: String default "",
    decorators: String default ""
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

{:create flow_event {
    file_path: String,
    event_id: Int
    =>
    func_id: String,
    func_name: String,
    func_start: Int,
    role: String,
    var: String,
    access_path: String,
    block: Int,
    start_byte: Int,
    end_byte: Int,
    start_line: Int,
    start_col: Int,
    end_line: Int,
    end_col: Int,
    ordinal: Int,
    call_id: Int default -1,
    arg_index: Int default -1,
    arg_name: String default "",
    text: String default ""
}}

{:create flow_edge {
    file_path: String,
    from_event: Int,
    to_event: Int,
    edge_kind: String
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

{:create exported_symbol {
    file_path: String,
    qualified_name: String
}}

{:create file_revision {
    file_path: String
    =>
    content_hash: String,
    language: String,
    module_name: String,
    origin: String,
    version: Int default -1
}}

{:create ref_by_block {
    file_path: String,
    func_qn: String,
    block_id: Int,
    symbol_qn: String
}}

{:create noncall_private_member_ref {
    file_path: String,
    func_qn: String,
    block_id: Int,
    member_name: String
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

{:create facts_meta {
    key: String
    =>
    value: String
}}

"""


def _init_schema(client: Any) -> None:
    """Create stored relations if they don't exist."""
    for stmt in _SCHEMA_INIT.strip().split("\n\n"):
        stmt = stmt.strip()
        if stmt:
            try:
                client.run(stmt)
            except Exception as exc:
                # Relation already exists — that's fine.
                if "conflicts with an existing one" not in str(exc):
                    raise


# ---------------------------------------------------------------------------
# FactGraph — CozoDB-backed
# ---------------------------------------------------------------------------

class FactGraph:
    """Queryable store of code facts, backed by CozoDB (SQLite engine).

    All queries are executed as CozoScript. The Python dataclass types
    are preserved as the public API for callers that need typed results.
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        snapshot: AnalysisSnapshot | None = None,
        source_overrides: dict[str, str] | None = None,
        source_loader: Callable[[FileRevision], str | None] | None = None,
    ) -> None:
        self._db_path = db_path
        self._client = _create_cozo_client(db_path)
        self._snapshot = snapshot
        self._source_overrides = dict(source_overrides or {})
        self._source_loader = source_loader
        # Detached compatibility graphs may own a private snapshot copy. The
        # analysis owner, by contrast, keeps its generation files alive.
        self._close_unlinks_db = False
        self._revisions_by_path = {
            revision.file_path: revision for revision in snapshot.files
        } if snapshot is not None else {}
        _init_schema(self._client)

    @property
    def snapshot(self) -> AnalysisSnapshot:
        """The immutable source generation this graph represents."""
        if self._snapshot is None:
            raise RuntimeError("FactGraph is not bound to an analysis snapshot")
        return self._snapshot

    def bind_snapshot(
        self,
        snapshot: AnalysisSnapshot,
        *,
        source_overrides: dict[str, str] | None = None,
        source_loader: Callable[[FileRevision], str | None] | None = None,
    ) -> None:
        """Bind this store to a successfully-published source generation."""
        self._snapshot = snapshot
        self._source_overrides = {
            str(Path(path).resolve()): content
            for path, content in (source_overrides or {}).items()
        }
        self._source_loader = source_loader
        self._revisions_by_path = {
            revision.file_path: revision for revision in snapshot.files
        }

    def source_text(self, file_path: str | Path) -> str:
        """Read source from this graph's overlay, falling back to disk."""
        resolved = str(Path(file_path).resolve())
        if resolved in self._source_overrides:
            return self._source_overrides[resolved]
        revision = self._revisions_by_path.get(resolved)
        try:
            content = Path(resolved).read_text(encoding="utf-8")
        except OSError:
            content = None
        if content is not None and (
            revision is None
            or hashlib.sha256(content.encode()).hexdigest() == revision.content_hash
        ):
            return content
        if revision is not None and self._source_loader is not None:
            preserved = self._source_loader(revision)
            if preserved is not None:
                return preserved
        if revision is None and content is None:
            raise FileNotFoundError(resolved)
        raise RuntimeError(f"source no longer matches snapshot: {resolved}")

    def stored_path(self, file_path: str | Path) -> str:
        """Return the canonical fact path for this graph's project."""
        resolved = Path(file_path).resolve()
        if self._snapshot is not None:
            try:
                return str(resolved.relative_to(Path(self._snapshot.project_root)))
            except ValueError:
                pass
        return str(resolved)

    def published_snapshot_id(self) -> str | None:
        """Return the generation marker stored alongside the fact relations."""
        try:
            rows = self._client.run(
                '?[value] := *facts_meta["snapshot_id", value]'
            )["rows"]
        except Exception:
            return None
        return str(rows[0][0]) if rows else None

    def published_snapshot(self, project_root: str | Path) -> AnalysisSnapshot | None:
        """Load the source inventory published with this graph."""
        try:
            schema_rows = self._client.run(
                '?[value] := *facts_meta["schema_version", value]'
            )["rows"]
        except Exception:
            return None
        if schema_rows != [[FACT_GRAPH_SCHEMA_VERSION]]:
            return None
        snapshot_id = self.published_snapshot_id()
        if snapshot_id is None:
            return None
        try:
            rows = self._client.run(
                "?[fp, hash, lang, module, origin, version] := "
                "*file_revision[fp, hash, lang, module, origin, version]"
            )["rows"]
        except Exception:
            return None
        revisions = tuple(
            FileRevision.create(
                project_root, row[0], row[1], row[2], row[3],
                origin=row[4], version=None if row[5] == -1 else row[5],
            )
            for row in rows
        )
        context_rows = self._client.run(
            '?[value] := *facts_meta["analysis_context_id", value]'
        )["rows"]
        return AnalysisSnapshot(
            str(Path(project_root).resolve()), snapshot_id, revisions,
            analysis_context_id=(str(context_rows[0][0]) if context_rows else ""),
        )

    def publish_snapshot(self, snapshot: AnalysisSnapshot) -> None:
        """Mark a generation current after all relation mutations succeed."""
        self._client.run(
            "?[file_path, content_hash, language, module_name, origin, version] <- $rows "
            ":replace file_revision {file_path => content_hash, language, module_name, "
            "origin, version}",
            {"rows": [
                [revision.file_path, revision.content_hash, revision.language,
                 revision.module_name, revision.origin,
                 -1 if revision.version is None else revision.version]
                for revision in snapshot.files
            ]},
        )
        self._client.run(
            "?[key, value] <- $rows :put facts_meta {key => value}",
            {"rows": [
                ["schema_version", FACT_GRAPH_SCHEMA_VERSION],
                ["project_root", snapshot.project_root],
                ["snapshot_id", snapshot.snapshot_id],
                ["analysis_context_id", snapshot.analysis_context_id],
            ]},
        )
        self.bind_snapshot(snapshot)

    def clear_snapshot_marker(self) -> None:
        """Make a graph unpublishable before an in-place delta starts."""
        self._client.run(
            '?[key] := *facts_meta[key, _], key == "snapshot_id" '
            ':rm facts_meta {key => }'
        )

    @property
    def client(self) -> Any:
        """Expose the underlying CozoDB client for raw CozoScript queries."""
        return self._client

    def run_query(self, cozoscript: str) -> dict[str, Any]:
        """Execute a read-only CozoScript query and return the result dict.

        The result has keys ``headers`` (list of column names) and
        ``rows`` (list of row tuples).
        """
        return self._client.run(cozoscript, read_only=True)

    def close(self) -> None:
        """Close the underlying database connection."""
        db_path = self._db_path if self._close_unlinks_db else None
        try:
            if self._client is not None:
                self._client.close()
            self._client = None
        except Exception:
            logger.debug("Failed to close CozoDB client", exc_info=True)
        finally:
            if db_path is not None:
                try:
                    Path(db_path).unlink(missing_ok=True)
                except OSError:
                    logger.debug("Failed to remove detached graph %s", db_path,
                                 exc_info=True)

    # -- Mutation ---------------------------------------------------------

    def add_symbol(self, fact: SymbolFact) -> None:
        """Add a symbol definition fact."""
        self.add_symbols_batch([fact])

    def add_call(self, fact: CallFact) -> None:
        """Add a call relationship fact."""
        self.add_calls_batch([fact])

    def add_reference(self, fact: ReferenceFact) -> None:
        """Add a reference fact."""
        self.add_references_batch([fact])

    def add_trace_flow(self, fact: TraceFlowFact) -> None:
        """Add a taint flow fact."""
        self.add_trace_flows_batch([fact])

    def add_type(self, fact: TypeFact) -> None:
        """Add a type binding fact."""
        self.add_types_batch([fact])

    def add_types_batch(self, facts: list[TypeFact]) -> None:
        """Bulk-insert type binding facts."""
        if not facts:
            return
        rows = [[f.symbol_qn, f.file_path, f.line, f.binding_kind, f.type_str] for f in facts]
        self._put_batch(
            "type_binding",
            "symbol_qn, file_path, line, binding_kind, type_str",
            "symbol_qn, file_path, line, binding_kind => type_str",
            rows,
        )

    def replace_types_batch(self, facts: list[TypeFact]) -> None:
        """Replace the type facts in this graph with one inference result.

        Type facts are a derived view, rather than part of syntax extraction,
        so they are materialized independently on a copy of a source graph.
        Keeping the replacement atomic prevents a consumer from observing a
        mixture of two analyzer generations.
        """
        operations: list[tuple[str, dict[str, Any]]] = [(
            "?[symbol_qn, file_path, line, binding_kind] := "
            "*type_binding[symbol_qn, file_path, line, binding_kind, _] "
            ":rm type_binding {symbol_qn, file_path, line, binding_kind => }",
            {},
        )]
        if facts:
            self._put_batch(
                "type_binding",
                "symbol_qn, file_path, line, binding_kind, type_str",
                "symbol_qn, file_path, line, binding_kind => type_str",
                [[f.symbol_qn, f.file_path, f.line, f.binding_kind, f.type_str]
                 for f in facts],
                operations,
            )
        self._run_mutations(operations)

    def add_trace_flows_batch(self, facts: list[TraceFlowFact]) -> None:
        """Bulk-insert taint flow facts."""
        if not facts:
            return
        rows = [
            [f.source_var, f.sink_var, f.label, f.file_path, f.func_qn,
             f.source_line, f.sink_line]
            for f in facts
        ]
        self._put_batch(
            "trace_flow",
            "source_var, sink_var, label, file_path, func_qn, source_line, sink_line",
            "source_var, sink_var, label, file_path, func_qn, source_line, sink_line",
            rows,
        )

    def add_import(self, fact: ImportFact) -> None:
        """Add an import fact."""
        self.add_imports_batch([fact])

    def add_cfg_edge(self, fact: CfgEdgeFact) -> None:
        """Add a control flow edge fact."""
        self.add_cfg_edges_batch([fact])

    def add_flow_event(self, fact: FlowEventFact) -> None:
        """Add one value-flow occurrence fact."""
        self.add_flow_events_batch([fact])

    def add_flow_events_batch(self, facts: list[FlowEventFact]) -> None:
        """Bulk-insert value-flow occurrence facts."""
        if not facts:
            return
        rows = [[
            f.file_path, f.event_id, f.func_id, f.func_name, f.func_start,
            f.role, f.var or "", f.access_path or "", f.block, f.start_byte, f.end_byte,
            f.start_line, f.start_col, f.end_line, f.end_col,
            f.ordinal, -1 if f.call_id is None else f.call_id,
            -1 if f.arg_index is None else f.arg_index,
            f.arg_name or "", f.text,
        ] for f in facts]
        self._put_batch(
            "flow_event",
            "file_path, event_id, func_id, func_name, func_start, role, var, access_path, "
            "block, start_byte, end_byte, start_line, start_col, end_line, "
            "end_col, ordinal, call_id, arg_index, arg_name, text",
            "file_path, event_id => func_id, func_name, func_start, role, var, access_path, "
            "block, start_byte, end_byte, start_line, start_col, end_line, "
            "end_col, ordinal, call_id, arg_index, arg_name, text",
            rows,
        )

    def add_flow_edge(self, fact: FlowEdgeFact) -> None:
        """Add one directed value-flow edge fact."""
        self.add_flow_edges_batch([fact])

    def add_flow_edges_batch(self, facts: list[FlowEdgeFact]) -> None:
        """Bulk-insert value-flow edges.

        ``edge_kind`` is intentionally part of the Cozo key: two occurrences
        can have more than one semantic relationship between them.
        """
        if not facts:
            return
        rows = [[f.file_path, f.from_event, f.to_event, f.edge_kind] for f in facts]
        self._put_batch(
            "flow_edge",
            "file_path, from_event, to_event, edge_kind",
            "file_path, from_event, to_event, edge_kind",
            rows,
        )

    def add_def_use(self, fact: DefUseFact) -> None:
        """Add a definition-use fact."""
        self.add_def_uses_batch([fact])

    # -- Batch mutation (for build_from_project performance) ---------------

    def _put_batch(
        self,
        relation: str,
        cols: str,
        schema: str,
        rows: list[list[Any]],
        operations: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        """Run ``?[<cols>] <- $rows :put <relation> {<schema>}``.

        Caller is responsible for skipping empty inserts.
        """
        operation = (
            f"?[{cols}] <- $rows :put {relation} {{{schema}}}",
            {"rows": rows},
        )
        if operations is None:
            self._client.run(*operation)
        else:
            operations.append(operation)

    def _run_mutations(
        self, operations: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """Run generated mutations in Cozo's synchronous atomic script."""
        queries, bindings = [], {}
        for index, (query, params) in enumerate(operations):
            # These internal statements contain only parameter uses of '$'.
            prefix = f"mutation_{index}_"
            queries.append("{" + query.replace("$", "$" + prefix) + "}")
            bindings.update((prefix + key, value) for key, value in params.items())
        if queries:
            self._client.run("\n".join(queries), bindings)

    def add_symbols_batch(self, facts: list[SymbolFact]) -> None:
        """Bulk-insert symbol facts."""
        if not facts:
            return
        rows = [[f.qualified_name, f.file_path, f.name, f.kind, f.line, f.end_line, f.parent or ""] for f in facts]
        cols = "qualified_name, file_path, name, kind, line, end_line, parent"
        self._put_batch("symbol", cols, "qualified_name => file_path, name, kind, line, end_line, parent", rows)

    def add_calls_batch(self, facts: list[CallFact]) -> None:
        """Bulk-insert call facts."""
        if not facts:
            return
        rows = [[f.caller_qn, f.callee_qn, f.file_path, f.line, f.col, f.func_qn, f.block_id] for f in facts]
        reverse_rows = [[f.callee_qn, f.caller_qn, f.file_path, f.line, f.col, f.func_qn, f.block_id] for f in facts]
        file_rows = [[f.file_path, f.caller_qn, f.callee_qn, f.line, f.col, f.func_qn, f.block_id] for f in facts]
        self._put_batch(
            "call",
            "caller_qn, callee_qn, file_path, line, col, func_qn, block_id",
            "caller_qn, callee_qn, file_path, line, col => func_qn, block_id",
            rows,
        )
        self._put_batch(
            "call_by_callee",
            "callee_qn, caller_qn, file_path, line, col, func_qn, block_id",
            "callee_qn, caller_qn, file_path, line, col => func_qn, block_id",
            reverse_rows,
        )
        self._put_batch(
            "call_by_file",
            "file_path, caller_qn, callee_qn, line, col, func_qn, block_id",
            "file_path, caller_qn, callee_qn, line, col => func_qn, block_id",
            file_rows,
        )

    def add_references_batch(self, facts: list[ReferenceFact]) -> None:
        """Bulk-insert reference facts."""
        if not facts:
            return
        rows = [[f.symbol_qn, f.file_path, f.line, f.col, f.ref_kind, f.func_qn, f.block_id] for f in facts]
        self._put_batch(
            "reference",
            "symbol_qn, file_path, line, col, ref_kind, func_qn, block_id",
            "symbol_qn, file_path, line, col => ref_kind, func_qn, block_id",
            rows,
        )
        module_rows = [[f.symbol_qn, f.file_path, f.line] for f in facts if f.func_qn == "" and f.block_id == -1]
        if module_rows:
            self._put_batch("module_level_ref", "symbol_qn, file_path, line", "symbol_qn, file_path, line", module_rows)

    def add_imports_batch(self, facts: list[ImportFact]) -> None:
        """Bulk-insert import facts."""
        if not facts:
            return
        rows = [[f.importing_file, f.imported_module, f.imported_name or "", f.line, f.alias or ""] for f in facts]
        self._put_batch(
            "import",
            "importing_file, imported_module, imported_name, line, alias",
            "importing_file, imported_module, imported_name, line => alias",
            rows,
        )

    def add_cfg_edges_batch(self, facts: list[CfgEdgeFact]) -> None:
        """Bulk-insert CFG edge facts."""
        if not facts:
            return
        rows = [[f.file_path, f.func_qn, f.from_block, f.to_block, f.edge_kind, f.from_line, f.to_line] for f in facts]
        cols = "file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line"
        self._put_batch("cfg_edge", cols, cols, rows)

    def add_def_uses_batch(self, facts: list[DefUseFact]) -> None:
        """Bulk-insert def-use facts."""
        if not facts:
            return
        rows = [
            [f.file_path, f.func_qn, f.var_name, f.kind, f.def_block, f.use_block, f.def_line, f.def_col, f.use_line, f.use_col]
            for f in facts
        ]
        self._put_batch(
            "def_use",
            "file_path, func_qn, var_name, kind, def_block, use_block, def_line, def_col, use_line, use_col",
            "file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line => def_col, use_col",
            rows,
        )

    def add_cfg_block(self, fact: CfgBlockFact) -> None:
        """Add a CFG block fact."""
        self.add_cfg_blocks_batch([fact])

    def add_cfg_blocks_batch(self, facts: list[CfgBlockFact]) -> None:
        """Bulk-insert CFG block facts."""
        if not facts:
            return
        rows = [[f.file_path, f.func_qn, f.block_id, f.is_entry, f.is_exit] for f in facts]
        self._put_batch(
            "cfg_block",
            "file_path, func_qn, block_id, is_entry, is_exit",
            "file_path, func_qn, block_id => is_entry, is_exit",
            rows,
        )

    def add_method_call(self, fact: MethodCallFact) -> None:
        """Add a method call fact."""
        self.add_method_calls_batch([fact])

    def add_method_calls_batch(self, facts: list[MethodCallFact]) -> None:
        """Bulk-insert method call facts."""
        if not facts:
            return
        rows = [[f.file_path, f.func_qn, f.receiver, f.method, f.block_id, f.line] for f in facts]
        cols = "file_path, func_qn, receiver, method, block_id, line"
        self._put_batch("method_call", cols, cols, rows)

    def add_decorator_on(self, fact: DecoratorOnFact) -> None:
        """Add a decorator-on fact."""
        self.add_decorator_on_batch([fact])

    def add_decorator_on_batch(self, facts: list[DecoratorOnFact]) -> None:
        """Bulk-insert decorator-on facts."""
        if not facts:
            return
        rows = [[f.symbol_qn, f.decorator] for f in facts]
        self._put_batch("decorator_on", "symbol_qn, decorator", "symbol_qn, decorator", rows)

    def add_source_loc(self, fact: SourceLocFact) -> None:
        """Add a source location fact."""
        self.add_source_locs_batch([fact])

    def add_source_locs_batch(self, facts: list[SourceLocFact]) -> None:
        """Bulk-insert source location facts."""
        if not facts:
            return
        rows = [[f.file_path, f.loc_kind, f.loc_id, f.line, f.col, f.end_line, f.rel_line] for f in facts]
        self._put_batch(
            "source_loc",
            "file_path, loc_kind, loc_id, line, col, end_line, rel_line",
            "file_path, loc_kind, loc_id => line, col, end_line, rel_line",
            rows,
        )

    def add_func_summary(self, fact: FuncSummaryFact) -> None:
        """Add a function summary fact."""
        self.add_func_summaries_batch([fact])

    def add_func_summaries_batch(self, facts: list[FuncSummaryFact]) -> None:
        """Bulk-insert function summary facts."""
        if not facts:
            return
        rows = [[f.func_qn, f.param_name, f.flows_to_return, f.flows_to_sink, f.sink_label] for f in facts]
        self._put_batch(
            "func_summary",
            "func_qn, param_name, flows_to_return, flows_to_sink, sink_label",
            "func_qn, param_name => flows_to_return, flows_to_sink, sink_label",
            rows,
        )

    def add_entry_point_decorator(self, fact: EntryPointDecoratorFact) -> None:
        """Add an entry point decorator fact."""
        self.add_entry_point_decorators_batch([fact])

    def add_entry_point_decorators_batch(self, facts: list[EntryPointDecoratorFact]) -> None:
        """Bulk-insert entry point decorator facts."""
        if not facts:
            return
        rows = [[f.decorator] for f in facts]
        self._put_batch("entry_point_decorator", "decorator", "decorator", rows)

    def add_entry_point_name(self, fact: EntryPointNameFact) -> None:
        """Add an entry point name fact."""
        self.add_entry_point_names_batch([fact])

    def add_entry_point_names_batch(self, facts: list[EntryPointNameFact]) -> None:
        """Bulk-insert entry point name facts."""
        if not facts:
            return
        rows = [[f.name] for f in facts]
        self._put_batch("entry_point_name", "name", "name", rows)

    def add_exported_symbol(self, fact: ExportedSymbolFact) -> None:
        """Add an exported symbol scoped to its defining file."""
        self.add_exported_symbols_batch([fact])

    def add_exported_symbols_batch(
        self, facts: list[ExportedSymbolFact | tuple[str, str] | str]
    ) -> None:
        """Bulk-insert exports; legacy bare QNs remain globally scoped."""
        if not facts:
            return
        rows = [
            [fact.file_path, fact.qualified_name]
            if isinstance(fact, ExportedSymbolFact)
            else (["", fact] if isinstance(fact, str) else [fact[0], fact[1]])
            for fact in facts
        ]
        self._put_batch(
            "exported_symbol",
            "file_path, qualified_name",
            "file_path, qualified_name",
            rows,
        )

    # -- Post-processing ---------------------------------------------------

    def _resolve_builtin_refs(self) -> None:
        """Resolve ``builtins.*`` references using import facts.

        When a scope resolver can't resolve a cross-file import (typical
        for TypeScript/Rust), it reports the callee as ``builtins.X``.
        This method finds such references, matches them against import
        facts, and adds corrected reference/call facts with the real
        qualified name.
        """
        try:
            builtin_calls = self._client.run(
                "?[caller_qn, callee_qn, file_path, line, col, func_qn, block_id] := "
                "*call[caller_qn, callee_qn, file_path, line, col, func_qn, block_id], "
                "starts_with(callee_qn, 'builtins.')"
            )["rows"]
        except Exception:
            logger.debug("builtins.* call query failed; skipping builtin ref resolution", exc_info=True)
            return

        if not builtin_calls:
            return

        try:
            imports = self._client.run(
                "?[importing_file, imported_name, imported_module] := "
                "*import[importing_file, imported_module, imported_name, _, _], "
                "imported_name != ''"
            )["rows"]
        except Exception:
            logger.debug("import fact query failed; skipping builtin ref resolution", exc_info=True)
            return

        import_map: dict[tuple[str, str], str] = {}
        for imp_file, imp_name, imp_module in imports:
            import_map[(imp_file, imp_name)] = imp_module

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
                imp_module, file_path, bare_name,
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
    ) -> str | None:
        """Resolve an import source path to a symbol QN.

        For relative imports like ``./target`` or ``../utils/helper``,
        resolves relative to the importing file's directory.
        """
        # Strip leading ./ and resolve relative paths
        source = import_source
        if source.startswith("./") or source.startswith("../"):
            # Resolve relative to the importing file's directory
            imp_dir = posixpath.dirname(importing_file)
            source = posixpath.normpath(posixpath.join(imp_dir, source))

        # Normalize separators to dots
        normalized = _normalize_qn(source)

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

    def flow_events(
        self,
        file_path: str | None = None,
        func_id: str | None = None,
        role: str | None = None,
    ) -> list[FlowEventFact]:
        """Query occurrence/value events with optional narrow filters."""
        clauses = [
            "*flow_event[fp, eid, fid, fn, fs, role, var, ap, block, sb, eb, "
            "sl, sc, el, ec, ord, cid, ai, an, text]"
        ]
        params: dict[str, Any] = {}
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path
        if func_id is not None:
            clauses.append("fid == $func_id")
            params["func_id"] = func_id
        if role is not None:
            clauses.append("role == $role")
            params["role"] = role
        result = self._client.run(
            "?[fp, eid, fid, fn, fs, role, var, ap, block, sb, eb, sl, sc, el, ec, ord, cid, ai, an, text] := "
            + ", ".join(clauses), params
        )
        return [
            FlowEventFact(
                file_path=r[0], event_id=r[1], func_id=r[2], func_name=r[3],
                func_start=r[4], role=r[5], var=r[6] or None, access_path=r[7] or None,
                block=r[8], start_byte=r[9], end_byte=r[10], start_line=r[11],
                start_col=r[12], end_line=r[13], end_col=r[14], ordinal=r[15],
                call_id=None if r[16] == -1 else r[16],
                arg_index=None if r[17] == -1 else r[17],
                arg_name=r[18] or None,
                text=r[19],
            )
            for r in result["rows"]
        ]

    def flow_edges(
        self,
        file_path: str | None = None,
        edge_kind: str | None = None,
    ) -> list[FlowEdgeFact]:
        """Query occurrence/value edges with optional filters."""
        clauses = ["*flow_edge[fp, fr, to, kind]"]
        params: dict[str, Any] = {}
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path
        if edge_kind is not None:
            clauses.append("kind == $edge_kind")
            params["edge_kind"] = edge_kind
        result = self._client.run(
            "?[fp, fr, to, kind] := " + ", ".join(clauses), params
        )
        return [
            FlowEdgeFact(file_path=r[0], from_event=r[1], to_event=r[2], edge_kind=r[3])
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
        except Exception:
            logger.debug("method_call_types query failed", exc_info=True)
            return []
        return [(r[0], r[1], r[2], r[3], r[4]) for r in result["rows"]]

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

    def exported_symbols(
        self,
        file_path: str | None = None,
        qualified_name: str | None = None,
    ) -> list[ExportedSymbolFact]:
        """Return exported symbols, optionally scoped by file or QN."""
        clauses = ["*exported_symbol[fp, qn]"]
        params: dict[str, Any] = {}
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = file_path
        if qualified_name is not None:
            clauses.append("qn == $qualified_name")
            params["qualified_name"] = qualified_name
        result = self._client.run("?[fp, qn] := " + ", ".join(clauses), params)
        return [
            ExportedSymbolFact(file_path=r[0], qualified_name=r[1])
            for r in result["rows"]
        ]

    def resolve_location(self, file_path: str, line: int) -> tuple[str, int]:
        """Resolve a line number to ``(func_qn, block_id)`` using stored facts.

        Uses symbol facts for function ranges and source_loc/cfg_block facts
        for block ranges.

        Returns ``(MODULE_LEVEL_FUNC, MODULE_LEVEL_BLOCK)`` for module-level
        code (i.e. when the line does not fall inside any known function).
        """
        from emend.location_resolver import LocationResolver

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

    @staticmethod
    def _cozo_quote(value: str | int) -> str:
        """Escape a value for a CozoScript single-quoted literal."""
        if isinstance(value, str):
            return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return str(value)

    @staticmethod
    def _inline_relation(
        name: str,
        columns: list[str],
        rows: list[tuple[str | int, ...]],
    ) -> str:
        """Render a small in-memory relation for a CozoScript query."""
        header = ", ".join(columns)
        if not rows:
            return f"{name}[{header}] <- []\n"
        quote = FactGraph._cozo_quote
        values = ", ".join(
            "[" + ", ".join(quote(value) for value in row) + "]"
            for row in rows
        )
        return f"{name}[{header}] <- [{values}]\n"

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
        exclude_reference_files: list[str] | None = None,
        entry_point_prefixes: list[str] | None = None,
        entry_point_qualified_names: list[str] | None = None,
        include_transitive: bool = False,
    ) -> tuple[list[DeadSymbolFact], list[CfgBlockFact]]:
        """Unified dead code detection via Datalog.

        Combines unreachable-block analysis with unreferenced-symbol detection
        in a single Datalog program:

        1. Computes reachable blocks via transitive closure from CFG entry blocks
        2. Only counts references from reachable code as "live"
        3. Applies entry point heuristics (dunders, test_, decorators) as Datalog rules
        4. Returns a tuple of (dead symbols, unreachable blocks).

        String literal filtering stays as a Python post-filter (caller's responsibility).
        With include_transitive, also return dependencies of direct roots that
        have no other live references, recording all contributing root names.
        Unrooted recursive components stay conservative (not reported).
        """
        # Per-invocation seeds must remain query-local.  The stored relations
        # contain only project configuration; putting CLI arguments into them
        # makes one dead-code invocation affect all later invocations.
        seed_rules = (
            self._inline_relation(
                "seed_ep_decorator", ["decorator"],
                [(d,) for d in (entry_point_decorators or [])],
            )
            + self._inline_relation(
                "seed_ep_name", ["name"],
                [(n,) for n in (entry_point_names or [])],
            )
            + self._inline_relation(
                "seed_ep_prefix", ["prefix"],
                [(p,) for p in (entry_point_prefixes or [])],
            )
        )

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
        excluded_file_rules = ""
        if exclude_reference_files:
            excluded_file_rules = self._inline_relation(
                "excluded_reference_file",
                ["fp"],
                [(file_path,) for file_path in exclude_reference_files],
            )
            excl_parts.append("not excluded_reference_file[fp]")
            excl_parts_ref.append("not excluded_reference_file[ref_fp]")
        if excl_parts:
            excl_clauses = ", " + ", ".join(excl_parts)
        if excl_parts_ref:
            excl_clauses_ref = ", " + ", ".join(excl_parts_ref)

        exact_entry_rules = ""
        if entry_point_qualified_names:
            exact_entry_rules = self._inline_relation(
                "configured_entry_point",
                ["qn"],
                [(qn,) for qn in entry_point_qualified_names],
            ) + "entry_point[qn] := configured_entry_point[qn]\n"

        query = (
            seed_rules + excluded_file_rules + exact_entry_rules +
            # Live references: from reachable code via pre-computed relations
            # (ref_by_block keyed on (fp, fq, bid, sq) joins efficiently with
            # reachable_block keyed on (fp, fq, bid))
            "reference_edge[fq, sq] := "
            "*ref_by_block[fp, fq, bid, sq], "
            "*reachable_block[fp, fq, bid], "
            f"sq != fq{excl_clauses}\n"

            # Without receiver types, any reachable member call may target a
            # same-named private method, including calls through local aliases.
            # Exact member names avoid suffix collisions.
            "live_private_method_name[fq, method_name] := "
            "*method_call[fp, fq, _, method_name, bid, _], "
            "*reachable_block[fp, fq, bid]"
            f"{excl_clauses}\n"

            'live_private_method_name[fq, method_name] := '
            '*method_call[fp, fq, _, method_name, _, _], fq == "<module>"'
            f"{excl_clauses}\n"

            # Non-call uses such as ``callbacks = [self._helper]`` join on a
            # materialized member name, avoiding a suffix-based cross product.
            "live_private_method_name[fq, method_name] := "
            "*noncall_private_member_ref[fp, fq, bid, method_name], "
            f"*reachable_block[fp, fq, bid]{excl_clauses}\n"

            "private_method[method_name, target_qn] := "
            "*symbol[target_qn, _, method_name, method_kind, _, _, _], "
            'method_kind in ["method", "async_method"], '
            'starts_with(method_name, "_"), not starts_with(method_name, "__")\n'
            "method_count[name, count(qn)] := private_method[name, qn]\n"
            "reference_edge[fq, target] := live_private_method_name[fq, name], "
            "method_count[name, 1], private_method[name, target]\n"
            # Ambiguous name-only matches keep all targets alive, but cannot
            # establish a causal dependency. This also avoids a callers x
            # same-named-methods cross product when producing root summaries.
            "external_ref[target] := live_private_method_name[_, name], "
            "method_count[name, n], n > 1, private_method[name, target]\n"

            # Live references: from module level (no function context)
            # Exclude self-references where the reference is the symbol's own definition
            'external_ref[sq] := '
            '*module_level_ref[sq, ref_fp, ref_line], '
            '*symbol[sq, sym_fp, _, _, sym_line, _, _], '
            f'not (ref_fp == sym_fp, ref_line == sym_line){excl_clauses_ref}\n'

            'live_ref[qn] := reference_edge[_, qn]\n'
            'live_ref[qn] := external_ref[qn]\n'

            # Entry points: dunder methods
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            'starts_with(name, "__"), ends_with(name, "__")\n'

            # Entry points: dynamic prefix rules (test_, Test, describe_, etc.)
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            '*entry_point_prefix[pfx], '
            'starts_with(name, pfx)\n'
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            'seed_ep_prefix[pfx], '
            'starts_with(name, pfx)\n'

            # Entry points: decorated symbols (case-insensitive for TS PascalCase)
            'entry_point[qn] := '
            '*decorator_on[qn, dec], '
            '*entry_point_decorator[ep_dec], '
            'lowercase(dec) == lowercase(ep_dec)\n'
            'entry_point[qn] := '
            '*decorator_on[qn, dec], '
            'seed_ep_decorator[ep_dec], '
            'lowercase(dec) == lowercase(ep_dec)\n'

            # Entry points: named symbols
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            '*entry_point_name[name]\n'
            'entry_point[qn] := '
            '*symbol[qn, _, name, _, _, _, _], '
            'seed_ep_name[name]\n'

            # Entry points: explicitly exported symbols
            'entry_point[qn] := '
            '*exported_symbol[_, qn]\n'

            # A private method is only actionable when its containing class is
            # itself live or externally exposed. This avoids duplicating every
            # method beneath an already-dead class.
            'live_container[qn] := live_ref[qn]\n'
            'live_container[qn] := entry_point[qn]\n'

            # Dead top-level symbols.
            "eligible[qn, fp, name, kind, line, end_line, parent] := "
            "*symbol[qn, fp, name, kind, line, end_line, parent], "
            'parent == "", '
            "not entry_point[qn]\n"

            # Private methods on live classes. Public methods stay conservative
            # because frameworks, protocols, and subclasses commonly invoke
            # them without a statically visible reference.
            "eligible[qn, fp, name, kind, line, end_line, parent] := "
            "*symbol[qn, fp, name, kind, line, end_line, parent], "
            'parent != "", '
            'kind in ["method", "async_method"], '
            'starts_with(name, "_"), not starts_with(name, "__"), '
            "live_container[parent], "
            "not entry_point[qn]\n"

            "root[qn] := eligible[qn, _, _, _, _, _, _], not live_ref[qn]\n"
            "reported[qn, causes] := root[qn], causes = []\n"
        )

        if include_transitive:
            query += (
                # First bound the analysis to dependencies of known roots.
                # Then retain every node reachable from an outside reference.
                # This handles shared callees and cycles without iterative
                # deletion or assuming every non-entry-point function is dead.
                "candidate[qn] := root[qn]\n"
                "candidate[target] := candidate[source], reference_edge[source, target], "
                "eligible[target, _, _, _, _, _, _]\n"
                "retained[qn] := candidate[qn], external_ref[qn]\n"
                "retained[target] := candidate[target], reference_edge[source, target], "
                "not candidate[source]\n"
                "retained[target] := retained[source], reference_edge[source, target], candidate[target]\n"
                "unused[qn] := candidate[qn], not retained[qn]\n"
                "cause[root, root] := root[root]\n"
                "cause[root, target] := cause[root, source], reference_edge[source, target], unused[target]\n"
                "causes[qn, collect(root)] := cause[root, qn], not root[qn]\n"
                "reported[qn, roots] := causes[qn, roots]\n"
            )
        query += (
            "?[fp, name, qn, kind, line, end_line, parent, roots] := "
            "reported[qn, roots], eligible[qn, fp, name, kind, line, end_line, parent]"
        )

        result = self._client.run(query)
        dead_symbols = [
            DeadSymbolFact(
                file_path=r[0], name=r[1], qualified_name=r[2],
                kind=r[3], line=r[4], end_line=r[5],
                parent=r[6] if r[6] else None,
                root_causes=tuple(sorted(r[7])),
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
        except Exception:
            logger.debug("Unreachable block query failed", exc_info=True)
            return dead_symbols, []

        unreachable_blocks = [
            CfgBlockFact(
                file_path=r[0], func_qn=r[1], block_id=r[2],
                is_entry=r[3], is_exit=r[4],
            )
            for r in unreachable_result["rows"]
        ]
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

    # -- Direct relation queries via Datalog --------------------

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

    # -- Generic query (predicate-based, for backwards compat) -----------

    def _fact_accessors(self) -> list[Callable[[], list[Fact]]]:
        """Ordered accessors covering every fact relation.

        Single source of truth for ``query`` and ``to_json`` — the order fixes
        the serialised JSON layout (pinned by tests), so append new relations
        at the end.
        """
        return [
            self.symbols,
            self._all_calls,
            self._all_references,
            self.trace_flows,
            self._all_types,
            self._all_imports,
            self._all_cfg_edges,
            self._all_def_uses,
            self._all_method_calls,
            self._all_cfg_blocks,
            self._all_decorator_on,
            self._all_source_locs,
            self._all_func_summaries,
            self._all_entry_point_decorators,
            self._all_entry_point_names,
            self._all_exported_symbols,
            self._all_flow_events,
            self._all_flow_edges,
        ]

    def query(self, predicate: Callable[[Fact], bool]) -> list[Fact]:
        """Return all facts matching *predicate*.

        This fetches all facts from CozoDB and filters in Python.
        For performance-sensitive queries, use ``run_query()`` with
        CozoScript instead.
        """
        results: list[Fact] = []
        for accessor in self._fact_accessors():
            results.extend(fact for fact in accessor() if predicate(fact))
        return results

    def _query_all(
        self,
        relation: str,
        select_cols: str,
        mapper: Callable[[list[Any]], Any],
        relation_cols: str | None = None,
    ) -> list[Any]:
        """Run ``?[select_cols] := *relation[relation_cols]`` and map each row.

        When *relation_cols* is omitted, it defaults to *select_cols*.
        """
        rel = relation_cols if relation_cols is not None else select_cols
        result = self._client.run(f"?[{select_cols}] := *{relation}[{rel}]")
        return [mapper(r) for r in result["rows"]]

    def _all_calls(self) -> list[CallFact]:
        return self._query_all(
            "call", "caller, callee, fp, line, col, fq, bid",
            lambda r: CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3],
                               col=r[4], func_qn=r[5], block_id=r[6]),
        )

    def _all_references(self) -> list[ReferenceFact]:
        return self._query_all(
            "reference", "qn, fp, line, col, kind, fq, bid",
            lambda r: ReferenceFact(symbol_qn=r[0], file_path=r[1], line=r[2], col=r[3],
                                    ref_kind=r[4], func_qn=r[5], block_id=r[6]),
        )

    def _all_types(self) -> list[TypeFact]:
        return self._query_all(
            "type_binding", "qn, ts, fp, line, bk",
            lambda r: TypeFact(symbol_qn=r[0], type_str=r[1], file_path=r[2], line=r[3], binding_kind=r[4]),
            relation_cols="qn, fp, line, bk, ts",
        )

    def _all_imports(self) -> list[ImportFact]:
        return self._query_all(
            "import", "f, mod, name, alias, line",
            lambda r: ImportFact(
                importing_file=r[0], imported_module=r[1],
                imported_name=r[2] if r[2] else None,
                alias=r[3] if r[3] else None, line=r[4],
            ),
            relation_cols="f, mod, name, line, alias",
        )

    def _all_cfg_edges(self) -> list[CfgEdgeFact]:
        return self._query_all(
            "cfg_edge", "fp, fq, fb, tb, ek, fl, tl",
            lambda r: CfgEdgeFact(
                file_path=r[0], func_qn=r[1], from_block=r[2],
                to_block=r[3], edge_kind=r[4], from_line=r[5], to_line=r[6],
            ),
        )

    def _all_flow_events(self) -> list[FlowEventFact]:
        return self._query_all(
            "flow_event",
            "fp, eid, fid, fn, fs, role, var, ap, block, sb, eb, sl, sc, el, ec, ord, cid, ai, an, text",
            lambda r: FlowEventFact(
                file_path=r[0], event_id=r[1], func_id=r[2], func_name=r[3],
                func_start=r[4], role=r[5], var=r[6] or None, access_path=r[7] or None,
                block=r[8], start_byte=r[9], end_byte=r[10], start_line=r[11],
                start_col=r[12], end_line=r[13], end_col=r[14], ordinal=r[15],
                call_id=None if r[16] == -1 else r[16],
                arg_index=None if r[17] == -1 else r[17],
                arg_name=r[18] or None,
                text=r[19],
            ),
            relation_cols="fp, eid, fid, fn, fs, role, var, ap, block, sb, eb, sl, sc, el, ec, ord, cid, ai, an, text",
        )

    def _all_flow_edges(self) -> list[FlowEdgeFact]:
        return self._query_all(
            "flow_edge",
            "fp, fr, to, kind",
            lambda r: FlowEdgeFact(
                file_path=r[0], from_event=r[1], to_event=r[2], edge_kind=r[3]
            ),
        )

    def _all_def_uses(self) -> list[DefUseFact]:
        return self._query_all(
            "def_use", "fp, fq, vn, k, db, ub, dl, dc, ul, uc",
            lambda r: DefUseFact(
                file_path=r[0], func_qn=r[1], var_name=r[2],
                kind=r[3], def_block=r[4], use_block=r[5],
                def_line=r[6], def_col=r[7], use_line=r[8], use_col=r[9],
            ),
            relation_cols="fp, fq, vn, k, db, ub, dl, ul, dc, uc",
        )

    def _all_method_calls(self) -> list[MethodCallFact]:
        return self._query_all(
            "method_call", "fp, fq, rcv, meth, bid, ln",
            lambda r: MethodCallFact(
                file_path=r[0], func_qn=r[1], receiver=r[2],
                method=r[3], block_id=r[4], line=r[5],
            ),
        )

    def _all_cfg_blocks(self) -> list[CfgBlockFact]:
        return self._query_all(
            "cfg_block", "fp, fq, bid, ie, ix",
            lambda r: CfgBlockFact(file_path=r[0], func_qn=r[1], block_id=r[2],
                                   is_entry=r[3], is_exit=r[4]),
        )

    def _all_decorator_on(self) -> list[DecoratorOnFact]:
        return self._query_all(
            "decorator_on", "sqn, dec",
            lambda r: DecoratorOnFact(symbol_qn=r[0], decorator=r[1]),
        )

    def _all_source_locs(self) -> list[SourceLocFact]:
        return self._query_all(
            "source_loc", "fp, lk, lid, line, col, el, rl",
            lambda r: SourceLocFact(file_path=r[0], loc_kind=r[1], loc_id=r[2],
                                    line=r[3], col=r[4], end_line=r[5], rel_line=r[6]),
        )

    def _all_func_summaries(self) -> list[FuncSummaryFact]:
        return self._query_all(
            "func_summary", "fq, pn, ftr, fts, sl",
            lambda r: FuncSummaryFact(func_qn=r[0], param_name=r[1], flows_to_return=r[2],
                                      flows_to_sink=r[3], sink_label=r[4]),
        )

    def _all_entry_point_decorators(self) -> list[EntryPointDecoratorFact]:
        return self._query_all(
            "entry_point_decorator", "dec",
            lambda r: EntryPointDecoratorFact(decorator=r[0]),
        )

    def _all_entry_point_names(self) -> list[EntryPointNameFact]:
        return self._query_all(
            "entry_point_name", "name",
            lambda r: EntryPointNameFact(name=r[0]),
        )

    def _all_exported_symbols(self) -> list[ExportedSymbolFact]:
        return self._query_all(
            "exported_symbol", "file_path, qualified_name",
            lambda r: ExportedSymbolFact(file_path=r[0], qualified_name=r[1]),
        )

    # -- Serialization ----------------------------------------------------

    def to_json(self) -> str:
        """Serialize the entire fact graph to a JSON string."""
        def _tag(fact: Fact) -> dict[str, Any]:
            d = asdict(fact)  # type: ignore[arg-type]
            d["_type"] = type(fact).__name__
            return d

        data: list[dict[str, Any]] = []
        for accessor in self._fact_accessors():
            data.extend(_tag(fact) for fact in accessor())
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
            "FlowEventFact": (FlowEventFact, graph.add_flow_event),
            "FlowEdgeFact": (FlowEdgeFact, graph.add_flow_edge),
            "DefUseFact": (DefUseFact, graph.add_def_use),
            "MethodCallFact": (MethodCallFact, graph.add_method_call),
            "CfgBlockFact": (CfgBlockFact, graph.add_cfg_block),
            "DecoratorOnFact": (DecoratorOnFact, graph.add_decorator_on),
            "SourceLocFact": (SourceLocFact, graph.add_source_loc),
            "FuncSummaryFact": (FuncSummaryFact, graph.add_func_summary),
            "EntryPointDecoratorFact": (EntryPointDecoratorFact, graph.add_entry_point_decorator),
            "EntryPointNameFact": (EntryPointNameFact, graph.add_entry_point_name),
            "ExportedSymbolFact": (ExportedSymbolFact, graph.add_exported_symbol),
        }

        for entry in json.loads(json_str):
            type_name = entry.pop("_type", None)
            if type_name not in _TYPE_MAP:
                logger.warning("Unknown fact type in JSON: %s", type_name)
                continue
            fact_cls, adder = _TYPE_MAP[type_name]
            if type_name == "ExportedSymbolFact":
                entry.setdefault("file_path", "")
            adder(fact_cls(**entry))
        return graph

    # -- Incremental update / removal -------------------------------------

    def remove_files(
        self,
        file_paths: list[str],
        *,
        operations: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
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
                # search_symbol
                "?[file_path, module_qualified_name] := "
                "*search_symbol[file_path, module_qualified_name, _, _, _, _, _, _, _, _, _, _], "
                "file_path == $fp  :rm search_symbol {file_path, module_qualified_name => }",
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
                # flow_event/flow_edge are file-owned occurrence relations.
                "?[file_path, event_id] := "
                "*flow_event[file_path, event_id, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _], "
                "file_path == $fp  :rm flow_event {file_path, event_id => }",
                "?[file_path, from_event, to_event, edge_kind] := "
                "*flow_edge[file_path, from_event, to_event, edge_kind], "
                "file_path == $fp  :rm flow_edge "
                "{file_path, from_event, to_event, edge_kind}",
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
                # noncall_private_member_ref (all keys)
                "?[file_path, func_qn, block_id, member_name] := "
                "*noncall_private_member_ref[file_path, func_qn, block_id, member_name], "
                "file_path == $fp  :rm noncall_private_member_ref "
                "{file_path, func_qn, block_id, member_name}",
                # reachable_block (all keys)
                "?[file_path, func_qn, block_id] := "
                "*reachable_block[file_path, func_qn, block_id], "
                "file_path == $fp  :rm reachable_block {file_path, func_qn, block_id}",
                # module_level_ref
                "?[symbol_qn, file_path, line] := "
                "*module_level_ref[symbol_qn, file_path, line], "
                "file_path == $fp  :rm module_level_ref {symbol_qn, file_path, line}",
                # exported_symbol is keyed by its defining file.
                "?[file_path, qualified_name] := "
                "*exported_symbol[file_path, qualified_name], "
                "file_path == $fp  :rm exported_symbol {file_path, qualified_name}",
                # import (uses importing_file, not file_path)
                "?[importing_file, imported_module, imported_name, line] := "
                "*import[importing_file, imported_module, imported_name, line, _], "
                "importing_file == $fp  :rm import "
                "{importing_file, imported_module, imported_name, line => }",
            ):
                if operations is not None:
                    operations.append((query, {"fp": fp}))
                    continue
                try:
                    self._client.run(query, {"fp": fp})
                except Exception:
                    logger.debug("Fact removal query failed for %s", fp, exc_info=True)

    def _insert_extracted_file_facts(
        self,
        extracted: ExtractedFile | dict[str, list[list[Any]]],
        *,
        operations: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        """Insert the language-neutral rows returned by the canonical extractor."""
        # Dict support is retained only for injected legacy/test extractors.
        rows = extracted.rows if isinstance(extracted, ExtractedFile) else extracted
        specs = {
            "fg_sym": ("symbol", "qualified_name, file_path, name, kind, line, end_line, parent", "qualified_name => file_path, name, kind, line, end_line, parent"),
            "search_sym": (
                "search_symbol",
                "file_path, module_qualified_name, name, qualified_name, kind, "
                "line, end_line, depth, parent, signature, returns, decorators",
                "file_path, module_qualified_name => name, qualified_name, kind, "
                "line, end_line, depth, parent, signature, returns, decorators",
            ),
            "dec": ("decorator_on", "symbol_qn, decorator", "symbol_qn, decorator"),
            "fg_refs": ("reference", "symbol_qn, file_path, line, col, ref_kind, func_qn, block_id", "symbol_qn, file_path, line, col => ref_kind, func_qn, block_id"),
            "calls": ("call", "caller_qn, callee_qn, file_path, line, col, func_qn, block_id", "caller_qn, callee_qn, file_path, line, col => func_qn, block_id"),
            "calls_by_callee": ("call_by_callee", "callee_qn, caller_qn, file_path, line, col, func_qn, block_id", "callee_qn, caller_qn, file_path, line, col => func_qn, block_id"),
            "calls_by_file": ("call_by_file", "file_path, caller_qn, callee_qn, line, col, func_qn, block_id", "file_path, caller_qn, callee_qn, line, col => func_qn, block_id"),
            "cfg_blocks": ("cfg_block", "file_path, func_qn, block_id, is_entry, is_exit", "file_path, func_qn, block_id => is_entry, is_exit"),
            "cfg_edges": ("cfg_edge", "file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line", "file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line"),
            "flow_events": (
                "flow_event",
                "file_path, event_id, func_id, func_name, func_start, role, var, access_path, block, start_byte, end_byte, start_line, start_col, end_line, end_col, ordinal, call_id, arg_index, arg_name, text",
                "file_path, event_id => func_id, func_name, func_start, role, var, access_path, block, start_byte, end_byte, start_line, start_col, end_line, end_col, ordinal, call_id, arg_index, arg_name, text",
            ),
            "flow_edges": (
                "flow_edge", "file_path, from_event, to_event, edge_kind",
                "file_path, from_event, to_event, edge_kind",
            ),
            "def_uses": ("def_use", "file_path, func_qn, var_name, kind, def_block, use_block, def_line, def_col, use_line, use_col", "file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line => def_col, use_col"),
            "method_calls": ("method_call", "file_path, func_qn, receiver, method, block_id, line", "file_path, func_qn, receiver, method, block_id, line"),
            "source_locs": ("source_loc", "file_path, loc_kind, loc_id, line, col, end_line, rel_line", "file_path, loc_kind, loc_id => line, col, end_line, rel_line"),
            "imports": ("import", "importing_file, imported_module, imported_name, line, alias", "importing_file, imported_module, imported_name, line => alias"),
            "ref_by_block": ("ref_by_block", "file_path, func_qn, block_id, symbol_qn", "file_path, func_qn, block_id, symbol_qn"),
            "noncall_private_member_refs": ("noncall_private_member_ref", "file_path, func_qn, block_id, member_name", "file_path, func_qn, block_id, member_name"),
            "module_level_refs": ("module_level_ref", "symbol_qn, file_path, line", "symbol_qn, file_path, line"),
            "exported_qns": (
                "exported_symbol", "file_path, qualified_name",
                "file_path, qualified_name",
            ),
        }
        for key, (relation, cols, schema) in specs.items():
            relation_rows = rows.get(key, [])
            if relation_rows:
                self._put_batch(
                    relation, cols, schema, relation_rows, operations
                )

        entries: dict[tuple[str, str], set[int]] = {}
        adjacency: dict[tuple[str, str, int], list[int]] = {}
        for fp, fq, bid, is_entry, _is_exit in rows["cfg_blocks"]:
            if is_entry:
                entries.setdefault((fp, fq), set()).add(bid)
        for fp, fq, from_block, to_block, *_ in rows["cfg_edges"]:
            adjacency.setdefault((fp, fq, from_block), []).append(to_block)
        reachable = _bfs_reachable_blocks(entries, adjacency)
        if reachable:
            self._put_batch(
                "reachable_block", "file_path, func_qn, block_id",
                "file_path, func_qn, block_id", reachable, operations,
            )

    def replace_extracted(
        self,
        extracted_files: list[ExtractedFile],
        *,
        stored_paths: list[str],
    ) -> None:
        """Replace path-owned rows using precomputed extraction artifacts."""
        operations: list[tuple[str, dict[str, Any]]] = []
        self.remove_files(stored_paths, operations=operations)
        for extracted in extracted_files:
            self._insert_extracted_file_facts(extracted, operations=operations)
        self._run_mutations(operations)

    def update_files(
        self,
        file_list: list[tuple[str, str]],
        language: str = "python",
        *,
        project_root: str | None = None,
        resolver_root: str | None = None,
    ) -> None:
        """Incrementally update facts for a set of files.

        Takes a list of ``(file_path, source_content)`` pairs.  For each
        file, all existing facts are deleted and fresh facts are extracted
        from the provided content.  Files not in *file_list* are untouched.

        Per-file extraction is shared with ``build_from_project()`` and the
        persisted index builder; this method owns incremental deletion and
        insertion only.
        """
        from emend import emend_core as _rust
        # 1. Delete existing facts for all files in the batch.
        resolved_project_root = Path(project_root).resolve() if project_root else None

        def stored_path(file_path: str) -> str:
            resolved = Path(file_path).resolve()
            if resolved_project_root is not None:
                try:
                    return str(resolved.relative_to(resolved_project_root))
                except ValueError:
                    pass
            return str(resolved)

        # 2. Extract and insert new facts per file.  The persisted index uses
        # this same extractor, so analysis semantics cannot drift by builder.
        extracted_files = []
        for abs_file_path, content in file_list:
            rel_path = stored_path(abs_file_path)
            if project_root:
                from emend.project_config import module_name_for_file

                module_name = module_name_for_file(abs_file_path, project_root)
            else:
                module_name = Path(abs_file_path).stem
            ext = Path(abs_file_path).suffix.lstrip(".") or "py"
            try:
                effective_resolver_root = resolver_root or str(Path(abs_file_path).parent.resolve())
                resolver = _rust.PyScopeResolver(effective_resolver_root, ext)
                resolver.index_file(abs_file_path, content)
            except Exception:
                logger.debug("Could not build scope resolver for %s", abs_file_path, exc_info=True)
                resolver = None

            extracted_files.append(_extract_file_facts(
                abs_file_path, rel_path, ext, content,
                project_root or str(Path(abs_file_path).parent),
                module_name, scope_resolver=resolver,
            ))
        self.replace_extracted(
            extracted_files,
            stored_paths=[stored_path(fp) for fp, _ in file_list],
        )

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
            except (OSError, UnicodeDecodeError):
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
        include_types: bool = True,
    ) -> FactGraph:
        """Populate a fact graph by visiting all files in a project.

        Uses emend's tree-sitter infrastructure (``emend_core``) to
        extract symbols, references, calls, and imports from every
        source file in the project.

        ``language`` (singular) keeps backward compatibility.  Pass
        ``languages`` (a list) or leave both as ``None`` to auto-detect
        from the project directory.

        This full-rebuild API and the persisted index share the same
        per-file extractor.
        """
        from emend.fact_graph_compat import build_from_project

        return build_from_project(
            cls, project_path, language, db_path, languages, include_types
        )


# ---------------------------------------------------------------------------
# Internal helpers for build_from_project
# ---------------------------------------------------------------------------



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
    # First step
    first_step = steps[0]
    join_clauses.append(f'step_{first_step.bind}[fp, fq, block_0, line_0]')

    for i in range(1, len(steps)):
        step = steps[i]
        reach_name = f"reachable_{i - 1}"
        block_var = f"block_{i}"
        line_var = f"line_{i}"

        # Step location
        join_clauses.append(f'step_{step.bind}[fp, fq, {block_var}, {line_var}]')

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

    # CozoScript doesn't support "as" aliases in the output — use positional
    # We'll just output all step lines plus fp and fq
    rules.append(
        '?[fp, fq, first_line, last_line] := '
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
        if kind in ("function", "method", "async_function", "async_method"):
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
            except (OSError, UnicodeDecodeError):
                logger.debug("Could not read %s for sequence step resolution", abs_path, exc_info=True)
                continue

            try:
                matches = find_pattern(
                    resolved_pattern,
                    abs_path,
                    source_override=source,
                    language="python",
                )
            except BUG_EXCEPTIONS:
                raise
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
            for fp in file_paths:
                abs_path = fp
                if resolve_root:
                    candidate = resolve_root / fp
                    if candidate.exists():
                        abs_path = str(candidate)
                try:
                    source = Path(abs_path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                try:
                    matches = find_pattern(resolved, abs_path, source_override=source, language="python")
                except BUG_EXCEPTIONS:
                    raise
                except Exception:
                    logger.debug("Blocker pattern match failed for %s in %s", resolved, fp, exc_info=True)
                    continue
                for m in matches:
                    if m.line is None:
                        continue
                    fq = _find_func_for_line(fp, m.line)
                    if fq:
                        bid = _find_block_for_line(fp, fq, m.line)
                        target.append((fp, fq, bid))

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
