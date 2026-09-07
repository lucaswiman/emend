"""Language-neutral extraction of one exact file revision."""

from __future__ import annotations

from bisect import bisect_right
import hashlib
import logging
from pathlib import Path
from typing import Any, Literal

from emend.analysis_snapshot import (
    DecoratorOnFact,
    DefUseFact,
    ExtractedFile,
    FileRevision,
    ImportFact,
    MethodCallFact,
    SymbolFact,
)
from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)


def _walk_symbols(
    out: list[SymbolFact],
    dec_out: list[DecoratorOnFact],
    search_out: list[list[object]],
    raw_symbols: list[dict[str, Any]],
    file_path: str,
    module_name: str,
    parent_qn: str | None,
) -> None:
    """Recursively walk Rust symbol dicts and collect SymbolFact entries."""
    # Normalize the module name to use dots so that symbol QNs are
    # consistent with reference QNs (which go through _normalize_qn()).
    normalized_module = _normalize_qn(module_name)
    for d in raw_symbols:
        kind = d.get("kind", "")
        name = d["name"]
        path_parts = list(d.get("path", []))
        local_qn = ".".join(path_parts) if path_parts else name
        module_qn = f"{normalized_module}.{local_qn}"
        depth = len(path_parts) if path_parts else 1
        decorators = d.get("decorators", []) or []
        returns = d.get("returns", "") or ""
        raw_signature = d.get("signature") or ""
        signature = (
            f"def {name}{raw_signature}"
            if kind in ("function", "method") else raw_signature
        )
        search_out.append([
            file_path, module_qn, name, local_qn, kind,
            d.get("line", 0), d.get("end_line", 0), depth,
            module_qn.rpartition(".")[0] if depth > 1 else "",
            signature, returns, ",".join(decorators),
        ])
        if kind in ("variable", "reference"):
            continue

        if path_parts:
            qn = module_qn
        else:
            qn = f"{normalized_module}.{name}"

        out.append(SymbolFact(
            file_path=file_path,
            name=name,
            qualified_name=qn,
            kind=kind,
            line=d["line"],
            end_line=d["end_line"],
            parent=parent_qn,
        ))

        # Extract decorators — strip @ prefix and arguments so that
        # ``@router.get('/users')`` becomes ``router.get`` and also
        # stores the basename ``get`` for broader matching.
        for dec_name in (d.get("decorators", []) or []):
            cleaned = dec_name
            if cleaned.startswith("@"):
                cleaned = cleaned[1:]
            if "(" in cleaned:
                cleaned = cleaned[:cleaned.index("(")]
            cleaned = cleaned.strip()
            dec_out.append(DecoratorOnFact(symbol_qn=qn, decorator=cleaned))
            # Also store the basename for broader matching
            basename = cleaned.rsplit(".", 1)[-1] if "." in cleaned else None
            if basename and basename != cleaned:
                dec_out.append(DecoratorOnFact(symbol_qn=qn, decorator=basename))

        children = d.get("children", [])
        if children:
            _walk_symbols(
                out, dec_out, search_out, children, file_path, module_name,
                parent_qn=qn,
            )


def _map_ref_kind(kind: str) -> Literal["read", "write", "call", "import", "definition"]:
    """Map a Rust scope-resolver reference kind to our fact model."""
    if kind == "definition":
        return "definition"
    if kind == "call":
        return "call"
    if kind == "write":
        return "write"
    if kind == "import":
        return "import"
    return "read"


def _build_symbol_line_index(
    sym_facts: list[SymbolFact],
    file_path: str,
) -> list[tuple[int, int, str]]:
    """Build a sorted list of (start_line, end_line, qn) for function symbols."""
    entries: list[tuple[int, int, str]] = []
    for sym in sym_facts:
        if sym.file_path == file_path and sym.kind in (
            "function", "async_function", "method", "async_method"
        ):
            entries.append((sym.line, sym.end_line, sym.qualified_name))
    entries.sort(key=lambda e: e[0], reverse=True)
    return entries


def _enclosing_symbol(
    symbol_ranges: list[tuple[int, int, str]],
    line: int,
) -> str | None:
    """Return the qualified name of the innermost function containing *line*."""
    for start, end, qn in symbol_ranges:
        if start <= line <= end:
            return qn
    return None


