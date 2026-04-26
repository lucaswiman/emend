"""MCP tooling tools: facts_query, mappings, grammar_and_cookbook."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from emend.mcp.dispatch import mcp_app


@mcp_app.tool()
def facts_query(
    project: Annotated[str, Field(description="Project root directory.")] = ".",
    limit: Annotated[int, Field(description="Maximum number of result rows to return.")] = 200,
    fact_type: Annotated[str | None, Field(description="Fact type (symbols, calls, references, trace_flows, types, imports).")] = None,
    name: Annotated[str | None, Field(description="Filter by name.")] = None,
    kind: Annotated[str | None, Field(description="Filter by kind.")] = None,
    file_path: Annotated[str | None, Field(description="Filter by file path.")] = None,
    symbol: Annotated[str | None, Field(description="Symbol qualified name.")] = None,
    label: Annotated[str | None, Field(description="Trace label filter.")] = None,
    transitive: Annotated[bool, Field(description="Compute transitive closure.")] = False,
    max_depth: Annotated[int, Field(description="Max depth for transitive queries.")] = 10,
) -> str:
    """Query the project fact graph via structured parameters."""
    from emend.fact_graph import FactGraph
    import dataclasses

    _fact_type = fact_type or "symbols"
    graph = FactGraph.build_from_project(project)

    if _fact_type == "symbols":
        results = graph.symbols(name=name, kind=kind, file_path=file_path)
        return json.dumps([dataclasses.asdict(f) for f in results[:limit]], indent=2)

    elif _fact_type == "calls":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for call queries."})
        if transitive:
            callers = graph.transitive_callers(symbol, max_depth=max_depth)
            return json.dumps({"symbol": symbol, "transitive_callers": sorted(callers)}, indent=2)
        from_calls = graph.calls_from(symbol)
        to_calls = graph.calls_to(symbol)
        return json.dumps({
            "calls_from": [dataclasses.asdict(c) for c in from_calls[:limit]],
            "calls_to": [dataclasses.asdict(c) for c in to_calls[:limit]],
        }, indent=2)

    elif _fact_type == "references":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for reference queries."})
        refs = graph.references_to(symbol)
        return json.dumps([dataclasses.asdict(r) for r in refs[:limit]], indent=2)

    elif _fact_type == "trace_flows":
        flows = graph.trace_flows(label=label, file_path=file_path)
        return json.dumps([dataclasses.asdict(f) for f in flows[:limit]], indent=2)

    elif _fact_type == "types":
        if not symbol:
            return json.dumps({"error": "Provide 'symbol' parameter for type queries."})
        types = graph.types_for(symbol)
        return json.dumps([dataclasses.asdict(t) for t in types[:limit]], indent=2)

    elif _fact_type == "imports":
        if not file_path:
            return json.dumps({"error": "Provide 'file_path' parameter for import queries."})
        imports = graph.imports_in(file_path)
        return json.dumps([dataclasses.asdict(i) for i in imports[:limit]], indent=2)

    return json.dumps({"error": f"Unknown fact_type: {_fact_type}"})


def map_read(
    kind: Annotated[str, Field(description="What to read: 'mapping' or 'module'.")] = "mapping",
    query: Annotated[str, Field(description="Search query (substring match) or exact identifier/module name. Omit to list all.")] = "",
    options: Annotated[dict | None, Field(description=(
        "Optional filters. "
        "For mappings: {source_project?, target_project?, relationship?, direction?, limit?}. "
        "For modules: no options needed."
    ))] = None,
) -> str:
    """Read from the mapping store."""
    from emend.knowledge import MappingStore, mapping_to_dict, module_mapping_to_dict

    store = MappingStore(".")
    opts = options or {}

    if kind == "mapping":
        if query and " " not in query:
            project = opts.get("project")
            direction = opts.get("direction", "both")
            results = store.find_mappings_for(query, project=project, direction=direction)
            if results:
                return json.dumps([mapping_to_dict(m) for m in results], indent=2)
        source_project = opts.get("source_project")
        target_project = opts.get("target_project")
        relationship = opts.get("relationship")
        limit = opts.get("limit", 50)
        if query:
            results = store.search_mappings(
                query, source_project=source_project,
                target_project=target_project, relationship=relationship, limit=limit,
            )
        else:
            results = store.list_mappings(
                source_project=source_project,
                target_project=target_project, relationship=relationship, limit=limit,
            )
        return json.dumps([mapping_to_dict(m) for m in results], indent=2)

    if kind == "module":
        if query:
            mm = store.resolve_module(query)
            if mm is not None:
                result = module_mapping_to_dict(mm)
                resolved = store.resolve_module_to_path(query)
                if resolved:
                    result["resolved_path"] = resolved
                return json.dumps(result, indent=2)
            return json.dumps({"error": f"No module mapping found for '{query}'."})
        results = store.list_module_mappings()
        return json.dumps([module_mapping_to_dict(m) for m in results], indent=2)

    return json.dumps({"error": f"Unknown kind '{kind}'. Use: mapping, module."})


def map_write(
    kind: Annotated[str, Field(description="Entry type: 'mapping' or 'module'.")],
    op: Annotated[str, Field(description="Operation: 'add' or 'delete'.")],
    entry: Annotated[dict, Field(description=(
        "Entry data. "
        "For mapping+add: {source_project, source_identifier, target_project, target_identifier, "
        "source_kind?, target_kind?, relationship?, confidence?, provenance?, evidence?, metadata?}. "
        "For mapping+delete: {source_identifier, source_project?, target_identifier?}. "
        "For module+add: {module_prefix, repo?, local_path?, branch?, subpath?, provenance?, metadata?}. "
        "For module+delete: {module_prefix}."
    ))],
) -> str:
    """Write to the mapping store: add or delete entries."""
    from emend.knowledge import (
        MappingStore, IdentifierMapping, ModuleMapping,
        mapping_to_dict, module_mapping_to_dict,
    )

    store = MappingStore(".")

    if kind == "mapping":
        if op == "add":
            source_project = entry.get("source_project")
            source_identifier = entry.get("source_identifier")
            target_project = entry.get("target_project")
            target_identifier = entry.get("target_identifier")
            if not source_project or not source_identifier or not target_project or not target_identifier:
                return json.dumps({"error": "source_project, source_identifier, target_project, target_identifier required."})
            m = IdentifierMapping(
                source_project=source_project,
                source_identifier=source_identifier,
                source_kind=entry.get("source_kind") or "",
                target_project=target_project,
                target_identifier=target_identifier,
                target_kind=entry.get("target_kind") or "",
                relationship=entry.get("relationship") or "equivalent",
                confidence=entry.get("confidence") if entry.get("confidence") is not None else 1.0,
                provenance=entry.get("provenance") or "llm",
                evidence=entry.get("evidence") or "",
                metadata=entry.get("metadata") or {},
            )
            store.add_mapping(m)
            return json.dumps(mapping_to_dict(m), indent=2)
        if op == "delete":
            source_identifier = entry.get("source_identifier")
            if not source_identifier:
                return json.dumps({"error": "source_identifier is required for delete."})
            ok = store.delete_mapping(
                source_identifier,
                source_project=entry.get("source_project"),
                target_identifier=entry.get("target_identifier"),
            )
            return json.dumps({"deleted": ok, "source_identifier": source_identifier})

    elif kind == "module":
        if op == "add":
            module_prefix = entry.get("module_prefix")
            if not module_prefix:
                return json.dumps({"error": "module_prefix is required."})
            repo = entry.get("repo")
            local_path = entry.get("local_path")
            if not repo and not local_path:
                return json.dumps({"error": "Either repo or local_path is required."})
            m = ModuleMapping(
                module_prefix=module_prefix,
                repo=repo or "", local_path=local_path or "",
                branch=entry.get("branch") or "", subpath=entry.get("subpath") or "",
                provenance=entry.get("provenance") or "llm",
                metadata=entry.get("metadata") or {},
            )
            store.add_module_mapping(m)
            return json.dumps(module_mapping_to_dict(m), indent=2)
        if op == "delete":
            module_prefix = entry.get("module_prefix")
            if not module_prefix:
                return json.dumps({"error": "module_prefix is required for delete."})
            ok = store.delete_module_mapping_by_prefix(module_prefix)
            return json.dumps({"deleted": ok, "module_prefix": module_prefix})

    else:
        return json.dumps({"error": f"Unknown kind '{kind}'. Use: mapping, module."})

    return json.dumps({"error": f"Unknown op '{op}'. Use: add, delete."})


@mcp_app.tool()
def mappings(
    operation: Annotated[str, Field(description="Mappings operation: read or write.")],
    kind: Annotated[str, Field(description="Mapping kind: mapping or module.")] = "mapping",
    query: Annotated[str, Field(description="Read query string for read operation.")] = "",
    options: Annotated[dict | None, Field(description="Read options for read operation.")] = None,
    op: Annotated[str | None, Field(description="Write operation: add or delete (write operation only).")] = None,
    entry: Annotated[dict | None, Field(description="Write payload (write operation only).")] = None,
) -> str:
    """Read/write mapping state through one discriminated endpoint."""
    normalized = (operation or "").lower()
    if normalized == "read":
        return map_read(kind=kind, query=query, options=options)
    if normalized == "write":
        if not op or entry is None:
            return json.dumps({"error": "write operation requires op and entry"})
        return map_write(kind=kind, op=op, entry=entry)
    return json.dumps({"error": f"Unknown operation {operation!r}. Use: read, write."})


def _parse_rst_sections(text: str) -> dict[str, str]:
    """Parse RST text into a dict mapping section keys to their content."""
    import re as _re

    key_map = {
        "selector_syntax": "selectors",
        "pattern_syntax": "patterns",
        "commands": "commands",
        "cookbook_recipes": "recipes",
        "fact_graph_relations": "facts",
    }

    lines = text.split("\n")
    section_starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            i > 0
            and stripped
            and all(c == "-" for c in stripped)
            and len(stripped) >= 3
        ):
            heading = lines[i - 1].strip()
            if heading:
                section_starts.append((i - 1, heading))

    sections: dict[str, str] = {}
    for idx, (start, heading) in enumerate(section_starts):
        end = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        raw_key = _re.sub(r"\s+", "_", heading.lower())
        key = key_map.get(raw_key, raw_key)
        sections[key] = content

    return sections


_SECTION_SUMMARIES: dict[str, str] = {
    "selectors": "addressing symbols, components, wildcards, file globs",
    "patterns": "metavariables, expressions, statements, replacements",
    "commands": "grep, replace, edit, add, rm, refs, rename, mv, cp, graph, deadcode, lint, batch",
    "recipes": "common refactoring patterns and examples",
    "facts": "CozoDB stored relations and example Datalog queries",
}


@mcp_app.tool()
def grammar_and_cookbook(
    section: Annotated[
        str | None,
        Field(
            description=(
                'Section to retrieve. Pass None (default) to get a table of contents. '
                'Pass "all" for the full document. '
                'Other values: selectors, patterns, commands, recipes, facts.'
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Return the emend grammar reference and cookbook, or a specific section of it.

    Call this tool when you need detailed syntax help for constructing
    selectors, patterns, or command invocations.  The response covers
    selector syntax, pattern metavariables, every command with examples,
    and common refactoring recipes.

    When called without arguments, returns a table of contents listing the
    available sections.  Pass ``section="<name>"`` to retrieve a specific
    section, or ``section="all"`` for the complete document.

    Available section names: selectors, patterns, commands, recipes, facts, all.
    """
    import importlib.resources
    import re as _re

    text = importlib.resources.read_text("emend", "grammar_and_cookbook.rst")

    _grammars = {
        "selector.lark": importlib.resources.read_text("emend.grammars", "selector.lark"),
        "pattern.lark": importlib.resources.read_text("emend.grammars", "pattern.lark"),
    }

    def _inline(m: "_re.Match[str]") -> str:
        path = m.group(1).strip()
        for name, content in _grammars.items():
            if name in path:
                indented = "\n".join("    " + line for line in content.splitlines())
                return f"::\n\n{indented}\n"
        return m.group(0)

    text = _re.sub(
        r"\.\. literalinclude:: ([^\n]+)\n(?:   :[^\n]+\n)*",
        _inline,
        text,
    )

    if section == "all":
        return text

    sections = _parse_rst_sections(text)

    if section is not None:
        if section in sections:
            return sections[section]
        available = ", ".join(sorted(sections.keys()))
        return f"Unknown section {section!r}. Available sections: {available}, all"

    toc_lines = [
        'Available sections (pass section="<name>" to retrieve):\n',
    ]
    ordered_keys = ["selectors", "patterns", "commands", "recipes", "facts"]
    seen: set[str] = set()
    for key in ordered_keys:
        if key in sections:
            summary = _SECTION_SUMMARIES.get(key, "")
            heading_line = sections[key].split("\n")[0]
            entry = f"- {key}: {heading_line} — {summary}" if summary else f"- {key}: {heading_line}"
            toc_lines.append(entry)
            seen.add(key)
    for key, content in sections.items():
        if key not in seen:
            heading_line = content.split("\n")[0]
            summary = _SECTION_SUMMARIES.get(key, "")
            entry = f"- {key}: {heading_line} — {summary}" if summary else f"- {key}: {heading_line}"
            toc_lines.append(entry)
    toc_lines.append("- all: Return the complete reference document")
    return "\n".join(toc_lines)
