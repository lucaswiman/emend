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
    # Installed layout: languages/ may be bundled inside the package
    candidate = Path(__file__).parent / "languages"
    if candidate.is_dir():
        return candidate

    # Dev layout: languages/ sits at the repo root, three levels above
    # src/emend/language_registry.py → src/emend → src → repo_root
    candidate = Path(__file__).parent.parent.parent / "languages"
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


def _config_path(language: str) -> Path | None:
    """Return the effective configuration path for one language."""
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


def config_identity(language: str) -> str:
    """Hash the exact language configuration consumed by analysis."""
    path = _config_path(language)
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
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        data = tomllib.loads(payload.decode())
    except (UnicodeError, ValueError):
        # TOMLDecodeError subclasses ValueError in both tomllib and tomli.
        logger.debug("Could not parse %s", label, exc_info=True)
        return None

    lang = data.get("language", {})
    name = lang.get("name")
    exts = lang.get("file_extensions", [])
    if name and exts:
        return name, list(exts)
    return None


def _registry_inputs() -> tuple[tuple[str, str, bytes], ...]:
    """Capture the exact language configurations used by one registry view."""
    inputs: list[tuple[str, str, bytes]] = []
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
            if source == "plugin" and name in lang_to_exts:
                continue
            register(name, exts)

    # Fill in any gaps from hardcoded builtins
    for lang, exts in _BUILTIN.items():
        if lang not in lang_to_exts:
            lang_to_exts[lang] = list(exts)
        for ext in exts:
            ext_to_lang.setdefault(ext.lower(), lang)

    return ext_to_lang, lang_to_exts


def registry_snapshot() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return one immutable-by-convention, exact registry revision."""
    return _build_registry(_registry_inputs())


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


def get_module_separator(language: str) -> str:
    """Return the qualified-name separator for *language* (e.g. ``"."`` or ``"::"``)."""
    config = load_config(language)
    return config.get("qualified_names", {}).get("module_separator", ".")


def get_comment_prefix(language: str) -> str:
    """Return the line-comment prefix for *language* (e.g. ``"#"`` or ``"//"``)."""
    config = load_config(language)
    # Prefer the dedicated [comments] section; fall back to the legacy
    # [language].comment_prefix key.
    comments_section = config.get("comments", {})
    if "line_prefix" in comments_section:
        return comments_section["line_prefix"]
    return config.get("language", {}).get("comment_prefix", "#")


def load_config(language: str) -> dict:
    """Load the full TOML configuration for *language*.

    Returns an empty dict if the language or config file is not found.
    Checks built-in languages first, then entry-point plugins.
    """
    config_path = _config_path(language)

    return _load_config(language, config_identity(language), config_path)


@lru_cache(maxsize=32)
def _load_config(language: str, _identity: str, config_path: Path | None) -> dict:
    """Parse one exact config revision, reusing unchanged revisions."""
    if config_path is None:
        return {}

    try:
        if sys.version_info >= (3, 11):
            import tomllib
            with open(config_path, "rb") as fh:
                return tomllib.load(fh)
        else:
            # Fallback for Python < 3.11: use tomli if available, else empty
            try:
                import tomli
                return tomli.loads(config_path.read_text())
            except ImportError:
                return {}
    except (OSError, ValueError):
        # TOMLDecodeError subclasses ValueError in both tomllib and tomli.
        logger.debug("Could not parse %s", config_path, exc_info=True)
        return {}


# Preserve the cache-management hook exposed by the formerly decorated loader.
load_config.cache_clear = _load_config.cache_clear  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tree-sitter-based export detection
# ---------------------------------------------------------------------------

def detect_exported_names(content: str, language: str) -> set[str]:
    """Detect explicit module exports using the canonical native parser."""
    if language not in ("python", "typescript", "javascript", "rust"):
        return set()

    from emend import emend_core

    extensions = get_extensions(language)
    ext = extensions[0] if extensions else "py"
    languages_dir = _find_languages_dir()
    config_root = languages_dir.parent if languages_dir is not None else Path(".")
    return set(emend_core.extract_exported_names(
        content, ext, str(config_root), language,
    ))
