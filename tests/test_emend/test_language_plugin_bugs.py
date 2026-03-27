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
