"""AST-based refactoring commands reimplemented using transform primitives."""

import sys
from dataclasses import dataclass, field, replace
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


def _line_suffix(sym: TreeSymbol) -> str:
    """Format a symbol's optional source location."""
    if not sym.line:
        return ""
    if sym.end_line and sym.line != sym.end_line:
        return f"  [L{sym.line}-L{sym.end_line}]"
    return f"  [L{sym.line}]"


def _format_symbol(sym: TreeSymbol, display_name: str, *, include_variables: bool) -> str | None:
    """Format one symbol, returning ``None`` for omitted flat-mode entries."""
    keyword = _KIND_KEYWORD.get(sym.kind, sym.kind[:3])
    suffix = _line_suffix(sym)
    if sym.kind in ("function", "async_function", "method", "async_method"):
        signature = sym.signature or "()"
        if not signature.startswith("("):
            signature = f"({signature})"
        return f"{keyword} {display_name}{signature}{suffix}"
    if sym.kind == "class":
        return f"{keyword} {display_name}{suffix}"
    if include_variables and sym.kind == "variable":
        annotation = f": {sym.type_annotation}" if sym.type_annotation else ""
        return f"{keyword} {display_name}{annotation}{suffix}"
    if include_variables and sym.kind == "reference":
        return f"{keyword} {display_name}"
    return None


def _print_symbol_tree(symbols: list[TreeSymbol], indent: int = 0, max_depth: int | None = None, current_display_depth: int = 1):
    """Print symbols in tree format with full Python keywords.

    current_display_depth starts at 1 for top-level symbols.
    max_depth is the limit on current_display_depth.
    """
    if max_depth is not None and current_display_depth > max_depth:
        return

    for sym in symbols:
        prefix = "  " * indent
        formatted = _format_symbol(sym, sym.name, include_variables=True)
        if formatted:
            print(prefix + formatted)

        if sym.children:
            _print_symbol_tree(sym.children, indent + 1, max_depth, current_display_depth + 1)


def _print_symbol_flat(symbols: list[TreeSymbol], parent_path: str = "", max_depth: int | None = None, current_display_depth: int = 1, separator: str = "."):
    """Print symbols in flat format with full paths and full Python keywords."""
    if max_depth is not None and current_display_depth > max_depth:
        return

    for sym in symbols:
        full_path = f"{parent_path}{separator}{sym.name}" if parent_path else sym.name
        formatted = _format_symbol(sym, full_path, include_variables=False)
        if formatted:
            print(formatted)

        _print_symbol_flat(sym.children, full_path, max_depth, current_display_depth + 1, separator=separator)


def dicts_to_tree_symbols(dicts: list[dict], module_path: str, separator: str = ".") -> list[TreeSymbol]:
    """Build a consumer-owned tree from the shared immutable projection."""
    from emend.symbol_projection import symbol_hierarchy

    def view(symbol):
        return TreeSymbol(
            symbol.name, symbol.kind, symbol.signature, symbol.type_annotation,
            [view(child) for child in symbol.children], len(symbol.path) - 1,
            symbol.line or None, symbol.end_line or None, list(symbol.path),
        )

    return [view(symbol) for symbol in symbol_hierarchy(dicts, module_path, separator)]


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
                for symbol in syms:
                    if symbol.name != target_parts[0]:
                        continue
                    if len(target_parts) == 1:
                        result.append(symbol)
                    elif children := find_selected(symbol.children, target_parts[1:]):
                        result.append(replace(symbol, children=children))
                return result
            
            symbols = find_selected(symbols, selector_parts)

        return symbols
    return []