def _normalize_qn(qn: str) -> str:
    """Normalize language-specific QN separators to dots.

    The Rust scope resolver uses ``::`` (Rust) and ``/`` (TypeScript) as
    separators, but ``_walk_symbols`` always uses ``.``.

    Also strips quotes and normalizes relative import paths (e.g.
    ``'./target'.process`` → ``target.process``).
    """
    qn = qn.replace("'", "").replace('"', "")
    qn = qn.replace("::", ".").replace("/", ".")
    # Most QNs contain no repeated dots. Avoid paying for a regex on every
    # reference in large indexes; relative paths are the uncommon slow path.
    while ".." in qn:
        qn = qn.replace("..", ".")
    qn = qn.lstrip(".")
    return qn


def _find_containing_block(
    block_ranges: list[tuple],
    line: int,
) -> tuple[str, int]:
    """Find the (func_qn, block_id) containing a given line.

    Returns ``("", -1)`` for module-level code.

    *block_ranges* must be sorted by ``(start_line, -(end_line - start_line))``
    — the default ordering produced by snapshot extraction. Uses binary search
    to find the insertion point, then scans backwards through candidates whose
    start_line <= line, picking the tightest (smallest span) enclosing block.
    """
    if not block_ranges:
        return ("", -1)

    # Find the rightmost block whose start_line is at most line.
    lo = bisect_right(block_ranges, line, key=lambda block: block[2])
    # lo is the first index where start_line > line.
    # Scan backwards from lo-1 to find all blocks containing the line.
    best_func_qn = ""
    best_block_id = -1
    best_span = float("inf")

    for i in range(lo - 1, -1, -1):
        start_line_i = block_ranges[i][2]
        # Once start_line is far enough before line that no block starting
        # here could still be the tightest, stop.  But blocks can be large,
        # so we must check end_line.  We can stop when
        # start_line < line - best_span (any block starting here with
        # span < best_span would end before line).
        if best_span < float("inf") and start_line_i < line - best_span:
            break
        end_line_i = block_ranges[i][3]
        if end_line_i >= line:
            span = end_line_i - start_line_i
            if span < best_span:
                best_span = span
                best_func_qn = block_ranges[i][0]
                best_block_id = block_ranges[i][1]

    return best_func_qn, best_block_id


def _extract_imports_python(file_path: str, content: str) -> list[ImportFact]:
    """Extract imports from Python source using tree-sitter via ``emend_core``."""
    from emend import emend_core

    resolver = getattr(_extract_imports_python, "_resolver", None)
    if resolver is None:
        resolver = emend_core.PyScopeResolver(".", extension="py")
        _extract_imports_python._resolver = resolver
    facts: list[ImportFact] = []
    for imp in resolver.collect_structured_imports_from_source(content, ext="py"):
        for name, alias in imp["names"]:
            facts.append(ImportFact(
                importing_file=file_path,
                imported_module=name if imp["is_plain"] else "." * imp["level"] + imp["module"],
                imported_name=None if imp["is_plain"] else name,
                alias=alias,
                line=imp["start_line"] + 1,
            ))
    return facts


# ---------------------------------------------------------------------------
# TypeScript / JavaScript import extraction
# ---------------------------------------------------------------------------

def _extract_imports_typescript(file_path: str, content: str) -> list[ImportFact]:
    """Extract imports from TypeScript/JavaScript source using tree-sitter.

    Uses ``PyScopeResolver.imports_in_file()`` (Rust-backed) for ES module
    import extraction.  Side-effect imports, re-exports, and CommonJS
    ``require()`` calls are not covered by the scope resolver and are silently
    omitted.  Line numbers are not available from this API and are recorded as
    ``0``.
    """
    facts: list[ImportFact] = []
    imports: list = []
    try:
        from emend import emend_core as ec  # type: ignore[attr-defined]
        ext = Path(file_path).suffix.lstrip(".") or "ts"
        if ext not in ("ts", "tsx", "js", "jsx"):
            ext = "ts"
        resolver = ec.PyScopeResolver(".", ext)
        resolver.index_file(file_path, content)
        imports = resolver.imports_in_file(file_path)
    except Exception:
        logger.debug(
            "emend_core TypeScript import extraction failed for %s",
            file_path,
            exc_info=True,
        )
    for local_name, module_path, imported_name, is_star in imports:
        # Strip surrounding quotes that the scope resolver includes in the
        # module path (e.g. '"./foo"' → './foo').
        clean_module = module_path.strip("\"'")
        if not clean_module:
            continue
        facts.append(ImportFact(
            importing_file=file_path,
            imported_module=clean_module,
            imported_name="*" if is_star else (imported_name or None),
            alias=local_name if local_name != imported_name else None,
            line=0,  # scope resolver does not return line numbers here
        ))
    return facts


