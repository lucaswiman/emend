"""Component access, modification, and diff generation."""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING
import difflib
import logging
import re

from ..component_selector import ExtendedSelector
from emend import emend_core as _rust

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

def _raise_component_not_found(
    selector: ExtendedSelector,
    source_code: str,
    _ext: str,
    message: str | None = None,
) -> None:
    """Raise a descriptive ValueError when a component lookup returns None.

    Checks whether the symbol itself is missing (raises "Symbol not found")
    or whether the component is invalid for the symbol kind (raises a
    specific type-mismatch error), falling back to the generic
    "Component not found" message.
    """
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
    if kind in ("function", "async_function", "method", "async_method") and selector.component == "bases":
        raise ValueError(f"Component '{selector.component}' not valid for FunctionDef")
    raise ValueError(
        message or f"Component '{selector.component}' not found or not valid for symbol {'.'.join(selector.symbol_path)}"
    )


def get_component(selector: ExtendedSelector) -> str:
    """Get value of component.

    Args:
        selector: Extended selector with component specified

    Returns:
        String representation of the component value

    Example:
        >>> sel = parse_extended_selector("file.py::func[params]")
        >>> get_component(sel)
        'ctx, request'

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found, invalid component for symbol type,
                   or accessor not found
    """
    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    from .project_iter import _get_imports, _ext_from_path
    # Handle module-level components (empty symbol_path)
    if not selector.symbol_path:
        if selector.component == "imports":
            return _get_imports(source_code, language=selector.language)
        else:
            raise ValueError(f"Component '{selector.component}' requires a symbol path")

    # Get the range for the component using Rust accelerator
    _ext = _ext_from_path(selector.file_path)
    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=_ext,
    )

    if range_info is None:
        _raise_component_not_found(selector, source_code, _ext)

    start_byte, end_byte = range_info

    # For returns, Rust returns an insertion point if it's not there.
    # get_component should raise error if it's truly not there.
    if selector.component == "returns" and start_byte == end_byte:
         raise ValueError(f"Function {'.'.join(selector.symbol_path)} has no return annotation")

    result = source_code.encode('utf-8')[start_byte:end_byte].decode('utf-8')

    if selector.component == "returns":
        s = result.strip()
        if s.startswith("->"):
            s = s[2:]
        return s.strip()
    elif selector.component == "body":
        return result.strip('\n').rstrip()
    
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
    from .project_iter import _ext_from_path
    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    # Get the range for the component using Rust accelerator
    _ext = _ext_from_path(selector.file_path)
    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=_ext,
    )

    if range_info is None:
        _raise_component_not_found(selector, source_code, _ext)

    start_byte, end_byte = range_info

    # Prepare the replacement value
    replacement = value
    if selector.component == "returns" and value.strip() and not value.strip().startswith("->"):
        replacement = f" -> {value.strip()}"
    elif selector.component == "decorators" and value.strip() and not value.strip().startswith("@"):
        # If it's a single decorator without @, add it
        if "\n" not in value.strip():
            replacement = f"@{value.strip()}"
    elif selector.component == "body":
        # Ensure it starts with a newline and is indented if it's a block
        if not value.startswith("\n"):
            # Simple heuristic: find indentation of the def/class line
            # or just assume 4 spaces
            replacement = "\n    " + value.strip().replace("\n", "\n    ")

    # Apply transformation using Rust FileTransform
    transform = _rust.PyFileTransform(source_code)
    transform.replace_range(start_byte, end_byte, replacement)
    
    new_code = transform.apply()
    if new_code is None:
        raise RuntimeError("Failed to apply transformation (overlapping edits)")

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def add_to_component(
    selector: ExtendedSelector,
    value: str,
    position: int = -1,
    before: str | None = None,
    after: str | None = None,
    apply: bool = False,
    kind: str | None = None
) -> str:
    """Add item to list component. Returns diff."""
    # Validate mutually exclusive position options
    if before is not None and after is not None:
        raise ValueError("Cannot specify both --before and --after")

    # Validate that component is a list type
    if selector.component not in ("params", "decorators", "bases", "imports"):
        raise ValueError(f"Component '{selector.component}' is not a list component")

    # Validate that accessor is None
    if selector.accessor is not None:
        raise ValueError("add_to_component requires accessor must be None")

    # Validate kind parameter
    if kind is not None:
        if selector.component != "params":
            raise ValueError("'kind' parameter can only be used with 'params' component")
        if kind not in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"):
            raise ValueError(f"Invalid kind value: {kind}. Must be one of: POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, KEYWORD_ONLY")

    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    from .project_iter import _add_import_text, _ext_from_path
    # Handle module-level imports component
    if selector.component == "imports" and not selector.symbol_path:
        return _add_import_text(value, position, file_path, apply, source_code, language=selector.language)

    # Get items and their ranges
    _ext = _ext_from_path(selector.file_path)
    items_info = _rust.get_symbol_component_list_items(
        source_code,
        selector.symbol_path,
        selector.component,
        ext=_ext,
    )

    if items_info is None:
        _raise_component_not_found(selector, source_code, _ext)

    # Calculate insertion index in the items list
    items = [item[0] for item in items_info]
    insert_idx = -1

    if before is not None:
        try:
            insert_idx = items.index(before)
        except ValueError:
            raise ValueError(f"{selector.component.capitalize()[:-1]} '{before}' not found")
    elif after is not None:
        try:
            insert_idx = items.index(after) + 1
        except ValueError:
            raise ValueError(f"{selector.component.capitalize()[:-1]} '{after}' not found")
    elif position == -1:
        insert_idx = len(items)
    else:
        insert_idx = position

    # Determine insertion byte offset
    transform = _rust.PyFileTransform(source_code)
    
    # Handle decorators doubling @
    val_to_add = value.strip()
    if selector.component == "decorators" and val_to_add.startswith("@"):
        val_to_add = val_to_add[1:]

    # Insert at insert_idx
    if not items_info:
        # Empty container
        replacement = val_to_add
        if selector.component == "decorators":
            # If adding first decorator, get_symbol_component_range returns the start of 'def'
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
        
        # Get the container range again to be sure
        container_range = _rust.get_symbol_component_range(
            source_code,
            selector.symbol_path,
            selector.component,
            None,
            ext=_ext,
        )
        if container_range is None:
            _raise_component_not_found(
                selector, source_code, _ext,
                message=f"Could not find container for {selector.component}",
            )

        cont_start, cont_end = container_range
        transform.replace_range(cont_start, cont_end, replacement)
    else:
        # Handle parameter kind for existing params
        if selector.component == "params" and (kind or selector.pseudo_class):
            target_kind = kind or selector.pseudo_class
            # Find separators
            pos_only_sep_idx = -1
            kw_only_sep_idx = -1
            star_arg_idx = -1
            star_kwarg_idx = -1
            
            for i, (name, _, _) in enumerate(items_info):
                if name == "/":
                    pos_only_sep_idx = i
                elif name == "*":
                    kw_only_sep_idx = i
                elif name.startswith("**"):
                    star_kwarg_idx = i
                elif name.startswith("*"):
                    star_arg_idx = i
            
            if target_kind == "POSITIONAL_ONLY":
                if pos_only_sep_idx == -1:
                    insert_idx = len(items_info)
                else:
                    insert_idx = min(insert_idx, pos_only_sep_idx)
            elif target_kind == "KEYWORD_ONLY":
                if kw_only_sep_idx == -1 and star_arg_idx == -1:
                    # Insert before **kwargs if it exists
                    if star_kwarg_idx != -1:
                        insert_idx = star_kwarg_idx
                    else:
                        insert_idx = len(items_info)
                    val_to_add = f"*, {val_to_add}"
                else:
                    # Insert after * or after star_arg, but before **kwargs
                    if kw_only_sep_idx != -1:
                        insert_idx = max(insert_idx, kw_only_sep_idx + 1)
                    else:
                        insert_idx = max(insert_idx, star_arg_idx + 1)
                    
                    if star_kwarg_idx != -1:
                        insert_idx = min(insert_idx, star_kwarg_idx)
            elif target_kind == "POSITIONAL_OR_KEYWORD":
                 if kw_only_sep_idx != -1:
                      insert_idx = min(insert_idx, kw_only_sep_idx)
                 elif star_arg_idx != -1:
                      insert_idx = min(insert_idx, star_arg_idx)
                 if pos_only_sep_idx != -1:
                      insert_idx = max(insert_idx, pos_only_sep_idx + 1)

        # Insert at insert_idx
        if insert_idx >= len(items_info):
            # Append
            last_item_end = items_info[-1][2]
            sep = ", "
            if selector.component == "decorators":
                sep = "\n"
                replacement = f"{sep}@{val_to_add}"
            else:
                replacement = f"{sep}{val_to_add}"
            transform.insert_after(last_item_end, replacement)
        elif insert_idx <= 0:
            # Prepend
            first_item_start = items_info[0][1]
            sep = ", "
            if selector.component == "decorators":
                sep = "\n"
                replacement = f"@{val_to_add}{sep}"
            else:
                replacement = f"{val_to_add}{sep}"
            transform.insert_before(first_item_start, replacement)
        else:
            # Insert in between
            target_start = items_info[insert_idx][1]
            sep = ", "
            if selector.component == "decorators":
                sep = "\n"
                replacement = f"@{val_to_add}{sep}"
            else:
                replacement = f"{val_to_add}{sep}"
            transform.insert_before(target_start, replacement)

    new_code = transform.apply()
    if new_code is None:
        raise ValueError(
            f"Failed to add to component '{selector.component}' in "
            f"{'.'.join(selector.symbol_path)}: overlapping byte ranges"
        )

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def remove_component(selector: ExtendedSelector, apply: bool = False) -> str:
    """Remove component or item. Returns diff."""
    from .patterns import remove_symbol
    from .project_iter import _ext_from_path
    # If no component specified, remove the entire symbol
    if selector.component is None:
        return remove_symbol(selector, apply=apply)

    # Validate that body cannot be removed
    if selector.component == "body":
        raise ValueError("Cannot remove body component")

    # Read file
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    source_code = file_path.read_text()

    # Get the range for the component using Rust accelerator
    _ext = _ext_from_path(selector.file_path)
    range_info = _rust.get_symbol_component_range(
        source_code,
        selector.symbol_path,
        selector.component,
        selector.accessor,
        ext=_ext,
    )

    if range_info is None:
        _raise_component_not_found(selector, source_code, _ext)

    start_byte, end_byte = range_info
    
    # Check if we are removing an individual item (accessor is present)
    # or the whole component.
    transform = _rust.PyFileTransform(source_code)
    source_bytes = source_code.encode('utf-8')

    if selector.accessor is not None:
        # Removing an individual item. Need to clean up commas/separators.
        # Check for following comma
        i = end_byte
        while i < len(source_bytes) and source_bytes[i:i+1] in (b' ', b'\t', b'\n', b'\r'):
            i += 1
        
        if i < len(source_bytes) and source_bytes[i:i+1] == b',':
            # Remove from item start through the comma and any following space
            j = i + 1
            while j < len(source_bytes) and source_bytes[j:j+1] in (b' ', b'\t'):
                j += 1
            transform.remove_range(start_byte, j)
        else:
            # Look for preceding comma (skip whitespace AND newlines for
            # multi-line parameter lists where the comma is on a previous line).
            i = start_byte
            while i > 0 and source_bytes[i-1:i] in (b' ', b'\t', b'\n', b'\r'):
                i -= 1

            if i > 0 and source_bytes[i-1:i] == b',':
                # Remove from preceding comma through the item end.
                # Also strip trailing whitespace before the comma on the
                # same line so "x: int , y" → "x: int", not "x: int ".
                j = i - 1
                while j > 0 and source_bytes[j-1:j] in (b' ', b'\t'):
                    j -= 1
                transform.remove_range(j, end_byte)
            else:
                # No comma found, just remove the item
                # For decorators, might need to remove the leading @ or trailing newline
                if selector.component == "decorators":
                    # Heuristic: remove from @ to newline
                    i = start_byte
                    while i > 0 and source_bytes[i-1:i] != b'\n' and source_bytes[i-1:i] != b'\r' and source_bytes[i-1:i] != b'@':
                        i -= 1
                    if i > 0 and source_bytes[i-1:i] == b'@':
                        i -= 1
                    
                    j = end_byte
                    while j < len(source_bytes) and source_bytes[j:j+1] in (b' ', b'\t'):
                        j += 1
                    if j < len(source_bytes) and source_bytes[j:j+1] in (b'\n', b'\r'):
                        j += 1
                        if j < len(source_bytes) and source_bytes[j-1:j+1] == b'\r\n':
                            j += 1
                    transform.remove_range(i, j)
                else:
                    transform.remove_range(start_byte, end_byte)
    else:
        # Removing whole component.
        if selector.component == "returns":
            # get_symbol_component_range for returns includes -> and leading space
            transform.remove_range(start_byte, end_byte)
        elif selector.component == "bases":
            # If removing all bases, we also want to remove parentheses if present.
            # Tree-sitter 'class_definition' has 'superclasses' node which includes parentheses.
            # Look for parentheses around the bases
            i = start_byte
            while i > 0 and source_bytes[i-1:i] in (b' ', b'\t'):
                i -= 1
            
            j = end_byte
            while j < len(source_bytes) and source_bytes[j:j+1] in (b' ', b'\t'):
                j += 1
                
            if i > 0 and source_bytes[i-1:i] == b'(' and j < len(source_bytes) and source_bytes[j:j+1] == b')':
                transform.remove_range(i-1, j+1)
            else:
                transform.remove_range(start_byte, end_byte)
        else:
            transform.remove_range(start_byte, end_byte)

    new_code = transform.apply()
    if new_code is None:
        raise ValueError(
            f"Failed to remove component '{selector.component}' from "
            f"{'.'.join(selector.symbol_path)}: overlapping byte ranges"
        )

    # Generate diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


_CONTENT_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\.content\}")


def _extract_string_content_from_text(text: str, ext: str = "py") -> str | None:
    """Extract the inner content of a string literal from source text.

    For a string like ``"MyClass"`` or ``'MyClass'`` returns ``MyClass``.
    Returns None for non-string text or complex strings that cannot be
    trivially unwrapped (f-strings, concatenated strings).
    """
    from emend import emend_core as _rust
    return _rust.parse_string_literal(text, ext)


