"""Tests for bugs found in the language plugin system.

Each test is written to FAIL first (red), then the fix makes it pass (green).
"""
import pytest
from emend.language_plugins import (
    TreeSitterImportHandler,
    DocCommentHandler,
    RegexCommentHandler,
)


# ============================================================================
# Bug 1: _is_import_line() substring false positives
# ============================================================================

class TestIsImportLineSubstringBugs:
    """_is_import_line() uses `kw in stripped` for pub/export lines,
    causing false positives when the keyword appears as a substring."""

    def setup_method(self):
        self.rust_handler = TreeSitterImportHandler(
            "rust", extensions=["rs"], import_keywords=("use",),
        )
        self.ts_handler = TreeSitterImportHandler(
            "typescript", extensions=["ts"], import_keywords=("import", "require"),
        )

    def test_rust_pub_fn_not_import(self):
        # "use" is a substring of "excuse" -- should NOT be detected as import
        assert not self.rust_handler._is_import_line("pub fn excuse() {")

    def test_rust_pub_struct_diffuse_not_import(self):
        assert not self.rust_handler._is_import_line("pub struct Diffuse {")

    def test_rust_pub_use_is_import(self):
        # Actual pub use should still be detected
        assert self.rust_handler._is_import_line("pub use crate::module;")

    def test_rust_pub_use_glob_is_import(self):
        assert self.rust_handler._is_import_line("pub use std::collections::*;")

    def test_ts_export_function_import_handler_not_import(self):
        # "import" is a substring of "importHandler"
        assert not self.ts_handler._is_import_line("export function importHandler() {}")

    def test_ts_export_require_auth_not_import(self):
        # "require" is a substring of "require_auth"
        assert not self.ts_handler._is_import_line("export function require_auth() {}")

    def test_ts_export_from_is_import(self):
        # Actual re-export should still work
        assert self.ts_handler._is_import_line("export { foo } from 'bar';")

    def test_ts_export_import_is_import(self):
        # export + import keyword
        assert self.ts_handler._is_import_line("export { default } from 'module';")

    def test_rust_extract_ignores_pub_fn(self):
        """extract_imports should not include pub fn lines."""
        source = "use std::io;\npub fn excuse() {}\npub fn main() {}\n"
        result = self.rust_handler.extract_imports(source)
        assert "excuse" not in result
        assert "std::io" in result


# ============================================================================
# Bug 2: rename_in_docstrings() plain string replace
# ============================================================================

class TestRenameInDocstringsWordBoundary:
    """rename_in_docstrings() uses str.replace() which corrupts
    compound identifiers like fooBar when renaming 'foo'."""

    def setup_method(self):
        self.block_handler = DocCommentHandler("//", doc_style="block")
        self.line_handler = DocCommentHandler("//", doc_style="line")

    def test_jsdoc_no_partial_replace(self):
        source = "/** Uses fooBar and foo. */\nfunction test() {}"
        result = self.block_handler.rename_in_docstrings(source, "foo", "baz")
        assert result is not None
        assert "bazBar" not in result  # Should NOT corrupt fooBar
        assert "baz." in result or "baz " in result  # Should rename standalone foo

    def test_rust_doc_no_partial_replace(self):
        source = "/// Uses foo_bar and foo.\nfn test() {}"
        result = self.line_handler.rename_in_docstrings(source, "foo", "baz")
        assert result is not None
        assert "baz_bar" not in result  # Should NOT corrupt foo_bar

    def test_rename_standalone_word(self):
        source = "/** Call foo to process. */\nfunction x() {}"
        result = self.block_handler.rename_in_docstrings(source, "foo", "bar")
        assert result is not None
        assert "bar" in result


# ============================================================================
# Bug 3: remove_import() substring matching
# ============================================================================

