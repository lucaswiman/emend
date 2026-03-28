"""Tests for tree-sitter grammar support for HTML, CSS, SQL, and Jinja2.

Verifies that the tree-sitter grammars for these DSL languages are properly
integrated into emend_core: parsing, config loading, symbol extraction,
scope resolution, and language registry detection.
"""
from __future__ import annotations

import textwrap

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(source: str, ext: str):
    """Parse source and return True if tree-sitter produced a valid tree."""
    from emend import emend_core
    syms = emend_core.collect_symbols_from_str(
        textwrap.dedent(source), ext=ext
    )
    # Even if no symbols found, if we get here without error the parser loaded
    return syms


def _scope_resolver(tmp_path, ext: str):
    """Create a PyScopeResolver for the given extension."""
    from emend import emend_core
    return emend_core.PyScopeResolver(str(tmp_path), ext)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class TestHtmlParsing:
    """Verify HTML tree-sitter grammar is loaded and can parse HTML."""

    def test_html_parse_basic(self):
        source = """\
            <html>
            <head><title>Test</title></head>
            <body><p>Hello</p></body>
            </html>
        """
        result = _parse(source, "html")
        # Should not raise; parser loaded successfully
        assert isinstance(result, list)

    def test_html_parse_with_attributes(self):
        source = '<div class="main" id="app"><span data-value="42">Text</span></div>'
        result = _parse(source, "html")
        assert isinstance(result, list)

    def test_html_parse_self_closing(self):
        source = '<img src="photo.jpg" /><br /><input type="text" />'
        result = _parse(source, "html")
        assert isinstance(result, list)

    def test_html_scope_resolver(self, tmp_path):
        f = tmp_path / "test.html"
        source = "<html><body><div>Hello</div></body></html>"
        f.write_text(source)
        resolver = _scope_resolver(tmp_path, "html")
        resolver.index_file(str(f), source)
        # Should not raise -- config loaded and indexed successfully

    def test_htm_extension(self):
        source = "<p>Hello</p>"
        result = _parse(source, "htm")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# CSS parsing
# ---------------------------------------------------------------------------


class TestCssParsing:
    """Verify CSS tree-sitter grammar is loaded and can parse CSS."""

    def test_css_parse_basic(self):
        source = """\
            body {
                color: red;
                font-size: 16px;
            }
        """
        result = _parse(source, "css")
        assert isinstance(result, list)

    def test_css_parse_selectors(self):
        source = """\
            .container > .item:hover {
                background: blue;
            }
            #main {
                display: flex;
            }
        """
        result = _parse(source, "css")
        assert isinstance(result, list)

    def test_css_parse_media_query(self):
        source = """\
            @media (max-width: 768px) {
                .sidebar { display: none; }
            }
        """
        result = _parse(source, "css")
        assert isinstance(result, list)

    def test_css_parse_keyframes(self):
        source = """\
            @keyframes fadeIn {
                from { opacity: 0; }
                to { opacity: 1; }
            }
        """
        result = _parse(source, "css")
        assert isinstance(result, list)

    def test_css_scope_resolver(self, tmp_path):
        f = tmp_path / "test.css"
        source = "body { color: red; } .main { font-size: 16px; }"
        f.write_text(source)
        resolver = _scope_resolver(tmp_path, "css")
        resolver.index_file(str(f), source)
        # Should not raise


# ---------------------------------------------------------------------------
# SQL parsing
# ---------------------------------------------------------------------------


class TestSqlParsing:
    """Verify SQL tree-sitter grammar is loaded and can parse SQL."""

    def test_sql_parse_select(self):
        source = "SELECT id, name FROM users WHERE active = true;"
        result = _parse(source, "sql")
        assert isinstance(result, list)

    def test_sql_parse_create_table(self):
        source = """\
            CREATE TABLE users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) UNIQUE,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """
        result = _parse(source, "sql")
        assert isinstance(result, list)

    def test_sql_parse_join(self):
        source = """\
            SELECT u.name, o.total
            FROM users u
            JOIN orders o ON u.id = o.user_id
            WHERE o.total > 100;
        """
        result = _parse(source, "sql")
        assert isinstance(result, list)

    def test_sql_parse_insert(self):
        source = "INSERT INTO users (name, email) VALUES ('Alice', 'alice@example.com');"
        result = _parse(source, "sql")
        assert isinstance(result, list)

    def test_sql_parse_create_function(self):
        source = """\
            CREATE FUNCTION add(a integer, b integer) RETURNS integer
            AS 'select $1 + $2;'
            LANGUAGE SQL;
        """
        result = _parse(source, "sql")
        assert isinstance(result, list)

    def test_sql_parse_cte(self):
        source = """\
            WITH active_users AS (
                SELECT * FROM users WHERE active = true
            )
            SELECT name FROM active_users;
        """
        result = _parse(source, "sql")
        assert isinstance(result, list)

    def test_sql_scope_resolver(self, tmp_path):
        f = tmp_path / "test.sql"
        source = "SELECT id, name FROM users WHERE active = true;"
        f.write_text(source)
        resolver = _scope_resolver(tmp_path, "sql")
        resolver.index_file(str(f), source)
        # Should not raise


