"""Component access, modification, and diff generation."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from emend import emend_core as _rust

from ..component_selector import ExtendedSelector

_LIST_COMPONENTS = frozenset({"params", "decorators", "bases", "imports"})
_PARAMETER_KINDS = frozenset(
    {"POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"}
)


def _read_source(selector: ExtendedSelector) -> tuple[Path, str, str]:
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    from .project_iter import _ext_from_path

    return file_path, file_path.read_text(), _ext_from_path(file_path)


def _finish_transform(
    transform: _rust.PyFileTransform,
    file_path: Path,
    source_code: str,
    *,
    diff_path: str,
    apply: bool,
    error_message: str,
    error_type: type[Exception] = ValueError,
) -> str:
    new_code = transform.apply()
    if new_code is None:
        raise error_type(error_message)
    if apply:
        file_path.write_text(new_code)
    return _generate_diff(diff_path, source_code, new_code)


def _raise_component_not_found(
    selector: ExtendedSelector,
    source_code: str,
    _ext: str,
    message: str | None = None,
) -> None:
    """Raise the most specific error available for a missing component."""
    syms = _rust.collect_symbols_from_str(
        source_code, selector=".".join(selector.symbol_path), ext=_ext
    )
    if not syms:
        raise ValueError(
            f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}"
        )
    kind = syms[0]["kind"]
    if kind == "class" and selector.component in ("params", "returns"):
        raise ValueError(f"Component '{selector.component}' not valid for ClassDef")
    if (
        kind in ("function", "async_function", "method", "async_method")
        and selector.component == "bases"
    ):
        raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")
    raise ValueError(
        message
        or f"Component '{selector.component}' not found or not valid for symbol "
        f"{'.'.join(selector.symbol_path)}"
    )


def get_component(selector: ExtendedSelector) -> str:
    """Return a selected component's source text."""
    _, source_code, ext = _read_source(selector)

    from .project_iter import _get_imports

    if not selector.symbol_path:
        if selector.component == "imports":
            return _get_imports(source_code, language=selector.language)
        raise ValueError(f"Component '{selector.component}' requires a symbol path")

    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=ext,
    )

    if range_info is None:
        _raise_component_not_found(selector, source_code, ext)

    start_byte, end_byte = range_info

    # For returns, Rust returns an insertion point if it's not there.
    # get_component should raise error if it's truly not there.
    if selector.component == "returns" and start_byte == end_byte:
        raise ValueError(
            f"Function {'.'.join(selector.symbol_path)} has no return annotation"
        )

    result = source_code.encode("utf-8")[start_byte:end_byte].decode("utf-8")

    if selector.component == "returns":
        s = result.strip()
        if s.startswith("->"):
            s = s[2:]
        return s.strip()
    if selector.component == "body":
        return result.strip("\n").rstrip()

    return result.strip()


def _generate_diff(file_path: str, old_code: str, new_code: str) -> str:
    """Generate unified diff string."""
    old_lines = old_code.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)
    return ''.join(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=file_path,
        tofile=file_path
    ))


def set_component(selector: ExtendedSelector, value: str, apply: bool = False) -> str:
    """Set value of component. Returns diff."""
    file_path, source_code, ext = _read_source(selector)

    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=ext,
    )

    if range_info is None:
        _raise_component_not_found(selector, source_code, ext)

    start_byte, end_byte = range_info

    # Prepare the replacement value
    replacement = value
    stripped = value.strip()
    if selector.component == "returns" and stripped and not stripped.startswith("->"):
        replacement = f" -> {stripped}"
    elif (
        selector.component == "decorators"
        and stripped
        and not stripped.startswith("@")
        and "\n" not in stripped
    ):
        replacement = f"@{stripped}"
    elif selector.component == "body":
        # Ensure it starts with a newline and is indented if it's a block
        if not value.startswith("\n"):
            replacement = "\n    " + stripped.replace("\n", "\n    ")

    # Apply transformation using Rust FileTransform
    transform = _rust.PyFileTransform(source_code)
    transform.replace_range(start_byte, end_byte, replacement)
    return _finish_transform(
        transform,
        file_path,
        source_code,
        diff_path=selector.file_path,
        apply=apply,
        error_message="Failed to apply transformation (overlapping edits)",
        error_type=RuntimeError,
    )