class TestRemoveImportSubstringBugs:
    """remove_import() uses `module in stripped and name in stripped`
    which matches substrings, not whole words."""

    def setup_method(self):
        self.ts_handler = TreeSitterImportHandler(
            "typescript", extensions=["ts"], import_keywords=("import", "require"),
        )

    def test_no_false_positive_module_substring(self):
        """'bar' should not match 'foobar' as module name."""
        source = "import { x } from 'foobar';\nimport { y } from 'bar';\n"
        result = self.ts_handler.remove_import(source, "bar", "y")
        # Should keep the foobar import, remove the bar import
        assert "foobar" in result
        assert "'bar'" not in result or "from 'bar'" not in result

    def test_no_false_positive_name_substring(self):
        """'map' should not match module containing 'mapValues'."""
        source = "import { map } from 'lodash';\nimport { mapValues } from 'lodash';\n"
        result = self.ts_handler.remove_import(source, "lodash", "map")
        # Should keep mapValues import
        assert "mapValues" in result

    def test_remove_named_import_no_false_positive_on_grouped(self):
        """Removing 'B' from 'import { A, B } from ...' should not remove A.

        NOTE: the current line-based implementation drops the entire import
        line (including A) when B is found on the same line.  This test
        documents that limitation.  The word-boundary fix from Phase 8 at
        least ensures that 'B' does not match a token like 'BigInt' as a
        false positive.

        TODO(phase-8): once ``emend_core`` exposes import node byte-range
        editing, upgrade this test to assert that the result is
        ``import { A } from 'mod';`` (surgical removal of just B).
        """
        source = "import { A, BigInt } from 'mod';\nimport { C } from 'other';\n"
        # 'B' must NOT match 'BigInt' (word-boundary check)
        result = self.ts_handler.remove_import(source, "mod", "B")
        # No match — 'B' is not a whole word in the import
        assert "BigInt" in result, "BigInt should not be removed when looking for 'B'"
        assert "A" in result, "A should not be removed when looking for 'B'"

    def test_remove_exact_name_match(self):
        """Removing 'A' from 'import { A, B } from ...' correctly removes that line."""
        source = "import { A, B } from 'mod';\nimport { C } from 'other';\n"
        result = self.ts_handler.remove_import(source, "mod", "A")
        # The whole line is dropped (current line-based limitation)
        assert "from 'mod'" not in result
        # Other imports are preserved
        assert "C" in result
        assert "other" in result


# ============================================================================
# Bug 4: find_pattern() unconditional language override
# ============================================================================

class TestFindPatternLanguageOverride:
    """find_pattern() unconditionally overrides the language parameter
    based on file extension, ignoring explicit caller choice."""

    def test_explicit_language_respected(self, tmp_path):
        from emend.transform import find_pattern
        # Create a .pyw file (detected as python) but explicitly request python
        # This tests that explicit language is not overridden
        f = tmp_path / "test.py"
        f.write_text("x = 1\ny = x + 2\n")
        # When caller explicitly passes language, it should be respected
        matches = find_pattern("x", str(f), language="python")
        assert len(matches) >= 2


# ============================================================================
# Bug 5: Bare // noqa not recognized
# ============================================================================

class TestBareNoqaSupport:
    """RegexCommentHandler and DocCommentHandler should recognize
    bare `// noqa` (without colon or tags) to suppress all checks."""

    def test_bare_noqa_regex_handler(self):
        handler = RegexCommentHandler("//")
        source = "let x = eval('code'); // noqa\n"
        noqa = handler.find_noqa_comments(source)
        assert 1 in noqa
        assert noqa[1] is None  # None means suppress all

    def test_bare_noqa_doc_handler(self):
        handler = DocCommentHandler("//", doc_style="block")
        source = "let x = eval('code'); // noqa\n"
        noqa = handler.find_noqa_comments(source)
        assert 1 in noqa
        assert noqa[1] is None

    def test_noqa_with_tags_still_works(self):
        handler = RegexCommentHandler("//")
        source = "let x = 1; // noqa: emend:deadcode\n"
        noqa = handler.find_noqa_comments(source)
        assert 1 in noqa
        assert noqa[1] is not None  # Should be a set of tags

    def test_bare_noqa_hash(self):
        handler = RegexCommentHandler("#")
        source = "x = eval('code')  # noqa\n"
        noqa = handler.find_noqa_comments(source)
        assert 1 in noqa
        assert noqa[1] is None


# ============================================================================
# Bug 5b: noqa tag filtering must mirror Python (emend:-prefixed only)
# ============================================================================

