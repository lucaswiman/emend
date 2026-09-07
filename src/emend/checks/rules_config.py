"""Shared helpers for loading emend rule/policy configs."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from emend.checks.rule_model import (
    CompiledCustomCheck,
    CompiledDatalogCheck,
    CompiledDeadCodeRule,
    CompiledDuplicateRule,
    CompiledEndpoint,
    CompiledFlowRule,
    CompiledPatternRule,
    CompiledPolicy,
    CompiledPolicyCheck,
    CompiledRuleDocument,
    CompiledSequenceCheck,
    CompiledSequencePath,
    CompiledSequenceStep,
    CompiledStructuralCheck,
    CompiledTraceConfig,
    CompiledTraceEndpoint,
    CompiledTypeCheck,
    FrozenOptions,
    freeze_options,
)


DEFAULT_RULES_PATH = Path(".emend/rules.yaml")


@dataclass
class DeadCodeConfig:
    """Shared configuration for dead-code detection.

    Used by both the lint engine (where ``enabled``/``rule_name``/``message``
    control whether and how the ``deadcode`` rule runs) and the policy engine
    (which only consults the entry-point and exclude-path fields; membership
    in a ``Policy.checks`` list already answers "should this run").
    """
    enabled: bool = False
    rule_name: str = "deadcode"
    kind: str | None = None
    include_private: bool = True
    exclude_references_from: list[str] | None = None
    exclude_test_references: bool = True
    strings_count_as_references: bool = True
    unused_modules: bool = True
    message: str = "Symbol appears to be unused"
    entry_point_decorators: list[str] | None = None
    entry_point_names: list[str] | None = None
    exclude_paths: list[str] | None = None


def coerce_optional_str_list(value: object) -> list[str] | None:
    values = [str(item) for item in as_list(value)]
    return values or None


def parse_deadcode_config(
    raw: object,
    *,
    rule_name: str = "deadcode",
) -> DeadCodeConfig | None:
    """Parse the canonical dead-code mapping used by lint and policy."""
    compiled = _compile_deadcode(raw, rule_name=rule_name)
    return compiled_deadcode_to_config(compiled) if compiled else None


def compiled_deadcode_to_config(config: CompiledDeadCodeRule) -> DeadCodeConfig:
    """Adapt the immutable model to the established lint/policy object."""
    def optional(values: tuple[str, ...]) -> list[str] | None:
        return list(values) or None

    return DeadCodeConfig(
        enabled=config.enabled,
        rule_name=config.rule_name,
        kind=config.kind,
        include_private=config.include_private,
        exclude_references_from=optional(config.exclude_references_from),
        exclude_test_references=config.exclude_test_references,
        strings_count_as_references=config.strings_count_as_references,
        unused_modules=config.unused_modules,
        message=config.message,
        entry_point_decorators=optional(config.entry_point_decorators),
        entry_point_names=optional(config.entry_point_names),
        exclude_paths=optional(config.exclude_paths),
    )


def compiled_duplicate_to_config(config: CompiledDuplicateRule) -> Any:
    """Adapt the immutable model without making rules_config depend cyclically."""
    from emend.checks.duplicates import DuplicateCodeConfig
    return DuplicateCodeConfig(
        enabled=config.enabled,
        rule_name=config.rule_name,
        mode=config.mode,
        min_lines=config.min_lines,
        min_score=config.min_score,
        cross_file_only=config.cross_file_only,
        exclude_tests=config.exclude_tests,
        exclude_generated=config.exclude_generated,
        message=config.message,
    )


def deadcode_engine_kwargs(
    config: DeadCodeConfig,
    *,
    show_last_reference: bool = False,
) -> dict[str, Any]:
    """Translate shared config into ``find_dead_code`` keyword arguments."""
    return {
        "kind": config.kind,
        "include_private": config.include_private,
        "exclude_references_from": config.exclude_references_from,
        "exclude_test_references": config.exclude_test_references,
        "strings_count_as_references": config.strings_count_as_references,
        "show_last_reference": show_last_reference,
        "entry_point_decorators": config.entry_point_decorators,
        "entry_point_names": config.entry_point_names,
        "exclude_paths": config.exclude_paths,
        "unused_modules": config.unused_modules,
    }


def load_rules_document(
    config_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Load a rules document from the canonical path or a given path."""
    if config_path is None:
        resolved = DEFAULT_RULES_PATH if DEFAULT_RULES_PATH.exists() else None
        if resolved is None:
            raise FileNotFoundError(f"Config file not found: {DEFAULT_RULES_PATH}")
    else:
        resolved = Path(config_path)
        if not resolved.exists():
            raise FileNotFoundError(f"Config file not found: {resolved}")

    with open(resolved) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {resolved}")
    return data, resolved


