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

import sys
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Hardcoded fallbacks
# Maps language name → list of file extensions (without leading dot).
# These are used when no TOML files are found / parseable.
# ---------------------------------------------------------------------------
_BUILTIN: dict[str, list[str]] = {
    "python": ["py", "pyi"],
    "typescript": ["ts", "tsx", "js", "jsx"],
    "rust": ["rs"],
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
                continue
    except Exception:
        pass
    return result


def _parse_toml_extensions(path: Path) -> tuple[str, list[str]] | None:
    """Return (language_name, [extensions]) from a config.toml, or None on error."""
    try:
        if sys.version_info >= (3, 11):
            import tomllib  # stdlib ≥ 3.11
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        else:
            # Python 3.10: parse just the two fields we need with regex
            import re
            text = path.read_text()
            name_m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
            exts_m = re.search(r'^file_extensions\s*=\s*\[([^\]]+)\]', text, re.MULTILINE)
            if not name_m or not exts_m:
                return None
            name = name_m.group(1)
            exts = [
                e.strip().strip('"')
                for e in exts_m.group(1).split(",")
                if e.strip().strip('"')
            ]
            return name, exts

        lang = data.get("language", {})
        name = lang.get("name")
        exts = lang.get("file_extensions", [])
        if name and exts:
            return name, list(exts)
        return None
    except Exception:
        return None


@lru_cache(maxsize=1)
def _registry() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return ``(ext_to_lang, lang_to_exts)`` built from TOML configs + builtins.

    Results are cached for the lifetime of the process.
    """
    ext_to_lang: dict[str, str] = {}
    lang_to_exts: dict[str, list[str]] = {}

    lang_dir = _find_languages_dir()
    if lang_dir:
        for config_path in sorted(lang_dir.glob("*/config.toml")):
            result = _parse_toml_extensions(config_path)
            if result:
                name, exts = result
                lang_to_exts[name] = exts
                for ext in exts:
                    ext_to_lang.setdefault(ext, name)

    # Discover languages from installed entry-point plugins.
    # These are loaded AFTER built-in languages so they cannot override them.
    for lang_name, config_dir in _discover_entry_point_languages().items():
        if lang_name in lang_to_exts:
            continue  # built-in takes precedence
        result = _parse_toml_extensions(config_dir / "config.toml")
        if result:
            name, exts = result
            lang_to_exts[name] = exts
            for ext in exts:
                ext_to_lang.setdefault(ext, name)

    # Fill in any gaps from hardcoded builtins
    for lang, exts in _BUILTIN.items():
        if lang not in lang_to_exts:
            lang_to_exts[lang] = list(exts)
        for ext in exts:
            ext_to_lang.setdefault(ext, lang)

    return ext_to_lang, lang_to_exts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_language(path: str | Path) -> str | None:
    """Return the language name for *path* based on its extension, or ``None``.

    Examples::

        detect_language("foo.py")    # "python"
        detect_language("bar.ts")    # "typescript"
        detect_language("baz.txt")   # None
    """
    ext = Path(path).suffix.lstrip(".")
    if not ext:
        return None
    ext_to_lang, _ = _registry()
    return ext_to_lang.get(ext)


def get_extensions(language: str) -> list[str]:
    """Return file extensions (without leading dot) registered for *language*.

    Returns an empty list for unknown languages.

    Example::

        get_extensions("python")       # ["py", "pyi"]
        get_extensions("typescript")   # ["ts", "tsx", "js", "jsx"]
        get_extensions("cobol")        # []
    """
    _, lang_to_exts = _registry()
    return lang_to_exts.get(language, []) or _BUILTIN.get(language, [])


def get_all_languages() -> list[str]:
    """Return all registered language names."""
    _, lang_to_exts = _registry()
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
    return config.get("language", {}).get("comment_prefix", "#")


@lru_cache(maxsize=16)
def load_config(language: str) -> dict:
    """Load the full TOML configuration for *language*.

    Returns an empty dict if the language or config file is not found.
    Checks built-in languages first, then entry-point plugins.
    """
    import sys

    config_path: Path | None = None

    # 1. Check built-in languages directory
    lang_dir = _find_languages_dir()
    if lang_dir:
        candidate = lang_dir / language / "config.toml"
        if candidate.is_file():
            config_path = candidate

    # 2. Check entry-point plugins
    if config_path is None:
        ep_langs = _discover_entry_point_languages()
        if language in ep_langs:
            candidate = ep_langs[language] / "config.toml"
            if candidate.is_file():
                config_path = candidate

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
    except Exception:
        return {}
