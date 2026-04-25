"""Symbol and module rename/move operations."""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING
import logging
import re

if TYPE_CHECKING:
    from ..component_selector import ExtendedSelector

logger = logging.getLogger(__name__)

def rename_symbol(
    selector: ExtendedSelector,
    new_name: str,
    project_path: str | None = None,
    in_hierarchy: bool = True,
    docs: bool = False,
    unsure: bool = False,
    apply: bool = False,
) -> dict[str, str]:
    """Rename a symbol across the entire project.

    Uses Tree-sitter and PyScopeResolver for scope-aware renaming:
    only renames references that actually refer to the target symbol,
    not coincidental same-named symbols in other scopes or files.

    Args:
        selector: Symbol to rename
        new_name: New name for the symbol
        project_path: Project root (auto-detected if None)
        in_hierarchy: Also rename in class hierarchies
        docs: Also rename in docstrings
        unsure: Rename uncertain occurrences
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes

    Raises:
        ValueError: If symbol not found
    """
    from emend import emend_core as _rust
    from .project_iter import _find_project_root, _normalize_module_qn, _file_to_module, _files_importing_module, visit_project_ts
    from .components import _generate_diff
    from .refs import _rename_in_docstrings
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for rename_symbol")

    # scan_root: where to collect files (respects --project for scope limiting)
    # module_root: project root for computing dotted module names (always git root)
    scan_root = project_path if project_path else _find_project_root(selector.file_path)
    module_root = _find_project_root(selector.file_path)
    resolved_target = str(Path(selector.file_path).resolve())
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))

    # Use fully qualified name for matching
    target_qn = f"{target_module}.{symbol_name}" if target_module else symbol_name

    # Use import graph to pre-filter files
    language = selector.language
    candidates = _files_importing_module(scan_root, target_module, language=language)

    diffs = {}
    for py_file, content, resolver in visit_project_ts(
        name_hint=symbol_name,
        project_path=scan_root,
        target_file=resolved_target,
        candidate_files=candidates,
        target_qnames={target_qn},
        language=language,
    ):
        references = resolver.references_in_file(py_file)
        transform = _rust.PyFileTransform(content)
        changed = False

        for qn, line, col, offset, end_offset, kind, _ann in references:
            if qn == target_qn:
                # Check if the text at the position matches symbol_name
                # (to avoid renaming aliases or coincidental names in attributes)
                # Now using end_offset for better precision!
                if content[offset:end_offset].endswith(symbol_name):
                    transform.replace_range(end_offset - len(symbol_name), end_offset, new_name)
                    changed = True

        if not changed:
            continue

        new_content = transform.apply()
        if new_content is None:
            continue

        # Apply docstring renaming if requested -- but only in files where
        # the scope-aware code rename found changes.
        if docs:
            docs_result = _rename_in_docstrings(new_content, symbol_name, new_name, language=language)
            if docs_result is not None:
                new_content = docs_result

        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    return diffs


def move_symbol(
    selector: ExtendedSelector,
    dest_file: str,
    position: str = "end",
    dedent: bool = False,
    update_imports: bool = True,
    project_path: str | None = None,
    apply: bool = False,
) -> dict[str, str]:
    """Move a symbol to another file with import updates.

    1. Copies the symbol to the destination file
    2. Removes the symbol from the source file
    3. Updates all import statements that reference the symbol

    Args:
        selector: Symbol to move
        dest_file: Destination file path
        position: Where to insert ("start" or "end")
        dedent: If True, dedent the source code to remove common indentation
        update_imports: If True, update imports across project
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes

    Raises:
        ValueError: If symbol not found
    """
    from .project_iter import _file_to_module, _find_project_root, _normalize_module_qn, _files_importing_module, visit_project_ts
    from .patterns import copy_symbol, remove_symbol
    from .components import _generate_diff
    diffs = {}
    symbol_name = selector.symbol_path[-1] if selector.symbol_path else None
    if not symbol_name:
        raise ValueError("Symbol path is required for move_symbol")

    # Compute source module name so that locally-defined symbols referenced by
    # the moved symbol get ``from source_module import Name`` statements added
    # to the destination file (issue #138 Bug 2).
    source_module = _file_to_module(selector.file_path, project_path)

    # Before removing the symbol, use the tree-sitter scope resolver to check
    # whether the source file has non-definition/non-import references to the
    # moved symbol (e.g. calls, type annotations).  After removal the name
    # becomes unresolved and the resolver can no longer see it.
    source_has_other_refs = _source_has_remaining_refs(
        selector.file_path, symbol_name, project_path,
    )

    # Step 1: Copy symbol to destination (include_imports=True so the moved
    # symbol carries its own import dependencies into the destination file,
    # fixing issue #138 Bug 2).
    copy_diff = copy_symbol(
        selector, dest_file, position=position, dedent=dedent,
        include_imports=True, source_module=source_module,
        project_path=project_path, apply=apply,
    )
    diffs[dest_file] = copy_diff

    # Step 2: Remove from source
    remove_diff = remove_symbol(selector, apply=apply)
    diffs[selector.file_path] = remove_diff

    # Step 3: Update imports if requested
    if update_imports:
        import_diffs = _update_imports_for_move(
            selector.file_path,
            dest_file,
            symbol_name,
            project_path,
            apply=apply,
            source_has_other_refs=source_has_other_refs,
        )
        diffs.update(import_diffs)

    return diffs