def load_yaml_config_with_fallback(
    config_path: str | Path,
    *,
    legacy_names: Iterable[str] = (),
) -> tuple[dict[str, Any], Path]:
    """Load a YAML mapping, optionally falling back to ``rules.yaml``.

    If *config_path* does not exist and its filename is one of *legacy_names*,
    this will look for a sibling ``rules.yaml``.
    """
    requested = Path(config_path)
    resolved = resolve_config_path_with_fallback(
        requested,
        legacy_names=legacy_names,
    )
    if resolved is None:
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(resolved) as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {resolved}")
    return data, resolved


def resolve_config_path_with_fallback(
    config_path: str | Path,
    *,
    legacy_names: Iterable[str] = (),
) -> Path | None:
    """Resolve an existing config path with optional legacy fallback."""
    path = Path(config_path)
    if path.exists():
        return path

    legacy = set(legacy_names)
    if path.name in legacy:
        candidate = path.with_name("rules.yaml")
        if candidate.exists():
            return candidate
    return None


def resolve_rules_path(
    config_path: str | Path | None = None,
) -> Path:
    """Resolve the active rules config path."""
    if config_path is None:
        return DEFAULT_RULES_PATH
    return Path(config_path)


def yaml_key(raw: dict[str, Any], *keys: str) -> Any:
    """Look up a key by trying underscore and hyphen variants."""
    for key in keys:
        if key in raw:
            return raw[key]
        alt = key.replace("_", "-")
        if alt in raw:
            return raw[alt]
    return None


def as_list(value: Any) -> list[Any]:
    """Coerce scalar-or-list config values to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def expand_pattern_macros(pattern: str | None, macros: dict[str, str]) -> str | None:
    """Expand ``{macro_name}`` references in a pattern-like string."""
    if pattern is None:
        return None
    expanded = pattern
    for name, replacement in macros.items():
        expanded = expanded.replace(f"{{{name}}}", replacement)
    return expanded


def expand_macros(pattern: str, macros: dict[str, str]) -> str:
    """Expand macros in a required pattern string."""
    return expand_pattern_macros(pattern, macros) or ""


def _pattern_text(value: Any, macros: dict[str, str]) -> str:
    """Extract and expand a pattern from scalar or ``{pattern: ...}`` data."""
    if isinstance(value, dict):
        value = value.get("pattern", "")
    return expand_macros(str(value), macros) if value else ""


def normalize_pattern_list(value: Any, macros: dict[str, str] | None = None) -> list[str]:
    """Normalize scalar/list pattern config without changing pattern syntax."""
    macro_map = macros or {}
    return [pattern for item in as_list(value) if (pattern := _pattern_text(item, macro_map))]


def expand_not_through(
    not_through: Any, macros: dict[str, str],
) -> list[str] | str | None:
    """Expand ``not_through`` into independent alternative patterns.

    Each sanitizer is matched separately by the flow engine.  Joining values
    with ``|`` changes the meaning for pattern languages that treat that text
    literally, and also loses the distinction between configured alternatives.
    """
    patterns = normalize_pattern_list(not_through, macros)
    if not patterns:
        return None
    # Preserve the historical scalar API while keeping multiple alternatives
    # as a list.  The flow executor normalizes both forms before matching.
    return patterns[0] if not isinstance(not_through, list) else patterns


def normalize_flow_definition(
    raw: dict[str, Any], macros: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Normalize nested and legacy flow-rule spellings in one place.

    The returned patterns are expanded, while endpoint dictionaries remain
    available to callers through ``from_raw``/``to_raw`` for type metadata.
    """
    macro_map = macros or {}
    nested = raw.get("flow")
    flow = nested if isinstance(nested, dict) else raw
    flow_from = yaml_key(flow, "from", "flows_from")
    flow_to = yaml_key(flow, "to", "flows_to")
    if flow_from is None:
        flow_from = yaml_key(raw, "flows_from")
    if flow_to is None:
        flow_to = yaml_key(raw, "flows_to")

    not_through = yaml_key(flow, "not_through")
    if not_through is None:
        not_through = yaml_key(raw, "not_through")
    not_through_scope = yaml_key(flow, "not_through_scope", "scope_sanitizers")
    if not_through_scope is None:
        not_through_scope = yaml_key(raw, "not_through_scope", "scope_sanitizers")

    return {
        "flow": flow,
        "from_raw": flow_from,
        "to_raw": flow_to,
        "from": _pattern_text(flow_from, macro_map),
        "to": _pattern_text(flow_to, macro_map),
        "not_through": expand_not_through(not_through, macro_map),
        "not_through_scope": normalize_pattern_list(not_through_scope, macro_map),
        "label": flow.get("label") or raw.get("label"),
        "quantifier": flow.get("quantifier", "all_paths"),
        "effect": flow.get("effect", ""),
    }


