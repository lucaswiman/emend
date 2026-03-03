"""AST-based refactoring commands reimplemented using transform primitives."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from emend.component_selector import parse_extended_selector, ExtendedSelector
from emend.transform import (
    get_component, set_component, add_to_component,
    remove_symbol, copy_symbol, replace_pattern
)
from emend.ast_utils import (
    find_nested_definitions,
    find_symbol_by_path,
    find_symbol_by_line,
)


def cmd_copy_to(
    selector: str,
    destination: str,
    append: bool = False,
    dedent: bool = False,
    apply: bool = False,
):
    """Copy a symbol to another file using copy_symbol primitive."""
    from emend.component_selector import ExtendedSelector, parse_extended_selector
    from emend.transform import copy_symbol

    ext_selector = parse_extended_selector(selector)

    # Handle line-based selectors
    if ext_selector.line_start is not None:
        from emend.ast_utils import get_symbol_source as get_symbol_source_ast

        symbols = find_nested_definitions(ext_selector.file_path)
        symbol = find_symbol_by_line(symbols, ext_selector.line_start, ext_selector.line_end)
        if not symbol:
            line_desc = f"line {ext_selector.line_start}" if ext_selector.line_start == ext_selector.line_end else f"lines {ext_selector.line_start}-{ext_selector.line_end}"
            print(f"No symbol found at {line_desc}")
            sys.exit(1)

        print(f"\nCopying {'.'.join(symbol.path)} to {destination}")
        print("-" * 50)
        print(f"Source: {ext_selector.file_path} lines {symbol.line_start}-{symbol.line_end}")
        print(f"Append: {append}")
        if dedent:
            print(f"Dedent: {dedent}")

        source = get_symbol_source_ast(ext_selector.file_path, symbol, dedent=dedent)

        dest_path = Path(destination)
        if dest_path.exists():
            dest_content = dest_path.read_text()
        else:
            dest_content = ""

        if append:
            if dest_content:
                new_content = dest_content.rstrip() + "\n\n\n" + source
            else:
                new_content = source
        else:
            if dest_content:
                new_content = source + "\n\n" + dest_content
            else:
                new_content = source

        import difflib
        diff_lines = list(difflib.unified_diff(
            dest_content.splitlines(keepends=True) if dest_content else [],
            new_content.splitlines(keepends=True),
            fromfile=destination,
            tofile=destination
        ))
        diff = ''.join(diff_lines)

        if diff:
            print("\nPreview:")
            print(diff, end='')

        if apply:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(new_content)
            print(f"\n✓ Written to {destination}")
        else:
            print("\nRun with --apply to write the file.")
        return

    # Use the new copy_symbol primitive for symbol-based selectors
    position = "end" if append else "start"
    diff = copy_symbol(ext_selector, destination, position=position, dedent=dedent, include_imports=True, apply=apply)

    print(diff, end='')
    if apply:
        print(f"\n✓ Written to {destination}")
    else:
        print("\nRun with --apply to write the file.")


# ---------------------------------------------------------------------------
# list-symbols reimplementation using LibCST
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


def _print_symbol_tree(symbols: list[TreeSymbol], indent: int = 0):
    """Print symbols in tree format with full Python keywords."""
    KIND_KEYWORD = {
        "function": "def",
        "async_function": "async def",
        "method": "def",
        "async_method": "async def",
        "class": "class",
        "variable": "var",
        "reference": "ref",
    }
    for sym in symbols:
        prefix = "  " * indent
        kind_keyword = KIND_KEYWORD.get(sym.kind, sym.kind[:3])

        if sym.line and sym.end_line and sym.line != sym.end_line:
            line_suffix = f"  [L{sym.line}-L{sym.end_line}]"
        elif sym.line:
            line_suffix = f"  [L{sym.line}]"
        else:
            line_suffix = ""

        if sym.kind in ("function", "async_function", "method", "async_method"):
            print(f"{prefix}{kind_keyword} {sym.name}{sym.signature or '()'}{line_suffix}")
        elif sym.kind == "class":
            print(f"{prefix}{kind_keyword} {sym.name}{line_suffix}")
        elif sym.kind == "variable":
            ann = f": {sym.type_annotation}" if sym.type_annotation else ""
            print(f"{prefix}{kind_keyword} {sym.name}{ann}{line_suffix}")
        elif sym.kind == "reference":
            print(f"{prefix}{kind_keyword} {sym.name}")

        if sym.children:
            _print_symbol_tree(sym.children, indent + 1)


def _print_symbol_flat(symbols: list[TreeSymbol], parent_path: str = ""):
    """Print symbols in flat format with full paths and full Python keywords."""
    KIND_KEYWORD = {
        "function": "def",
        "async_function": "async def",
        "method": "def",
        "async_method": "async def",
        "class": "class",
        "variable": "var",
        "reference": "ref",
    }
    for sym in symbols:
        full_path = f"{parent_path}.{sym.name}" if parent_path else sym.name
        kind_keyword = KIND_KEYWORD.get(sym.kind, sym.kind[:3])

        if sym.line and sym.end_line and sym.line != sym.end_line:
            line_suffix = f"  [L{sym.line}-L{sym.end_line}]"
        elif sym.line:
            line_suffix = f"  [L{sym.line}]"
        else:
            line_suffix = ""

        if sym.kind in ("function", "async_function", "method", "async_method"):
            print(f"{kind_keyword} {full_path}{sym.signature or '()'}{line_suffix}")
        elif sym.kind == "class":
            print(f"{kind_keyword} {full_path}{line_suffix}")
        # Skip variables and references in flat mode

        _print_symbol_flat(sym.children, full_path)


def dicts_to_tree_symbols(dicts: list[dict]) -> list[TreeSymbol]:
    """Convert Rust collect_symbols_batch dicts to TreeSymbol objects."""
    return [
        TreeSymbol(
            name=d["name"],
            kind=d["kind"],
            signature=d.get("signature"),
            type_annotation=d.get("type_annotation"),
            children=dicts_to_tree_symbols(d.get("children", [])),
            depth=d["depth"],
            line=d.get("line") or None,
            end_line=d.get("end_line") or None,
            path=d.get("path", []),
        )
        for d in dicts
    ]


def collect_symbols(
    file: str,
    tree_depth: int | None = None,
    selector: Optional[str] = None,
) -> list[TreeSymbol]:
    """Collect symbols from a file using the bundled Rust extension."""
    from emend import emend_core
    result_dicts = emend_core.collect_symbols_batch(
        [file], max_depth=tree_depth, selector=selector,
    )
    if result_dicts:
        return dicts_to_tree_symbols(result_dicts[0][1])
    return []
