"""Unified command dispatch: lookup, edit, add."""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING
import io
import json
import logging
import sys

from ..component_selector import ExtendedSelector, parse_extended_selector

if TYPE_CHECKING:
    from ..type_oracle import TypeOracle

logger = logging.getLogger(__name__)

def _cmd_lookup_single_selector(  # noqa: C901
    selector: ExtendedSelector,
    file_or_pattern: str,
    case_insensitive: bool = False,
    smart_case: bool = False,
    json_output: bool = False,
    metadata: bool = False,
    paths_only: bool = False,
    count: bool = False,
    dedent: bool = False,
) -> str:
    """Lookup logic for a single (non-glob) selector."""
    from .components import get_component
    from .patterns import get_symbol_source, find_pattern
    # Handle line-based selectors with metadata - find containing symbol
    if selector.line_start is not None and metadata:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_line
        file_path = Path(selector.file_path)
        symbols = find_nested_definitions(str(file_path))
        symbol = find_symbol_by_line(symbols, selector.line_start, selector.line_end)

        if symbol is None:
            raise ValueError(f"No symbol found at line {selector.line_start}")

        selector = ExtendedSelector(
            file_path=selector.file_path,
            symbol_path=symbol.path,
        )

    # Handle metadata output
    if metadata:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_path
        file_path = Path(selector.file_path)
        symbols = find_nested_definitions(str(file_path))
        symbol = find_symbol_by_path(symbols, selector.symbol_path)

        if symbol is None:
            raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

        selector_path = f"{selector.file_path}::{'.'.join(symbol.path)}"
        total_lines = symbol.line_end - symbol.line_start + 1

        with open(selector.file_path) as f:
            lines = f.readlines()
        offset_start = sum(len(line) for line in lines[:symbol.line_start - 1])
        offset_end = sum(len(line) for line in lines[:symbol.line_end])

        output = [
            selector_path,
            "-" * 50,
            f"  Lines: {symbol.line_start}-{symbol.line_end} ({total_lines} lines)",
            f"  Offset: {offset_start}-{offset_end}",
        ]

        if symbol.decorators:
            decs_with_prefix = [f"@{d}" if not d.startswith('@') else d for d in symbol.decorators]
            dec_str = ", ".join(decs_with_prefix)
            output.append(f"  Decorators: {dec_str}")

        if symbol.parameters:
            param_names = ", ".join(symbol.parameters)
            output.append(f"  Parameters: {len(symbol.parameters)} ({param_names})")

        output.append(f"  Kind: {symbol.kind}")

        return "\n".join(output) + "\n"

    # If wildcard without component and with query flags, treat as query
    if selector.has_wildcards() and not selector.component and (count or paths_only or json_output):
        from emend.query import cmd_query

        old_stdout = sys.stdout
        sys.stdout = buffer = io.StringIO()
        try:
            cmd_query(
                filepath=file_or_pattern,
                kinds=None,
                names=None,
                decorators=None,
                returns_patterns=None,
                in_classes=None,
                depths=None,
                params=None,
                case_insensitive=case_insensitive,
                smart_case=smart_case,
                output_json=json_output,
                paths_only=paths_only,
                count_only=count,
            )
        finally:
            sys.stdout = old_stdout

        return buffer.getvalue()

    # If component specified, act like get
    if selector.component:
        if selector.has_wildcards():
            from emend.ast_utils import find_nested_definitions, expand_wildcard_path
            file_path = Path(selector.file_path)
            symbols = find_nested_definitions(str(file_path))
            matched_symbols = expand_wildcard_path(symbols, selector.symbol_path)

            if not matched_symbols:
                raise ValueError(f"No symbols match pattern {'.'.join(selector.symbol_path)}")

            results = []
            for sym in matched_symbols:
                specific_selector = ExtendedSelector(
                    file_path=selector.file_path,
                    symbol_path=sym.path,
                    component=selector.component,
                    accessor=selector.accessor,
                    pseudo_class=selector.pseudo_class,
                )
                try:
                    result = get_component(specific_selector)
                    if json_output:
                        results.append({"symbol": '.'.join(sym.path), "value": result})
                    else:
                        results.append(result)
                except (ValueError, FileNotFoundError):
                    pass

            if json_output:
                return json.dumps(results, indent=2)
            else:
                return '\n'.join(results)
        else:
            return get_component(selector)
    else:
        # No component - act like show
        if selector.has_wildcards():
            from emend.ast_utils import find_nested_definitions, expand_wildcard_path
            file_path = Path(selector.file_path)
            symbols = find_nested_definitions(str(file_path))
            matched_symbols = expand_wildcard_path(symbols, selector.symbol_path)

            if not matched_symbols:
                raise ValueError(f"No symbols match pattern {'.'.join(selector.symbol_path)}")

            results = []
            for sym in matched_symbols:
                specific_selector = ExtendedSelector(
                    file_path=selector.file_path,
                    symbol_path=sym.path,
                )
                try:
                    result = get_symbol_source(specific_selector, dedent=dedent)
                    results.append(result)
                except (ValueError, FileNotFoundError):
                    pass

            return '\n'.join(results)
        return get_symbol_source(selector, dedent=dedent)