def _extract_imports_rust(file_path: str, content: str) -> list[ImportFact]:
    """Extract imports from Rust source using tree-sitter via ``emend_core``.

    Handles ``use`` declarations (plain, aliased, glob, grouped/nested),
    ``pub use``, ``pub(crate) use``, and ``mod name;`` declarations.
    """
    facts: list[ImportFact] = []
    imports: list = []
    try:
        from emend import emend_core as ec  # type: ignore[attr-defined]
        resolver = getattr(_extract_imports_rust, "_resolver", None)
        if resolver is None:
            resolver = ec.PyScopeResolver(".", extension="rs")
            _extract_imports_rust._resolver = resolver  # type: ignore[attr-defined]
        imports = resolver.collect_rust_imports_from_source(content, ext="rs")
    except Exception:
        logger.debug(
            "emend_core Rust import extraction failed for %s",
            file_path,
            exc_info=True,
        )
    for local_name, module_path, imported_name, is_star, line in imports:
        if not module_path and not is_star:
            continue
        alias = local_name if (imported_name and local_name != imported_name) else None
        facts.append(ImportFact(
            importing_file=file_path,
            imported_module=module_path,
            imported_name="*" if is_star else (imported_name or None),
            alias=alias,
            line=line,
        ))
    return facts


def _extract_imports(file_path: str, content: str) -> list[ImportFact]:
    """Extract import facts from *content*, dispatching by language.

    - Python files: tree-sitter via ``emend_core``
    - TypeScript / JavaScript files: tree-sitter via ``PyScopeResolver``
    - Rust files: tree-sitter via ``emend_core`` (``collect_rust_imports_from_source``)
    - All others: treated as Python (best-effort)
    """
    from emend.language_registry import detect_language
    lang = detect_language(file_path)
    if lang == "typescript":
        return _extract_imports_typescript(file_path, content)
    elif lang == "rust":
        return _extract_imports_rust(file_path, content)
    else:
        return _extract_imports_python(file_path, content)


def _resolve_cfg_func_qn(
    cfg: Any,
    sym_facts: list[SymbolFact],
    rel_path: str,
    module_name: str,
) -> str:
    """Resolve the qualified name of a CFG's function.

    Matches ``cfg.func_name`` against *sym_facts* first by name and line range
    (disambiguating same-named methods across classes; CFG lines are
    0-indexed, symbol lines 1-indexed), then by name only, and finally falls
    back to ``module_name.func_name``.
    """
    func_name = cfg.func_name
    cfg_start = cfg.func_start_line + 1
    matching = [
        sf for sf in sym_facts
        if sf.name == func_name and sf.file_path == rel_path
    ]
    for sf in matching:
        if sf.line <= cfg_start <= (sf.end_line or sf.line):
            return sf.qualified_name
    return matching[0].qualified_name if matching else f"{module_name}.{func_name}"


def _bfs_reachable_blocks(
    entries_by_func: dict[tuple[str, str], set[int]],
    adj: dict[tuple[str, str, int], list[int]],
) -> list[list]:
    """Return ``[file_path, func_qn, block_id]`` rows reachable from entry blocks.

    Runs a per-function traversal over the CFG adjacency map *adj*, seeded by
    the entry block ids in *entries_by_func*.
    """
    reachable_rows: list[list] = []
    for (fp, fq), entry_set in entries_by_func.items():
        visited: set[int] = set()
        stack = list(entry_set)
        while stack:
            bid = stack.pop()
            if bid in visited:
                continue
            visited.add(bid)
            reachable_rows.append([fp, fq, bid])
            for nb in adj.get((fp, fq, bid), []):
                if nb not in visited:
                    stack.append(nb)
    return reachable_rows


