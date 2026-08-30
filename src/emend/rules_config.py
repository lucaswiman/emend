"""Back-compat shim — real code lives in emend.checks.rules_config."""

from emend.checks.rules_config import (  # noqa: F401
    DEFAULT_RULES_PATH,
    DeadCodeConfig,
    coerce_optional_str_list,
    deadcode_engine_kwargs,
    load_rules_document,
    load_yaml_config_with_fallback,
    parse_deadcode_config,
    resolve_config_path_with_fallback,
    resolve_rules_path,
    yaml_key,
    as_list,
    expand_pattern_macros,
    expand_macros,
    expand_not_through,
)
