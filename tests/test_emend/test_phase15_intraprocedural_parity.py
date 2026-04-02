"""Phase 15: Intraprocedural Datalog parity differential tests.

These tests exercise edge cases where the Python and Datalog intraprocedural
trace engines may diverge: container mutations, for-loop iteration variable
taint, augmented assignments, module-level code, scope sanitizers, and
path-sensitive sanitizers.

Each scenario has a ``_both_engines`` variant that asserts agreement using
``_assert_engines_agree``.  If a divergence is expected and accepted, it is
marked with ``@pytest.mark.xfail`` and a rationale.

Uses the same helpers from test_phase9_differential.
"""

from __future__ import annotations

import pytest

from emend.trace import (
    TraceConfig,
    TraceSanitizer,
    TraceScopeSanitizer,
    TraceSink,
    TraceSource,
    TraceViolation,
    _run_trace_datalog,
    run_trace_analysis,
)


# ---------------------------------------------------------------------------
# Helpers (same as phase 9)
# ---------------------------------------------------------------------------

def _violation_key(v: TraceViolation) -> tuple[str, int, str, str]:
    return (v.file_path, v.line, v.label, v.sink_pattern)


def _run_both_engines(
    tmp_path,
    source_code: str,
    config: TraceConfig,
) -> tuple[list[TraceViolation], list[TraceViolation] | None]:
    src_file = tmp_path / "app.py"
    src_file.write_text(source_code)
    paths = [str(src_file)]
    project_path = str(tmp_path)

    python_violations = run_trace_analysis(
        paths=paths,
        config=config,
        label_filter=None,
        language="python",
        project_path=project_path,
    )
    for v in python_violations:
        assert v.engine == "python", f"Expected engine='python', got {v.engine!r}"

    datalog_violations = _run_trace_datalog(
        paths=paths,
        config=config,
        label_filter=None,
        language="python",
        project_path=project_path,
    )
    if datalog_violations is not None:
        for v in datalog_violations:
            assert v.engine == "datalog", f"Expected engine='datalog', got {v.engine!r}"

    return python_violations, datalog_violations


def _assert_engines_agree(
    python_violations: list[TraceViolation],
    datalog_violations: list[TraceViolation] | None,
    *,
    context: str = "",
) -> None:
    if datalog_violations is None:
        pytest.skip(f"Datalog engine unavailable{': ' + context if context else ''}")
    py_keys = set(_violation_key(v) for v in python_violations)
    dl_keys = set(_violation_key(v) for v in datalog_violations)
    assert py_keys == dl_keys, (
        f"Engine divergence{': ' + context if context else ''}.\n"
        f"  Python-only:  {py_keys - dl_keys}\n"
        f"  Datalog-only: {dl_keys - py_keys}"
    )


# ---------------------------------------------------------------------------
# Shared configs
# ---------------------------------------------------------------------------

def _sqli_config() -> TraceConfig:
    return TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
    )


def _sqli_sanitized_config() -> TraceConfig:
    return TraceConfig(
        labels=["sqli"],
        sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
        sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
        sanitizers=[TraceSanitizer(pattern="escape($X)", label="sqli")],
    )


# ===========================================================================
# Container mutations
# ===========================================================================

class TestDifferentialContainerMutations:
    """Differential tests for container mutation taint propagation."""

    def test_append_taints_container_both_engines(self, tmp_path):
        """Taint flows from appended value to container."""
        source = """\
def handler():
    user_input = request.args.get("name")
    items = []
    items.append(user_input)
    cursor.execute(items)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect taint via .append()"
        _assert_engines_agree(py_v, dl_v, context="container .append()")

    def test_extend_taints_container_both_engines(self, tmp_path):
        """Taint flows from extended list to container."""
        source = """\
def handler():
    user_input = request.args.get("name")
    items = []
    items.extend([user_input])
    cursor.execute(items)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect taint via .extend()"
        _assert_engines_agree(py_v, dl_v, context="container .extend()")

    @pytest.mark.xfail(
        reason=(
            "Subscript assignment (data['key'] = x) is not tracked as a "
            "container mutation by the Rust CFG builder — the target is "
            "treated as a use, not a definition.  The Datalog engine lacks "
            "a method_call fact for this case.  Accepted divergence."
        ),
        strict=False,
    )
    def test_subscript_assignment_taints_container_both_engines(self, tmp_path):
        """Taint flows from subscript-assigned value to container."""
        source = """\
def handler():
    user_input = request.args.get("name")
    data = {}
    data["key"] = user_input
    cursor.execute(data)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect taint via subscript assignment"
        _assert_engines_agree(py_v, dl_v, context="subscript assignment")

    def test_subscript_read_propagates_taint_both_engines(self, tmp_path):
        """Taint propagates when reading from a tainted container."""
        source = """\
