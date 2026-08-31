"""Tests for framework-specific taint presets."""

import pytest

from emend.trace import TraceConfig, run_trace_analysis
from emend.trace_presets import get_preset, list_presets, merge_configs


class TestPresetLoading:
    def test_list_presets(self):
        presets = list_presets()
        assert "flask" in presets
        assert "django" in presets
        assert "sqlalchemy" in presets
        assert "fastapi" in presets
        assert "all" in presets

    def test_get_flask_preset(self):
        config = get_preset("flask")
        assert "user_input" in config.labels
        assert len(config.sources) >= 5
        assert len(config.sinks) >= 5
        assert len(config.sanitizers) >= 3

    def test_get_django_preset(self):
        config = get_preset("django")
        assert "user_input" in config.labels
        assert any("request.GET" in s.pattern for s in config.sources)
        assert any("mark_safe" in s.pattern for s in config.sinks)

    def test_get_sqlalchemy_preset(self):
        config = get_preset("sqlalchemy")
        assert any("text($X)" in s.pattern for s in config.sinks)
        assert any("session.execute" in s.pattern for s in config.sinks)

    def test_get_unknown_preset_raises(self):
        with pytest.raises(ValueError, match="Unknown preset"):
            get_preset("nonexistent")

    def test_merge_configs(self):
        c1 = TraceConfig(labels=["a"], sources=[], sinks=[], sanitizers=[])
        c2 = TraceConfig(labels=["b", "a"], sources=[], sinks=[], sanitizers=[])
        merged = merge_configs(c1, c2)
        assert sorted(merged.labels) == ["a", "b"]

    def test_get_all_preset(self):
        config = get_preset("all")
        # "all" should include sources from multiple frameworks
        patterns = [s.pattern for s in config.sources]
        assert any("request.args" in p for p in patterns)  # Flask
        assert any("request.GET" in p for p in patterns)  # Django

    def test_get_fastapi_preset(self):
        config = get_preset("fastapi")
        assert "user_input" in config.labels
        assert len(config.sinks) >= 5
        assert len(config.sanitizers) >= 3

    def test_merge_configs_sources_combined(self):
        flask = get_preset("flask")
        sqlalchemy = get_preset("sqlalchemy")
        merged = merge_configs(flask, sqlalchemy)
        # Flask sources + SQLAlchemy sinks
        flask_source_count = len(flask.sources)
        assert len(merged.sources) >= flask_source_count
        # Sinks should include both Flask and SQLAlchemy sinks
        sa_sink_patterns = {s.pattern for s in sqlalchemy.sinks}
        flask_sink_patterns = {s.pattern for s in flask.sinks}
        merged_patterns = {s.pattern for s in merged.sinks}
        assert sa_sink_patterns.issubset(merged_patterns)
        assert flask_sink_patterns.issubset(merged_patterns)

    def test_merge_configs_sanitizers_combined(self):
        c1 = TraceConfig(
            labels=["x"],
            sources=[],
            sinks=[],
            sanitizers=[],
        )
        c2 = TraceConfig(
            labels=["x"],
            sources=[],
            sinks=[],
            sanitizers=[],
        )
        merged = merge_configs(c1, c2)
        assert merged.labels == ["x"]

    def test_django_sources_count(self):
        config = get_preset("django")
        assert len(config.sources) >= 5

    def test_django_sanitizers_count(self):
        config = get_preset("django")
        assert len(config.sanitizers) >= 3

    def test_sqlalchemy_has_no_sources(self):
        config = get_preset("sqlalchemy")
        # SQLAlchemy preset has no sources (meant to be composed)
        assert len(config.sources) == 0

    def test_sqlalchemy_has_bindparam_sanitizer(self):
        config = get_preset("sqlalchemy")
        assert any("bindparam" in s.pattern for s in config.sanitizers)