def cmd_lookup(
    file_or_pattern: str,
    selector_str: str | None = None,
    kind: list[str] | None = None,
    name: list[str] | None = None,
    has_decorator: list[str] | None = None,
    returns: list[str] | None = None,
    in_class: list[str] | None = None,
    depth: list[str] | None = None,
    has_param: list[str] | None = None,
    case_insensitive: bool = False,
    smart_case: bool = False,
    json_output: bool = False,
    metadata: bool = False,
    paths_only: bool = False,
    count: bool = False,
    dedent: bool = False,
    matching: str | None = None,
    type_oracle: TypeOracle | None = None,
    out: "IO[str] | None" = None,
) -> str:
    """Unified lookup command combining get, query, and show.

    If selector_str contains component (e.g., [params], [returns]), acts like get.
    If filter flags provided, acts like query.
    Otherwise acts like show (display source code).
    """
    # If filter flags provided, act as query
    if any([kind, name, has_decorator, returns, in_class, depth, has_param]):
        from emend.query import cmd_query

        # Expand file globs for query mode
        import glob as glob_mod
        from emend.language_registry import is_source_file, get_extensions
        files_to_query = []
        fop = Path(file_or_pattern)
        if fop.is_dir():
            # Collect all known source files under the directory
            files_to_query = [str(f) for f in fop.rglob("*") if f.is_file() and is_source_file(str(f))]
        elif '*' in file_or_pattern or '?' in file_or_pattern:
            files_to_query = [f for f in glob_mod.glob(file_or_pattern, recursive=True) if is_source_file(f)]
        else:
            files_to_query = [file_or_pattern]

        if out is not None and not count:
            # Streaming path: write each file's output directly to out as it completes
            for fpath in files_to_query:
                old_stdout = sys.stdout
                sys.stdout = out
                try:
                    cmd_query(
                        filepath=fpath,
                        kinds=kind,
                        names=name,
                        decorators=has_decorator,
                        returns_patterns=returns,
                        in_classes=in_class,
                        depths=depth,
                        params=has_param,
                        case_insensitive=case_insensitive,
                        smart_case=smart_case,
                        output_json=json_output,
                        paths_only=paths_only,
                        count_only=False,
                        type_oracle=type_oracle,
                    )
                finally:
                    sys.stdout = old_stdout
                out.flush()
            return ''

        all_output = []
        total_count_val = 0
        for fpath in files_to_query:
            old_stdout = sys.stdout
            sys.stdout = buffer = io.StringIO()
            try:
                cmd_query(
                    filepath=fpath,
                    kinds=kind,
                    names=name,
                    decorators=has_decorator,
                    returns_patterns=returns,
                    in_classes=in_class,
                    depths=depth,
                    params=has_param,
                    case_insensitive=case_insensitive,
                    smart_case=smart_case,
                    output_json=json_output,
                    paths_only=paths_only,
                    count_only=count,
                    type_oracle=type_oracle,
                )
            finally:
                sys.stdout = old_stdout
            result = buffer.getvalue()
            if result:
                if count:
                    try:
                        total_count_val += int(result.strip())
                    except ValueError:
                        all_output.append(result)
                else:
                    all_output.append(result)

        if count:
            return str(total_count_val) + '\n'
        return ''.join(all_output)

    # Parse selector if provided
    if selector_str:
        selector = parse_extended_selector(selector_str)

        # Reject line selectors with file globs
        if selector.has_file_glob() and selector.line_start is not None:
            raise ValueError("Line selectors cannot be combined with file globs")

        # Multi-file dispatch for file globs
        if selector.has_file_glob():
            expanded_files = selector.expand_file_glob()

            if out is not None and not matching:
                # Streaming path: write each file's result to out as it completes
                any_results = False
                for fpath in expanded_files:
                    concrete = selector.with_file_path(fpath)
                    try:
                        result = _cmd_lookup_single_selector(
                            concrete,
                            file_or_pattern=fpath,
                            case_insensitive=case_insensitive,
                            smart_case=smart_case,
                            json_output=json_output,
                            metadata=metadata,
                            paths_only=paths_only,
                            count=count,
                            dedent=dedent,
                        )
                        if result:
                            out.write(result)
                            if not result.endswith('\n'):
                                out.write('\n')
                            out.flush()
                            any_results = True
                    except (ValueError, FileNotFoundError):
                        continue
                if not any_results:
                    raise ValueError(f"No symbols found matching {selector_str}")
                return ''

            all_results = []
            for fpath in expanded_files:
                concrete = selector.with_file_path(fpath)
                try:
                    result = _cmd_lookup_single_selector(
                        concrete,
                        file_or_pattern=fpath,
                        case_insensitive=case_insensitive,
                        smart_case=smart_case,
                        json_output=json_output,
                        metadata=metadata,
                        paths_only=paths_only,
                        count=count,
                        dedent=dedent,
                    )
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue

            if not all_results:
                raise ValueError(f"No symbols found matching {selector_str}")

            combined = '\n'.join(all_results)

            # Apply --matching filter if specified
            if matching:
                combined = _apply_matching_filter(combined, matching, selector, expanded_files, json_output)

            return combined

        result = _cmd_lookup_single_selector(
            selector,
            file_or_pattern=file_or_pattern,
            case_insensitive=case_insensitive,
            smart_case=smart_case,
            json_output=json_output,
            metadata=metadata,
            paths_only=paths_only,
            count=count,
            dedent=dedent,
        )

        # Apply --matching filter for single-file selectors
        if matching and result:
            result = _apply_matching_filter(
                result, matching, selector, [selector.file_path], json_output
            )

        return result
    else:
        raise ValueError("No selector provided")