def handler():
    user_input = request.args.get("name")
    data = {"key": user_input}
    value = data["key"]
    cursor.execute(value)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        # Both engines should detect this: data is tainted, value read from it
        assert py_v, "Python engine should detect taint via subscript read"
        _assert_engines_agree(py_v, dl_v, context="subscript read")

    def test_update_taints_container_both_engines(self, tmp_path):
        """Taint flows from dict.update() argument to container."""
        source = """\
def handler():
    user_input = request.args.get("name")
    data = {}
    data.update({"key": user_input})
    cursor.execute(data)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect taint via .update()"
        _assert_engines_agree(py_v, dl_v, context="dict .update()")


# ===========================================================================
# For-loop iteration variables
# ===========================================================================

class TestDifferentialForLoopTaint:
    """Differential tests for for-loop iteration variable taint."""

    def test_for_loop_taints_iteration_var_both_engines(self, tmp_path):
        """Taint flows from tainted iterable to loop variable."""
        source = """\
def handler():
    user_input = request.args.get("name")
    items = [user_input]
    for item in items:
        cursor.execute(item)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect taint via for-loop iteration"
        _assert_engines_agree(py_v, dl_v, context="for-loop iteration variable")

    def test_for_loop_direct_tainted_iterable_both_engines(self, tmp_path):
        """Taint flows when iterating directly over tainted variable."""
        source = """\
def handler():
    user_input = request.args.get("name")
    for char in user_input:
        cursor.execute(char)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect taint via direct iteration"
        _assert_engines_agree(py_v, dl_v, context="for-loop direct tainted iterable")

    def test_for_loop_untainted_iterable_no_violation_both_engines(self, tmp_path):
        """No taint when iterating over untainted collection."""
        source = """\
def handler():
    user_input = request.args.get("name")
    items = ["safe1", "safe2"]
    for item in items:
        cursor.execute(item)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        # items is not tainted (assigned a literal list), so item should not be tainted
        # However, the Python engine may be conservative here
        _assert_engines_agree(py_v, dl_v, context="for-loop untainted iterable")


# ===========================================================================
# Augmented assignment
# ===========================================================================

class TestDifferentialAugmentedAssignment:
    """Differential tests for augmented assignment (+=, etc.) taint propagation."""

    def test_aug_assign_string_concat_both_engines(self, tmp_path):
        """Taint propagates via += string concatenation."""
        source = """\
def handler():
    user_input = request.args.get("name")
    query = "SELECT * FROM users WHERE name = "
    query += user_input
    cursor.execute(query)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect taint via += concatenation"
        _assert_engines_agree(py_v, dl_v, context="augmented assignment +=")

    def test_aug_assign_preserves_existing_taint_both_engines(self, tmp_path):
        """Tainted variable stays tainted after augmented assignment with safe value."""
        source = """\
def handler():
    user_input = request.args.get("name")
    user_input += " suffix"
    cursor.execute(user_input)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should preserve taint after += with safe value"
        _assert_engines_agree(py_v, dl_v, context="augmented assignment preserves taint")

    def test_aug_assign_does_not_taint_from_nothing_both_engines(self, tmp_path):
        """Augmented assignment with safe values does not introduce taint."""
        source = """\
def handler():
    user_input = request.args.get("name")
    safe = "prefix"
    safe += " suffix"
    cursor.execute(safe)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        # safe is never tainted; += with string literal doesn't introduce taint
        _assert_engines_agree(py_v, dl_v, context="augmented assignment no taint")


# ===========================================================================
# Module-level code (extended scenarios)
# ===========================================================================

class TestDifferentialModuleLevelCode:
    """Differential tests for module-level (non-function) taint flows."""

    def test_module_level_intermediate_var_both_engines(self, tmp_path):
        """Taint flows through intermediate variable at module level."""
        source = """\
raw = request.args.get("data")
processed = raw
cursor.execute(processed)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect module-level intermediate taint"
        _assert_engines_agree(py_v, dl_v, context="module-level intermediates")

    def test_module_level_sanitized_both_engines(self, tmp_path):
        """Sanitizer at module level should suppress violation.

        Accepted divergence: the Python engine reports a false positive here
        because its CFG-based sanitizer suppression does not work at module
        level (no CFG is built for module-level code).  The Datalog engine
        correctly suppresses the violation because the sanitizer pattern match
        creates a sanitizer_block on block 0, which blocks taint propagation.
        This divergence will be resolved when the Python engine is removed in
        Phase 17.
        """
        source = """\
raw = request.args.get("data")
safe = escape(raw)
cursor.execute(safe)
"""
        config = _sqli_sanitized_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        # Datalog engine correctly suppresses; Python engine has a false positive.
        if dl_v is not None:
            assert dl_v == [], "Datalog engine should suppress via sanitizer_block"
        # Don't assert parity — accepted divergence (Datalog is correct, Python is not)

    def test_module_level_with_function_mixed_both_engines(self, tmp_path):
        """Module-level taint and function-level taint are independent."""
        source = """\
mod_input = request.args.get("mod")
cursor.execute(mod_input)

def handler():
    func_input = request.args.get("func")
    cursor.execute(func_input)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert len(py_v) >= 2, "Python engine should detect both module and function violations"
        _assert_engines_agree(py_v, dl_v, context="mixed module+function level")

    def test_module_level_container_mutation_both_engines(self, tmp_path):
        """Container mutation taint at module level."""
        source = """\