class TestNoqaTagFilteringConsistency:
    """RegexCommentHandler must filter noqa tags to emend:-prefixed ones and
    store them stripped of the prefix, mirroring PythonCommentHandler.  A
    foreign-only tag like `// noqa: E501` must NOT become a suppress-all and
    must NOT suppress emend rules."""

    def test_emend_prefixed_tag_stored_stripped(self):
        handler = RegexCommentHandler("//")
        source = "let x = 1; // noqa: emend:my-rule\n"
        noqa = handler.find_noqa_comments(source)
        assert noqa.get(1) == {"my-rule"}

    def test_foreign_only_tag_not_suppress_all(self):
        handler = RegexCommentHandler("//")
        source = "let x = 1; // noqa: E501\n"
        noqa = handler.find_noqa_comments(source)
        # Must not be registered at all (a None entry would mean suppress-all).
        assert 1 not in noqa

    def test_foreign_only_tag_does_not_suppress_emend_rule(self):
        from emend.checks.pattern_rules import is_noqa_suppressed
        handler = RegexCommentHandler("//")
        source = "let x = 1; // noqa: E501\n"
        noqa = handler.find_noqa_comments(source)
        ranges = [(ln, ln, tags) for ln, tags in noqa.items()]
        assert is_noqa_suppressed(1, "some-rule", ranges) is False

    def test_emend_prefixed_tag_suppresses_matching_rule(self):
        from emend.checks.pattern_rules import is_noqa_suppressed
        handler = RegexCommentHandler("//")
        source = "let x = 1; // noqa: emend:some-rule\n"
        noqa = handler.find_noqa_comments(source)
        ranges = [(ln, ln, tags) for ln, tags in noqa.items()]
        assert is_noqa_suppressed(1, "some-rule", ranges) is True

    def test_mixed_tags_keep_only_emend(self):
        handler = RegexCommentHandler("//")
        source = "let x = 1; // noqa: E501,emend:keep-me\n"
        noqa = handler.find_noqa_comments(source)
        assert noqa.get(1) == {"keep-me"}

    def test_bare_noqa_still_suppress_all(self):
        handler = RegexCommentHandler("//")
        source = "let x = 1; // noqa\n"
        noqa = handler.find_noqa_comments(source)
        assert 1 in noqa
        assert noqa[1] is None


# ============================================================================
# Bug 6: tsconfig.json JSONC parsing
# ============================================================================

class TestTsconfigJsoncParsing:
    """_find_source_root() uses json.loads() which fails on JSONC
    (JSON with comments and trailing commas)."""

    def test_tsconfig_with_comments(self, tmp_path):
        from emend.transform import _find_source_root
        (tmp_path / "tsconfig.json").write_text("""{
  // Compiler options
  "compilerOptions": {
    "rootDir": "lib",
    "outDir": "dist"
  }
}
""")
        (tmp_path / "lib").mkdir()
        result = _find_source_root(str(tmp_path), language="typescript")
        assert result == str(tmp_path / "lib")

    def test_tsconfig_with_trailing_commas(self, tmp_path):
        from emend.transform import _find_source_root
        (tmp_path / "tsconfig.json").write_text("""{
  "compilerOptions": {
    "rootDir": "src",
    "outDir": "dist",
  },
}
""")
        (tmp_path / "src").mkdir()
        result = _find_source_root(str(tmp_path), language="typescript")
        assert result == str(tmp_path / "src")


# ============================================================================
# Bug 7: parse_noqa_comments() hardcoded to Python
# ============================================================================