def _apply_matching_filter(
    lookup_result: str,
    matching_pattern: str,
    selector: ExtendedSelector,
    files: list[str],
    json_output: bool = False,
) -> str:
    """Filter lookup results to only symbols whose body matches a pattern."""
    from .patterns import get_symbol_source, find_pattern
    filtered_parts = []
    for part in lookup_result.split('\n'):
        part = part.strip()
        if not part:
            continue
        # Try to parse as a selector path (file.py::Symbol.path format)
        if '::' in part:
            try:
                sel = parse_extended_selector(part)
                source = get_symbol_source(sel)
                matches = find_pattern(matching_pattern, sel.file_path, source_override=source)
                if matches:
                    filtered_parts.append(part)
            except (ValueError, FileNotFoundError):
                continue
        else:
            # For source code output, check the whole result against the pattern
            for fpath in files:
                try:
                    matches = find_pattern(matching_pattern, fpath, source_override=lookup_result)
                    if matches:
                        return lookup_result
                except (ValueError, FileNotFoundError):
                    pass
            return ""

    return '\n'.join(filtered_parts)


def _merge_type_filter(
    selector: ExtendedSelector,
    returns_filter: list[str] | None,
) -> list[str] | None:
    """Merge a selector's :returns[X] type_filter into the returns_filter list.

    If the selector has a ``type_filter`` like ``returns[str]``, the type
    string is appended to (or creates) the returns_filter list so the
    existing returns-based filtering logic handles it.
    """
    if selector.type_filter is None:
        return returns_filter
    # Parse "returns[str]" or "type[Connection]"
    tf = selector.type_filter
    bracket = tf.index("[")
    kind = tf[:bracket]
    type_string = tf[bracket + 1:-1]
    if kind == "returns":
        merged = list(returns_filter) if returns_filter else []
        merged.append(type_string)
        return merged
    # For :type[X], pass through as-is (future: filter by inferred type)
    return returns_filter


