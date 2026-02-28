"""Query command for finding symbols with filters.

This module provides a filter-based search for Python symbols,
designed for AI agent refactoring workflows.
"""

import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.metadata import PositionProvider, MetadataWrapper


@dataclass
class SymbolInfo:
    """Information about a discovered symbol."""

    path: str  # Full selector path like "file.py::Class.method"
    name: str
    kind: str  # 'class', 'function', 'async_function', 'method', 'async_method'
    line: int
    end_line: int
    decorators: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    returns: str | None = None
    parent: str | None = None
    depth: int = 1

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "line": self.line,
            "end_line": self.end_line,
        }
        if self.decorators:
            result["decorators"] = self.decorators
        if self.parameters:
            result["parameters"] = self.parameters
        if self.returns:
            result["returns"] = self.returns
        if self.parent:
            result["parent"] = self.parent
        return result


@dataclass
class QueryFilter:
    """Parsed query filters."""

    kinds: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    returns_patterns: list[str] = field(default_factory=list)
    in_classes: list[str] = field(default_factory=list)
    depths: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    case_insensitive: bool = False
    smart_case: bool = False


def _normalize_to_words(name: str) -> list[str]:
    """Normalize a name to word boundaries.

    Examples:
        process_request -> ['process', 'request']
        processRequest -> ['process', 'request']
        ProcessRequest -> ['process', 'request']
        get_http_response -> ['get', 'http', 'response']
    """
    # First split on underscores (snake_case)
    parts = re.split(r'_+', name)

    words = []
    for part in parts:
        # Then split each part on camelCase boundaries
        subparts = re.split(r'(?<=[a-z])(?=[A-Z])', part)
        words.extend(subparts)

    return [w.lower() for w in words if w]


def _smart_case_pattern(pattern: str) -> str:
    """Convert a name pattern to match all naming variants."""
    words = _normalize_to_words(pattern)
    if len(words) == 0:
        return pattern
    if len(words) == 1:
        # Single word - just case-insensitive match
        return pattern

    # Generate variants
    snake = '_'.join(words)                           # word_one_two
    camel = words[0] + ''.join(w.capitalize() for w in words[1:])  # wordOneTwo
    pascal = ''.join(w.capitalize() for w in words)   # WordOneTwo

    return f'({snake}|{camel}|{pascal})'


def _parse_pattern(pattern: str, case_insensitive: bool = False) -> tuple[str, bool]:
    """Parse a pattern into (regex_pattern, is_regex).

    Handles:
    - /regex/ - explicit regex
    - glob patterns with * and ? - converted to regex
    """
    # Check for regex pattern (slash-delimited)
    if pattern.startswith("/") and pattern.endswith("/"):
        regex = pattern[1:-1]
        return regex, True

    # Convert glob to regex
    # Escape regex special chars except * and ?
    regex = ""
    for char in pattern:
        if char == "*":
            regex += ".*"
        elif char == "?":
            regex += "."
        elif char in r"\.^$+{}[]|()":
            regex += "\\" + char
        else:
            regex += char

    return f"^{regex}$", False


def _match_pattern(
    value: str, pattern: str, case_insensitive: bool = False, smart_case: bool = False
) -> bool:
    """Match a value against a pattern (glob or regex)."""
    # Apply smart-case transformation if enabled
    if smart_case:
        # Don't transform if it's a regex or contains wildcards
        if not (pattern.startswith("/") and pattern.endswith("/")) and "*" not in pattern and "?" not in pattern:
            smart_pattern = _smart_case_pattern(pattern)
            # Smart pattern is already a regex, don't parse it
            flags = re.IGNORECASE
            return bool(re.search(smart_pattern, value, flags))
        # If it's a regex or wildcard, still use case-insensitive
        case_insensitive = True

    regex, _ = _parse_pattern(pattern, case_insensitive)
    flags = re.IGNORECASE if case_insensitive else 0
    return bool(re.search(regex, value, flags))


def _cst_get_return_annotation(node: cst.FunctionDef) -> str | None:
    """Extract return type annotation as string from a LibCST FunctionDef."""
    if node.returns is not None:
        return cst.Module([]).code_for_node(node.returns.annotation).strip()
    return None


def _cst_get_decorators(decorators: tuple[cst.Decorator, ...]) -> list[str]:
    """Extract decorator strings with @ prefix from LibCST decorators."""
    result = []
    for dec in decorators:
        code = cst.Module([]).code_for_node(dec.decorator).strip()
        result.append(f"@{code}")
    return result


