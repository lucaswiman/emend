"""Immutable, engine-neutral representation of a compiled rules document."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union


FrozenValue = Union[None, bool, int, float, str, tuple["FrozenValue", ...], "FrozenOptions"]


@dataclass(frozen=True)
class FrozenOptions(Mapping[str, FrozenValue]):
    """Recursively immutable storage for open-ended check options."""

    entries: tuple[tuple[str, FrozenValue], ...] = ()

    def __getitem__(self, key: str) -> FrozenValue:
        for name, value in self.entries:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def freeze_options(values: Mapping[str, Any] | None = None) -> FrozenOptions:
    def freeze(value: Any) -> FrozenValue:
        if isinstance(value, Mapping):
            return freeze_options({str(key): item for key, item in value.items()})
        if isinstance(value, (list, tuple)):
            return tuple(freeze(item) for item in value)
        return value if value is None or isinstance(value, (bool, int, float, str)) else str(value)

    return FrozenOptions(tuple((str(key), freeze(value)) for key, value in (values or {}).items()))


@dataclass(frozen=True)
class CompiledEndpoint:
    pattern: str = ""
    effect: str = ""
    type_constraint: str = ""
    options: FrozenOptions = field(default_factory=FrozenOptions)


@dataclass(frozen=True)
class CompiledFlowRule:
    """The sole executable representation of a flow rule."""

    rule_id: str
    name: str
    label: str
    message: str
    sources: tuple[CompiledEndpoint, ...]
    sinks: tuple[CompiledEndpoint, ...]
    sanitizers: tuple[CompiledEndpoint, ...] = ()
    scope_sanitizers: tuple[CompiledEndpoint, ...] = ()
    quantifier: str = "all_paths"
    severity: str = "warning"
    languages: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    enabled: bool = True
    options: FrozenOptions = field(default_factory=FrozenOptions)


@dataclass(frozen=True)
class CompiledPatternRule:
    rule_id: str
    name: str
    pattern: str
    message: str
    not_inside: str | None = None
    replace: str | None = None
    dsl: str | None = None
    severity: str = "warning"
    languages: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    enabled: bool = True
    options: FrozenOptions = field(default_factory=FrozenOptions)


@dataclass(frozen=True)
class CompiledDeadCodeRule:
    rule_id: str = "deadcode"
    enabled: bool = False
    rule_name: str = "deadcode"
    kind: str | None = None
    include_private: bool = True
    exclude_references_from: tuple[str, ...] = ()
    exclude_test_references: bool = True
    strings_count_as_references: bool = True
    unused_modules: bool = True
    message: str = "Symbol appears to be unused"
    entry_point_decorators: tuple[str, ...] = ()
    entry_point_names: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    options: FrozenOptions = field(default_factory=FrozenOptions)


@dataclass(frozen=True)
class CompiledDuplicateRule:
    rule_id: str = "duplicate-code"
    enabled: bool = False
    rule_name: str = "duplicate-code"
    mode: str = "all"
    min_lines: int = 5
    min_score: float = 50.0
    cross_file_only: bool = True
    exclude_tests: bool = True
    exclude_generated: bool = True
    message: str = "Duplicate code detected"
    options: FrozenOptions = field(default_factory=FrozenOptions)


@dataclass(frozen=True)
class CompiledStructuralCheck:
    pattern: str
    inside: str | None = None
    not_inside: str | None = None
    where: str | None = None


@dataclass(frozen=True)
class CompiledTypeCheck:
    symbol_pattern: str
    expected_type: str
    kind: str = "has_type"


@dataclass(frozen=True)
class CompiledCustomCheck:
    query_source: str


@dataclass(frozen=True)
class CompiledDatalogCheck:
    cozoscript: str


@dataclass(frozen=True)
class CompiledSequenceStep:
    bind: str
    pattern: str | None = None
    effect: str | None = None
    type_constraint: str | None = None


@dataclass(frozen=True)
class CompiledSequencePath:
    from_step: str
    to_step: str
    not_through: tuple[str, ...] = ()
    not_through_scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledSequenceCheck:
    name: str
    message: str
    sequence: tuple[CompiledSequenceStep, ...]
    path_constraints: tuple[CompiledSequencePath, ...] = ()
    severity: str = "error"


CheckPayload = Union[
    CompiledFlowRule, CompiledDeadCodeRule, CompiledStructuralCheck,
    CompiledTypeCheck, CompiledCustomCheck, CompiledDatalogCheck,
    CompiledSequenceCheck, FrozenOptions,
]


@dataclass(frozen=True)
class CompiledPolicyCheck:
    rule_id: str
    kind: str
    payload: CheckPayload


@dataclass(frozen=True)
class CompiledPolicy:
    rule_id: str
    name: str
    description: str
    severity: str
    checks: tuple[CompiledPolicyCheck, ...]
    enabled: bool = True
    options: FrozenOptions = field(default_factory=FrozenOptions)


@dataclass(frozen=True)
class CompiledTraceEndpoint:
    label: str
    endpoint: CompiledEndpoint
    message: str = ""
    quantifier: str = "all_paths"


@dataclass(frozen=True)
class CompiledTraceConfig:
    labels: tuple[str, ...] = ()
    sources: tuple[CompiledTraceEndpoint, ...] = ()
    sinks: tuple[CompiledTraceEndpoint, ...] = ()
    sanitizers: tuple[CompiledTraceEndpoint, ...] = ()
    scope_sanitizers: tuple[CompiledTraceEndpoint, ...] = ()
    flow_rules: tuple[CompiledFlowRule, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    presets: tuple[str, ...] = ()
    options: FrozenOptions = field(default_factory=FrozenOptions)


@dataclass(frozen=True)
class CompiledRuleDocument:
    source_path: Path | None = None
    has_rules_section: bool = False
    has_policies_section: bool = False
    macros: FrozenOptions = field(default_factory=FrozenOptions)
    pattern_rules: tuple[CompiledPatternRule, ...] = ()
    flow_rules: tuple[CompiledFlowRule, ...] = ()
    deadcode: CompiledDeadCodeRule | None = None
    duplicate: CompiledDuplicateRule | None = None
    policies: tuple[CompiledPolicy, ...] = ()
    unified_policies: tuple[CompiledPolicy, ...] = ()
    trace: CompiledTraceConfig = field(default_factory=CompiledTraceConfig)
    options: FrozenOptions = field(default_factory=FrozenOptions)

    @property
    def policy_flow_rules(self) -> tuple[CompiledFlowRule, ...]:
        return tuple(check.payload for policy in self.policies for check in policy.checks
                     if isinstance(check.payload, CompiledFlowRule))

    @property
    def trace_flow_rules(self) -> tuple[CompiledFlowRule, ...]:
        return self.trace.flow_rules + self.flow_rules

    @property
    def all_flow_rules(self) -> tuple[CompiledFlowRule, ...]:
        return self.flow_rules + self.policy_flow_rules + self.trace.flow_rules
