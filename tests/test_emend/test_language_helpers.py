"""Tests for Phase 3: Language-Parameterised Helpers.

Tests that _get_keywords(), _extract_identifiers(), and
_get_entry_point_config() are driven by language config rather than
hardcoded Python assumptions.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Keyword helpers (trace.py)
# ---------------------------------------------------------------------------

class TestGetKeywords:
    def test_python_returns_python_keywords(self):
        from emend.trace import _get_keywords
        kws = _get_keywords("python")
        # Core Python keywords
        assert "False" in kws
        assert "None" in kws
        assert "True" in kws
        assert "return" in kws
        assert "def" in kws
        assert "class" in kws
        assert "import" in kws
        assert "if" in kws
        assert "for" in kws

    def test_typescript_returns_typescript_keywords(self):
        from emend.trace import _get_keywords
        kws = _get_keywords("typescript")
        # TypeScript-specific keywords
        assert "const" in kws
        assert "let" in kws
        assert "var" in kws
        assert "function" in kws
        assert "interface" in kws
        assert "typeof" in kws
        assert "null" in kws
        assert "undefined" in kws
        # Must NOT contain Python-only keywords
        assert "def" not in kws
        assert "elif" not in kws
        assert "nonlocal" not in kws

    def test_rust_returns_rust_keywords(self):
        from emend.trace import _get_keywords
        kws = _get_keywords("rust")
        # Rust-specific keywords
        assert "fn" in kws
        assert "let" in kws
        assert "mut" in kws
        assert "struct" in kws
        assert "impl" in kws
        assert "match" in kws
        assert "self" in kws
        assert "Self" in kws
        # Must NOT contain Python-only keywords
        assert "def" not in kws
        assert "elif" not in kws

    def test_python_keywords_is_frozenset(self):
        from emend.trace import _get_keywords
        kws = _get_keywords("python")
        assert isinstance(kws, frozenset)

    def test_unknown_language_falls_back_to_python(self):
        from emend.trace import _get_keywords
        # Unknown language should fall back to _PYTHON_KEYWORDS
        kws = _get_keywords("unknown_lang_xyz")
        assert isinstance(kws, frozenset)
        # Should at least have some keywords (the fallback)
        assert len(kws) > 0

    def test_repeated_calls_cached(self):
        from emend.trace import _get_keywords
        # Should return the same object (cached)
        kws1 = _get_keywords("python")
        kws2 = _get_keywords("python")
        assert kws1 is kws2


# ---------------------------------------------------------------------------
# _extract_identifiers with language parameter (trace.py)
# ---------------------------------------------------------------------------

class TestExtractIdentifiers:
    def test_python_filters_python_keywords(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("x + True", language="python")
        assert "x" in result
        assert "True" not in result

    def test_python_filters_none_keyword(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("x if x is not None else y", language="python")
        assert "x" in result
        assert "y" in result
        assert "None" not in result
        assert "not" not in result
        assert "is" not in result

    def test_typescript_filters_typescript_keywords(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("x + null", language="typescript")
        assert "x" in result
        assert "null" not in result

    def test_typescript_filters_undefined(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("x !== undefined", language="typescript")
        assert "x" in result
        assert "undefined" not in result

    def test_rust_filters_rust_keywords(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("x + self", language="rust")
        assert "x" in result
        assert "self" not in result

    def test_rust_filters_let_keyword(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("let x = y", language="rust")
        assert "x" in result
        assert "y" in result
        assert "let" not in result

    def test_dotted_identifiers_extracted(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("obj.field + x", language="python")
        assert "obj.field" in result
        assert "x" in result

    def test_subscript_identifiers_extracted(self):
        from emend.trace import _extract_identifiers
        result = _extract_identifiers("data['key']", language="python")
        assert "data['key']" in result

    def test_default_language_is_python(self):
        from emend.trace import _extract_identifiers
        # Default should behave like Python
        result_default = _extract_identifiers("x + True")
        result_python = _extract_identifiers("x + True", language="python")
        assert result_default == result_python


# ---------------------------------------------------------------------------
# Entry point config (transform.py)
# ---------------------------------------------------------------------------

class TestGetEntryPointConfig:
    def test_python_returns_python_config(self):
        from emend.transform import _get_entry_point_config
        ep = _get_entry_point_config("python")
        assert "decorators" in ep
        assert "decorator_basenames" in ep
        assert "names" in ep
        assert "name_prefixes" in ep
        assert "has_dunders" in ep
        # Python-specific values
        assert "app.route" in ep["decorators"]
        assert "pytest.fixture" in ep["decorators"]
        assert "main" in ep["names"]
        assert ep["has_dunders"] is True

    def test_typescript_returns_typescript_config(self):
        from emend.transform import _get_entry_point_config
        ep = _get_entry_point_config("typescript")
        assert "decorators" in ep
        assert "decorator_basenames" in ep
        assert "names" in ep
        assert "name_prefixes" in ep
        assert "has_dunders" in ep
        # TypeScript specifics
        assert ep["has_dunders"] is False
        # TypeScript entry-point names include test framework functions
        assert "main" in ep["names"]

    def test_rust_returns_rust_config(self):
        from emend.transform import _get_entry_point_config
        ep = _get_entry_point_config("rust")
        assert "decorators" in ep
        assert "decorator_basenames" in ep
        assert "names" in ep
        assert "name_prefixes" in ep
        assert "has_dunders" in ep
        # Rust specifics
        assert ep["has_dunders"] is False
        assert "main" in ep["names"]
        # Rust uses #[test] as entry point decorator
        assert "test" in ep["decorators"]

    def test_returns_frozensets(self):
        from emend.transform import _get_entry_point_config
        ep = _get_entry_point_config("python")
        assert isinstance(ep["decorators"], frozenset)
        assert isinstance(ep["decorator_basenames"], frozenset)
        assert isinstance(ep["names"], frozenset)
        assert isinstance(ep["name_prefixes"], list)

    def test_repeated_calls_cached(self):
        from emend.transform import _get_entry_point_config
        ep1 = _get_entry_point_config("python")
        ep2 = _get_entry_point_config("python")
        # Same dict object (cached)
        assert ep1 is ep2
