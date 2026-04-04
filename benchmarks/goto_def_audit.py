#!/usr/bin/env python3
"""Audit goto-definition and callers/callees invariants across a Python codebase.

For each function defined in the target source tree, this script:

  1. Parses the file with the ``ast`` module to enumerate all function calls
     that occur inside each function body.

  2. **CALLEES invariant** – calls ``find_callees(F)`` and verifies that every
     call found by the AST walker appears in the returned list.

  3. **GOTO_DEF invariant** – for calls whose callee is defined within the same
     project, calls ``EditorSearchEngine.goto_definition(file, line, col)`` and
     verifies that it returns at least one result.

Violations are printed as a human-readable report (or JSON with ``--json``).

Usage::

    # Audit the emend source tree (default)
    python benchmarks/goto_def_audit.py

    # Audit a different directory
    python benchmarks/goto_def_audit.py --root /path/to/project

    # Limit to N files (quick smoke-test)
    python benchmarks/goto_def_audit.py --max-files 20

    # JSON output
    python benchmarks/goto_def_audit.py --json

    # Save report
    python benchmarks/goto_def_audit.py --json --save report.json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Path setup – allow running as a top-level script
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CallSite:
    """A single function call discovered by the AST walker."""
    file: str
    line: int           # 1-based
    col: int            # 1-based (start of identifier)
    callee_name: str    # Simple name: "bar" for both bar() and self.bar()
    is_method: bool     # True if it is an attribute call (self.bar(), x.foo())
    enclosing_func: str  # Qualified name within the file, e.g. "MyClass.method"


@dataclass
class Violation:
    kind: str          # "CALLEES_MISSING" | "GOTO_DEF_EMPTY"
    file: str
    line: int
    col: int
    enclosing_func: str
    callee_name: str
    detail: str


@dataclass
class AuditResult:
    violations: list[Violation] = field(default_factory=list)
    call_sites_checked: int = 0
    callees_checked: int = 0
    goto_def_checked: int = 0
    files_checked: int = 0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "summary": {
                "files_checked": self.files_checked,
                "call_sites_checked": self.call_sites_checked,
                "callees_checked": self.callees_checked,
                "goto_def_checked": self.goto_def_checked,
                "violations": len(self.violations),
                "elapsed_s": round(self.elapsed_s, 3),
            },
            "violations": [
                {
                    "kind": v.kind,
                    "file": v.file,
                    "line": v.line,
                    "col": v.col,
                    "enclosing_func": v.enclosing_func,
                    "callee_name": v.callee_name,
                    "detail": v.detail,
                }
                for v in self.violations
            ],
        }


# ---------------------------------------------------------------------------
# AST walking
# ---------------------------------------------------------------------------


def _collect_call_sites(file_path: Path) -> list[CallSite]:
    """Parse *file_path* and return one :class:`CallSite` per function call."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, str(file_path))
    except SyntaxError:
        return []

    results: list[CallSite] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            # Stack of (simple_name, start_line) – rebuilt on each function entry
            self._func_stack: list[str] = []

        def _enclosing(self) -> str | None:
            return ".".join(self._func_stack) if self._func_stack else None

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._func_stack.append(node.name)
            self.generic_visit(node)
            self._func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._func_stack.append(node.name)
            self.generic_visit(node)
            self._func_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            enclosing = self._enclosing()
            if enclosing is None:
                # Module-level call – skip (find_callees only works on functions)
                self.generic_visit(node)
                return

            func = node.func
            if isinstance(func, ast.Name):
                callee_name = func.id
                line = func.lineno
                col = func.col_offset + 1  # convert to 1-based
                is_method = False
            elif isinstance(func, ast.Attribute):
                callee_name = func.attr
                # Attribute line/col: the identifier starts at
                # end_col_offset - len(attr) on end_lineno.
                line = getattr(func, "end_lineno", func.lineno)
                end_col = getattr(func, "end_col_offset", None)
                if end_col is not None:
                    col = end_col - len(callee_name) + 1  # 1-based
                else:
                    col = func.col_offset + 1
                is_method = True
            else:
                # Subscript calls, lambdas, etc. – not easy to name
                self.generic_visit(node)
                return

            results.append(
                CallSite(
                    file=str(file_path),
                    line=line,
                    col=col,
                    callee_name=callee_name,
                    is_method=is_method,
                    enclosing_func=enclosing,
                )
            )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> Iterator[Path]:
    for p in sorted(root.rglob("*.py")):
        # Skip __pycache__ and similar
        if any(part.startswith("__pycache__") for part in p.parts):
            continue
        if any(part.startswith(".") for part in p.parts):
            continue
        yield p


