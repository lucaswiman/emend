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
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from emend.analysis_extraction import _extract_file_facts, _normalize_qn
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
    FileNamespaceFact,
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

FACT_GRAPH_SCHEMA_VERSION = "13"
_FACT_INSERT_BATCH_SIZE = 25_000

# File paths and qualified names are stored as integer ids; the ``intern`` and
# ``interned`` relations hold each text once.  Ids count up from ``ID_BASE``,
# which is also the id of the empty string, so each is larger than any line,
# column, block or byte offset in the graph.
ID_BASE = 1 << 40


@dataclass(frozen=True)
class DeadSymbolFact(SymbolFact):
    """A query result, not a stored fact; empty causes identify a direct root."""

    root_causes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CozoDB helpers
# ---------------------------------------------------------------------------

# Cozo keeps every relation in one key/value table and creates it only when it
# is missing.  Its own definition is a rowid table, which stores each key twice
# (once in the table, once in the primary-key index).  Creating the table first
# as a clustered index keeps one copy; Cozo's statements are unchanged.
_COZO_STORAGE_TABLE = (
    "CREATE TABLE IF NOT EXISTS cozo (k BLOB PRIMARY KEY, v BLOB) WITHOUT ROWID"
)


def prepare_cozo_storage(db_path: str | Path) -> None:
    """Create Cozo's storage table with single-copy keys in a new database."""
    with closing(sqlite3.connect(str(db_path))) as db:
        db.execute(_COZO_STORAGE_TABLE)
        db.commit()


def _create_cozo_client(db_path: str | None = None, *, prepare: bool = True) -> Any:
    """Use the required native backend, in memory unless a path is supplied."""
    from emend.emend_core import PyCozoDb

    if db_path is None:
        return PyCozoDb("mem", "")
    if prepare:
        try:
            prepare_cozo_storage(db_path)
        except sqlite3.Error:
            # Cozo reports an unusable file itself, as it did before.
            logger.debug("Could not prepare Cozo storage at %s", db_path, exc_info=True)
    return PyCozoDb("sqlite", str(db_path))


class _SharedGenerationClient:
    """Cozo client for a generation that other readers can also see.

    Every script first runs with Cozo's read-only guarantee, so the backing
    file cannot change.  The first script Cozo rejects as a write moves the
    owning graph to a private copy and runs there.
    """

    def __init__(self, graph: "FactGraph", client: Any) -> None:
        self._graph = graph
        self._client = client

    def run(self, query: str, params: dict[str, Any] | None = None, *,
            read_only: bool = False, decode: bool = True) -> dict[str, Any]:
        try:
            return self._client.run(query, params, read_only=True, decode=decode)
        except RuntimeError as error:
            if read_only or "read-only" not in str(error):
                raise
        return self._graph._private_client().run(query, params, decode=decode)

    def intern_ids(self, texts: list[str]) -> list[int]:
        return self._client.intern_ids(texts)

    def texts_containing(
        self, fragments: list[str], limit: int, *, ignore_case: bool = False,
    ) -> list[tuple[str, int]]:
        return self._client.texts_containing(fragments, limit, ignore_case=ignore_case)

    def run_transaction(self, operations: list[tuple[Any, ...]]) -> None:
        self._graph._private_client().run_transaction(operations)

    def import_relations(
        self,
        data: dict[str, tuple[list[str], list[list[Any]]]],
        interned: dict[str, tuple[int, ...]] | None = None,
    ) -> None:
        self._graph._private_client().import_relations(data, interned)

    def backup(self, path: str) -> None:
        self._client.backup(path)

    def close(self) -> None:
        self._client.close()


# Columns of the pseudo-type ``Id`` hold a file path or a qualified name.  They
# are stored as ``Int`` ids of the ``intern``/``interned`` dictionary: every
# write encodes them by column name and every query result is decoded, so only
# rule text sees the ids.  Two columns that a rule unifies must both be ``Id``
# or both be ``String``; a mismatch silently matches nothing.
#
# Left as text on purpose: ``search_symbol.qualified_name`` (the in-file name,
# such as ``Class.method``, stored once per symbol and never joined),
# ``source_loc.loc_id`` (``func_qn:block`` for blocks, unique per block),
# ``import.imported_module`` and ``file_revision.file_path`` (absolute, while
# facts hold project-relative paths).  An id for a text that occurs once would
# cost a dictionary row and save nothing.
_SCHEMA = """\
{:create intern {text: String => id: Int}}

{:create interned {id: Int => text: String}}

{:create symbol {
    qualified_name: Id,
    file_path: Id
    =>
    name: String,
    kind: String,
    line: Int,
    end_line: Int,
    parent: Id default ""
}}

{:create search_symbol {
    file_path: Id,
    module_qualified_name: Id
    =>
    name: String,
    qualified_name: String,
    kind: String,
    line: Int,
    end_line: Int,
    depth: Int,
    parent: Id default "",
    signature: String default "",
    returns: String default "",
    decorators: String default ""
}}

{:create call {
    caller_qn: Id,
    callee_qn: Id,
    file_path: Id,
    line: Int,
    col: Int
    =>
    func_qn: Id default "",
    block_id: Int default -1
}}

{:create call_by_callee {
    callee_qn: Id,
    caller_qn: Id,
    file_path: Id,
    line: Int,
    col: Int
    =>
    func_qn: Id default "",
    block_id: Int default -1
}}

{:create call_by_file {
    file_path: Id,
    caller_qn: Id,
    callee_qn: Id,
    line: Int,
    col: Int
    =>
    func_qn: Id default "",
    block_id: Int default -1
}}

{:create reference {
    symbol_qn: Id,
    file_path: Id,
    line: Int,
    col: Int
    =>
    ref_kind: String,
    func_qn: Id default "",
    block_id: Int default -1
}}

{:create trace_flow {
    source_var: String,
    sink_var: String,
    label: String,
    file_path: Id,
    func_qn: Id,
    source_line: Int,
    sink_line: Int
}}

{:create type_binding {
    symbol_qn: Id,
    file_path: Id,
    line: Int,
    binding_kind: String
    =>
    type_str: String
}}

{:create import {
    importing_file: Id,
    imported_module: String,
    imported_name: String default "",
    line: Int
    =>
    alias: String default ""
}}

{:create cfg_edge {
    file_path: Id,
    func_qn: Id,
    from_block: Int,
    to_block: Int,
    edge_kind: String,
    from_line: Int,
    to_line: Int
}}

{:create flow_blob {
    file_path: Id
    =>
    payload: Bytes
}}

{:create def_use {
    file_path: Id,
    func_qn: Id,
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
    file_path: Id,
    func_qn: Id,
    receiver: String,
    method: String,
    block_id: Int,
    line: Int
}}

{:create cfg_block {
    file_path: Id,
    func_qn: Id,
    block_id: Int
    =>
    is_entry: Bool default false,
    is_exit: Bool default false
}}

{:create decorator_on {
    symbol_qn: Id,
    decorator: String,
    file_path: Id
}}

{:create source_loc {
    file_path: Id,
    loc_kind: String,
    loc_id: String
    =>
    line: Int,
    col: Int default 0,
    end_line: Int default 0,
    rel_line: Int default 0
}}

{:create func_summary {
    func_qn: Id,
    param_name: String,
    file_path: Id
    =>
    flows_to_return: Bool default false,
    flows_to_sink: Bool default false,
    sink_label: String default ""
}}

{:create entry_point_decorator { decorator: String }}

{:create entry_point_name { name: String }}

{:create entry_point_prefix { prefix: String }}

{:create exported_symbol {
    file_path: Id,
    qualified_name: Id
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

{:create file_namespace {file_path: Id => namespace: String}}

{:create ref_by_block {
    file_path: Id,
    func_qn: Id,
    block_id: Int,
    symbol_qn: Id
}}

{:create noncall_private_member_ref {
    file_path: Id,
    func_qn: Id,
    block_id: Int,
    member_name: String
}}

{:create reachable_block {
    file_path: Id,
    func_qn: Id,
    block_id: Int
}}

{:create module_level_ref {
    symbol_qn: Id,
    file_path: Id,
    line: Int
}}

{:create facts_meta {
    key: String
    =>
    value: String
}}

"""


def _interned_columns(schema: str) -> dict[str, frozenset[str]]:
    """Map each relation to its ``Id`` columns."""
    return {
        relation: frozenset(re.findall(r"(\w+): Id\b", columns))
        for relation, columns in re.findall(r"\{:create (\w+) \{([^}]*)\}\}", schema)
    }


_INTERNED_COLUMNS = _interned_columns(_SCHEMA)
_SCHEMA_INIT = _SCHEMA.replace(': Id default ""', f": Int default {ID_BASE}").replace(": Id", ": Int")


