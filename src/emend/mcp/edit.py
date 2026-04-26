"""MCP edit/transform tools."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Annotated

from pydantic import Field

from emend.component_selector import parse_extended_selector
from emend.transform import (
    replace_pattern,
    rename_symbol,
    move_symbol,
    move_module,
    rename_module,
    cmd_edit,
    cmd_add,
)
from emend import ast_commands

from emend.mcp.dispatch import mcp_app


def replace(
    pattern: Annotated[str, Field(description="Pattern to find (e.g. 'print($X)').")],
    replacement: Annotated[str, Field(description="Replacement pattern using same $X captures (e.g. 'logger.info($X)').")],
    path: Annotated[str, Field(description="Python file, glob, or directory to modify.")],
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    where: Annotated[str | None, Field(description="Scope constraint: 'def test_*', 'not class', 'MyClass.method'.")] = None,
) -> str:
    """Replace pattern matches in Python file(s).

    Supports $X metavariables in both pattern and replacement.
    By default shows a diff (dry-run). Set apply=True to write changes.
    """
    from emend.cli_base import resolve_files, parse_where_clause

    where_params = parse_where_clause([where] if where else [])
    scope = where_params.get("scope")
    inside = where_params.get("inside")
    not_inside = where_params.get("not_inside")

    files, is_multi_file = resolve_files(path)
    all_diffs: list[str] = []
    total_count = 0
    for file_path in files:
        file_path_str = str(file_path)
        try:
            diff, cnt = replace_pattern(
                pattern,
                replacement,
                file_path_str,
                scope=scope,
                apply=apply,
                inside=inside,
                not_inside=not_inside,
            )
            if diff:
                all_diffs.append(diff)
            total_count += cnt
        except FileNotFoundError:
            if not is_multi_file:
                raise
            continue

    result = "".join(all_diffs)
    if total_count > 0:
        result += f"\n{total_count} replacement(s) {'applied' if apply else 'found (dry-run)'}."
    else:
        result = "No matches found."
    return result


def modify(
    selector: Annotated[str, Field(description="Symbol selector with component (e.g. 'file.py::func[returns]', 'file.py::func[params]', 'file.py::Class[bases]').")],
    value: Annotated[str | None, Field(description="New value. Required for 'set' and 'add' modes. Omit for 'remove'.")] = None,
    mode: Annotated[str, Field(description="Operation: 'set' replaces a component value, 'add' inserts into a list component (params/bases/decorators), 'remove' deletes the component or symbol.")] = "set",
    before: Annotated[str | None, Field(description="Insert before this named item (add mode only).")] = None,
    after: Annotated[str | None, Field(description="Insert after this named item (add mode only).")] = None,
    at: Annotated[int | None, Field(description="Insert at position, 0-indexed (add mode only).")] = None,
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
) -> str:
    """Modify a symbol component: set its value, add to a list, or remove it."""
    if mode == "set":
        return cmd_edit(selector_str=selector, value=value, rm=False, apply=apply)
    elif mode == "add":
        if value is None:
            return "Error: value is required for 'add' mode."
        return cmd_add(
            selector_str=selector,
            value=value,
            before=before,
            after=after,
            at=at,
            apply=apply,
        )
    elif mode == "remove":
        return cmd_edit(selector_str=selector, rm=True, apply=apply)
    else:
        return f"Error: unknown mode '{mode}'. Use 'set', 'add', or 'remove'."


def rename(
    selector: Annotated[str, Field(description="Symbol selector (file.py::name) or module path (file.py). Uses :: for symbols, bare path for modules.")],
    to: Annotated[str, Field(description="New name for the symbol or module.")],
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    docs: Annotated[bool, Field(description="Also rename in docstrings (symbol mode only).")] = False,
    no_hierarchy: Annotated[bool, Field(description="Don't rename in class hierarchy (symbol mode only).")] = False,
    unsure: Annotated[bool, Field(description="Rename uncertain occurrences (symbol mode only).")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Rename a symbol or module across the project, updating all references."""
    if "::" in selector:
        parsed = parse_extended_selector(selector)
        diffs = rename_symbol(
            parsed,
            to,
            project,
            in_hierarchy=not no_hierarchy,
            docs=docs,
            unsure=unsure,
            apply=apply,
        )
        if not diffs:
            return "No changes needed."
        parts = [d for d in diffs.values() if d]
        result = "".join(parts)
        if not apply:
            result += "\nDry-run. Set apply=True to write changes."
        return result
    else:
        diffs = rename_module(selector, to, project, apply)
        if apply:
            return "Module renamed successfully."
        if "__description__" in diffs:
            return diffs["__description__"] + "\nDry-run. Set apply=True to write changes."
        parts = [d for d in diffs.values() if d]
        return "".join(parts) + "\nDry-run. Set apply=True to write changes."