class TestTypeScriptPresetLoading:
    def test_list_presets_includes_typescript(self):
        presets = list_presets()
        assert "express" in presets
        assert "react" in presets
        assert "nextjs" in presets
        assert "node-sql" in presets

    def test_get_express_preset(self):
        config = get_preset("express")
        assert "user_input" in config.labels
        assert len(config.sources) >= 3
        assert len(config.sinks) >= 3
        assert len(config.sanitizers) >= 2

    def test_express_sources_include_req_query(self):
        config = get_preset("express")
        source_patterns = [s.pattern for s in config.sources]
        assert any("req.query" in p for p in source_patterns)

    def test_express_sources_include_req_body(self):
        config = get_preset("express")
        source_patterns = [s.pattern for s in config.sources]
        assert any("req.body" in p for p in source_patterns)

    def test_express_sinks_include_eval(self):
        config = get_preset("express")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("eval" in p for p in sink_patterns)

    def test_express_sanitizers_include_escape(self):
        config = get_preset("express")
        sanitizer_patterns = [s.pattern for s in config.sanitizers]
        assert any("escape" in p for p in sanitizer_patterns)

    def test_get_react_preset(self):
        config = get_preset("react")
        assert "user_input" in config.labels
        assert len(config.sources) >= 2
        assert len(config.sinks) >= 2
        assert len(config.sanitizers) >= 1

    def test_react_sources_include_location(self):
        config = get_preset("react")
        source_patterns = [s.pattern for s in config.sources]
        assert any("location" in p or "searchParams" in p or "cookie" in p for p in source_patterns)

    def test_react_sinks_include_dangerously_set_inner_html(self):
        config = get_preset("react")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("dangerouslySetInnerHTML" in p or "innerHTML" in p or "eval" in p for p in sink_patterns)

    def test_react_sanitizers_include_dompurify(self):
        config = get_preset("react")
        sanitizer_patterns = [s.pattern for s in config.sanitizers]
        assert any("DOMPurify" in p or "sanitize" in p for p in sanitizer_patterns)

    def test_get_nextjs_preset(self):
        config = get_preset("nextjs")
        assert "user_input" in config.labels
        assert len(config.sources) >= 3
        assert len(config.sinks) >= 2
        assert len(config.sanitizers) >= 1

    def test_nextjs_sources_include_search_params(self):
        config = get_preset("nextjs")
        source_patterns = [s.pattern for s in config.sources]
        assert any("searchParams" in p or "params" in p or "cookies" in p for p in source_patterns)

    def test_nextjs_sinks_include_dangerous_html(self):
        config = get_preset("nextjs")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("dangerouslySetInnerHTML" in p or "sql" in p or "redirect" in p for p in sink_patterns)

    def test_nextjs_sanitizers_include_encode(self):
        config = get_preset("nextjs")
        sanitizer_patterns = [s.pattern for s in config.sanitizers]
        assert any("encodeURIComponent" in p or "escape" in p or "sanitize" in p for p in sanitizer_patterns)

    def test_get_node_sql_preset(self):
        config = get_preset("node-sql")
        assert len(config.sinks) >= 3
        assert len(config.sanitizers) >= 1

    def test_node_sql_sinks_include_pool_query(self):
        config = get_preset("node-sql")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("pool.query" in p or "connection.query" in p or "query" in p for p in sink_patterns)

    def test_node_sql_sinks_include_knex_raw(self):
        config = get_preset("node-sql")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("knex.raw" in p or "sequelize.query" in p or "queryRaw" in p or "raw" in p for p in sink_patterns)


class TestRustPresetLoading:
    def test_list_presets_includes_rust(self):
        presets = list_presets()
        assert "actix-web" in presets
        assert "axum" in presets
        assert "sqlx" in presets
        assert "diesel" in presets

    def test_get_actix_web_preset(self):
        config = get_preset("actix-web")
        assert "user_input" in config.labels
        assert len(config.sources) >= 3
        assert len(config.sinks) >= 3
        assert len(config.sanitizers) >= 1

    def test_actix_web_sources_include_into_inner(self):
        """actix-web sources use .into_inner() as extractor proxy (path-qualified
        calls like web::Query are not matchable by the emend pattern engine)."""
        config = get_preset("actix-web")
        source_patterns = [s.pattern for s in config.sources]
        assert any("into_inner" in p or "get" in p or "cookie" in p for p in source_patterns)

    def test_actix_web_sinks_include_execute(self):
        config = get_preset("actix-web")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("execute" in p or "arg" in p or "write" in p for p in sink_patterns)

    def test_get_axum_preset(self):
        config = get_preset("axum")
        assert "user_input" in config.labels
        assert len(config.sources) >= 3
        assert len(config.sinks) >= 2
        assert len(config.sanitizers) >= 1

    def test_axum_sources_include_extractor(self):
        """axum sources use .into_inner() as extractor proxy (path-qualified
        calls like Query() are not matchable by the emend pattern engine)."""
        config = get_preset("axum")
        source_patterns = [s.pattern for s in config.sources]
        assert any("into_inner" in p or "get" in p for p in source_patterns)

    def test_axum_sinks_include_execute(self):
        config = get_preset("axum")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("execute" in p or "arg" in p or "write" in p for p in sink_patterns)

    def test_get_sqlx_preset(self):
        config = get_preset("sqlx")
        assert len(config.sinks) >= 1
        assert len(config.sanitizers) >= 1

    def test_sqlx_sinks_include_execute_or_query(self):
        """sqlx sinks use method-call style (.execute, .query) since path-qualified
        calls like sqlx::query are not matchable by the emend pattern engine."""
        config = get_preset("sqlx")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("execute" in p or "query" in p or "fetch" in p for p in sink_patterns)

    def test_get_diesel_preset(self):
        config = get_preset("diesel")
        assert len(config.sinks) >= 1
        assert len(config.sanitizers) >= 1

    def test_diesel_sinks_include_execute(self):
        """diesel sinks use method-call style (.execute, .load) since path-qualified
        calls like diesel::sql_query are not matchable by the emend pattern engine."""
        config = get_preset("diesel")
        sink_patterns = [s.pattern for s in config.sinks]
        assert any("execute" in p or "load" in p or "sql_query" in p for p in sink_patterns)