def _expand_selector_with_returns_filter(
    selector: ExtendedSelector,
    returns_filter: list[str],
    type_oracle: TypeOracle | None = None,
) -> list[ExtendedSelector]:
    """Expand a selector to only include symbols matching a returns filter.

    Uses annotation-based matching, falling back to type oracle when available.
    Returns concrete selectors for each matching symbol.
    """
    import fnmatch as _fnmatch
    from emend.query import _collect_symbols, _filter_by_returns_with_oracle

    file_path = Path(selector.file_path)
    if not file_path.exists():
        return []
    source = file_path.read_text()
    symbols = _collect_symbols(file_path, source)

    # Build type index if oracle available
    file_types = None
    if type_oracle is not None:
        file_types = type_oracle.infer_file(file_path)

    result = []
    for symbol in symbols:
        # Extract symbol's path segments from its full path (file.py::Class.method → [Class, method])
        parts = symbol.path.split("::")
        sym_path = parts[1].split(".") if len(parts) > 1 else [symbol.name]

        # Check if symbol matches the selector's symbol_path pattern
        if len(sym_path) != len(selector.symbol_path):
            continue
        match = True
        for seg, pat in zip(sym_path, selector.symbol_path):
            if pat != "*" and not _fnmatch.fnmatch(seg, pat):
                match = False
                break
        if not match:
            continue

        # Check returns filter
        if not _filter_by_returns_with_oracle(
            symbol, returns_filter, case_insensitive=False, file_types=file_types,
        ):
            continue

        # Create concrete selector for this symbol
        concrete = ExtendedSelector(
            file_path=selector.file_path,
            symbol_path=sym_path,
            component=selector.component,
            accessor=selector.accessor,
            pseudo_class=selector.pseudo_class,
        )
        result.append(concrete)

    return result


def _dispatch_with_returns_filter(
    selector_str: str,
    selector: ExtendedSelector,
    returns_filter: list[str] | None,
    type_oracle: TypeOracle | None,
    single_fn: Callable[[ExtendedSelector], str],
) -> str:
    """Common dispatch logic for cmd_edit and cmd_add.

    Handles:
    - Returns-filter expansion: expand wildcard selector to matching symbols
    - File-glob dispatch: iterate over multiple matching files
    - Single-selector fall-through

    *single_fn* is called with each concrete selector and should return a diff
    string (empty string = no change).
    """
    if returns_filter:
        files = (
            selector.expand_file_glob()
            if selector.has_file_glob()
            else [selector.file_path]
        )
        all_results = []
        for fpath in files:
            concrete_base = selector.with_file_path(fpath) if fpath != selector.file_path else selector
            for concrete in _expand_selector_with_returns_filter(
                concrete_base, returns_filter, type_oracle
            ):
                try:
                    result = single_fn(concrete)
                    if result:
                        all_results.append(result)
                except (ValueError, FileNotFoundError):
                    continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str} with --returns {returns_filter}")
        return '\n'.join(all_results)

    if selector.has_file_glob():
        expanded_files = selector.expand_file_glob()
        all_results = []
        for fpath in expanded_files:
            concrete = selector.with_file_path(fpath)
            try:
                result = single_fn(concrete)
                if result:
                    all_results.append(result)
            except (ValueError, FileNotFoundError):
                continue
        if not all_results:
            raise ValueError(f"No symbols found matching {selector_str}")
        return '\n'.join(all_results)

    return single_fn(selector)