# ---------------------------------------------------------------------------
# Canonical document compiler
# ---------------------------------------------------------------------------

def _str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in as_list(value))


def _macro_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(name): str(value) for name, value in raw.items()}


def _unknown_options(raw: dict[str, Any], known: set[str]) -> FrozenOptions:
    return freeze_options({
        key: value for key, value in raw.items()
        if key not in known and key.replace("-", "_") not in known
    })


def _endpoint(raw: Any, macros: dict[str, str]) -> CompiledEndpoint:
    if isinstance(raw, dict):
        pattern = _pattern_text(raw.get("pattern"), macros)
        effect = expand_macros(str(raw.get("effect", "")), macros)
        constraint = str(yaml_key(raw, "type_constraint") or "")
        options = _unknown_options(raw, {"pattern", "effect", "type_constraint"})
    else:
        pattern = _pattern_text(raw, macros)
        effect = ""
        constraint = ""
        options = FrozenOptions()
    return CompiledEndpoint(pattern, effect, constraint, options)


def _effect_endpoint(endpoint: CompiledEndpoint) -> CompiledEndpoint:
    stripped = endpoint.pattern.strip()
    predicate, separator, _argument = stripped.partition("(")
    if (
        not endpoint.effect
        and stripped.endswith(")")
        and separator
        and predicate.strip() in {"writes", "reads"}
    ):
        return CompiledEndpoint(
            effect=stripped,
            type_constraint=endpoint.type_constraint,
            options=endpoint.options,
        )
    return endpoint


def compile_flow_rule(
    raw: dict[str, Any],
    *,
    rule_id: str,
    name: str,
    macros: dict[str, str] | None = None,
    message: str = "",
    severity: str = "warning",
    label: str | None = None,
    exclude_paths: tuple[str, ...] = (),
) -> CompiledFlowRule | None:
    """Compile any accepted nested/legacy flow spelling into one model."""
    macro_map = macros or {}
    normalized = normalize_flow_definition(raw, macro_map)
    flow = normalized["flow"]
    source = _endpoint(normalized["from_raw"], macro_map)
    sink = _effect_endpoint(_endpoint(normalized["to_raw"], macro_map))
    flow_constraint = str(yaml_key(flow, "type_constraint") or "")
    if flow_constraint and not source.type_constraint:
        source = CompiledEndpoint(
            source.pattern, source.effect, flow_constraint, source.options,
        )
    if flow_constraint and not sink.type_constraint:
        sink = CompiledEndpoint(
            sink.pattern, sink.effect, flow_constraint, sink.options,
        )
    flow_effect = expand_macros(str(flow.get("effect", "")), macro_map)
    if flow_effect and not sink.effect:
        sink = CompiledEndpoint(
            sink.pattern, flow_effect, sink.type_constraint, sink.options,
        )
    if not (source.pattern or source.effect) or not (sink.pattern or sink.effect):
        return None

    sanitizer_values = yaml_key(flow, "not_through")
    if sanitizer_values is None:
        sanitizer_values = yaml_key(raw, "not_through")
    sanitizers = tuple(
        endpoint for value in as_list(sanitizer_values)
        if (endpoint := _endpoint(value, macro_map)).pattern or endpoint.effect
    )
    if flow_constraint:
        sanitizers = tuple(
            endpoint if endpoint.type_constraint else CompiledEndpoint(
                endpoint.pattern, endpoint.effect, flow_constraint, endpoint.options,
            )
            for endpoint in sanitizers
        )
    scope_values = yaml_key(flow, "not_through_scope", "scope_sanitizers")
    if scope_values is None:
        scope_values = yaml_key(raw, "not_through_scope", "scope_sanitizers")
    scope_sanitizers = tuple(
        endpoint for value in as_list(scope_values)
        if (endpoint := _endpoint(value, macro_map)).pattern or endpoint.effect
    )
    languages = _str_tuple(flow.get("language", raw.get("language")))
    files = _str_tuple(flow.get("files", flow.get("file", raw.get("files", raw.get("file")))))
    local_excludes = _str_tuple(yaml_key(flow, "exclude_paths")) or _str_tuple(
        yaml_key(raw, "exclude_paths")
    )
    known = {
        "from", "to", "flows_from", "flows_to", "not_through",
        "not_through_scope", "scope_sanitizers", "label", "quantifier",
        "effect", "type_constraint", "language", "file", "files",
        "exclude_paths", "enabled", "options",
    }
    option_values = dict(flow.get("options") or {}) if isinstance(flow.get("options"), dict) else {}
    option_values.update(dict(_unknown_options(flow, known)))
    return CompiledFlowRule(
        rule_id=rule_id,
        name=name,
        label=str(flow.get("label") or raw.get("label") or label or name),
        message=str(raw.get("message", message)),
        sources=(source,),
        sinks=(sink,),
        sanitizers=sanitizers,
        scope_sanitizers=scope_sanitizers,
        quantifier=str(flow.get("quantifier", "all_paths")),
        severity=str(raw.get("severity", severity)),
        languages=languages,
        files=files,
        exclude_paths=local_excludes or exclude_paths,
        enabled=bool(flow.get("enabled", raw.get("enabled", True))),
        options=freeze_options(option_values),
    )


