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


def _express_preset() -> TraceConfig:
    """Return the Express.js (Node.js/TypeScript) trace preset."""
    labels = ["user_input", "html_output"]

    sources = [
        # Bracket notation (standard TypeScript/JavaScript access pattern)
        TraceSource(pattern="req.params[$X]", label="user_input"),
        TraceSource(pattern="req.query[$X]", label="user_input"),
        TraceSource(pattern="req.body[$X]", label="user_input"),
        TraceSource(pattern="req.headers[$X]", label="user_input"),
        TraceSource(pattern="req.cookies[$X]", label="user_input"),
        # Whole-object access
        TraceSource(pattern="req.body", label="user_input"),
        # Method-based access (e.g. via get() helpers)
        TraceSource(pattern="req.query.get($X)", label="user_input"),
        TraceSource(pattern="req.params.get($X)", label="user_input"),
        TraceSource(pattern="req.headers.get($X)", label="user_input"),
    ]

    sinks = [
        # XSS sinks — labeled user_input so sources flow directly to them
        TraceSink(
            pattern="res.send($X)",
            label="user_input",
            message="XSS: user input in res.send()",
        ),
        TraceSink(
            pattern="res.write($X)",
            label="user_input",
            message="XSS: user input in res.write()",
        ),
        TraceSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection: user input in eval()",
        ),
        # Assignment form captures the RHS value (the tainted variable)
        TraceSink(
            pattern="$X.innerHTML = $Y",
            label="user_input",
            message="XSS: user input assigned to innerHTML",
        ),
        TraceSink(
            pattern="document.write($X)",
            label="user_input",
            message="XSS: user input in document.write()",
        ),
        TraceSink(
            pattern="child_process.exec($X)",
            label="user_input",
            message="Command injection: user input in child_process.exec()",
        ),
        TraceSink(
            pattern="exec($X)",
            label="user_input",
            message="Command injection: user input in exec()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="escape($X)", label="user_input"),
        TraceSanitizer(pattern="sanitize($X)", label="user_input"),
        TraceSanitizer(pattern="DOMPurify.sanitize($X)", label="user_input"),
        TraceSanitizer(pattern="validator.escape($X)", label="user_input"),
        TraceSanitizer(pattern="encodeURIComponent($X)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _react_preset() -> TraceConfig:
    """Return the React (TypeScript/JavaScript) trace preset."""
    labels = ["user_input"]

    sources = [
        TraceSource(pattern="useSearchParams()", label="user_input"),
        # Specific window.location property patterns (dot-notation works for property access)
        TraceSource(pattern="window.location.hash", label="user_input"),
        TraceSource(pattern="window.location.search", label="user_input"),
        TraceSource(pattern="window.location.href", label="user_input"),
        TraceSource(pattern="window.location.pathname", label="user_input"),
        TraceSource(pattern="window.location", label="user_input"),
        TraceSource(pattern="document.cookie", label="user_input"),
        TraceSource(pattern="localStorage.getItem($X)", label="user_input"),
        TraceSource(pattern="sessionStorage.getItem($X)", label="user_input"),
    ]

    sinks = [
        # All XSS sinks labeled user_input so sources flow directly to them
        TraceSink(
            pattern="dangerouslySetInnerHTML",
            label="user_input",
            message="XSS: user input in dangerouslySetInnerHTML",
        ),
        TraceSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection: user input in eval()",
        ),
        TraceSink(
            pattern="document.write($X)",
            label="user_input",
            message="XSS: user input in document.write()",
        ),
        # Assignment form captures the RHS value (the tainted variable)
        TraceSink(
            pattern="$X.innerHTML = $Y",
            label="user_input",
            message="XSS: user input assigned to innerHTML",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="DOMPurify.sanitize($X)", label="user_input"),
        TraceSanitizer(pattern="encodeURIComponent($X)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _nextjs_preset() -> TraceConfig:
    """Return the Next.js trace preset."""
    labels = ["user_input"]

    sources = [
        # Bracket notation for dynamic key access (standard JS/TS pattern)
        TraceSource(pattern="params[$X]", label="user_input"),
        TraceSource(pattern="searchParams[$X]", label="user_input"),
        TraceSource(pattern="searchParams.get($X)", label="user_input"),
        TraceSource(pattern="cookies()[$X]", label="user_input"),
        TraceSource(pattern="cookies().get($X)", label="user_input"),
        TraceSource(pattern="headers()[$X]", label="user_input"),
        TraceSource(pattern="headers().get($X)", label="user_input"),
        # Pages Router (req object)
        TraceSource(pattern="req.query[$X]", label="user_input"),
        TraceSource(pattern="req.body[$X]", label="user_input"),
        TraceSource(pattern="req.body", label="user_input"),
    ]

    sinks = [
        # All sinks labeled user_input so sources flow directly to them
        TraceSink(
            pattern="dangerouslySetInnerHTML",
            label="user_input",
            message="XSS: user input in dangerouslySetInnerHTML",
        ),
        TraceSink(
            pattern="redirect($X)",
            label="user_input",
            message="Open redirect: user input in redirect()",
        ),
        TraceSink(
            pattern="eval($X)",
            label="user_input",
            message="Code injection: user input in eval()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="encodeURIComponent($X)", label="user_input"),
        TraceSanitizer(pattern="DOMPurify.sanitize($X)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _node_sql_preset() -> TraceConfig:
    """Return the Node.js SQL (label-based) trace preset.

    This preset has no sources — it is meant to be composed with a
    framework preset (e.g. express) that provides user-input sources.
    """
    labels = ["user_input"]

    sources: list[TraceSource] = []

    sinks = [
        TraceSink(
            pattern="pool.query($X)",
            label="user_input",
            message="SQL injection: user input in pool.query()",
        ),
        TraceSink(
            pattern="connection.query($X)",
            label="user_input",
            message="SQL injection: user input in connection.query()",
        ),
        TraceSink(
            pattern="knex.raw($X)",
            label="user_input",
            message="SQL injection: user input in knex.raw()",
        ),
        TraceSink(
            pattern="sequelize.query($X)",
            label="user_input",
            message="SQL injection: user input in sequelize.query()",
        ),
        TraceSink(
            pattern="db.query($X)",
            label="user_input",
            message="SQL injection: user input in db.query()",
        ),
        # Note: prisma.$queryRaw cannot be expressed as an emend pattern because
        # the $ in $queryRaw is a literal part of the Prisma API method name,
        # not a metavar.  Use prisma.$queryRawUnsafe in code reviews instead.
    ]

    sanitizers = [
        TraceSanitizer(pattern="pool.query($X, $PARAMS)", label="user_input"),
        TraceSanitizer(pattern="connection.query($X, $PARAMS)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _actix_web_preset() -> TraceConfig:
    """Return the Actix-web (Rust) trace preset.

    Note: Rust path-qualified calls (e.g. ``web::Query``, ``Command::arg``)
    cannot be expressed as emend patterns because the tree-sitter Rust grammar
    treats the full ``A::B`` path as a single opaque identifier.  The patterns
    below use method-call style (``$X.into_inner()``) and simple function names
    that ARE matchable by the pattern engine.
    """
    labels = ["user_input", "file_path"]

    sources = [
        # Actix extractors expose user data via .into_inner()
        TraceSource(pattern="$X.into_inner()", label="user_input"),
        # Cookie / header access via method calls
        TraceSource(pattern="req.cookie($X)", label="user_input"),
        TraceSource(pattern="$X.get($Y)", label="user_input"),
    ]

    sinks = [
        TraceSink(
            pattern="execute_query($X)",
            label="user_input",
            message="SQL injection: user input in execute_query()",
        ),
        TraceSink(
            pattern="execute($X)",
            label="user_input",
            message="SQL injection: user input in execute()",
        ),
        TraceSink(
            pattern="$X.arg($Y)",
            label="user_input",
            message="Command injection: user input passed to .arg()",
        ),
        TraceSink(
            pattern="$X.write($Y)",
            label="file_path",
            message="Path traversal: file path in .write()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="$X.parse::<i32>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<u32>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<i64>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<u64>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<f64>()", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _axum_preset() -> TraceConfig:
    """Return the Axum (Rust) trace preset.

    Note: Rust path-qualified calls (e.g. ``Query()``, ``Command::arg()``)
    cannot be expressed as emend patterns because the tree-sitter Rust grammar
    treats the full ``A::B`` path as a single opaque identifier.  The patterns
    below use method-call style and simple function names that ARE matchable
    by the pattern engine.
    """
    labels = ["user_input", "file_path"]

    sources = [
        # Axum extractors: tuple-destructured extractors yield inner value directly
        # via .0 field access or method calls on the extracted type.
        TraceSource(pattern="$X.into_inner()", label="user_input"),
        TraceSource(pattern="headers.get($X)", label="user_input"),
        TraceSource(pattern="$X.get($Y)", label="user_input"),
    ]

    sinks = [
        TraceSink(
            pattern="execute_query($X)",
            label="user_input",
            message="SQL injection: user input in execute_query()",
        ),
        TraceSink(
            pattern="execute($X)",
            label="user_input",
            message="SQL injection: user input in execute()",
        ),
        TraceSink(
            pattern="$X.arg($Y)",
            label="user_input",
            message="Command injection: user input passed to .arg()",
        ),
        TraceSink(
            pattern="$X.write($Y)",
            label="file_path",
            message="Path traversal: file path in .write()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="$X.parse::<i32>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<u32>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<i64>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<u64>()", label="user_input"),
        TraceSanitizer(pattern="$X.parse::<f64>()", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _sqlx_preset() -> TraceConfig:
    """Return the sqlx (Rust) trace preset.

    This preset has no sources — it is meant to be composed with a
    framework preset (e.g. actix-web or axum) that provides user-input sources.

    Note: Rust path-qualified calls like ``sqlx::query()`` cannot be expressed
    as emend patterns.  The patterns below use method-call style that IS
    matchable.  Use ``$X.execute($Y)`` to catch raw SQL execution via any pool
    or connection object.
    """
    labels = ["user_input"]

    sources: list[TraceSource] = []

    sinks = [
        # Method-call sinks that work with the tree-sitter Rust pattern engine
        TraceSink(
            pattern="$X.execute($Y)",
            label="user_input",
            message="SQL injection: user input in .execute()",
        ),
        TraceSink(
            pattern="$X.query($Y)",
            label="user_input",
            message="SQL injection: user input in .query()",
        ),
        TraceSink(
            pattern="$X.fetch_all($Y)",
            label="user_input",
            message="SQL injection: user input in .fetch_all()",
        ),
        TraceSink(
            pattern="$X.fetch_one($Y)",
            label="user_input",
            message="SQL injection: user input in .fetch_one()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="$X.bind($Y)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


def _diesel_preset() -> TraceConfig:
    """Return the Diesel (Rust) trace preset.

    This preset has no sources — it is meant to be composed with a
    framework preset (e.g. actix-web or axum) that provides user-input sources.

    Note: Rust path-qualified calls like ``diesel::sql_query()`` cannot be
    expressed as emend patterns.  The patterns below use method-call style
    that IS matchable.  Use ``$X.execute($Y)`` to catch raw SQL execution.
    """
    labels = ["user_input"]

    sources: list[TraceSource] = []

    sinks = [
        # Method-call sinks that work with the tree-sitter Rust pattern engine
        TraceSink(
            pattern="$X.execute($Y)",
            label="user_input",
            message="SQL injection: user input in .execute()",
        ),
        TraceSink(
            pattern="$X.load($Y)",
            label="user_input",
            message="SQL injection: user input in .load()",
        ),
        TraceSink(
            pattern="$X.get_result($Y)",
            label="user_input",
            message="SQL injection: user input in .get_result()",
        ),
    ]

    sanitizers = [
        TraceSanitizer(pattern="$X.filter($Y)", label="user_input"),
        TraceSanitizer(pattern="$X.select($Y)", label="user_input"),
        TraceSanitizer(pattern="$X.bind_sql($Y)", label="user_input"),
    ]

    return TraceConfig(labels=labels, sources=sources, sinks=sinks, sanitizers=sanitizers)


# Registry of available presets
_PRESETS: dict[str, Callable[[], TraceConfig]] = {
    "flask": _flask_preset,
    "django": _django_preset,
    "sqlalchemy": _sqlalchemy_preset,
    "fastapi": _fastapi_preset,
    "express": _express_preset,
    "react": _react_preset,
    "nextjs": _nextjs_preset,
    "node-sql": _node_sql_preset,
    "actix-web": _actix_web_preset,
    "axum": _axum_preset,
    "sqlx": _sqlx_preset,
    "diesel": _diesel_preset,
}


def list_presets() -> list[str]:
    """Return available preset names."""
    return [
        "django",
        "flask",
        "sqlalchemy",
        "fastapi",
        "express",
        "react",
        "nextjs",
        "node-sql",
        "actix-web",
        "axum",
        "sqlx",
        "diesel",
        "all",
    ]


def get_preset(name: str) -> TraceConfig:
    """Return a predefined TraceConfig for the named framework.

    Args:
        name: Preset name. Python: "django", "flask", "sqlalchemy", "fastapi".
              TypeScript/Node.js: "express", "react", "nextjs", "node-sql".
              Rust: "actix-web", "axum", "sqlx", "diesel".
              Special: "all" merges all presets.

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
