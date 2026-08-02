import glob as glob_mod
import logging
import re as _re_module
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Optional

import click
import typer
from lark.exceptions import LarkError

from emend.component_selector import parse_extended_selector, parse_selector


# Shared Typer flag type aliases. Using these keeps CLI signatures short and
# gives the same flag identical help text across commands. Commands that need
# a short option (e.g. ``-a``) or distinct wording continue to declare
# ``typer.Option`` inline.
ApplyFlag = Annotated[bool, typer.Option("--apply", help="Apply changes (default is dry-run)")]
JsonFlag = Annotated[bool, typer.Option("--json", help="Output as JSON")]


@contextmanager
def cli_error_handler():
    """Standard CLI error handling: FileNotFoundError -> exit 3, ValueError -> exit 2, Exception -> exit 1.

    `typer.Exit` is re-raised unchanged so commands can signal nonzero exit codes
    (e.g. lint violations) without being treated as a generic failure.
    """
    try:
        yield
    except typer.Exit:
        raise
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(3)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(2)
    except Exception as e:
        print(f"Error: {e!r}", file=sys.stderr)
        raise typer.Exit(1)


def _maybe_create_oracle(type_engine: str | None):
    """Create a TypeOracle if *type_engine* is specified, returning ``None`` if unavailable."""
    from emend.type_oracle import create_type_oracle

    engine = type_engine or "pyrefly"
    oracle = create_type_oracle(engine=engine)
    if not oracle.is_available():
        logging.getLogger("emend.type_oracle").warning(
            "Type engine '%s' not available; type constraints will have no effect",
            engine,
        )
        return None
    return oracle


def _reject_file_glob(selector_str: str, command_name: str) -> None:
    """Raise ValueError if selector contains file globs (for commands that don't support them)."""
    file_part, _ = parse_selector(selector_str)
    if "*" in file_part or "?" in file_part:
        raise ValueError(
            f"File glob selectors are not supported for {command_name}. "
            "Use a specific file path instead."
        )


def resolve_files(path: str, language: str | None = "python") -> tuple[list[Path], bool]:
    """Resolve a path argument to a list of source files.

    When *language* is ``None``, files of **all** registered languages are
    collected (useful for lint which should scan everything).
    """
    from emend.language_registry import get_extensions, matches_language, get_all_languages, is_source_file

    path_obj = Path(path)
    if path_obj.is_dir():
        from emend import emend_core

        abs_path = str(path_obj.resolve())
        if language is None:
            all_exts: list[str] = []
            for lang in get_all_languages():
                all_exts.extend(get_extensions(lang))
            exts = list(dict.fromkeys(all_exts))  # deduplicate, preserve order
        else:
            exts = get_extensions(language)
        return [Path(f) for f in emend_core.collect_files(abs_path, exts)], True
    if "*" in path or "?" in path:
        if language is None:
            return [
                Path(f)
                for f in glob_mod.glob(path, recursive=True)
                if is_source_file(f)
            ], True
        return [
            Path(f)
            for f in glob_mod.glob(path, recursive=True)
            if matches_language(f, language)
        ], True
    return [path_obj], False


def resolve_file_scopes(
    paths: list[str] | tuple[str, ...] | None,
    language: str = "python",
) -> tuple[list[Path], bool]:
    """Resolve multiple file/path arguments into a deduplicated file list."""
    raw_paths = list(paths or [])

    resolved: list[Path] = []
    seen: set[str] = set()
    is_multi_file = len(raw_paths) != 1
    for path in raw_paths:
        files, path_is_multi = resolve_files(path, language=language)
        is_multi_file = is_multi_file or path_is_multi or len(files) > 1
        for file_path in files:
            file_key = str(file_path)
            if file_key in seen:
                continue
            seen.add(file_key)
            resolved.append(file_path)
    return resolved, is_multi_file


def resolve_many_files(
    paths: list[str] | tuple[str, ...] | None,
    *,
    language: str = "python",
    default: str | None = None,
) -> tuple[list[Path], bool]:
    """Compatibility wrapper for resolving multiple file scopes."""
    effective_paths = paths
    if not effective_paths and default is not None:
        effective_paths = [default]
    return resolve_file_scopes(effective_paths, language=language)


def print_symbol_rename_move(diffs: dict[str, str], *, apply: bool) -> None:
    """Print CLI output for a symbol rename/move given the per-file diffs."""
    if not diffs:
        print("No changes needed.")
        return
    for _file_path, diff in diffs.items():
        if diff:
            print(diff, end='')
    if not apply:
        print("\nRun with --apply to write changes.")


def print_module_rename_move(
    diffs: dict[str, str], *, apply: bool, success_msg: str
) -> None:
    """Print CLI output for a module rename/move given the resulting diffs."""
    if apply:
        print(success_msg)
        return
    if "__description__" in diffs:
        print("\n" + "=" * 60)
        print("CHANGES PREVIEW")
        print("=" * 60)
        print(diffs["__description__"])
        print("=" * 60 + "\n")
    else:
        for _file_path, diff in diffs.items():
            if diff:
                print(diff)
    print("\nRun with --apply to apply these changes.")


def format_symbol_rename_move(diffs: dict[str, str], *, apply: bool) -> str:
    """Return MCP output text for a symbol rename/move given the per-file diffs."""
    if not diffs:
        return "No changes needed."
    result = "".join(d for d in diffs.values() if d)
    if not apply:
        result += "\nDry-run. Set apply=True to write changes."
    return result


