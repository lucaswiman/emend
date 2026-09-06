"""Query command for finding symbols with filters.

This module provides a filter-based search for Python symbols,
designed for AI agent refactoring workflows.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from emend.type_oracle import FileTypes, TypeOracle

_logger = logging.getLogger(__name__)


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
    bases: list[str] = field(default_factory=list)
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
        if self.bases:
            result["bases"] = self.bases
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


def _parse_pattern(pattern: str) -> str:
    """Parse a pattern into a regex string.

    Handles:
    - /regex/ - explicit regex (returned as-is without delimiters)
    - glob patterns with * and ? - converted to regex
    """
    # Check for regex pattern (slash-delimited)
    if pattern.startswith("/") and pattern.endswith("/"):
        return pattern[1:-1]

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

    return f"^{regex}$"


def _match_pattern(
    value: str, pattern: str, case_insensitive: bool = False, smart_case: bool = False
) -> bool:
    """Match a value against a pattern (glob or regex)."""
    # Apply smart-case transformation if enabled
    if smart_case:
        # Don't transform if it's a regex or contains wildcards
        if not (pattern.startswith("/") and pattern.endswith("/")) and "*" not in pattern and "?" not in pattern:
            smart_pattern = _smart_case_pattern(pattern)
            # Smart pattern is already a regex; anchor it for full-match semantics
            flags = re.IGNORECASE
            return bool(re.search(f"^{smart_pattern}$", value, flags))
        # If it's a regex or wildcard, still use case-insensitive
        case_insensitive = True

    regex = _parse_pattern(pattern)
    flags = re.IGNORECASE if case_insensitive else 0
    return bool(re.search(regex, value, flags))


def _extract_params_from_signature(signature: str | None) -> list[str]:
    """Parse parameter strings from Rust signature like '(x: int, y, *args, **kwargs) -> str'."""
    if not signature:
        return []
    # Strip return type
    sig = signature
    if " -> " in sig:
        sig = sig[:sig.index(" -> ")]
    # Strip parens
    sig = sig.strip()
    if sig.startswith("(") and sig.endswith(")"):
        sig = sig[1:-1]
    if not sig.strip():
        return []
    # Split on top-level commas only — a comma inside brackets/parens (e.g. in
    # ``b: Dict[str, int]`` or a default like ``x=(1, 2)``) is part of a single
    # parameter and must not split it.
    params: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in sig:
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            params.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        params.append("".join(current))
    return [p.strip() for p in params if p.strip()]


def _rust_dict_to_symbol_info_list(
    dicts: list[dict], filepath: str, depth: int = 1, parent: str | None = None,
) -> list[SymbolInfo]:
    """Convert flat Rust symbol dicts to a list of SymbolInfo objects."""
    symbols: list[SymbolInfo] = []
    for d in dicts:
        kind = d.get("kind", "")
        sym_path = list(d.get("path", []))

        # Skip reference symbols
        if kind == "reference":
            continue

        path_str = f"{filepath}::{'.'.join(sym_path)}"

        # Extract decorators with @ prefix
        raw_decorators = list(d.get("decorators", []))
        decorators = [f"@{dec}" for dec in raw_decorators]

        # Extract parameters from signature
        signature = d.get("signature")
        parameters = _extract_params_from_signature(signature)

        # Get return type
        returns = d.get("returns")

        # Get bases (superclasses)
        bases = list(d.get("bases", []))

        symbols.append(
            SymbolInfo(
                path=path_str,
                name=d["name"],
                kind=kind,
                line=d["line"],
                end_line=d["end_line"],
                decorators=decorators,
                parameters=parameters,
                returns=returns,
                parent=parent,
                bases=bases,
                depth=depth,
            )
        )

        # Recurse into children
        children = d.get("children", [])
        if children:
            child_parent = d["name"] if kind == "class" else parent
            symbols.extend(
                _rust_dict_to_symbol_info_list(
                    children, filepath, depth=depth + 1, parent=child_parent,
                )
            )

    return symbols


# Symbol cache: content_hash -> (filepath_used, symbols)
# When the same content is collected for different filepaths, we re-map the
# path prefix but avoid re-parsing and re-visiting.
_symbol_cache: dict[bytes, tuple[str, list[SymbolInfo]]] = {}
_SYMBOL_CACHE_MAX = 256


def _collect_symbols(
    filepath: Path,
    source: str,
) -> list[SymbolInfo]:
    """Collect all symbols from source using tree-sitter via Rust, with caching.

    Caches results by content hash so repeated queries on unchanged files
    are near-instant.
    """
    import hashlib
    # Key on (extension, content-hash): two files with identical content but
    # different languages parse to different symbol sets, so the extension must
    # be part of the cache key to avoid cross-language collisions.
    ext = Path(filepath).suffix.lstrip('.') or 'py'
    key = (ext, hashlib.md5(source.encode(), usedforsecurity=False).digest())
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
                bases=sym.bases,
                depth=sym.depth,
            )
            remapped.append(new_sym)
        return remapped

    from emend import emend_core
    rust_syms = emend_core.collect_symbols_from_str(source, ext=ext)
    symbols = _rust_dict_to_symbol_info_list(rust_syms, str(filepath))

    if len(_symbol_cache) >= _SYMBOL_CACHE_MAX:
        keys_to_evict = list(_symbol_cache.keys())[:_SYMBOL_CACHE_MAX // 4]
        for k in keys_to_evict:
            del _symbol_cache[k]
    _symbol_cache[key] = (str(filepath), symbols)
    return symbols


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


def _filter_by_returns_with_oracle(
    symbol: SymbolInfo,
    returns_patterns: list[str],
    case_insensitive: bool,
    smart_case: bool = False,
    file_types: FileTypes | None = None,
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
        from emend.type_oracle import parse_type_string
        bindings = file_types.types_for_name(symbol.name)
        for binding in bindings:
            if binding.line == symbol.line and binding.binding_kind in ("definition", "inferred"):
                td = binding.type_descriptor
                # For callable types, check the return type
                if td.kind == "callable" and td.return_type is not None:
                    ret_str = td.return_type.display()
                    for pattern in returns_patterns:
                        if _match_pattern(ret_str, pattern, case_insensitive, smart_case):
                            return True
                        # Also try structural matching via TypeDescriptor
                        constraint_td = parse_type_string(pattern)
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
            try:
                min_depth = int(depth_spec[:-1])
            except ValueError:
                raise ValueError(
                    f"invalid depth value {depth_spec!r}: expected an integer followed by '+' (e.g. '2+')"
                )
            if symbol.depth >= min_depth:
                return True
        else:
            # Exact depth match
            try:
                exact_depth = int(depth_spec)
            except ValueError:
                raise ValueError(
                    f"invalid depth value {depth_spec!r}: expected an integer (e.g. '2')"
                )
            if symbol.depth == exact_depth:
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


def query_symbols(filepath: Path, filters: QueryFilter, type_oracle: TypeOracle | None = None) -> list[SymbolInfo]:
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
    with open(filepath) as f:
        source = f.read()

    all_symbols = _collect_symbols(filepath, source)

    # Build TypeOracle index for this file if needed for returns filtering
    file_types = None
    if type_oracle is not None and filters.returns_patterns:
        _logger.debug("Building type index for lookup returns filtering: %s", filepath)
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


def format_query_results(
    results: list[SymbolInfo],
    *,
    output_json: bool = False,
    paths_only: bool = False,
    count_only: bool = False,
) -> str:
    """Render query results without redirecting process-wide output."""
    if count_only:
        return f"{len(results)}\n"
    if output_json and not paths_only:
        return json.dumps([s.to_dict() for s in results], indent=2) + "\n"
    return "".join(
        f"{symbol.path}\n" if paths_only
        else f"{symbol.path} ({symbol.kind}, line {symbol.line})\n"
        for symbol in results
    )
