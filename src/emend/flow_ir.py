"""Back-compat shim: re-exports from emend.checks.flow.

The canonical location of this module's content is now
``emend.checks.flow``.  This file is kept as a one-line re-export
shim so existing ``from emend.flow_ir import ...`` calls continue
to work without changes.
"""

# ruff: noqa: F401
from emend.checks.flow import (  # noqa: F401
    FlowSpec,
    FlowViolation,
    WitnessStep,
    execute_flow_spec,
    from_lint_rule,
    from_flow_check,
    format_witness,
    _flow_witness_to_steps,
    _var_name_from_match,
    _execute_via_python,
    _execute_via_datalog,
)

__all__ = [
    "FlowSpec",
    "FlowViolation",
    "WitnessStep",
    "execute_flow_spec",
    "from_lint_rule",
    "from_flow_check",
    "format_witness",
]