def _compile_deadcode(
    raw: object,
    *,
    rule_name: str = "deadcode",
    rule_id: str | None = None,
) -> CompiledDeadCodeRule | None:
    if raw is None or (not isinstance(raw, (bool, dict))):
        return None
    values = raw if isinstance(raw, dict) else {}
    entry_points = values.get("entry-points")
    decorators = yaml_key(values, "entry_point_decorators")
    names = yaml_key(values, "entry_point_names")
    if isinstance(entry_points, dict):
        decorators = decorators or entry_points.get("decorators")
        names = names or entry_points.get("names")
    known = {
        "enabled", "kind", "include_private", "exclude_references_from",
        "exclude_test_references", "include_test_references",
        "strings_count_as_references", "unused_modules", "message",
        "entry_point_decorators", "entry_point_names", "entry_points",
        "exclude_paths", "options",
    }
    option_values = dict(values.get("options") or {}) if isinstance(values.get("options"), dict) else {}
    option_values.update(dict(_unknown_options(values, known)))
    return CompiledDeadCodeRule(
        rule_id=rule_id or f"rule:{rule_name}:deadcode",
        enabled=raw if isinstance(raw, bool) else bool(values.get("enabled", True)),
        rule_name=rule_name,
        kind=values.get("kind"),
        include_private=bool(values.get("include-private", True)),
        exclude_references_from=_str_tuple(yaml_key(values, "exclude_references_from")),
        exclude_test_references=bool(values.get(
            "exclude-test-references", not values.get("include-test-references", False),
        )),
        strings_count_as_references=bool(values.get("strings-count-as-references", True)),
        unused_modules=bool(values.get("unused-modules", True)),
        message=str(values.get("message", "Symbol appears to be unused")),
        entry_point_decorators=_str_tuple(decorators),
        entry_point_names=_str_tuple(names),
        exclude_paths=_str_tuple(yaml_key(values, "exclude_paths")),
        options=freeze_options(option_values),
    )


def _compile_duplicate(raw: object) -> CompiledDuplicateRule | None:
    if raw is None or not isinstance(raw, (bool, dict)):
        return None
    values = raw if isinstance(raw, dict) else {}
    known = {
        "enabled", "mode", "min_lines", "min_score", "cross_file_only",
        "exclude_tests", "exclude_generated", "message", "options",
    }
    option_values = dict(values.get("options") or {}) if isinstance(values.get("options"), dict) else {}
    option_values.update(dict(_unknown_options(values, known)))
    min_lines = yaml_key(values, "min_lines")
    min_score = yaml_key(values, "min_score")
    return CompiledDuplicateRule(
        enabled=raw if isinstance(raw, bool) else bool(values.get("enabled", True)),
        mode=str(values.get("mode", "all")),
        min_lines=int(5 if min_lines is None else min_lines),
        min_score=float(50.0 if min_score is None else min_score),
        cross_file_only=bool(values.get("cross-file-only", values.get("cross_file_only", True))),
        exclude_tests=bool(values.get("exclude-tests", values.get("exclude_tests", True))),
        exclude_generated=bool(values.get("exclude-generated", values.get("exclude_generated", True))),
        message=str(values.get("message", "Duplicate code detected")),
        options=freeze_options(option_values),
    )


