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
import stat as stat_module
import sys
from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_jsonc(path: Path) -> dict[str, Any]:
    """Load a JSON-with-comments configuration file."""
    import json

    raw = _read_config_bytes(path).decode("utf-8")
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
        if specifier.startswith(".") or Path(specifier).is_absolute():
            return base if base.suffix else base.with_suffix(".json")
        return None

    def load(path: Path) -> tuple[dict[str, Any], dict[str, Path], list[Path]]:
        path = path.resolve()
        if path in seen:
            return {}, {}, []
        seen.add(path)
        if not path.is_file():
            return {}, {}, [path]
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


@dataclass(frozen=True)
class _ModuleContextEntry:
    module_root: Path
    crate_roots: tuple[Path, ...]
    watched: tuple[Path, ...]
    config_files: frozenset[Path]
    signatures: tuple[object, ...]


_MODULE_CONTEXT_CACHE: dict[tuple[Path, str], _ModuleContextEntry] = {}


@dataclass
class _ContextSnapshot:
    contents: dict[Path, bytes | None] = field(default_factory=dict)
    entries: dict[tuple[Path, str], _ModuleContextEntry] = field(default_factory=dict)


_CONTEXT_SNAPSHOT: ContextVar[_ContextSnapshot | None] = ContextVar("module_context", default=None)


@contextmanager
def module_context_snapshot():
    """Share exact configuration bytes and module roots within one operation."""
    current = _CONTEXT_SNAPSHOT.get()
    if current is not None:
        yield current
        return
    token = _CONTEXT_SNAPSHOT.set(_ContextSnapshot())
    try:
        yield _CONTEXT_SNAPSHOT.get()
    finally:
        _CONTEXT_SNAPSHOT.reset(token)


def _read_config_bytes(path: Path) -> bytes:
    snapshot = _CONTEXT_SNAPSHOT.get()
    if snapshot is None:
        return path.read_bytes()
    if path not in snapshot.contents:
        try:
            snapshot.contents[path] = path.read_bytes()
        except OSError:
            snapshot.contents[path] = None
    content = snapshot.contents[path]
    if content is None:
        raise FileNotFoundError(path)
    return content


def _context_signatures(watched, config_files):
    signatures = []
    for path in watched:
        if path in config_files:
            try:
                signatures.append(_read_config_bytes(path))
            except OSError:
                signatures.append(None)
        else:
            signatures.append(_path_signature(path))
    return tuple(signatures)


