"""Shared helpers for loading emend rule/policy configs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml


DEFAULT_RULES_PATH = Path(".emend/rules.yaml")
LEGACY_PATTERNS_PATH = Path(".emend/patterns.yaml")
LEGACY_POLICIES_PATH = Path(".emend/policies.yaml")


def load_rules_document(
    config_path: str | Path | None = None,
    *,
    fallbacks: Iterable[str | Path] = (),
) -> tuple[dict[str, Any], Path]:
    """Load a rules document with canonical + legacy fallback behavior."""
    if config_path is None:
        candidates = [DEFAULT_RULES_PATH, *[Path(p) for p in fallbacks]]
    else:
        requested = Path(config_path)
        candidates = [requested]
        fallback_names = {Path(p).name for p in fallbacks}

        # For legacy filenames, prefer a sibling rules.yaml.
        if requested.name in fallback_names:
            sibling_rules = requested.with_name(DEFAULT_RULES_PATH.name)
            if sibling_rules not in candidates:
                candidates.append(sibling_rules)

        # If caller passed the canonical filename explicitly, allow legacy
        # fallbacks in the same directory.
        if requested.name == DEFAULT_RULES_PATH.name:
            for fallback in fallbacks:
                sibling_legacy = requested.with_name(Path(fallback).name)
                if sibling_legacy not in candidates:
                    candidates.append(sibling_legacy)

    resolved: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            resolved = candidate
            break
    if resolved is None:
        missing = Path(config_path) if config_path is not None else DEFAULT_RULES_PATH
        raise FileNotFoundError(f"Config file not found: {missing}")

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
    *,
    fallbacks: Iterable[str | Path] = (),
) -> Path:
    """Resolve the active rules/policy config path with canonical fallback."""
    if config_path is None:
        candidates = [DEFAULT_RULES_PATH, *[Path(p) for p in fallbacks]]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return DEFAULT_RULES_PATH

    requested = Path(config_path)
    resolved = resolve_config_path_with_fallback(
        requested,
        legacy_names=[Path(p).name for p in fallbacks],
    )
    return resolved or requested


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