def _compiled_pattern(
    name: str, raw: dict[str, Any], macros: dict[str, str],
) -> CompiledPatternRule:
    flow = normalize_flow_definition(raw, macros)
    is_flow = bool(flow["from"] and flow["to"])
    pattern = (
        str(raw.get("find", "")) if is_flow
        else expand_macros(str(raw.get("match", raw.get("find", ""))), macros)
    )
    known = {
        "match", "find", "message", "not_within", "not_inside", "fix",
        "replace", "flow", "flows_from", "flows_to", "not_through", "dsl",
        "files", "language", "severity", "enabled", "label", "options",
    }
    option_values = dict(raw.get("options") or {}) if isinstance(raw.get("options"), dict) else {}
    option_values.update(dict(_unknown_options(raw, known)))
    return CompiledPatternRule(
        rule_id=f"rule:{name}:pattern",
        name=name,
        pattern=pattern,
        message=str(raw.get("message", "")),
        not_inside=expand_pattern_macros(yaml_key(raw, "not_within", "not_inside"), macros),
        replace=expand_pattern_macros(raw.get("fix", raw.get("replace")), macros),
        dsl=raw.get("dsl"),
        severity=str(raw.get("severity", "warning")),
        languages=_str_tuple(raw.get("language")),
        files=_str_tuple(raw.get("files")),
        enabled=bool(raw.get("enabled", True)),
        options=freeze_options(option_values),
    )


def _unified_policy(
    name: str,
    raw: dict[str, Any],
    macros: dict[str, str],
    flow: CompiledFlowRule | None,
) -> CompiledPolicy | None:
    checks: list[CompiledPolicyCheck] = []
    prefix = f"rule:{name}"
    if "match" in raw or "find" in raw:
        structural = CompiledStructuralCheck(
            expand_macros(str(raw.get("match", raw.get("find", ""))), macros),
            expand_pattern_macros(yaml_key(raw, "within", "inside"), macros),
            expand_pattern_macros(yaml_key(raw, "not_within", "not_inside"), macros),
            raw.get("where"),
        )
        checks.append(CompiledPolicyCheck(
            f"{prefix}:structural", "structural", structural,
        ))
    if flow:
        checks.append(CompiledPolicyCheck(flow.rule_id, "flow", flow))
    deadcode = _compile_deadcode(raw.get("deadcode"), rule_name=name, rule_id=f"{prefix}:deadcode")
    if deadcode and deadcode.enabled:
        checks.append(CompiledPolicyCheck(deadcode.rule_id, "deadcode", deadcode))
    type_raw = raw.get("type-check", raw.get("type_check"))
    if isinstance(type_raw, dict):
        symbol = type_raw.get("selector") or yaml_key(type_raw, "symbol_pattern")
        expected = type_raw.get("expected") or yaml_key(type_raw, "expected_type")
        if symbol and expected:
            checks.append(CompiledPolicyCheck(
                f"{prefix}:type", "type",
                CompiledTypeCheck(str(symbol), str(expected), str(type_raw.get("kind", "has_type"))),
            ))
    if "datalog" in raw:
        datalog = raw["datalog"]
        query = datalog if isinstance(datalog, str) else (
            datalog.get("query") or datalog.get("cozoscript") if isinstance(datalog, dict) else None
        )
        if query:
            checks.append(CompiledPolicyCheck(
                f"{prefix}:datalog", "datalog", CompiledDatalogCheck(str(query)),
            ))
    if not checks:
        return None
    return CompiledPolicy(
        rule_id=prefix,
        name=name,
        description=str(raw.get("message") or raw.get("description") or name),
        severity=str(raw.get("severity", "warning")),
        checks=tuple(checks),
        enabled=bool(raw.get("enabled", True)),
        options=freeze_options(raw.get("options") if isinstance(raw.get("options"), dict) else {}),
    )


def _sequence_payload(raw: dict[str, Any]) -> CompiledSequenceCheck:
    if not raw.get("name"):
        raise ValueError("SequenceCheck requires 'name'")
    if len(raw.get("sequence", []) or []) < 2:
        raise ValueError("SequenceCheck requires at least 2 steps in 'sequence'")
    steps = tuple(CompiledSequenceStep(
        str(step.get("bind", "")), step.get("pattern"), step.get("effect"),
        yaml_key(step, "type_constraint"),
    ) for step in raw.get("sequence", []) or [] if isinstance(step, dict))
    paths: list[CompiledSequencePath] = []
    for key, value in (raw.get("path") or {}).items():
        parts = [part.strip() for part in key.split("->")]
        if len(parts) != 2:
            raise ValueError(f"Invalid path key {key!r}: expected 'step1 -> step2'")
        mapping = value if isinstance(value, dict) else {}
        paths.append(CompiledSequencePath(
            parts[0], parts[1],
            tuple(_pattern_text(item, {}) for item in as_list(yaml_key(mapping, "not_through")) if item),
            tuple(_pattern_text(item, {}) for item in as_list(yaml_key(mapping, "not_through_scope")) if item),
        ))
    return CompiledSequenceCheck(
        str(raw.get("name", "")), str(raw.get("message", "")), steps,
        tuple(paths), str(raw.get("severity", "error")),
    )