def _parameter_insertion(
    items_info: list[tuple[str, int, int]],
    insert_idx: int,
    value: str,
    target_kind: str | None,
) -> tuple[int, str]:
    """Place a parameter within Python's legal signature partitions."""
    names = [item[0] for item in items_info]
    positional_separator = names.index("/") if "/" in names else None
    keyword_separator = names.index("*") if "*" in names else None
    vararg = next(
        (
            i
            for i, name in enumerate(names)
            if name.startswith("*") and not name.startswith("**") and name != "*"
        ),
        None,
    )
    kwarg = next(
        (i for i, name in enumerate(names) if name.startswith("**")), None
    )

    if target_kind == "POSITIONAL_ONLY":
        if positional_separator is None:
            return 0, f"{value}, /"
        return min(insert_idx, positional_separator), value

    if target_kind == "KEYWORD_ONLY":
        if keyword_separator is None and vararg is None:
            return kwarg if kwarg is not None else len(items_info), f"*, {value}"
        boundary = keyword_separator if keyword_separator is not None else vararg
        assert boundary is not None
        insert_idx = max(insert_idx, boundary + 1)
        if kwarg is not None:
            insert_idx = min(insert_idx, kwarg)
        return insert_idx, value

    # An unclassified parameter appended after ``*`` is keyword-only; only
    # **kwargs is an unconditional end-of-signature boundary.  The explicit
    # POSITIONAL_OR_KEYWORD mode must also stay before the other partitions.
    if target_kind is None:
        if kwarg is not None:
            insert_idx = min(insert_idx, kwarg)
        return insert_idx, value

    if keyword_separator is not None:
        insert_idx = min(insert_idx, keyword_separator)
    elif vararg is not None:
        insert_idx = min(insert_idx, vararg)
    if kwarg is not None:
        insert_idx = min(insert_idx, kwarg)
    if positional_separator is not None:
        insert_idx = max(insert_idx, positional_separator + 1)
    return insert_idx, value


def _list_insertion(component: str, value: str, *, before: bool) -> str:
    if component == "decorators":
        return f"@{value}\n" if before else f"\n@{value}"
    return f"{value}, " if before else f", {value}"


def add_to_component(
    selector: ExtendedSelector,
    value: str,
    position: int = -1,
    before: str | None = None,
    after: str | None = None,
    apply: bool = False,
    kind: str | None = None,
) -> str:
    """Add item to list component. Returns diff."""
    # Validate mutually exclusive position options
    if before is not None and after is not None:
        raise ValueError("Cannot specify both --before and --after")

    # Validate that component is a list type
    if selector.component not in _LIST_COMPONENTS:
        raise ValueError(f"Component '{selector.component}' is not a list component")

    # Validate that accessor is None
    if selector.accessor is not None:
        raise ValueError("add_to_component requires accessor must be None")

    # Validate kind parameter
    if kind is not None:
        if selector.component != "params":
            raise ValueError("'kind' parameter can only be used with 'params' component")
        if kind not in _PARAMETER_KINDS:
            allowed = ", ".join(sorted(_PARAMETER_KINDS))
            raise ValueError(f"Invalid kind value: {kind}. Must be one of: {allowed}")

    file_path, source_code, ext = _read_source(selector)

    from .project_iter import _add_import_text
    # Handle module-level imports component
    if selector.component == "imports" and not selector.symbol_path:
        return _add_import_text(value, position, file_path, apply, source_code, language=selector.language)

    items_info = _rust.get_symbol_component_list_items(
        source_code,
        selector.symbol_path,
        selector.component,
        ext=ext,
    )

    if items_info is None:
        _raise_component_not_found(selector, source_code, ext)

    # Calculate insertion index in the items list
    items = [item[0] for item in items_info]
    if before is not None:
        try:
            insert_idx = items.index(before)
        except ValueError as error:
            raise ValueError(
                f"{selector.component.capitalize()[:-1]} '{before}' not found"
            ) from error
    elif after is not None:
        try:
            insert_idx = items.index(after) + 1
        except ValueError as error:
            raise ValueError(
                f"{selector.component.capitalize()[:-1]} '{after}' not found"
            ) from error
    elif position == -1:
        insert_idx = len(items)
    elif position < -1:
        raise ValueError("position must be -1 or a non-negative index")
    else:
        insert_idx = position

    transform = _rust.PyFileTransform(source_code)

    val_to_add = value.strip()
    if selector.component == "decorators" and val_to_add.startswith("@"):
        val_to_add = val_to_add[1:]

    if not items_info:
        replacement = val_to_add
        if selector.component == "decorators":
            replacement = f"@{val_to_add}\n"
        elif selector.component == "bases":
            replacement = f"({val_to_add})"
        elif selector.component == "params":
            target_kind = kind or selector.pseudo_class
            if target_kind == "KEYWORD_ONLY":
                replacement = f"*, {val_to_add}"
            elif target_kind == "POSITIONAL_ONLY":
                replacement = f"{val_to_add}, /"
            else:
                replacement = val_to_add
        
        container_range = _rust.get_symbol_component_range(
            source_code,
            selector.symbol_path,
            selector.component,
            None,
            ext=ext,
        )
        if container_range is None:
            _raise_component_not_found(
                selector,
                source_code,
                ext,
                message=f"Could not find container for {selector.component}",
            )

        cont_start, cont_end = container_range
        transform.replace_range(cont_start, cont_end, replacement)
    else:
        if selector.component == "params":
            insert_idx, val_to_add = _parameter_insertion(
                items_info, insert_idx, val_to_add, kind or selector.pseudo_class
            )

        if insert_idx >= len(items_info):
            transform.insert_after(
                items_info[-1][2],
                _list_insertion(selector.component, val_to_add, before=False),
            )
        else:
            transform.insert_before(
                items_info[insert_idx][1],
                _list_insertion(selector.component, val_to_add, before=True),
            )

    return _finish_transform(
        transform,
        file_path,
        source_code,
        diff_path=selector.file_path,
        apply=apply,
        error_message=(
            f"Failed to add to component '{selector.component}' in "
            f"{'.'.join(selector.symbol_path)}: overlapping byte ranges"
        ),
    )