class TestPresetIntegration:
    def test_flask_sql_injection(self, tmp_path):
        """Flask preset detects SQL injection."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    cursor.execute(name)\n"
        )
        config = get_preset("flask")
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_flask_markup_xss(self, tmp_path):
        """The Flask preset's Markup sink is reachable from request input."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def view(request):\n"
            "    name = request.args.get('name')\n"
            "    return Markup(name)\n"
        )
        violations = run_trace_analysis([str(test_file)], get_preset("flask"))
        assert any("Markup" in violation.message for violation in violations)

    def test_django_mark_safe_xss(self, tmp_path):
        """Django preset detects mark_safe XSS."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def view(request):\n"
            "    name = request.GET.get('name')\n"
            "    html = mark_safe(name)\n"
        )
        config = get_preset("django")
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_sqlalchemy_text_injection(self, tmp_path):
        """SQLAlchemy preset detects text() injection when composed with Flask."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def query(request, session):\n"
            "    name = request.args.get('name')\n"
            "    stmt = text(name)\n"
        )
        config = merge_configs(get_preset("flask"), get_preset("sqlalchemy"))
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_flask_sanitizer_blocks(self, tmp_path):
        """Flask sanitizer (escape) blocks taint propagation."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "def handle(request, cursor):\n"
            "    name = request.args.get('name')\n"
            "    name = escape(name)\n"
            "    cursor.execute(name)\n"
        )
        config = get_preset("flask")
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) == 0

    def test_flask_command_injection(self, tmp_path):
        """Flask preset detects command injection via os.system."""
        test_file = tmp_path / "app.py"
        test_file.write_text(
            "import os\n"
            "def run_cmd(request):\n"
            "    cmd = request.args.get('cmd')\n"
            "    os.system(cmd)\n"
        )
        config = get_preset("flask")
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_django_cursor_sql_injection(self, tmp_path):
        """Django preset detects raw SQL injection via cursor.execute."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def search(request, cursor):\n"
            "    q = request.POST.get('q')\n"
            "    cursor.execute(q)\n"
        )
        config = get_preset("django")
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) >= 1

    def test_no_violations_when_sanitized(self, tmp_path):
        """Django sanitizer (int conversion) blocks taint."""
        test_file = tmp_path / "views.py"
        test_file.write_text(
            "def view(request, cursor):\n"
            "    user_id = request.GET.get('id')\n"
            "    user_id = int(user_id)\n"
            "    cursor.execute(user_id)\n"
        )
        config = get_preset("django")
        violations = run_trace_analysis([str(test_file)], config)
        assert len(violations) == 0