def _policy_check(
    raw: dict[str, Any], *, policy_name: str, index: int, severity: str,
) -> CompiledPolicyCheck:
    kind = str(raw.get("type", ""))
    rule_id = f"policy:{policy_name}:check:{index}:{kind}"
    if kind == "flow":
        payload = compile_flow_rule(
            raw, rule_id=rule_id, name=policy_name,
            message="", severity=severity, label=str(raw.get("label") or policy_name),
        )
        if payload is None:
            raise ValueError("FlowCheck requires 'flows_from' and 'flows_to'")
    elif kind == "deadcode":
        payload = _compile_deadcode(raw, rule_name=policy_name, rule_id=rule_id)
        if payload is None:
            raise ValueError("DeadCodeCheck requires a mapping")
    elif kind == "structural":
        pattern = raw.get("pattern")
        if not pattern:
            raise ValueError("StructuralCheck requires 'pattern'")
        payload = CompiledStructuralCheck(
            str(pattern), yaml_key(raw, "inside", "within"),
            yaml_key(raw, "not_inside", "not_within"), raw.get("where"),
        )
    elif kind == "type":
        symbol, expected = yaml_key(raw, "symbol_pattern"), yaml_key(raw, "expected_type")
        if not symbol or not expected:
            raise ValueError("TypeCheck requires 'symbol_pattern' and 'expected_type'")
        payload = CompiledTypeCheck(str(symbol), str(expected), str(raw.get("kind", "has_type")))
    elif kind == "custom":
        query = yaml_key(raw, "query_source")
        if not query:
            raise ValueError("CustomCheck requires 'query_source'")
        payload = CompiledCustomCheck(str(query))
    elif kind == "datalog":
        query = raw.get("cozoscript") or yaml_key(raw, "query")
        if not query:
            raise ValueError("DatalogCheck requires 'cozoscript' or 'query'")
        payload = CompiledDatalogCheck(str(query))
    elif kind == "sequence":
        payload = _sequence_payload(raw)
    else:
        raise ValueError(f"Unknown check type: {kind!r}")
    return CompiledPolicyCheck(rule_id, kind, payload)


def _compile_policies(raw: Any) -> tuple[CompiledPolicy, ...]:
    policies: list[CompiledPolicy] = []
    for policy_index, policy_raw in enumerate(raw or []):
        if not isinstance(policy_raw, dict):
            continue
        name = str(policy_raw.get("name", ""))
        severity = str(policy_raw.get("severity", "warning"))
        checks = tuple(
            _policy_check(check, policy_name=name, index=check_index, severity=severity)
            for check_index, check in enumerate(policy_raw.get("checks", []) or [])
            if isinstance(check, dict)
        )
        known = {"name", "description", "severity", "checks", "enabled", "options"}
        option_values = dict(policy_raw.get("options") or {}) if isinstance(policy_raw.get("options"), dict) else {}
        option_values.update(dict(_unknown_options(policy_raw, known)))
        policies.append(CompiledPolicy(
            rule_id=f"policy:{name}",
            name=name,
            description=str(policy_raw.get("description", "")),
            severity=severity,
            checks=checks,
            enabled=bool(policy_raw.get("enabled", True)),
            options=freeze_options(option_values),
        ))
    return tuple(policies)


def _trace_endpoint(raw: Any, role: str) -> CompiledTraceEndpoint | None:
    if not isinstance(raw, dict) or "label" not in raw:
        return None
    endpoint = _effect_endpoint(_endpoint(raw, {}))
    if role != "sink" and not endpoint.pattern:
        return None
    if role == "sink" and not (endpoint.pattern or endpoint.effect):
        # This was historically accepted, although it cannot match anything.
        endpoint = CompiledEndpoint()
    return CompiledTraceEndpoint(
        label=str(raw["label"]),
        endpoint=endpoint,
        message=str(raw.get("message", "Traced value reaches sink")) if role == "sink" else "",
        quantifier=str(raw.get("quantifier", "all_paths")),
    )