def remove_component(selector: ExtendedSelector, apply: bool = False) -> str:
    """Remove component or item. Returns diff."""
    from .patterns import remove_symbol

    if selector.component is None:
        return remove_symbol(selector, apply=apply)

    if selector.component == "body":
        raise ValueError("Cannot remove body component")

    file_path, source_code, ext = _read_source(selector)

    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=ext,
    )

    if range_info is None:
        _raise_component_not_found(selector, source_code, ext)

    start_byte, end_byte = range_info
    transform = _rust.PyFileTransform(source_code)
    source_bytes = source_code.encode("utf-8")

    if selector.accessor is not None:
        i = end_byte
        while i < len(source_bytes) and source_bytes[i : i + 1] in (
            b" ", b"\t", b"\n", b"\r"
        ):
            i += 1

        if i < len(source_bytes) and source_bytes[i : i + 1] == b",":
            j = i + 1
            while j < len(source_bytes) and source_bytes[j : j + 1] in (b" ", b"\t"):
                j += 1
            transform.remove_range(start_byte, j)
        else:
            i = start_byte
            while i > 0 and source_bytes[i - 1 : i] in (
                b" ", b"\t", b"\n", b"\r"
            ):
                i -= 1

            if i > 0 and source_bytes[i - 1 : i] == b",":
                j = i - 1
                while j > 0 and source_bytes[j - 1 : j] in (b" ", b"\t"):
                    j -= 1
                transform.remove_range(j, end_byte)
            else:
                if selector.component == "decorators":
                    i = start_byte
                    while i > 0 and source_bytes[i - 1 : i] not in (b"\n", b"\r", b"@"):
                        i -= 1
                    if i > 0 and source_bytes[i - 1 : i] == b"@":
                        i -= 1

                    j = end_byte
                    while j < len(source_bytes) and source_bytes[j : j + 1] in (b" ", b"\t"):
                        j += 1
                    if j < len(source_bytes) and source_bytes[j : j + 1] in (b"\n", b"\r"):
                        j += 1
                        if source_bytes[j - 1 : j + 1] == b"\r\n":
                            j += 1
                    transform.remove_range(i, j)
                else:
                    transform.remove_range(start_byte, end_byte)
    else:
        if selector.component == "returns":
            transform.remove_range(start_byte, end_byte)
        elif selector.component == "bases":
            i = start_byte
            while i > 0 and source_bytes[i - 1 : i] in (b" ", b"\t"):
                i -= 1

            j = end_byte
            while j < len(source_bytes) and source_bytes[j : j + 1] in (b" ", b"\t"):
                j += 1

            if (
                i > 0
                and source_bytes[i - 1 : i] == b"("
                and j < len(source_bytes)
                and source_bytes[j : j + 1] == b")"
            ):
                transform.remove_range(i - 1, j + 1)
            else:
                transform.remove_range(start_byte, end_byte)
        else:
            transform.remove_range(start_byte, end_byte)

    return _finish_transform(
        transform,
        file_path,
        source_code,
        diff_path=selector.file_path,
        apply=apply,
        error_message=(
            f"Failed to remove component '{selector.component}' from "
            f"{'.'.join(selector.symbol_path)}: overlapping byte ranges"
        ),
    )


_CONTENT_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\.content\}")


def _extract_string_content_from_text(text: str, ext: str = "py") -> str | None:
    """Extract the inner content of a string literal from source text.

    For a string like ``"MyClass"`` or ``'MyClass'`` returns ``MyClass``.
    Returns None for non-string text or complex strings that cannot be
    trivially unwrapped (f-strings, concatenated strings).
    """
    from emend import emend_core as _rust
    return _rust.parse_string_literal(text, ext)
