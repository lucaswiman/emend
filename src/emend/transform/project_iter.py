"""Project file iteration, pattern search, and module utilities."""
from __future__ import annotations
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import logging
import re
import time

from ..language_plugins import NOQA_PATTERN as _NOQA_PATTERN
from emend import emend_core as _rust
from emend.errors import BUG_EXCEPTIONS

if TYPE_CHECKING:
    import sqlite3
    from ..type_oracle import TypeOracle

logger = logging.getLogger(__name__)

_METAVAR_RE = re.compile(r'\$(?:\.\.\.)?[A-Z_][A-Z_0-9]*')

def _ext_from_path(file_path: str | Path) -> str:
    """Return the file extension (without dot) for passing to emend_core functions."""
    return Path(file_path).suffix.lstrip('.') or 'py'


def extract_pattern_literals(pattern_str: str) -> list[str]:
    """Extract literal identifier tokens from a pattern string for pre-filtering.

    For a pattern like "$X.objects.filter($...ARGS)", returns ["objects", "filter"].
    These can be used with Rust filter_files_by_content to quickly eliminate files
    that cannot possibly match the pattern.
    """
    # Remove metavariables
    cleaned = _METAVAR_RE.sub('', pattern_str)
    # Extract identifier-like tokens (Python identifiers)
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z_0-9]*', cleaned)
    # Filter out Python keywords and very short tokens that would match too broadly
    _PY_KEYWORDS = {'if', 'else', 'elif', 'for', 'while', 'try', 'except',
                    'finally', 'with', 'as', 'import', 'from', 'class', 'def',
                    'return', 'yield', 'raise', 'pass', 'break', 'continue',
                    'and', 'or', 'not', 'in', 'is', 'True', 'False', 'None',
                    'lambda', 'global', 'nonlocal', 'del', 'assert', 'async',
                    'await'}
    return [t for t in tokens if t not in _PY_KEYWORDS and len(t) > 1]


@dataclass
class ProjectPatternMatch:
    """A pattern match paired with its originating file path."""
    file_path: str
    match: "PatternMatch"


