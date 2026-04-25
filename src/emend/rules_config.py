"""Back-compat shim: re-exports from emend.checks.rules_config.

The canonical location of this module's content is now
``emend.checks.rules_config``.  This file is kept as a one-line re-export
shim so existing ``from emend.rules_config import ...`` calls continue
to work without changes.
"""

# ruff: noqa: F401
from emend.checks.rules_config import (  # noqa: F401
    DEFAULT_RULES_PATH,
    LEGACY_PATTERNS_PATH,
    LEGACY_POLICIES_PATH,
    DeadCodeConfig,
    load_rules_document,
    load_yaml_config_with_fallback,
    resolve_config_path_with_fallback,
    resolve_rules_path,
    yaml_key,
    as_list,
    expand_pattern_macros,
    expand_macros,
    expand_not_through,
)

__all__ = [
    "DEFAULT_RULES_PATH",
    "LEGACY_PATTERNS_PATH",
    "LEGACY_POLICIES_PATH",
    "DeadCodeConfig",
    "load_rules_document",
    "load_yaml_config_with_fallback",
    "resolve_config_path_with_fallback",
    "resolve_rules_path",
    "yaml_key",
    "as_list",
    "expand_pattern_macros",
    "expand_macros",
    "expand_not_through",
]