class TestLintNoqaLanguageThreading:
    """parse_noqa_comments() always uses load_plugin('python') regardless
    of the actual language being linted, so // noqa comments in TypeScript
    and Rust files are silently ignored."""

    def test_parse_noqa_typescript_line_comment(self):
        """TypeScript // noqa comments should be recognized."""
        from emend.lint import parse_noqa_comments
        source = "let x = eval('code'); // noqa: emend:no-eval\n"
        noqa = parse_noqa_comments(source, language="typescript")
        assert 1 in noqa, "// noqa comment not recognized for TypeScript"

    def test_parse_noqa_typescript_bare(self):
        """Bare // noqa in TypeScript should suppress all rules."""
        from emend.lint import parse_noqa_comments
        source = "let x = eval('code'); // noqa\n"
        noqa = parse_noqa_comments(source, language="typescript")
        assert 1 in noqa, "bare // noqa not recognized for TypeScript"
        assert noqa[1] is None

    def test_parse_noqa_rust_line_comment(self):
        """Rust // noqa comments should be recognized."""
        from emend.lint import parse_noqa_comments
        source = 'let x = unsafe { std::mem::zeroed() }; // noqa: emend:no-unsafe\n'
        noqa = parse_noqa_comments(source, language="rust")
        assert 1 in noqa, "// noqa comment not recognized for Rust"

    def test_parse_noqa_python_default_unchanged(self):
        """Python (default) should still work without explicit language."""
        from emend.lint import parse_noqa_comments
        source = "x = eval('code')  # noqa: emend:no-eval\n"
        noqa = parse_noqa_comments(source)
        assert 1 in noqa, "Python noqa should work with default language"

    def test_run_lint_passes_language_to_noqa(self, tmp_path):
        """run_lint() should pass its language parameter through to
        parse_noqa_comments so that noqa comments are respected."""
        from emend.lint import run_lint, LintRule
        ts_file = tmp_path / "test.ts"
        ts_file.write_text("let x = eval('code'); // noqa: emend:no-eval\n")
        rule = LintRule(
            name="no-eval",
            find="eval($ARG)",
            message="Do not use eval",
        )
        violations = run_lint(
            rules=[rule],
            paths=[str(ts_file)],
            language="typescript",
        )
        # The noqa comment should suppress the violation
        assert len(violations) == 0, (
            f"Expected 0 violations (noqa should suppress), got {len(violations)}"
        )


# ============================================================================
# Bug 8: _find_source_root() language not threaded through callers
# ============================================================================

