"""Back-compat shim — real code lives in emend.checks.rules_config."""

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