def _cst_format_param(param: cst.Param) -> str:
    """Format a single parameter with optional annotation and default."""
    name = param.name.value
    if param.annotation is not None:
        ann = cst.Module([]).code_for_node(param.annotation.annotation).strip()
        name += f": {ann}"
    if param.default is not None:
        default = cst.Module([]).code_for_node(param.default).strip()
        name += f" = {default}"
    return name


def _cst_get_parameters(params: cst.Parameters) -> list[str]:
    """Extract parameter strings with type annotations from LibCST Parameters."""
    result = []

    # Positional-only args
    if hasattr(params, 'posonly_params'):
        for p in params.posonly_params:
            result.append(_cst_format_param(p))

    # Regular positional args
    for p in params.params:
        result.append(_cst_format_param(p))

    # *args
    if params.star_arg is not None and isinstance(params.star_arg, cst.Param):
        result.append(f"*{_cst_format_param(params.star_arg)}")

    # Keyword-only args
    for p in params.kwonly_params:
        result.append(_cst_format_param(p))

    # **kwargs
    if params.star_kwarg:
        result.append(f"**{_cst_format_param(params.star_kwarg)}")

    return result


class _SymbolCollector(cst.CSTVisitor):
    """LibCST visitor to collect all symbols with query info."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.symbols: list[SymbolInfo] = []
        self._path: list[str] = []
        self._depth: int = 1
        self._in_class_stack: list[bool] = [False]
        self._parent_name_stack: list[str | None] = [None]

    def _get_position(self, node: cst.CSTNode) -> tuple[int, int]:
        """Get (line_start, line_end) for a node."""
        pos = self.get_metadata(PositionProvider, node)
        return pos.start.line, pos.end.line

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        name = node.name.value
        symbol_path = self._path + [name]
        path_str = f"{self.filepath}::{'.'.join(symbol_path)}"
        line, end_line = self._get_position(node)

        self.symbols.append(
            SymbolInfo(
                path=path_str,
                name=name,
                kind="class",
                line=line,
                end_line=end_line,
                decorators=_cst_get_decorators(node.decorators),
                parent=self._parent_name_stack[-1],
                depth=self._depth,
            )
        )

        self._path = symbol_path
        self._depth += 1
        self._in_class_stack.append(True)
        self._parent_name_stack.append(name)
        return True

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._path = self._path[:-1]
        self._depth -= 1
        self._in_class_stack.pop()
        self._parent_name_stack.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        name = node.name.value
        symbol_path = self._path + [name]
        path_str = f"{self.filepath}::{'.'.join(symbol_path)}"
        line, end_line = self._get_position(node)

        is_async = node.asynchronous is not None
        is_method = self._in_class_stack[-1]

        if is_async:
            kind = "async_method" if is_method else "async_function"
        else:
            kind = "method" if is_method else "function"

        self.symbols.append(
            SymbolInfo(
                path=path_str,
                name=name,
                kind=kind,
                line=line,
                end_line=end_line,
                decorators=_cst_get_decorators(node.decorators),
                parameters=_cst_get_parameters(node.params),
                returns=_cst_get_return_annotation(node),
                parent=self._parent_name_stack[-1],
                depth=self._depth,
            )
        )

        # Functions inside functions are not methods
        self._path = symbol_path
        self._depth += 1
        self._in_class_stack.append(False)
        self._parent_name_stack.append(self._parent_name_stack[-1])
        return True

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._path = self._path[:-1]
        self._depth -= 1
        self._in_class_stack.pop()
        self._parent_name_stack.pop()


# Symbol cache: content_hash -> (filepath_used, symbols)
# When the same content is collected for different filepaths, we re-map the
# path prefix but avoid re-parsing and re-visiting.
_symbol_cache: dict[bytes, tuple[str, list[SymbolInfo]]] = {}
_SYMBOL_CACHE_MAX = 256


def _collect_symbols(
    filepath: Path,
    source: str,
) -> list[SymbolInfo]:
    """Collect all symbols from source using LibCST, with caching.

    Caches results by content hash so repeated queries on unchanged files
    are near-instant.
    """
    import hashlib
    key = hashlib.md5(source.encode(), usedforsecurity=False).digest()
    cached = _symbol_cache.get(key)
    if cached is not None:
        cached_path, cached_symbols = cached
        if cached_path == str(filepath):
            return cached_symbols
        # Same content, different filepath — remap paths
        old_prefix = cached_path
        new_prefix = str(filepath)
        remapped = []
        for sym in cached_symbols:
            new_sym = SymbolInfo(
                path=sym.path.replace(old_prefix, new_prefix, 1),
                name=sym.name,
                kind=sym.kind,
                line=sym.line,
                end_line=sym.end_line,
                decorators=sym.decorators,
                parameters=sym.parameters,
                returns=sym.returns,
                parent=sym.parent,
                depth=sym.depth,
            )
            remapped.append(new_sym)
        return remapped

    module = cst.parse_module(source)
    wrapper = MetadataWrapper(module)
    visitor = _SymbolCollector(str(filepath))
    wrapper.visit(visitor)

    if len(_symbol_cache) >= _SYMBOL_CACHE_MAX:
        keys_to_evict = list(_symbol_cache.keys())[:_SYMBOL_CACHE_MAX // 4]
        for k in keys_to_evict:
            del _symbol_cache[k]
    _symbol_cache[key] = (str(filepath), visitor.symbols)
    return visitor.symbols


def _filter_by_kind(symbol: SymbolInfo, kinds: list[str]) -> bool:
    """Check if symbol matches any of the kind filters (OR logic)."""
    if not kinds:
        return True

    for kind_pattern in kinds:
        if kind_pattern.endswith("*"):
            # Wildcard: async_* matches async_function and async_method
            prefix = kind_pattern[:-1]
            if symbol.kind.startswith(prefix):
                return True
        elif symbol.kind == kind_pattern:
            return True
    return False


def _filter_by_name(
    symbol: SymbolInfo, names: list[str], case_insensitive: bool, smart_case: bool = False
) -> bool:
    """Check if symbol name matches any of the name patterns (OR logic)."""
    if not names:
        return True

    for pattern in names:
        if _match_pattern(symbol.name, pattern, case_insensitive, smart_case):
            return True
    return False


def _filter_by_decorator(
    symbol: SymbolInfo, decorators: list[str], case_insensitive: bool, smart_case: bool = False
) -> bool:
    """Check if symbol has any matching decorator (OR logic)."""
    if not decorators:
        return True

    for dec_pattern in decorators:
        for dec in symbol.decorators:
            # Remove @ prefix for matching if pattern doesn't have it
            dec_name = dec[1:] if dec.startswith("@") else dec
            pattern = dec_pattern[1:] if dec_pattern.startswith("@") else dec_pattern

            if _match_pattern(dec_name, pattern, case_insensitive, smart_case):
                return True
    return False


def _filter_by_returns(
    symbol: SymbolInfo, returns_patterns: list[str], case_insensitive: bool, smart_case: bool = False
) -> bool:
    """Check if symbol return type matches any pattern (OR logic)."""
    if not returns_patterns:
        return True

    if symbol.returns is None:
        return False

    for pattern in returns_patterns:
        if _match_pattern(symbol.returns, pattern, case_insensitive, smart_case):
            return True
    return False


def _filter_by_returns_with_oracle(
    symbol: SymbolInfo,
    returns_patterns: list[str],
    case_insensitive: bool,
    smart_case: bool = False,
    file_types: object | None = None,
) -> bool:
    """Check if symbol return type matches any pattern, with TypeOracle fallback.

    First checks the annotation string (like _filter_by_returns). If no
    annotation is present but file_types (from TypeOracle) is available,
    looks up the inferred return type for the symbol definition.
    """
    if not returns_patterns:
        return True

    # First try annotation-based matching
    if symbol.returns is not None:
        for pattern in returns_patterns:
            if _match_pattern(symbol.returns, pattern, case_insensitive, smart_case):
                return True
        return False

    # No annotation — try TypeOracle if available
    if file_types is not None:
        from emend.type_oracle import _parse_type_string
        bindings = file_types.types_for_name(symbol.name)
        for binding in bindings:
            if binding.line == symbol.line and binding.binding_kind == "definition":
                td = binding.type_descriptor
                # For callable types, check the return type
                if td.kind == "callable" and td.return_type is not None:
                    ret_str = td.return_type.display()
                    for pattern in returns_patterns:
                        if _match_pattern(ret_str, pattern, case_insensitive, smart_case):
                            return True
                        # Also try structural matching via TypeDescriptor
                        constraint_td = _parse_type_string(pattern)
                        if td.return_type.matches(constraint_td):
                            return True
                # For non-callable definitions, check the raw type string
                elif binding.raw_type:
                    for pattern in returns_patterns:
                        if _match_pattern(binding.raw_type, pattern, case_insensitive, smart_case):
                            return True

    return False


def _filter_by_in_class(symbol: SymbolInfo, in_classes: list[str]) -> bool:
    """Check if symbol is in one of the specified classes (OR logic)."""
    if not in_classes:
        return True

    if symbol.parent is None:
        return False

    return symbol.parent in in_classes


def _filter_by_depth(symbol: SymbolInfo, depths: list[str]) -> bool:
    """Check if symbol matches depth filter.

    Depth 1 = top-level
    Depth 2 = one level nested (e.g., method in class)
    """
    if not depths:
        return True

    for depth_spec in depths:
        if depth_spec.endswith("+"):
            # "2+" means depth >= 2
            min_depth = int(depth_spec[:-1])
            if symbol.depth >= min_depth:
                return True
        else:
            # Exact depth match
            if symbol.depth == int(depth_spec):
                return True
    return False


def _filter_by_param(
    symbol: SymbolInfo, params: list[str], case_insensitive: bool, smart_case: bool = False
) -> bool:
    """Check if symbol has matching parameter (OR logic)."""
    if not params:
        return True

    for param_pattern in params:
        for param in symbol.parameters:
            if _match_pattern(param, param_pattern, case_insensitive, smart_case):
                return True
            # Also match just the parameter name (before :)
            param_name = param.split(":")[0].split("=")[0].strip()
            if _match_pattern(param_name, param_pattern, case_insensitive, smart_case):
                return True
    return False


def query_symbols(filepath: Path, filters: QueryFilter, type_oracle: object | None = None) -> list[SymbolInfo]:
    """Query symbols from a file with filters.

    Args:
        filepath: Path to Python file
        filters: QueryFilter with search criteria
        type_oracle: Optional TypeOracle instance for type-aware filtering.
            When provided and --returns filter is used, symbols without
            annotations are also checked against inferred return types.

    Returns:
        List of matching SymbolInfo objects
    """
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    with open(filepath) as f:
        source = f.read()

    all_symbols = _collect_symbols(filepath, source)

    # Build TypeOracle index for this file if needed for returns filtering
    file_types = None
    if type_oracle is not None and filters.returns_patterns:
        _logger.info("Building type index for lookup returns filtering: %s", filepath)
        file_types = type_oracle.infer_file(filepath)

    # Apply filters (different filter types use AND logic)
    results = []
    for symbol in all_symbols:
        # Each filter type must pass (AND logic)
        if not _filter_by_kind(symbol, filters.kinds):
            continue
        if not _filter_by_name(symbol, filters.names, filters.case_insensitive, filters.smart_case):
            continue
        if not _filter_by_decorator(symbol, filters.decorators, filters.case_insensitive, filters.smart_case):
            continue
        if not _filter_by_returns_with_oracle(
            symbol, filters.returns_patterns,
            filters.case_insensitive, filters.smart_case,
            file_types,
        ):
            continue
        if not _filter_by_in_class(symbol, filters.in_classes):
            continue
        if not _filter_by_depth(symbol, filters.depths):
            continue
        if not _filter_by_param(symbol, filters.params, filters.case_insensitive, filters.smart_case):
            continue

        results.append(symbol)

    return results


def cmd_query(
    filepath: str,
    kinds: list[str] | None = None,
    names: list[str] | None = None,
    decorators: list[str] | None = None,
    returns_patterns: list[str] | None = None,
    in_classes: list[str] | None = None,
    depths: list[str] | None = None,
    params: list[str] | None = None,
    case_insensitive: bool = False,
    smart_case: bool = False,
    output_json: bool = False,
    paths_only: bool = False,
    count_only: bool = False,
    type_oracle: object | None = None,
) -> None:
    """Execute the query command.

    Args:
        filepath: Path to Python file to query
        kinds: Symbol kinds to match (OR logic)
        names: Name patterns to match (OR logic)
        decorators: Decorator patterns to match (OR logic)
        returns_patterns: Return type patterns to match (OR logic)
        in_classes: Class names to restrict to (OR logic)
        depths: Depth filters like "1", "2", "2+"
        params: Parameter patterns to match
        case_insensitive: Enable case-insensitive matching
        smart_case: Enable smart-case pattern matching (snake_case, camelCase, PascalCase)
        output_json: Output as JSON
        paths_only: Output only selector paths
        count_only: Output only count of matches
        type_oracle: Optional TypeOracle for type-aware returns filtering
    """
    path = Path(filepath)
    if not path.exists():
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    filters = QueryFilter(
        kinds=kinds or [],
        names=names or [],
        decorators=decorators or [],
        returns_patterns=returns_patterns or [],
        in_classes=in_classes or [],
        depths=depths or [],
        params=params or [],
        case_insensitive=case_insensitive,
        smart_case=smart_case,
    )

    results = query_symbols(path, filters, type_oracle=type_oracle)

    # Output
    if count_only:
        print(len(results))
    elif paths_only:
        for symbol in results:
            print(symbol.path)
    elif output_json:
        print(json.dumps([s.to_dict() for s in results], indent=2))
    else:
        # Human-readable format
        for symbol in results:
            print(f"{symbol.path} ({symbol.kind}, line {symbol.line})")
