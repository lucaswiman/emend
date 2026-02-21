"""AST-based refactoring commands reimplemented using transform primitives."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import libcst as cst
from libcst.metadata import PositionProvider, MetadataWrapper

from emend.component_selector import parse_extended_selector, ExtendedSelector
from emend.transform import (
    get_component, set_component, add_to_component,
    remove_symbol, copy_symbol, replace_pattern
)
from emend.ast_utils import (
    find_nested_definitions,
    find_symbol_by_path,
    find_symbol_by_line,
    get_byte_offset,
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


def _cst_format_param(param: cst.Param, prefix: str = "") -> str:
    """Format a single parameter with optional annotation and default."""
    p = prefix + param.name.value
    if param.annotation is not None:
        ann = cst.Module([]).code_for_node(param.annotation.annotation).strip()
        p += f": {ann}"
    if param.default is not None:
        default = cst.Module([]).code_for_node(param.default).strip()
        p += f" = {default}"
    return p


def _cst_get_function_signature(node: cst.FunctionDef) -> str:
    """Build function signature string with type annotations from LibCST FunctionDef."""
    params = node.params
    parts: list[str] = []

    # 1. Positional-only params (before /)
    if hasattr(params, 'posonly_params') and params.posonly_params:
        for p in params.posonly_params:
            parts.append(_cst_format_param(p))
        parts.append("/")

    # 2. Regular positional-or-keyword params
    for p in params.params:
        parts.append(_cst_format_param(p))

    # 3. *args or bare * separator
    if params.star_arg is not None:
        if isinstance(params.star_arg, cst.Param):
            parts.append(_cst_format_param(params.star_arg, prefix="*"))
        elif isinstance(params.star_arg, cst.ParamStar):
            parts.append("*")
    elif params.kwonly_params:
        parts.append("*")

    # 4. Keyword-only params
    for p in params.kwonly_params:
        parts.append(_cst_format_param(p))

    # 5. **kwargs
    if params.star_kwarg:
        parts.append(_cst_format_param(params.star_kwarg, prefix="**"))

    sig = f"({', '.join(parts)})"
    if node.returns is not None:
        ret = cst.Module([]).code_for_node(node.returns.annotation).strip()
        sig += f" -> {ret}"
    return sig


class _NameLoadCollector(cst.CSTVisitor):
    """Collect all Name nodes that are used as reads (not in assignment targets)."""

    def __init__(self):
        self.referenced_names: set[str] = set()
        self._in_assign_target = False

    def visit_Assign(self, node: cst.Assign) -> bool:
        return True

    def visit_AssignTarget(self, node: cst.AssignTarget) -> bool:
        self._in_assign_target = True
        return True

    def leave_AssignTarget(self, node: cst.AssignTarget) -> None:
        self._in_assign_target = False

    def visit_AnnAssign(self, node: cst.AnnAssign) -> bool:
        return True

    def visit_Name(self, node: cst.Name) -> None:
        if not self._in_assign_target:
            self.referenced_names.add(node.value)


class _ListSymbolsVisitor(cst.CSTVisitor):
    """LibCST visitor to collect symbols for list-symbols output."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(
        self,
        max_depth: float,
        selector_path: list[str] | None,
    ):
        self.max_depth = max_depth
        self.selector_path = selector_path
        self.symbols: list[TreeSymbol] = []
        self._path: list[str] = []
        self._depth: int = 0
        self._symbol_stack: list[list[TreeSymbol]] = [self.symbols]
        self._defined_names_stack: list[set[str]] = [set()]
        self._current_func_nodes: list[cst.FunctionDef | None] = [None]

    def _matches_selector(self, current_path: list[str]) -> bool:
        if self.selector_path is None:
            return True
        if len(current_path) < len(self.selector_path):
            return self.selector_path[:len(current_path)] == current_path
        return current_path[:len(self.selector_path)] == self.selector_path

    def _get_position(self, node: cst.CSTNode) -> tuple[int, int]:
        pos = self.get_metadata(PositionProvider, node)
        return pos.start.line, pos.end.line

    def _add_outer_references(self, func_node: cst.FunctionDef, sym: TreeSymbol):
        """Find references to outer-scope variables within a function body."""
        if self._depth + 1 >= self.max_depth:
            return

        collector = _NameLoadCollector()
        # Visit the function body to find referenced names
        # We need to wrap it in a dummy module for visiting
        try:
            func_node.body.visit(collector)
        except Exception:
            return

        referenced_names = collector.referenced_names

        # Check which ones are defined in outer scopes
        seen = set()
        for outer_scope in self._defined_names_stack:
            for name in referenced_names:
                if name in outer_scope and name not in seen:
                    if not any(c.name == name and c.kind == "reference" for c in sym.children):
                        ref = TreeSymbol(
                            name=name,
                            kind="reference",
                            signature=None,
                            type_annotation=None,
                            children=[],
                            depth=self._depth + 1,
                        )
                        sym.children.append(ref)
                    seen.add(name)

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        name = node.name.value
        current_path = self._path + [name]

        if self._depth < self.max_depth and self._matches_selector(current_path):
            line, end_line = self._get_position(node)
            is_async = node.asynchronous is not None
            kind = "async_function" if is_async else "function"
            sig = _cst_get_function_signature(node)
            sym = TreeSymbol(
                name=name,
                kind=kind,
                signature=sig,
                type_annotation=None,
                children=[],
                depth=self._depth,
                line=line,
                end_line=end_line,
                path=current_path,
            )

            self._symbol_stack[-1].append(sym)

            # Push state
            self._path = current_path
            self._depth += 1
            self._symbol_stack.append(sym.children)
            child_defined = set()
            self._defined_names_stack.append(child_defined)
            self._current_func_nodes.append(node)
            return True
        else:
            if self._defined_names_stack:
                self._defined_names_stack[-1].add(name)
            # Don't traverse children when not matching
            return False

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        name = node.name.value

        # Only pop state if we actually pushed (i.e., we entered in visit_FunctionDef)
        if self._path and self._path[-1] == name:
            func_node = self._current_func_nodes.pop()
            self._defined_names_stack.pop()
            children_list = self._symbol_stack.pop()
            self._depth -= 1
            self._path = self._path[:-1]

            # Now add outer references
            if func_node is not None:
                # Find the TreeSymbol we created
                parent_children = self._symbol_stack[-1]
                for sym in parent_children:
                    if sym.name == name and sym.kind in ("function", "async_function"):
                        self._add_outer_references(func_node, sym)
                        break

            # Register name in parent scope
            if self._defined_names_stack:
                self._defined_names_stack[-1].add(name)

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        name = node.name.value
        current_path = self._path + [name]

        if self._depth < self.max_depth and self._matches_selector(current_path):
            line, end_line = self._get_position(node)
            sym = TreeSymbol(
                name=name,
                kind="class",
                signature=None,
                type_annotation=None,
                children=[],
                depth=self._depth,
                line=line,
                end_line=end_line,
                path=current_path,
            )

            self._symbol_stack[-1].append(sym)

            # Push state
            self._path = current_path
            self._depth += 1
            self._symbol_stack.append(sym.children)
            child_defined = set()
            self._defined_names_stack.append(child_defined)
            self._current_func_nodes.append(None)
            return True
        else:
            if self._defined_names_stack:
                self._defined_names_stack[-1].add(name)
            return False

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        name = node.name.value
        if self._path and self._path[-1] == name:
            self._current_func_nodes.pop()
            self._defined_names_stack.pop()
            self._symbol_stack.pop()
            self._depth -= 1
            self._path = self._path[:-1]

            if self._defined_names_stack:
                self._defined_names_stack[-1].add(name)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> bool:
        if not isinstance(node.target, cst.Name):
            return False

        name = node.target.value
        current_path = self._path + [name]

        if self._depth < self.max_depth and self._matches_selector(current_path):
            line, end_line = self._get_position(node)
            type_str = cst.Module([]).code_for_node(node.annotation.annotation).strip()
            sym = TreeSymbol(
                name=name,
                kind="variable",
                signature=None,
                type_annotation=type_str,
                children=[],
                depth=self._depth,
                line=line,
                end_line=end_line,
                path=current_path,
            )
            self._symbol_stack[-1].append(sym)

        if self._defined_names_stack:
            self._defined_names_stack[-1].add(name)
        return False

    def visit_Assign(self, node: cst.Assign) -> bool:
        for target in node.targets:
            if isinstance(target.target, cst.Name):
                name = target.target.value
                current_path = self._path + [name]

                if self._depth < self.max_depth and self._matches_selector(current_path):
                    line, end_line = self._get_position(node)
                    sym = TreeSymbol(
                        name=name,
                        kind="variable",
                        signature=None,
                        type_annotation=None,
                        children=[],
                        depth=self._depth,
                        line=line,
                        end_line=end_line,
                        path=current_path,
                    )
                    self._symbol_stack[-1].append(sym)

                if self._defined_names_stack:
                    self._defined_names_stack[-1].add(name)
        return False