def move(
    selector: Annotated[str, Field(description="Symbol selector (file.py::name) or module path (file.py). Uses :: for symbols, bare path for modules.")],
    destination: Annotated[str, Field(description="Destination file or package.")],
    copy_only: Annotated[bool, Field(description="Copy without removing from source (symbol mode only). The body is copied exactly from the AST.")] = False,
    dedent: Annotated[bool, Field(description="Dedent nested symbols (symbol mode only).")] = False,
    no_update_imports: Annotated[bool, Field(description="Don't update imports across the project (symbol mode only).")] = False,
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    project: Annotated[str | None, Field(description="Project root directory.")] = None,
) -> str:
    """Move (or copy) a symbol or module to another file, updating all imports."""
    if copy_only:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ast_commands.cmd_copy_to(selector, destination, append=True, dedent=dedent, apply=apply, project_path=project)
        return buf.getvalue()
    if "::" in selector:
        parsed = parse_extended_selector(selector)
        diffs = move_symbol(
            parsed,
            destination,
            dedent=dedent,
            update_imports=not no_update_imports,
            project_path=project,
            apply=apply,
        )
        if not diffs:
            return "No changes needed."
        parts = [d for d in diffs.values() if d]
        result = "".join(parts)
        if not apply:
            result += "\nDry-run. Set apply=True to write changes."
        return result
    else:
        diffs = move_module(selector, destination, project, apply)
        if apply:
            return "Module moved successfully."
        if "__description__" in diffs:
            return diffs["__description__"] + "\nDry-run. Set apply=True to write changes."
        parts = [d for d in diffs.values() if d]
        return "".join(parts) + "\nDry-run. Set apply=True to write changes."


@mcp_app.tool()
def transform(
    operation: Annotated[str, Field(description="Operation: replace, edit, add, remove, rename, move.")],
    apply: Annotated[bool, Field(description="Write changes to disk. Default is dry-run.")] = False,
    selector: Annotated[str | None, Field(description="Symbol selector for edit/add/remove/rename/move operations.")] = None,
    value: Annotated[str | None, Field(description="New component value for edit/add operations.")] = None,
    before: Annotated[str | None, Field(description="Insert before this name (add operation).")] = None,
    after: Annotated[str | None, Field(description="Insert after this name (add operation).")] = None,
    at: Annotated[int | None, Field(description="Insert at index (add operation).")] = None,
    pattern: Annotated[str | None, Field(description="Code pattern for replace operation.")] = None,
    replacement: Annotated[str | None, Field(description="Replacement code pattern for replace operation.")] = None,
    path: Annotated[str | None, Field(description="File path/glob/dir for replace operation.")] = None,
    destination: Annotated[str | None, Field(description="Destination for move operation.")] = None,
    to: Annotated[str | None, Field(description="Target name for rename operation.")] = None,
    where: Annotated[str | None, Field(description="Compatibility scope constraint for replace.")] = None,
    docs: Annotated[bool, Field(description="Rename docs in rename operation.")] = False,
    no_hierarchy: Annotated[bool, Field(description="Disable hierarchy-aware symbol rename.")] = False,
    unsure: Annotated[bool, Field(description="Rename uncertain occurrences.")] = False,
    copy_only: Annotated[bool, Field(description="Copy instead of move for move operation.")] = False,
    dedent: Annotated[bool, Field(description="Dedent moved/copied symbol body.")] = False,
    no_update_imports: Annotated[bool, Field(description="Skip import updates for move operation.")] = False,
    project: Annotated[str | None, Field(description="Project root for rename/move operations.")] = None,
) -> str:
    """Apply write-style refactoring operations through one discriminated endpoint."""
    op = (operation or "").lower()
    if op == "replace":
        if not pattern or not replacement or not path:
            return json.dumps({"error": "replace requires pattern, replacement, and path"})
        return replace(
            pattern=pattern,
            replacement=replacement,
            path=path,
            apply=apply,
            where=where,
        )
    if op in {"edit", "add", "remove"}:
        if not selector:
            return json.dumps({"error": f"{op} requires selector"})
        mode = "set" if op == "edit" else op
        return modify(
            selector=selector,
            value=value,
            mode=mode,
            before=before,
            after=after,
            at=at,
            apply=apply,
        )
    if op == "rename":
        if not selector or not to:
            return json.dumps({"error": "rename requires selector and to"})
        return rename(
            selector=selector,
            to=to,
            apply=apply,
            docs=docs,
            no_hierarchy=no_hierarchy,
            unsure=unsure,
            project=project,
        )
    if op == "move":
        if not selector or not destination:
            return json.dumps({"error": "move requires selector and destination"})
        return move(
            selector=selector,
            destination=destination,
            copy_only=copy_only,
            dedent=dedent,
            no_update_imports=no_update_imports,
            apply=apply,
            project=project,
        )
    return json.dumps({"error": f"Unknown operation {operation!r}. Use: replace, edit, add, remove, rename, move."})
