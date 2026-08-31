import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from lark.exceptions import LarkError

from emend.cli_base import JsonFlag
from emend.component_selector import parse_extended_selector
from emend.knowledge import (
    IdentifierMapping,
    MappingStore,
    ModuleMapping,
    make_resolve_module_cb,
    mapping_to_dict,
    module_mapping_to_dict,
)
from emend.cli_output import emit_json

map_app = typer.Typer(help="Identifier and module mappings.")


def _infer_module_subpath(module_prefix: str, path: str, subpath: str) -> str:
    """Infer a package subpath when *path* points at a repo root.

    If the user maps ``pkg`` to ``/repo`` and ``/repo/pkg`` or ``/repo/src/pkg``
    exists, infer the relative subpath automatically. Explicit ``subpath`` wins.
    """
    if subpath or not path:
        return subpath

    root = Path(os.path.expanduser(path)).resolve()
    if not root.is_dir():
        return subpath

    prefix_parts = tuple(part for part in module_prefix.split(".") if part)
    if not prefix_parts:
        return subpath

    if root.name == prefix_parts[-1]:
        return subpath

    direct_candidate = root.joinpath(*prefix_parts)
    if direct_candidate.is_dir():
        return direct_candidate.relative_to(root).as_posix()

    matches: list[Path] = []
    for candidate in root.rglob(prefix_parts[-1]):
        if not candidate.is_dir():
            continue
        rel_parts = candidate.relative_to(root).parts
        if tuple(rel_parts[-len(prefix_parts):]) == prefix_parts:
            matches.append(candidate)

    if len(matches) == 1:
        return matches[0].relative_to(root).as_posix()

    return subpath

def map_add_cmd(
    source_project: Annotated[str, typer.Argument(help="Source project/repo.")],
    source_id: Annotated[str, typer.Argument(help="Source identifier (e.g. 'users.UserService.create').")],
    target_project: Annotated[str, typer.Argument(help="Target project/repo.")],
    target_id: Annotated[str, typer.Argument(help="Target identifier (e.g. 'POST /api/v1/users').")],
    relationship: Annotated[str, typer.Option("--rel", help="Relationship: equivalent, calls, implements, produces, consumes.")] = "equivalent",
    confidence: Annotated[float, typer.Option("--confidence")] = 1.0,
    provenance: Annotated[str, typer.Option("--provenance", help="manual, heuristic, llm.")] = "manual",
    evidence: Annotated[str, typer.Option("--evidence", help="Why this mapping exists.")] = "",
    source_kind: Annotated[str, typer.Option("--source-kind")] = "",
    target_kind: Annotated[str, typer.Option("--target-kind")] = "",
    json_output: JsonFlag = False,
):
    """Add a cross-service identifier mapping."""
    store = MappingStore(".")
    m = IdentifierMapping(
        source_project=source_project,
        source_identifier=source_id,
        source_kind=source_kind,
        target_project=target_project,
        target_identifier=target_id,
        target_kind=target_kind,
        relationship=relationship,
        confidence=confidence,
        provenance=provenance,
        evidence=evidence,
    )
    store.add_mapping(m)
    if json_output:
        emit_json(mapping_to_dict(m))
    else:
        print(f"Added mapping: {source_project}::{source_id} -> {target_project}::{target_id} ({relationship})")



