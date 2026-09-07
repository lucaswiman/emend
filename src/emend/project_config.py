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

import logging
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_jsonc(path: Path) -> dict[str, Any]:
    """Load a JSON-with-comments configuration file."""
    import json

    raw = path.read_text()
    cleaned: list[str] = []
    index = 0
    quoted = escaped = False
    while index < len(raw):
        char = raw[index]
        if quoted:
            cleaned.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
        elif char == '"':
            quoted = True
            cleaned.append(char)
            index += 1
        elif raw.startswith("//", index):
            index = raw.find("\n", index)
            if index < 0:
                break
        elif raw.startswith("/*", index):
            end = raw.find("*/", index + 2)
            if end < 0:
                raise ValueError(f"unterminated JSONC comment in {path}")
            cleaned.extend("\n" for char in raw[index:end + 2] if char == "\n")
            index = end + 2
        else:
            cleaned.append(char)
            index += 1

    raw = "".join(cleaned)
    quoted = escaped = False
    commas: set[int] = set()
    for index, char in enumerate(raw):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == ",":
            commas.add(index)
    trailing: set[int] = set()
    next_significant = ""
    for index in range(len(raw) - 1, -1, -1):
        char = raw[index]
        if index in commas and next_significant and next_significant in "}]":
            trailing.add(index)
        elif not char.isspace():
            next_significant = char
    return json.loads("".join(
        char for index, char in enumerate(raw) if index not in trailing
    ))


def load_typescript_config(
    project_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Path], tuple[Path, ...]]:
    """Load an inherited tsconfig and retain each option's defining directory."""
    root = Path(project_root).resolve()
    seen: set[Path] = set()

    def resolve(specifier: str, current: Path) -> Path | None:
        base = current.parent / specifier
        candidates = [base, base.with_suffix(base.suffix + ".json")]
        if not specifier.startswith(".") and not Path(specifier).is_absolute():
            candidates.extend(
                candidate / "node_modules" / specifier
                for candidate in (current.parent, *current.parents)
            )
        for candidate in candidates:
            if candidate.is_dir():
                candidate = candidate / "tsconfig.json"
            if candidate.is_file():
                return candidate.resolve()
            json_candidate = candidate.with_suffix(candidate.suffix + ".json")
            if json_candidate.is_file():
                return json_candidate.resolve()
        return None

    def load(path: Path) -> tuple[dict[str, Any], dict[str, Path], list[Path]]:
        path = path.resolve()
        if path in seen or not path.is_file():
            return {}, {}, []
        seen.add(path)
        data = load_jsonc(path)
        options: dict[str, Any] = {}
        origins: dict[str, Path] = {}
        sources: list[Path] = []
        parents = data.get("extends", [])
        if isinstance(parents, str):
            parents = [parents]
        for parent in parents if isinstance(parents, list) else []:
            parent_path = resolve(str(parent), path)
            if parent_path is not None:
                inherited, inherited_origins, inherited_sources = load(parent_path)
                options.update(inherited)
                origins.update(inherited_origins)
                sources.extend(inherited_sources)
        current = data.get("compilerOptions", {})
        if isinstance(current, dict):
            options.update(current)
            origins.update({key: path.parent for key in current})
        return options, origins, [*sources, path]

    options, origins, sources = load(root / "tsconfig.json")
    return options, origins, tuple(dict.fromkeys(sources))


def find_project_root(start: str | Path = ".") -> Path:
    """Return the nearest configured project boundary."""
    path = Path(start).resolve()
    if path.is_file():
        path = path.parent
    markers = (
        ".emend/config.toml", ".emend/rules.yaml", ".emend/mappings.yaml",
        "pyproject.toml", "setup.py", "setup.cfg", "package.json",
        "tsconfig.json", "Cargo.toml",
    )
    for candidate in (path, *path.parents):
        git_marker = candidate / ".git"
        if git_marker.is_file() or (git_marker / "HEAD").is_file():
            return candidate
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    return path