# ---------------------------------------------------------------------------
# Jinja2 parsing
# ---------------------------------------------------------------------------


class TestJinja2Parsing:
    """Verify Jinja2 tree-sitter grammar is loaded and can parse Jinja2."""

    def test_jinja2_parse_expression(self):
        source = "<p>Hello {{ name }}</p>"
        result = _parse(source, "jinja2")
        assert isinstance(result, list)

    def test_jinja2_parse_for_loop(self):
        source = """\
            {% for item in items %}
                <li>{{ item.name }}</li>
            {% endfor %}
        """
        result = _parse(source, "jinja2")
        assert isinstance(result, list)

    def test_jinja2_parse_if_block(self):
        source = """\
            {% if user.is_admin %}
                <span>Admin</span>
            {% elif user.is_staff %}
                <span>Staff</span>
            {% else %}
                <span>User</span>
            {% endif %}
        """
        result = _parse(source, "jinja2")
        assert isinstance(result, list)

    def test_jinja2_parse_extends_and_block(self):
        source = """\
            {% extends "base.html" %}
            {% block title %}My Page{% endblock %}
            {% block content %}
                <h1>Hello</h1>
            {% endblock %}
        """
        result = _parse(source, "jinja2")
        assert isinstance(result, list)

    def test_jinja2_parse_macro(self):
        source = """\
            {% macro input(name, value='', type='text') %}
                <input type="{{ type }}" name="{{ name }}" value="{{ value }}">
            {% endmacro %}
        """
        result = _parse(source, "jinja2")
        assert isinstance(result, list)

    def test_jinja2_parse_filter(self):
        source = "{{ name|title|escape }}"
        result = _parse(source, "jinja2")
        assert isinstance(result, list)

    def test_jinja2_j2_extension(self):
        source = "{{ variable }}"
        result = _parse(source, "j2")
        assert isinstance(result, list)

    def test_jinja2_jinja_extension(self):
        source = "{% block content %}Hello{% endblock %}"
        result = _parse(source, "jinja")
        assert isinstance(result, list)

    def test_jinja2_scope_resolver(self, tmp_path):
        f = tmp_path / "test.jinja2"
        source = "{% for item in items %}{{ item.name }}{% endfor %}"
        f.write_text(source)
        resolver = _scope_resolver(tmp_path, "jinja2")
        resolver.index_file(str(f), source)
        # Should not raise


# ---------------------------------------------------------------------------
# Language registry integration
# ---------------------------------------------------------------------------


class TestLanguageRegistry:
    """Verify the Python-side language registry detects the new languages."""

    def test_detect_html(self):
        from emend.language_registry import detect_language
        assert detect_language("page.html") == "html"
        assert detect_language("page.htm") == "html"

    def test_detect_css(self):
        from emend.language_registry import detect_language
        assert detect_language("style.css") == "css"

    def test_detect_sql(self):
        from emend.language_registry import detect_language
        assert detect_language("schema.sql") == "sql"

    def test_detect_jinja2(self):
        from emend.language_registry import detect_language
        assert detect_language("template.jinja2") == "jinja2"
        assert detect_language("template.jinja") == "jinja2"
        assert detect_language("template.j2") == "jinja2"

    def test_get_extensions_html(self):
        from emend.language_registry import get_extensions
        exts = get_extensions("html")
        assert "html" in exts
        assert "htm" in exts

    def test_get_extensions_css(self):
        from emend.language_registry import get_extensions
        exts = get_extensions("css")
        assert "css" in exts

    def test_get_extensions_sql(self):
        from emend.language_registry import get_extensions
        exts = get_extensions("sql")
        assert "sql" in exts

    def test_get_extensions_jinja2(self):
        from emend.language_registry import get_extensions
        exts = get_extensions("jinja2")
        assert "jinja2" in exts
        assert "jinja" in exts
        assert "j2" in exts

    def test_all_languages_includes_new(self):
        from emend.language_registry import get_all_languages
        langs = get_all_languages()
        for lang in ("html", "css", "sql", "jinja2"):
            assert lang in langs

    def test_is_source_file(self):
        from emend.language_registry import is_source_file
        assert is_source_file("test.html")
        assert is_source_file("test.css")
        assert is_source_file("test.sql")
        assert is_source_file("test.jinja2")
        assert is_source_file("test.j2")
