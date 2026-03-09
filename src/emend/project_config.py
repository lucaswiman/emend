"""Project-level configuration for emend.

Loads settings from (in priority order, highest wins):
1. ``.emend/config.toml`` in the project root
2. ``pyproject.toml`` under ``[tool.emend]``
3. Language-level defaults from ``languages/<lang>/config.toml``

Currently supports:
- ``environment_lookup.enabled`` (bool) — whether to search environment paths for symbols
- ``environment_lookup.paths`` (list[str]) — environment directory names to probe
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass
class EnvironmentLookupConfig:
    """Configuration for environment path symbol lookup.

    Supports looking up symbols in environment-specific package directories:
    - Python: .venv/venv site-packages
    - TypeScript/JavaScript: node_modules
    - Rust: target/debug/deps or target/release/deps
    """
    enabled: bool = True
    paths: list[str] = field(default_factory=lambda: [".venv", "venv"])


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file, returning {} on any error."""
    if not path.is_file():
        return {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return {}
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def _merge_environment_lookup(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge environment_lookup dicts; override wins for each key present."""
    merged = dict(base)
    for key in ("enabled", "paths"):
        if key in override:
            merged[key] = override[key]
    return merged


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*; override wins for scalars."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=16)
def load_project_config(project_root: str, language: str = "python") -> dict[str, Any]:
    """Load the merged project configuration.

    Merges (lowest → highest priority):
    1. Language defaults from ``languages/<lang>/config.toml``
    2. ``pyproject.toml`` under ``[tool.emend]``
    3. ``.emend/config.toml``

    All top-level sections are forwarded (``environment_lookup``, ``vim``,
    ``editor``, etc.) so downstream consumers can read project-level
    settings without knowing about every section upfront.
    """
    from emend.language_registry import load_config as load_lang_config

    lang_config = load_lang_config(language)
    root = Path(project_root)

    # Layer 1: language defaults
    result: dict[str, Any] = {}
    if "environment_lookup" in lang_config:
        result["environment_lookup"] = dict(lang_config["environment_lookup"])

    # Layer 2: pyproject.toml [tool.emend]
    pyproject_data = _load_toml(root / "pyproject.toml")
    tool_emend = pyproject_data.get("tool", {}).get("emend", {})
    if tool_emend:
        result = _deep_merge(result, tool_emend)

    # Layer 3: .emend/config.toml
    emend_config = _load_toml(root / ".emend" / "config.toml")
    if emend_config:
        result = _deep_merge(result, emend_config)

    return result


def get_environment_lookup_config(project_root: str, language: str = "python") -> EnvironmentLookupConfig:
    """Return the resolved EnvironmentLookupConfig for a project."""
    config = load_project_config(project_root, language)
    env_section = config.get("environment_lookup", {})
    return EnvironmentLookupConfig(
        enabled=env_section.get("enabled", True),
        paths=list(env_section.get("paths", [".venv", "venv"])),
    )


def resolve_environment_path(project_root: str, language: str = "python") -> Path | None:
    """Find the first existing environment path directory.

    For Python: Returns the ``site-packages`` path inside the first matching venv,
    or ``None`` if environment lookup is disabled or no venv is found.

    For other languages, returns the first matching environment directory.
    """
    cfg = get_environment_lookup_config(project_root, language)
    if not cfg.enabled:
        return None

    root = Path(project_root)
    for env_name in cfg.paths:
        env_dir = root / env_name
        if not env_dir.is_dir():
            continue

        if language == "python":
            # Find site-packages: lib/python*/site-packages
            lib_dir = env_dir / "lib"
            if lib_dir.is_dir():
                for child in lib_dir.iterdir():
                    sp = child / "site-packages"
                    if sp.is_dir():
                        return sp
            # Windows layout: Lib/site-packages
            lib_dir_win = env_dir / "Lib" / "site-packages"
            if lib_dir_win.is_dir():
                return lib_dir_win
        else:
            # For other languages, return the environment directory directly
            return env_dir

    return None


# Backward compatibility aliases
def get_venv_lookup_config(project_root: str, language: str = "python") -> EnvironmentLookupConfig:
    """Deprecated: use get_environment_lookup_config instead."""
    return get_environment_lookup_config(project_root, language)


def resolve_venv_site_packages(project_root: str, language: str = "python") -> Path | None:
    """Deprecated: use resolve_environment_path instead."""
    return resolve_environment_path(project_root, language)
