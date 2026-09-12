"""Language detection registry built from ``languages/*/config.toml`` files.

Provides a single source of truth for which file extensions belong to which
language.  The registry is populated at import time by discovering all
``languages/*/config.toml`` files shipped with emend (and optionally any found
in the current working directory for user-defined languages).

Hardcoded fallbacks ensure the module works even if TOML files are missing
(e.g. in test environments that don't have the full package installed).

Usage::

    from emend.language_registry import detect_language, get_extensions

    detect_language("foo.py")      # "python"
    detect_language("bar.ts")      # "typescript"
    detect_language("baz.txt")     # None
    get_extensions("python")       # ["py", "pyi"]
    matches_language("a.ts", "typescript")  # True
"""
from __future__ import annotations

import hashlib
import logging
import sys
from functools import lru_cache
from pathlib import Path

from emend.analysis_snapshot import LanguageConfigRevision

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded fallbacks
# Maps language name → list of file extensions (without leading dot).
# These are used when no TOML files are found / parseable.
# ---------------------------------------------------------------------------
_BUILTIN: dict[str, list[str]] = {
    "python": ["py", "pyi"],
    "typescript": ["ts", "tsx", "js", "jsx"],
    "rust": ["rs"],
    "html": ["html", "htm"],
    "css": ["css"],
    "sql": ["sql"],
    "jinja2": ["jinja", "jinja2", "j2"],
    "datalog": ["dl", "datalog"],
}


def _find_languages_dir() -> Path | None:
    """Return the ``languages/`` config directory shipped with emend, or None."""
    candidate = Path(__file__).parent / "languages"
    if candidate.is_dir():
        return candidate
    return None


def _discover_entry_point_languages() -> dict[str, Path]:
    """Discover language configs from installed entry-point plugins.

    Third-party packages register via::

        [project.entry-points."emend.languages"]
        go = "emend_golang"

    The entry point value is a module whose directory must contain a
    ``config.toml`` file.  Returns ``{language_name: config_dir_path}``.
    """
    import importlib.metadata

    result: dict[str, Path] = {}
    try:
        eps = importlib.metadata.entry_points()
        # Python 3.12+ returns a SelectableGroups; 3.10-3.11 may return a dict
        if hasattr(eps, "select"):
            lang_eps = eps.select(group="emend.languages")
        else:
            lang_eps = eps.get("emend.languages", [])  # type: ignore[union-attr]

        for ep in lang_eps:
            try:
                mod = ep.load()
                mod_dir = (
                    Path(mod.__file__).parent
                    if hasattr(mod, "__file__") and mod.__file__
                    else None
                )
                if mod_dir and (mod_dir / "config.toml").is_file():
                    result[ep.name] = mod_dir
            except Exception:
                logger.debug("Skipping broken language plugin %s", ep.name, exc_info=True)
                continue
    except Exception:
        logger.debug("Entry-point discovery for emend.languages failed", exc_info=True)
    return result


def _config_path(language: str, project_root: str | Path | None = None) -> Path | None:
    """Return the effective configuration path for one language."""
    if project_root is not None:
        candidate = Path(project_root) / "languages" / language / "config.toml"
        if candidate.is_file():
            return candidate
    lang_dir = _find_languages_dir()
    if lang_dir is not None:
        candidate = lang_dir / language / "config.toml"
        if candidate.is_file():
            return candidate
    plugin_dir = _discover_entry_point_languages().get(language)
    if plugin_dir is not None:
        candidate = plugin_dir / "config.toml"
        if candidate.is_file():
            return candidate
    return None


def config_identity(language: str, project_root: str | Path | None = None) -> str:
    """Hash the exact language configuration consumed by analysis."""
    path = _config_path(language, project_root)
    try:
        payload = path.read_bytes() if path is not None else repr(
            _BUILTIN.get(language, ())
        ).encode()
    except OSError:
        payload = repr(_BUILTIN.get(language, ())).encode()
    return hashlib.sha256(payload).hexdigest()


