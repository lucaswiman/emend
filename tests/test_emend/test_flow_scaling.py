"""Flow linking must not rescan every occurrence once per call site.

Whole-project traces hold millions of occurrence events.  A join that walks
all of them for each resolved call (or each container mutation) is quadratic
and never finishes on a real codebase, so these tests count full scans of the
event table rather than timing anything.
"""

from pathlib import Path

import pytest

from emend.checks import flow
from emend.checks.flow import (
    CompiledFlowConfig,
    FlowSink,
    FlowSource,
    evaluate_flow_config,
)

CALL_SITES = 40


class _CountingEvents(dict):
    """An event table that records how often it is walked end to end."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scans = 0

    def items(self):
        self.scans += 1
        return super().items()

    def values(self):
        self.scans += 1
        return super().values()

    def keys(self):
        self.scans += 1
        return super().keys()

    def __iter__(self):
        self.scans += 1
        return super().__iter__()


def _count_scans(monkeypatch: pytest.MonkeyPatch, name: str) -> list[_CountingEvents]:
    """Hand *name* a counting event table and return the tables it received."""
    original = getattr(flow, name)
    seen: list[_CountingEvents] = []

    def counted(events, *args, **kwargs):
        table = _CountingEvents(events)
        seen.append(table)
        return original(table, *args, **kwargs)

    monkeypatch.setattr(flow, name, counted)
    return seen


def _config() -> CompiledFlowConfig:
    return CompiledFlowConfig(
        labels=["value"],
        sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "tainted")],
    )


def test_cross_file_call_linking_scans_events_a_constant_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    (tmp_path / "lib.py").write_text(
        "def helper(value):\n"
        "    sink(value)\n"
    )
    calls = "".join(f"    helper({index})\n" for index in range(CALL_SITES))
    (tmp_path / "app.py").write_text(
        "from lib import helper\n"
        "def run():\n"
        f"{calls}"
        "    helper(source())\n"
    )
    tables = _count_scans(monkeypatch, "_add_cross_file_calls")

    violations = evaluate_flow_config(
        _config(),
        [str(tmp_path / "app.py"), str(tmp_path / "lib.py")],
        language="python",
        project_path=str(tmp_path),
    )

    assert [(violation.file_path, violation.line) for violation in violations] == [
        (str(tmp_path / "lib.py"), 2),
    ]
    assert len(tables) == 1
    assert tables[0].scans <= 4, (
        f"{tables[0].scans} full event scans for {CALL_SITES + 1} call sites"
    )


def test_container_mutation_linking_scans_events_a_constant_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    appends = "".join(f"    items.append({index})\n" for index in range(CALL_SITES))
    (tmp_path / "app.py").write_text(
        "def run():\n"
        "    items = []\n"
        f"{appends}"
        "    items.append(source())\n"
        "    sink(items)\n"
    )
    tables = _count_scans(monkeypatch, "_add_container_mutations")

    violations = evaluate_flow_config(
        _config(),
        [str(tmp_path / "app.py")],
        language="python",
        project_path=str(tmp_path),
    )

    assert [violation.line for violation in violations] == [CALL_SITES + 4]
    assert len(tables) == 1
    assert tables[0].scans <= 4, (
        f"{tables[0].scans} full event scans for {CALL_SITES + 1} mutations"
    )


def test_control_path_walk_covers_only_functions_the_value_reaches(tmp_path, monkeypatch):
    """A project's functions are mostly one call-connected component.  The
    control-path walk keeps a liveness set per occurrence, so walking the
    whole component for one source is quadratic in the size of the project."""
    from emend.checks.flow import FlowSanitizer

    chain = 60
    source = "".join(
        f"def step_{index}():\n    return step_{index + 1}()\n\n" for index in range(chain)
    ) + (
        f"def step_{chain}():\n    return 0\n\n"
        "def handler():\n"
        "    clean(0)\n"
        "    step_0()\n"
        "    value = source()\n"
        "    sink(value)\n"
    )
    path = tmp_path / "app.py"
    path.write_text(source)

    walked: list[int] = []
    build = flow._component_value_path_walker

    def counting(sanitizers, scopes, returns, values, control, events, *rest):
        walked.append(len({event.func_id for event in events.values()}))
        return build(sanitizers, scopes, returns, values, control, events, *rest)

    monkeypatch.setattr(flow, "_component_value_path_walker", counting)
    config = CompiledFlowConfig(
        sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        # A sanitizer that is called somewhere makes the rule one that is
        # checked along control paths.
        sanitizers=[FlowSanitizer("clean($X)", "value")],
    )
    found = evaluate_flow_config(
        config, [str(path)], interprocedural=True, project_path=str(tmp_path))

    assert [row.line for row in found] == [chain * 3 + 8]
    # The value never leaves ``handler``; the chain it calls is not walked.
    assert walked == [1]


def test_a_call_into_an_unwalked_function_that_never_returns_ends_the_path(tmp_path):
    """The function that always raises is not walked, because no tainted
    value reaches it, but control still does not come back from it."""
    from emend.checks.flow import FlowSanitizer

    path = tmp_path / "app.py"
    path.write_text(
        "def fail():\n"
        "    raise ValueError()\n"
        "\n"
        "def fail_through():\n"
        "    fail()\n"
        "\n"
        "def returns():\n"
        "    return 0\n"
        "\n"
        "def unreachable():\n"
        "    clean(0)\n"
        "    value = source()\n"
        "    fail_through()\n"
        "    sink(value)\n"
        "\n"
        "def reachable():\n"
        "    value = source()\n"
        "    returns()\n"
        "    sink(value)\n"
    )
    config = CompiledFlowConfig(
        sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")],
    )
    found = evaluate_flow_config(
        config, [str(path)], interprocedural=True, project_path=str(tmp_path))
    assert [row.line for row in found] == [19]
