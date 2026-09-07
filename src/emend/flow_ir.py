"""Back-compat shim — real code lives in emend.checks.flow."""

from emend.checks.flow import (  # noqa: F401
    FlowSpec,
    WitnessStep,
    FlowViolation,
    from_lint_rule,
    from_flow_check,
    format_witness,
    execute_flow_spec,
    evaluate_compiled_flow,
)
