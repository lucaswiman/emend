"""Dead code detection: symbols, blocks, modules, and safe deletion."""
from __future__ import annotations
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
import fnmatch
import logging
import re as _re
import time

if TYPE_CHECKING:
    from ..component_selector import ExtendedSelector

from emend.errors import BUG_EXCEPTIONS

logger = logging.getLogger(__name__)

@dataclass
class DeadSymbol:
    """A symbol detected as potentially dead (unreferenced) code."""
    file_path: str
    name: str
    kind: str  # 'function', 'class', 'async_function'
    line: int
    selector: str  # e.g. "file.py::func_name"
    reason: str  # Why it's flagged (e.g. "no references found")
    last_reference_commit: str | None = None  # git commit that last touched this symbol


@dataclass
class DeadBlock:
    """An unreachable code block detected as dead code."""
    file_path: str
    func_qn: str
    block_id: int
    start_line: int
    end_line: int


@dataclass
class DeadModule:
    """A module file detected as unused because nothing imports it."""
    file_path: str
    name: str
    module_name: str
    reason: str


def dead_code_result_details(
    result: DeadSymbol | DeadBlock | DeadModule,
) -> tuple[str, int, str, str]:
    """Return ``(name, line, witness, reason)`` for any dead-code result."""
    if isinstance(result, DeadBlock):
        return (
            f"unreachable block in {result.func_qn}",
            result.start_line,
            f"{result.file_path}:{result.start_line}",
            "unreachable code",
        )
    if isinstance(result, DeadModule):
        return result.module_name, 1, result.file_path, result.reason
    return result.name, result.line, result.selector, result.reason


def dead_code_result_to_dict(
    result: DeadSymbol | DeadBlock | DeadModule,
) -> dict[str, object]:
    """Serialize one dead-code result consistently across CLI and MCP."""
    if isinstance(result, DeadBlock):
        return {
            "file_path": result.file_path,
            "func_qn": result.func_qn,
            "kind": "unreachable_block",
            "start_line": result.start_line,
            "end_line": result.end_line,
            "reason": "unreachable code",
        }
    if isinstance(result, DeadModule):
        return {
            "file_path": result.file_path,
            "name": result.name,
            "module_name": result.module_name,
            "kind": "module",
            "reason": result.reason,
        }
    data: dict[str, object] = {
        "file_path": result.file_path,
        "name": result.name,
        "kind": result.kind,
        "line": result.line,
        "selector": result.selector,
        "reason": result.reason,
    }
    if result.last_reference_commit:
        data["last_reference_commit"] = result.last_reference_commit
    return data


# Decorator prefixes that indicate a symbol is an entry point / framework hook.
# These are kept as fallbacks; the config-driven path via _get_entry_point_config()
# is the primary source.
_ENTRY_POINT_DECORATORS = frozenset({
    'app.command', 'app.route', 'app.get', 'app.post', 'app.put',
    'app.delete', 'app.patch',
    'pytest.fixture', 'fixture',
    'staticmethod', 'classmethod', 'property',
    'abstractmethod', 'abc.abstractmethod',
    'override',
    'overload', 'typing.overload',
    'click.command', 'click.group',
    'celery.task',
    'register',
})

# Decorator base names that indicate entry points
_ENTRY_POINT_DECORATOR_BASENAMES = frozenset({
    'route', 'get', 'post', 'put', 'delete', 'patch', 'head', 'options',
    'sync_get', 'sync_post', 'sync_put', 'sync_delete', 'sync_patch',
    'websocket', 'websocket_route',
    'command', 'task', 'hook', 'listener',
    'receiver', 'signal', 'handler', 'middleware',
    'register', 'export',
    'tool',  # MCP tool registration (@mcp_app.tool(), @server.tool(), etc.)
})

# Names that are conventional entry points and should never be flagged
_ENTRY_POINT_NAMES = frozenset({
    'main', 'setup', 'teardown', 'configure',
    'setUp', 'tearDown', 'setUpClass', 'tearDownClass',
    'setUpModule', 'tearDownModule',
})

# Receiver types whose methods register framework entry points.  The
# config-driven copy in languages/python/config.toml is authoritative; this
# fallback keeps plugin-less/unknown-language use conservative and useful.
_ENTRY_POINT_DECORATOR_TYPE_METHODS = {
    "fastapi.FastAPI": frozenset({
        "api_route", "get", "post", "put", "delete", "patch", "head",
        "options", "websocket", "websocket_route", "exception_handler",
        "on_event", "middleware",
    }),
    "fastapi.APIRouter": frozenset({
        "api_route", "get", "post", "put", "delete", "patch", "head",
        "options", "websocket", "websocket_route",
    }),
    "typer.Typer": frozenset({"callback", "command"}),
}


@lru_cache(maxsize=8)
def _get_entry_point_config(language: str = "python") -> dict:
    """Return the entry-point heuristic config for *language* from config.toml.

    Returns a dict with keys:
        ``decorators``         — frozenset of full decorator names (dotted).
        ``decorator_basenames``— frozenset of decorator base-names (last component).
        ``names``              — frozenset of conventional entry-point function names.
        ``name_prefixes``      — list of name prefixes that mark entry points.
        ``has_dunders``        — bool: whether dunder names are entry points.
        ``decorator_type_methods`` — receiver type to registering methods.

    Falls back to the hardcoded Python frozensets for unknown languages.
    """
    from emend.language_registry import load_config
    config = load_config(language)
    dc = config.get("dead_code", {})
    if dc:
        raw_type_methods = dc.get("entry_point_decorator_type_methods", {})
        if not isinstance(raw_type_methods, dict):
            raw_type_methods = {}
        return {
            "decorators": frozenset(dc.get("entry_point_decorators", [])),
            "decorator_basenames": frozenset(dc.get("entry_point_decorator_basenames", [])),
            "names": frozenset(dc.get("entry_point_names", [])),
            "name_prefixes": list(dc.get("entry_point_name_prefixes", [])),
            "has_dunders": bool(dc.get("has_dunders", False)),
            "decorator_type_methods": {
                str(type_name): frozenset(str(method) for method in methods)
                for type_name, methods in raw_type_methods.items()
            },
        }
    # Fallback for unknown languages: use Python defaults
    return {
        "decorators": _ENTRY_POINT_DECORATORS,
        "decorator_basenames": _ENTRY_POINT_DECORATOR_BASENAMES,
        "names": _ENTRY_POINT_NAMES,
        "name_prefixes": ["test_", "Test", "describe_"],
        "has_dunders": True,
        "decorator_type_methods": _ENTRY_POINT_DECORATOR_TYPE_METHODS,
    }


def _is_dunder(name: str) -> bool:
    """Check if a name is a dunder (double underscore) name."""
    return name.startswith('__') and name.endswith('__') and len(name) > 4


