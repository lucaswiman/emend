"""Project-level configuration for emend.

Loads settings from (in priority order, highest wins):
1. ``.emend/config.toml`` in the project root
2. ``pyproject.toml`` under ``[tool.emend]``
3. Language-level defaults from ``languages/<lang>/config.toml``

Currently supports:
- ``venv_lookup.enabled`` (bool) — whether to search venv for symbols
- ``venv_lookup.paths`` (list[str]) — venv directory names to probe
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass
class VenvLookupConfig:
    """Configuration for virtual-environment symbol lookup."""
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


def _merge_venv_lookup(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge venv_lookup dicts; override wins for each key present."""
    merged = dict(base)
    for key in ("enabled", "paths"):
        if key in override:
            merged[key] = override[key]
    return merged


@lru_cache(maxsize=16)
def load_project_config(project_root: str, language: str = "python") -> dict[str, Any]:
    """Load the merged project configuration.

    Merges (lowest → highest priority):
    1. Language defaults from ``languages/<lang>/config.toml``
    2. ``pyproject.toml`` ``[tool.emend]``
    3. ``.emend/config.toml``
    """
    from emend.language_registry import load_config as load_lang_config

    lang_config = load_lang_config(language)
    root = Path(project_root)

    # Layer 1: language defaults
    result: dict[str, Any] = {}
    if "venv_lookup" in lang_config:
        result["venv_lookup"] = dict(lang_config["venv_lookup"])

    # Layer 2: pyproject.toml [tool.emend]
    pyproject_data = _load_toml(root / "pyproject.toml")
    tool_emend = pyproject_data.get("tool", {}).get("emend", {})
    if "venv_lookup" in tool_emend:
        base = result.get("venv_lookup", {})
        result["venv_lookup"] = _merge_venv_lookup(base, tool_emend["venv_lookup"])

    # Layer 3: .emend/config.toml
    emend_config = _load_toml(root / ".emend" / "config.toml")
    if "venv_lookup" in emend_config:
        base = result.get("venv_lookup", {})
        result["venv_lookup"] = _merge_venv_lookup(base, emend_config["venv_lookup"])

    return result


def get_venv_lookup_config(project_root: str, language: str = "python") -> VenvLookupConfig:
    """Return the resolved VenvLookupConfig for a project."""
    config = load_project_config(project_root, language)
    venv_section = config.get("venv_lookup", {})
    return VenvLookupConfig(
        enabled=venv_section.get("enabled", True),
        paths=list(venv_section.get("paths", [".venv", "venv"])),
    )


def resolve_venv_site_packages(project_root: str, language: str = "python") -> Path | None:
    """Find the first existing venv site-packages directory.

    Returns the ``site-packages`` path inside the first matching venv,
    or ``None`` if venv lookup is disabled or no venv is found.
    """
    cfg = get_venv_lookup_config(project_root, language)
    if not cfg.enabled:
        return None

    root = Path(project_root)
    for venv_name in cfg.paths:
        venv_dir = root / venv_name
        if not venv_dir.is_dir():
            continue
        # Find site-packages: lib/python*/site-packages
        lib_dir = venv_dir / "lib"
        if lib_dir.is_dir():
            for child in lib_dir.iterdir():
                sp = child / "site-packages"
                if sp.is_dir():
                    return sp
        # Windows layout: Lib/site-packages
        lib_dir_win = venv_dir / "Lib" / "site-packages"
        if lib_dir_win.is_dir():
            return lib_dir_win

    return None