def _parse_toml_extensions(
    payload: bytes, label: str
) -> tuple[str, list[str]] | None:
    """Return (language_name, [extensions]) from a config.toml, or None on error."""
    data = _parse_config_payload(payload, label)
    lang = data.get("language", {})
    name = lang.get("name")
    exts = lang.get("file_extensions", [])
    if name and exts:
        return name, list(exts)
    return None


def _registry_inputs(
    project_root: str | Path | None = None,
) -> tuple[tuple[str, str, bytes], ...]:
    """Capture the exact language configurations used by one registry view."""
    inputs: list[tuple[str, str, bytes]] = []
    if project_root is not None:
        for path in sorted((Path(project_root) / "languages").glob("*/config.toml")):
            try:
                inputs.append(("project", str(path), path.read_bytes()))
            except OSError:
                pass
    lang_dir = _find_languages_dir()
    if lang_dir:
        for path in sorted(lang_dir.glob("*/config.toml")):
            try:
                inputs.append(("builtin", str(path), path.read_bytes()))
            except OSError:
                pass
    for name, directory in sorted(_discover_entry_point_languages().items()):
        path = directory / "config.toml"
        try:
            inputs.append(("plugin", name, path.read_bytes()))
        except OSError:
            pass
    return tuple(inputs)


@lru_cache(maxsize=8)
def _build_registry(
    inputs: tuple[tuple[str, str, bytes], ...],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return ``(ext_to_lang, lang_to_exts)`` built from TOML configs + builtins.

    Results are content-addressed so live configuration edits cannot leave a
    stale extension map behind.
    """
    ext_to_lang: dict[str, str] = {}
    lang_to_exts: dict[str, list[str]] = {}

    def register(name: str, extensions: list[str]) -> None:
        lang_to_exts[name] = extensions
        for extension in extensions:
            ext_to_lang.setdefault(extension.lower(), name)

    for source, label, payload in inputs:
        result = _parse_toml_extensions(payload, label)
        if result:
            name, exts = result
            if name in lang_to_exts:
                continue
            register(name, exts)

    # Fill in any gaps from hardcoded builtins
    for lang, exts in _BUILTIN.items():
        if lang not in lang_to_exts:
            lang_to_exts[lang] = list(exts)
        for ext in exts:
            ext_to_lang.setdefault(ext.lower(), lang)

    return ext_to_lang, lang_to_exts


def registry_snapshot(
    project_root: str | Path | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return one immutable-by-convention, exact registry revision."""
    ext_to_lang, lang_to_exts = _build_registry(_registry_inputs(project_root))
    return dict(ext_to_lang), {
        language: list(extensions) for language, extensions in lang_to_exts.items()
    }


def registry_and_config_snapshots(
    project_root: str | Path | None = None,
) -> tuple[
    tuple[dict[str, str], dict[str, list[str]]],
    dict[str, LanguageConfigRevision],
]:
    """Build extension and config views from one immutable byte capture."""
    inputs = _registry_inputs(project_root)
    snapshots: dict[str, LanguageConfigRevision] = {}
    for _source, label, payload in inputs:
        parsed = _parse_toml_extensions(payload, label)
        if parsed is None:
            continue
        language, _extensions = parsed
        snapshots.setdefault(
            language,
            LanguageConfigRevision.create(language, payload.decode()),
        )
    ext_to_lang, lang_to_exts = _build_registry(inputs)
    registry = dict(ext_to_lang), {
        language: list(extensions) for language, extensions in lang_to_exts.items()
    }
    return registry, snapshots


def language_config_snapshots(
    project_root: str | Path | None = None,
) -> dict[str, LanguageConfigRevision]:
    """Capture the effective config bytes for every language in one registry view."""
    return registry_and_config_snapshots(project_root)[1]


def language_config_snapshot(
    language: str,
    project_root: str | Path | None = None,
) -> LanguageConfigRevision:
    """Return the exact immutable config revision used for *language*."""
    snapshot = language_config_snapshots(project_root).get(language)
    if snapshot is None:
        raise ValueError(f"no language configuration registered for {language!r}")
    return snapshot


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_language(
    path: str | Path,
    *,
    registry: tuple[dict[str, str], dict[str, list[str]]] | None = None,
) -> str | None:
    """Return the language name for *path* based on its extension, or ``None``.

    Examples::

        detect_language("foo.py")    # "python"
        detect_language("bar.ts")    # "typescript"
        detect_language("baz.txt")   # None
    """
    ext = Path(path).suffix.lstrip(".").lower()
    if not ext:
        return None
    ext_to_lang, _ = registry or registry_snapshot()
    return ext_to_lang.get(ext)


def get_extensions(language: str) -> list[str]:
    """Return file extensions (without leading dot) registered for *language*.

    Returns an empty list for unknown languages.

    Example::

        get_extensions("python")       # ["py", "pyi"]
        get_extensions("typescript")   # ["ts", "tsx", "js", "jsx"]
        get_extensions("cobol")        # []
    """
    _, lang_to_exts = registry_snapshot()
    return lang_to_exts.get(language, []) or _BUILTIN.get(language, [])


def get_all_languages() -> list[str]:
    """Return all registered language names."""
    _, lang_to_exts = registry_snapshot()
    return list(lang_to_exts.keys())


def matches_language(path: str | Path, language: str) -> bool:
    """Return ``True`` if *path*'s extension belongs to *language*."""
    return detect_language(path) == language


def is_source_file(path: str | Path) -> bool:
    """Return ``True`` if *path* has an extension known to any registered language."""
    return detect_language(path) is not None


def get_module_separator(
    language: str, config: LanguageConfigRevision | None = None
) -> str:
    """Return the qualified-name separator for *language* (e.g. ``"."`` or ``"::"``)."""
    document = load_config(language, config=config)
    return document.get("qualified_names", {}).get("module_separator", ".")


def get_comment_prefix(language: str) -> str:
    """Return the line-comment prefix for *language* (e.g. ``"#"`` or ``"//"``)."""
    config = load_config(language)
    # Prefer the dedicated [comments] section; fall back to the legacy
    # [language].comment_prefix key.
    comments_section = config.get("comments", {})
    if "line_prefix" in comments_section:
        return comments_section["line_prefix"]
    return config.get("language", {}).get("comment_prefix", "#")


def load_config(
    language: str, *, config: LanguageConfigRevision | None = None
) -> dict:
    """Load the full TOML configuration for *language*.

    Returns an empty dict if the language or config file is not found.
    Checks built-in languages first, then entry-point plugins.
    """
    if config is not None:
        return _parse_config_payload(config.payload, language)
    config_path = _config_path(language)

    return _load_config(language, config_identity(language), config_path)


def _parse_config_payload(payload: str | bytes, label: str) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}
    try:
        return tomllib.loads(payload.decode() if isinstance(payload, bytes) else payload)
    except (UnicodeError, ValueError):
        logger.debug("Could not parse language config %s", label, exc_info=True)
        return {}


@lru_cache(maxsize=32)
def _load_config(language: str, _identity: str, config_path: Path | None) -> dict:
    """Parse one exact config revision, reusing unchanged revisions."""
    if config_path is None:
        return {}

    try:
        return _parse_config_payload(config_path.read_bytes(), str(config_path))
    except OSError:
        logger.debug("Could not read %s", config_path, exc_info=True)
        return {}


# Preserve the cache-management hook exposed by the formerly decorated loader.
load_config.cache_clear = _load_config.cache_clear  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tree-sitter-based export detection
# ---------------------------------------------------------------------------

def detect_exported_names(
    content: str,
    language: str,
    *,
    extension: str | None = None,
    config: LanguageConfigRevision | None = None,
) -> set[str]:
    """Detect explicit module exports using the canonical native parser."""
    if language not in ("python", "typescript", "javascript", "rust"):
        return set()

    from emend import emend_core

    revision = config or language_config_snapshot(language)
    extensions = get_extensions(language)
    ext = extension or (extensions[0] if extensions else "py")
    return set(emend_core.extract_exported_names(
        content, ext, language, revision.payload,
    ))
