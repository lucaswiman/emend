"""checks/ package — unified rule engine for emend.

Public surface re-exported for ``from emend.checks import ...`` callers.
"""

from emend.checks.engine import CheckViolation, run_checks

__all__ = ["CheckViolation", "run_checks"]