def map_search_cmd(
    query: Annotated[str, typer.Argument(help="Search query.")],
    source_project: Annotated[Optional[str], typer.Option("--source-project")] = None,
    target_project: Annotated[Optional[str], typer.Option("--target-project")] = None,
    relationship: Annotated[Optional[str], typer.Option("--rel")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    json_output: JsonFlag = False,
):
    """Search identifier mappings (substring match)."""

    store = MappingStore(".")
    results = store.search_mappings(
        query, source_project=source_project,
        target_project=target_project, relationship=relationship, limit=limit,
    )
    if json_output:
        emit_json([mapping_to_dict(m) for m in results])
    elif not results:
        print("No matching mappings.")
    else:
        for m in results:
            conf = f" ({m.confidence:.0%})" if m.confidence < 1.0 else ""
            print(f"{m.source_project}::{m.source_identifier} -> {m.target_project}::{m.target_identifier} [{m.relationship}]{conf}")



def map_lookup_cmd(
    identifier: Annotated[str, typer.Argument(help="Identifier to look up (exact match).")],
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    direction: Annotated[str, typer.Option("--direction", help="source, target, or both.")] = "both",
    json_output: JsonFlag = False,
):
    """Look up mappings for a specific identifier."""
    store = MappingStore(".")
    results = store.find_mappings_for(identifier, project=project, direction=direction)
    if json_output:
        emit_json([mapping_to_dict(m) for m in results])
    elif not results:
        print(f"No mappings found for '{identifier}'.")
    else:
        for m in results:
            print(f"{m.source_project}::{m.source_identifier} -> {m.target_project}::{m.target_identifier} [{m.relationship}]")



def map_rm_cmd(
    source_identifier: Annotated[str, typer.Argument(help="Source identifier to delete mappings for.")],
    source_project: Annotated[Optional[str], typer.Option("--source-project")] = None,
    target_identifier: Annotated[Optional[str], typer.Option("--target-identifier")] = None,
):
    """Delete identifier mappings matching the given source identifier."""
    store = MappingStore(".")
    ok = store.delete_mapping(
        source_identifier,
        source_project=source_project,
        target_identifier=target_identifier,
    )
    if ok:
        print(f"Deleted mapping(s) for '{source_identifier}'.")
    else:
        print(f"No mappings found for '{source_identifier}'.", file=sys.stderr)
        raise typer.Exit(1)



def map_add_module_cmd(
    module_prefix: Annotated[str, typer.Argument(help="Module prefix (e.g. 'payments').")],
    repo: Annotated[str, typer.Option("--repo", help="GitHub repo (org/name).")] = "",
    path: Annotated[str, typer.Option("--path", help="Local directory.")] = "",
    branch: Annotated[str, typer.Option("--branch", help="Branch/tag for gh clone.")] = "",
    subpath: Annotated[str, typer.Option("--subpath", help="Subdirectory within repo.")] = "",
    provenance: Annotated[str, typer.Option("--provenance")] = "manual",
    json_output: JsonFlag = False,
):
    """Register a module prefix -> repo/directory mapping.

    Examples:
        emend map add-module payments --repo org/payments-service
        emend map add-module shared.utils --path /home/user/shared-utils
        emend map add-module gateway --repo org/gateway --subpath src/gateway
    """
    if not repo and not path:
        print("Error: specify --repo or --path", file=sys.stderr)
        raise typer.Exit(1)

    subpath = _infer_module_subpath(module_prefix, path, subpath)
    store = MappingStore(".")
    m = ModuleMapping(
        module_prefix=module_prefix, repo=repo, local_path=path,
        branch=branch, subpath=subpath, provenance=provenance,
    )
    store.add_module_mapping(m)
    if json_output:
        emit_json(module_mapping_to_dict(m))
    else:
        target = repo if repo else path
        print(f"Added module mapping: {module_prefix} -> {target}")



def map_list_modules_cmd(
    json_output: JsonFlag = False,
):
    """List all module mappings."""
    store = MappingStore(".")
    results = store.list_module_mappings()
    if json_output:
        emit_json([module_mapping_to_dict(m) for m in results])
    elif not results:
        print("No module mappings registered.")
    else:
        for m in results:
            target = m.repo if m.repo else m.local_path
            sub = f" (subpath: {m.subpath})" if m.subpath else ""
            print(f"{m.module_prefix} -> {target}{sub}")



def map_update_module_cmd(
    module_prefix: Annotated[str, typer.Argument(help="Module prefix to update.")],
    repo: Annotated[str, typer.Option("--repo", help="GitHub repo (org/name).")] = "",
    path: Annotated[str, typer.Option("--path", help="Local directory.")] = "",
    branch: Annotated[str, typer.Option("--branch", help="Branch/tag for gh clone.")] = "",
    subpath: Annotated[str, typer.Option("--subpath", help="Subdirectory within repo.")] = "",
    fetch: Annotated[bool, typer.Option("--fetch", help="Force fetch the latest commits from the remote repo.")] = False,
    json_output: JsonFlag = False,
):
    """Update an existing module mapping.

    Examples:
        emend map update-module payments --repo org/payments-v2
        emend map update-module shared.utils --path /new/path
        emend map update-module gateway --branch v2 --subpath src/gw
        emend map update-module payments --fetch
    """
    store = MappingStore(".")
    mm = store.get_module_mapping_by_prefix(module_prefix)
    if mm is None:
        print(f"No module mapping for '{module_prefix}'.", file=sys.stderr)
        raise typer.Exit(1)

    kwargs: dict[str, str] = {}
    if repo:
        kwargs["repo"] = repo
    if path:
        kwargs["local_path"] = path
    if branch:
        kwargs["branch"] = branch
    if subpath:
        kwargs["subpath"] = subpath

    if not kwargs and not fetch:
        print("Nothing to update (provide --repo, --path, --branch, --subpath, or --fetch).", file=sys.stderr)
        raise typer.Exit(1)

    if kwargs:
        store.update_module_mapping(module_prefix, **kwargs)

    if fetch:
        result = store.fetch_module_repo(module_prefix)
        if result is None:
            print(f"Module mapping '{module_prefix}' has no repo to fetch.", file=sys.stderr)

    saved = store.get_module_mapping_by_prefix(module_prefix)
    if json_output:
        emit_json(module_mapping_to_dict(saved))  # type: ignore[arg-type]
    else:
        target = saved.repo if saved.repo else saved.local_path  # type: ignore[union-attr]
        print(f"Updated module mapping '{module_prefix}' -> {target}")



def map_rm_module_cmd(
    prefix: Annotated[str, typer.Argument(help="Module prefix to delete.")],
):
    """Delete a module mapping by prefix name."""
    store = MappingStore(".")
    ok = store.delete_module_mapping_by_prefix(prefix)
    if ok:
        print(f"Deleted module mapping '{prefix}'.")
    else:
        print(f"Module mapping '{prefix}' not found.", file=sys.stderr)
        raise typer.Exit(1)



def map_resolve_cmd(
    selector: Annotated[str, typer.Argument(help="Selector or module to resolve (e.g. 'payments.models.Order' or 'payments::Order').")],
    location: Annotated[bool, typer.Option("--location", "-l", help="Resolve to file path and line number instead of a selector.")] = False,
    json_output: JsonFlag = False,
):
    """Unified resolution for modules and selectors.

    If the input is a module prefix, it resolves to the mapped repo/directory.
    If it's a dotted selector like 'a.b.C', it uses module mappings to find
    where 'a.b' lives, then treats 'C' as a symbol.
    """

    store = MappingStore(".")
    if location:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_path, resolve_through_reexports

        # Resolve to a selector with an explicit file path first.
        file_path = None
        symbol_parts = []

        try:
            sel = parse_extended_selector(selector)
        except LarkError:
            sel = None
        if sel and sel.file_path:
            file_path = sel.file_path
            symbol_parts = sel.symbol_path
        else:
            # Use resolve_selector which handles deep paths and __init__.py re-exports.
            resolved_sel = store.resolve_selector(selector)
            if resolved_sel and "::" in resolved_sel:
                parsed = parse_extended_selector(resolved_sel)
                file_path = parsed.file_path
                symbol_parts = parsed.symbol_path
            elif resolved_sel and os.path.isfile(resolved_sel):
                file_path = resolved_sel
            elif resolved_sel and os.path.isdir(resolved_sel):
                init_py = os.path.join(resolved_sel, "__init__.py")
                if os.path.isfile(init_py):
                    file_path = init_py

        if not file_path or not os.path.isfile(file_path):
            print(f"Could not resolve '{selector}' to a file.", file=sys.stderr)
            raise typer.Exit(1)

        # Follow re-exports if the symbol isn't defined in the file.
        from emend.knowledge import make_resolve_module_cb
        resolve_cb = make_resolve_module_cb(store)

        if symbol_parts:
            res = resolve_through_reexports(file_path, symbol_parts[0], resolve_cb)
            if res:
                file_path, _ = res

        # Now find the symbol in the file to get the line number.
        if symbol_parts:
            symbols = find_nested_definitions(file_path)
            target = find_symbol_by_path(symbols, symbol_parts)
            if target:
                if json_output:
                    emit_json({
                        "file": file_path,
                        "line": target.line_start,
                        "kind": target.kind
                    })
                else:
                    print(f"File: {file_path}")
                    print(f"Line: {target.line_start}")
                    print(f"Kind: {target.kind}")
                return

        # If no symbol parts or symbol not found, just return the file.
        if json_output:
            emit_json({"file": file_path, "line": 1})
        else:
            print(f"File: {file_path}")
            print("Line: 1")
        return

    # First, try resolving as a pure module.
    mm = store.resolve_module(selector)
    if mm and mm.module_prefix == selector:
        # Exact module match
        resolved = store.resolve_module_to_path(selector)
        if json_output:
            d = module_mapping_to_dict(mm)
            if resolved: d["resolved_path"] = resolved
            emit_json(d)
        else:
            print(f"Module '{selector}' -> {mm.repo or mm.local_path}")
            if resolved: print(f"Local path: {resolved}")
        return

    # Use unified resolve_selector logic
    resolved_sel = store.resolve_selector(selector)
    if resolved_sel:
        if json_output:
            # ``resolve_selector`` may return a bare path for a module without
            # a symbol suffix; parsing it as an extended selector would fail.
            if "::" in resolved_sel:
                sel = parse_extended_selector(resolved_sel)
                file_path = sel.file_path
            else:
                file_path = resolved_sel
            emit_json({"selector": resolved_sel, "path": file_path})
        else:
            print(resolved_sel)
        return

    print(f"Could not resolve '{selector}'.", file=sys.stderr)
    raise typer.Exit(1)
