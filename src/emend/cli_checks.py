import sys
from typing import Annotated, Optional

import typer

from emend.cli_base import _state, app, resolve_file_scopes, resolve_files
from emend.rules_config import LEGACY_PATTERNS_PATH, resolve_rules_path

@app.command("lint")
def lint_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to lint")],
    config: Annotated[
        Optional[str],
        typer.Option("--config", help="Path to rules.yaml or legacy patterns.yaml config file")
    ] = None,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Auto-apply replace rules")
    ] = False,
    rule: Annotated[
        Optional[str],
        typer.Option("--rule", help="Run only a specific rule by name")
    ] = None,
):
    """Lint files using unified rules from a YAML config.

    Reads rules from .emend/rules.yaml by default, falling back to the legacy
    .emend/patterns.yaml when needed.
    Rules define patterns to find and optional replacements.

    Examples:
        emend lint src/
        emend lint src/ --config .emend/rules.yaml
        emend lint src/ --fix
        emend lint src/ --rule no-print
    """
    try:
        from emend.lint import load_rules, run_lint

        config_path = resolve_rules_path(config, fallbacks=(LEGACY_PATTERNS_PATH,))
        if not config_path.exists():
            print(f"Error: Config file not found: {config_path}", file=sys.stderr)
            raise typer.Exit(2)

        rules, macros, deadcode_config = load_rules(str(config_path))

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved]

        violations = run_lint(
            rules, files, fix=fix, rule_filter=rule,
            deadcode_config=deadcode_config, project_path=path,
            language=_lang,
        )

        for v in violations:
            print(f"{v.file_path}:{v.line}:{v.col}: [{v.rule_name}] {v.message}")

        if violations:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


import sys
from typing import Optional
import typer
from typing import Annotated
from emend.rules_config import LEGACY_PATTERNS_PATH, LEGACY_POLICIES_PATH, resolve_rules_path

@app.command("policy")
def policy_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to check")],
    config: Annotated[Optional[str], typer.Option("--config", help="Path to rules.yaml or legacy policies.yaml")] = None,
    policy_name: Annotated[Optional[str], typer.Option("--policy", "-p", help="Run only a specific policy")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
):
    """Run policy checks against source code.

    Policies combine flow analysis, structural checks, type constraints,
    and dead code detection into named, reusable compliance rules loaded
    from .emend/rules.yaml by default, falling back to .emend/policies.yaml.

    Examples:
        emend policy src/
        emend policy src/ --config .emend/rules.yaml
        emend policy src/ --policy no-sql-injection
        emend policy src/ --json
    """
    try:
        from emend.policy import load_policies, run_policy_checks, format_policy_violations

        config_path = resolve_rules_path(
            config,
            fallbacks=(LEGACY_POLICIES_PATH, LEGACY_PATTERNS_PATH),
        )
        if not config_path.exists():
            print(f"Error: Config file not found: {config_path}", file=sys.stderr)
            raise typer.Exit(2)

        policies = load_policies(str(config_path))
        if policy_name:
            policies = [p for p in policies if p.name == policy_name]
            if not policies:
                print(f"Error: Policy '{policy_name}' not found.", file=sys.stderr)
                raise typer.Exit(2)

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved]

        violations = run_policy_checks(files, policies, language=_lang, project_path=path)

        output = format_policy_violations(violations, json_output=json_output)
        if output:
            print(output, end='' if not output.endswith('\n') else '')

        if violations:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


import sys
from typing import Optional
import typer
from typing import Annotated

@app.command("check")
def check_cmd(
    paths: Annotated[Optional[list[str]], typer.Argument(help="File or directory scope(s) to check")] = None,
    config: Annotated[Optional[str], typer.Option("--config", help="Path to rules.yaml")] = None,
    rule_name: Annotated[Optional[str], typer.Option("--rule", help="Run only a specific rule")] = None,
    kind: Annotated[Optional[str], typer.Option("--kind", help="Restrict to one rule kind: match, flow, deadcode, type")] = None,
    fix: Annotated[bool, typer.Option("--fix", help="Apply auto-fixes for match rules")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
):
    """Run unified project rules from ``.emend/rules.yaml``."""
    try:
        import json as _json

        from emend.checks import run_checks

        _lang = _state["language"]
        resolved, _ = resolve_file_scopes(paths or ["."], language=_lang)
        file_paths = [str(f) for f in resolved]
        project_path = paths[0] if paths else "."
        violations = run_checks(
            file_paths,
            config=config,
            rule_name=rule_name,
            kind=kind,
            fix=fix,
            language=_lang,
            project_path=project_path,
        )

        if json_output:
            print(_json.dumps([
                {
                    "rule": violation.rule_name,
                    "kind": violation.kind,
                    "severity": violation.severity,
                    "message": violation.message,
                    "file": violation.file_path,
                    "line": violation.line,
                    "col": violation.col,
                    "witness": violation.witness or [],
                }
                for violation in violations
            ], indent=2))
        else:
            for violation in violations:
                print(
                    f"{violation.file_path}:{violation.line}:{violation.col}: "
                    f"[{violation.kind}:{violation.rule_name}] {violation.message}"
                )
                for witness_line in violation.witness or []:
                    print(f"  {witness_line}")

        if violations:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)