class TestTypeScriptPresetIntegration:
    def test_express_code_injection_via_eval(self, tmp_path):
        """Express preset detects code injection from req.query into eval().

        TypeScript uses bracket notation (req.query["key"]) for property access.
        The preset sources include req.query[$X] to match this pattern.
        """
        test_file = tmp_path / "handler.ts"
        test_file.write_text(
            "function handler(req: any): void {\n"
            '    const code = req.query["code"];\n'
            "    eval(code);\n"
            "}\n"
        )
        config = get_preset("express")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) >= 1

    def test_express_xss_via_res_send(self, tmp_path):
        """Express preset detects XSS from req.body into res.send()."""
        test_file = tmp_path / "handler.ts"
        test_file.write_text(
            "function handler(req: any, res: any): void {\n"
            '    const msg = req.body["message"];\n'
            "    res.send(msg);\n"
            "}\n"
        )
        config = get_preset("express")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) >= 1

    def test_express_sanitizer_blocks(self, tmp_path):
        """Express escape() sanitizer blocks taint propagation."""
        test_file = tmp_path / "handler.ts"
        test_file.write_text(
            "function handler(req: any): void {\n"
            '    let code = req.query["code"];\n'
            "    code = escape(code);\n"
            "    eval(code);\n"
            "}\n"
        )
        config = get_preset("express")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) == 0

    def test_react_xss_via_inner_html(self, tmp_path):
        """React preset detects XSS when window.location.hash flows to innerHTML.

        The sink is ``$X.innerHTML = $Y`` (assignment form) to capture the RHS
        tainted variable rather than the LHS target element.
        """
        test_file = tmp_path / "component.ts"
        test_file.write_text(
            "function render(el: any): void {\n"
            "    const hash = window.location.hash;\n"
            "    el.innerHTML = hash;\n"
            "}\n"
        )
        config = get_preset("react")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) >= 1

    def test_react_xss_via_eval(self, tmp_path):
        """React preset detects code injection from localStorage into eval()."""
        test_file = tmp_path / "component.ts"
        test_file.write_text(
            'function run(): void {\n'
            '    const code = localStorage.getItem("script");\n'
            "    eval(code);\n"
            "}\n"
        )
        config = get_preset("react")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) >= 1

    def test_react_sanitizer_blocks_xss(self, tmp_path):
        """React DOMPurify sanitizer blocks taint from reaching innerHTML."""
        test_file = tmp_path / "component.ts"
        test_file.write_text(
            "function render(el: any): void {\n"
            "    const hash = window.location.hash;\n"
            "    const clean = DOMPurify.sanitize(hash);\n"
            "    el.innerHTML = clean;\n"
            "}\n"
        )
        config = get_preset("react")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) == 0

    def test_nextjs_open_redirect(self, tmp_path):
        """Next.js preset detects open redirect from searchParams into redirect()."""
        test_file = tmp_path / "page.ts"
        test_file.write_text(
            "function Page(searchParams: any): void {\n"
            '    const url = searchParams["redirect"];\n'
            "    redirect(url);\n"
            "}\n"
        )
        config = get_preset("nextjs")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) >= 1

    def test_nextjs_sanitizer_blocks(self, tmp_path):
        """Next.js encodeURIComponent sanitizer blocks taint propagation."""
        test_file = tmp_path / "page.ts"
        test_file.write_text(
            "function Page(searchParams: any): void {\n"
            '    const q = searchParams["q"];\n'
            "    const safe = encodeURIComponent(q);\n"
            "    redirect(safe);\n"
            "}\n"
        )
        config = get_preset("nextjs")
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) == 0

    def test_node_sql_injection_via_pool_query(self, tmp_path):
        """node-sql preset detects injection when composed with express."""
        test_file = tmp_path / "handler.ts"
        test_file.write_text(
            "function handler(req: any, pool: any): void {\n"
            '    const name = req.query["name"];\n'
            "    pool.query(name);\n"
            "}\n"
        )
        config = merge_configs(get_preset("express"), get_preset("node-sql"))
        violations = run_trace_analysis([str(test_file)], config, language="typescript")
        assert len(violations) >= 1

    def test_node_sql_no_sources_alone(self):
        """node-sql preset has no sources — must be composed with a framework preset."""
        config = get_preset("node-sql")
        assert len(config.sources) == 0