user_input = request.args.get("name")
items = []
items.append(user_input)
cursor.execute(items)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect module-level container taint"
        _assert_engines_agree(py_v, dl_v, context="module-level container mutation")

    def test_module_level_aug_assign_both_engines(self, tmp_path):
        """Augmented assignment taint at module level."""
        source = """\
user_input = request.args.get("name")
query = "SELECT * FROM users WHERE name = "
query += user_input
cursor.execute(query)
"""
        config = _sqli_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Python engine should detect module-level augmented assignment taint"
        _assert_engines_agree(py_v, dl_v, context="module-level augmented assignment")


# ===========================================================================
# Scope sanitizers
# ===========================================================================

class TestDifferentialScopeSanitizer:
    """Differential tests for scope sanitizer behaviour."""

    def test_scope_sanitizer_kills_taint_both_engines(self, tmp_path):
        """Scope sanitizer kills all taint for the label within scope."""
        source = """\
def handler():
    user_input = request.args.get("name")
    session.commit()
    cursor.execute(user_input)
"""
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
            scope_sanitizers=[TraceScopeSanitizer(pattern="session.commit()", label="sqli")],
        )
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v == [], "Python engine should suppress via scope sanitizer"
        _assert_engines_agree(py_v, dl_v, context="scope sanitizer kills taint")

    def test_scope_sanitizer_before_source_no_effect_both_engines(self, tmp_path):
        """Scope sanitizer before source should NOT suppress later violations."""
        source = """\
def handler():
    session.commit()
    user_input = request.args.get("name")
    cursor.execute(user_input)
"""
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
            scope_sanitizers=[TraceScopeSanitizer(pattern="session.commit()", label="sqli")],
        )
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v, "Scope sanitizer before source should not suppress violation"
        _assert_engines_agree(py_v, dl_v, context="scope sanitizer before source")


# ===========================================================================
# Path-sensitive sanitization
# ===========================================================================

class TestDifferentialPathSensitiveSanitizer:
    """Differential tests for path-sensitive (CFG-aware) sanitization."""

    def test_sanitizer_on_all_paths_suppresses_both_engines(self, tmp_path):
        """Sanitizer on all CFG paths from source to sink suppresses violation."""
        source = """\
def handler(condition):
    user_input = request.args.get("name")
    if condition:
        safe = escape(user_input)
    else:
        safe = escape(user_input)
    cursor.execute(safe)
"""
        config = _sqli_sanitized_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v == [], "Both paths sanitized → no violation"
        _assert_engines_agree(py_v, dl_v, context="sanitizer on all paths")

    def test_sanitizer_on_one_path_does_not_suppress_both_engines(self, tmp_path):
        """Sanitizer on only one branch: violation should persist (all_paths quantifier).

        Accepted divergence: the Python engine processes statements in line order
        and does not track per-path taint state, so it incorrectly suppresses
        the violation.  The Datalog engine correctly detects it via CFG-aware
        unsanitized-reachability.  This divergence will be resolved when the
        Python engine is removed in Phase 17.
        """
        source = """\
def handler(condition):
    user_input = request.args.get("name")
    if condition:
        processed = escape(user_input)
    else:
        processed = user_input
    cursor.execute(processed)
"""
        config = _sqli_sanitized_config()
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        # Python engine limitation: incorrectly suppresses due to line-order
        # processing.  Datalog engine correctly flags the violation.
        if dl_v is not None:
            assert dl_v, "Datalog engine should detect violation (unsanitized else-branch)"
        # Don't assert parity — accepted divergence (Datalog is correct, Python is not)

    def test_some_path_sanitizer_suppresses_both_engines(self, tmp_path):
        """some_path quantifier: sanitizer on any path suppresses violation."""
        source = """\
def handler(condition):
    user_input = request.args.get("name")
    if condition:
        processed = escape(user_input)
    else:
        processed = user_input
    cursor.execute(processed)
"""
        config = TraceConfig(
            labels=["sqli"],
            sources=[TraceSource(pattern="request.args.get($X)", label="sqli")],
            sinks=[TraceSink(pattern="cursor.execute($X)", label="sqli", message="SQL injection")],
            sanitizers=[TraceSanitizer(pattern="escape($X)", label="sqli", quantifier="some_path")],
        )
        py_v, dl_v = _run_both_engines(tmp_path, source, config)
        assert py_v == [], "some_path quantifier: any path sanitized → no violation"
        _assert_engines_agree(py_v, dl_v, context="some_path sanitizer")
