"""Shared helpers for loading emend rule/policy configs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


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
    if raw is None:
        return None
    if isinstance(raw, bool):
        return DeadCodeConfig(enabled=raw, rule_name=rule_name)
    if not isinstance(raw, dict):
        return None

    entry_points = raw.get("entry-points")
    decorators = yaml_key(raw, "entry_point_decorators")
    names = yaml_key(raw, "entry_point_names")
    if isinstance(entry_points, dict):
        decorators = decorators or entry_points.get("decorators")
        names = names or entry_points.get("names")

    return DeadCodeConfig(
        enabled=raw.get("enabled", True),
        rule_name=rule_name,
        kind=raw.get("kind"),
        include_private=raw.get("include-private", True),
        exclude_references_from=coerce_optional_str_list(
            yaml_key(raw, "exclude_references_from")
        ),
        exclude_test_references=raw.get(
            "exclude-test-references",
            not raw.get("include-test-references", False),
        ),
        strings_count_as_references=raw.get("strings-count-as-references", True),
        unused_modules=raw.get("unused-modules", True),
        message=raw.get("message", "Symbol appears to be unused"),
        entry_point_decorators=coerce_optional_str_list(decorators),
        entry_point_names=coerce_optional_str_list(names),
        exclude_paths=coerce_optional_str_list(yaml_key(raw, "exclude_paths")),
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


def expand_not_through(not_through: Any, macros: dict[str, str]) -> str | None:
    """Expand and join a ``not_through`` value (string or list) with macros.

    Returns a single pipe-joined pattern string, or ``None`` if *not_through*
    is falsy.
    """
    if not not_through:
        return None
    if isinstance(not_through, list):
        expanded = [expand_macros(str(item), macros) for item in not_through]
        return " | ".join(item for item in expanded if item) or None
    return expand_macros(str(not_through), macros) or None