def build_def_use_facts(
    cfgs: list[Any],
    sym_facts: list[SymbolFact],
    rel_path: str,
    module_name: str,
) -> list[DefUseFact]:
    """Extract def-use facts from CFG blocks.

    Covers only intra-function code; module-level def-use facts must be
    synthesised separately from scope-resolver references.
    """
    def_use_facts: list[DefUseFact] = []

    for cfg in cfgs:
        func_qn = _resolve_cfg_func_qn(cfg, sym_facts, rel_path, module_name)

        defs_map: dict[str, list[tuple[int, int, int, str]]] = {}
        for block in cfg.get_blocks():
            bid = block["id"]
            for d in block.get("defs", []) or []:
                var_name = d[0] if isinstance(d, (list, tuple)) else d
                dline = d[1] if isinstance(d, (list, tuple)) and len(d) > 1 else 0
                dcol = d[2] if isinstance(d, (list, tuple)) and len(d) > 2 else 0
                dkind = d[3] if isinstance(d, (list, tuple)) and len(d) > 3 else "write"
                defs_map.setdefault(var_name, []).append((bid, dline, dcol, dkind))

        for block in cfg.get_blocks():
            bid = block["id"]
            for u in block.get("uses", []) or []:
                var_name = u[0] if isinstance(u, (list, tuple)) else u
                uline = u[1] if isinstance(u, (list, tuple)) and len(u) > 1 else 0
                ucol = u[2] if isinstance(u, (list, tuple)) and len(u) > 2 else 0
                if var_name in defs_map:
                    for def_bid, dl, dc, dk in defs_map[var_name]:
                        def_use_facts.append(DefUseFact(
                            file_path=rel_path,
                            func_qn=func_qn,
                            var_name=var_name,
                            kind=dk,
                            def_block=def_bid,
                            use_block=bid,
                            def_line=dl,
                            def_col=dc,
                            use_line=uline,
                            use_col=ucol,
                        ))

    return def_use_facts


def _build_method_call_facts(
    refs: list[tuple],
    rel_path: str,
    block_ranges: list,
    *,
    normalize_qn: bool,
) -> list[MethodCallFact]:
    """Extract MethodCallFacts from dotted-name call references.

    The scope resolver emits 1-based line numbers, but CFG def-use facts use
    0-based.  Method-call lines are emitted as 0-based so same-line Datalog
    taint comparisons work.
    """
    from emend.location_resolver import MODULE_LEVEL_BLOCK as _MLB
    from emend.location_resolver import MODULE_LEVEL_FUNC as _MLF

    facts: list[MethodCallFact] = []
    for qn, line, _col, _offset, _end_offset, kind, _ann in refs:
        if normalize_qn:
            qn = _normalize_qn(qn)
        if _map_ref_kind(kind) != "call" or "." not in qn:
            continue
        parts = qn.rsplit(".", 1)
        if len(parts) != 2:
            continue
        fq, bid = _find_containing_block(block_ranges, line)
        if fq == "" and bid == -1:
            fq, bid = _MLF, _MLB
        facts.append(MethodCallFact(
            file_path=rel_path,
            func_qn=fq,
            receiver=parts[0].rsplit(".", 1)[-1],
            method=parts[1],
            block_id=bid,
            line=line - 1,
        ))
    return facts


