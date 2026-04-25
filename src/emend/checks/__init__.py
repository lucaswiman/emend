"""checks package — unified rule runner for emend.

Public surface (stable):
    run_checks(paths, *, config, rule_name, kind, fix, language, project_path)
    CheckViolation

This package consolidates lint.py, policy.py, checks.py, flow_ir.py, and
rules_config.py into a single rule-kind-dispatched engine (checks/engine.py).
Back-compat shims at the original module paths re-export from here so existing
imports continue to work.

Note: imports from engine are lazy to avoid circular import issues.
The import chain ``checks.__init__ -> engine -> lint -> rules_config -> checks``
is broken by deferring the engine imports until first use.
"""

from __future__ import annotations


def __getattr__(name: str) -> object:
    """Lazy attribute access to avoid circular import during module init."""
    if name in ("run_checks", "CheckViolation", "load_rules"):
        from emend.checks import engine as _engine
        return getattr(_engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["run_checks", "CheckViolation", "load_rules"]
