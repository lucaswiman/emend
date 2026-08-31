"""AST-based refactoring commands reimplemented using transform primitives."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from emend.component_selector import parse_extended_selector, ExtendedSelector
from emend.transform import copy_symbol


def cmd_copy_to(
    selector: str,
    destination: str,
    append: bool = False,
    dedent: bool = False,
    apply: bool = False,
    project_path: str | None = None,
):
    """Copy a symbol to another file using copy_symbol primitive."""
    from emend.ast_utils import find_nested_definitions, find_symbol_by_line

    ext_selector = parse_extended_selector(selector)

    # Resolve line-based selectors (e.g. file.py:4) to the enclosing symbol
    # so that "copy the symbol at line 4" copies the whole symbol, not just
    # the raw line.
    if ext_selector.line_start is not None:
        symbols = find_nested_definitions(ext_selector.file_path)
        symbol = find_symbol_by_line(symbols, ext_selector.line_start, ext_selector.line_end)
        if not symbol:
            line_desc = (
                f"line {ext_selector.line_start}"
                if ext_selector.line_start == ext_selector.line_end
                else f"lines {ext_selector.line_start}-{ext_selector.line_end}"
            )
            print(f"No symbol found at {line_desc}")
            sys.exit(1)
        ext_selector = ExtendedSelector(
            file_path=ext_selector.file_path,
            symbol_path=symbol.path,
            component=None,
            accessor=None,
        )

    position = "end" if append else "start"
    diff = copy_symbol(
        ext_selector, destination,
        position=position, dedent=dedent, include_imports=True,
        project_path=project_path, apply=apply,
    )

    print(diff, end='')
    if apply:
        print(f"\n✓ Written to {destination}")
    else:
        print("\nRun with --apply to write the file.")


# ---------------------------------------------------------------------------
# list-symbols reimplementation using ScopeResolver
# ---------------------------------------------------------------------------

@dataclass
class TreeSymbol:
    name: str
    kind: str
    signature: str | None
    type_annotation: str | None
    children: list['TreeSymbol']
    depth: int
    line: int | None = None
    end_line: int | None = None
    path: list[str] = field(default_factory=list)


_KIND_KEYWORD = {
    "function": "def",
    "async_function": "async def",
    "method": "def",
    "async_method": "async def",
    "class": "class",
    "variable": "var",
    "reference": "ref",
}


def _print_symbol_tree(symbols: list[TreeSymbol], indent: int = 0, max_depth: int | None = None, current_display_depth: int = 1):
    """Print symbols in tree format with full Python keywords.

    current_display_depth starts at 1 for top-level symbols.
    max_depth is the limit on current_display_depth.
    """
    if max_depth is not None and current_display_depth > max_depth:
        return

    for sym in symbols:
        prefix = "  " * indent
        kind_keyword = _KIND_KEYWORD.get(sym.kind, sym.kind[:3])

        if sym.line and sym.end_line and sym.line != sym.end_line:
            line_suffix = f"  [L{sym.line}-L{sym.end_line}]"
        elif sym.line:
            line_suffix = f"  [L{sym.line}]"
        else:
            line_suffix = ""

        if sym.kind in ("function", "async_function", "method", "async_method"):
            # Ensure signature starts with (
            sig = sym.signature or "()"
            if not sig.startswith("("):
                sig = f"({sig})"
            print(f"{prefix}{kind_keyword} {sym.name}{sig}{line_suffix}")
        elif sym.kind == "class":
            print(f"{prefix}{kind_keyword} {sym.name}{line_suffix}")
        elif sym.kind == "variable":
            ann = f": {sym.type_annotation}" if sym.type_annotation else ""
            print(f"{prefix}{kind_keyword} {sym.name}{ann}{line_suffix}")
        elif sym.kind == "reference":
            print(f"{prefix}{kind_keyword} {sym.name}")

        if sym.children:
            _print_symbol_tree(sym.children, indent + 1, max_depth, current_display_depth + 1)


def _print_symbol_flat(symbols: list[TreeSymbol], parent_path: str = "", max_depth: int | None = None, current_display_depth: int = 1, separator: str = "."):
    """Print symbols in flat format with full paths and full Python keywords."""
    if max_depth is not None and current_display_depth > max_depth:
        return

    for sym in symbols:
        full_path = f"{parent_path}{separator}{sym.name}" if parent_path else sym.name
        kind_keyword = _KIND_KEYWORD.get(sym.kind, sym.kind[:3])

        if sym.line and sym.end_line and sym.line != sym.end_line:
            line_suffix = f"  [L{sym.line}-L{sym.end_line}]"
        elif sym.line:
            line_suffix = f"  [L{sym.line}]"
        else:
            line_suffix = ""

        if sym.kind in ("function", "async_function", "method", "async_method"):
            # Ensure signature starts with (
            sig = sym.signature or "()"
            if not sig.startswith("("):
                sig = f"({sig})"
            print(f"{kind_keyword} {full_path}{sig}{line_suffix}")
        elif sym.kind == "class":
            print(f"{kind_keyword} {full_path}{line_suffix}")
        # Skip variables and references in flat mode

        _print_symbol_flat(sym.children, full_path, max_depth, current_display_depth + 1, separator=separator)


def dicts_to_tree_symbols(dicts: list[dict], module_path: str, separator: str = ".") -> list[TreeSymbol]:
    """Build a TreeSymbol hierarchy from flat or nested definitions."""
    root_symbols = []
    symbol_map = {} # path_tuple -> TreeSymbol

    def flatten(definitions: list[dict]):
        for definition in definitions:
            yield definition
            yield from flatten(definition.get("children", []))

    mod_parts = tuple(module_path.split(separator))

    # First pass: create all symbols
    for d in flatten(dicts):
        full_path = tuple(d.get("path", [d["name"]]))
        
        # Strip module path from the beginning if it matches
        if full_path[:len(mod_parts)] == mod_parts:
            path = full_path[len(mod_parts):]
        else:
            path = full_path
            
        if not path:
            continue
            
        sym = TreeSymbol(
            name=path[-1],
            kind=d["kind"],
            signature=d.get("signature"),
            type_annotation=d.get("type_annotation"),
            children=[],
            depth=len(path) - 1,
            line=d.get("line") or None,
            end_line=d.get("end_line") or None,
            path=list(path),
        )
        symbol_map[path] = sym

    # Second pass: build parent-child links
    # Sort paths by length so parents are processed or at least we know where children go
    sorted_paths = sorted(symbol_map.keys(), key=len)
    
    for path in sorted_paths:
        sym = symbol_map[path]
        if len(path) == 1:
            root_symbols.append(sym)
        else:
            parent_path = path[:-1]
            if parent_path in symbol_map:
                symbol_map[parent_path].children.append(sym)
            else:
                # Parent not in definitions (e.g. from an import or outside module)
                root_symbols.append(sym)

    return root_symbols


def derive_module_path(
    file: str | Path,
    project_root: str | Path,
    language: str = "python",
) -> str:
    """Derive the qualified module path used by collected symbol paths.

    ``PyScopeResolver`` reports paths rooted at the project/module name.  Keep
    the filesystem-to-qualified-name conversion in one place so CLI and MCP
    summary output agree across languages and source layouts.
    """
    from emend.language_registry import get_module_separator

    file_path = Path(file)
    root = Path(project_root)
    try:
        relative = file_path.resolve().relative_to(root.resolve())
    except ValueError:
        return file_path.stem

    parts = list(relative.parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    if parts:
        parts[-1] = file_path.stem

    # These are conventional package entry files, not syntax parsing rules.
    # Strip them so symbols are displayed below the package/module name.
    if parts and parts[-1] in {"__init__", "index", "lib", "mod"}:
        parts.pop()

    return get_module_separator(language).join(parts) or file_path.stem


def collect_symbols(
    file: str,
    tree_depth: int | None = None,
    selector: Optional[str] = None,
) -> list[TreeSymbol]:
    """Collect symbols from a file using the unified PyScopeResolver."""
    from emend import emend_core
    from pathlib import Path
    from emend.language_registry import detect_language, get_module_separator

    ext = Path(file).suffix.lstrip('.')
    language = detect_language(file) or "python"
    sep = get_module_separator(language)

    # Initialize resolver for the file's project root
    resolver = emend_core.PyScopeResolver(str(Path(file).parent), extension=ext)

    # Read and index the file
    source = Path(file).read_text()
    resolver.index_file(file, source)

    # Get symbols from the unified resolver
    result_dicts = resolver.get_symbols(file)

    if result_dicts:
        # Get module path to correctly strip it from symbol paths
        from emend.transform import _find_source_root
        root = _find_source_root(Path(file).parent, language=language)

        module_path = derive_module_path(file, root, language)

        symbols = dicts_to_tree_symbols(result_dicts, module_path, separator=sep)
        
        # Filter by selector if provided
        if selector:
            selector_parts = selector.split('.')
            
            def find_selected(syms, target_parts):
                result = []
                for s in syms:
                    if s.name == target_parts[0]:
                        if len(target_parts) == 1:
                            # Found the target, include it and all its children
                            result.append(s)
                        else:
                            # Recurse into children to find the next part
                            selected_children = find_selected(s.children, target_parts[1:])
                            if selected_children:
                                # Keep this ancestor but only with the selected children
                                s.children = selected_children
                                result.append(s)
                return result
            
            symbols = find_selected(symbols, selector_parts)

        return symbols
    return []