def _path_signature(path: Path) -> tuple[bool, int, int, int, int, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return False, 0, 0, 0, 0, 0, 0
    return (
        True, stat_module.S_IFMT(stat.st_mode), stat.st_dev, stat.st_ino, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns,
    )


def _source_root_and_inputs(
    root: Path, language: str,
) -> tuple[Path, tuple[Path, ...], dict[str, Any]]:
    """Compute one source root and return the configuration inputs consumed."""
    watched = [root / "src"]
    parsed: dict[str, Any] = {}
    if language == "python":
        pyproject = root / "pyproject.toml"
        setup_cfg = root / "setup.cfg"
        parsed["config_files"] = {pyproject, setup_cfg}
        watched.extend((pyproject, setup_cfg))
        data = _load_toml(pyproject)
        tool = data.get("tool", {})
        candidates = [tool.get("maturin", {}).get("python-source")]

        setuptools = tool.get("setuptools", {})
        packages = setuptools.get("packages", {})
        if isinstance(packages, dict):
            find = packages.get("find", {})
            where = find.get("where") if isinstance(find, dict) else None
            if isinstance(where, str):
                candidates.append(where)
            elif isinstance(where, list):
                candidates.extend(where)
        package_dir = setuptools.get("package-dir", {})
        if isinstance(package_dir, dict):
            candidates.append(package_dir.get(""))

        sources = tool.get("hatch", {}).get("build", {}).get("sources")
        if isinstance(sources, (list, dict)):
            candidates.extend(sources)
        for relative in candidates:
            if isinstance(relative, str) and relative:
                candidate = root / relative
                watched.append(candidate)
                if candidate.is_dir():
                    return candidate.resolve(), tuple(watched), parsed

        if setup_cfg.is_file():
            import configparser

            try:
                config = configparser.ConfigParser()
                config.read_string(_read_config_bytes(setup_cfg).decode("utf-8"))
                package_dir = config.get(
                    "options", "package_dir", fallback=""
                )
                for part in package_dir.splitlines():
                    if part.strip().startswith("="):
                        candidate = root / part.split("=", 1)[1].strip()
                        watched.append(candidate)
                        if candidate.is_dir():
                            return candidate.resolve(), tuple(watched), parsed
            except (OSError, UnicodeDecodeError, configparser.Error):
                logger.debug("setup.cfg source-root detection failed", exc_info=True)

        src = root / "src"
        children = list(src.iterdir()) if src.is_dir() else []
        for child in children:
            watched.extend((child, child / "__init__.py"))
        if any(
            child.is_dir() and (child / "__init__.py").is_file()
            for child in children
        ):
            return src.resolve(), tuple(watched), parsed
    elif language == "rust":
        cargo_path = root / "Cargo.toml"
        parsed["config_files"] = {cargo_path}
        watched.extend((cargo_path, root / "src" / "lib.rs", root / "src" / "main.rs"))
        cargo = _load_toml(cargo_path)
        parsed["cargo"] = cargo
        lib_path = cargo.get("lib", {}).get("path")
        if lib_path:
            watched.extend((root / lib_path, (root / lib_path).parent))
            if (root / lib_path).parent.is_dir():
                return (root / lib_path).parent.resolve(), tuple(watched), parsed
        if (root / "src").is_dir():
            return (root / "src").resolve(), tuple(watched), parsed
    elif language == "typescript":
        tsconfig = root / "tsconfig.json"
        parsed["config_files"] = {tsconfig}
        watched.append(tsconfig)
        if tsconfig.is_file():
            try:
                compiler, origins, sources = load_typescript_config(root)
                parsed["config_files"].update(sources)
                watched.extend(sources)
                root_dir = compiler.get("rootDir")
                root_base = origins.get("rootDir", root)
                if root_dir:
                    root_dir_path = root_base / root_dir
                    watched.append(root_dir_path)
                    if root_dir_path.is_dir():
                        return root_dir_path.resolve(), tuple(watched), parsed
                base_url = compiler.get("baseUrl")
                base_base = origins.get("baseUrl", root)
                if base_url and base_url != ".":
                    base_url_path = base_base / base_url
                    watched.append(base_url_path)
                    if base_url_path.is_dir():
                        return base_url_path.resolve(), tuple(watched), parsed
            except (OSError, ValueError, TypeError, AttributeError):
                logger.debug("tsconfig source-root detection failed", exc_info=True)
        if (root / "src").is_dir():
            return (root / "src").resolve(), tuple(watched), parsed
    elif (root / "src").is_dir():
        return (root / "src").resolve(), tuple(watched), parsed
    return root, tuple(watched), parsed


def _build_module_context(root: Path, language: str) -> _ModuleContextEntry:
    module_root, watched, parsed = _source_root_and_inputs(root, language)
    roots: list[Path] = []
    if language == "rust":
        cargo = parsed.get("cargo", {})
        lib_path = cargo.get("lib", {}).get("path")
        if lib_path:
            roots.append((root / lib_path).resolve())
        elif (module_root / "lib.rs").is_file():
            roots.append(module_root / "lib.rs")
        main = (root / "src" if (root / "src").is_dir() else root) / "main.rs"
        watched = (*watched, main)
        if main.is_file():
            roots.append(main)
        binaries = cargo.get("bin", [])
        for binary in binaries if isinstance(binaries, list) else []:
            if isinstance(binary, dict) and binary.get("path"):
                binary_path = (root / binary["path"]).resolve()
                roots.append(binary_path)
                watched = (*watched, binary_path)
    watched = tuple(dict.fromkeys(watched))
    config_files = frozenset(parsed.get("config_files", ()))
    return _ModuleContextEntry(
        module_root, tuple(dict.fromkeys(roots)), watched, config_files,
        _context_signatures(watched, config_files),
    )


def _module_context_entry(root: Path, language: str) -> _ModuleContextEntry:
    key = (root, language)
    with module_context_snapshot() as snapshot:
        if key not in snapshot.entries:
            entry = _MODULE_CONTEXT_CACHE.get(key)
            if entry is None or entry.signatures != _context_signatures(entry.watched, entry.config_files):
                entry = _build_module_context(root, language)
                _MODULE_CONTEXT_CACHE[key] = entry
            snapshot.entries[key] = entry
        return snapshot.entries[key]


def _clear_module_context_cache() -> None:
    _MODULE_CONTEXT_CACHE.clear()


def find_source_root(project_root: str, language: str = "python") -> Path:
    """Return the cached configured or conventional source root."""
    return _module_context_entry(Path(project_root).resolve(), language).module_root


find_source_root.cache_clear = _clear_module_context_cache  # type: ignore[attr-defined]


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
    source_root, _root_module = module_resolution_context(root, language, file_path=path)
    try:
        relative = path.relative_to(source_root)
    except ValueError:
        relative = path.relative_to(root)
    stem = relative.stem
    directories = list(relative.parts[:-1])
    module_parts = (
        directories
        if directories and (
            stem == "__init__"
            or language == "rust" and stem == "mod"
        )
        else [*directories, stem]
    )
    separator = module_separator or get_module_separator(language)
    return separator.join(module_parts) if module_parts else stem


def module_resolution_context(
    project_root: str | Path, language: str, *, file_path: str | Path | None = None,
) -> tuple[Path, Path | None]:
    """Return the configured module root and optional crate-root file."""
    root = Path(project_root).resolve()
    entry = _module_context_entry(root, language)
    module_root, roots = entry.module_root, entry.crate_roots
    if language != "rust":
        return module_root, None
    selected = Path(file_path).resolve() if file_path is not None else None
    if selected in roots:
        return module_root, selected
    if len(roots) == 1:
        return module_root, roots[0]
    # With both library and binary crate roots, assigning one project-wide
    # root would conflate their root symbols.  Keep explicit file identities;
    # shared physical submodules remain addressable without guessing ownership.
    return module_root, None


def resolver_module_name_for_file(
    file_path: str | Path, project_root: str | Path | None = None,
) -> str:
    """Return the module identity emitted by the configured scope resolver."""
    from emend import emend_core
    from emend.language_registry import detect_language

    path = Path(file_path).resolve()
    root = Path(project_root).resolve() if project_root else find_project_root(path)
    module_root, crate_root = module_resolution_context(
        root, detect_language(path) or "python", file_path=path,
    )
    resolver = emend_core.PyScopeResolver(
        str(root), path.suffix.lstrip("."), str(module_root),
        str(crate_root) if crate_root else None,
    )
    return resolver.module_name_for_file(str(path))


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
    try:
        return _load_toml_payload(_read_config_bytes(path), str(path))
    except OSError:
        logger.debug("Could not parse %s", path, exc_info=True)
        return {}


def _merge_environment_lookup(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge environment_lookup dicts; override wins for each key present."""
    merged = dict(base)
    merged.update({key: override[key] for key in ("enabled", "paths") if key in override})
    return merged


def _project_config_inputs(root: Path) -> tuple[bytes, bytes]:
    """Capture the exact mutable project configuration consumed below."""
    payloads = []
    for path in (root / "pyproject.toml", root / ".emend" / "config.toml"):
        try:
            payloads.append(path.read_bytes())
        except OSError:
            payloads.append(b"")
    return payloads[0], payloads[1]


def _load_toml_payload(payload: bytes, label: str) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                return {}
        return tomllib.loads(payload.decode())
    except (ImportError, UnicodeError, ValueError):
        logger.debug("Could not parse %s", label, exc_info=True)
        return {}


@lru_cache(maxsize=16)
def _load_project_config(
    project_root: str, language: str, inputs: tuple[bytes, bytes], language_identity: str,
) -> dict[str, Any]:
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
    pyproject_data = _load_toml_payload(inputs[0], str(root / "pyproject.toml"))
    tool_emend = pyproject_data.get("tool", {}).get("emend", {})
    # Layer 3: .emend/config.toml
    emend_config = _load_toml_payload(
        inputs[1], str(root / ".emend" / "config.toml")
    )
    for config in (tool_emend, emend_config):
        if "environment_lookup" in config:
            result["environment_lookup"] = _merge_environment_lookup(
                result.get("environment_lookup", {}), config["environment_lookup"]
            )

    return result


def load_project_config(project_root: str, language: str = "python") -> dict[str, Any]:
    """Load content-addressed configuration that notices live file changes."""
    from emend.language_registry import config_identity
    root = Path(project_root)
    return _load_project_config(
        str(root), language, _project_config_inputs(root), config_identity(language),
    )


load_project_config.cache_clear = _load_project_config.cache_clear  # type: ignore[attr-defined]


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
