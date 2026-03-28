"""Framework-specific taint analysis presets.

Provides predefined TaintConfig instances for popular Python frameworks,
covering common vulnerability patterns (SQL injection, XSS, command injection,
path traversal, SSRF).
"""

from __future__ import annotations

from emend.taint import TaintConfig, TaintSanitizer, TaintSink, TaintSource


def _flask_preset() -> TaintConfig:
    """Return the Flask taint preset."""
    labels = ["user_input", "file_path", "html_output"]

    sources = [
        TaintSource(pattern="request.args.get($X)", label="user_input"),
        TaintSource(pattern="request.args[$X]", label="user_input"),
        TaintSource(pattern="request.form.get($X)", label="user_input"),
        TaintSource(pattern="request.form[$X]", label="user_input"),
        TaintSource(pattern="request.json.get($X)", label="user_input"),
        TaintSource(pattern="request.data", label="user_input"),
        TaintSource(pattern="request.cookies.get($X)", label="user_input"),
        TaintSource(pattern="request.headers.get($X)", label="user_input"),
        TaintSource(pattern="request.files[$X]", label="file_path"),
    ]

    sinks = [
        TaintSink(
            pattern="cursor.execute($X)",
            label="user_input",
            message="SQL injection: user input in cursor.execute()",
        ),
        TaintSink(
            pattern="db.engine.execute($X)",
            label="user_input",
            message="SQL injection: user input in engine.execute()",
        ),
        TaintSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection: user input in eval()",
        ),
        TaintSink(
            pattern="exec($X)",
            label="user_input",
            message="Code injection: user input in exec()",
        ),
        TaintSink(
            pattern="os.system($X)",
            label="user_input",
            message="Command injection: user input in os.system()",
        ),
        TaintSink(
            pattern="subprocess.call($X)",
            label="user_input",
            message="Command injection: user input in subprocess.call()",
        ),
        TaintSink(
            pattern="subprocess.run($X)",
            label="user_input",
            message="Command injection: user input in subprocess.run()",
        ),
        TaintSink(
            pattern="open($X)",
            label="file_path",
            message="Path traversal: file path in open()",
        ),
        TaintSink(
            pattern="send_file($X)",
            label="file_path",
            message="Path traversal: file path in send_file()",
        ),
        TaintSink(
            pattern="Markup($X)",
            label="html_output",
            message="XSS: unescaped user input in Markup()",
        ),
    ]

    sanitizers = [
        TaintSanitizer(pattern="escape($X)", label="user_input"),
        TaintSanitizer(pattern="bleach.clean($X)", label="html_output"),
        TaintSanitizer(pattern="secure_filename($X)", label="file_path"),
        TaintSanitizer(pattern="int($X)", label="user_input"),
        TaintSanitizer(pattern="float($X)", label="user_input"),
    ]

    return TaintConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _django_preset() -> TaintConfig:
    """Return the Django taint preset."""
    labels = ["user_input", "file_path", "html_output"]

    sources = [
        TaintSource(pattern="request.GET.get($X)", label="user_input"),
        TaintSource(pattern="request.GET[$X]", label="user_input"),
        TaintSource(pattern="request.POST.get($X)", label="user_input"),
        TaintSource(pattern="request.POST[$X]", label="user_input"),
        TaintSource(pattern="request.body", label="user_input"),
        TaintSource(pattern="request.META.get($X)", label="user_input"),
        TaintSource(pattern="request.COOKIES.get($X)", label="user_input"),
        TaintSource(pattern="request.FILES.get($X)", label="file_path"),
        TaintSource(pattern="request.FILES[$X]", label="file_path"),
    ]

    sinks = [
        TaintSink(
            pattern="cursor.execute($X)",
            label="user_input",
            message="SQL injection: user input in raw SQL",
        ),
        TaintSink(
            pattern="RawSQL($X)",
            label="user_input",
            message="SQL injection: user input in RawSQL()",
        ),
        TaintSink(
            pattern="extra(where=[$X])",
            label="user_input",
            message="SQL injection: user input in extra(where=)",
        ),
        TaintSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection",
        ),
        TaintSink(
            pattern="exec($X)",
            label="user_input",
            message="Code injection",
        ),
        TaintSink(
            pattern="os.system($X)",
            label="user_input",
            message="Command injection",
        ),
        TaintSink(
            pattern="subprocess.call($X)",
            label="user_input",
            message="Command injection",
        ),
        TaintSink(
            pattern="mark_safe($X)",
            label="user_input",
            message="XSS: user input in mark_safe()",
        ),
        TaintSink(
            pattern="format_html($X)",
            label="user_input",
            message="XSS: check format_html() arguments",
        ),
    ]

    sanitizers = [
        TaintSanitizer(pattern="escape($X)", label="user_input"),
        TaintSanitizer(pattern="conditional_escape($X)", label="user_input"),
        TaintSanitizer(pattern="int($X)", label="user_input"),
        TaintSanitizer(pattern="float($X)", label="user_input"),
        TaintSanitizer(pattern="slugify($X)", label="user_input"),
    ]

    return TaintConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _sqlalchemy_preset() -> TaintConfig:
    """Return the SQLAlchemy taint preset.

    This preset has no sources — it is meant to be composed with a
    framework preset (e.g. Flask or Django) that provides user-input sources.
    """
    labels = ["user_input"]

    sources: list[TaintSource] = []

    sinks = [
        TaintSink(
            pattern="text($X)",
            label="user_input",
            message="SQL injection: user input in text()",
        ),
        TaintSink(
            pattern="TextClause($X)",
            label="user_input",
            message="SQL injection: user input in TextClause()",
        ),
        TaintSink(
            pattern="session.execute($X)",
            label="user_input",
            message="SQL injection: user input in session.execute()",
        ),
        TaintSink(
            pattern="engine.execute($X)",
            label="user_input",
            message="SQL injection: user input in engine.execute()",
        ),
        TaintSink(
            pattern="connection.execute($X)",
            label="user_input",
            message="SQL injection: user input in connection.execute()",
        ),
    ]

    sanitizers = [
        TaintSanitizer(pattern="bindparam($X)", label="user_input"),
    ]

    return TaintConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _fastapi_preset() -> TaintConfig:
    """Return the FastAPI taint preset.

    FastAPI's dependency injection system means sources are function parameters
    annotated with Query/Path/Body etc. This preset covers common sinks and
    sanitizers. For best results, use with interprocedural analysis.
    """
    labels = ["user_input", "file_path"]

    # FastAPI sources: common parameter patterns accessible as variables
    # from dependency-injected parameters
    sources = [
        TaintSource(pattern="Query(default=$X)", label="user_input"),
        TaintSource(pattern="Body(default=$X)", label="user_input"),
        TaintSource(pattern="Form(default=$X)", label="user_input"),
        TaintSource(pattern="Header(default=$X)", label="user_input"),
        TaintSource(pattern="Cookie(default=$X)", label="user_input"),
        TaintSource(pattern="File(default=$X)", label="file_path"),
    ]

    sinks = [
        TaintSink(
            pattern="cursor.execute($X)",
            label="user_input",
            message="SQL injection: user input in cursor.execute()",
        ),
        TaintSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection: user input in eval()",
        ),
        TaintSink(
            pattern="exec($X)",
            label="user_input",
            message="Code injection: user input in exec()",
        ),
        TaintSink(
            pattern="os.system($X)",
            label="user_input",
            message="Command injection: user input in os.system()",
        ),
        TaintSink(
            pattern="subprocess.call($X)",
            label="user_input",
            message="Command injection: user input in subprocess.call()",
        ),
        TaintSink(
            pattern="subprocess.run($X)",
            label="user_input",
            message="Command injection: user input in subprocess.run()",
        ),
        TaintSink(
            pattern="open($X)",
            label="file_path",
            message="Path traversal: file path in open()",
        ),
        TaintSink(
            pattern="send_file($X)",
            label="file_path",
            message="Path traversal: file path in send_file()",
        ),
    ]

    sanitizers = [
        TaintSanitizer(pattern="escape($X)", label="user_input"),
        TaintSanitizer(pattern="bleach.clean($X)", label="user_input"),
        TaintSanitizer(pattern="secure_filename($X)", label="file_path"),
        TaintSanitizer(pattern="int($X)", label="user_input"),
        TaintSanitizer(pattern="float($X)", label="user_input"),
    ]

    return TaintConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