def _source_has_remaining_refs(
    source_file: str,
    symbol_name: str,
    project_path: str | None,
) -> bool:
    """Check whether *source_file* references *symbol_name* outside its definition.

    Uses the tree-sitter scope resolver on the **current** (pre-removal) content
    so that all references are still resolvable.  Returns True when there are
    read/write/call references to the symbol beyond its own definition and
    import sites — meaning the source file will need an import after the symbol
    is removed.
    """
    from emend import emend_core as _rust
    from .project_iter import _find_project_root
    source_path = Path(source_file)
    try:
        content = source_path.read_text()
    except FileNotFoundError:
        return False

    proj_root = str(
        Path(project_path or _find_project_root(source_file)).resolve()
    )
    ext = source_path.suffix.lstrip(".")
    resolver = _rust.PyScopeResolver(proj_root, ext)
    resolved = str(source_path.resolve())
    resolver.index_file(resolved, content)

    target_suffix = f".{symbol_name}"
    return any(
        kind in ("read", "write", "call")
        for qn, _line, _col, _off, _end, kind, _ann
        in resolver.references_in_file(resolved)
        if qn.endswith(target_suffix) or qn == symbol_name
    )


def _split_or_retarget_import(
    content: str,
    py_file: str,
    source_module: str,
    dest_module: str,
    symbol_name: str,
    resolver: object = None,
) -> str | None:
    """Rewrite ``from source_module import ...`` statements for a symbol move.

    For each ``from source_module import A, B, C`` where ``symbol_name`` is one
    of the names:

    * If ``symbol_name`` is the *only* name, simply change the module:
      ``from dest_module import symbol_name``.
    * If there are *other* names in the statement, split it into two separate
      import lines so that sibling names are not inadvertently retargeted to
      ``dest_module`` (issue #138 Bug 1).

    Returns the rewritten file content string, or ``None`` if no change was
    needed.
    """
    structured_imports = resolver.structured_imports_in_file(py_file)

    original_content = content
    lines = content.splitlines(keepends=True)

    # Precompute cumulative line offsets for O(1) lookup per import.
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line))

    # Collect (stmt_start_byte, stmt_end_byte, replacement_text) tuples;
    # applied in reverse order to preserve earlier byte offsets.
    replacements: list[tuple[int, int, str]] = []

    for imp in structured_imports:
        if imp["is_plain"]:
            continue
        if imp["module"] != source_module:
            continue
        if imp["level"]:
            continue

        names = imp["names"]
        moved_aliases = [(n, a) for n, a in names if n == symbol_name]
        if not moved_aliases:
            continue

        remaining_aliases = [(n, a) for n, a in names if n != symbol_name]

        def _alias_str(name: str, alias: str | None) -> str:
            if alias:
                return f"{name} as {alias}"
            return name

        # Preserve the indentation of the original import statement.
        start_line = imp["start_line"]
        orig_line = lines[start_line - 1] if start_line - 1 < len(lines) else ""
        indent = orig_line[: len(orig_line) - len(orig_line.lstrip())]

        moved_line = (
            f"{indent}from {dest_module} import "
            + ", ".join(_alias_str(n, a) for n, a in moved_aliases)
        )

        if remaining_aliases:
            remaining_line = (
                f"{indent}from {source_module} import "
                + ", ".join(_alias_str(n, a) for n, a in remaining_aliases)
            )
            replacement = moved_line + "\n" + remaining_line
        else:
            replacement = moved_line

        stmt_start = line_offsets[imp["start_line"] - 1]
        stmt_end = line_offsets[imp["end_line"]]
        replacements.append((stmt_start, stmt_end, replacement + "\n"))

    if not replacements:
        return None

    replacements.sort(key=lambda x: x[0], reverse=True)
    for start, end, repl in replacements:
        content = content[:start] + repl + content[end:]

    return content if content != original_content else None