def _trace_endpoints(items: Any, role: str) -> tuple[CompiledTraceEndpoint, ...]:
    result: list[CompiledTraceEndpoint] = []
    for item in items or []:
        endpoint = _trace_endpoint(item, role)
        if endpoint is not None:
            result.append(endpoint)
    return tuple(result)


def _compile_trace(data: dict[str, Any]) -> CompiledTraceConfig:
    raw = data.get("trace", data.get("taint"))
    section = raw if isinstance(raw, dict) else {}
    sources = _trace_endpoints(section.get("sources"), "source")
    sinks = _trace_endpoints(section.get("sinks"), "sink")
    sanitizers = _trace_endpoints(section.get("sanitizers"), "sanitizer")
    scope_sanitizers = _trace_endpoints(section.get("scope_sanitizers"), "scope")
    excludes = tuple(dict.fromkeys(
        _str_tuple(section.get("exclude_paths")) + _str_tuple(yaml_key(data, "exclude_paths"))
    ))
    presets = tuple(dict.fromkeys(
        _str_tuple(data.get("presets")) + _str_tuple(section.get("presets"))
    ))
    languages = _str_tuple(section.get("language"))
    files = _str_tuple(section.get("files", section.get("file")))
    enabled = bool(section.get("enabled", True))
    flow_options = freeze_options(
        section.get("options") if isinstance(section.get("options"), dict) else {}
    )
    flows: list[CompiledFlowRule] = []
    for source_index, source in enumerate(sources):
        for sink_index, sink in enumerate(sinks):
            if source.label != sink.label:
                continue
            matching_sanitizers = tuple(item.endpoint for item in sanitizers if item.label == source.label)
            matching_scopes = tuple(item.endpoint for item in scope_sanitizers if item.label == source.label)
            quantifier = (
                "some_path" if any(
                    item.label == source.label and item.quantifier == "some_path"
                    for item in sanitizers
                ) else "all_paths"
            )
            flows.append(CompiledFlowRule(
                rule_id=f"trace:{source.label}:source:{source_index}:sink:{sink_index}",
                name=source.label,
                label=source.label,
                message=sink.message,
                sources=(source.endpoint,),
                sinks=(sink.endpoint,),
                sanitizers=matching_sanitizers,
                scope_sanitizers=matching_scopes,
                quantifier=quantifier,
                languages=languages,
                files=files,
                exclude_paths=excludes,
                enabled=enabled,
                options=flow_options,
            ))
    known = {
        "labels", "sources", "sinks", "sanitizers", "scope_sanitizers",
        "exclude_paths", "presets", "language", "file", "files", "enabled", "options",
    }
    option_values = dict(section.get("options") or {}) if isinstance(section.get("options"), dict) else {}
    option_values.update(dict(_unknown_options(section, known)))
    return CompiledTraceConfig(
        labels=_str_tuple(section.get("labels")),
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
        scope_sanitizers=scope_sanitizers,
        flow_rules=tuple(flows),
        exclude_paths=excludes,
        presets=presets,
        options=freeze_options(option_values),
    )


