"""Regression tests for bugs found by the goto-definition / callers-callees audit.

Running ``python benchmarks/goto_def_audit.py`` against the emend source tree
found two distinct classes of failure.  Each is reproduced here with a minimal
self-contained test case that does **not** require a Django checkout.

Bugs
----

**Bug #1 – find_callees / find_callers raise when type engine is unavailable**

  ``_get_or_build_fact_graph`` calls ``warm_caches(project_path)`` with the
  default ``type_engine="pyrefly"``.  When pyrefly (or any supported engine) is
  not on PATH the call raises ``TypeEngineUnavailableError`` *before* the
  fallback ``FactGraph.build_from_project()`` is reached, so callers of
  ``find_callees`` / ``find_callers`` see the exception instead of results.

  Repro: monkeypatch ``warm_caches`` to raise and assert the fallback is used.

**Bug #2 – goto_definition returns empty when project root has a malformed
``languages/python/config.toml``**

  ``EditorSearchEngine.goto_definition`` creates a ``PyScopeResolver``
  initialised with ``engine.project_root``.  In any checkout that contains
  ``languages/python/config.toml`` with the line ``[bindings.walrus]`` (a TOML
  dict key) the Rust config loader raises a ``RuntimeError`` because it expects
  ``[[bindings]]`` (a TOML array item).  The ``RuntimeError`` is caught by an
  outer ``except Exception`` block and ``goto_definition`` silently returns
  an empty result for *every* query.

  Repro: plant the malformed config under the project root, then assert that a
  simple same-file or cross-file call still resolves correctly.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write *files* (relative-path → dedented content) under *tmp_path*."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        dest = project / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(textwrap.dedent(content))
    return project


def _write_malformed_config(project_root: Path) -> None:
    """Plant the same ``[bindings.walrus]`` TOML bug as exists in the emend repo."""
    cfg_dir = project_root / "languages" / "python"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        "[bindings.walrus]\n"
        'node = "named_expression"\n'
        'target = "name"\n'
    )


# ---------------------------------------------------------------------------
# Bug #1 – find_callees / find_callers raise on missing type engine
# ---------------------------------------------------------------------------


def test_find_callees_works_without_type_engine(tmp_path, monkeypatch):
    """find_callees should fall back to FactGraph.build_from_project() when the
    type engine is unavailable instead of propagating TypeEngineUnavailableError.

    Current behaviour (BUG): raises TypeEngineUnavailableError.
    Expected behaviour:       returns callee results from the fallback path.
    """
    from emend.type_oracle import TypeEngineUnavailableError
    import emend.transform as transform_mod

    project = _make_project(
        tmp_path,
        {
            "module.py": """\
                def helper():
                    return 42

                def main():
                    return helper()
                """,
        },
    )

    def _raise_unavailable(*args, **kwargs):
        raise TypeEngineUnavailableError("Simulated: type engine not on PATH")

    monkeypatch.setattr(transform_mod, "warm_caches", _raise_unavailable)

    selector = ExtendedSelector(
        file_path=str(project / "module.py"),
        symbol_path=["main"],
        component=None,
        accessor=None,
    )

    # Should NOT raise; should fall back to FactGraph.build_from_project().
    callees = transform_mod.find_callees(selector, project_path=str(project))
    callee_names = {c.name for c in callees}
    assert "helper" in callee_names, (
        f"Expected 'helper' in callees even without a type engine; got {callee_names}"
    )


def test_find_callers_works_without_type_engine(tmp_path, monkeypatch):
    """find_callers should fall back gracefully when the type engine is unavailable.

    Same root cause as test_find_callees_works_without_type_engine.
    """
    from emend.type_oracle import TypeEngineUnavailableError
    import emend.transform as transform_mod

    project = _make_project(
        tmp_path,
        {
            "target.py": """\
                def process(x):
                    return x + 1
                """,
            "caller.py": """\
                from target import process

                def run():
                    return process(42)
                """,
        },
    )

    def _raise_unavailable(*args, **kwargs):
        raise TypeEngineUnavailableError("Simulated: type engine not on PATH")

    monkeypatch.setattr(transform_mod, "warm_caches", _raise_unavailable)

    selector = ExtendedSelector(
        file_path=str(project / "target.py"),
        symbol_path=["process"],
        component=None,
        accessor=None,
    )

    callers = list(transform_mod.find_callers(selector, project_path=str(project)))
    caller_files = {Path(r.file_path).name for r in callers}
    assert "caller.py" in caller_files, (
        f"Expected 'caller.py' in callers even without a type engine; "
        f"got {caller_files}"
    )


# ---------------------------------------------------------------------------
# Bug #2 – goto_definition silent failure with malformed config
# ---------------------------------------------------------------------------


def test_goto_definition_same_file_recursive_with_malformed_config(tmp_path):
    """goto_definition should resolve a recursive same-file call even when the
    project root contains a malformed ``languages/python/config.toml``.

    Current behaviour (BUG): PyScopeResolver raises RuntimeError loading the
    config; the error is silently swallowed; goto_definition returns empty.
    Expected behaviour:       returns the definition at line 1.
    """
    from emend.editor_search import EditorSearchEngine

    project = _make_project(
        tmp_path,
        {
            "module.py": """\
                def factorial(n):
                    if n <= 1:
                        return 1
                    return n * factorial(n - 1)
                """,
        },
    )
    _write_malformed_config(project)

    engine = EditorSearchEngine(str(project))
    try:
        # Line 4: '    return n * factorial(n - 1)'
        # 'factorial' starts at col 20 (1-based, after dedent → 4 spaces + "return n * ")
        source_line = (project / "module.py").read_text().splitlines()[3]
        col = source_line.index("factorial") + 1  # 1-based
        result = engine.goto_definition(str(project / "module.py"), line=4, col=col)
        assert result.items, (
            "goto_definition returned empty for a same-file recursive call "
            "when the project root has a malformed config."
        )
        assert result.items[0]["line"] == 1, (
            f"Expected definition at line 1, got {result.items}"
        )
    finally:
        engine.close()


def test_goto_definition_cross_file_call_with_malformed_config(tmp_path):
    """goto_definition should resolve an imported function call even when the
    project root contains a malformed ``languages/python/config.toml``.

    Same root cause as the same-file variant: config load failure silences all
    goto_definition results.
    """
    from emend.editor_search import EditorSearchEngine

    project = _make_project(
        tmp_path,
        {
            "helpers.py": """\
                def compute(x):
                    return x * 2
                """,
            "main.py": """\
                from helpers import compute

                def run():
                    return compute(21)
                """,
        },
    )
    _write_malformed_config(project)

    engine = EditorSearchEngine(str(project))
    try:
        # Line 4 of main.py: '    return compute(21)'
        source_line = (project / "main.py").read_text().splitlines()[3]
        col = source_line.index("compute") + 1  # 1-based
        result = engine.goto_definition(str(project / "main.py"), line=4, col=col)
        assert result.items, (
            "goto_definition returned empty for an imported function call "
            "when the project root has a malformed config."
        )
        assert Path(result.items[0]["file_path"]).name == "helpers.py", (
            f"Expected definition in helpers.py, got {result.items}"
        )
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Sanity checks – these should PASS now (no malformed config, no type-engine
# mocking).  They confirm the feature works in a clean environment and serve
# as the baseline against which the bug tests can be compared.
# ---------------------------------------------------------------------------


def test_goto_definition_same_file_baseline(tmp_path):
    """goto_definition resolves a same-file recursive call in a clean project."""
    from emend.editor_search import EditorSearchEngine

    project = _make_project(
        tmp_path,
        {
            "module.py": """\
                def factorial(n):
                    if n <= 1:
                        return 1
                    return n * factorial(n - 1)
                """,
        },
    )
    engine = EditorSearchEngine(str(project))
    try:
        source_line = (project / "module.py").read_text().splitlines()[3]
        col = source_line.index("factorial") + 1
        result = engine.goto_definition(str(project / "module.py"), line=4, col=col)
        assert result.items, "Expected definition for same-file recursive call"
        assert result.items[0]["line"] == 1
    finally:
        engine.close()


def test_callees_invariant_baseline(tmp_path):
    """Every function call inside a function should appear in find_callees results."""
    from emend.transform import find_callees

    project = _make_project(
        tmp_path,
        {
            "module.py": """\
                def helper_a():
                    return 1

                def helper_b():
                    return 2

                def top():
                    a = helper_a()
                    b = helper_b()
                    return a + b
                """,
        },
    )

    selector = ExtendedSelector(
        file_path=str(project / "module.py"),
        symbol_path=["top"],
        component=None,
        accessor=None,
    )

    callees = find_callees(selector, project_path=str(project))
    names = {c.name for c in callees}
    assert "helper_a" in names, f"helper_a missing from callees: {names}"
    assert "helper_b" in names, f"helper_b missing from callees: {names}"