def _cmd_edit_single(
    selector: ExtendedSelector,
    value: str | None = None,
    rm: bool = False,
    apply: bool = False,
) -> str:
    """Edit logic for a single (non-glob) selector."""
    from .components import remove_component, set_component
    if rm or value == "":
        return remove_component(selector, apply=apply)

    if selector.pseudo_class is not None:
        raise ValueError(
            f"Cannot use pseudo-class '{selector.pseudo_class}' with 'edit' command. "
            "Use 'add' command to insert new items."
        )

    if value is not None:
        return set_component(selector, value, apply=apply)

    raise ValueError("No operation specified (provide value or --rm)")


def cmd_edit(
    selector_str: str,
    value: str | None = None,
    rm: bool = False,
    apply: bool = False,
    returns_filter: list[str] | None = None,
    type_oracle: TypeOracle | None = None,
) -> str:
    """Edit or replace existing symbol components.

    - If rm=True or value="", remove the component or symbol
    - If accessor present + value, modify specific item (e.g., [params][x])
    - If no accessor + value, replace entire component (e.g., [returns])
    - If returns_filter or selector :returns[X] specified, only edit symbols
      whose return type matches (annotation first, then inferred via oracle)
    """
    selector = parse_extended_selector(selector_str)

    # Merge selector type_filter into returns_filter
    returns_filter = _merge_type_filter(selector, returns_filter)

    def _single(sel: ExtendedSelector) -> str:
        return _cmd_edit_single(sel, value=value, rm=rm, apply=apply)

    return _dispatch_with_returns_filter(
        selector_str, selector, returns_filter, type_oracle, _single
    )


def _cmd_add_single(
    selector: ExtendedSelector,
    value: str,
    before: str | None = None,
    after: str | None = None,
    at: int | None = None,
    apply: bool = False,
) -> str:
    """Add logic for a single (non-glob) selector."""
    from .components import add_to_component
    position = at if at is not None else -1
    kind = selector.pseudo_class if selector.pseudo_class else None
    return add_to_component(
        selector,
        value,
        position=position,
        before=before,
        after=after,
        apply=apply,
        kind=kind,
    )


def cmd_add(
    selector_str: str,
    value: str,
    before: str | None = None,
    after: str | None = None,
    at: int | None = None,
    apply: bool = False,
    returns_filter: list[str] | None = None,
    type_oracle: TypeOracle | None = None,
) -> str:
    """Add new items to symbol components.

    - Position can be specified with --at, --before, or --after
    - Default is to append to end
    - Pseudo-class (e.g., :KEYWORD_ONLY) specifies parameter kind
    - If returns_filter or selector :returns[X] specified, only add to symbols
      whose return type matches (annotation first, then inferred via oracle)
    """
    selector = parse_extended_selector(selector_str)

    # Merge selector type_filter into returns_filter
    returns_filter = _merge_type_filter(selector, returns_filter)

    def _single(sel: ExtendedSelector) -> str:
        return _cmd_add_single(sel, value=value, before=before, after=after, at=at, apply=apply)

    return _dispatch_with_returns_filter(
        selector_str, selector, returns_filter, type_oracle, _single
    )