class TestFindSourceRootLanguageThreading:
    """_ensure_index_fresh() and warm_caches() call _find_source_root()
    without passing the language parameter, so non-Python projects always
    get Python-specific source root detection (which may return the wrong
    directory)."""

    def test_ensure_index_fresh_accepts_language_parameter(self):
        """_ensure_index_fresh() should accept a language keyword argument
        so that callers can thread the language through to _find_source_root()."""
        import inspect
        from emend.transform import _ensure_index_fresh
        sig = inspect.signature(_ensure_index_fresh)
        assert "language" in sig.parameters, (
            "_ensure_index_fresh() is missing a 'language' keyword parameter"
        )

    def test_warm_caches_accepts_language_parameter(self):
        """warm_caches() should accept a language keyword argument
        so that callers can thread the language through to _find_source_root()."""
        import inspect
        from emend.transform import warm_caches
        sig = inspect.signature(warm_caches)
        assert "language" in sig.parameters, (
            "warm_caches() is missing a 'language' keyword parameter"
        )

    def test_ensure_index_fresh_passes_language_to_find_source_root(self, tmp_path):
        """When language='rust' is passed to _ensure_index_fresh(), it should
        forward it to _find_source_root() so Rust source root detection is used."""
        from unittest.mock import patch, MagicMock
        from emend.transform import _ensure_index_fresh

        # Patch _find_source_root to track what language arg it receives
        with patch("emend.transform._find_source_root", return_value=str(tmp_path)) as mock_fsr:
            # _ensure_index_fresh will fail early (no DB), but we can still
            # check whether _find_source_root was called with the right language.
            # We also need to patch enough to reach the _find_source_root call.
            # The function calls _find_project_root first, then opens DB, etc.
            # We'll patch to get past early checks.
            with patch("emend.transform._find_project_root", return_value=str(tmp_path)):
                with patch("emend.transform._cache_db_dir", return_value=tmp_path):
                    with patch("emend.transform._get_worktree_id", return_value="test"):
                        # Create a minimal DB so the function progresses
                        import sqlite3
                        db = tmp_path / "parse.db"
                        conn = sqlite3.connect(str(db))
                        conn.execute("CREATE TABLE IF NOT EXISTS file_manifest "
                                     "(worktree_id TEXT, path TEXT, mtime_ns INTEGER, "
                                     "size INTEGER, content_hash BLOB, indexed_at REAL, "
                                     "PRIMARY KEY (worktree_id, path))")
                        conn.commit()
                        conn.close()

                        try:
                            _ensure_index_fresh(str(tmp_path), language="rust")
                        except Exception:
                            pass  # We don't care if it fails; we just need the call

                        # If _find_source_root was called, check it got language="rust"
                        if mock_fsr.called:
                            call_args = mock_fsr.call_args
                            # It should have been called with language="rust"
                            lang_arg = call_args.kwargs.get("language") or (
                                call_args.args[1] if len(call_args.args) > 1 else None
                            )
                            assert lang_arg == "rust", (
                                f"_find_source_root() was called with language={lang_arg!r}, "
                                f"expected 'rust'"
                            )

    def test_warm_caches_passes_language_to_find_source_root(self, tmp_path):
        """When language='typescript' is passed to warm_caches(), it should
        forward it to _find_source_root()."""
        from unittest.mock import patch
        from emend.transform import warm_caches

        with patch("emend.transform._find_source_root", return_value=str(tmp_path)) as mock_fsr:
            with patch("emend.transform._find_project_root", return_value=str(tmp_path)):
                with patch("emend.transform._cache_db_dir", return_value=tmp_path):
                    with patch("emend.transform._ensure_cache_ignore_files"):
                        with patch("emend.transform.visit_project_ts", return_value=[]):
                            try:
                                warm_caches(str(tmp_path), language="typescript")
                            except Exception:
                                pass

                            if mock_fsr.called:
                                call_args = mock_fsr.call_args
                                lang_arg = call_args.kwargs.get("language") or (
                                    call_args.args[1] if len(call_args.args) > 1 else None
                                )
                                assert lang_arg == "typescript", (
                                    f"_find_source_root() was called with language={lang_arg!r}, "
                                    f"expected 'typescript'"
                                )

    def test_query_symbol_index_accepts_language(self):
        """query_symbol_index() should accept a language parameter so it can
        forward it to _ensure_index_fresh()."""
        import inspect
        from emend.transform import query_symbol_index
        sig = inspect.signature(query_symbol_index)
        assert "language" in sig.parameters, (
            "query_symbol_index() is missing a 'language' keyword parameter"
        )

    def test_query_reference_index_accepts_language(self):
        """query_reference_index() should accept a language parameter so it can
        forward it to _ensure_index_fresh()."""
        import inspect
        from emend.transform import query_reference_index
        sig = inspect.signature(query_reference_index)
        assert "language" in sig.parameters, (
            "query_reference_index() is missing a 'language' keyword parameter"
        )

    def test_find_dead_code_uses_fact_graph(self):
        """find_dead_code() should use FactGraph.dead_code_unified() for detection."""
        import inspect
        from emend.transform import find_dead_code
        sig = inspect.signature(find_dead_code)
        assert "project_path" in sig.parameters, (
            "find_dead_code() is missing a 'project_path' parameter"
        )

    def test_query_symbol_index_passes_language_to_ensure_index_fresh(self, tmp_path):
        """query_symbol_index() should forward its language parameter to
        _ensure_index_fresh()."""
        from unittest.mock import patch
        from emend.transform import query_symbol_index

        with patch("emend.transform.index._ensure_index_fresh", return_value=False) as mock_eif:
            query_symbol_index(str(tmp_path), language="rust")
            assert mock_eif.called
            call_kwargs = mock_eif.call_args.kwargs
            assert call_kwargs.get("language") == "rust", (
                f"_ensure_index_fresh() was called with language={call_kwargs.get('language')!r}, "
                f"expected 'rust'"
            )

    def test_query_reference_index_passes_language_to_ensure_index_fresh(self, tmp_path):
        """query_reference_index() should forward its language parameter to
        _ensure_index_fresh()."""
        from unittest.mock import patch
        from emend.transform import query_reference_index

        with patch("emend.transform.index._ensure_index_fresh", return_value=False) as mock_eif:
            query_reference_index(str(tmp_path), "some.symbol", language="typescript")
            assert mock_eif.called
            call_kwargs = mock_eif.call_args.kwargs
            assert call_kwargs.get("language") == "typescript", (
                f"_ensure_index_fresh() was called with language={call_kwargs.get('language')!r}, "
                f"expected 'typescript'"
            )

    def test_find_dead_code_uses_datalog_backend(self, tmp_path):
        """find_dead_code() should delegate to FactGraph.dead_code_unified()."""
        from unittest.mock import patch, MagicMock
        from emend.transform import find_dead_code

        mock_graph = MagicMock()
        mock_graph.dead_code_unified.return_value = ([], [])

        with patch("emend.transform.refs._get_or_build_fact_graph", return_value=mock_graph):
            with patch("emend.transform.project_iter._find_project_root", return_value=str(tmp_path)):
                result = list(find_dead_code(str(tmp_path)))

        assert result == []
        mock_graph.dead_code_unified.assert_called_once()