def find_pattern_in_project(
    pattern_str: str,
    file_paths: list[str],
    *,
    scope: list[str] | None = None,
    inside: str | None = None,
    not_inside: str | None = None,
    imported_from: str | None = None,
    scope_local: bool = False,
    type_oracle: TypeOracle | None = None,
    index_conn: sqlite3.Connection | None = None,
    limit: int | None = None,
    language: str = "python",
) -> list[ProjectPatternMatch]:
    """Search for a pattern across multiple files.

    Four-stage pipeline, each stage reducing the file set:

    1. **Index prefilter** (optional) — if *index_conn* is provided,
       query ``reference_index`` / ``symbol_index`` for files that
       mention the pattern's literal identifiers.
    2. **Rust string-contains filter** — ``read_and_filter_files``
       drops files whose text doesn't contain every required literal.
    3. **Rust tree-sitter batch** — if the pattern compiles to Rust IR
       and no advanced constraints are active, match all files at once
       in Rust.
    4. **Pattern matching fallback** — parse and match remaining files
       in parallel via ``ThreadPoolExecutor``.

    Returns a list of ``ProjectPatternMatch`` (file_path + match).
    """
    from .patterns import find_pattern, PatternMatch
    # Validate constraints eagerly so callers see errors immediately.
    if inside and not_inside:
        raise ValueError("Cannot specify both 'inside' and 'not_inside' parameters")

    is_single_file = len(file_paths) == 1

    literals = extract_pattern_literals(pattern_str)

    # --- Stage 1: index prefilter ---
    if literals and index_conn is not None and not is_single_file:
        candidate_set = _index_prefilter(literals, index_conn)
        if candidate_set is not None:
            before = len(file_paths)
            file_paths = [f for f in file_paths if f in candidate_set]
            logger.debug(
                "index prefilter: %d → %d files", before, len(file_paths),
            )
            if not file_paths:
                return []

    # --- Stage 2: Rust string-contains filter ---
    if literals and len(file_paths) > 1:
        try:
            file_contents: list[tuple[str, str]] = _rust.read_and_filter_files(
                file_paths, literals,
            )
        except Exception:
            logger.debug(
                "Rust read_and_filter_files failed, using Python fallback",
                exc_info=True,
            )
            file_contents = _read_and_filter_py(file_paths, literals)
    else:
        file_contents = []
        for fp in file_paths:
            try:
                file_contents.append((fp, Path(fp).read_text()))
            except OSError:
                # For single-file requests, propagate not-found so callers
                # can report a meaningful error.
                if is_single_file:
                    raise FileNotFoundError(f"File not found: {fp}")
                pass

    logger.debug(
        "string-contains filter: %d files surviving", len(file_contents),
    )

    if not file_contents:
        return []

    # --- Stage 3: Rust batch fast-path ---
    has_constraints = (
        scope is not None
        or imported_from is not None
        or scope_local
        or type_oracle is not None
    )

    if not has_constraints:
        from emend.pattern import (
            compile_pattern_to_rust_ir,
            compile_constraint_to_rust_ir,
        )

        pattern_ir = compile_pattern_to_rust_ir(pattern_str, language=language)
        if pattern_ir is not None:
            inside_ir = (
                compile_constraint_to_rust_ir(inside, language=language) if inside else None
            )
            not_inside_ir = (
                compile_constraint_to_rust_ir(not_inside, language=language)
                if not_inside
                else None
            )
            if (inside is None or inside_ir is not None) and (
                not_inside is None or not_inside_ir is not None
            ):
                from emend.language_registry import get_extensions

                extensions = get_extensions(language)
                raw = None
                try:
                    raw = _rust.find_pattern_in_files(
                        list(file_contents), pattern_ir,
                        inside_ir, not_inside_ir,
                        extension=extensions[0] if extensions else None,
                    )
                except Exception:
                    logger.debug("Rust batch path failed, falling back", exc_info=True)
                if raw is not None:
                    results = [
                        ProjectPatternMatch(
                            file_path=fp,
                            match=PatternMatch(
                                node_text=text,
                                captures={
                                    k: v for k, v in captures.items()
                                    if k != "_"
                                },
                                line=line, end_line=end_line,
                                col=col, end_col=end_col,
                                matched_text=text,
                            ),
                        )
                        for fp, line, col, end_line, end_col, text, captures in raw
                    ]
                    if limit is not None:
                        results = results[:limit]
                    return results

    # --- Stage 4: Pattern matching fallback (parallel) ---
    results: list[ProjectPatternMatch] = []

    if is_single_file:
        # Single file: call directly so errors propagate to caller.
        fp, content = file_contents[0]
        matches = find_pattern(
            pattern_str, fp,
            scope=scope, inside=inside, not_inside=not_inside,
            imported_from=imported_from, scope_local=scope_local,
            source_override=content, type_oracle=type_oracle,
            language=language,
        )
        results = [ProjectPatternMatch(file_path=fp, match=m) for m in matches]
        if limit is not None:
            results = results[:limit]
    else:
        from concurrent.futures import ThreadPoolExecutor

        def _find_one(args: tuple[str, str]) -> list[ProjectPatternMatch]:
            fp, content = args
            try:
                matches = find_pattern(
                    pattern_str, fp,
                    scope=scope, inside=inside, not_inside=not_inside,
                    imported_from=imported_from, scope_local=scope_local,
                    source_override=content, type_oracle=type_oracle,
                    language=language,
                )
                return [ProjectPatternMatch(file_path=fp, match=m) for m in matches]
            except BUG_EXCEPTIONS:
                raise
            except Exception:
                logger.debug("find_pattern failed for %s, skipping", fp, exc_info=True)
                return []

        with ThreadPoolExecutor() as executor:
            for batch in executor.map(_find_one, file_contents):
                results.extend(batch)
                if limit is not None and len(results) >= limit:
                    results = results[:limit]
                    break

    return results


