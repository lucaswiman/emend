"""Shared runner for language-specific trace contract data."""

from emend.trace import TraceConfig, TraceSanitizer, TraceSink, TraceSource


def run_trace_contract(
    tmp_path, runner, language, suffix, source, source_patterns, sink_pattern,
    message, sanitizer_patterns,
):
    path = tmp_path / f"handler.{suffix}"
    path.write_text(source)
    config = TraceConfig(
        labels=["user_input"],
        sources=[TraceSource(pattern=p, label="user_input") for p in source_patterns],
        sinks=[TraceSink(pattern=sink_pattern, label="user_input", message=message)],
        sanitizers=[
            TraceSanitizer(pattern=p, label="user_input")
            for p in sanitizer_patterns
        ],
    )
    return path, runner([str(path)], config, language=language)