# Registry of available presets
_PRESETS: dict[str, object] = {
    "flask": _flask_preset,
    "django": _django_preset,
    "sqlalchemy": _sqlalchemy_preset,
    "fastapi": _fastapi_preset,
}


def list_presets() -> list[str]:
    """Return available preset names."""
    return ["django", "flask", "sqlalchemy", "fastapi", "all"]


def get_preset(name: str) -> TaintConfig:
    """Return a predefined TaintConfig for the named framework.

    Args:
        name: Preset name ("django", "flask", "sqlalchemy", "fastapi", "all").

    Returns:
        A TaintConfig with framework-specific rules.

    Raises:
        ValueError: If the preset name is unknown.
    """
    if name == "all":
        return merge_configs(
            _flask_preset(),
            _django_preset(),
            _sqlalchemy_preset(),
            _fastapi_preset(),
        )
    factory = _PRESETS.get(name)
    if factory is None:
        known = ", ".join(sorted(_PRESETS.keys()) + ["all"])
        raise ValueError(f"Unknown preset: {name!r}. Available presets: {known}")
    return factory()  # type: ignore[operator]


def merge_configs(*configs: TaintConfig) -> TaintConfig:
    """Merge multiple TaintConfigs, deduplicating labels.

    Sources, sinks, and sanitizers are concatenated; labels are deduplicated
    while preserving first-seen order.
    """
    seen_labels: set[str] = set()
    labels: list[str] = []
    sources: list[TaintSource] = []
    sinks: list[TaintSink] = []
    sanitizers: list[TaintSanitizer] = []

    for cfg in configs:
        for lbl in cfg.labels:
            if lbl not in seen_labels:
                seen_labels.add(lbl)
                labels.append(lbl)
        sources.extend(cfg.sources)
        sinks.extend(cfg.sinks)
        sanitizers.extend(cfg.sanitizers)

    return TaintConfig(
        labels=labels,
        sources=sources,
        sinks=sinks,
        sanitizers=sanitizers,
    )