class TestRustPresetIntegration:
    def test_actix_web_sql_injection(self, tmp_path):
        """actix-web preset detects SQL injection from extractor via .into_inner().

        Note: Rust path-qualified calls (web::Query, Command::arg) cannot be
        expressed as emend patterns.  The preset uses ``$X.into_inner()`` as
        the source proxy and simple function-call sinks like ``execute_query()``.
        """
        test_file = tmp_path / "handler.rs"
        test_file.write_text(
            "fn handler(query: web::Query<Params>) {\n"
            "    let name = query.into_inner();\n"
            "    execute_query(name);\n"
            "}\n"
        )
        config = get_preset("actix-web")
        violations = run_trace_analysis([str(test_file)], config, language="rust")
        assert len(violations) >= 1

    def test_actix_web_json_body_injection(self, tmp_path):
        """actix-web preset detects injection from web::Json body extractor."""
        test_file = tmp_path / "handler.rs"
        test_file.write_text(
            "fn handler(body: web::Json<Payload>) {\n"
            "    let data = body.into_inner();\n"
            "    execute_query(data);\n"
            "}\n"
        )
        config = get_preset("actix-web")
        violations = run_trace_analysis([str(test_file)], config, language="rust")
        assert len(violations) >= 1

    def test_actix_web_sanitizer_blocks(self, tmp_path):
        """actix-web sanitizer blocks taint propagation.

        Note: Rust turbofish syntax ``parse::<i32>()`` cannot be matched by the
        emend pattern engine (tree-sitter represents it as a path expression).
        This test uses a plain sanitize() function call as a stand-in.
        """
        test_file = tmp_path / "handler.rs"
        test_file.write_text(
            "fn handler(query: web::Query<Params>) {\n"
            "    let raw = query.into_inner();\n"
            "    let safe = sanitize(raw);\n"
            "    execute_query(safe);\n"
            "}\n"
        )
        # Add a generic sanitize() sanitizer to the preset config
        from emend.trace import TraceSanitizer as TS
        config = get_preset("actix-web")
        config.sanitizers.append(TS(pattern="sanitize($X)", label="user_input"))
        violations = run_trace_analysis([str(test_file)], config, language="rust")
        assert len(violations) == 0

    def test_axum_sql_injection(self, tmp_path):
        """axum preset detects SQL injection from extractor via .into_inner()."""
        test_file = tmp_path / "handler.rs"
        test_file.write_text(
            "async fn handler(body: Json<Payload>) {\n"
            "    let data = body.into_inner();\n"
            "    execute_query(data);\n"
            "}\n"
        )
        config = get_preset("axum")
        violations = run_trace_analysis([str(test_file)], config, language="rust")
        assert len(violations) >= 1

    def test_axum_sanitizer_blocks(self, tmp_path):
        """axum sanitizer blocks taint propagation."""
        test_file = tmp_path / "handler.rs"
        test_file.write_text(
            "async fn handler(body: Json<Payload>) {\n"
            "    let raw = body.into_inner();\n"
            "    let safe = sanitize(raw);\n"
            "    execute_query(safe);\n"
            "}\n"
        )
        # Add a generic sanitize() sanitizer to the preset config
        from emend.trace import TraceSanitizer as TS
        config = get_preset("axum")
        config.sanitizers.append(TS(pattern="sanitize($X)", label="user_input"))
        violations = run_trace_analysis([str(test_file)], config, language="rust")
        assert len(violations) == 0

    def test_sqlx_injection_via_execute(self, tmp_path):
        """sqlx preset detects injection via pool.execute() when composed with actix-web.

        Note: ``sqlx::query($X)`` path patterns are not matchable by the emend
        pattern engine.  The preset uses ``$X.execute($Y)`` method-call style.
        """
        test_file = tmp_path / "handler.rs"
        test_file.write_text(
            "fn handler(query: web::Query<Params>, pool: PgPool) {\n"
            "    let name = query.into_inner();\n"
            "    pool.execute(name);\n"
            "}\n"
        )
        config = merge_configs(get_preset("actix-web"), get_preset("sqlx"))
        violations = run_trace_analysis([str(test_file)], config, language="rust")
        assert len(violations) >= 1

    def test_sqlx_no_sources_alone(self):
        """sqlx preset has no sources — must be composed with a framework preset."""
        config = get_preset("sqlx")
        assert len(config.sources) == 0

    def test_diesel_injection_via_execute(self, tmp_path):
        """diesel preset detects SQL injection via conn.execute() when composed with actix-web.

        Note: ``diesel::sql_query($X)`` path patterns are not matchable by the
        emend pattern engine.  The preset uses ``$X.execute($Y)`` method-call style.
        """
        test_file = tmp_path / "handler.rs"
        test_file.write_text(
            "fn handler(query: web::Query<Params>, conn: Connection) {\n"
            "    let name = query.into_inner();\n"
            "    conn.execute(name);\n"
            "}\n"
        )
        config = merge_configs(get_preset("actix-web"), get_preset("diesel"))
        violations = run_trace_analysis([str(test_file)], config, language="rust")
        assert len(violations) >= 1

    def test_diesel_no_sources_alone(self):
        """diesel preset has no sources — must be composed with a framework preset."""
        config = get_preset("diesel")
        assert len(config.sources) == 0
