"""Shared helpers for loading emend rule/policy configs."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
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