def _index_prefilter(
    literals: list[str],
    conn: sqlite3.Connection,
) -> set[str] | None:
    """Query the index for files likely to contain *literals*.

    Returns a set of file paths, or ``None`` if the index has no useful
    data (caller should skip this stage).
    """
    import sqlite3

    per_literal: list[set[str]] = []
    for lit in literals:
        files_for_lit: set[str] = set()
        try:
            for (fp,) in conn.execute(
                "SELECT DISTINCT file_path FROM reference_index "
                "WHERE target_qn LIKE ?",
                ("%" + lit + "%",),
            ):
                files_for_lit.add(fp)
        except sqlite3.Error:
            pass  # index table missing or corrupt
        try:
            for (fp,) in conn.execute(
                "SELECT DISTINCT file_path FROM symbol_index "
                "WHERE name = ? OR qualified_name LIKE ?",
                (lit, "%" + lit + "%"),
            ):
                files_for_lit.add(fp)
        except sqlite3.Error:
            pass  # index table missing or corrupt
        if files_for_lit:
            per_literal.append(files_for_lit)

    if not per_literal:
        return None

    candidates = per_literal[0]
    for s in per_literal[1:]:
        candidates &= s
    return candidates


def _read_and_filter_py(
    file_paths: list[str], literals: list[str],
) -> list[tuple[str, str]]:
    """Pure-Python fallback for Rust ``read_and_filter_files``."""
    results: list[tuple[str, str]] = []
    for fp in file_paths:
        try:
            content = Path(fp).read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if all(lit in content for lit in literals):
            results.append((fp, content))
    return results


# Helper functions for cross-project operations

def _find_project_root(start_path: str) -> str:
    """Find project root by looking for markers.

    Checks for a valid Git marker or an emend config file first, then
    language-specific project files for Python, TypeScript/JS, and Rust.
    """
    from emend.project_config import find_project_root

    return str(find_project_root(start_path))


def _find_source_root(project_root: str, language: str = "python") -> str:
    """Compatibility wrapper for the canonical source-root resolver."""
    from emend.project_config import find_source_root

    return str(find_source_root(project_root, language))


def _normalize_module_qn(module: str) -> str:
    """Normalize a module name to use dots for fact-graph QN construction.

    Delegates to ``_normalize_qn`` from ``fact_graph`` which handles
    language-specific separators (``::`` for Rust, ``/`` for TypeScript),
    quotes, and relative path segments.
    """
    from emend.fact_graph import _normalize_qn
    return _normalize_qn(module)


def _file_to_module(file_path: str, project_path: str | None) -> str:
    """Compatibility wrapper for the dependency-neutral module resolver."""
    from emend.project_config import module_name_for_file

    return module_name_for_file(file_path, project_path)


# Non-dot directories to skip.  All directories starting with '.' are
# skipped automatically by the Rust scanner (emend_core.collect_python_files).
# The canonical list lives in Rust (scanner.rs); we import it here so
# Python and Rust always agree.
_SKIP_DIRS = frozenset(_rust.skip_dirs())

from emend.file_collection import (
    collect_source_files as _collect_source_files,
    collect_source_files_scandir as _collect_source_files_scandir,
    collect_all_source_files as _collect_all_source_files,
    collect_git_tracked_source_files as _collect_git_tracked_source_files,
    detect_project_languages,
)




def _files_importing_module(project_root: str, module_dotted: str, language: str = "python") -> set[str] | None:
    """Return the set of files that import from *module_dotted*, or None if unknown.

    First tries the cached import_graph (instant).  Falls back to the Rust
    targeted import filter which text-prefilters then tree-sitter-parses
    only candidate files.

    Returns None if the filter cannot be applied (caller should fall back
    to scanning all files).
    """
    from .index import query_import_graph
    # The legacy graph stores dotted module names, which are lossy for TS/Rust.
    # Their native scan below retains exact identities; QN caching stays enabled.
    cached = query_import_graph(project_root, module_dotted) if language == "python" else None
    if cached is not None:
        return set(cached)

    source_files = _collect_source_files(project_root, language=language)
    try:
        from emend.project_config import module_resolution_context

        module_root, root_module = module_resolution_context(project_root, language)
        matching = _rust.files_importing_module(
            source_files, module_dotted, project_root, str(module_root),
            str(root_module) if root_module else None,
        )
        return set(matching)
    except Exception:
        logger.debug("Rust files_importing_module failed", exc_info=True)
        return None