def _interned_positions(relation: str, columns: list[str]) -> tuple[int, ...]:
    """Positions, in a row of *columns*, of the cells stored as ids."""
    interned = _INTERNED_COLUMNS.get(relation, frozenset())
    return tuple(index for index, column in enumerate(columns) if column in interned)


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
        initialize_schema: bool = True,
    ) -> None:
        self._db_path = db_path
        # A published generation already has its schema.  Opening one must not
        # write to it, so its owner skips both table and relation creation.
        self._client = _create_cozo_client(db_path, prepare=initialize_schema)
        self._privatize: Callable[[str], str] | None = None
        self._privatize_lock = threading.Lock()
        self._snapshot = snapshot
        self._source_overrides = dict(source_overrides or {})
        self._source_loader = source_loader
        # Detached compatibility graphs may own a private snapshot copy. The
        # analysis owner, by contrast, keeps its generation files alive.
        self._close_unlinks_db = False
        self._revisions_by_path = {
            revision.file_path: revision for revision in snapshot.files
        } if snapshot is not None else {}
        if initialize_schema:
            _init_schema(self._client)

    def mark_shared(self, privatize: Callable[[str], str]) -> None:
        """Stop writing this graph's backing file; other readers can see it.

        ``privatize(current_path)`` must return the path of a private copy.
        It is called at most once, before this graph's first later write.
        """
        with self._privatize_lock:
            if self._privatize is None:
                self._client = _SharedGenerationClient(self, self._client)
            self._privatize = privatize

    def _private_client(self) -> Any:
        """Move this graph to a private copy of its shared generation."""
        with self._privatize_lock:
            privatize = self._privatize
            if privatize is not None:
                private_path = privatize(str(self._db_path))
                self._client = _create_cozo_client(private_path, prepare=False)
                self._db_path = private_path
                self._privatize = None
            return self._client

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

    def stored_paths_containing(self, fragment: str) -> list[str]:
        """Stored paths of the files whose absolute path contains *fragment*, ignoring case."""
        if self._snapshot is None:
            return []
        root = Path(self._snapshot.project_root)
        fragment = fragment.lower()
        paths = []
        for revision in self._snapshot.files:
            if fragment in revision.file_path.lower():
                try:
                    paths.append(str(Path(revision.file_path).relative_to(root)))
                except ValueError:
                    paths.append(revision.file_path)
        return paths

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

    def run_query(self, cozoscript: str, *, decode: bool = True) -> dict[str, Any]:
        """Execute a read-only CozoScript query and return the result dict.

        The result has keys ``headers`` (list of column names) and
        ``rows`` (list of row tuples).  Path and qualified-name columns hold
        ids inside the script, but the rows carry their text, in the order of
        that text unless the script has its own ``:order``.  To compare such a
        column with text, look its id up: ``*intern["pkg.mod.func", id]``.
        ``decode=False`` returns the stored ids.
        """
        return self._client.run(cozoscript, read_only=True, decode=decode)

    def _id(self, text: str) -> int:
        """The stored id of a path or qualified name; -1, which no row holds, if unknown."""
        return self._client.intern_ids([text])[0]

    def _ids(self, texts: Iterable[str]) -> list[int]:
        """``_id`` of each text, in one lookup."""
        return self._client.intern_ids(list(texts))

    def _known_ids(self, texts: Iterable[str]) -> list[int]:
        """Ids of the texts the graph holds; no row can name the others."""
        return [ident for ident in self._ids(texts) if ident >= 0]

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
        operations: list[tuple[Any, ...]] = [(
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

    # Flow occurrences and edges are stored as one compact value per file
    # (``flow_blob``).  A project has millions of them and nothing joins on
    # them, so a stored row each made them over half of the database.

    def _flow_blobs(self, file_path: str | None = None) -> list[tuple[str, bytes]]:
        """Return ``(file_path, payload)`` for one file, or for every file."""
        if file_path is None:
            rows = self._client.run(
                "?[file_path, payload] := *flow_blob[file_path, payload]"
            )["rows"]
        else:
            rows = self._client.run(
                "?[file_path, payload] := *flow_blob[file_path, payload], "
                "file_path == $file_path",
                {"file_path": self._id(file_path)},
            )["rows"]
        return [(row[0], row[1]) for row in rows]

    def _merge_flow_facts(
        self,
        events: dict[str, list[tuple]],
        edges: dict[str, list[tuple]],
    ) -> None:
        """Add facts to each named file's stored value, keyed like relations.

        An event replaces the stored event with the same id; an edge that is
        already stored is not repeated.
        """
        from emend import emend_core

        rows = []
        for file_path in sorted(events.keys() | edges.keys()):
            stored_events: list[tuple] = []
            stored_edges: list[tuple] = []
            for _path, payload in self._flow_blobs(file_path):
                stored_events = emend_core.decode_flow_events(payload)
                stored_edges = emend_core.decode_flow_edges(payload)
            rows.append([file_path, emend_core.encode_flow_facts(
                [*stored_events, *events.get(file_path, ())],
                [*stored_edges, *edges.get(file_path, ())],
            )])
        if rows:
            self._put_batch(
                "flow_blob", "file_path, payload", "file_path => payload", rows,
            )

    def add_flow_event(self, fact: FlowEventFact) -> None:
        """Add one value-flow occurrence fact."""
        self.add_flow_events_batch([fact])

    def add_flow_events_batch(self, facts: list[FlowEventFact]) -> None:
        """Bulk-insert value-flow occurrence facts."""
        events: dict[str, list[tuple]] = {}
        for f in facts:
            events.setdefault(f.file_path, []).append((
                f.event_id, f.func_id, f.func_name, f.func_start, f.role,
                f.var, f.access_path, f.block, f.start_byte, f.end_byte,
                f.start_line, f.start_col, f.end_line, f.end_col, f.ordinal,
                f.call_id, f.arg_index, f.arg_name, f.text,
            ))
        self._merge_flow_facts(events, {})

    def add_flow_edge(self, fact: FlowEdgeFact) -> None:
        """Add one directed value-flow edge fact."""
        self.add_flow_edges_batch([fact])

    def add_flow_edges_batch(self, facts: list[FlowEdgeFact]) -> None:
        """Bulk-insert value-flow edges.

        ``edge_kind`` is part of an edge's identity: two occurrences can have
        more than one semantic relationship between them.
        """
        edges: dict[str, list[tuple]] = {}
        for f in facts:
            edges.setdefault(f.file_path, []).append(
                (f.from_event, f.to_event, f.edge_kind)
            )
        self._merge_flow_facts({}, edges)

    def add_def_use(self, fact: DefUseFact) -> None:
        """Add a definition-use fact."""
        self.add_def_uses_batch([fact])

    # -- Batch mutation ----------------------------------------------------

    def _put_batch(
        self,
        relation: str,
        cols: str,
        schema: str,
        rows: list[list[Any]],
        operations: list[tuple[Any, ...]] | None = None,
        *, register_namespace: bool = True,
    ) -> None:
        """Run ``?[<cols>] <- $rows :put <relation> {<schema>}``.

        The cells of the relation's ``Id`` columns are given as text; the
        client stores them as ids.  Caller is responsible for skipping empty
        inserts.
        """
        columns = [column.strip() for column in cols.split(",")]
        pending: list[tuple[Any, ...]] = [] if operations is None else operations
        if register_namespace and relation != "file_namespace" and "file_path" in columns:
            from emend.language_registry import detect_language

            index = columns.index("file_path")
            pending.append((
                "candidate[file_path, namespace] <- $rows\n"
                "?[file_path, namespace] := candidate[file_path, namespace], "
                "not *file_namespace[file_path, _] :put file_namespace {file_path => namespace}",
                {"rows": [[path, detect_language(path) or ""]
                          for path in sorted({r[index] for r in rows})]},
                {"rows": (0,)},
            ))
        query = f"?[{cols}] <- $rows :put {relation} {{{schema}}}"
        interned = _interned_positions(relation, columns)
        for start in range(0, len(rows), _FACT_INSERT_BATCH_SIZE):
            params = {"rows": rows[start : start + _FACT_INSERT_BATCH_SIZE]}
            pending.append((query, params, {"rows": interned}) if interned else (query, params))
        if operations is None:
            self._client.run_transaction(pending)

    def _run_mutations(self, operations: list[tuple[Any, ...]]) -> None:
        """Run generated mutations in one synchronous Cozo transaction.

        An operation is ``(script, params)``, or ``(script, params, interned)``
        where ``interned`` maps a row parameter to the positions of the cells
        holding text that is stored as ids.
        """
        if operations:
            self._client.run_transaction(operations)

    def namespace_for_file(self, file_path: str) -> str:
        """Resolve a source file's language namespace without changing its QNs."""
        rows = self._client.run(
            "?[ns] := *file_namespace[fp, ns], fp in $paths",
            {"paths": self._ids([file_path, self.stored_path(file_path)])},
        )["rows"]
        if rows:
            return rows[0][0]
        from emend.language_registry import detect_language
        return detect_language(file_path) or ""

    def namespace_lookup(self) -> Callable[[str], str]:
        """Return ``namespace_for_file`` over one read of the namespaces.

        A query has a fixed cost of a millisecond or more, so a loop over
        files asks this instead.  The answers are as of this call.
        """
        namespaces = dict(self._client.run("?[fp, ns] := *file_namespace[fp, ns]")["rows"])
        answered: dict[str, str] = {}

        def namespace_of(file_path: str) -> str:
            namespace = answered.get(file_path)
            if namespace is None:
                namespace = namespaces.get(file_path)
                if namespace is None:
                    namespace = namespaces.get(self.stored_path(file_path))
                if namespace is None:
                    from emend.language_registry import detect_language
                    namespace = detect_language(file_path) or ""
                answered[file_path] = namespace
            return namespace

        return namespace_of

    def add_file_namespace(self, fact: FileNamespaceFact) -> None:
        self._put_batch("file_namespace", "file_path, namespace", "file_path => namespace",
                        [[fact.file_path, fact.namespace]])

    def _run_namespaced(self, query, params=None, *, namespace=None):
        """Evaluate explicit query-local views in each requested namespace.

        Bare-QN read APIs aggregate namespace-local answers. Recursive joins
        never combine edges from distinct languages. Raw run_query remains
        an unspecialized interface over the complete stored relations.
        """
        relations = {
            "symbol": "qn, fp, name, kind, line, end_line, parent",
            "call": "caller, callee, fp, line, col, fq, bid",
            "call_by_callee": "callee, caller, fp, line, col, fq, bid",
            "reference": "qn, fp, line, col, kind, fq, bid",
            "decorator_on": "qn, dec, fp",
            "func_summary": "fq, pn, fp, ftr, fts, sl",
            "exported_symbol": "fp, qn",
            "ref_by_block": "fp, fq, bid, qn",
            "method_call": "fp, fq, receiver, method, bid, line",
            "noncall_private_member_ref": "fp, fq, bid, name",
            "module_level_ref": "qn, fp, line",
            "cfg_block": "fp, fq, bid, ie, ix",
        }
        views = "".join(
            f"scope_{name}[{columns}] := *{name}[{columns}], "
            "*file_namespace[fp, $namespace]\n"
            for name, columns in relations.items() if f"scope_{name}[" in query
        )
        if not views:
            return self._client.run(query, params or {})
        namespaces = ([namespace] if namespace is not None else
                      [row[0] for row in self._client.run(
                          "?[ns] := *file_namespace[_, ns]"
                      )["rows"]])
        result = {"headers": [], "rows": []}
        for current in namespaces:
            part = self._client.run(views + query, {**(params or {}), "namespace": current})
            result["headers"] = part["headers"]
            result["rows"].extend(part["rows"])
        return result

    def _symbol_owner(self, qn: str, file_path: str) -> str:
        """Legacy bare-QN writes require an unambiguous definition owner."""
        if file_path:
            return file_path
        rows = self._client.run(
            "?[fp] := *symbol[$qn, fp, _, _, _, _, _]", {"qn": self._id(qn)},
        )["rows"]
        if len(rows) > 1:
            raise ValueError(f"Ambiguous symbol {qn!r}; provide file_path")
        return rows[0][0] if rows else ""

    def require_unambiguous_symbols(self, qnames, *, namespace=None) -> None:
        """Reject ambiguous bare names before building a destructive plan."""
        seen = set()
        namespace_of = self.namespace_lookup()
        for symbol in self.symbols():
            qn = symbol.qualified_name
            if qn not in qnames or (namespace is not None and
                                   namespace_of(symbol.file_path) != namespace):
                continue
            if qn in seen:
                raise ValueError(f"Ambiguous symbol {qn!r}; cannot plan a cascade")
            seen.add(qn)


    def add_symbols_batch(self, facts: list[SymbolFact]) -> None:
        """Bulk-insert symbol facts."""
        if not facts:
            return
        rows = [[f.qualified_name, f.file_path, f.name, f.kind, f.line, f.end_line, f.parent or ""] for f in facts]
        cols = "qualified_name, file_path, name, kind, line, end_line, parent"
        self._put_batch("symbol", cols, "qualified_name, file_path => name, kind, line, end_line, parent", rows)

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
        rows = [[f.symbol_qn, f.decorator, self._symbol_owner(f.symbol_qn, f.file_path)] for f in facts]
        self._put_batch("decorator_on", "symbol_qn, decorator, file_path", "symbol_qn, decorator, file_path", rows)

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
        rows = [[f.func_qn, f.param_name, self._symbol_owner(f.func_qn, f.file_path),
                 f.flows_to_return, f.flows_to_sink, f.sink_label] for f in facts]
        self._put_batch(
            "func_summary",
            "func_qn, param_name, file_path, flows_to_return, flows_to_sink, sink_label",
            "func_qn, param_name, file_path => flows_to_return, flows_to_sink, sink_label",
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
            params["file_path"] = self._id(file_path)

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

    def calls_from(self, caller_qn: str, *, namespace: str | None = None) -> list[CallFact]:
        """Return all calls made by *caller_qn*."""
        result = self._run_namespaced(
            "?[caller, callee, fp, line, col, fq, bid] := "
            "caller = $qn, scope_call[$qn, callee, fp, line, col, fq, bid]",
            {"qn": self._id(caller_qn)}, namespace=namespace,
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3],
                     col=r[4], func_qn=r[5], block_id=r[6])
            for r in result["rows"]
        ]

    def calls_to(self, callee_qn: str, *, namespace: str | None = None) -> list[CallFact]:
        """Return all call sites that invoke *callee_qn*."""
        result = self._run_namespaced(
            "?[caller, callee, fp, line, col, fq, bid] := "
            "callee = $qn, scope_call_by_callee[$qn, caller, fp, line, col, fq, bid]",
            {"qn": self._id(callee_qn)}, namespace=namespace,
        )
        return [
            CallFact(caller_qn=r[0], callee_qn=r[1], file_path=r[2], line=r[3],
                     col=r[4], func_qn=r[5], block_id=r[6])
            for r in result["rows"]
        ]

    def references_to(self, symbol_qn: str, *, namespace: str | None = None) -> list[ReferenceFact]:
        """Return all references to *symbol_qn*."""
        result = self._run_namespaced(
            "?[qn, fp, line, col, kind, fq, bid] := "
            "qn = $qn, scope_reference[$qn, fp, line, col, kind, fq, bid]",
            {"qn": self._id(symbol_qn)}, namespace=namespace,
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
            params["file_path"] = self._id(file_path)

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
            {"qn": self._id(symbol_qn)},
        )
        return [
            TypeFact(symbol_qn=r[0], type_str=r[1], file_path=r[2], line=r[3], binding_kind=r[4])
            for r in result["rows"]
        ]

    def imports_by_file(self) -> dict[str, list[tuple[str, str | None]]]:
        """Every file's ``(imported_module, imported_name)`` pairs, in one read.

        Each list is what ``imports_in`` gives for that stored path, in the
        same order; a file that imports nothing has no entry.
        """
        rows = self._client.run(
            "?[f, mod, name, alias, line] := *import[f, mod, name, line, alias]"
        )["rows"]
        imports: dict[str, list[tuple[str, str | None]]] = {}
        for file_path, module, name, _alias, _line in rows:
            imports.setdefault(file_path, []).append((module, name or None))
        return imports

    def imports_in(self, file_path: str) -> list[ImportFact]:
        """Return all imports declared in *file_path*."""
        result = self._client.run(
            "?[f, mod, name, alias, line] := "
            "*import[f, mod, name, line, alias], f == $fp",
            {"fp": self._id(file_path)},
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
            params["func_qn"] = self._id(func_qn)
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = self._id(file_path)
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
        """Return occurrence/value events, ordered by file and event id."""
        from emend import emend_core

        facts: list[FlowEventFact] = []
        for path, payload in self._flow_blobs(file_path):
            events = emend_core.decode_flow_events(payload)
            if func_id is not None:
                events = [event for event in events if event[1] == func_id]
            if role is not None:
                events = [event for event in events if event[4] == role]
            facts.extend(FlowEventFact(path, *event) for event in events)
        return facts

    def flow_edges(
        self,
        file_path: str | None = None,
        edge_kind: str | None = None,
    ) -> list[FlowEdgeFact]:
        """Return occurrence/value edges, ordered by file and endpoints."""
        from emend import emend_core

        facts: list[FlowEdgeFact] = []
        for path, payload in self._flow_blobs(file_path):
            edges = emend_core.decode_flow_edges(payload)
            if edge_kind is not None:
                edges = [edge for edge in edges if edge[2] == edge_kind]
            facts.extend(FlowEdgeFact(path, *edge) for edge in edges)
        return facts

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
            params["func_qn"] = self._id(func_qn)
        if var_name is not None:
            clauses.append("vn == $var_name")
            params["var_name"] = var_name
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = self._id(file_path)
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
            params["func_qn"] = self._id(func_qn)
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = self._id(file_path)
        query = "?[fp, fq, rcv, meth, bid, ln] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            MethodCallFact(
                file_path=r[0], func_qn=r[1], receiver=r[2],
                method=r[3], block_id=r[4], line=r[5],
            )
            for r in result["rows"]
        ]

    def cfg_blocks(self, func_qn: str | None = None, file_path: str | None = None) -> list[CfgBlockFact]:
        """Query CFG block facts."""
        clauses = ["*cfg_block[fp, fq, bid, ie, ix]"]
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses.append("fq == $func_qn")
            params["func_qn"] = self._id(func_qn)
        if file_path is not None:
            clauses.append("fp == $file_path")
            params["file_path"] = self._id(file_path)
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
            "?[sqn, dec, fp] := *decorator_on[sqn, dec, fp], sqn == $qn",
            {"qn": self._id(symbol_qn)},
        )
        return [DecoratorOnFact(symbol_qn=r[0], decorator=r[1], file_path=r[2]) for r in result["rows"]]

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
        clauses = ["*func_summary[fq, pn, fp, ftr, fts, sl]"]
        params: dict[str, Any] = {}
        if func_qn is not None:
            clauses.append("fq == $func_qn")
            params["func_qn"] = self._id(func_qn)
        query = "?[fq, pn, ftr, fts, sl, fp] := " + ", ".join(clauses)
        result = self._client.run(query, params)
        return [
            FuncSummaryFact(func_qn=r[0], param_name=r[1], flows_to_return=r[2],
                           flows_to_sink=r[3], sink_label=r[4], file_path=r[5])
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
            params["file_path"] = self._id(file_path)
        if qualified_name is not None:
            clauses.append("qn == $qualified_name")
            params["qualified_name"] = self._id(qualified_name)
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

    def transitive_callers(self, symbol_qn: str, max_depth: int | None = None) -> set[str]:
        """Compute the transitive set of callers of *symbol_qn* via Datalog.

        *max_depth* keeps callers at most that many calls away: 1 is the
        direct callers.  ``None`` follows every chain.
        """
        if max_depth is None:
            query = (
                "reaches[a] := scope_call_by_callee[$qn, a, _, _, _, _, _]\n"
                "reaches[a] := reaches[mid], scope_call_by_callee[mid, a, _, _, _, _, _]\n"
                "?[a] := reaches[a]"
            )
            params: dict[str, Any] = {"qn": self._id(symbol_qn)}
        else:
            # The shortest distance to each caller decides whether it is in
            # range, and lets the recursion stop on a cycle.
            query = (
                "reaches[a, min(depth)] := scope_call_by_callee[$qn, a, _, _, _, _, _], "
                "depth = 1\n"
                "reaches[a, min(depth)] := reaches[mid, nearer], nearer < $max_depth, "
                "scope_call_by_callee[mid, a, _, _, _, _, _], depth = nearer + 1\n"
                "?[a] := reaches[a, _]"
            )
            params = {"qn": self._id(symbol_qn), "max_depth": int(max_depth)}
        result = self._run_namespaced(query, params)
        return {r[0] for r in result["rows"]} - {symbol_qn}

    def transitive_callees(self, symbol_qn: str) -> set[str]:
        """Compute the transitive set of callees of *symbol_qn* via Datalog."""
        result = self._run_namespaced(
            "reaches[b] := scope_call[$qn, b, _, _, _, _, _]\n"
            "reaches[b] := reaches[mid], scope_call[mid, b, _, _, _, _, _]\n"
            "?[b] := reaches[b]",
            {"qn": self._id(symbol_qn)},
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
        result = self._run_namespaced(
            "has_ref[qn] := scope_reference[qn, _, _, _, _, _, _]\n"
            "dead[qn, fp, name, kind, line, end_line, parent] := "
            "scope_symbol[qn, fp, name, kind, line, end_line, parent], "
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

    def _entry_point_rules(
        self, entry_point_decorators=None, entry_point_names=None,
        entry_point_prefixes=None, entry_point_qualified_names=None,
    ) -> str:
        """One query-local entry-point policy for analysis and destructive planning."""
        rules = "".join(
            self._inline_relation(f"seed_ep_{kind}", ["value"], [(v,) for v in values or []])
            for kind, values in (
                ("decorator", entry_point_decorators), ("name", entry_point_names),
                ("prefix", entry_point_prefixes),
                ("qn", self._known_ids(entry_point_qualified_names or [])),
            )
        )
        return rules + (
            'entry_point[qn] := seed_ep_qn[qn]\n'
            'entry_point[qn] := scope_symbol[qn, _, name, _, _, _, _], '
            'starts_with(name, "__"), ends_with(name, "__")\n'
            'entry_point[qn] := scope_exported_symbol[_, qn]\n'
        ) + "".join(
            f'entry_point[qn] := {source}, {relation}[value], {condition}\n'
            for kind, source, condition in (
                ("prefix", 'scope_symbol[qn, _, name, _, _, _, _]', 'starts_with(name, value)'),
                ("name", 'scope_symbol[qn, _, name, _, _, _, _]', 'name == value'),
                ("decorator", 'scope_decorator_on[qn, dec, _]', 'lowercase(dec) == lowercase(value)'),
            )
            for relation in (f"*entry_point_{kind}", f"seed_ep_{kind}")
        )

    def entry_point_qualified_names(self, *, namespace=None, **options) -> set[str]:
        # A requested name the graph does not hold has no id to seed the rules
        # with; it is an entry point all the same.
        requested = list(options.get("entry_point_qualified_names") or [])
        unheld = [name for name, ident in zip(requested, self._ids(requested)) if ident < 0]
        return {row[0] for row in self._run_namespaced(
            self._entry_point_rules(**options) + '?[qn] := entry_point[qn]\n'
            + self._inline_relation("unheld_ep_qn", ["qn"], [(name,) for name in unheld])
            + '?[qn] := unheld_ep_qn[qn]',
            namespace=namespace,
        )["rows"]}

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
        namespace: str | None = None,
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
        seed_rules = self._entry_point_rules(
            entry_point_decorators, entry_point_names,
            entry_point_prefixes, entry_point_qualified_names,
        )

        # Compatibility path options resolve to exact files, just like callers
        # supplying an inventory. Never interpolate filesystem data as syntax.
        excluded_files = set(exclude_reference_files or [])
        if exclude_reference_paths or exclude_reference_segments:
            paths = [Path(path) for path in exclude_reference_paths or []]
            rows = self._run_namespaced(
                '?[fp] := scope_reference[_, fp, _, _, _, _, _]\n'
                '?[fp] := scope_method_call[fp, _, _, _, _, _]\n'
                '?[fp] := scope_noncall_private_member_ref[fp, _, _, _]'
            )["rows"]
            excluded_files.update(fp for (fp,) in rows if
                any(Path(fp).is_relative_to(path) for path in paths)
                or set(exclude_reference_segments or []) & set(Path(fp).parts[:-1]))
        excluded_file_rules = self._inline_relation(
            "excluded_reference_file", ["fp"],
            [(fp,) for fp in self._known_ids(sorted(excluded_files))],
        )
        # Module-level code has this function name; top-level symbols have
        # the empty string for a parent.
        module_fq, no_parent = self._id("<module>"), ID_BASE
        excl_clauses = ", not excluded_reference_file[fp]"
        excl_clauses_ref = ", not excluded_reference_file[ref_fp]"

        query = (
            seed_rules + excluded_file_rules +
            # Live references: from reachable code via pre-computed relations
            # (ref_by_block keyed on (fp, fq, bid, sq) joins efficiently with
            # reachable_block keyed on (fp, fq, bid))
            "reference_edge[fq, sq] := "
            "scope_ref_by_block[fp, fq, bid, sq], "
            "*reachable_block[fp, fq, bid], "
            f"sq != fq{excl_clauses}\n"

            # Without receiver types, any reachable member call may target a
            # same-named private method, including calls through local aliases.
            # Exact member names avoid suffix collisions.
            "live_private_method_name[fq, method_name] := "
            "scope_method_call[fp, fq, _, method_name, bid, _], "
            "*reachable_block[fp, fq, bid]"
            f"{excl_clauses}\n"

            'live_private_method_name[fq, method_name] := '
            f'scope_method_call[fp, fq, _, method_name, _, _], fq == {module_fq}'
            f"{excl_clauses}\n"

            # Non-call uses such as ``callbacks = [self._helper]`` join on a
            # materialized member name, avoiding a suffix-based cross product.
            "live_private_method_name[fq, method_name] := "
            "scope_noncall_private_member_ref[fp, fq, bid, method_name], "
            f"*reachable_block[fp, fq, bid]{excl_clauses}\n"

            "private_method[method_name, target_qn] := "
            "scope_symbol[target_qn, _, method_name, method_kind, _, _, _], "
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
            'scope_module_level_ref[sq, ref_fp, ref_line], '
            'scope_symbol[sq, sym_fp, _, _, sym_line, _, _], '
            f'not (ref_fp == sym_fp, ref_line == sym_line){excl_clauses_ref}\n'

            'live_ref[qn] := reference_edge[_, qn]\n'
            'live_ref[qn] := external_ref[qn]\n'

            # A private method is only actionable when its containing class is
            # itself live or externally exposed. This avoids duplicating every
            # method beneath an already-dead class.
            'live_container[qn] := live_ref[qn]\n'
            'live_container[qn] := entry_point[qn]\n'

            # Dead top-level symbols.
            "eligible[qn, fp, name, kind, line, end_line, parent] := "
            "scope_symbol[qn, fp, name, kind, line, end_line, parent], "
            f'parent == {no_parent}, '
            "not entry_point[qn]\n"

            # Private methods on live classes. Public methods stay conservative
            # because frameworks, protocols, and subclasses commonly invoke
            # them without a statically visible reference.
            "eligible[qn, fp, name, kind, line, end_line, parent] := "
            "scope_symbol[qn, fp, name, kind, line, end_line, parent], "
            f'parent != {no_parent}, '
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

        # Scoping each relation to a namespace reads all of it, whatever the
        # namespace holds.  One without a symbol (a stylesheet, markup) has no
        # dead symbol to find, so it is not asked.
        if namespace is not None and not self._client.run(
            "?[fp] := *symbol[_, fp, _, _, _, _, _], *file_namespace[fp, $namespace] "
            ":limit 1",
            {"namespace": namespace},
        )["rows"]:
            result = {"rows": []}
        else:
            result = self._run_namespaced(query, namespace=namespace)
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
            "scope_cfg_block[fp, fq, bid, _, is_exit], "
            "is_exit == false, "
            "not *reachable_block[fp, fq, bid]\n"

            "?[fp, fq, bid, ie, ix] := "
            "unreachable[fp, fq, bid], "
            "scope_cfg_block[fp, fq, bid, ie, ix]"
        )

        try:
            unreachable_result = self._run_namespaced(unreachable_query, namespace=namespace)
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
            params["fqn"] = self._id(func_qn)

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

        if max_depth <= 0:
            return {"impacted": set(), "edges": []}

        # Build depth-bounded rules by unrolling the recursion.
        # layer_0 = direct callers of changed; layer_N = callers of layer_(N-1).
        # This avoids unbounded recursion and respects max_depth exactly.
        rules = [self._inline_relation(
            "changed", ["x"], [(qn,) for qn in self._known_ids(changed_qns)],
        )]
        rules.append(
            "layer_0[caller] := changed[callee], scope_call_by_callee[callee, caller, _, _, _, _, _]\n"
        )
        for i in range(1, max_depth):
            rules.append(
                f"layer_{i}[caller] := layer_{i - 1}[mid], "
                f"scope_call_by_callee[mid, caller, _, _, _, _, _]\n"
            )
        # Union all layers into impacted_node
        for i in range(max_depth):
            rules.append(f"impacted_node[x] := layer_{i}[x]\n")
        # Edges: witness pairs — only between impacted nodes and their
        # callees that are either changed or themselves impacted.
        rules.append(
            "edge[caller, callee] := impacted_node[caller], "
            "scope_call[caller, callee, _, _, _, _, _], changed[callee]\n"
        )
        if max_depth > 1:
            # Inner edges: caller in layer_N calls mid in layer_(N-1)
            for i in range(1, max_depth):
                rules.append(
                    f"edge[caller, mid] := layer_{i}[caller], "
                    f"scope_call[caller, mid, _, _, _, _, _], layer_{i - 1}[mid]\n"
                )
        rules.append(
            "?[caller, callee] := edge[caller, callee], not changed[caller]"
        )
        query = "".join(rules)
        result = self._run_namespaced(query)
        edges = [(r[0], r[1]) for r in result["rows"]]
        impacted = {r[0] for r in result["rows"]}
        return {"impacted": impacted, "edges": edges}

    # -- Cascade dead code (Datalog negation) -------------------------------

    def cascade_dead(
        self,
        initial_deletes: set[str],
        exclude_entry_points: bool = True,
        *, namespace: str | None = None,
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

        self.require_unambiguous_symbols(initial_deletes, namespace=namespace)

        query = (
            self._inline_relation(
                "to_delete", ["x"], [(qn,) for qn in self._known_ids(initial_deletes)],
            ) +
            # A symbol has an external caller if called by something NOT
            # in the delete set.
            "has_external_caller[qn] := scope_call_by_callee[qn, caller, _, _, _, _, _], "
            "not to_delete[caller]\n"
            # Cascade targets: callees of deleted symbols with no external callers,
            # excluding the initial deletes themselves.
            "callee_of_deleted[qn] := scope_call_by_callee[qn, caller, _, _, _, _, _], to_delete[caller]\n"
            "cascade[qn] := callee_of_deleted[qn], "
            "not has_external_caller[qn], "
            "not to_delete[qn]\n"
            # Return full symbol info for cascade targets
            "?[fp, name, qn, kind, line, end_line, parent] := "
            "cascade[qn], "
            "scope_symbol[qn, fp, name, kind, line, end_line, parent]"
        )
        result = self._run_namespaced(query, namespace=namespace)
        self.require_unambiguous_symbols({r[2] for r in result["rows"]}, namespace=namespace)
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
            # When excluding callers, we only consider a symbol alive if
            # it has a call-site caller NOT in the excluded set.  We use
            # call facts (which carry the caller QN) rather than bare
            # reference facts (which don't).
            query = (
                self._inline_relation(
                    "excluded", ["x"], [(qn,) for qn in self._known_ids(exclude_qns)],
                ) +
                "alive[qn] := scope_call_by_callee[qn, caller, _, _, _, _, _], not excluded[caller]\n"
                "dead[qn, fp, name, kind, line, end_line, parent] := "
                "scope_symbol[qn, fp, name, kind, line, end_line, parent], "
                "not alive[qn]\n"
                "?[fp, name, qn, kind, line, end_line, parent] := "
                "dead[qn, fp, name, kind, line, end_line, parent]"
            )
        else:
            query = (
                'has_ref[qn] := scope_reference[qn, _, _, _, _, _, _]\n'
                'dead[qn, fp, name, kind, line, end_line, parent] := '
                'scope_symbol[qn, fp, name, kind, line, end_line, parent], '
                'not has_ref[qn]\n'
                '?[fp, name, qn, kind, line, end_line, parent] := '
                'dead[qn, fp, name, kind, line, end_line, parent]'
            )
        result = self._run_namespaced(query)
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
        namespace: str | None = None,
    ) -> list[ReferenceFact]:
        """Find all references to *symbol_qn* via Datalog query.

        Replaces Python file traversal in find_references().
        """
        clauses = ["scope_reference[$qn, fp, line, col, kind, fq, bid]"]
        params: dict[str, Any] = {"qn": self._id(symbol_qn)}

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
        result = self._run_namespaced(query, params, namespace=namespace)
        return [
            ReferenceFact(
                symbol_qn=symbol_qn, file_path=r[0], line=r[1], col=r[2],
                ref_kind=r[3], func_qn=r[4], block_id=r[5],
            )
            for r in result["rows"]
        ]

    # -- Reference lookups for interactive search --------------------------
    #
    # These read the stored relation as it is, in every language at once.  A
    # limited answer does not depend on the order ids were handed out, so it is
    # the same after a rebuild.

    def reference_locations(
        self, symbol_qn: str, *, ref_kind: str | None = None, limit: int | None = None,
    ) -> list[tuple[str, int, int, str]]:
        """Where *symbol_qn* is named: ``(file_path, line, col, ref_kind)`` rows.

        Rows are in path order, then position, and stop at *limit*.
        """
        params: dict[str, Any] = {"qn": self._id(symbol_qn)}
        kind = ""
        if ref_kind is not None:
            kind = ", kind == $kind"
            params["kind"] = ref_kind
        rows = self._client.run(
            "?[path, line, col, kind] := "
            "*reference{symbol_qn: $qn, file_path: fid, line, col, ref_kind: kind}, "
            f"*interned[fid, path]{kind} :order path, line, col"
            + (f" :limit {int(limit)}" if limit is not None else ""),
            params,
        )["rows"]
        return [tuple(row) for row in rows]

    def _referenced_names(
        self,
        fragments: list[str],
        script: str,
        params: dict[str, Any],
        *,
        limit: int,
        ignore_case: bool,
    ) -> list[list[Any]]:
        """Run *script* over the first names, in name order, that hold a fragment.

        *script* reads its candidates from ``names[nid, name]`` and returns at
        most *limit* rows in an order that starts with the name.  Not every
        candidate has a row (a path is in the dictionary too, and a name may
        have no reference in scope), so candidates are taken until enough do.

        The bridge searches the dictionary in memory.  Its first search on a
        database reads the whole dictionary; the query engine would read it on
        every search, and test each row far more slowly.
        """
        wanted = max(8 * limit, 256)
        while True:
            names = self._client.texts_containing(fragments, wanted, ignore_case=ignore_case)
            if not names:
                return []
            rows = self._client.run(
                "names[nid, name] <- $names\n" + script,
                {**params, "names": [[nid, name] for name, nid in names]},
            )["rows"]
            if len(rows) >= limit or len(names) < wanted:
                return rows
            wanted *= 8

    def references_to_names_containing(
        self, fragment: str, *, limit: int, file_paths: Iterable[str] | None = None,
    ) -> list[tuple[str, str, int, int, str]]:
        """References to names that contain *fragment*, ignoring case.

        Rows are ``(symbol_qn, file_path, line, col, ref_kind)`` in path order,
        then position.  At most *limit* are returned: those of the first
        matching names in name order, each name's in path order.  *file_paths*
        keeps only references in those stored paths.
        """
        limit = int(limit)
        params: dict[str, Any] = {}
        scope = files = ""
        if file_paths is not None:
            params["files"] = [[ident] for ident in self._known_ids(file_paths)]
            if not params["files"]:
                return []
            files = "scope[fid] <- $files\n"
            scope = "scope[fid], "
        rows = self._referenced_names(
            [fragment],
            f"{files}?[name, path, line, col, kind] := names[nid, name], "
            "*reference{symbol_qn: nid, file_path: fid, line, col, ref_kind: kind}, "
            f"{scope}*interned[fid, path] "
            f":order name, path, line, col :limit {limit}",
            params, limit=limit, ignore_case=True,
        )
        return sorted(
            (tuple(row) for row in rows),
            key=lambda row: (row[1], row[2], row[3], row[0]),
        )

    def referenced_names_containing(
        self, fragments: str | Iterable[str], *, limit: int,
    ) -> list[str]:
        """The first referenced names, in name order, that contain a fragment.

        The match is exact about case.
        """
        fragments = [fragments] if isinstance(fragments, str) else list(fragments)
        limit = int(limit)
        rows = self._referenced_names(
            fragments,
            "?[name] := names[nid, name], *reference{symbol_qn: nid} "
            f":order name :limit {limit}",
            {}, limit=limit, ignore_case=False,
        )
        return [row[0] for row in rows]

    def reference_count(self) -> int:
        """How many references the graph holds."""
        rows = self._client.run("?[count(line)] := *reference{line}")["rows"]
        return int(rows[0][0]) if rows else 0

    def callers_datalog(self, symbol_qn: str, *, namespace: str | None = None) -> list[CallFact]:
        """Find all callers of *symbol_qn* via Datalog query.

        Replaces Python file traversal in find_callers().
        """
        return self.calls_to(symbol_qn, namespace=namespace)

    def callees_datalog(self, func_qn: str, *, namespace: str | None = None) -> list[CallFact]:
        """Find all callees of *func_qn* via Datalog query.

        Uses caller_qn on call facts to find what a function calls.
        Replaces Python line-range filtering in find_callees().
        """
        result = self._run_namespaced(
            "?[caller_qn, callee_qn, fp, line, col, fq, bid] := "
            "caller_qn = $fqn, "
            "scope_call[$fqn, callee_qn, fp, line, col, fq, bid]",
            {"fqn": self._id(func_qn)}, namespace=namespace,
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
        """
        if file_path is not None:
            result = self._client.run(
                "?[caller_qn, callee_qn] := "
                "*call_by_file[$fp, caller_qn, callee_qn, _, _, _, _]",
                {"fp": self._id(file_path)},
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
            self.flow_events,
            self.flow_edges,
            self._all_file_namespaces,
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
            "decorator_on", "sqn, dec, fp",
            lambda r: DecoratorOnFact(symbol_qn=r[0], decorator=r[1], file_path=r[2]),
        )

    def _all_source_locs(self) -> list[SourceLocFact]:
        return self._query_all(
            "source_loc", "fp, lk, lid, line, col, el, rl",
            lambda r: SourceLocFact(file_path=r[0], loc_kind=r[1], loc_id=r[2],
                                    line=r[3], col=r[4], end_line=r[5], rel_line=r[6]),
        )

    def _all_func_summaries(self) -> list[FuncSummaryFact]:
        return self._query_all(
            "func_summary", "fq, pn, ftr, fts, sl, fp",
            lambda r: FuncSummaryFact(func_qn=r[0], param_name=r[1], flows_to_return=r[2],
                                      flows_to_sink=r[3], sink_label=r[4], file_path=r[5]),
            relation_cols="fq, pn, fp, ftr, fts, sl",
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

    def _all_file_namespaces(self) -> list[FileNamespaceFact]:
        return self._query_all("file_namespace", "fp, ns", lambda r: FileNamespaceFact(*r))

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
        flow_events: list[FlowEventFact] = []
        flow_edges: list[FlowEdgeFact] = []
        _TYPE_MAP: dict[str, tuple[type, Callable[..., None]]] = {
            "SymbolFact": (SymbolFact, graph.add_symbol),
            "CallFact": (CallFact, graph.add_call),
            "ReferenceFact": (ReferenceFact, graph.add_reference),
            "TraceFlowFact": (TraceFlowFact, graph.add_trace_flow),
            "TypeFact": (TypeFact, graph.add_type),
            "ImportFact": (ImportFact, graph.add_import),
            "CfgEdgeFact": (CfgEdgeFact, graph.add_cfg_edge),
            "FlowEventFact": (FlowEventFact, flow_events.append),
            "FlowEdgeFact": (FlowEdgeFact, flow_edges.append),
            "DefUseFact": (DefUseFact, graph.add_def_use),
            "MethodCallFact": (MethodCallFact, graph.add_method_call),
            "CfgBlockFact": (CfgBlockFact, graph.add_cfg_block),
            "DecoratorOnFact": (DecoratorOnFact, graph.add_decorator_on),
            "SourceLocFact": (SourceLocFact, graph.add_source_loc),
            "FuncSummaryFact": (FuncSummaryFact, graph.add_func_summary),
            "EntryPointDecoratorFact": (EntryPointDecoratorFact, graph.add_entry_point_decorator),
            "EntryPointNameFact": (EntryPointNameFact, graph.add_entry_point_name),
            "ExportedSymbolFact": (ExportedSymbolFact, graph.add_exported_symbol),
            "FileNamespaceFact": (FileNamespaceFact, graph.add_file_namespace),
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
        graph.add_flow_events_batch(flow_events)
        graph.add_flow_edges_batch(flow_edges)
        return graph

    # -- Incremental update / removal -------------------------------------

    def remove_files(
        self,
        file_paths: list[str],
        *,
        operations: list[tuple[Any, ...]] | None = None,
    ) -> None:
        """Delete all facts for the given files.

        Removes rows from every stored relation that references any of the
        supplied file paths.  Relations that key on ``file_path`` directly
        use a simple filter; relations keyed on ``symbol_qn`` or
        ``func_qn`` join through the ``symbol`` relation first.
        """
        # A path the graph has no id for has no rows.
        stored = self._known_ids(file_paths)
        if not stored:
            return

        # A relation whose key starts with the file is looked up by file.
        # ``file_path in $fps`` is a filter: it reads every row of the
        # relation, which for one changed file is nearly all of the work.
        keyed_by_file = {
            "file_namespace", "search_symbol", "call_by_file", "cfg_edge", "flow_blob",
            "def_use", "method_call", "cfg_block", "source_loc", "ref_by_block",
            "noncall_private_member_ref", "reachable_block", "exported_symbol", "import",
        }
        # The three call relations hold the same rows under three keys, so the
        # one keyed by file names the rows to remove from the other two.
        call_keys = (
            "files[file_path] <- $files\n"
            "?[{keys}] := files[file_path], "
            "*call_by_file[file_path, caller_qn, callee_qn, line, col, _, _] "
            ":rm {relation} {{{keys} => }}"
        )

        def lookup(query: str) -> str:
            head, body = query.split(" := ", 1)
            relation = body[1:body.index("[")]
            if relation in ("call", "call_by_callee"):
                return call_keys.format(relation=relation, keys=head[2:-1])
            if relation not in keyed_by_file:
                return query
            column = "importing_file" if relation == "import" else "file_path"
            atom, rest = body.split(f", {column} in $fps", 1)
            return f"files[{column}] <- $files\n{head} := files[{column}], {atom}{rest}"

        params = {"fps": stored, "files": [[ident] for ident in stored]}
        # CozoDB `:rm` requires that the query variable names in `?[...]`
        # and `:rm relation { }` match the actual column names in the
        # stored relation schema.
        for query in map(lookup, (
            # All definition metadata has explicit file ownership.
            "?[symbol_qn, decorator, file_path] := *decorator_on[symbol_qn, decorator, file_path], "
            "file_path in $fps :rm decorator_on {symbol_qn, decorator, file_path}",
            "?[func_qn, param_name, file_path] := *func_summary[func_qn, param_name, file_path, _, _, _], "
            "file_path in $fps :rm func_summary {func_qn, param_name, file_path => }",
            "?[qualified_name, file_path] := *symbol[qualified_name, file_path, _, _, _, _, _], "
            "file_path in $fps :rm symbol {qualified_name, file_path => }",
            "?[file_path] := *file_namespace[file_path, _], "
            "file_path in $fps :rm file_namespace {file_path => }",
            # search_symbol
            "?[file_path, module_qualified_name] := "
            "*search_symbol[file_path, module_qualified_name, _, _, _, _, _, _, _, _, _, _], "
            "file_path in $fps  :rm search_symbol {file_path, module_qualified_name => }",
            # call
            "?[caller_qn, callee_qn, file_path, line, col] := "
            "*call[caller_qn, callee_qn, file_path, line, col, _, _], "
            "file_path in $fps  :rm call {caller_qn, callee_qn, file_path, line, col => }",
            # call_by_callee
            "?[callee_qn, caller_qn, file_path, line, col] := "
            "*call_by_callee[callee_qn, caller_qn, file_path, line, col, _, _], "
            "file_path in $fps  :rm call_by_callee {callee_qn, caller_qn, file_path, line, col => }",
            # call_by_file
            "?[file_path, caller_qn, callee_qn, line, col] := "
            "*call_by_file[file_path, caller_qn, callee_qn, line, col, _, _], "
            "file_path in $fps  :rm call_by_file {file_path, caller_qn, callee_qn, line, col => }",
            # reference
            "?[symbol_qn, file_path, line, col] := "
            "*reference[symbol_qn, file_path, line, col, _, _, _], "
            "file_path in $fps  :rm reference {symbol_qn, file_path, line, col => }",
            # trace_flow (all keys)
            "?[source_var, sink_var, label, file_path, func_qn, source_line, sink_line] := "
            "*trace_flow[source_var, sink_var, label, file_path, func_qn, source_line, sink_line], "
            "file_path in $fps  :rm trace_flow "
            "{source_var, sink_var, label, file_path, func_qn, source_line, sink_line}",
            # type_binding
            "?[symbol_qn, file_path, line, binding_kind] := "
            "*type_binding[symbol_qn, file_path, line, binding_kind, _], "
            "file_path in $fps  :rm type_binding {symbol_qn, file_path, line, binding_kind => }",
            # cfg_edge (all keys)
            "?[file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line] := "
            "*cfg_edge[file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line], "
            "file_path in $fps  :rm cfg_edge "
            "{file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line}",
            # One stored value holds all of a file's flow occurrences.
            "?[file_path] := *flow_blob[file_path, _], "
            "file_path in $fps  :rm flow_blob {file_path => }",
            # def_use
            "?[file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line] := "
            "*def_use[file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line, _, _], "
            "file_path in $fps  :rm def_use "
            "{file_path, func_qn, var_name, kind, def_block, use_block, def_line, use_line => }",
            # method_call (all keys)
            "?[file_path, func_qn, receiver, method, block_id, line] := "
            "*method_call[file_path, func_qn, receiver, method, block_id, line], "
            "file_path in $fps  :rm method_call "
            "{file_path, func_qn, receiver, method, block_id, line}",
            # cfg_block
            "?[file_path, func_qn, block_id] := "
            "*cfg_block[file_path, func_qn, block_id, _, _], "
            "file_path in $fps  :rm cfg_block {file_path, func_qn, block_id => }",
            # source_loc
            "?[file_path, loc_kind, loc_id] := "
            "*source_loc[file_path, loc_kind, loc_id, _, _, _, _], "
            "file_path in $fps  :rm source_loc {file_path, loc_kind, loc_id => }",
            # ref_by_block (all keys)
            "?[file_path, func_qn, block_id, symbol_qn] := "
            "*ref_by_block[file_path, func_qn, block_id, symbol_qn], "
            "file_path in $fps  :rm ref_by_block "
            "{file_path, func_qn, block_id, symbol_qn}",
            # noncall_private_member_ref (all keys)
            "?[file_path, func_qn, block_id, member_name] := "
            "*noncall_private_member_ref[file_path, func_qn, block_id, member_name], "
            "file_path in $fps  :rm noncall_private_member_ref "
            "{file_path, func_qn, block_id, member_name}",
            # reachable_block (all keys)
            "?[file_path, func_qn, block_id] := "
            "*reachable_block[file_path, func_qn, block_id], "
            "file_path in $fps  :rm reachable_block {file_path, func_qn, block_id}",
            # module_level_ref
            "?[symbol_qn, file_path, line] := "
            "*module_level_ref[symbol_qn, file_path, line], "
            "file_path in $fps  :rm module_level_ref {symbol_qn, file_path, line}",
            # exported_symbol is keyed by its defining file.
            "?[file_path, qualified_name] := "
            "*exported_symbol[file_path, qualified_name], "
            "file_path in $fps  :rm exported_symbol {file_path, qualified_name}",
            # import (uses importing_file, not file_path)
            "?[importing_file, imported_module, imported_name, line] := "
            "*import[importing_file, imported_module, imported_name, line, _], "
            "importing_file in $fps  :rm import "
            "{importing_file, imported_module, imported_name, line => }",
        )):
            if operations is not None:
                operations.append((query, params))
                continue
            try:
                self._client.run(query, params)
            except Exception:
                logger.debug("Fact removal query failed", exc_info=True)

    def _insert_extracted_facts(
        self,
        extracted_files: list[ExtractedFile],
        *,
        operations: list[tuple[Any, ...]] | None = None,
    ) -> None:
        """Insert the language-neutral rows returned by the canonical extractor."""
        specs = {
            "fg_sym": ("symbol", "qualified_name, file_path, name, kind, line, end_line, parent", "qualified_name, file_path => name, kind, line, end_line, parent"),
            "search_sym": (
                "search_symbol",
                "file_path, module_qualified_name, name, qualified_name, kind, "
                "line, end_line, depth, parent, signature, returns, decorators",
                "file_path, module_qualified_name => name, qualified_name, kind, "
                "line, end_line, depth, parent, signature, returns, decorators",
            ),
            "dec": ("decorator_on", "symbol_qn, decorator, file_path", "symbol_qn, decorator, file_path"),
            "fg_refs": ("reference", "symbol_qn, file_path, line, col, ref_kind, func_qn, block_id", "symbol_qn, file_path, line, col => ref_kind, func_qn, block_id"),
            "calls": ("call", "caller_qn, callee_qn, file_path, line, col, func_qn, block_id", "caller_qn, callee_qn, file_path, line, col => func_qn, block_id"),
            "calls_by_callee": ("call_by_callee", "callee_qn, caller_qn, file_path, line, col, func_qn, block_id", "callee_qn, caller_qn, file_path, line, col => func_qn, block_id"),
            "calls_by_file": ("call_by_file", "file_path, caller_qn, callee_qn, line, col, func_qn, block_id", "file_path, caller_qn, callee_qn, line, col => func_qn, block_id"),
            "cfg_blocks": ("cfg_block", "file_path, func_qn, block_id, is_entry, is_exit", "file_path, func_qn, block_id => is_entry, is_exit"),
            "cfg_edges": ("cfg_edge", "file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line", "file_path, func_qn, from_block, to_block, edge_kind, from_line, to_line"),
            "flow_blob": ("flow_blob", "file_path, payload", "file_path => payload"),
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
            "reachable_blocks": (
                "reachable_block", "file_path, func_qn, block_id",
                "file_path, func_qn, block_id",
            ),
            "file_namespace": ("file_namespace", "file_path, namespace", "file_path => namespace"),
        }
        rows: dict[str, list[list[Any]]] = {key: [] for key in specs}
        for extracted in extracted_files:
            for key in specs:
                rows[key].extend(extracted.rows.get(key, []))

        if operations is None:
            # Nothing is removed first, so there is no script to share a
            # transaction with: put every relation's rows in one bulk write.
            data = {
                relation: ([column.strip() for column in cols.split(",")], rows[key])
                for key, (relation, cols, _schema) in specs.items() if rows[key]
            }
            if data:
                self._client.import_relations(data, {
                    relation: _interned_positions(relation, columns)
                    for relation, (columns, _rows) in data.items()
                })
            return
        for key, (relation, cols, schema) in specs.items():
            relation_rows = rows.get(key, [])
            if relation_rows:
                self._put_batch(
                    relation, cols, schema, relation_rows, operations, register_namespace=False,
                )

    def replace_extracted(
        self,
        extracted_files: list[ExtractedFile],
        *,
        stored_paths: list[str],
    ) -> None:
        """Replace path-owned rows using precomputed extraction artifacts."""
        if not stored_paths:
            self._insert_extracted_facts(extracted_files)
            return
        operations: list[tuple[Any, ...]] = []
        self.remove_files(stored_paths, operations=operations)
        self._insert_extracted_facts(extracted_files, operations=operations)
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
        from emend.analysis_linking import ModuleCatalog, link_extracted_files
        from emend.analysis_snapshot import FileRevision
        from emend.language_registry import (
            detect_language,
            get_module_separator,
            registry_and_config_snapshots,
        )

        extracted_files = []
        revisions = []
        for abs_file_path, content in file_list:
            rel_path = stored_path(abs_file_path)
            from emend.project_config import module_name_for_file

            root = resolved_project_root or Path(abs_file_path).resolve().parent
            registry, configs = registry_and_config_snapshots(root)
            file_language = detect_language(abs_file_path, registry=registry) or language
            config = configs[file_language]
            module_name = (
                module_name_for_file(
                    abs_file_path, root, language=file_language,
                    module_separator=get_module_separator(file_language, config),
                )
                if resolved_project_root is not None
                else Path(abs_file_path).stem
            )
            revision = FileRevision.create(
                root, abs_file_path, hashlib.sha256(content.encode()).hexdigest(),
                file_language, module_name, analysis_config=config,
            )
            revisions.append(revision)
            extracted_files.append(_extract_file_facts(
                revision, rel_path, content,
            ))
        extracted_files = link_extracted_files(
            extracted_files, ModuleCatalog.from_revisions(revisions)
        )
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
    blocker_locations: dict | None = None,
) -> str | None:
    """Join occurrence-local captures and CFG paths without losing their origin."""
    if errors := check.contract_errors():
        raise ValueError("; ".join(errors))
    steps = check.sequence
    if len(steps) < 2:
        return None

    rules: list[str] = []
    keys: list[list[str]] = []
    origins: dict[str, int] = {}
    effects: dict[int, tuple[str, str]] = {}
    for i, step in enumerate(steps):
        locs = step_locations.get(step.bind, [])
        if step.effect:
            parsed = _parse_effect(step.effect)
            if parsed is None or parsed[1] not in origins:
                return None
            effects[i] = parsed
            names = [parsed[1]]
        else:
            if not locs:
                return None
            names = sorted({name for *_, bindings in locs for name in bindings})
        keys.append(names)
        for name in names:
            origins.setdefault(name, i)

    variables = {name: f"v{i}" for i, name in enumerate(origins)}

    def located(name: str, captures: list[str], rows: list[tuple]) -> list[str]:
        """Rules for matched locations, which name their file and function as text.

        The stored relations hold ids, so the rows are joined with the
        dictionary once here; a location the graph has no id for has no facts.
        """
        columns = ", ".join(["block", "line", *captures])
        return [
            FactGraph._inline_relation(
                f"{name}_at", ["path", "function", "block", "line", *captures], rows,
            ).rstrip("\n"),
            f"{name}[fp, fq, {columns}] := {name}_at[path, function, {columns}], "
            "*intern[path, fp], *intern[function, fq]",
        ]

    def relation(i: int, block: str, line: str) -> str:
        args = ["fp", "fq", block, line, *(variables[name] for name in keys[i])]
        return f"step_{i}[{', '.join(args)}]"

    for i, step in enumerate(steps):
        if i not in effects:
            rules.extend(located(
                f"step_{i}", [variables[name] for name in keys[i]],
                [(fp, fq, bid, line, *(bindings.get(name) for name in keys[i]))
                 for fp, fq, bid, line, bindings in step_locations[step.bind]],
            ))
            continue
        kind, name = effects[i]
        var = variables[name]
        origin = origins[name]
        # Resolve effects for every captured value, not the first match in the project.
        seed = relation(origin, "_", "_")
        candidates = [
            f'*def_use[fp, fq, {var}, "read", block, _, line, _, _, _]'
        ] if kind == "reads" else [
            f'*def_use[fp, fq, target, kind, block, _, line, _, _, _], '
            f'kind in ["write", "aug_write"], '
            f'(target == {var} || starts_with(target, concat({var}, ".")))',
            f'*method_call[fp, fq, {var}, _, block, line]',
        ]
        for candidate in candidates:
            rules.append(f'{relation(i, "block", "line")} := {seed}, {candidate}')

    joins = [relation(i, f"block_{i}", f"line_{i}") for i in range(len(steps))]
    for i in range(len(steps) - 1):
        pair = (steps[i].bind, steps[i + 1].bind)
        blockers = (blocker_locations or {}).get(pair, {})
        # Captured blockers are scoped to the candidate binding environment.
        rows = [row for kind in ("not_through", "not_through_scope")
                for row in blockers.get(kind, [])]
        blocker_keys = sorted({name for row in rows if len(row) > 3
                               for name in row[3] if name in variables})
        capture_args = [variables[name] for name in blocker_keys]
        suffix = "".join(f", {arg}" for arg in capture_args)
        rules.extend(located(
            f"blocker_rows_{i}", capture_args,
            [(row[0], row[1], row[2], row[4] if len(row) > 4 else -1,
              *((row[3] if len(row) > 3 else {}).get(name) for name in blocker_keys))
             for row in rows],
        ))
        # Missing captures are wildcards (e.g. unconditional commit()).
        blocker_params = "".join(f", b{j}" for j in range(len(capture_args)))
        checks = "".join(f", (b{j} == null || b{j} == {var})"
                         for j, var in enumerate(capture_args))
        seeds = [relation(i, "origin", "start_line"), relation(i + 1, "endpoint", "end_line")]
        for origin in sorted({origins[name] for name in blocker_keys} - {i, i + 1}):
            seeds.append(relation(origin, "_", "_"))
        context = "fp, fq, origin, start_line, endpoint, end_line"
        # Initial/final visits cover partial blocks; intermediate visits cover
        # the whole block, including when a loop revisits an endpoint block.
        for part, condition in {
            "any": "true",
            "start": "block == origin, (line < 0 || line >= start_line)",
            "end": "block == endpoint, (line < 0 || line <= end_line)",
            "direct": "block == origin, (line < 0 || (line >= start_line && line <= end_line))",
        }.items():
            rules.append(
                f'blocked_{part}_{i}[{context}, block{suffix}] := '
                + ", ".join(seeds) + ", "
                f'blocker_rows_{i}[fp, fq, block, line{blocker_params}]{checks}, {condition}'
            )
        rules.append(
            f'reach_{i}[{context}, target{suffix}] := ' + ", ".join(seeds)
            + f', *cfg_edge[fp, fq, origin, target, _, _, _], '
            f'not blocked_start_{i}[{context}, origin{suffix}]'
        )
        rules.append(
            f'reach_{i}[{context}, target{suffix}] := '
            f'reach_{i}[{context}, source{suffix}], '
            f'*cfg_edge[fp, fq, source, target, _, _, _], '
            f'not blocked_any_{i}[{context}, source{suffix}]'
        )
        rules.append(
            f'ordered_{i}[{context}{suffix}] := '
            f'reach_{i}[{context}, endpoint{suffix}], '
            f'not blocked_end_{i}[{context}, endpoint{suffix}]'
        )
        ordering = ">" if i + 1 in effects else ">="
        rules.append(
            f'ordered_{i}[{context}{suffix}] := ' + ", ".join(seeds)
            + f', origin == endpoint, end_line {ordering} start_line, '
            f'not blocked_direct_{i}[{context}, origin{suffix}]'
        )
        joins.append(f'ordered_{i}[fp, fq, block_{i}, line_{i}, '
                     f'block_{i + 1}, line_{i + 1}{suffix}]')

    # A definition capture requires its value to reach the later occurrence.
    # A reference capture (close($FD), for example) is not a definition site.
    for i, names in enumerate(keys):
        for name in names:
            origin = origins[name]
            if origin == i:
                continue
            var = variables[name]
            live = f"live_{i}_{var}"
            rules.extend([
                f'defined_{i}_{var}[fp, fq, block, {var}] := '
                f'*def_use[fp, fq, {var}, _, block, _, _, _, _, _]',
                f'{live}[fp, fq, origin, target, {var}] := '
                f'{relation(origin, "origin", "_")}, {relation(i, "target", "_")}, '
                f'*def_use[fp, fq, {var}, _, origin, target, _, _, _, _]',
                f'{live}[fp, fq, origin, target, {var}] := '
                f'{relation(origin, "origin", "_")}, {relation(i, "target", "_")}, '
                f'not defined_{i}_{var}[fp, fq, origin, {var}]',
            ])
            joins.append(f'{live}[fp, fq, block_{origin}, block_{i}, {var}]')

    rules.append(
        '?[fp, fq, first_line, last_line] := ' + ", ".join(joins)
        + f', first_line = line_0, last_line = line_{len(steps) - 1}'
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
    if errors := check.contract_errors():
        raise ValueError("; ".join(errors))
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

    from emend.location_resolver import LocationResolver
    from emend.transform import find_pattern

    resolvers = {fp: LocationResolver.from_fact_graph(graph, fp) for fp in file_paths}

    def resolve_patterns(patterns: list[str]) -> list[tuple[str, str, int, int, dict[str, str]]]:
        locations = []
        for fp in file_paths:
            path = (resolve_root / fp) if resolve_root else Path(fp)
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                logger.debug("Could not read %s for sequence resolution", path, exc_info=True)
                continue
            for pattern in patterns:
                matches = find_pattern(pattern, str(path), source_override=source)
                for match in matches:
                    if match.line is None:
                        continue
                    location = resolvers[fp].resolve(fp, match.line)
                    if not location.is_module_level:
                        locations.append((fp, location.func_qn, location.block_id,
                                          match.line, dict(match.captures or {})))
        return locations

    step_locations = {
        step.bind: resolve_patterns([step.pattern]) if step.pattern and not step.effect else []
        for step in steps
    }
    blocker_locations = {
        (pc.from_step, pc.to_step): {
            kind: [(fp, fq, bid, bindings, line) for fp, fq, bid, line, bindings
                   in resolve_patterns(getattr(pc, kind))]
            for kind in ("not_through", "not_through_scope")
        }
        for pc in check.path_constraints
    }

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
        "bindings": {
            step: [bindings for *_, bindings in locs]
            for step, locs in step_locations.items()
        },
    }
    return query, step_data


def symbol_has_type(type_pattern: str) -> Callable[[Fact], bool]:
    """Return a predicate matching TypeFacts whose type_str matches *type_pattern*."""
    compiled = re.compile(type_pattern)

    def _predicate(fact: Fact) -> bool:
        return isinstance(fact, TypeFact) and compiled.search(fact.type_str) is not None

    return _predicate
