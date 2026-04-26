import sys
from typing import Annotated, Optional

import typer

from emend.cli_base import JsonFlag, _state, resolve_file_scopes, resolve_files
from emend.rules_config import resolve_rules_path

def lint_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to lint")],
    config: Annotated[
        Optional[str],
        typer.Option("--config", help="Path to rules.yaml config file")
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

    Reads rules from .emend/rules.yaml by default.
    Rules define patterns to find and optional replacements.

    Examples:
        emend lint src/
        emend lint src/ --config .emend/rules.yaml
        emend lint src/ --fix
        emend lint src/ --rule no-print
    """
    try:
        from emend.checks import run_checks

        config_path = resolve_rules_path(config)
        if not config_path.exists():
            print(f"Error: Config file not found: {config_path}", file=sys.stderr)
            raise typer.Exit(2)

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=None)
        files = [str(f) for f in resolved]

        violations = run_checks(
            files,
            config=str(config_path),
            rule_name=rule,
            mode="lint",
            fix=fix,
            language=_lang,
            project_path=path,
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



def policy_cmd(
    path: Annotated[str, typer.Argument(help="File or directory to check")],
    config: Annotated[Optional[str], typer.Option("--config", help="Path to rules.yaml")] = None,
    policy_name: Annotated[Optional[str], typer.Option("--policy", "-p", help="Run only a specific policy")] = None,
    json_output: JsonFlag = False,
):
    """Run policy checks against source code.

    Policies combine flow analysis, structural checks, type constraints,
    and dead code detection into named, reusable compliance rules loaded
    from .emend/rules.yaml.

    Examples:
        emend policy src/
        emend policy src/ --config .emend/rules.yaml
        emend policy src/ --policy no-sql-injection
        emend policy src/ --json
    """
    try:
        from emend.checks import run_checks
        from emend.policy import format_policy_violations, PolicyViolation

        config_path = resolve_rules_path(config)
        if not config_path.exists():
            print(f"Error: Config file not found: {config_path}", file=sys.stderr)
            raise typer.Exit(2)

        _lang = _state["language"]
        resolved, _ = resolve_files(path, language=_lang)
        files = [str(f) for f in resolved]

        violations = run_checks(
            files,
            config=str(config_path),
            rule_name=policy_name,
            mode="policy",
            language=_lang,
            project_path=path,
        )

        if json_output:
            import json as _json
            data = [
                {
                    "file": v.file_path,
                    "line": v.line,
                    "col": v.col,
                    "policy": v.rule_name,
                    "check": v.kind,
                    "severity": v.severity,
                    "message": v.message,
                    "witness": v.witness or [],
                }
                for v in violations
            ]
            output = _json.dumps(data, indent=2)
            if output:
                print(output, end='' if not output.endswith('\n') else '')
        else:
            current_policy = ""
            for v in violations:
                if v.rule_name != current_policy:
                    if current_policy:
                        print()
                    print(f"[{v.severity.upper()}] {v.rule_name}")
                    current_policy = v.rule_name
                location = f"{v.file_path}:{v.line}"
                if v.col:
                    location += f":{v.col}"
                print(f"  {location}: {v.message}")
                for w in v.witness or []:
                    print(f"    | {w}")
            if violations:
                error_count = sum(1 for v in violations if v.severity == "error")
                warning_count = sum(1 for v in violations if v.severity == "warning")
                info_count = sum(1 for v in violations if v.severity == "info")
                parts = []
                if error_count:
                    parts.append(f"{error_count} error(s)")
                if warning_count:
                    parts.append(f"{warning_count} warning(s)")
                if info_count:
                    parts.append(f"{info_count} info(s)")
                print()
                print(f"Found {len(violations)} violation(s): {', '.join(parts)}")
            else:
                print("No policy violations found.")

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



def check_cmd(
    paths: Annotated[Optional[list[str]], typer.Argument(help="File or directory scope(s) to check")] = None,
    config: Annotated[Optional[str], typer.Option("--config", help="Path to rules.yaml")] = None,
    rule_name: Annotated[Optional[str], typer.Option("--rule", help="Run only a specific rule")] = None,
    kind: Annotated[Optional[str], typer.Option("--kind", help="Restrict to one rule kind: match, flow, deadcode, type")] = None,
    fix: Annotated[bool, typer.Option("--fix", help="Apply auto-fixes for match rules")] = False,
    json_output: JsonFlag = False,
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
