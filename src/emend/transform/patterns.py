"""Pattern matching, find, replace, copy, and symbol source utilities."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import logging
import re

from ..component_selector import ExtendedSelector
from ..pattern import (
    parse_pattern,
    compile_pattern_to_rust_ir,
    compile_constraint_to_rust_ir,
    Pattern,
    is_oracle_type_constraint,
    parse_oracle_type_constraint,
)
from emend import emend_core as _rust
from .components import _CONTENT_REF_RE, _extract_string_content_from_text

if TYPE_CHECKING:
    from ..type_oracle import TypeOracle

logger = logging.getLogger(__name__)

@dataclass
class PatternMatch:
    """Represents a match of a pattern in code."""
    node_text: str | None
    captures: dict[str, str]
    line: int | None = None
    matched_text: str | None = None
    end_line: int | None = None
    col: int | None = None
    end_col: int | None = None



def _filter_matches_by_import(
    matches: list[PatternMatch],
    imported_from: str,
    file_path: str,
    project_root: str,
    content: str,
) -> list[PatternMatch]:
    """Post-filter pattern matches to only include those where the root name
    is imported from the specified module.

    Uses PyScopeResolver to resolve the qualified name of the leftmost
    name in each match and verifies it matches the target module.
    """
    if not matches:
        return []

    # Use a single resolver per file for efficiency
    resolver = _rust.PyScopeResolver(project_root)
    resolver.index_file(file_path, content)

    filtered = []
    for match in matches:
        # Extract the root name from the matched node
        # For simplicity, we use the first identifier in the matched text
        _root_match = re.search(r"[a-zA-Z_]\w*", match.node_text or "")
        root_name = _root_match.group(0) if _root_match else None
        if not root_name:
            continue

        # Resolve QN at match position
        references = resolver.references_in_file(file_path)
        
        match_qn = None
        for qn, line, col, offset, end_offset, kind, _ann in references:
            if line == match.line and col == match.col:
                match_qn = qn
                break
        
        if match_qn and match_qn.startswith(f"{imported_from}."):
            filtered.append(match)
        elif match_qn == imported_from:
            filtered.append(match)

    return filtered


def _filter_matches_by_scope_local(
    matches: list[PatternMatch],
    file_path: str,
    project_root: str,
    content: str,
) -> list[PatternMatch]:
    """Post-filter pattern matches to only include those where the root name
    is locally defined (not imported).

    Uses PyScopeResolver to check the origin of each match.
    """
    if not matches:
        return []

    resolver = _rust.PyScopeResolver(project_root)
    resolver.index_file(file_path, content)

    # Build a set of names that are imported (defined via import statements).
    imported_names: set[str] = set()
    references = resolver.references_in_file(file_path)
    for qn, line, col, offset, end_offset, kind, _ann in references:
        if kind == "import":
            # Extract the local name from the qualified name
            # (e.g., "os.path.join" → "join")
            local_name = qn.rsplit(".", 1)[-1] if "." in qn else qn
            imported_names.add(local_name)

    filtered = []
    for match in matches:
        _root_match = re.search(r"[a-zA-Z_]\w*", match.node_text or "")
        root_name = _root_match.group(0) if _root_match else None
        if not root_name:
            continue

        if root_name not in imported_names:
            filtered.append(match)

    return filtered


def _filter_matches_by_type_oracle(
    matches: list[PatternMatch],
    constraints: dict[str, tuple[str, str]],
    type_oracle: TypeOracle,
    file_path: str,
) -> list[PatternMatch]:
    """Post-filter pattern matches using inferred types from TypeOracle.

    Filters each match based on metavar type constraints (e.g., :type[X] or :returns[X]).
    """
    if not matches:
        return []

    from pathlib import Path
    from emend.type_oracle import parse_type_string

    # Get type info for the file
    file_types = type_oracle.infer_file(Path(file_path))

    # Read source to find capture positions
    source_lines = Path(file_path).read_text().splitlines()

    filtered = []
    for match in matches:
        keep = True
        for metavar_name, (kind, type_str) in constraints.items():
            captured_text = match.captures.get(metavar_name)
            if captured_text is None:
                keep = False
                break

            # Find the position of the captured text within the match
            match_line = match.line
            if match_line is None or match_line < 1:
                keep = False
                break

            # Look up type binding at the match position
            # Try to find the captured name in the source line
            line_idx = match_line - 1
            if line_idx >= len(source_lines):
                keep = False
                break

            line_text = source_lines[line_idx]
            col = line_text.find(captured_text)
            if col < 0:
                keep = False
                break

            binding = file_types.type_at(match_line, col + 1)  # 1-indexed col
            if binding is None:
                keep = False
                break

            if kind == "type":
                constraint_td = parse_type_string(type_str)
                if not binding.type_descriptor.matches(constraint_td):
                    keep = False
                    break
            elif kind == "returns":
                # For returns constraint, check the return type
                constraint_td = parse_type_string(type_str)
                ret_type = binding.type_descriptor.return_type
                if ret_type is None or not ret_type.matches(constraint_td):
                    keep = False
                    break

        if keep:
            filtered.append(match)

    return filtered


def find_pattern(
    pattern_str: str,
    file_path: str,
    scope: list[str] | None = None,
    inside: str | None = None,
    not_inside: str | None = None,
    imported_from: str | None = None,
    where: str | None = None,
    scope_local: bool = False,
    source_override: str | None = None,
    type_oracle: "TypeOracle | None" = None,
    language: str = "python",
) -> list[PatternMatch]:
    """Find all matches of pattern in file.

    Args:
        pattern_str: Pattern string with metavariables like "print($X)"
        file_path: Path to source file to search
        scope: Optional symbol path to limit matches to (e.g., ["MyClass", "method"])
        inside: Optional constraint - only match inside this structure.
        not_inside: Optional constraint - only match outside this structure.
        imported_from: Optional module name - only match when the root name
                       in the pattern is imported from this module
        where: Optional constraint - only match inside a structure matching
               this pattern (e.g., 'class MyClass', 'def test_*').
               Alias for inside with pattern support.
        scope_local: If True, only match names that are locally defined
                     (not imported).
        source_override: If provided, search this source string instead of reading from file_path.
        type_oracle: Optional TypeOracle instance for :type[X] and :returns[X] constraints.

    Returns:
        List of matches with locations and captured values
    """
    # Handle --where as alias for --inside
    if where is not None:
        if inside is not None:
            raise ValueError("Cannot specify both 'where' and 'inside' parameters")
        inside = where

    # Validate inside/not_inside constraints
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")

    # Parse pattern
    pattern = parse_pattern(pattern_str)

    # Read file (or use source_override)
    if source_override is not None:
        source_code = source_override
    else:
        file = Path(file_path)
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        source_code = file.read_text()

    # Auto-detect language from file extension when caller used the default
    if language == "python" and file_path:
        from emend.language_registry import detect_language
        detected = detect_language(file_path)
        if detected:
            language = detected

    # Compile pattern and constraints to Rust IR
    rust_ir = compile_pattern_to_rust_ir(pattern_str, language=language)
    if rust_ir is None:
        raise ValueError(f"Pattern '{pattern_str}' could not be compiled to Rust IR")

    inside_ir = compile_constraint_to_rust_ir(inside, language=language) if inside else None
    not_inside_ir = compile_constraint_to_rust_ir(not_inside, language=language) if not_inside else None
    
    if inside and inside_ir is None:
        raise ValueError(f"Unknown inside/not_inside constraint: '{inside}'")
    if not_inside and not_inside_ir is None:
        raise ValueError(f"Unknown inside/not_inside constraint: '{not_inside}'")

    # Find matches using Rust engine
    ext = Path(file_path).suffix.lstrip('.') if file_path else None
    # print(f"DEBUG: find_pattern ext={ext} ir={rust_ir}")
    raw_matches = _rust.find_pattern_in_files(
        [(str(file_path), source_code)], rust_ir, inside_ir, not_inside_ir,
        extension=ext
    )


    matches = []
    for m in raw_matches:
        captures = {k: v for k, v in m[6].items() if k != "_"}
        matches.append(PatternMatch(
            node_text=m[5],
            captures=captures,
            line=m[1],
            col=m[2],
            end_line=m[3],
            end_col=m[4],
            matched_text=m[5],
        ))

    # Post-filter by scope if requested
    if scope is not None:
        from emend.ast_utils import find_nested_definitions, find_symbol_by_path
        symbols = find_nested_definitions(file_path)
        target_sym = find_symbol_by_path(symbols, scope)
        if target_sym:
            matches = [m for m in matches if m.line is not None and target_sym.line_start <= m.line <= target_sym.line_end]
        else:
            matches = []

    # Post-filter by import origin if requested
    if imported_from is not None:
        from .project_iter import _find_project_root
        project_root = _find_project_root(file_path)
        matches = _filter_matches_by_import(
            matches, imported_from, file_path, project_root, source_code
        )

    # Post-filter by scope locality if requested
    if scope_local:
        from .project_iter import _find_project_root
        project_root = _find_project_root(file_path)
        matches = _filter_matches_by_scope_local(
            matches, file_path, project_root, source_code
        )

    # Post-filter by TypeOracle type constraints
    if type_oracle is not None:
        oracle_constraints = {}
        for mv in pattern.metavars:
            if is_oracle_type_constraint(mv.type_constraint):
                oracle_constraints[mv.name] = parse_oracle_type_constraint(mv.type_constraint)
        if oracle_constraints:
            matches = _filter_matches_by_type_oracle(
                matches, oracle_constraints, type_oracle, file_path
            )

    return matches


def remove_symbol(
selector: ExtendedSelector, apply: bool = False) -> str:
    """Remove a symbol (function, class) from a file.

    Args:
        selector: Extended selector specifying the symbol to remove
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found
    """
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    # Use tree-sitter symbols to find the target symbol's range
    from emend.ast_utils import find_nested_definitions, find_symbol_by_path
    symbols = find_nested_definitions(str(file_path))
    sym = find_symbol_by_path(symbols, selector.symbol_path)
    
    if sym is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    # Read original source
    source_code = file_path.read_text()
    lines = source_code.splitlines(keepends=True)
    
    # Symbols in tree-sitter include decorators if they are part of a decorated_definition.
    # Our NestedSymbol uses decorator_line_start if decorators are present.
    start_line = sym.decorator_line_start if sym.decorator_line_start is not None else sym.line_start
    
    # Remove the specified lines (1-indexed)
    # We want to remove the range [start_line, sym.line_end]
    start_idx = start_line - 1
    end_idx = sym.line_end
    
    new_lines = lines[:start_idx] + lines[end_idx:]
    new_code = "".join(new_lines)

    # Generate diff
    from .components import _generate_diff
    diff = _generate_diff(selector.file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file_path.write_text(new_code)

    return diff


def get_symbol_source(selector: ExtendedSelector, dedent: bool = False) -> str:
    """Get the complete source code of a symbol including decorators.

    Args:
        selector: Extended selector specifying the symbol
        dedent: If True, remove leading indentation

    Returns:
        String containing the complete source code of the symbol

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If symbol not found
    """
    file_path = Path(selector.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {selector.file_path}")

    # Handle line-based selectors (file.py:42 or file.py:10-20)
    if selector.line_start is not None:
        # Read the lines directly
        with open(file_path) as f:
            lines = f.readlines()

        # Extract the specified lines (1-indexed)
        start_idx = selector.line_start - 1
        end_idx = (selector.line_end or selector.line_start) - 1

        if start_idx < 0 or end_idx >= len(lines):
            raise ValueError(f"Line range {selector.line_start}-{selector.line_end or selector.line_start} out of bounds")

        code = ''.join(lines[start_idx:end_idx + 1])

        if dedent:
            import textwrap
            code = textwrap.dedent(code)

        return code

    # Handle symbol-based selectors
    from emend.ast_utils import find_nested_definitions, find_symbol_by_path
    symbols = find_nested_definitions(str(file_path))
    sym = find_symbol_by_path(symbols, selector.symbol_path)
    
    if sym is None:
        raise ValueError(f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}")

    # Extract source lines
    source_code = file_path.read_text()
    lines = source_code.splitlines(keepends=True)
    
    # Symbols in tree-sitter include decorators if they are part of a decorated_definition.
    # Our NestedSymbol uses decorator_line_start if decorators are present.
    start_line = sym.decorator_line_start if sym.decorator_line_start is not None else sym.line_start
    
    # line numbers are 1-indexed
    symbol_lines = lines[start_line - 1 : sym.line_end]
    code = "".join(symbol_lines)

    # We ALWAYS dedent here because we extracted raw lines from a potentially
    # indented context (e.g. a method in a class). The parser returns positions
    # relative to the node's own start, which is effectively dedented.
    import textwrap
    code = textwrap.dedent(code)

    # If the explicit dedent flag is True, we've already done it above.
    # The expected behavior is that get_symbol_source(selector) returns
    # dedented code for the symbol.
    
    # Ensure it ends with exactly one newline to match expected test behavior
    if not code.endswith("\n"):
        code += "\n"

    return code


def _collect_name_contexts(source: str) -> tuple[set[str], set[str]]:
    """Return ``(runtime_names, annotation_names)`` used in *source*.

    ``annotation_names`` only includes names that appear in annotation
    positions.  ``runtime_names`` includes names referenced in executable
    positions, including decorators, bases, defaults, and function bodies.

    Uses tree-sitter's annotation_fields config to classify identifiers.
    """
    resolver = _rust.PyScopeResolver("/tmp", "py")
    identifiers = resolver.collect_identifiers_from_source(source)

    runtime_names: set[str] = set()
    annotation_names: set[str] = set()

    for name, in_annotation in identifiers:
        if in_annotation:
            annotation_names.add(name)
        else:
            runtime_names.add(name)

    return runtime_names, annotation_names


def _resolve_relative_module(
    level: int,
    module: str,
    source_file: str,
    project_path: str | None,
) -> str:
    """Convert a relative import into an absolute module path.

    ``level`` is the number of leading dots (``stmt.level`` from AST),
    ``module`` is the dotted name after the dots (may be empty for
    ``from . import X``), and ``source_file`` is the file containing the
    import.

    Returns the fully-qualified module name, or falls back to ``module``
    unchanged if resolution is not possible.
    """
    if project_path is None:
        return module

    from .project_iter import _file_to_module
    src_module = _file_to_module(source_file, project_path)
    # Compute the package that owns source_file.
    if src_module.endswith(".__init__"):
        # __init__.py IS the package.
        package = src_module[: -len(".__init__")]
    elif "." in src_module:
        package = src_module.rsplit(".", 1)[0]
    else:
        package = ""

    parts = package.split(".") if package else []
    # ``from . import X`` has level=1, meaning current package (0 levels up).
    # ``from .. import X`` has level=2, meaning 1 level up, etc.
    levels_up = level - 1
    if levels_up > len(parts):
        return module  # can't resolve — too many dots
    base_parts = parts[: len(parts) - levels_up] if levels_up else parts

    if module:
        base_parts.append(module)
    return ".".join(base_parts) if base_parts else module


def analyze_imports(
    symbol_source: str,
    source_file: str,
    source_module: str | None = None,
    project_path: str | None = None,
) -> list[str]:
    """Analyze which imports from source_file are needed by symbol_source.

    Args:
        symbol_source: Source code of the symbol being copied
        source_file: Path to file where symbol originated (to read imports from)
        source_module: Dotted module name of source_file.  When provided,
            top-level names that are *defined* in source_file (classes,
            functions, assignments) rather than imported are also pulled in as
            ``from source_module import Name`` statements so the destination
            file remains self-contained after a move (issue #138 Bug 2).
        project_path: Project root for resolving relative imports to absolute.

    Returns:
        List of import statement strings needed for the symbol

    Example:
        >>> source = "def func():\\n    return ast.parse('x = 1')"
        >>> imports = analyze_imports(source, "module.py")
        >>> # Returns ["import ast"] if module.py has that import
    """
    runtime_names, annotation_names = _collect_name_contexts(symbol_source)
    used_names = runtime_names | annotation_names
    if not used_names:
        return []

    source_path = Path(source_file)
    if not source_path.exists():
        return []

    from .project_iter import _find_project_root, _file_to_module
    # Use tree-sitter scope resolver to parse imports from source file.
    proj_root = _find_project_root(project_path or source_file)
    resolver = _rust.PyScopeResolver(proj_root, "py")
    try:
        source_content = source_path.read_text()
        resolver.index_file(str(source_path.resolve()), source_content)
    except Exception:
        return []

    structured_imports = resolver.structured_imports_in_file(
        str(source_path.resolve())
    )

    needed_imports = []
    covered_names: set[str] = set()

    for imp in structured_imports:
        if imp["is_plain"]:
            # Plain `import X` / `import X as A` statements.
            for name, alias in imp["names"]:
                effective_name = alias or name.split('.')[0]
                if effective_name in used_names:
                    covered_names.add(effective_name)
                    if alias:
                        needed_imports.append(f"import {name} as {alias}")
                    else:
                        needed_imports.append(f"import {name}")
        else:
            # `from X import Y` statements.
            names = imp["names"]
            if names and names[0][0] == '*':
                continue

            module_name = imp["module"]

            # Resolve relative imports to absolute so they work from the
            # destination file (which is typically in a different package).
            if imp["level"] > 0:
                module_name = _resolve_relative_module(
                    imp["level"], module_name, source_file, project_path,
                )

            used_import_names = []
            for name, alias in names:
                effective_name = alias or name
                if effective_name in used_names:
                    covered_names.add(effective_name)
                    used_import_names.append((name, alias))

            if used_import_names:
                import_parts = []
                for name, asname in used_import_names:
                    if asname:
                        import_parts.append(f"{name} as {asname}")
                    else:
                        import_parts.append(name)
                needed_imports.append(f"from {module_name} import {', '.join(import_parts)}")

    # When moving a symbol, detect locally-defined top-level names that the
    # moved symbol references.  These need TYPE_CHECKING imports to avoid
    # circular imports at runtime.
    if source_module:
        # Use definitions_in_file to find top-level defined names.
        # Top-level definitions have qn = "module.name" (one component after
        # the module prefix).  Nested definitions like "module.Class.method"
        # have more components and must be excluded.
        file_module = _file_to_module(str(source_path), project_path)
        defs = resolver.definitions_in_file(str(source_path.resolve()))
        locally_defined: set[str] = set()
        prefix = file_module + "."
        for qn, _line, _col in defs:
            if qn.startswith(prefix):
                remainder = qn[len(prefix):]
                if "." not in remainder:
                    locally_defined.add(remainder)

        local_refs_needed_runtime = sorted(
            n for n in locally_defined
            if n in runtime_names and n not in covered_names
        )
        local_refs_needed_annotations = sorted(
            n for n in locally_defined
            if (
                n in annotation_names
                and n not in runtime_names
                and n not in covered_names
            )
        )

        if local_refs_needed_runtime:
            needed_imports.append(
                f"from {source_module} import {', '.join(local_refs_needed_runtime)}"
            )
        if local_refs_needed_annotations:
            needed_imports.append("from __future__ import annotations")
            type_checking_block = (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                + "".join(
                    f"    from {source_module} import {n}\n"
                    for n in local_refs_needed_annotations
                )
            )
            needed_imports.append(type_checking_block)

    return needed_imports


def copy_symbol(
    selector: ExtendedSelector,
    dest_file: str,
    position: str = "end",
    dedent: bool = False,
    include_imports: bool = False,
    source_module: str | None = None,
    project_path: str | None = None,
    apply: bool = False,
) -> str:
    """Copy a symbol from one location to another.

    Args:
        selector: Extended selector specifying the source symbol
        dest_file: Path to destination file
        position: Where to insert: "start", "end" (default)
        dedent: If True, dedent the source code to remove common indentation
        include_imports: If True, analyze and include necessary imports from source file
        source_module: Dotted module name of the source file.  Passed to
            ``analyze_imports`` when ``include_imports`` is True so that
            locally-defined symbols referenced by the moved symbol also get
            import statements in the destination (issue #138 Bug 2).
        project_path: Project root for resolving relative imports to absolute.
        apply: If True, write changes to file. If False, return diff only.

    Returns:
        Unified diff showing the changes to the destination file

    Raises:
        FileNotFoundError: If source file doesn't exist
        ValueError: If symbol not found
    """
    import textwrap
    from emend.language_registry import detect_language
    from emend.language_plugins import load_plugin

    # Get source code of the symbol
    source = get_symbol_source(selector)

    # Dedent if requested
    if dedent:
        source = textwrap.dedent(source)

    # Read destination file (create if doesn't exist)
    dest_path = Path(dest_file)
    if dest_path.exists():
        dest_content = dest_path.read_text()
    else:
        dest_content = ""

    if position == "start":
        if dest_content:
            new_content = source + "\n\n" + dest_content
        else:
            new_content = source
    else:  # "end"
        if dest_content:
            new_content = dest_content.rstrip() + "\n\n\n" + source + "\n"
        else:
            new_content = source

    # Add necessary imports to the import section of the destination file.
    # This is done AFTER appending the symbol so that imports land in the
    # proper location at the top of the file rather than being embedded at
    # the insertion point (which matters especially for "from __future__"
    # imports that must appear before any other statements, issue #138 Bug 2).
    if include_imports:
        lang = detect_language(dest_file) or "python"
        imp_handler = load_plugin(lang).import_handler
        imports = analyze_imports(source, selector.file_path, source_module=source_module, project_path=project_path)
        for imp in imports:
            try:
                pos = 0 if imp.startswith("from __future__") else -1
                new_content = imp_handler.add_import_text(imp.rstrip("\n"), pos, new_content)
            except Exception:
                new_content = imp + "\n" + new_content

    # Generate diff
    from .components import _generate_diff
    diff = _generate_diff(dest_file, dest_content, new_content)

    # Apply changes if requested
    if apply:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(new_content)

    return diff


def _is_valid_replacement(code: str, language: str = "python") -> bool:
    """Verify if the given code string parses as valid syntax.

    Uses tree-sitter via ``emend_core.validate_syntax``; accepts the
    replacement if no tree-sitter grammar is available for the language.
    """
    from emend import emend_core as _rust
    from emend.language_registry import get_extensions
    try:
        exts = get_extensions(language)
    except Exception:
        exts = []
    ext = exts[0] if exts else ("py" if language == "python" else None)
    if ext is None:
        return True
    try:
        return _rust.validate_syntax(code, ext)
    except Exception:
        return True


def _substitute_metavars(
    replacement_str: str,
    captures: dict[str, str],
) -> str | None:
    """Substitute metavars in replacement string with captured code.

    Returns substituted string, or None if replacement cannot be resolved
    (e.g. ${NAME.content} on a non-string).
    """
    replacement_code = replacement_str

    # First pass: resolve ${NAME.content} references (string
    # interpolation).  These extract the inner content of a string
    # literal, stripping the surrounding quotes.  If any reference
    # cannot be resolved (e.g. the captured node is not a string
    # literal), skip the entire replacement to avoid producing
    # nonsense output.
    content_failed = False
    for ref_match in _CONTENT_REF_RE.finditer(replacement_code):
        ref_name = ref_match.group(1)
        captured = captures.get(ref_name)
        if captured is None:
            content_failed = True
            break
        content = _extract_string_content_from_text(captured)
        if content is None:
            content_failed = True
            break
        replacement_code = replacement_code.replace(
            ref_match.group(0), content
        )
    if content_failed:
        return None

    # Second pass: substitute regular metavar references ($NAME, $...NAME).
    for name, code in captures.items():
        # Replace $...NAME with the captured text (already a string from Rust)
        replacement_code = replacement_code.replace(f"$...{name}", code)
        # Replace $NAME with the captured text
        replacement_code = replacement_code.replace(f"${name}", code)

    # Clean up comma artifacts from empty ellipsis substitutions
    replacement_code = re.sub(r'(\()\s*,\s*', r'\1', replacement_code)
    replacement_code = re.sub(r'(\[)\s*,\s*', r'\1', replacement_code)
    replacement_code = re.sub(r',\s*,', ',', replacement_code)

    return replacement_code


def replace_pattern(
    pattern_str: str,
    replacement_str: str,
    file_path: str,
    scope: list[str] | None = None,
    apply: bool = False,
    inside: str | None = None,
    not_inside: str | None = None,
    where: str | None = None,
    type_oracle: TypeOracle | None = None,
    language: str = "python",
) -> tuple[str, int]:
    """Replace pattern matches with replacement template.

    Args:
        pattern_str: Pattern string with metavariables like "print($X)"
        replacement_str: Replacement template like "logger.info($X)"
        file_path: Path to source file to transform
        scope: Optional symbol path to limit replacements to (e.g., ["MyClass", "method"])
        apply: If True, write changes to file. If False, return diff only.
        inside: Optional constraint - only replace inside this structure.
                Keywords: "def", "async def", "class", "for", "while", "try", "with", "if".
                Patterns: "def test_*", "class MyClass", "try:", "except ValueError:".
        not_inside: Optional constraint - only replace outside this structure.
                    Supports same syntax as inside.
        where: Optional constraint - alias for inside with pattern support.
        type_oracle: Optional TypeOracle instance for :type[X] and :returns[X]
                     constraints.  When present, matching is delegated to
                     ``find_pattern`` so that the oracle post-filter is applied
                     and only type-verified positions are replaced.

    Returns:
        Tuple of (diff, count) where diff is a unified diff and count is number of replacements
    """
    # Handle --where as alias for --inside
    if where is not None:
        if inside is not None:
            raise ValueError("Cannot specify both 'where' and 'inside' parameters")
        inside = where

    # Validate inside/not_inside constraints
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")

    # Read file
    file = Path(file_path)
    if not file.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file.read_text()

    # Find all matches using find_pattern (already migrated to tree-sitter fast paths)
    matches = find_pattern(
        pattern_str, file_path, scope=scope,
        inside=inside, not_inside=not_inside, where=where,
        type_oracle=type_oracle, language=language,
        source_override=source_code,
    )

    if not matches:
        return "", 0

    # Build a newline offset table for the source
    line_starts = [0]
    for i, ch in enumerate(source_code):
        if ch == '\n':
            line_starts.append(i + 1)

    # Use Rust transformation engine for byte-range replacements
    transform = _rust.PyFileTransform(source_code)
    replacement_count = 0
    accepted_ranges: list[tuple[int, int]] = []

    for match in matches:
        if match.line is None or match.col is None or match.end_line is None or match.end_col is None:
            continue

        # Convert line/col to byte offsets
        start_offset = line_starts[match.line - 1] + match.col
        
        if match.matched_text is not None:
            # If we have the exact matched text from Rust (potentially adjusted range),
            # use its length to determine the end offset.
            end_offset = start_offset + len(match.matched_text)
        else:
            end_offset = line_starts[match.end_line - 1] + match.end_col

        # Filter out matches that are contained within a previously accepted match
        # Since find_pattern returns matches in top-down DFS order, the first match
        # of a nested set is the outermost one.
        is_contained = False
        for a_start, a_end in accepted_ranges:
            if start_offset >= a_start and end_offset <= a_end:
                is_contained = True
                break
        if is_contained:
            continue

        # Build replacement by substituting metavars
        replacement_code = _substitute_metavars(replacement_str, match.captures)
        if replacement_code is None:
            continue

        # Verify replacement parses as valid syntax
        if not _is_valid_replacement(replacement_code, language=language):
            continue

        # Apply replacement to the transform
        transform.replace_range(start_offset, end_offset, replacement_code)
        accepted_ranges.append((start_offset, end_offset))
        replacement_count += 1

    if replacement_count == 0:
        return "", 0

    # Apply all edits
    new_code = transform.apply()
    if new_code is None:
        # This should not happen due to the is_contained filter above
        logger.error("Overlapping edits detected in replace_pattern")
        return "", 0

    # Generate diff
    from .components import _generate_diff
    diff = _generate_diff(file_path, source_code, new_code)

    # Apply changes if requested
    if apply:
        file.write_text(new_code)

    return diff, replacement_count



# Cross-project semantic primitives