def _build_project_symbols(project_root: Path) -> set[str]:
    """Collect all top-level and nested function/class names defined in the project.

    Used to restrict the GOTO_DEF check to calls whose target is plausibly
    defined somewhere in the project (as opposed to stdlib / third-party).
    """
    names: set[str] = set()
    for py_file in _iter_python_files(project_root):
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


# ---------------------------------------------------------------------------
# Audit engine
# ---------------------------------------------------------------------------


def _run_audit(
    project_root: Path,
    *,
    max_files: int | None = None,
    verbose: bool = False,
) -> AuditResult:
    """Run the full invariant audit against *project_root*."""
    from emend.editor_search import EditorSearchEngine
    from emend.transform import find_callees
    from emend.component_selector import ExtendedSelector

    result = AuditResult()
    t0 = time.monotonic()

    if verbose:
        print(f"Indexing project: {project_root}", file=sys.stderr)

    engine = EditorSearchEngine(str(project_root))

    # Collect project-defined names to scope GOTO_DEF checks
    project_names = _build_project_symbols(project_root)

    # Group call sites by (file, enclosing_func) so we can batch find_callees
    # calls instead of calling it once per call site.
    from collections import defaultdict
    file_func_calls: dict[tuple[str, str], list[CallSite]] = defaultdict(list)

    py_files = list(_iter_python_files(project_root))
    if max_files:
        py_files = py_files[:max_files]

    for py_file in py_files:
        for cs in _collect_call_sites(py_file):
            file_func_calls[(cs.file, cs.enclosing_func)].append(cs)
        result.files_checked += 1

    if verbose:
        total_sites = sum(len(v) for v in file_func_calls.values())
        print(
            f"  {result.files_checked} files, "
            f"{len(file_func_calls)} functions, "
            f"{total_sites} call sites",
            file=sys.stderr,
        )

    # -----------------------------------------------------------------------
    # CALLEES invariant
    # -----------------------------------------------------------------------
    # For each (file, func) pair, call find_callees once and check that every
    # AST-discovered call site has a matching entry.
    for (file_str, func_qn), call_sites in sorted(file_func_calls.items()):
        file_path = Path(file_str)
        # Only check the innermost function name (last component of the dot-path)
        # because find_callees uses selector.symbol_path[-1] as the key.
        func_name = func_qn.split(".")[-1]

        selector = ExtendedSelector(
            file_path=file_str,
            symbol_path=[func_name],
            component=None,
            accessor=None,
        )

        try:
            callees = find_callees(selector, project_path=str(project_root))
        except Exception as exc:
            # If find_callees errors, record a violation for each call site.
            for cs in call_sites:
                result.violations.append(
                    Violation(
                        kind="CALLEES_ERROR",
                        file=file_str,
                        line=cs.line,
                        col=cs.col,
                        enclosing_func=func_qn,
                        callee_name=cs.callee_name,
                        detail=f"find_callees raised: {exc}",
                    )
                )
            result.callees_checked += len(call_sites)
            result.call_sites_checked += len(call_sites)
            continue

        callee_names_in_result = {c.name for c in callees}
        callee_lines_in_result = {(c.name, c.line) for c in callees if c.line is not None}

        for cs in call_sites:
            result.callees_checked += 1
            result.call_sites_checked += 1

            if cs.callee_name not in callee_names_in_result:
                result.violations.append(
                    Violation(
                        kind="CALLEES_MISSING",
                        file=file_str,
                        line=cs.line,
                        col=cs.col,
                        enclosing_func=func_qn,
                        callee_name=cs.callee_name,
                        detail=(
                            f"call to '{cs.callee_name}' at line {cs.line} not in "
                            f"find_callees({func_qn!r}). "
                            f"Got: {sorted(callee_names_in_result)}"
                        ),
                    )
                )

    # -----------------------------------------------------------------------
    # GOTO_DEF invariant
    # -----------------------------------------------------------------------
    # For each call site whose callee name exists somewhere in the project,
    # goto_definition should return at least one result.
    for (file_str, func_qn), call_sites in sorted(file_func_calls.items()):
        for cs in call_sites:
            if cs.callee_name not in project_names:
                continue  # likely stdlib / third-party – skip

            result.goto_def_checked += 1
            try:
                gd_result = engine.goto_definition(cs.file, cs.line, cs.col)
            except Exception as exc:
                result.violations.append(
                    Violation(
                        kind="GOTO_DEF_ERROR",
                        file=cs.file,
                        line=cs.line,
                        col=cs.col,
                        enclosing_func=func_qn,
                        callee_name=cs.callee_name,
                        detail=f"goto_definition raised: {exc}",
                    )
                )
                continue

            if not gd_result.items:
                result.violations.append(
                    Violation(
                        kind="GOTO_DEF_EMPTY",
                        file=cs.file,
                        line=cs.line,
                        col=cs.col,
                        enclosing_func=func_qn,
                        callee_name=cs.callee_name,
                        detail=(
                            f"goto_definition returned no results for "
                            f"'{cs.callee_name}' at {cs.file}:{cs.line}:{cs.col}"
                        ),
                    )
                )

    engine.close()
    result.elapsed_s = time.monotonic() - t0
    return result


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _print_report(result: AuditResult, project_root: Path) -> None:
    print()
    print("=" * 72)
    print("  emend goto-definition / callers-callees invariant audit")
    print("=" * 72)
    print(f"  Project root  : {project_root}")
    print(f"  Files checked : {result.files_checked}")
    print(f"  Call sites    : {result.call_sites_checked}")
    print(f"  CALLEES checks: {result.callees_checked}")
    print(f"  GOTO_DEF checks: {result.goto_def_checked}")
    print(f"  Violations    : {len(result.violations)}")
    print(f"  Elapsed       : {result.elapsed_s:.1f}s")
    print()

    if not result.violations:
        print("  No violations found.")
        print()
        return

    # Group by kind
    from itertools import groupby
    by_kind: dict[str, list[Violation]] = {}
    for v in result.violations:
        by_kind.setdefault(v.kind, []).append(v)

    for kind, viols in sorted(by_kind.items()):
        print(f"  [{kind}] {len(viols)} violation(s)")
        print()
        for v in viols[:20]:  # cap display at 20 per kind
            rel = Path(v.file).relative_to(project_root) if Path(v.file).is_relative_to(project_root) else v.file
            print(f"    {rel}:{v.line}:{v.col}  func={v.enclosing_func}  callee={v.callee_name}")
            print(f"      {v.detail[:120]}")
        if len(viols) > 20:
            print(f"    ... and {len(viols) - 20} more")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    default_root = _REPO_ROOT / "src" / "emend"

    parser = argparse.ArgumentParser(
        description="Audit goto-definition and callers/callees invariants.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root,
        help=f"Project root to audit (default: {default_root})",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        metavar="N",
        help="Limit audit to first N Python files (for quick checks).",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output results as JSON.",
    )
    parser.add_argument(
        "--save",
        type=str,
        default=None,
        metavar="FILE",
        help="Save JSON report to FILE.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress to stderr.",
    )
    args = parser.parse_args()

    project_root = args.root.resolve()
    if not project_root.is_dir():
        print(f"ERROR: {project_root} is not a directory.", file=sys.stderr)
        sys.exit(1)

    result = _run_audit(
        project_root,
        max_files=args.max_files,
        verbose=args.verbose,
    )

    if args.json_output or args.save:
        data = result.to_dict()
        data["project_root"] = str(project_root)
        json_str = json.dumps(data, indent=2)
        if args.save:
            Path(args.save).write_text(json_str)
            print(f"Report saved to {args.save}", file=sys.stderr)
        if args.json_output:
            print(json_str)
        else:
            _print_report(result, project_root)
    else:
        _print_report(result, project_root)

    # Exit 1 if there are violations so CI can catch it.
    sys.exit(1 if result.violations else 0)


if __name__ == "__main__":
    main()