def _extract_file_facts(
    abs_path: str,
    rel_path: str,
    ext: str,
    content: str,
    project_root: str,
    module_name: str,
    scope_resolver=None,
) -> ExtractedFile:
    """Extract all analysis facts for a single file.

    Returns a dependency-neutral record carrying revision identity and rows.
    Thread-safe — only reads from the shared scope_resolver, no writes.

    """
    from emend import emend_core
    from emend.cfg import build_cfgs_for_source

    result: dict[str, list] = {
        "fg_sym": [], "search_sym": [], "dec": [], "cfg_blocks": [],
        "cfg_edges": [], "fg_refs": [],
        "calls": [], "calls_by_callee": [], "calls_by_file": [],
        "def_uses": [], "method_calls": [], "source_locs": [],
        "imports": [], "ref_by_block": [], "noncall_private_member_refs": [],
        "module_level_refs": [],
        "exported_qns": [],
    }

    # -- Extract symbols via Rust
    try:
        raw_symbols = emend_core.collect_symbols_from_str(content, ext=ext)
    except Exception:
        logger.debug("symbol extraction failed for %s", rel_path, exc_info=True)
        raw_symbols = []

    sym_facts_for_file: list = []
    dec_facts_for_file: list = []
    _walk_symbols(
        sym_facts_for_file, dec_facts_for_file, result["search_sym"],
        raw_symbols, rel_path, module_name, parent_qn=None,
    )

    # Populate FactGraph-style symbol rows (filtered)
    for sf in sym_facts_for_file:
        result["fg_sym"].append([
            sf.qualified_name, sf.file_path, sf.name, sf.kind,
            sf.line, sf.end_line, sf.parent or "",
        ])

    # Populate decorator_on
    for df in dec_facts_for_file:
        result["dec"].append([df.symbol_qn, df.decorator])

    # Collect exported symbol QNs.  Python's module export contract is
    # ``__all__``; keep it in the canonical facts as well as the legacy
    # search index so consumers such as safe-delete see the same boundary.
    from emend.language_registry import detect_language as _detect_lang_eff
    _lang_eff = _detect_lang_eff(abs_path) or "python"
    from emend.language_registry import detect_exported_names as _detect_exports_eff
    exported_names = _detect_exports_eff(content, _lang_eff)
    if exported_names:
        for sf in sym_facts_for_file:
            # ``__all__`` exports module-level names, not nested definitions
            # that happen to share one of those names.
            if sf.parent is None and sf.name in exported_names:
                result["exported_qns"].append([rel_path, sf.qualified_name])

    imports = _extract_imports(rel_path, content)
    relative_bindings: dict[str, str] = {}
    if ext == "py":
        from importlib.util import resolve_name

        package = module_name if Path(abs_path).stem == "__init__" else module_name.rpartition(".")[0]
        for imp in imports:
            if imp.imported_module.startswith(".") and imp.imported_name:
                try:
                    resolved = resolve_name(imp.imported_module, package)
                except (ImportError, ValueError):
                    continue  # Invalid relative import; do not invent a target.
                relative_bindings[f"{imp.imported_module}.{imp.imported_name}"] = f"{resolved}.{imp.imported_name}"

    def reference_qn(qn: str) -> str:
        # Resolve relative bindings before separator normalization erases their
        # package context. Structured imports also disambiguate `from . import`.
        if qn.startswith("."):
            for relative, absolute in relative_bindings.items():
                if qn == relative or qn.startswith(relative + "."):
                    qn = absolute + qn[len(relative):]
                    break
        return _normalize_qn(qn)

    # -- Extract references via the exact source revision's scope resolver
    file_refs: list[tuple] = []
    try:
        raw_refs = scope_resolver.references_in_file(abs_path)
    except Exception:
        logger.debug("reference extraction failed for %s", rel_path, exc_info=True)
        raw_refs = []
    for qn_str, line, col, _start, _end, kind, _annotation in raw_refs:
        qn_str = reference_qn(qn_str)
        kind = _map_ref_kind(kind)
        file_refs.append((qn_str, line, col, kind))

    # -- Extract imports (all languages)
    # Detailed imports via _extract_imports (dispatches by language for TS/Rust).
    for imp in imports:
        result["imports"].append([
            imp.importing_file, imp.imported_module,
            imp.imported_name or "", imp.line,
            imp.alias or "",
        ])

    # -- source_loc (from symbol facts)
    for sf in sym_facts_for_file:
        result["source_locs"].append([
            sf.file_path, "symbol", sf.qualified_name,
            sf.line, 0, sf.end_line, 0,
        ])

    # -- CFG
    try:
        cfgs = build_cfgs_for_source(content, ext=ext)
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("CFG build failed for %s", rel_path, exc_info=True)
        cfgs = []

    block_ranges: list[tuple[str, int, int, int, bool]] = []
    for cfg in cfgs:
        func_qn = _resolve_cfg_func_qn(cfg, sym_facts_for_file, rel_path, module_name)

        for block in cfg.get_blocks():
            bid = block["id"]
            result["cfg_blocks"].append([
                rel_path, func_qn, bid,
                bid == cfg.entry, bid == cfg.exit,
            ])
            has_content = bool(
                block.get("statements")
                or block.get("defs")
                or block.get("uses")
            )
            # Tree-sitter lines are 0-indexed; convert to 1-indexed
            # for consistency with reference lines and source_loc.
            block_ranges.append((func_qn, bid, block["start_line"] + 1, block["end_line"] + 1, has_content))

        for edge in cfg.get_edges():
            result["cfg_edges"].append([
                rel_path, func_qn,
                edge["from"], edge["to"], edge["kind"], 0, 0,
            ])

    block_ranges.sort(key=lambda x: (x[2], -(x[3] - x[2])))

    # -- source_loc entries for blocks (for unreachable block reporting)
    # Only store blocks with real content (statements, defs, or uses)
    # to avoid reporting empty structural join blocks as unreachable.
    for func_qn_br, bid_br, start_line_br, end_line_br, has_content_br in block_ranges:
        if start_line_br > 0 and has_content_br:
            result["source_locs"].append([
                rel_path, "block", f"{func_qn_br}:{bid_br}",
                start_line_br, 0, end_line_br, 0,
            ])

    # -- Block-tagged references, calls, method_calls
    # Filter out empty structural blocks (exit/join blocks with 0-0 ranges)
    # which get converted to (1,1) and can incorrectly match references.
    content_block_ranges = [br for br in block_ranges if br[4]]
    symbol_ranges = _build_symbol_line_index(sym_facts_for_file, rel_path)

    # Pre-compute definition-site locations to exclude class/fn name
    # references at their own definition line from ref_by_block.
    _sym_def_lines = {(sf.qualified_name, sf.line) for sf in sym_facts_for_file}
    _block_for_line: dict[int, tuple[str, int]] = {}
    for tqn, line, col, kind in file_refs:
        block = _block_for_line.get(line) or _find_containing_block(
            content_block_ranges, line,
        )
        _block_for_line[line] = block
        fq, bid = block
        result["fg_refs"].append([tqn, rel_path, line, col, kind, fq, bid])
        # ref_by_block: only for refs with real block data, excluding
        # definition-site "references" to avoid inflating live_ref.
        if fq and bid >= 0 and (tqn, line) not in _sym_def_lines:
            result["ref_by_block"].append([rel_path, fq, bid, tqn])
            member_name = tqn.rsplit(".", 1)[-1]
            if (
                kind != "call"
                and "." in tqn
                and member_name.startswith("_")
                and not member_name.startswith("__")
            ):
                result["noncall_private_member_refs"].append([
                    rel_path, fq, bid, member_name,
                ])
        else:
            result["module_level_refs"].append([tqn, rel_path, line])

        if kind == "call":
            caller = _enclosing_symbol(symbol_ranges, line)
            caller_qn = caller if caller is not None else module_name
            result["calls"].append([caller_qn, tqn, rel_path, line, col, fq, bid])
            result["calls_by_callee"].append([tqn, caller_qn, rel_path, line, col, fq, bid])
            result["calls_by_file"].append([rel_path, caller_qn, tqn, line, col, fq, bid])

    # Method-call location conventions are shared with the public builders:
    # 0-based lines and explicit sentinels for module-level code.
    raw_method_refs = [
        (qn, line, col, 0, 0, kind, None)
        for qn, line, col, kind in file_refs
    ]
    for fact in _build_method_call_facts(
        raw_method_refs, rel_path, content_block_ranges, normalize_qn=False,
    ):
        result["method_calls"].append([
            fact.file_path, fact.func_qn, fact.receiver, fact.method,
            fact.block_id, fact.line,
        ])

    for du in build_def_use_facts(cfgs, sym_facts_for_file, rel_path, module_name):
        result["def_uses"].append([
            du.file_path, du.func_qn, du.var_name, du.kind,
            du.def_block, du.use_block,
            du.def_line, du.def_col, du.use_line, du.use_col,
        ])

    # CFGs cover functions only.  Preserve module-level data flow using the
    # same reference stream regardless of which builder invoked us.
    from emend.location_resolver import MODULE_LEVEL_BLOCK, MODULE_LEVEL_FUNC
    module_defs: dict[str, list[tuple[int, int]]] = {}
    module_uses: dict[str, list[tuple[int, int]]] = {}
    for qn, line, col, kind in file_refs:
        if _find_containing_block(content_block_ranges, line) != ("", -1):
            continue
        name = qn.rsplit(".", 1)[-1]
        target = module_defs if kind == "write" else module_uses
        if kind in ("write", "read", "call"):
            target.setdefault(name, []).append((line - 1, col))
    for name, uses in module_uses.items():
        for def_line, def_col in module_defs.get(name, []):
            for use_line, use_col in uses:
                result["def_uses"].append([
                    rel_path, MODULE_LEVEL_FUNC, name, "write",
                    MODULE_LEVEL_BLOCK, MODULE_LEVEL_BLOCK,
                    def_line, def_col, use_line, use_col,
                ])

    revision = FileRevision.create(
        project_root,
        abs_path,
        hashlib.md5(content.encode(), usedforsecurity=False).hexdigest(),
        ext,
        module_name,
    )
    qnames = frozenset(row[0] for row in result["fg_sym"])
    return ExtractedFile(revision=revision, qnames=qnames, rows=result)
