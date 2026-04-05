"""Framework-specific trace analysis presets.

Provides predefined TraceConfig instances for popular Python frameworks,
covering common vulnerability patterns (SQL injection, XSS, command injection,
path traversal, SSRF).
"""

from __future__ import annotations

from typing import Callable

from emend.trace import TraceConfig, TraceSanitizer, TraceScopeSanitizer, TraceSink, TraceSource


def _flask_preset() -> TraceConfig:
    """Return the Flask trace preset."""
    labels = ["user_input", "file_path", "html_output"]

    sources = [
        TraceSource(pattern="request.args.get($X)", label="user_input"),
        TraceSource(pattern="request.args[$X]", label="user_input"),
        TraceSource(pattern="request.form.get($X)", label="user_input"),
        TraceSource(pattern="request.form[$X]", label="user_input"),
        TraceSource(pattern="request.json.get($X)", label="user_input"),
        TraceSource(pattern="request.data", label="user_input"),
        TraceSource(pattern="request.cookies.get($X)", label="user_input"),
        TraceSource(pattern="request.headers.get($X)", label="user_input"),
        TraceSource(pattern="request.files[$X]", label="file_path"),
    ]

    sinks = [
        TraceSink(
            pattern="cursor.execute($X)",
            label="user_input",
            message="SQL injection: user input in cursor.execute()",
        ),
        TraceSink(
            pattern="db.engine.execute($X)",
            label="user_input",
            message="SQL injection: user input in engine.execute()",
        ),
        TraceSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection: user input in eval()",
        ),
        TraceSink(
            pattern="exec($X)",
            label="user_input",
            message="Code injection: user input in exec()",
        ),
        TraceSink(
            pattern="os.system($X)",
            label="user_input",
            message="Command injection: user input in os.system()",
        ),
        TraceSink(
            pattern="subprocess.call($X)",
            label="user_input",
            message="Command injection: user input in subprocess.call()",
        ),
        TraceSink(
            pattern="subprocess.run($X)",
            label="user_input",
            message="Command injection: user input in subprocess.run()",
        ),
        TraceSink(
            pattern="open($X)",
            label="file_path",
            message="Path traversal: file path in open()",
        ),
        TraceSink(
            pattern="send_file($X)",
            label="file_path",
            message="Path traversal: file path in send_file()",
        ),
        TraceSink(
            pattern="Markup($X)",
            label="html_output",
            message="XSS: unescaped user input in Markup()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="escape($X)", label="user_input"),
        TraceSanitizer(pattern="bleach.clean($X)", label="html_output"),
        TraceSanitizer(pattern="secure_filename($X)", label="file_path"),
        TraceSanitizer(pattern="int($X)", label="user_input"),
        TraceSanitizer(pattern="float($X)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _django_preset() -> TraceConfig:
    """Return the Django trace preset."""
    labels = ["user_input", "file_path", "html_output"]

    sources = [
        TraceSource(pattern="request.GET.get($X)", label="user_input"),
        TraceSource(pattern="request.GET[$X]", label="user_input"),
        TraceSource(pattern="request.POST.get($X)", label="user_input"),
        TraceSource(pattern="request.POST[$X]", label="user_input"),
        TraceSource(pattern="request.body", label="user_input"),
        TraceSource(pattern="request.META.get($X)", label="user_input"),
        TraceSource(pattern="request.COOKIES.get($X)", label="user_input"),
        TraceSource(pattern="request.FILES.get($X)", label="file_path"),
        TraceSource(pattern="request.FILES[$X]", label="file_path"),
    ]

    sinks = [
        TraceSink(
            pattern="cursor.execute($X)",
            label="user_input",
            message="SQL injection: user input in raw SQL",
        ),
        TraceSink(
            pattern="RawSQL($X)",
            label="user_input",
            message="SQL injection: user input in RawSQL()",
        ),
        TraceSink(
            pattern="extra(where=[$X])",
            label="user_input",
            message="SQL injection: user input in extra(where=)",
        ),
        TraceSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection",
        ),
        TraceSink(
            pattern="exec($X)",
            label="user_input",
            message="Code injection",
        ),
        TraceSink(
            pattern="os.system($X)",
            label="user_input",
            message="Command injection",
        ),
        TraceSink(
            pattern="subprocess.call($X)",
            label="user_input",
            message="Command injection",
        ),
        TraceSink(
            pattern="mark_safe($X)",
            label="user_input",
            message="XSS: user input in mark_safe()",
        ),
        TraceSink(
            pattern="format_html($X)",
            label="user_input",
            message="XSS: check format_html() arguments",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="escape($X)", label="user_input"),
        TraceSanitizer(pattern="conditional_escape($X)", label="user_input"),
        TraceSanitizer(pattern="int($X)", label="user_input"),
        TraceSanitizer(pattern="float($X)", label="user_input"),
        TraceSanitizer(pattern="slugify($X)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _sqlalchemy_preset() -> TraceConfig:
    """Return the SQLAlchemy trace preset.

    This preset has no sources — it is meant to be composed with a
    framework preset (e.g. Flask or Django) that provides user-input sources.
    """
    labels = ["user_input"]

    sources: list[TraceSource] = []

    sinks = [
        TraceSink(
            pattern="text($X)",
            label="user_input",
            message="SQL injection: user input in text()",
        ),
        TraceSink(
            pattern="TextClause($X)",
            label="user_input",
            message="SQL injection: user input in TextClause()",
        ),
        TraceSink(
            pattern="session.execute($X)",
            label="user_input",
            message="SQL injection: user input in session.execute()",
        ),
        TraceSink(
            pattern="engine.execute($X)",
            label="user_input",
            message="SQL injection: user input in engine.execute()",
        ),
        TraceSink(
            pattern="connection.execute($X)",
            label="user_input",
            message="SQL injection: user input in connection.execute()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="bindparam($X)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _fastapi_preset() -> TraceConfig:
    """Return the FastAPI trace preset.

    FastAPI's dependency injection system means sources are function parameters
    annotated with Query/Path/Body etc. This preset covers common sinks and
    sanitizers. For best results, use with interprocedural analysis.
    """
    labels = ["user_input", "file_path"]

    # FastAPI sources: common parameter patterns accessible as variables
    # from dependency-injected parameters
    sources = [
        TraceSource(pattern="Query(default=$X)", label="user_input"),
        TraceSource(pattern="Body(default=$X)", label="user_input"),
        TraceSource(pattern="Form(default=$X)", label="user_input"),
        TraceSource(pattern="Header(default=$X)", label="user_input"),
        TraceSource(pattern="Cookie(default=$X)", label="user_input"),
        TraceSource(pattern="File(default=$X)", label="file_path"),
    ]

    sinks = [
        TraceSink(
            pattern="cursor.execute($X)",
            label="user_input",
            message="SQL injection: user input in cursor.execute()",
        ),
        TraceSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection: user input in eval()",
        ),
        TraceSink(
            pattern="exec($X)",
            label="user_input",
            message="Code injection: user input in exec()",
        ),
        TraceSink(
            pattern="os.system($X)",
            label="user_input",
            message="Command injection: user input in os.system()",
        ),
        TraceSink(
            pattern="subprocess.call($X)",
            label="user_input",
            message="Command injection: user input in subprocess.call()",
        ),
        TraceSink(
            pattern="subprocess.run($X)",
            label="user_input",
            message="Command injection: user input in subprocess.run()",
        ),
        TraceSink(
            pattern="open($X)",
            label="file_path",
            message="Path traversal: file path in open()",
        ),
        TraceSink(
            pattern="send_file($X)",
            label="file_path",
            message="Path traversal: file path in send_file()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="escape($X)", label="user_input"),
        TraceSanitizer(pattern="bleach.clean($X)", label="user_input"),
        TraceSanitizer(pattern="secure_filename($X)", label="file_path"),
        TraceSanitizer(pattern="int($X)", label="user_input"),
        TraceSanitizer(pattern="float($X)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


# Registry of available presets
_PRESETS: dict[str, Callable[[], TraceConfig]] = {
    "flask": _flask_preset,
    "django": _django_preset,
    "sqlalchemy": _sqlalchemy_preset,
    "fastapi": _fastapi_preset,
}


def list_presets() -> list[str]:
    """Return available preset names."""
    return ["django", "flask", "sqlalchemy", "fastapi", "all"]


def get_preset(name: str) -> TraceConfig:
    """Return a predefined TraceConfig for the named framework.

    Args:
        name: Preset name ("django", "flask", "sqlalchemy", "fastapi", "all").

    Returns:
        A TraceConfig with framework-specific rules.

    Raises:
        ValueError: If the preset name is unknown.
    """
    if name == "all":
        return merge_configs(*[factory() for factory in _PRESETS.values()])
    factory = _PRESETS.get(name)
    if factory is None:
        known = ", ".join(sorted(_PRESETS.keys()) + ["all"])
        raise ValueError(f"Unknown preset: {name!r}. Available presets: {known}")
    return factory()


def merge_configs(*configs: TraceConfig) -> TraceConfig:
    """Merge multiple TraceConfigs, deduplicating labels.

    Sources, sinks, and sanitizers are concatenated;
    labels are deduplicated while preserving first-seen order.
    """
    seen_labels: set[str] = set()
    labels: list[str] = []
    sources: list[TraceSource] = []
    sinks: list[TraceSink] = []
    sanitizers: list[TraceSanitizer] = []
    scope_sanitizers: list[TraceScopeSanitizer] = []
    exclude_paths: list[str] = []

    for cfg in configs:
        for lbl in cfg.labels:
            if lbl not in seen_labels:
                seen_labels.add(lbl)
                labels.append(lbl)
        sources.extend(cfg.sources)
        sinks.extend(cfg.sinks)
        sanitizers.extend(cfg.sanitizers)
        scope_sanitizers.extend(cfg.scope_sanitizers)
        exclude_paths.extend(cfg.exclude_paths)

    return TraceConfig(
        labels=labels,
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
        scope_sanitizers=scope_sanitizers,
        exclude_paths=exclude_paths,
    )