def _is_likely_entry_point(
    name: str,
    kind: str,
    decorators: list[str],
    depth: int,
    language: str = "python",
) -> bool:
    """Check if a symbol is likely an entry point based on heuristics.

    Entry points are symbols that are invoked by frameworks or conventions
    rather than explicit code references.

    Args:
        name: Symbol name.
        kind: Symbol kind (function, class, method, …).
        decorators: List of decorator strings applied to the symbol.
        depth: Nesting depth (1 = top-level).
        language: Source language — loads heuristics from config.toml.
            Defaults to ``"python"`` for backward compatibility.
    """
    ep = _get_entry_point_config(language)

    # Dunder methods/functions are entry points only for languages that have them.
    if ep["has_dunders"] and _is_dunder(name):
        return True

    # Conventional entry-point names
    if name in ep["names"]:
        return True

    # Name-prefix heuristics (e.g. test_, Test, describe_)
    for prefix in ep["name_prefixes"]:
        if name.startswith(prefix):
            return True

    # Private names (single underscore prefix) at depth > 1 are methods,
    # which may be called via getattr or framework internals.
    # We only flag private top-level symbols.

    # Check decorators
    for dec in decorators:
        # Strip @ prefix if present (Python style: @app.route)
        # Also strip Rust attribute wrapper: #[test] → test
        dec_name = dec
        if dec_name.startswith('#[') and dec_name.endswith(']'):
            dec_name = dec_name[2:-1]
        elif dec_name.startswith('@'):
            dec_name = dec_name[1:]
        # Strip arguments: @app.command("name") -> app.command
        if '(' in dec_name:
            dec_name = dec_name[:dec_name.index('(')]
        dec_name = dec_name.strip()

        if dec_name in ep["decorators"]:
            return True

        # Check basename: @anything.route -> "route" is entry point
        basename = dec_name.rsplit('.', 1)[-1] if '.' in dec_name else dec_name
        if basename in ep["decorator_basenames"]:
            return True

    return False


