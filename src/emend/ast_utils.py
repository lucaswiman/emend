"""AST utilities for traversing and finding symbols in Python code."""

import fnmatch

import libcst as cst
from libcst.metadata import PositionProvider, MetadataWrapper

from emend.component_selector import NestedSymbol


class _NestedDefinitionVisitor(cst.CSTVisitor):
    """LibCST visitor to find all class/function definitions with nesting info."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, max_depth: int | None = None):
        self.max_depth = max_depth
        self.symbols: list[NestedSymbol] = []
        self._path: list[str] = []
        self._depth: int = 0
        self._symbol_stack: list[list[NestedSymbol]] = [self.symbols]
        # Track whether we're directly inside a class body
        self._in_class_stack: list[bool] = [False]

    def _get_position(self, node: cst.CSTNode) -> tuple[int, int, int]:
        """Get (line_start, line_end, col_offset) for a node."""
        pos = self.get_metadata(PositionProvider, node)
        return pos.start.line, pos.end.line, pos.start.column

    def _get_decorator_strings(self, decorators: tuple[cst.Decorator, ...]) -> list[str]:
        """Extract decorator strings (without @)."""
        result = []
        for dec in decorators:
            code = cst.Module([]).code_for_node(dec.decorator).strip()
            result.append(code)
        return result

    def _get_first_decorator_line(self, decorators: tuple[cst.Decorator, ...]) -> int | None:
        """Get line number of first decorator."""
        if not decorators:
            return None
        first_pos = self.get_metadata(PositionProvider, decorators[0])
        return first_pos.start.line

    def _get_parameters(self, params: cst.Parameters) -> list[str]:
        """Extract parameter names with proper separators."""
        result = []
        # Positional-only args (before /)
        if hasattr(params, 'posonly_params'):
            for p in params.posonly_params:
                result.append(p.name.value)
            if params.posonly_params:
                result.append('/')
        # Regular positional args
        for p in params.params:
            result.append(p.name.value)
        # *args or bare * (if kwonlyargs exist)
        if params.star_arg is not None:
            if isinstance(params.star_arg, cst.Param):
                result.append(f"*{params.star_arg.name.value}")
            elif isinstance(params.star_arg, cst.ParamStar):
                result.append('*')  # Bare * separator
        elif params.kwonly_params:
            result.append('*')
        # Keyword-only args
        for p in params.kwonly_params:
            result.append(p.name.value)
        # **kwargs
        if params.star_kwarg:
            result.append(f"**{params.star_kwarg.name.value}")
        return result

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if self.max_depth is not None and self._depth > self.max_depth:
            return False

        name = node.name.value
        new_path = self._path + [name]
        line_start, line_end, col_offset = self._get_position(node)

        symbol = NestedSymbol(
            name=name,
            kind='class',
            line_start=line_start,
            line_end=line_end,
            col_offset=col_offset,
            path=new_path,
            decorators=self._get_decorator_strings(node.decorators),
            decorator_line_start=self._get_first_decorator_line(node.decorators),
        )
        self._symbol_stack[-1].append(symbol)

        # Push state for children
        self._path = new_path
        self._depth += 1
        self._symbol_stack.append(symbol.children)
        self._in_class_stack.append(True)
        return True

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        name = node.name.value
        if self._path and self._path[-1] == name:
            self._path = self._path[:-1]
            self._depth -= 1
            self._symbol_stack.pop()
            self._in_class_stack.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if self.max_depth is not None and self._depth > self.max_depth:
            return False

        name = node.name.value
        new_path = self._path + [name]
        line_start, line_end, col_offset = self._get_position(node)

        is_async = node.asynchronous is not None
        is_method = self._in_class_stack[-1]

        if is_async:
            kind = 'async_method' if is_method else 'async_function'
        else:
            kind = 'method' if is_method else 'function'

        symbol = NestedSymbol(
            name=name,
            kind=kind,
            line_start=line_start,
            line_end=line_end,
            col_offset=col_offset,
            path=new_path,
            decorators=self._get_decorator_strings(node.decorators),
            decorator_line_start=self._get_first_decorator_line(node.decorators),
            parameters=self._get_parameters(node.params),
        )
        self._symbol_stack[-1].append(symbol)

        # Push state for children (functions inside functions are NOT methods)
        self._path = new_path
        self._depth += 1
        self._symbol_stack.append(symbol.children)
        self._in_class_stack.append(False)
        return True

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        name = node.name.value
        if self._path and self._path[-1] == name:
            self._path = self._path[:-1]
            self._depth -= 1
            self._symbol_stack.pop()
            self._in_class_stack.pop()


def find_nested_definitions(filepath: str, max_depth: int | None = None) -> list[NestedSymbol]:
    """Walk CST and find all class/function definitions with nesting info."""
    with open(filepath) as f:
        source = f.read()

    module = cst.parse_module(source)
    wrapper = MetadataWrapper(module)
    visitor = _NestedDefinitionVisitor(max_depth=max_depth)
    wrapper.visit(visitor)
    return visitor.symbols


def find_symbol_by_path(symbols: list[NestedSymbol], path: list[str]) -> NestedSymbol | None:
    """Find a symbol by its path (e.g., ['MyClass', '_build', 'nested_func'])."""
    if not path:
        return None

    for sym in symbols:
        if sym.name == path[0]:
            if len(path) == 1:
                return sym
            return find_symbol_by_path(sym.children, path[1:])
    return None


def expand_wildcard_path(symbols: list[NestedSymbol], path: list[str]) -> list[NestedSymbol]:
    """Expand wildcard patterns in a symbol path (e.g., ['MyClass', '*', 'handle_*']).

    Returns all symbols matching the pattern.
    """
    if not path:
        return []

    def match_at_level(syms: list[NestedSymbol], remaining_path: list[str]) -> list[NestedSymbol]:
        if not remaining_path:
            return []

        pattern = remaining_path[0]
        matches = []

        for sym in syms:
            if fnmatch.fnmatch(sym.name, pattern):
                if len(remaining_path) == 1:
                    # Last segment - return this symbol
                    matches.append(sym)
                else:
                    # Recurse into children
                    matches.extend(match_at_level(sym.children, remaining_path[1:]))

        return matches

    return match_at_level(symbols, path)


def find_symbol_by_line(symbols: list[NestedSymbol], line: int, line_end: int | None = None) -> NestedSymbol | None:
    """Find the innermost symbol that contains the given line or line range.

    Args:
        symbols: List of symbols to search
        line: Line number to search for
        line_end: Optional end line for range search (defaults to line)

    Returns:
        The innermost symbol containing the line(s), or None if not found
    """
    if line_end is None:
        line_end = line

    def find_innermost(syms: list[NestedSymbol]) -> NestedSymbol | None:
        best_match = None
        for sym in syms:
            if sym.line_start <= line <= sym.line_end and sym.line_start <= line_end <= sym.line_end:
                best_match = sym
                child_match = find_innermost(sym.children)
                if child_match:
                    best_match = child_match
        return best_match

    return find_innermost(symbols)


def get_symbol_source(filepath: str, symbol: NestedSymbol, dedent: bool = False) -> str:
    """Extract the source code for a symbol, optionally dedenting."""
    with open(filepath) as f:
        lines = f.readlines()

    # Include decorator lines if present
    start_line = symbol.line_start
    if symbol.decorator_line_start is not None:
        start_line = symbol.decorator_line_start

    source_lines = lines[start_line - 1:symbol.line_end]
    source = ''.join(source_lines)

    if dedent:
        # Find minimum indentation
        non_empty_lines = [l for l in source_lines if l.strip()]
        if non_empty_lines:
            min_indent = min(len(l) - len(l.lstrip()) for l in non_empty_lines)
            dedented_lines = []
            for line in source_lines:
                if line.strip():
                    dedented_lines.append(line[min_indent:])
                else:
                    dedented_lines.append('\n')
            source = ''.join(dedented_lines)

    return source
