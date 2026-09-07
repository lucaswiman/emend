"""Scope sanitizer behavior through the public flow evaluator."""

import yaml
import pytest

from emend.trace import (
    TraceConfig,
    TraceScopeSanitizer,
    TraceSink,
    TraceSource,
    load_trace_config,
    run_trace_analysis,
)


def _config(*, labels=("lbl",), scope=()):
    return TraceConfig(
        labels=list(labels),
        sources=[TraceSource("tainted()", labels[0])],
        sinks=[TraceSink("use($X)", labels[0], "tainted value used")],
        scope_sanitizers=list(scope),
    )


def _run(path, source, config):
    path.write_text(source)
    return run_trace_analysis([str(path)], config)


def test_scope_sanitizer_dataclass_and_config():
    sanitizer = TraceScopeSanitizer("session.commit()", "lbl")
    assert (sanitizer.pattern, sanitizer.label) == ("session.commit()", "lbl")
    assert _config(scope=[sanitizer]).scope_sanitizers == [sanitizer]


@pytest.mark.parametrize(
    ("body", "labels", "expected"),
    [
        ("session.commit()\n    use(x)", ("lbl",), False),
        ("if flag:\n        session.commit()\n    use(x)", ("lbl",), True),
        ("use(x)\n    session.commit()", ("lbl",), True),
    ],
)
def test_scope_sanitizer_path_coverage(tmp_path, body, labels, expected):
    source = "def process():\n    x = tainted()\n    " + body + "\n"
    config = _config(scope=[TraceScopeSanitizer("session.commit()", labels[0])])
    assert bool(_run(tmp_path / "app.py", source, config)) is expected


def test_scope_sanitizer_isolated_by_label(tmp_path):
    source = (
        "def process():\n"
        "    x = source_a()\n"
        "    y = source_b()\n"
        "    scope_kill()\n"
        "    sink(x)\n"
        "    sink(y)\n"
    )
    config = TraceConfig(
        labels=["a", "b"],
        sources=[TraceSource("source_a()", "a"), TraceSource("source_b()", "b")],
        sinks=[TraceSink("sink($X)", label, f"{label} reached") for label in ("a", "b")],
        scope_sanitizers=[TraceScopeSanitizer("scope_kill()", "a")],
    )
    violations = _run(tmp_path / "app.py", source, config)
    assert {violation.label for violation in violations} == {"b"}


def test_scope_sanitizer_yaml_loading(tmp_path):
    config_path = tmp_path / "patterns.yaml"
    config_path.write_text(yaml.safe_dump({
        "trace": {
            "labels": ["lbl"],
            "sources": [{"pattern": "tainted()", "label": "lbl"}],
            "sinks": [{"pattern": "use($X)", "label": "lbl", "message": "used"}],
            "scope_sanitizers": [{"pattern": "commit()", "label": "lbl"}],
        },
    }))
    config = load_trace_config(str(config_path))
    assert [(item.pattern, item.label) for item in config.scope_sanitizers] == [("commit()", "lbl")]


def test_merge_configs_preserves_scope_sanitizers():
    from emend.trace_presets import merge_configs

    first = TraceConfig(
        labels=["lbl"], sources=[TraceSource("src()", "lbl")],
        scope_sanitizers=[TraceScopeSanitizer("a()", "lbl")],
    )
    second = TraceConfig(
        sinks=[TraceSink("sink($X)", "lbl", "used")],
        scope_sanitizers=[TraceScopeSanitizer("b()", "lbl")],
    )
    merged = merge_configs(first, second)
    assert [item.pattern for item in merged.scope_sanitizers] == ["a()", "b()"]
    assert len(merged.sources) == len(merged.sinks) == 1