@lru_cache(maxsize=64)
def find_source_root(project_root: str, language: str = "python") -> Path:
    """Return the configured or conventional source root for *language*."""
    root = Path(project_root).resolve()
    if language == "python":
        data = _load_toml(root / "pyproject.toml")
        candidates = [
            data.get("tool", {}).get("maturin", {}).get("python-source"),
        ]
        where = (
            data.get("tool", {}).get("setuptools", {}).get("packages", {})
            .get("find", {}).get("where")
        )
        if isinstance(where, list) and where:
            candidates.append(where[0])
        hatch_source = (
            data.get("tool", {}).get("hatch", {}).get("build", {})
            .get("sources", {}).get("src")
        )
        if isinstance(hatch_source, str):
            candidates.append(hatch_source)
        for relative in candidates:
            if relative and (root / relative).is_dir():
                return (root / relative).resolve()

        setup_cfg = root / "setup.cfg"
        if setup_cfg.is_file():
            import configparser

            try:
                config = configparser.ConfigParser()
                config.read(setup_cfg)
                package_dir = config.get(
                    "options", "package_dir", fallback=""
                )
                for part in package_dir.splitlines():
                    if part.strip().startswith("="):
                        candidate = root / part.split("=", 1)[1].strip()
                        if candidate.is_dir():
                            return candidate.resolve()
            except (OSError, UnicodeDecodeError, configparser.Error):
                logger.debug("setup.cfg source-root detection failed", exc_info=True)

        src = root / "src"
        if src.is_dir() and any(
            child.is_dir() and (child / "__init__.py").is_file()
            for child in src.iterdir()
        ):
            return src.resolve()
    elif language == "rust":
        lib_path = _load_toml(root / "Cargo.toml").get("lib", {}).get("path")
        if lib_path and (root / lib_path).parent.is_dir():
            return (root / lib_path).parent.resolve()
        if (root / "src").is_dir():
            return (root / "src").resolve()
    elif language == "typescript":
        tsconfig = root / "tsconfig.json"
        if tsconfig.is_file():
            try:
                compiler, origins, _sources = load_typescript_config(root)
                root_dir = compiler.get("rootDir")
                root_base = origins.get("rootDir", root)
                if root_dir and (root_base / root_dir).is_dir():
                    return (root_base / root_dir).resolve()
                base_url = compiler.get("baseUrl")
                base_base = origins.get("baseUrl", root)
                if base_url and base_url != "." and (base_base / base_url).is_dir():
                    return (base_base / base_url).resolve()
            except (OSError, ValueError, TypeError, AttributeError):
                logger.debug("tsconfig source-root detection failed", exc_info=True)
        if (root / "src").is_dir():
            return (root / "src").resolve()
    elif (root / "src").is_dir():
        return (root / "src").resolve()
    return root


def module_name_for_file(
    file_path: str | Path,
    project_root: str | Path | None = None,
    *,
    language: str | None = None,
    module_separator: str | None = None,
) -> str:
    """Return the canonical import/module identity for one source file."""
    from emend.language_registry import detect_language, get_module_separator

    path = Path(file_path).resolve()
    root = Path(project_root).resolve() if project_root else find_project_root(path)
    language = language or detect_language(path) or "python"
    source_root = find_source_root(str(root), language)
    try:
        relative = path.relative_to(source_root)
    except ValueError:
        relative = path.relative_to(root)
    stem = relative.stem
    directories = list(relative.parts[:-1])
    module_parts = (
        directories
        if directories and (stem == "__init__" or language == "rust" and stem == "mod")
        else [*directories, stem]
    )
    separator = module_separator or get_module_separator(language)
    return separator.join(module_parts) if module_parts else stem


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
    except (OSError, ValueError):
        # TOMLDecodeError subclasses ValueError in both tomllib and tomli.
        logger.debug("Could not parse %s", path, exc_info=True)
        return {}


def _merge_environment_lookup(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge environment_lookup dicts; override wins for each key present."""
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
    if "environment_lookup" in lang_config:
        result["environment_lookup"] = dict(lang_config["environment_lookup"])

    # Layer 2: pyproject.toml [tool.emend]
    pyproject_data = _load_toml(root / "pyproject.toml")
    tool_emend = pyproject_data.get("tool", {}).get("emend", {})
    if "environment_lookup" in tool_emend:
        base = result.get("environment_lookup", {})
        result["environment_lookup"] = _merge_environment_lookup(base, tool_emend["environment_lookup"])

    # Layer 3: .emend/config.toml
    emend_config = _load_toml(root / ".emend" / "config.toml")
    if "environment_lookup" in emend_config:
        base = result.get("environment_lookup", {})
        result["environment_lookup"] = _merge_environment_lookup(base, emend_config["environment_lookup"])

    return result


def get_environment_lookup_config(project_root: str, language: str = "python") -> EnvironmentLookupConfig:
    """Return the resolved EnvironmentLookupConfig for a project."""
    config = load_project_config(project_root, language)
    env_section = config.get("environment_lookup", {})
    paths = env_section.get("paths", [".venv", "venv"])
    if isinstance(paths, str):
        paths = [paths]
    return EnvironmentLookupConfig(
        enabled=env_section.get("enabled", True),
        paths=list(paths),
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