def format_module_rename_move(
    diffs: dict[str, str], *, apply: bool, success_msg: str
) -> str:
    """Return MCP output text for a module rename/move given the resulting diffs."""
    if apply:
        return success_msg
    if "__description__" in diffs:
        return diffs["__description__"] + "\nDry-run. Set apply=True to write changes."
    parts = [d for d in diffs.values() if d]
    return "".join(parts) + "\nDry-run. Set apply=True to write changes."


_state: dict[str, str] = {"language": "python"}


@dataclass
class QueryShape:
    """Result of detecting a query's mode (pattern, selector, or line)."""

    query: str
    path: str | None
    is_pattern_mode: bool
    has_selector: bool
    is_line_selector: bool


def detect_query_shape(query: str, path: str | None = None) -> QueryShape:
    """Detect whether a search query is a pattern, selector, or line selector."""
    is_line_selector = bool(_re_module.search(r":\d+(-\d+)?$", query))
    is_pattern_mode = False
    has_selector = False

    if "::" in query and not is_line_selector:
        file_part, right_part = parse_selector(query)
        if "$" in right_part:
            is_pattern_mode = True
        else:
            selector_query = query if not query.startswith("::") else "**" + query
            try:
                parse_extended_selector(selector_query)
                has_selector = True
            except LarkError:
                is_pattern_mode = True

        if is_pattern_mode:
            query = right_part
            file_scope = file_part.strip()
            if not path and file_scope and file_scope != "**":
                path = file_scope
    elif "$" in query:
        is_pattern_mode = True
    elif _re_module.match(r"\s*(?:async\s+)?(?:def|class)\s+\w*[*?]", query):
        is_pattern_mode = True

    if has_selector and query.startswith("::"):
        query = "**" + query

    return QueryShape(
        query=query,
        path=path,
        is_pattern_mode=is_pattern_mode,
        has_selector=has_selector,
        is_line_selector=is_line_selector,
    )


_STRUCTURAL_KEYWORDS = (
    "def",
    "async def",
    "class",
    "for",
    "while",
    "try",
    "with",
    "if",
    "except",
)


def parse_where_clause(values: list[str]) -> dict:
    """Parse --where values into internal API params."""
    result: dict = {}
    for value in values:
        if not value.strip():
            # An empty --where (e.g. from an unset shell variable) would
            # otherwise fall through to the scope branch and produce the
            # filter [""], which silently matches nothing.
            continue
        if value.startswith("not "):
            result["not_inside"] = value[4:].strip()
        elif value.startswith("@"):
            result["matching"] = value
        elif "$" in value:
            result["matching"] = value
        elif any(
            value == kw or value.startswith(kw + " ") or value.startswith(kw + ":")
            for kw in _STRUCTURAL_KEYWORDS
        ):
            result["inside"] = value
        else:
            result["scope"] = value.split(".")
    return result


class _LegacyEditGroup(typer.core.TyperGroup):
    """Route unknown `edit` forms to `edit set` for legacy compatibility."""

    _usage_error = getattr(
        typer, "_click", click
    ).exceptions.UsageError

    def resolve_command(self, ctx: click.Context, args: list[str]):
        try:
            return super().resolve_command(ctx, args)
        except (click.UsageError, self._usage_error):
            return super().resolve_command(ctx, ["set", *args])


app = typer.Typer(
    help="Python refactoring CLI",
    no_args_is_help=True,
    add_completion=False,
)
edit_app = typer.Typer(help="Code changes and refactors.", cls=_LegacyEditGroup)
analyze_app = typer.Typer(help="Read-only code analysis commands.")
tool_app = typer.Typer(help="Infrastructure and debugging commands.")
app.add_typer(edit_app, name="edit")
app.add_typer(analyze_app, name="analyze")
app.add_typer(tool_app, name="tool")


def _version_callback(value: bool) -> None:
    if value:
        from emend import __version__

        typer.echo(f"emend {__version__}")
        raise typer.Exit()


@app.callback()
def _app_callback(
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = None,
    verbose: Annotated[
        int,
        typer.Option(
            "-v",
            "--verbose",
            count=True,
            help="Verbose output (-v info, -vv debug with timestamps).",
        ),
    ] = 0,
    language: Annotated[
        Optional[str],
        typer.Option(
            "--language",
            "-L",
            help="Source language (python, typescript, etc.). Default: python.",
        ),
    ] = None,
) -> None:
    if verbose >= 2:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
    elif verbose >= 1:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s.%(msecs)03d %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
    if language is not None:
        _state["language"] = language


__all__ = [
    "ApplyFlag",
    "JsonFlag",
    "QueryShape",
    "_LegacyEditGroup",
    "_app_callback",
    "_maybe_create_oracle",
    "_reject_file_glob",
    "_state",
    "_version_callback",
    "analyze_app",
    "app",
    "cli_error_handler",
    "detect_query_shape",
    "edit_app",
    "format_module_rename_move",
    "format_symbol_rename_move",
    "parse_where_clause",
    "print_module_rename_move",
    "print_symbol_rename_move",
    "resolve_file_scopes",
    "resolve_files",
    "resolve_many_files",
    "tool_app",
]
