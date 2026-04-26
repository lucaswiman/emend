"""Framework-specific trace analysis presets.

Each preset is a YAML file shipped under :mod:`emend.presets`.  The schema
matches the ``trace:`` section of ``.emend/rules.yaml`` and is parsed by
:func:`emend.trace._trace_config_from_trace_section`.

Public API:

* :func:`list_presets` -- known preset names (plus ``"all"``).
* :func:`get_preset` -- load a single preset (or ``"all"``).
* :func:`merge_configs` -- combine multiple :class:`TraceConfig` instances,
  deduplicating labels.
"""

from __future__ import annotations

import importlib.resources

import yaml

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceScopeSanitizer,
    TraceSink,
    TraceSource,
    _trace_config_from_trace_section,
)

_PRESETS_PACKAGE = "emend.presets"

# Ordered list so ``"all"`` and :func:`list_presets` are deterministic and
# match the historical surface (Python first, then TS, then Rust).
_PRESET_NAMES: tuple[str, ...] = (
    "flask",
    "django",
    "sqlalchemy",
    "fastapi",
    "express",
    "react",
    "nextjs",
    "node-sql",
    "actix-web",
    "axum",
    "sqlx",
    "diesel",
)


def list_presets() -> list[str]:
    """Return available preset names, with ``"django"`` first then ``"flask"``.

    The historical ordering puts Django before Flask in the listed surface,
    even though Flask is loaded first in ``"all"``.
    """
    return [
        "django",
        "flask",
        "sqlalchemy",
        "fastapi",
        "express",
        "react",
        "nextjs",
        "node-sql",
        "actix-web",
        "axum",
        "sqlx",
        "diesel",
        "all",
    ]


def _load_preset_yaml(name: str) -> TraceConfig:
    """Read ``<name>.yaml`` from :mod:`emend.presets` and parse it."""
    resource = importlib.resources.files(_PRESETS_PACKAGE).joinpath(f"{name}.yaml")
    with resource.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Preset {name!r} must be a YAML mapping")
    return _trace_config_from_trace_section(raw)


def get_preset(name: str) -> TraceConfig:
    """Return a predefined TraceConfig for the named framework.

    Args:
        name: Preset name. Python: ``"django"``, ``"flask"``, ``"sqlalchemy"``,
              ``"fastapi"``.  TypeScript/Node.js: ``"express"``, ``"react"``,
              ``"nextjs"``, ``"node-sql"``.  Rust: ``"actix-web"``, ``"axum"``,
              ``"sqlx"``, ``"diesel"``.  Special: ``"all"`` merges every preset.

    Returns:
        A :class:`TraceConfig` with framework-specific rules.

    Raises:
        ValueError: If the preset name is unknown.
    """
    if name == "all":
        return merge_configs(*[_load_preset_yaml(n) for n in _PRESET_NAMES])
    if name not in _PRESET_NAMES:
        known = ", ".join(sorted(_PRESET_NAMES) + ["all"])
        raise ValueError(f"Unknown preset: {name!r}. Available presets: {known}")
    return _load_preset_yaml(name)


def merge_configs(*configs: TraceConfig) -> TraceConfig:
    """Merge multiple TraceConfigs, deduplicating labels.

    Sources, sinks, sanitizers, scope sanitizers, and exclude paths are
    concatenated; labels are deduplicated while preserving first-seen order.
    """
    seen_labels: set[str] = set()
    labels: list[str] = []
    sources: list[TraceSource] = []
    sinks: list[TraceSink] = []
    sanitizers: list[TraceSanitizer] = []
    scope_sanitizers: list[TraceScopeSanitizer] = []
    exclude_paths: list[str] = []

    for cfg in configs:
        for lbl in cfg.labels:
            if lbl not in seen_labels:
                seen_labels.add(lbl)
                labels.append(lbl)
        sources.extend(cfg.sources)
        sinks.extend(cfg.sinks)
        sanitizers.extend(cfg.sanitizers)
        scope_sanitizers.extend(cfg.scope_sanitizers)
        exclude_paths.extend(cfg.exclude_paths)

    return TraceConfig(
        labels=labels,
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
        scope_sanitizers=scope_sanitizers,
        exclude_paths=exclude_paths,
    )
