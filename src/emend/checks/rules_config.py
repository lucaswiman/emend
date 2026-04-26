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
    include_private: bool = False
    exclude_references_from: list[str] | None = None
    strings_count_as_references: bool = True
    message: str = "Symbol appears to be unused"
    entry_point_decorators: list[str] | None = None
    entry_point_names: list[str] | None = None
    exclude_paths: list[str] | None = None


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