def _get_last_reference_commit(file_path: str, symbol_name: str) -> str | None:
    """Use ``git log -S`` to find the last commit that added/removed *symbol_name*.

    Returns a one-line summary like ``abc1234 2024-01-15 Fix: remove usage``
    or None if git is unavailable or nothing found.
    """
    import subprocess
    cwd = str(Path(file_path).resolve().parent)
    try:
        result = subprocess.run(
            ['git', 'log', '-S', symbol_name, '--format=%h %ai %s',
             '-1', '--', file_path],
            capture_output=True, text=True, timeout=10,
            cwd=cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass  # git missing, not a repo, or timed out
    return None


def _string_literal_filter(
    candidates: list["DeadSymbol"],
    scan_root: str,
    all_files: bool,
    exclude_references_from: list[str] | None,
    exclude_test_references: bool,
) -> list["DeadSymbol"]:
    """Filter out dead code candidates referenced inside string literals.

    Scans project source files and suppresses a candidate when its name
    appears within at least one *string literal* (e.g. ``getattr(mod,
    "func_name")`` or a registry dict key).  This reduces false positives
    from dynamic dispatch, serialization, and similar patterns.

    Unlike a raw substring scan, mentions in comments or as substrings of
    unrelated code identifiers do **not** keep a symbol alive: only string
    literals (extracted via tree-sitter through
    ``emend_core.collect_string_literals``) count.  Docstrings are string
    literals and therefore do count, matching the documented "string
    literal" semantics; a symbol whose name appears only in a comment is
    still reported dead.
    """
    from .project_iter import _collect_source_files
    str_names = {d.name for d in candidates if len(d.name) > 3}
    if not str_names:
        return candidates

    source_files = _collect_source_files(
        scan_root, git_tracked_only=not all_files,
    )

    _exclude_prefixes: list[str] = []
    _exclude_globs: list[str] = []
    if exclude_references_from:
        import fnmatch as _fnmatch
        for pattern in exclude_references_from:
            if "*" in pattern or "?" in pattern:
                if not pattern.startswith("*") and not Path(pattern).is_absolute():
                    pattern = str(Path(scan_root) / pattern)
                if not pattern.endswith("*"):
                    pattern = pattern.rstrip("/") + "/*"
                _exclude_globs.append(pattern)
            else:
                raw_path = Path(pattern)
                _exclude_prefixes.append(str(
                    raw_path.resolve()
                    if raw_path.is_absolute()
                    else (Path(scan_root) / raw_path).resolve()
                ))

    def _is_excluded_ref(path: str) -> bool:
        if exclude_test_references:
            from .impact import _is_test_file

            if _is_test_file(path):
                return True
        if _exclude_prefixes and any(path.startswith(p) for p in _exclude_prefixes):
            return True
        if _exclude_globs:
            return any(_fnmatch.fnmatch(path, g) for g in _exclude_globs)
        return False

    def _collect_string_literals(source: str, path: str) -> list[str]:
        """Return the inner text of every string literal in *source*.

        Uses tree-sitter via ``emend_core.collect_string_literals``.  On any
        failure (missing extension, unparseable file) returns an empty list
        so that dead-code analysis never crashes on a bad file; such a file
        simply contributes no string-literal references.
        """
        try:
            from emend import emend_core as _rust  # type: ignore[attr-defined]
            ext = Path(path).suffix.lstrip(".") or "py"
            literals = _rust.collect_string_literals(source, ext)
        except Exception:
            logger.debug(
                "string-literal collection failed for %s", path, exc_info=True,
            )
            return []
        # Each tuple is
        # (start_byte, end_byte, start_line, start_col, end_line, end_col, content)
        return [lit[6] for lit in literals]

    # Cache of resolved-path -> concatenated string-literal text. We only
    # populate entries for files that contain at least one candidate name as
    # a raw substring (cheap prefilter) and then extract the string literals.
    file_str_cache: dict[str, str] = {}
    for _fp in source_files:
        _r = str(Path(_fp).resolve())
        if _is_excluded_ref(_r):
            continue
        try:
            _content = Path(_fp).read_text(errors="replace")
        except OSError:
            continue
        # Cheap prefilter: skip files that don't mention any candidate name.
        if not any(n in _content for n in str_names):
            continue
        literals = _collect_string_literals(_content, _fp)
        if literals:
            file_str_cache[_r] = "\n".join(literals)

    # Concatenate all collected literal text into one project-wide blob and
    # scan it once per unique candidate name. Joining on "\n" prevents a name
    # from matching across a file boundary (identifiers contain no newline), so
    # this preserves the per-file substring semantics exactly. A name counts as
    # referenced if it appears inside a string literal in any scanned file
    # (including its own — e.g. a registry dict key or its own docstring).
    project_str_blob = "\n".join(file_str_cache.values())
    referenced_names = {n for n in str_names if n in project_str_blob}

    return [d for d in candidates if d.name not in referenced_names]


# Caching decorators that may need invalidation on mutations
_CACHE_DECORATORS = frozenset({
    'cache', 'lru_cache', 'cached_property', 'cache_page',
    'cache_control', 'memoize', 'cacheable',
})

# Regex for detecting a name inside string literals (matches dead code approach)
_STRING_LITERAL_RE = _re.compile(r"'[^']*'|\"[^\"]*\"")


def _parse_decorator_name(dec: str) -> tuple[str, str]:
    """Return (full_name, basename) from a raw decorator string."""
    dec_clean = dec.lstrip('@').split('(')[0].strip()
    dec_basename = dec_clean.rsplit('.', 1)[-1] if '.' in dec_clean else dec_clean
    return dec_clean, dec_basename


# Default decorators that indicate a symbol is an external interface
_EXTERNAL_INTERFACE_DECORATORS = frozenset({
    'app.route', 'app.get', 'app.post', 'app.put', 'app.delete', 'app.patch',
    'router.get', 'router.post', 'router.put', 'router.delete', 'router.patch',
    'api_view', 'action',
    'rpc_endpoint', 'grpc_method',
    'click.command', 'click.group',
    'app.command',
    'strawberry.mutation', 'strawberry.query', 'strawberry.subscription',
    'graphene.resolve',
    'task', 'celery.task', 'shared_task',
    'webhook', 'endpoint',
    'message_handler', 'event_handler',
})

_EXTERNAL_INTERFACE_BASENAMES = frozenset({
    'route', 'get', 'post', 'put', 'delete', 'patch', 'head', 'options',
    'command', 'task', 'endpoint', 'webhook',
    'mutation', 'query', 'subscription',
    'rpc', 'grpc', 'api',
})

# Patterns in callees that indicate async side effects
_ASYNC_SIDE_EFFECT_PATTERNS = frozenset({
    'delay', 'apply_async', 'send_task',
    'submit', 'create_task', 'ensure_future',
    'run_in_executor',
})

# Patterns in callees that indicate I/O or external effects
_SIDE_EFFECT_CALLEE_PATTERNS = {
    'db_write': {'save', 'commit', 'add', 'delete', 'update', 'insert',
                 'execute', 'executemany', 'bulk_create', 'bulk_update'},
    'network': {'request', 'get', 'post', 'put', 'fetch', 'urlopen', 'send'},
    'file_io': {'write', 'open', 'unlink', 'remove', 'rename', 'mkdir'},
    'cache': {'set', 'delete', 'clear', 'invalidate'},
}


@dataclass
class Danger:
    """A potential hazard the agent should know about before editing."""
    level: str  # "high", "medium", "low"
    category: str
    message: str
    evidence: str  # file:line or brief code snippet


@dataclass
class DataFlow:
    """A data input or output of the symbol."""
    name: str
    type_annotation: str | None = None
    flows_to: list[str] | None = None
    flows_from: list[str] | None = None
    note: str | None = None


@dataclass
class SideEffect:
    """A side effect performed by the symbol."""
    kind: str  # 'db_write', 'network', 'file_io', 'cache', 'async_task', 'external_call'
    target: str
    evidence: str


@dataclass
class CallerInfo:
    """A caller of the symbol."""
    symbol: str  # selector-style path
    file: str
    line: int
    kind: str = "direct"  # "direct", "test", "indirect"


@dataclass
class TestInfo:
    """Test coverage information."""
    direct: list[str]
    indirect: list[str]


@dataclass
class SemanticContext:
    """Full semantic dossier on a symbol — the agent's situational awareness."""
    symbol: str  # qualified name
    kind: str
    file: str
    line: int
    end_line: int

    # Signature
    parameters: list[str]
    returns: str | None
    decorators: list[str]
    is_async: bool

    # The whole point — what could bite you
    dangers: list[Danger]

    # Data flow
    data_in: list[DataFlow]
    data_out: list[DataFlow]
    side_effects: list[SideEffect]

    # Relationships
    callers: list[CallerInfo]
    callees: list[str]
    references_count: int

    # Tests
    tests: TestInfo

    def to_dict(self) -> dict:
        """Serialize to JSON-friendly dict."""
        d: dict = {
            "symbol": self.symbol,
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "end_line": self.end_line,
            "signature": {
                "parameters": self.parameters,
                "returns": self.returns,
                "decorators": self.decorators,
                "is_async": self.is_async,
            },
            "dangers": [
                {"level": dg.level, "category": dg.category,
                 "message": dg.message, "evidence": dg.evidence}
                for dg in self.dangers
            ],
            "flow": {
                "data_in": [
                    {k: v for k, v in {
                        "name": di.name, "type": di.type_annotation,
                        "flows_from": di.flows_from, "note": di.note,
                    }.items() if v is not None}
                    for di in self.data_in
                ],
                "data_out": [
                    {k: v for k, v in {
                        "name": do.name, "type": do.type_annotation,
                        "flows_to": do.flows_to, "note": do.note,
                    }.items() if v is not None}
                    for do in self.data_out
                ],
                "side_effects": [
                    {"kind": se.kind, "target": se.target, "evidence": se.evidence}
                    for se in self.side_effects
                ],
            },
            "callers": [
                {"symbol": c.symbol, "file": c.file, "line": c.line, "kind": c.kind}
                for c in self.callers
            ],
            "callees": self.callees,
            "references_count": self.references_count,
            "tests": {
                "direct": self.tests.direct,
                "indirect": self.tests.indirect,
            },
        }
        return d


def semantic_context(
    selector: ExtendedSelector,
    project_path: str | None = None,
    extra_interface_decorators: list[str] | None = None,
) -> SemanticContext:
    """Build a semantic dossier on a symbol.

    Composes callers, callees, references, and heuristic danger
    detection into a single structured result that gives an agent
    full situational awareness before making changes.

    Args:
        selector: Symbol to analyze.
        project_path: Project root (auto-detected if None).
        extra_interface_decorators: Additional decorator names that
            indicate external interfaces.

    Returns:
        SemanticContext with dangers, flow, callers, tests, etc.
    """
    from emend.ast_utils import find_nested_definitions, find_symbol_by_path
    from emend import emend_core as _rust
    from .project_iter import _find_project_root, _collect_source_files
    from .refs import find_callers, find_callees, find_references
    from .impact import _is_test_file

    file_path = selector.file_path
    symbol_path = selector.symbol_path
    if not symbol_path:
        raise ValueError("Symbol path is required for semantic_context")

    project_root = project_path or _find_project_root(file_path)

    # ---- Resolve the symbol -----------------------------------------------
    symbols = find_nested_definitions(file_path)
    target = find_symbol_by_path(symbols, symbol_path)
    if target is None:
        raise ValueError(f"Symbol not found: {'.'.join(symbol_path)}")

    qualified_name = f"{file_path}::{'.'.join(symbol_path)}"
    is_async = target.kind in ('async_function', 'async_method')

    if not Path(file_path).exists():
        raise ValueError(f"File not found: {file_path}")

    # ---- Gather callers (partition test/non-test in one pass) -------------
    callers_list: list[CallerInfo] = []
    test_caller_count = 0
    non_test_caller_count = 0
    try:
        for ref in find_callers(selector, project_path=project_root):
            is_test = _is_test_file(ref.file_path)
            callers_list.append(CallerInfo(
                symbol=ref.file_path + f":{ref.line}",
                file=ref.file_path,
                line=ref.line,
                kind="test" if is_test else "direct",
            ))
            if is_test:
                test_caller_count += 1
            else:
                non_test_caller_count += 1
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("semantic_context: find_callers failed", exc_info=True)

    # ---- Gather callees ---------------------------------------------------
    callees_list: list[str] = []
    try:
        for callee in find_callees(selector, project_path=project_root):
            callees_list.append(callee.qualified_name or callee.name)
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("semantic_context: find_callees failed", exc_info=True)

    # ---- Count references -------------------------------------------------
    ref_count = 0
    try:
        for _ in find_references(selector, project_path=project_root,
                                 include_definition=False, include_imports=False):
            ref_count += 1
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("semantic_context: find_references failed", exc_info=True)

    # ---- Build interface decorators set -----------------------------------
    iface_decorators = set(_EXTERNAL_INTERFACE_DECORATORS)
    iface_basenames = set(_EXTERNAL_INTERFACE_BASENAMES)
    if extra_interface_decorators:
        for d in extra_interface_decorators:
            iface_decorators.add(d)
            if '.' in d:
                iface_basenames.add(d.rsplit('.', 1)[-1])
            else:
                iface_basenames.add(d)

    # ---- Detect dangers ---------------------------------------------------
    dangers: list[Danger] = []

    # Parse decorators once, reuse for interface + caching checks
    parsed_decorators = [_parse_decorator_name(dec) for dec in target.decorators]

    # 1. External interface decorators
    for dec_clean, dec_basename in parsed_decorators:
        if dec_clean in iface_decorators or dec_basename in iface_basenames:
            dangers.append(Danger(
                level="high",
                category="external_interface",
                message=f"Decorated with @{dec_clean} — signature is part of external API/protocol",
                evidence=f"{file_path}:{target.decorator_line_start or target.line_start}",
            ))

    # 2. Async side effects in callees
    for callee_name in callees_list:
        short_name = callee_name.rsplit('.', 1)[-1] if '.' in callee_name else callee_name
        if short_name in _ASYNC_SIDE_EFFECT_PATTERNS:
            dangers.append(Danger(
                level="high",
                category="async_side_effect",
                message=f"Calls {callee_name}() — triggers async/background work that completes after return",
                evidence=f"{file_path} (callee)",
            ))

    # 3. String references to this symbol (dynamic dispatch risk)
    # Uses same regex approach as dead code string scanning
    symbol_name = symbol_path[-1]
    if len(symbol_name) > 3:
        matched: list[tuple[str, str]] = []
        try:
            source_files = _collect_source_files(project_root)
            matched = _rust.read_and_filter_files(source_files, [symbol_name])
        except BUG_EXCEPTIONS:
            raise
        except Exception:
            logger.debug(
                "semantic_context: string-reference scan failed", exc_info=True,
            )
        str_ref_files: list[str] = []
        for fp, content in matched:
            for line_text in content.splitlines():
                if symbol_name not in line_text:
                    continue
                # Strip non-string content; if name disappears, it was in a string
                stripped = _STRING_LITERAL_RE.sub("", line_text)
                if symbol_name in line_text and symbol_name not in stripped:
                    str_ref_files.append(fp)
                    break
        if str_ref_files:
            dangers.append(Danger(
                level="medium",
                category="dynamic_reference",
                message=f"Name '{symbol_name}' appears as string literal — renaming may miss dynamic references",
                evidence=", ".join(str_ref_files[:3]) + (
                    f" (+{len(str_ref_files) - 3} more)" if len(str_ref_files) > 3 else ""
                ),
            ))

    # 4. High fan-out (many callers)
    if non_test_caller_count >= 10:
        dangers.append(Danger(
            level="high",
            category="high_fan_out",
            message=f"Called from {non_test_caller_count} non-test locations — changes have wide blast radius",
            evidence=f"{len(callers_list)} total callers ({non_test_caller_count} non-test)",
        ))
    elif non_test_caller_count >= 5:
        dangers.append(Danger(
            level="medium",
            category="high_fan_out",
            message=f"Called from {non_test_caller_count} non-test locations",
            evidence=f"{len(callers_list)} total callers ({non_test_caller_count} non-test)",
        ))

    # 5. Caching decorators (may need invalidation on mutations)
    for dec_clean, dec_basename in parsed_decorators:
        if dec_basename in _CACHE_DECORATORS:
            dangers.append(Danger(
                level="medium",
                category="caching",
                message=f"Decorated with @{dec_clean} — results are cached, mutations may serve stale data",
                evidence=f"{file_path}:{target.decorator_line_start or target.line_start}",
            ))

    # 6. No test coverage
    if test_caller_count == 0 and target.kind in ('function', 'async_function', 'method', 'async_method'):
        dangers.append(Danger(
            level="medium",
            category="no_test_coverage",
            message="No test files call this symbol directly",
            evidence="0 test callers found",
        ))

    # ---- Build data flow info ---------------------------------------------
    data_in: list[DataFlow] = []
    for param in target.parameters:
        # Parse "name: type = default" or just "name"
        param_name = param.split(':')[0].split('=')[0].strip()
        param_type = None
        if ':' in param:
            param_type = param.split(':', 1)[1].split('=')[0].strip()
        if param_name and param_name not in ('self', 'cls'):
            data_in.append(DataFlow(
                name=param_name,
                type_annotation=param_type,
            ))

    data_out: list[DataFlow] = []
    # Get return type from source if available
    # (NestedSymbol doesn't have returns, so we check SymbolInfo)
    try:
        from emend.query import query_symbols
        sym_infos = query_symbols(file_path, selector_str=qualified_name)
        if sym_infos and sym_infos[0].returns:
            data_out.append(DataFlow(
                name="return",
                type_annotation=sym_infos[0].returns,
            ))
    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug(
            "semantic_context: return-type lookup failed", exc_info=True,
        )

    # ---- Detect side effects from callees ---------------------------------
    # Build a prefix to identify local-scope callees (e.g., set.add on local vars)
    from .project_iter import _file_to_module
    _module = _file_to_module(file_path, project_root)
    _local_prefix = f"{_module}.{'.'.join(symbol_path)}."
    side_effects: list[SideEffect] = []
    for callee_name in callees_list:
        # Skip builtins, unqualified names, and local-scope operations
        if (callee_name.startswith('builtins.') or
                '.' not in callee_name or
                callee_name.startswith(_local_prefix)):
            continue
        short = callee_name.rsplit('.', 1)[-1]
        for effect_kind, patterns in _SIDE_EFFECT_CALLEE_PATTERNS.items():
            if short in patterns:
                side_effects.append(SideEffect(
                    kind=effect_kind,
                    target=callee_name,
                    evidence=f"calls {callee_name}()",
                ))
                break
        if short in _ASYNC_SIDE_EFFECT_PATTERNS:
            side_effects.append(SideEffect(
                kind="async_task",
                target=callee_name,
                evidence=f"calls {callee_name}()",
            ))

    # ---- Classify tests ---------------------------------------------------
    direct_tests = [c.symbol for c in callers_list if c.kind == "test"]
    tests = TestInfo(direct=direct_tests, indirect=[])

    return SemanticContext(
        symbol=qualified_name,
        kind=target.kind,
        file=file_path,
        line=target.line_start,
        end_line=target.line_end,
        parameters=target.parameters,
        returns=data_out[0].type_annotation if data_out else None,
        decorators=target.decorators,
        is_async=is_async,
        dangers=dangers,
        data_in=data_in,
        data_out=data_out,
        side_effects=side_effects,
        callers=callers_list,
        callees=callees_list,
        references_count=ref_count,
        tests=tests,
    )


def _resolve_import_identity(name: str, imports: dict[str, str]) -> str:
    """Resolve the leading name component through a file's import table."""
    root, dot, suffix = name.partition(".")
    imported = imports.get(root)
    if imported is None:
        return name
    return f"{imported}.{suffix}" if dot else imported


def _python_metadata_entry_points(project_root: str) -> set[str]:
    """Read exact Python callable entry points from packaging metadata."""
    targets: set[str] = set()

    def _add(raw_target: object) -> None:
        if isinstance(raw_target, dict):
            raw_target = raw_target.get("callable")
        if not isinstance(raw_target, str):
            return
        target = raw_target.strip().split(maxsplit=1)[0]
        module, separator, symbol = target.partition(":")
        if separator and module and symbol:
            # A dotted callable (``pkg.cli:App.run``) also makes its owning
            # class live. Module liveness is derived by qualified-name prefix.
            symbol_parts = [part for part in symbol.split(".") if part]
            for index in range(1, len(symbol_parts) + 1):
                targets.add(f"{module}.{'.'.join(symbol_parts[:index])}")

    def _add_table_values(table: object) -> None:
        if isinstance(table, dict):
            for value in table.values():
                _add(value)

    pyproject = Path(project_root) / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            with pyproject.open("rb") as handle:
                config = tomllib.load(handle)
            project = config.get("project", {})
            if not isinstance(project, Mapping):
                project = {}
            for table_name in ("scripts", "gui-scripts"):
                _add_table_values(project.get(table_name, {}))
            groups = project.get("entry-points", {})
            if isinstance(groups, dict):
                for table in groups.values():
                    _add_table_values(table)
            tool = config.get("tool", {})
            poetry = tool.get("poetry", {}) if isinstance(tool, Mapping) else {}
            poetry_scripts = (
                poetry.get("scripts", {}) if isinstance(poetry, Mapping) else {}
            )
            _add_table_values(poetry_scripts)
        except (OSError, ValueError, TypeError):
            logger.debug("Could not read entry points from %s", pyproject, exc_info=True)

    setup_cfg = Path(project_root) / "setup.cfg"
    if setup_cfg.is_file():
        try:
            import configparser

            config = configparser.ConfigParser()
            config.read(setup_cfg)
            if config.has_section("options.entry_points"):
                for _group, definitions in config.items("options.entry_points"):
                    for definition in definitions.splitlines():
                        _name, separator, target = definition.partition("=")
                        if separator:
                            _add(target)
        except (OSError, ValueError, configparser.Error):
            logger.debug("Could not read entry points from %s", setup_cfg, exc_info=True)

    return targets


def _path_is_excluded(file_path: str, patterns: list[str] | None) -> bool:
    if not patterns:
        return False
    for pattern in patterns:
        candidates = (pattern, pattern + "*")
        if any(fnmatch.fnmatch(file_path, candidate) for candidate in candidates):
            return True
        if "**" in pattern:
            relaxed = pattern.replace("**", "*")
            if any(
                fnmatch.fnmatch(file_path, candidate)
                for candidate in (relaxed, relaxed + "*")
            ):
                return True
    return False


def _has_python_main_guard(file_path: Path) -> bool:
    """Return whether a Python module has an executable ``__main__`` guard."""
    try:
        source = file_path.read_text(encoding="utf-8")
        if "__name__" not in source or "__main__" not in source:
            return False
        from .patterns import find_pattern

        matches = find_pattern(
            "if $COND:\n    $BODY", str(file_path), source_override=source,
        )
    except (OSError, UnicodeDecodeError):
        return False
    except Exception:
        logger.debug("Main-guard scan failed for %s", file_path, exc_info=True)
        return False
    for match in matches:
        if match.col != 0:
            continue
        condition = match.captures.get("COND", "").replace(" ", "")
        while condition.startswith("(") and condition.endswith(")"):
            condition = condition[1:-1]
        if condition in {
            "__name__=='__main__'",
            '__name__=="__main__"',
            "'__main__'==__name__",
            '"__main__"==__name__',
        }:
            return True
    return False


def _typed_decorator_entry_points(
    graph,
    project_root: str,
    type_methods: dict[str, set[str] | frozenset[str]],
) -> set[str]:
    """Resolve decorated entry points by receiver type without inferring types.

    Direct constructor assignments are followed through structured imports
    using tree-sitter patterns.  If an explicit type index already exists, it
    is consumed read-only; a cache miss never starts a type checker.
    """
    if not type_methods:
        return set()

    try:
        decorator_rows = graph._client.run(
            "?[qn, fp, dec] := *decorator_on[qn, dec], "
            "*symbol[qn, fp, _, _, _, _, _]"
        )["rows"]
    except Exception:
        logger.debug("Typed decorator fact query failed", exc_info=True)
        return set()

    registered_methods = {
        method for methods in type_methods.values() for method in methods
    }
    decorated_by_file: dict[str, list[tuple[str, str]]] = {}
    for qn, file_path, decorator in decorator_rows:
        if "." not in decorator or decorator.rsplit(".", 1)[-1] not in registered_methods:
            continue
        decorated_by_file.setdefault(file_path, []).append((qn, decorator))
    if not decorated_by_file:
        return set()

    relevant_files = set(decorated_by_file)
    try:
        import_rows = graph._client.run(
            "?[fp, mod, name, alias] := *import[fp, mod, name, _, alias]"
        )["rows"]
        type_rows = graph._client.run(
            "?[fp, qn, ts] := *type_binding[qn, fp, _, _, ts]"
        )["rows"]
    except Exception:
        logger.debug("Typed decorator support-fact query failed", exc_info=True)
        return set()

    imports_by_file: dict[str, dict[str, str]] = {}
    for file_path, module, imported_name, alias in import_rows:
        if file_path not in relevant_files:
            continue
        file_imports = imports_by_file.setdefault(file_path, {})
        if imported_name:
            local_name = alias or imported_name
            identity = f"{module}.{imported_name}".strip(".")
        else:
            local_name = alias or module.split(".", 1)[0]
            identity = module if alias else local_name
        if local_name:
            file_imports[local_name] = identity

    graph_types_by_file: dict[str, list[tuple[str, str]]] = {}
    for file_path, qn, type_name in type_rows:
        if file_path in relevant_files:
            graph_types_by_file.setdefault(file_path, []).append((qn, type_name))

    known_types = set(type_methods)
    resolved_entry_points: set[str] = set()
    root = Path(project_root).resolve()

    for file_path, decorators in decorated_by_file.items():
        imports = imports_by_file.get(file_path, {})
        receivers = {decorator.rsplit(".", 1)[0] for _, decorator in decorators}
        receiver_types: dict[str, str] = {}

        # Consume types already present in facts.db.
        for binding_qn, raw_type in graph_types_by_file.get(file_path, []):
            binding_name = binding_qn.rsplit(".", 1)[-1]
            if binding_name not in receivers:
                continue
            resolved_type = _resolve_import_identity(raw_type, imports)
            if resolved_type in known_types:
                receiver_types[binding_name] = resolved_type

        abs_path = Path(file_path)
        if not abs_path.is_absolute():
            abs_path = root / abs_path
        try:
            source = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Read a prior type index if available, but never populate it here.
        try:
            import hashlib
            from emend.type_oracle import load_cached_file_types, parse_type_string

            cached_types = load_cached_file_types(
                abs_path,
                project_root=root,
                content_hash=hashlib.md5(
                    source.encode("utf-8"), usedforsecurity=False,
                ).hexdigest(),
            )
            if cached_types is not None:
                for receiver in receivers:
                    for binding in cached_types.types_for_name(receiver):
                        parsed = parse_type_string(binding.raw_type)
                        resolved_type = _resolve_import_identity(parsed.name, imports)
                        if resolved_type in known_types:
                            receiver_types[receiver] = resolved_type
                            break
        except Exception:
            logger.debug("Cached decorator type lookup failed for %s", abs_path, exc_info=True)

        # Follow ``receiver = ImportedType(...)`` and simple aliases using
        # structural tree-sitter patterns.  No source regex parsing is used.
        try:
            from .patterns import find_pattern

            assignments = find_pattern(
                "$TARGET = $VALUE", str(abs_path), source_override=source,
                not_inside="def",
            )
            calls = find_pattern(
                "$FACTORY($...ARGS)", str(abs_path), source_override=source,
                not_inside="def",
            )
        except Exception:
            logger.debug("Decorator receiver scan failed for %s", abs_path, exc_info=True)
            assignments = []
            calls = []

        constructor_types = {}
        for call in calls:
            factory = call.captures.get("FACTORY", "").strip()
            identity = _resolve_import_identity(factory, imports)
            if identity in known_types:
                constructor_types[call.matched_text] = identity

        changed = True
        while changed:
            changed = False
            for assignment in assignments:
                target = assignment.captures.get("TARGET", "").strip()
                value = assignment.captures.get("VALUE", "").strip()
                if not target or target in receiver_types:
                    continue

                resolved_type = receiver_types.get(value) or constructor_types.get(value)
                if resolved_type is not None:
                    receiver_types[target] = resolved_type
                    changed = True

        for symbol_qn, decorator in decorators:
            receiver, method = decorator.rsplit(".", 1)
            receiver_type = receiver_types.get(receiver)
            if receiver_type and method in type_methods[receiver_type]:
                resolved_entry_points.add(symbol_qn)

    return resolved_entry_points


def find_dead_code(
    project_path: str,
    kind: str | None = None,
    include_private: bool = True,
    exclude_references_from: list[str] | None = None,
    exclude_test_references: bool = True,
    strings_count_as_references: bool = True,
    show_last_reference: bool = True,
    all_files: bool = False,
    entry_point_decorators: list[str] | None = None,
    entry_point_names: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    unused_modules: bool = True,
) -> Iterator[DeadSymbol | DeadBlock | DeadModule]:
    """Find potentially dead (unreferenced) code in a project.

    Uses ``dead_code_unified()`` Datalog query over the FactGraph for
    combined reachable-block + unreferenced-symbol detection.  String
    literal filtering stays as a Python post-filter.

    Args:
        project_path: Project root directory.
        kind: Optional filter: 'function', 'class', or None for all.
        include_private: If True (default), include _private symbols and
            unreferenced private methods on otherwise-live classes.
        exclude_references_from: Directories/globs to exclude when scanning for
            references (e.g. ``["tests/"]``).  Symbols are still collected from
            these paths but references *in* them are ignored.
        exclude_test_references: If True (default), references from recognized
            test files do not keep production symbols or modules alive.
        strings_count_as_references: If True (default), string literals that
            contain the symbol name are treated as references.  This reduces
            false positives from dynamic dispatch, serialization, and similar.
        show_last_reference: If True (default), annotate each dead symbol with
            the last ``git log -S`` commit that touched its name.
        all_files: If True, scan all Python files (including untracked).
            By default only git-tracked files are scanned when inside a
            git repository.
        entry_point_decorators: Additional decorator names to treat as entry
            points (e.g. ``["my_framework.handler"]``).  Symbols with these
            decorators are never flagged as dead code.
        entry_point_names: Additional function/class names to treat as entry
            points (e.g. ``["plugin_init"]``).  Symbols with these names are
            never flagged as dead code.
        exclude_paths: Directories to exclude entirely from dead code analysis.
            Symbols defined in these paths are never reported.
        unused_modules: If True (default), also report Python module files that
            have no incoming imports from non-excluded project files.

    Yields:
        DeadBlock items for unreachable code blocks, then DeadSymbol objects
        sorted by file path and line number, then optional DeadModule objects.
    """
    if kind not in {None, "function", "class"}:
        raise ValueError("kind must be 'function', 'class', or None")

    from .project_iter import _find_project_root, _file_to_module, _collect_source_files, detect_project_languages
    from .refs import _get_or_build_fact_graph
    from .impact import _is_test_file
    from .index import _NOQA_RE
    t0 = time.monotonic()
    scan_root = str(Path(project_path).resolve())

    # Build the FactGraph and run the unified Datalog dead code query.
    graph = _get_or_build_fact_graph(project_path)

    # Detect project languages and merge entry-point configs from all.
    detected_langs = detect_project_languages(scan_root)
    all_ep_decorators: list[str] = []
    all_ep_basenames: list[str] = []
    all_ep_names: list[str] = []
    all_ep_prefixes: list[str] = []
    all_ep_type_methods: dict[str, set[str]] = {}
    for lang in (detected_langs or ["python"]):
        ep = _get_entry_point_config(lang)
        all_ep_decorators.extend(ep["decorators"])
        all_ep_basenames.extend(ep["decorator_basenames"])
        all_ep_names.extend(ep["names"])
        all_ep_prefixes.extend(ep["name_prefixes"])
        for type_name, methods in ep["decorator_type_methods"].items():
            all_ep_type_methods.setdefault(type_name, set()).update(methods)
    # Add user-supplied overrides
    if entry_point_decorators:
        all_ep_decorators.extend(entry_point_decorators)
        all_ep_basenames.extend(
            d.rsplit(".", 1)[-1] for d in entry_point_decorators
        )
    if entry_point_names:
        all_ep_names.extend(entry_point_names)

    project_root_resolved = str(Path(_find_project_root(project_path)).resolve())

    # Convert exclude_references_from to relative paths for the fact graph.
    excl_ref_paths: list[str] | None = None
    excl_ref_segments: list[str] | None = None  # For ** glob patterns
    if exclude_references_from:
        excl_ref_paths = []
        excl_ref_segments = []
        for excl_path in exclude_references_from:
            if excl_path.startswith("**/"):
                # Extract directory segment for str_includes matching
                segment = excl_path[3:].rstrip("/")
                if segment:
                    excl_ref_segments.append(segment)
            elif "*" in excl_path or "?" in excl_path:
                continue  # Complex globs not supported in Datalog
            else:
                raw_path = Path(excl_path)
                resolved = str(
                    raw_path.resolve()
                    if raw_path.is_absolute()
                    else (Path(project_root_resolved) / raw_path).resolve()
                )
                try:
                    rel = str(Path(resolved).relative_to(project_root_resolved))
                except ValueError:
                    rel = resolved
                excl_ref_paths.append(rel)

    # Test references are ignored by default, but exact file matching avoids
    # broad directory-name guesses (e.g. a production ``contest/`` package).
    excluded_test_files: list[str] = []
    if exclude_test_references:
        try:
            ref_files = graph._client.run(
                "?[fp] := *reference[_, fp, _, _, _, _, _]"
            )["rows"]
            excluded_test_files = sorted({
                file_path for (file_path,) in ref_files
                if _is_test_file(str(Path(project_root_resolved) / file_path))
            })
        except Exception:
            logger.debug("Could not enumerate test reference files", exc_info=True)

    exact_entry_points = _python_metadata_entry_points(project_root_resolved)
    exact_entry_points.update(_typed_decorator_entry_points(
        graph, project_root_resolved, all_ep_type_methods,
    ))

    raw_dead, raw_unreachable = graph.dead_code_unified(
        entry_point_decorators=all_ep_decorators + all_ep_basenames,
        entry_point_names=all_ep_names,
        entry_point_prefixes=all_ep_prefixes,
        exclude_reference_paths=excl_ref_paths if excl_ref_paths else None,
        exclude_reference_segments=excl_ref_segments if excl_ref_segments else None,
        exclude_reference_files=excluded_test_files or None,
        entry_point_qualified_names=sorted(exact_entry_points) or None,
    )

    # Build file content cache for noqa checking
    _file_lines_cache: dict[str, list[str]] = {}

    def _has_noqa(fp: str, line: int) -> bool:
        if fp not in _file_lines_cache:
            try:
                _file_lines_cache[fp] = Path(fp).read_text(errors="replace").splitlines()
            except OSError:
                _file_lines_cache[fp] = []
        lines = _file_lines_cache[fp]
        if 0 < line <= len(lines):
            if _NOQA_RE.search(lines[line - 1]):
                return True
        return False

    # Convert SymbolFact results to DeadSymbol, applying Python post-filters.
    dead_symbols: list[DeadSymbol] = []
    for sym in raw_dead:
        abs_fp = (
            str(Path(project_root_resolved) / sym.file_path)
            if not Path(sym.file_path).is_absolute()
            else sym.file_path
        )

        # Kind filter
        if kind == "function" and sym.kind not in ("function", "async_function"):
            continue
        if kind == "class" and sym.kind != "class":
            continue

        # Private filter
        if not include_private and sym.name.startswith("_") and not sym.name.startswith("__"):
            continue

        # Exclude paths filter
        if _path_is_excluded(abs_fp, exclude_paths):
            continue

        # noqa suppression
        if _has_noqa(abs_fp, sym.line):
            continue

        # Skip symbols in test files — they are entry points by convention
        if _is_test_file(abs_fp):
            continue

        dead_symbols.append(DeadSymbol(
            file_path=abs_fp,
            name=sym.name,
            kind=sym.kind,
            line=sym.line,
            selector=f"{abs_fp}::{sym.qualified_name}",
            reason="no references found",
        ))

    # String-literal post-filter
    if strings_count_as_references:
        dead_symbols = _string_literal_filter(
            dead_symbols, scan_root, all_files, exclude_references_from,
            exclude_test_references,
        )

    dead_symbols.sort(key=lambda symbol: (symbol.file_path, symbol.line))

    logger.info(
        "dead_code: %d dead symbols in %.3fs",
        len(dead_symbols), time.monotonic() - t0,
    )

    def _reference_file_is_excluded(file_path: str) -> bool:
        # A reference file is excluded only when it actually matches one of
        # the configured exclude patterns. Test files are not special-cased:
        # excluding an unrelated directory (e.g. legacy/) must not silently
        # drop test-file imports and produce false-positive "unused module"
        # reports. Test references are handled separately by the default
        # ``exclude_test_references`` policy.
        if exclude_test_references and _is_test_file(file_path):
            return True
        if not exclude_references_from:
            return False
        try:
            rel_path = str(Path(file_path).resolve().relative_to(project_root_resolved))
        except ValueError:
            rel_path = file_path

        for pattern in exclude_references_from:
            if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(rel_path, pattern):
                return True
            if fnmatch.fnmatch(file_path, pattern + "*") or fnmatch.fnmatch(rel_path, pattern + "*"):
                return True
            if pattern.startswith("**/"):
                segment = pattern[3:].rstrip("/")
                if segment and segment in Path(rel_path).parts:
                    return True
        return False

    # Batch all block line-range lookups into a single source_loc query
    # instead of running one query per unreachable block.
    needed_loc_keys = {
        (ub.file_path, f"{ub.func_qn}:{ub.block_id}") for ub in raw_unreachable
    }
    block_loc_index: dict[tuple[str, str], tuple[int, int]] = {}
    if needed_loc_keys:
        loc_rows: list = []
        try:
            loc_rows = graph._client.run(
                '?[fp, loc_id, line, end_line] := '
                '*source_loc[fp, "block", loc_id, line, _, end_line, _]'
            )["rows"]
        except Exception:
            logger.debug("source_loc block query failed", exc_info=True)
        for fp, loc_id, line, end_line in loc_rows:
            if (fp, loc_id) in needed_loc_keys:
                block_loc_index.setdefault((fp, loc_id), (line, end_line))

    # Yield unreachable blocks first when the caller requested all result kinds.
    for ub in (raw_unreachable if kind is None else []):
        abs_fp = (
            str(Path(project_root_resolved) / ub.file_path)
            if not Path(ub.file_path).is_absolute()
            else ub.file_path
        )
        loc = block_loc_index.get((ub.file_path, f"{ub.func_qn}:{ub.block_id}"))
        if loc is None:
            continue  # Skip blocks without line info
        start_line, end_line = loc

        # Skip blocks with no real lines
        if start_line <= 0:
            continue

        yield DeadBlock(
            file_path=abs_fp,
            func_qn=ub.func_qn,
            block_id=ub.block_id,
            start_line=start_line,
            end_line=end_line,
        )

    if show_last_reference and dead_symbols:
        from concurrent.futures import ThreadPoolExecutor

        history_keys = list(dict.fromkeys(
            (symbol.file_path, symbol.name) for symbol in dead_symbols
        ))
        with ThreadPoolExecutor() as pool:
            commits = dict(zip(
                history_keys,
                pool.map(lambda key: _get_last_reference_commit(*key), history_keys),
            ))
        for symbol in dead_symbols:
            symbol.last_reference_commit = commits[(symbol.file_path, symbol.name)]
            yield symbol
    else:
        yield from dead_symbols

    if kind is not None or not unused_modules:
        return

    source_files = _collect_source_files(
        project_root_resolved,
        language="python",
        git_tracked_only=not all_files,
    )
    imported_targets: set[str] = set()

    def _record_import(importing_path: Path, module: str, name: str = "") -> None:
        if module.startswith("."):
            level = len(module) - len(module.lstrip("."))
            importing_module = _file_to_module(
                str(importing_path), project_root_resolved,
            )
            package_parts = importing_module.split(".")
            if importing_path.name != "__init__.py":
                package_parts = package_parts[:-1]
            if level > 1:
                package_parts = package_parts[:max(0, len(package_parts) - level + 1)]
            suffix = module[level:]
            if suffix:
                package_parts.extend(suffix.split("."))
            module = ".".join(package_parts)
        if module:
            imported_targets.add(module)
            if name:
                imported_targets.add(f"{module}.{name}")

    source_file_set = {str(Path(path).resolve()) for path in source_files}
    try:
        import_rows = graph._client.run(
            "?[fp, mod, name] := *import[fp, mod, name, _, _]"
        )["rows"]
        for file_path, module, imported_name in import_rows:
            importing_path = Path(file_path)
            if not importing_path.is_absolute():
                importing_path = Path(project_root_resolved) / importing_path
            importing_path = importing_path.resolve()
            if str(importing_path) not in source_file_set:
                continue
            if _reference_file_is_excluded(str(importing_path)):
                continue
            _record_import(importing_path, module, imported_name)
    except Exception:
        # Compatibility fallback for an older/incomplete facts database.
        logger.debug("Import fact query failed; reparsing modules", exc_info=True)
        from emend.fact_graph import _extract_imports

        for abs_file in source_files:
            abs_path = Path(abs_file).resolve()
            if not abs_path.exists() or _reference_file_is_excluded(str(abs_path)):
                continue
            try:
                content = abs_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for imp in _extract_imports(str(abs_path), content):
                _record_import(abs_path, imp.imported_module, imp.imported_name)

    candidate_modules: list[DeadModule] = []
    scan_root_path = Path(scan_root).resolve()
    for abs_file in source_files:
        abs_path = Path(abs_file).resolve()
        if not abs_path.exists():
            continue
        if not abs_path.is_relative_to(scan_root_path):
            continue
        if abs_path.name in {"__init__.py", "__main__.py", "main.py"}:
            continue
        if _is_test_file(str(abs_path)):
            continue
        if _path_is_excluded(str(abs_path), exclude_paths):
            continue
        if not include_private and abs_path.stem.startswith("_"):
            continue
        module_name = _file_to_module(str(abs_path), project_root_resolved)
        if module_name in imported_targets:
            continue
        if any(
            qualified_name.startswith(f"{module_name}.")
            for qualified_name in exact_entry_points
        ):
            continue
        if _has_python_main_guard(abs_path):
            continue
        candidate_modules.append(
            DeadModule(
                file_path=str(abs_path),
                name=abs_path.stem,
                module_name=module_name,
                reason="module is never imported",
            )
        )

    candidate_modules.sort(key=lambda m: m.file_path)
    yield from candidate_modules


@dataclass
class DeletePlan:
    """A plan for safe-deleting a symbol and its cascade targets."""
    target: str  # selector of the original target
    deletions: list[dict]  # [{selector, file_path, name, kind, line, reason}]
    diffs: dict[str, str]  # file_path -> unified diff


def safe_delete(
    selector: ExtendedSelector,
    cascade: bool = False,
    project_path: str | None = None,
    apply: bool = False,
) -> DeletePlan:
    """Delete a symbol and optionally cascade to newly-dead dependents.

    Without ``--cascade``, removes the target symbol only.  With cascade,
    uses CozoDB Datalog queries on the persisted ``facts.db`` to
    iteratively identify symbols that become dead after the deletion
    (i.e. symbols whose *only* remaining callers are in the delete set)
    and includes them in the plan.

    Args:
        selector: Symbol to delete.
        cascade: If True, transitively delete newly-dead dependents.
        project_path: Project root (auto-detected if None).
        apply: If True, write changes to files.

    Returns:
        A ``DeletePlan`` with the list of deletions and per-file diffs.
    """
    from emend.ast_utils import find_nested_definitions, find_symbol_by_path
    from .project_iter import _find_project_root, _file_to_module, _normalize_module_qn
    from .cache import _get_facts_db
    from .components import _generate_diff

    scan_root = project_path or _find_project_root(selector.file_path)

    # ----- Phase 1: Build the delete set via BFS -------------------------
    delete_set: list[dict] = []  # [{selector_str, file_path, name, kind, line, reason}]
    delete_qns: set[str] = set()  # qualified names already scheduled

    # Seed with the target.
    file_path = str(Path(selector.file_path).resolve())
    symbols = find_nested_definitions(file_path)
    target_sym = find_symbol_by_path(symbols, selector.symbol_path)
    if target_sym is None:
        raise ValueError(
            f"Symbol {'.'.join(selector.symbol_path)} not found in {selector.file_path}"
        )

    module_root = _find_project_root(selector.file_path)
    target_module = _normalize_module_qn(_file_to_module(selector.file_path, module_root))
    target_name = selector.symbol_path[-1]
    target_qn = f"{target_module}.{target_name}" if target_module else target_name
    selector_str = f"{selector.file_path}::{'.'.join(selector.symbol_path)}"

    delete_set.append({
        "selector": selector_str,
        "file_path": file_path,
        "name": target_name,
        "kind": target_sym.kind,
        "line": target_sym.line_start,
        "reason": "target of delete",
    })
    delete_qns.add(target_qn)

    if cascade:
        # Compute cascade via CozoDB queries on the persisted facts.db.
        # Iteratively finds callees of deleted symbols, then checks
        # whether each callee has references outside the delete set.
        fdb = _get_facts_db(scan_root)
        if fdb is not None:
            changed = True
            while changed:
                changed = False
                # Build inline relation for current delete set
                del_rows = ", ".join(f'["{qn}"]' for qn in delete_qns)
                try:
                    # Find callees of deleted symbols that have no
                    # external references (references not from deleted symbols).
                    result = fdb.run(
                        f'deleted[mqn] <- [{del_rows}]\n'
                        # Find callees: symbols called by deleted functions
                        'callee_of_deleted[callee_mqn] := '
                        '  deleted[caller_mqn], '
                        '  *fact_reference[callee_mqn, fp, ref_line, _, kind], kind == "call", '
                        '  *fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
                        '  caller_kind in ["function", "async_function", "method", "async_method"], '
                        '  caller_line <= ref_line, ref_line <= caller_end, '
                        '  not deleted[callee_mqn]\n'
                        # Also match by short qn
                        'callee_of_deleted[callee_mqn] := '
                        '  deleted[caller_mqn], '
                        '  *fact_symbol[_, callee_mqn, _, callee_qn, _, _, _, _, _, _, _, _, _, _, _, _], '
                        '  callee_qn != "", '
                        '  *fact_reference[callee_qn, fp, ref_line, _, kind], kind == "call", '
                        '  *fact_symbol[fp, caller_mqn, _, _, caller_kind, caller_line, caller_end, _, _, _, _, _, _, _, _, _], '
                        '  caller_kind in ["function", "async_function", "method", "async_method"], '
                        '  caller_line <= ref_line, ref_line <= caller_end, '
                        '  not deleted[callee_mqn]\n'
                        # Has external ref: reference from a non-deleted symbol
                        'has_ext_ref[mqn] := '
                        '  callee_of_deleted[mqn], '
                        '  *fact_reference[mqn, ref_fp, ref_line, _, _], '
                        '  *fact_symbol[sym_fp, mqn, _, _, _, sym_line, _, _, _, _, _, _, _, _, _, _], '
                        '  not (ref_fp == sym_fp, ref_line == sym_line), '
                        '  *fact_symbol[ref_fp, ref_mqn, _, _, ref_kind, ref_start, ref_end, _, _, _, _, _, _, _, _, _], '
                        '  ref_kind in ["function", "async_function", "method", "async_method"], '
                        '  ref_start <= ref_line, ref_line <= ref_end, '
                        '  not deleted[ref_mqn]\n'
                        # Cascade candidates: callees with no external refs
                        '?[mqn, name, kind, fp, line] := '
                        '  callee_of_deleted[mqn], not has_ext_ref[mqn], '
                        '  *fact_symbol[fp, mqn, name, _, kind, line, _, depth, _, _, _, _, _, is_entry, is_exported, _], '
                        '  depth == 1, is_entry == false, is_exported == false, '
                        '  not starts_with(name, "test_"), not starts_with(name, "Test"), '
                        '  not (starts_with(name, "__"), ends_with(name, "__"))\n'
                    )
                except Exception:
                    logger.debug("CozoDB cascade query failed", exc_info=True)
                    break
                for row in result["rows"]:
                    mqn, name, sym_kind, fp, line = row
                    if mqn not in delete_qns:
                        # Convert relative path back to absolute.
                        abs_fp = str(Path(scan_root) / fp) if not Path(fp).is_absolute() else fp
                        sym_selector = f"{abs_fp}::{name}"
                        delete_set.append({
                            "selector": sym_selector,
                            "file_path": abs_fp,
                            "name": name,
                            "kind": sym_kind,
                            "line": line,
                            "reason": "only referenced by deleted symbol(s)",
                        })
                        delete_qns.add(mqn)
                        changed = True

    # ----- Phase 2: Apply deletions and collect diffs --------------------
    # Group by file, process in reverse line order to avoid offset shifts.
    from collections import defaultdict
    by_file: dict[str, list[dict]] = defaultdict(list)
    for d in delete_set:
        by_file[d["file_path"]].append(d)

    all_diffs: dict[str, str] = {}

    for fpath, entries in by_file.items():
        fp = Path(fpath)
        if not fp.exists():
            continue
        source_code = fp.read_text()
        lines = source_code.splitlines(keepends=True)

        # Sort by line descending so we remove from bottom first.
        entries.sort(key=lambda e: e["line"], reverse=True)

        for entry in entries:
            from emend.component_selector import parse_extended_selector as _parse_sel
            sel = _parse_sel(entry["selector"])
            syms = find_nested_definitions(fpath)
            sym = find_symbol_by_path(syms, sel.symbol_path)
            if sym is None:
                continue

            start_line = (
                sym.decorator_line_start
                if sym.decorator_line_start is not None
                else sym.line_start
            )
            start_idx = start_line - 1
            end_idx = sym.line_end
            lines = lines[:start_idx] + lines[end_idx:]

        new_code = "".join(lines)
        diff = _generate_diff(fpath, source_code, new_code)
        if diff:
            all_diffs[fpath] = diff
            if apply:
                fp.write_text(new_code)

    return DeletePlan(
        target=selector_str,
        deletions=delete_set,
        diffs=all_diffs,
    )


# visit_project_ts yields (py_file, content, resolver)
