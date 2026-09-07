"""Dependency-neutral records for one coherent source-analysis snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Union


@dataclass(frozen=True)
class FileRevision:
    """Identity of one file within a project analysis snapshot."""

    project_root: str
    file_path: str
    content_hash: str
    language: str
    module_name: str
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
        origin: Literal["disk", "overlay"] = "disk",
        version: int | None = None,
    ) -> "FileRevision":
        return cls(
            project_root=str(Path(project_root).resolve()),
            file_path=str(Path(file_path).resolve()),
            content_hash=content_hash,
            language=language,
            module_name=module_name,
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