def _update_imports_for_move(
    source_file: str,
    dest_file: str,
    symbol_name: str,
    project_path: str | None,
    apply: bool,
    source_has_other_refs: bool = False,
) -> dict[str, str]:
    """Update imports across project when a symbol moves."""
    from emend import emend_core as _rust
    from .project_iter import _file_to_module, _find_project_root, visit_project_ts
    from .components import _generate_diff
    diffs = {}

    # Get module names
    source_module = _file_to_module(source_file, project_path)
    dest_module = _file_to_module(dest_file, project_path)

    # Resolve skip paths
    resolved_source = str(Path(source_file).resolve())
    resolved_dest = str(Path(dest_file).resolve())
    proj_root = _find_project_root(project_path or source_file)

    target_qn = f"{source_module}.{symbol_name}"
    from emend.language_registry import detect_language
    language = detect_language(source_file) or "python"

    for py_file, content, resolver in visit_project_ts(
        name_hint=symbol_name,
        project_path=proj_root,
        language=language,
    ):
        resolved_py = str(Path(py_file).resolve())
        if resolved_py == resolved_source or resolved_py == resolved_dest:
            continue

        changed = False

        # Use tree-sitter to handle multi-name imports correctly.
        # When the consumer has 'from source_mod import A, B' and only A is
        # moved, we must split the statement instead of rewriting just the
        # module name (which would drag B to dest_mod — issue #138 Bug 1).
        new_content = _split_or_retarget_import(
            content, py_file, source_module, dest_module, symbol_name,
            resolver=resolver,
        )
        if new_content is not None and new_content != content:
            changed = True
        else:
            # Fallback: handle dotted 'import source_module.symbol_name' style
            # that the AST splitter does not cover.
            transform = _rust.PyFileTransform(content)

            references = resolver.references_in_file(py_file)

            for i, (qn, line, col, offset, end_offset, kind, _ann) in enumerate(references):
                if kind != "import":
                    continue

                if qn == target_qn:
                    # Only handle 'import source_module.symbol_name' (dotted)
                    # style; 'from source_module import ...' is handled above.
                    if content[offset : offset + len(source_module)] == source_module:
                        transform.replace_range(
                            offset, offset + len(source_module), dest_module
                        )
                        changed = True

            if changed:
                new_content = transform.apply()
            else:
                new_content = None

        if not changed or new_content is None or new_content == content:
            continue

        diff = _generate_diff(py_file, content, new_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(new_content)

    # If the source file has read/write/call references to the moved symbol
    # (detected by the caller via tree-sitter scope resolver on pre-removal
    # content), add an import so the source file doesn't break at runtime.
    if source_has_other_refs and dest_module:
        try:
            source_content = Path(source_file).read_text()
        except FileNotFoundError:
            source_content = None

        if source_content is not None:
            import_stmt = f"from {dest_module} import {symbol_name}"
            from emend.language_plugins import load_plugin
            try:
                new_source_content = load_plugin(language).import_handler.add_import_text(
                    import_stmt, 0, source_content
                )
            except Exception:
                new_source_content = None

            if new_source_content and new_source_content != source_content:
                diff = _generate_diff(source_file, source_content, new_source_content)
                diffs[source_file] = diff
                if apply:
                    Path(source_file).write_text(new_source_content)

    return diffs


def _resolve_relative_import_qn(
    qn: str,
    file_path: str,
    project_root: str,
    sep: str = ".",
    src_text: str | None = None,
) -> str | None:
    """Resolve a relative-import QN like ``.models`` to an absolute QN like ``pkg.models``.

    The Rust resolver emits QNs such as ``.models`` for ``from .models import X``
    and ``..util`` for ``from ..util import Y``.  We resolve these by computing the
    containing package from the file path.

    For ``from . import X`` style imports (bare name after dot-only relative), the
    Rust resolver adds an extra separator dot to the QN (e.g. ``..models`` instead
    of ``.models``).  When *src_text* is provided and does not start with a dot,
    we compensate by reducing the dot count.

    Returns the absolute module QN, or ``None`` if resolution fails.
    """
    from .project_iter import _file_to_module
    if not qn.startswith("."):
        return None

    dot_count = len(qn) - len(qn.lstrip("."))
    relative_part = qn[dot_count:]

    # For ``from . import X`` the source text is just the bare name (no dots),
    # but the Rust resolver produces QN ``..X`` with an extra separator dot.
    # Compensate so that the dot count reflects the actual import level.
    if src_text is not None and not src_text.startswith("."):
        dot_count = max(1, dot_count - 1)

    module = _file_to_module(file_path, project_root)
    package = module.rsplit(".", 1)[0] if "." in module else None
    parts = package.split(sep) if package else []

    levels_up = dot_count - 1
    if levels_up > len(parts):
        return None

    base_parts = parts[: len(parts) - levels_up] if levels_up else parts
    if relative_part:
        return sep.join(base_parts + [relative_part]) if base_parts else relative_part
    else:
        return sep.join(base_parts) if base_parts else None


def _replace_module_in_strings(
    content: str,
    old_module: str,
    new_module: str,
    full_name_only: bool = False,
    file_path: str = "_.py",
    language: str = "python",
) -> str:
    """Replace occurrences of old_module inside string literals in *content*.

    Uses tree-sitter to identify string literal nodes (via the
    ``{type: string, value: null}`` any-string pattern), so comments and
    non-string contexts are correctly ignored regardless of language.

    Handles:
    - Full dotted module path inside strings (for ``importlib.import_module("pkg.models")``).
    - Bare module name when it is the entire string content (for ``__all__``
      entries like ``"models"``) — only when *full_name_only* is False.

    When *full_name_only* is True, only the full dotted module path is replaced.
    This avoids false positives when scanning files that have no import
    relationship with the module (e.g. an unrelated ``TABLE = "models"``).
    """
    import re as _re
    from emend import emend_core as _rust
    from emend.language_registry import get_extensions

    exts = get_extensions(language)
    ext = exts[0] if exts else Path(file_path).suffix.lstrip(".")
    any_string_ir: dict = {"type": "string", "value": None}
    matches = _rust.find_pattern_in_files(
        [(file_path, content)], any_string_ir, extension=ext,
    )

    old_bare = old_module.rsplit(".", 1)[-1]
    new_bare = new_module.rsplit(".", 1)[-1]

    lines = content.splitlines(keepends=True)

    # Collect (char_start, char_end, replacement) tuples.
    replacements: list[tuple[int, int, str]] = []

    for _file, start_line, start_col, end_line, end_col, text, _caps in matches:
        char_start = sum(len(lines[i]) for i in range(start_line - 1)) + start_col
        char_end = sum(len(lines[i]) for i in range(end_line - 1)) + end_col

        # Determine the string's inner content (without surrounding quotes).
        if text[:3] in ('"""', "'''"):
            inner = text[3:-3]
        else:
            inner = text[1:-1]

        new_text = text
        # Replace full dotted module path (most specific).
        if old_module in text:
            new_text = re.sub(
                r'(?<![.\w])' + re.escape(old_module) + r'(?![.\w])',
                new_module,
                text,
            )
        # Replace bare module name only when it is the entire string content.
        elif not full_name_only and inner == old_bare:
            if text[:3] in ('"""', "'''"):
                new_text = text[:3] + new_bare + text[-3:]
            else:
                new_text = text[0] + new_bare + text[-1]

        if new_text != text:
            replacements.append((char_start, char_end, new_text))

    if not replacements:
        return content

    # Apply in reverse order to preserve earlier offsets.
    replacements.sort(key=lambda x: x[0], reverse=True)
    for start, end, repl in replacements:
        content = content[:start] + repl + content[end:]

    return content


def _rename_module_references(
    project_root: str,
    old_module: str,
    new_module: str,
    apply: bool,
    language: str = "python",
) -> dict[str, str]:
    """Update all imports from old_module to new_module across the project."""
    from emend import emend_core as _rust
    from emend.language_registry import get_module_separator
    from .project_iter import visit_project_ts
    from .components import _generate_diff
    diffs = {}

    sep = get_module_separator(language)

    # hint for structural filter
    name_hint = old_module.rsplit(sep, 1)[-1]

    for py_file, content, resolver in visit_project_ts(
        name_hint=name_hint,
        project_path=project_root,
        language=language,
    ):
        transform = _rust.PyFileTransform(content)
        changed = False

        old_bare_mod = old_module.rsplit(sep, 1)[-1]
        new_bare_mod = new_module.rsplit(sep, 1)[-1]

        for qn, line, col, offset, end_offset, kind, _ann in resolver.references_in_file(py_file):
            # Resolve relative QNs (e.g. ".models" -> "pkg.models") so that
            # the comparison against old_module works correctly.
            resolved_qn = qn
            src_text = content[offset:end_offset]
            if qn.startswith("."):
                resolved = _resolve_relative_import_qn(qn, py_file, project_root, sep, src_text=src_text)
                if resolved is not None:
                    resolved_qn = resolved

            if kind == "import":
                # Exact match: import old_module or from old_module import ...
                if resolved_qn == old_module:
                    if qn.startswith(".") and resolved_qn != qn:
                        # Relative import: preserve leading dots, replace only the module name.
                        if src_text.startswith("."):
                            # Text includes dots (e.g. ``from .models import VALUE``).
                            dot_count = len(qn) - len(qn.lstrip("."))
                            new_relative = "." * dot_count + new_bare_mod
                        else:
                            # Bare name (e.g. ``from . import models``); dots are in
                            # the ``from .`` part, not in the captured text span.
                            new_relative = new_bare_mod
                        transform.replace_range(offset, end_offset, new_relative)
                    else:
                        transform.replace_range(offset, end_offset, new_module)
                    changed = True
                # Prefix match: import old_module.sub or from old_module.sub import ...
                elif resolved_qn.startswith(old_module + sep):
                    prefix_len = len(old_module)
                    if content[offset : offset + prefix_len] == old_module:
                        transform.replace_range(offset, offset + prefix_len, new_module)
                        changed = True
                    elif qn.startswith(".") and resolved_qn != qn:
                        # Relative sub-module import: replace old bare name at offset.
                        dot_count = len(qn) - len(qn.lstrip("."))
                        relative_module_part = qn[dot_count:]
                        if relative_module_part == old_bare_mod or relative_module_part.startswith(old_bare_mod + sep):
                            if content[offset : offset + len(old_bare_mod)] == old_bare_mod:
                                transform.replace_range(offset, offset + len(old_bare_mod), new_bare_mod)
                                changed = True

            elif kind in ("read", "write"):
                # Attribute access through a module binding, e.g. ``models.VALUE``
                # after ``from . import models``.  The bare module name in the
                # source text must be updated to match the new module name.
                if resolved_qn == old_module and src_text == old_bare_mod:
                    transform.replace_range(offset, end_offset, new_bare_mod)
                    changed = True

        # Check if string literals might contain the old module name.
        old_bare_name = old_module.rsplit(sep, 1)[-1]
        strings_may_need_update = old_module in content or old_bare_name in content

        if changed:
            final_content = transform.apply() or content
            if strings_may_need_update:
                final_content = _replace_module_in_strings(
                    final_content, old_module, new_module,
                    file_path=py_file, language=language,
                )
        elif strings_may_need_update:
            final_content = _replace_module_in_strings(
                content, old_module, new_module,
                file_path=py_file, language=language,
            )
        else:
            continue

        if final_content == content:
            continue

        diff = _generate_diff(py_file, content, final_content)
        diffs[py_file] = diff

        if apply:
            Path(py_file).write_text(final_content)

    # Third pass: string-literal replacements in files that the structural pre-filter
    # may have excluded (e.g. files with importlib.import_module("pkg.models") but no
    # import statement mentioning "models" as an identifier that tree-sitter picks up).
    #
    # Only match the full dotted module name here — NOT the bare name.  Files
    # processed in the first/second pass already get bare-name string updates
    # (for __all__ entries etc.), but files with no import relationship should
    # not have coincidental bare-name strings like TABLE = "models" rewritten.
    from .project_iter import _collect_source_files
    already_processed = set(diffs.keys())
    all_source_files = _collect_source_files(project_root, language=language)
    for py_file in all_source_files:
        if py_file in already_processed:
            continue
        try:
            content = Path(py_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Quick substring check before the heavier tree-sitter scan.
        if old_module not in content:
            continue
        final_content = _replace_module_in_strings(
            content, old_module, new_module, full_name_only=True,
            file_path=py_file, language=language,
        )
        if final_content == content:
            continue
        diff = _generate_diff(py_file, content, final_content)
        diffs[py_file] = diff
        if apply:
            Path(py_file).write_text(final_content)

    return diffs


def move_module(
    source_path: str,
    destination: str,
    project_path: str | None = None,
    apply: bool = False
) -> dict[str, str]:
    """Move a module to another package, updating imports.

    Args:
        source_path: Path to module file to move
        destination: Destination package path like 'pkg.subpkg' or folder path
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes
    """
    import shutil
    import os
    from .project_iter import _find_project_root, _file_to_module

    project_root = _find_project_root(project_path or source_path)
    old_module = _file_to_module(source_path, project_root)

    # Resolve destination to a directory path
    if '.' in destination and not os.path.isdir(destination):
        # Dotted module path like "pkg.subpkg"
        dest_dir = Path(project_root) / Path(destination.replace('.', '/'))
    else:
        # Could be a relative path or absolute path
        dest_dir_candidate = Path(destination)
        if not dest_dir_candidate.is_absolute():
            dest_dir = Path(project_root) / dest_dir_candidate
        else:
            dest_dir = dest_dir_candidate

    # New file location
    new_path = dest_dir / Path(source_path).name
    new_module = _file_to_module(str(new_path), project_root)

    # Update all imports across project
    from emend.language_registry import detect_language
    language = detect_language(source_path) or "python"
    diffs = _rename_module_references(project_root, old_module, new_module, apply, language=language)

    if apply:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(new_path))
        return {}

    # For dry-run, describe the file move
    description = f"Move {source_path} -> {new_path}"
    diffs["__description__"] = description
    return diffs


def rename_module(
    file_path: str,
    new_name: str,
    project_path: str | None = None,
    apply: bool = False
) -> dict[str, str]:
    """Rename a module file, updating imports across the project.

    Args:
        file_path: Path to module file to rename
        new_name: New name for the module (without .py extension)
        project_path: Project root (auto-detected if None)
        apply: If True, write changes. If False, return diffs only.

    Returns:
        Dict mapping file_path -> unified diff of changes
    """
    from .project_iter import _find_project_root, _file_to_module
    from emend.language_registry import detect_language, get_module_separator
    project_root = _find_project_root(project_path or file_path)
    old_module = _file_to_module(file_path, project_root)
    language = detect_language(file_path) or "python"
    sep = get_module_separator(language)

    parts = old_module.rsplit(sep, 1)
    new_module = f"{parts[0]}{sep}{new_name}" if len(parts) > 1 else new_name

    diffs = _rename_module_references(project_root, old_module, new_module, apply, language=language)

    ext = Path(file_path).suffix
    if apply:
        new_path = Path(file_path).parent / f"{new_name}{ext}"
        Path(file_path).rename(new_path)
        return {}

    # For dry-run, describe the file rename
    new_path = Path(file_path).parent / f"{new_name}{ext}"
    description = f"Rename {file_path} -> {new_path}"
    diffs["__description__"] = description
    return diffs


# ============================================================================
# Unified Commands (lookup, edit) - simplified interface combining multiple
# commands with convenient aliases
# ============================================================================

