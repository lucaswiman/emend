"""Dependency-neutral records for one coherent source-analysis snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Literal, Union


@dataclass(frozen=True)
class LanguageConfigRevision:
    """One immutable language configuration consumed by native analysis."""

    language: str
    payload: str
    identity: str

    @classmethod
    def create(cls, language: str, payload: str) -> "LanguageConfigRevision":
        return cls(
            language=language,
            payload=payload,
            identity=hashlib.sha256(payload.encode()).hexdigest(),
        )


@dataclass(frozen=True)
class FileRevision:
    """Identity of one file within a project analysis snapshot."""

    project_root: str
    file_path: str
    content_hash: str
    language: str
    module_name: str
    analysis_config: LanguageConfigRevision | None = field(
        default=None, compare=False, repr=False
    )
    origin: Literal["disk", "overlay"] = "disk"
    version: int | None = None

    @classmethod
    def create(
        cls,
        project_root: str | Path,
        file_path: str | Path,
        content_hash: str,
        language: str,
        module_name: str,
        *,
        analysis_config: LanguageConfigRevision | None = None,
        origin: Literal["disk", "overlay"] = "disk",
        version: int | None = None,
    ) -> "FileRevision":
        return cls(
            project_root=str(Path(project_root).resolve()),
            file_path=str(Path(file_path).resolve()),
            content_hash=content_hash,
            language=language,
            module_name=module_name,
            analysis_config=analysis_config,
            origin=origin,
            version=version,
        )


@dataclass(frozen=True)
class ExtractedFile:
    """All language-neutral rows extracted from one exact file revision."""

    revision: FileRevision
    qnames: frozenset[str] = frozenset()
    rows: dict[str, list[list[object]]] = field(default_factory=dict)

    def __getitem__(self, key: str) -> list[list[object]]:
        return self.rows[key]


@dataclass(frozen=True)
class AnalysisSnapshot:
    """A coherent set of source revisions for one project scope."""

    project_root: str
    snapshot_id: str
    files: tuple[FileRevision, ...]
    base_snapshot_id: str | None = None
    analysis_context_id: str = ""


@dataclass(frozen=True)
class SymbolFact:
    file_path: str
    name: str
    qualified_name: str
    kind: str
    line: int
    end_line: int
    parent: str | None = None


@dataclass(frozen=True)
class CallFact:
    caller_qn: str
    callee_qn: str
    file_path: str
    line: int
    col: int
    func_qn: str = ""
    block_id: int = -1


@dataclass(frozen=True)
class ReferenceFact:
    symbol_qn: str
    file_path: str
    line: int
    col: int
    ref_kind: Literal["read", "write", "call", "import", "definition"]
    func_qn: str = ""
    block_id: int = -1


@dataclass(frozen=True)
class TraceFlowFact:
    source_var: str
    sink_var: str
    label: str
    file_path: str
    func_qn: str
    source_line: int
    sink_line: int


@dataclass(frozen=True)
class TypeFact:
    symbol_qn: str
    type_str: str
    file_path: str
    line: int
    binding_kind: str


@dataclass(frozen=True)
class ImportFact:
    importing_file: str
    imported_module: str
    imported_name: str | None
    alias: str | None
    line: int


@dataclass(frozen=True)
class CfgEdgeFact:
    file_path: str
    func_qn: str
    from_block: int
    to_block: int
    edge_kind: str
    from_line: int
    to_line: int


@dataclass(frozen=True)
class FlowEventFact:
    """One ordered value-flow occurrence in a source file.

    ``event_id`` is local to the extracted file revision.  Keeping the file
    path in the fact (rather than relying on the caller to provide it) makes
    the relation removable with the rest of a file's facts.
    """

    file_path: str
    event_id: int
    func_id: str
    func_name: str
    func_start: int
    role: str
    var: str | None
    access_path: str | None
    block: int
    start_byte: int
    end_byte: int
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    ordinal: int
    call_id: int | None = None
    arg_index: int | None = None
    arg_name: str | None = None
    text: str = ""

    @property
    def id(self) -> int:
        """Rust API spelling for the local occurrence identifier."""
        return self.event_id

    @property
    def start_column(self) -> int:
        """Compatibility spelling used by some Python callers."""
        return self.start_col

    @property
    def end_column(self) -> int:
        """Compatibility spelling used by some Python callers."""
        return self.end_col

    @property
    def line(self) -> int:
        return self.start_line

    @property
    def col(self) -> int:
        return self.start_col

    @property
    def block_id(self) -> int:
        return self.block


@dataclass(frozen=True)
class FlowEdgeFact:
    """A directed relationship between two value-flow occurrences."""

    file_path: str
    from_event: int
    to_event: int
    edge_kind: str

    @property
    def from_id(self) -> int:
        return self.from_event

    @property
    def to_id(self) -> int:
        return self.to_event


@dataclass(frozen=True)
class DefUseFact:
    file_path: str
    func_qn: str
    var_name: str
    kind: str = "write"
    def_block: int = 0
    use_block: int = 0
    def_line: int = 0
    def_col: int = 0
    use_line: int = 0
    use_col: int = 0


@dataclass(frozen=True)
class MethodCallFact:
    file_path: str
    func_qn: str
    receiver: str
    method: str
    block_id: int = 0
    line: int = 0


@dataclass(frozen=True)
class CfgBlockFact:
    file_path: str
    func_qn: str
    block_id: int
    is_entry: bool = False
    is_exit: bool = False


@dataclass(frozen=True)
class DecoratorOnFact:
    symbol_qn: str
    decorator: str


@dataclass(frozen=True)
class SourceLocFact:
    file_path: str
    loc_kind: str
    loc_id: str
    line: int
    col: int = 0
    end_line: int = 0
    rel_line: int = 0


@dataclass(frozen=True)
class FuncSummaryFact:
    func_qn: str
    param_name: str
    flows_to_return: bool = False
    flows_to_sink: bool = False
    sink_label: str = ""


@dataclass(frozen=True)
class EntryPointDecoratorFact:
    decorator: str


@dataclass(frozen=True)
class EntryPointNameFact:
    name: str


@dataclass(frozen=True)
class ExportedSymbolFact:
    file_path: str
    qualified_name: str


Fact = Union[
    SymbolFact,
    CallFact,
    ReferenceFact,
    TraceFlowFact,
    TypeFact,
    ImportFact,
    CfgEdgeFact,
    FlowEventFact,
    FlowEdgeFact,
    DefUseFact,
    MethodCallFact,
    CfgBlockFact,
    DecoratorOnFact,
    SourceLocFact,
    FuncSummaryFact,
    EntryPointDecoratorFact,
    EntryPointNameFact,
    ExportedSymbolFact,
]