def _print_symbol_tree(symbols: list[TreeSymbol], indent: int = 0):
    """Print symbols in tree format with full Python keywords."""
    KIND_KEYWORD = {
        "function": "def",
        "async_function": "async def",
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

        if sym.kind in ("function", "async_function"):
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

        if sym.kind in ("function", "async_function"):
            print(f"{kind_keyword} {full_path}{sym.signature or '()'}{line_suffix}")
        elif sym.kind == "class":
            print(f"{kind_keyword} {full_path}{line_suffix}")
        # Skip variables and references in flat mode

        _print_symbol_flat(sym.children, full_path)


def cmd_list_symbols(
    file: str,
    project: Optional[str] = None,
    tree_depth: int | None = None,
    flat: bool = False,
    selector: Optional[str] = None,
):
    """List symbols in a module using LibCST."""
    file_path = Path(file)
    code = file_path.read_text()

    max_depth = tree_depth if tree_depth is not None else float('inf')
    selector_path = selector.split(".") if selector else None

    module = cst.parse_module(code)
    wrapper = MetadataWrapper(module)
    visitor = _ListSymbolsVisitor(max_depth=max_depth, selector_path=selector_path)
    wrapper.visit(visitor)
    symbols = visitor.symbols

    print(f"\nModule: {file}")
    if symbols:
        if flat:
            _print_symbol_flat(symbols)
        else:
            _print_symbol_tree(symbols, indent=1)