def visit_project_ts(
    name_hint: str,
    project_path: str,
    target_file: str | None = None,
    candidate_files: set[str] | None = None,
    target_qnames: set[str] | None = None,
    language: str = "python",
) -> Iterator[tuple[str, str, _rust.PyScopeResolver]]:
    """Iterate over source files using tree-sitter + PyScopeResolver.

    Yields (file_path, content, resolver).
    """
    t_start = time.monotonic()
    project_root = str(Path(project_path).resolve())
    from emend.project_config import module_resolution_context
    source_files = _collect_source_files(project_root, language=language)

    if candidate_files is not None:
        source_files = [f for f in source_files
                        if f in candidate_files
                        or (target_file and str(Path(f).resolve()) == target_file)]

    # Structural pre-filter: use tree-sitter to find files containing
    # an actual identifier matching name_hint (not just substring matches
    # in strings/comments).
    if name_hint:
        _name_matches = _rust.find_name_in_files(source_files, name_hint)
        source_files = list({m.file for m in _name_matches})
        if target_file and target_file not in source_files:
            source_files.append(target_file)

    # Read and filter files
    file_contents = _rust.read_and_filter_files(source_files, [name_hint] if name_hint else [])

    # QN-index pre-filter
    if target_qnames:
        filtered_contents = []
        for py_file, content in file_contents:
            if target_file and str(Path(py_file).resolve()) == target_file:
                filtered_contents.append((py_file, content))
                continue

            from .index import _get_cached_qnames
            content_hash = hashlib.md5(content.encode(), usedforsecurity=False).digest()
            cached_qns = _get_cached_qnames(
                content_hash,
                file_path=str(py_file),
                project_root=project_root,
            )
            if cached_qns is not None:
                if not target_qnames.intersection(cached_qns):
                    continue
            filtered_contents.append((py_file, content))
        file_contents = filtered_contents

    # Index and yield
    for py_file, content in file_contents:
        ext = Path(py_file).suffix.lstrip('.')
        try:
            module_root, root_module = module_resolution_context(
                project_root, language, file_path=py_file,
            )
            resolver = _rust.PyScopeResolver(
                project_root, ext, str(module_root),
                str(root_module) if root_module else None,
            )
            resolver.index_file(py_file, content)
        except Exception:
            logger.debug("scope resolver failed for %s, skipping", py_file, exc_info=True)
            continue
        yield py_file, content, resolver

    logger.info("visit_project_ts: finished in %.3fs", time.monotonic() - t_start)


def _get_imports(source_code: str, language: str = "python") -> str:
    """Extract all top-level import statements as a single string."""
    from emend.language_plugins import load_plugin
    return load_plugin(language).import_handler.extract_imports(source_code)


def _add_import_text(
    import_str: str,
    position: int,
    file_path: Path,
    apply: bool,
    source_code: str,
    language: str = "python",
) -> str:
    """Add an import statement to a file using text manipulation.

    Args:
        import_str: Import statement to add (e.g., "import os")
        position: 0 for prepend, -1 for append
        file_path: Path to the file
        apply: Whether to apply changes
        source_code: Original source code
        language: Source language for import handling

    Returns:
        Unified diff showing changes
    """
    from emend.language_plugins import load_plugin
    try:
        new_code = load_plugin(language).import_handler.add_import_text(
            import_str, position, source_code
        )
    except SyntaxError:
        raise ValueError(f"Cannot parse {file_path}")

    from .components import _generate_diff
    diff = _generate_diff(str(file_path), source_code, new_code)

    if apply:
        file_path.write_text(new_code)

    return diff