def compile_rules_document(
    data: dict[str, Any], *, source_path: str | Path | None = None,
) -> CompiledRuleDocument:
    """Compile a loaded YAML mapping once for all rule-engine consumers."""
    if not isinstance(data, dict):
        raise ValueError("Rules document must be a mapping")
    macros = _macro_map(data.get("macros"))
    raw_rules = data.get("rules") or {}
    if not isinstance(raw_rules, dict):
        raw_rules = {}
    top_excludes = _str_tuple(yaml_key(data, "exclude_paths"))
    patterns: list[CompiledPatternRule] = []
    flows: list[CompiledFlowRule] = []
    unified_policies: list[CompiledPolicy] = []
    embedded_deadcode: CompiledDeadCodeRule | None = None
    for name_value, raw in raw_rules.items():
        if not isinstance(raw, dict):
            continue
        name = str(name_value)
        if "deadcode" in raw:
            candidate = _compile_deadcode(
                raw.get("deadcode"), rule_name=name, rule_id=f"rule:{name}:deadcode",
            )
            if candidate and raw.get("message"):
                candidate = CompiledDeadCodeRule(
                    **{**candidate.__dict__, "message": str(raw["message"])}
                )
            embedded_deadcode = embedded_deadcode or candidate
        else:
            patterns.append(_compiled_pattern(name, raw, macros))
        flow = compile_flow_rule(
            raw, rule_id=f"rule:{name}:flow", name=name,
            macros=macros, exclude_paths=top_excludes,
        )
        if flow:
            flows.append(flow)
        if raw.get("enabled") is not False:
            policy = _unified_policy(name, raw, macros, flow)
            if policy:
                unified_policies.append(policy)

    deadcode = _compile_deadcode(data.get("deadcode")) or embedded_deadcode
    if (
        "deadcode" in data and deadcode and deadcode.enabled
        and all(policy.name != "deadcode" for policy in unified_policies)
    ):
        unified_policies.append(CompiledPolicy(
            "rule:deadcode", "deadcode", "Dead code check", "warning",
            (CompiledPolicyCheck(deadcode.rule_id, "deadcode", deadcode),),
        ))
    duplicate = _compile_duplicate(data.get("duplicate-code", data.get("duplicate")))
    policies = _compile_policies(data.get("policies")) if "policies" in data else ()
    known = {
        "macros", "rules", "deadcode", "duplicate_code", "duplicate",
        "policies", "trace", "taint", "exclude_paths", "presets", "options",
    }
    option_values = dict(data.get("options") or {}) if isinstance(data.get("options"), dict) else {}
    option_values.update(dict(_unknown_options(data, known)))
    return CompiledRuleDocument(
        source_path=Path(source_path) if source_path is not None else None,
        has_rules_section="rules" in data,
        has_policies_section="policies" in data,
        macros=freeze_options(macros),
        pattern_rules=tuple(patterns),
        flow_rules=tuple(flows),
        deadcode=deadcode,
        duplicate=duplicate,
        policies=policies,
        unified_policies=tuple(unified_policies),
        trace=_compile_trace(data),
        options=freeze_options(option_values),
    )


def load_compiled_rules(
    config_path: str | Path | None = None,
) -> CompiledRuleDocument:
    """Load and compile one canonical rules document for all consumers."""
    data, path = load_rules_document(config_path)
    return compile_rules_document(data, source_path=path)


def _glob_matches(candidate: str, pattern: str) -> bool:
    """Match slash-separated path components without letting ``*`` cross ``/``."""
    path_parts = tuple(part for part in candidate.split("/") if part)
    pattern_parts = tuple(part for part in pattern.split("/") if part)

    @lru_cache(maxsize=None)
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], part)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def path_matches_glob(
    file_path: str | Path,
    pattern: str | Path,
    *,
    project_root: str | Path | None = None,
) -> bool:
    """Match paths by components, with directory and ``**`` semantics.

    A bare directory pattern (``tests``) matches that directory and all of its
    descendants, but not similarly-prefixed files such as ``tests_helper.py``.
    Relative patterns are evaluated from *project_root* when an absolute file
    path is supplied.  ``**`` may match zero path components, so
    ``src/**/*.py`` includes ``src/main.py``.
    """
    candidate = str(file_path).replace("\\", "/")
    pat = str(pattern).replace("\\", "/")
    candidate_path = Path(candidate)
    pattern_path = Path(pat)

    if candidate_path.is_absolute() and not pattern_path.is_absolute():
        if project_root is not None:
            try:
                candidate = candidate_path.relative_to(Path(project_root).resolve()).as_posix()
            except ValueError:
                candidate = candidate_path.as_posix()
        else:
            candidate = candidate_path.as_posix()

    # Plain directory patterns are intentionally component-aware.  This also
    # handles absolute directory patterns without treating ``tests_helper.py``
    # as a descendant of ``tests``.
    if not any(char in pat for char in "*?["):
        clean_pat = pat.strip("/")
        clean_candidate = candidate.strip("/")
        return clean_candidate == clean_pat or clean_candidate.startswith(clean_pat + "/")

    # Unqualified globs (e.g. ``*.py``) match a basename at any depth.
    if "/" not in pat and not pattern_path.is_absolute():
        return any(fnmatchcase(part, pat) for part in candidate.split("/"))

    # Preserve the established ``*/src/*.py`` spelling, where the leading
    # component is intentionally an arbitrary absolute-path prefix.
    if pat.startswith("*/"):
        pat = "**/" + pat[2:]
    variants = [candidate.strip("/")]
    if not pattern_path.is_absolute():
        # Absolute inputs are common even when config patterns are project-
        # relative.  Try each path suffix so callers without an explicit root
        # still get project-relative matching.
        components = candidate.strip("/").split("/")
        variants.extend("/".join(components[index:]) for index in range(1, len(components)))
    return any(_glob_matches(variant, pat) for variant in variants)
