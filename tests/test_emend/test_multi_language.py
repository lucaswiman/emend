"""Multi-language support tests for TypeScript and Rust.

Tests cover symbol collection, pattern matching, scope resolution,
import handling, doc comment handling, language detection, and plugin loading.
"""
import pytest
from pathlib import Path
from emend.cli_base import resolve_files
from emend.transform import find_pattern, visit_project_ts
from emend.ast_commands import collect_symbols
from emend.language_registry import (
    detect_language,
    get_extensions,
    get_all_languages,
    get_module_separator,
    is_source_file,
    load_config,
)
from emend.language_plugins import (
    load_plugin,
    TreeSitterImportHandler,
    DocCommentHandler,
    TreeSitterPatternCompiler,
)

# ============================================================================
# Language detection and registry
# ============================================================================

class TestLanguageDetection:
    @pytest.mark.parametrize(
        "path, expected",
        [
            pytest.param("foo.py", "python", id="python-source"),
            pytest.param("bar.pyi", "python", id="python-stub"),
            pytest.param("foo.ts", "typescript", id="typescript"),
            pytest.param("bar.tsx", "typescript", id="tsx"),
            pytest.param("baz.js", "typescript", id="javascript"),
            pytest.param("qux.jsx", "typescript", id="jsx"),
            pytest.param("foo.rs", "rust", id="rust"),
            pytest.param("foo.txt", None, id="unknown-extension"),
            pytest.param("Makefile", None, id="extensionless"),
        ],
    )
    def test_detect_language_contract(self, path, expected):
        assert detect_language(path) == expected

    @pytest.mark.parametrize(
        "language, expected_extensions",
        [
            pytest.param("python", {"py", "pyi"}, id="python"),
            pytest.param("typescript", {"ts", "tsx", "js"}, id="typescript"),
            pytest.param("rust", {"rs"}, id="rust"),
        ],
    )
    def test_get_extensions_contract(self, language, expected_extensions):
        assert expected_extensions <= set(get_extensions(language))

    def test_get_all_languages(self):
        langs = get_all_languages()
        assert "python" in langs
        assert "typescript" in langs
        assert "rust" in langs

    def test_module_separator(self):
        assert get_module_separator("python") == "."
        assert get_module_separator("rust") == "::"

    def test_is_source_file(self):
        assert is_source_file("foo.py")
        assert is_source_file("bar.ts")
        assert is_source_file("baz.rs")
        assert not is_source_file("readme.txt")


class TestLanguageConfig:
    @pytest.mark.parametrize(
        "language, extension",
        [
            pytest.param("python", "py", id="python"),
            pytest.param("typescript", "ts", id="typescript"),
            pytest.param("rust", "rs", id="rust"),
        ],
    )
    def test_config_contract(self, language, extension):
        config = load_config(language)
        assert config.get("language", {}).get("name") == language
        assert extension in config.get("language", {}).get("file_extensions", [])
        for required_section in ("scoping", "pattern_matching", "symbols"):
            assert required_section in config, (
                f"{language} config missing [{required_section}]"
            )


# ============================================================================
# Plugin loading
# ============================================================================

class TestPluginLoading:
    def test_load_python_plugin(self):
        plugin = load_plugin("python")
        assert plugin.import_handler is not None
        assert plugin.comment_handler is not None
        assert plugin.pattern_compiler is not None

    def test_load_typescript_plugin(self):
        plugin = load_plugin("typescript")
        assert isinstance(plugin.import_handler, TreeSitterImportHandler)
        assert isinstance(plugin.comment_handler, DocCommentHandler)
        assert isinstance(plugin.pattern_compiler, TreeSitterPatternCompiler)

    def test_load_rust_plugin(self):
        plugin = load_plugin("rust")
        assert isinstance(plugin.import_handler, TreeSitterImportHandler)
        assert isinstance(plugin.comment_handler, DocCommentHandler)
        assert isinstance(plugin.pattern_compiler, TreeSitterPatternCompiler)

    def test_unknown_language_gets_stubs(self):
        plugin = load_plugin("cobol")
        assert plugin.import_handler is not None
        assert plugin.comment_handler is not None


# ============================================================================
# File resolution
# ============================================================================

class TestFileResolution:
    def test_resolve_files_typescript(self, tmp_path):
        (tmp_path / "a.ts").write_text("const x = 1;")
        (tmp_path / "b.py").write_text("x = 1")

        files, is_multi = resolve_files(str(tmp_path), language="typescript")
        assert len(files) == 1
        assert files[0].name == "a.ts"

    def test_resolve_files_rust(self, tmp_path):
        (tmp_path / "a.rs").write_text("fn main() {}")
        (tmp_path / "b.py").write_text("x = 1")

        files, is_multi = resolve_files(str(tmp_path), language="rust")
        assert len(files) == 1
        assert files[0].name == "a.rs"

    def test_resolve_files_mixed(self, tmp_path):
        (tmp_path / "a.ts").write_text("const x = 1;")
        (tmp_path / "b.rs").write_text("fn main() {}")
        (tmp_path / "c.py").write_text("x = 1")

        py_files, _ = resolve_files(str(tmp_path), language="python")
        ts_files, _ = resolve_files(str(tmp_path), language="typescript")
        rs_files, _ = resolve_files(str(tmp_path), language="rust")
        assert len(py_files) == 1
        assert len(ts_files) == 1
        assert len(rs_files) == 1


# ============================================================================
# Symbol collection
# ============================================================================

SYMBOL_COLLECTION_SCENARIOS = [
    pytest.param("ts", "function greet(name: string): string { return name; }", [("greet",)], id="typescript-function"),
    pytest.param("ts", """
class Foo {
    bar() { return 1; }
    baz(x: number) { return x; }
}
""", [("Foo",)], id="typescript-class-methods"),
    pytest.param("ts", "const add = (a: number, b: number) => a + b;", [("add",)], id="typescript-arrow-function"),
    pytest.param("ts", "export function helper() { return 42; }", [("helper",)], id="typescript-exported-function"),
    pytest.param("ts", "class Foo { method() {} } function bar() {}", [("Foo",), ("bar",)], id="typescript-multiple-symbols"),
    pytest.param("rs", 'fn hello() -> String { String::from("hello") }', [("hello",)], id="rust-function"),
    pytest.param("rs", """
struct Point {
    x: f64,
    y: f64,
}

impl Point {
    fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }
}
""", [("Point",)], id="rust-struct-impl"),
    pytest.param("rs", "enum Color { Red, Green, Blue }", [("Color",)], id="rust-enum"),
    pytest.param("rs", "trait Drawable { fn draw(&self); }", [("Drawable",)], id="rust-trait"),
    pytest.param("rs", "mod utils { pub fn helper() {} }", [("helper", "utils")], id="rust-module-or-nested-function"),
    pytest.param("rs", "struct Foo; impl Foo { fn method(&self) {} } fn bar() {}", [("Foo",), ("bar",)], id="rust-multiple-symbols"),
]


@pytest.mark.parametrize("extension, source, expected_name_groups", SYMBOL_COLLECTION_SCENARIOS)
def test_symbol_collection_contract(tmp_path, extension, source, expected_name_groups):
    path = tmp_path / f"test.{extension}"
    path.write_text(source)
    names = [symbol.name for symbol in collect_symbols(str(path))]
    for alternatives in expected_name_groups:
        assert any(candidate in name for candidate in alternatives for name in names)


# ============================================================================
# Pattern matching
# ============================================================================

PATTERN_MATCH_SCENARIOS = [
    pytest.param("ts", "const x = 1;\nconst y = x + 2;", "x", 2, None, id="typescript-identifier"),
    pytest.param("ts", "console.log('hello'); console.log('world');", "console.log", 2, 2, id="typescript-member-expression"),
    pytest.param("ts", "const a = 'hello';\nconst b = 'world';", "'hello'", 1, 1, id="typescript-string-literal"),
    pytest.param("ts", "const x = 42;\nconst y = 43;\nconst z = 42;", "42", 2, 2, id="typescript-number-literal"),
    pytest.param("rs", "let x = 1;\nlet y = 2;\nlet z = x;", "x", 2, 2, id="rust-identifier-declaration-and-use"),
    pytest.param("rs", 'fn main() { println!("hello"); let v = vec![1,2,3]; }', "println", 1, None, id="rust-macro-identifier"),
]


@pytest.mark.parametrize("extension, source, pattern, minimum, exact", PATTERN_MATCH_SCENARIOS)
def test_pattern_matching_contract(tmp_path, extension, source, pattern, minimum, exact):
    path = tmp_path / f"test.{extension}"
    path.write_text(source)
    matches = find_pattern(pattern, str(path))
    assert len(matches) >= minimum
    if exact is not None:
        assert len(matches) == exact


def test_rust_number_literal_pattern_is_a_best_effort_smoke_contract(tmp_path):
    """Rust integer matching is not promised here, but must not crash."""
    path = tmp_path / "test.rs"
    path.write_text("fn main() { let x = 42;\nlet y = 43;\nlet z = 42; }")
    assert isinstance(find_pattern("42", str(path)), list)


# ============================================================================
# Scope resolution
# ============================================================================

class TestScopeResolution:
    def test_typescript_scope(self, tmp_path):
        from emend import emend_core
        f = tmp_path / "test.ts"
        source = """
function outer() {
    const x = 1;
    function inner() {
        const y = x + 1;
    }
}
"""
        f.write_text(source)
        resolver = emend_core.PyScopeResolver(str(tmp_path), "ts")
        resolver.index_file(str(f), source)
        defs = resolver.definitions_in_file(str(f))
        names = [d[0] for d in defs]
        # Definitions may be qualified (e.g. "test/outer")
        assert any("outer" in n for n in names)

    def test_rust_scope(self, tmp_path):
        from emend import emend_core
        f = tmp_path / "test.rs"
        source = """
fn outer() {
    let x = 1;
    fn inner() {
        let y = 2;
    }
}
"""
        f.write_text(source)
        resolver = emend_core.PyScopeResolver(str(tmp_path), "rs")
        resolver.index_file(str(f), source)
        defs = resolver.definitions_in_file(str(f))
        names = [d[0] for d in defs]
        # Definitions may be qualified (e.g. "test::outer")
        assert any("outer" in n for n in names)

    def test_typescript_qualified_names(self, tmp_path):
        from emend import emend_core
        f = tmp_path / "test.ts"
        source = "class Foo { bar() {} }"
        f.write_text(source)
        resolver = emend_core.PyScopeResolver(str(tmp_path), "ts")
        resolver.index_file(str(f), source)
        qns = resolver.all_qualified_names()
        # Should contain Foo and Foo.bar (or similar)
        assert any("Foo" in qn for qn in qns)

    def test_rust_qualified_names(self, tmp_path):
        from emend import emend_core
        f = tmp_path / "test.rs"
        source = "struct Foo; impl Foo { fn bar(&self) {} }"
        f.write_text(source)
        resolver = emend_core.PyScopeResolver(str(tmp_path), "rs")
        resolver.index_file(str(f), source)
        qns = resolver.all_qualified_names()
        assert any("Foo" in qn for qn in qns)


# ============================================================================
# Import handling
# ============================================================================

class TestImportHandlerTypescript:
    def setup_method(self):
        self.handler = TreeSitterImportHandler(
            "typescript", extensions=["ts", "tsx", "js", "jsx"],
            import_keywords=("import", "require"),
        )

    def test_extract_named_import(self):
        source = "import { foo, bar } from 'module';\nconst x = foo();"
        result = self.handler.extract_imports(source)
        assert "import" in result
        assert "module" in result

    def test_extract_default_import(self):
        source = "import React from 'react';\nconst el = React.createElement('div');"
        result = self.handler.extract_imports(source)
        assert "react" in result

    def test_extract_no_imports(self):
        source = "const x = 1;\nconst y = 2;"
        result = self.handler.extract_imports(source)
        assert result == ""

    def test_add_import_append(self):
        source = "import { foo } from 'bar';\nconst x = 1;\n"
        result = self.handler.add_import_text("import { baz } from 'qux';", -1, source)
        assert "baz" in result
        assert "qux" in result

    def test_add_import_prepend(self):
        source = "import { foo } from 'bar';\nconst x = 1;\n"
        result = self.handler.add_import_text("import { baz } from 'qux';", 0, source)
        lines = result.splitlines()
        # New import should be at the top
        assert any("baz" in line for line in lines[:2])

    def test_remove_import(self):
        source = "import { foo } from 'bar';\nimport { baz } from 'qux';\nconst x = 1;\n"
        result = self.handler.remove_import(source, "bar", "foo")
        assert "foo" not in result
        assert "baz" in result


class TestImportHandlerRust:
    def setup_method(self):
        self.handler = TreeSitterImportHandler(
            "rust", extensions=["rs"], import_keywords=("use",),
        )

    def test_extract_use(self):
        source = "use std::collections::HashMap;\nfn main() {}\n"
        result = self.handler.extract_imports(source)
        assert "use" in result
        assert "HashMap" in result

    def test_extract_multiple_use(self):
        source = "use std::io;\nuse std::fmt;\nfn main() {}\n"
        result = self.handler.extract_imports(source)
        assert "std::io" in result
        assert "std::fmt" in result

    def test_extract_no_imports(self):
        source = "fn main() { let x = 1; }\n"
        result = self.handler.extract_imports(source)
        assert result == ""

    def test_add_import(self):
        source = "use std::io;\nfn main() {}\n"
        result = self.handler.add_import_text("use std::fmt;", -1, source)
        assert "std::fmt" in result

    def test_remove_import(self):
        source = "use std::io;\nuse std::fmt;\nfn main() {}\n"
        result = self.handler.remove_import(source, "std", "io")
        assert "std::io" not in result
        assert "std::fmt" in result


# ============================================================================
# Doc comment handling
# ============================================================================

class TestDocCommentHandlerTypescript:
    def setup_method(self):
        self.handler = DocCommentHandler("//", doc_style="block")

    def test_find_jsdoc(self):
        source = """/** This is a doc comment */
function foo() {}
const x = 1;
"""
        docs = self.handler.find_docstrings(source, (0, len(source.encode())))
        assert len(docs) == 1
        assert "doc comment" in docs[0][2]

    def test_find_multiline_jsdoc(self):
        source = """/**
 * Does something useful.
 * @param x - a number
 */
function doSomething(x: number) {}
"""
        docs = self.handler.find_docstrings(source, (0, len(source.encode())))
        assert len(docs) == 1
        assert "@param" in docs[0][2]

    def test_rename_in_jsdoc(self):
        source = """/**
 * Calls oldFunc to do work.
 */
function wrapper() { someFunc(); }
"""
        result = self.handler.rename_in_docstrings(source, "oldFunc", "newFunc")
        assert result is not None
        assert "newFunc" in result
        assert "oldFunc" not in result

    def test_no_doc_comments(self):
        source = "function foo() { return 1; }"
        docs = self.handler.find_docstrings(source, (0, len(source.encode())))
        assert docs == []

    def test_noqa_comments(self):
        source = "const x = 1; // noqa: emend:deadcode\n"
        noqa = self.handler.find_noqa_comments(source)
        assert 1 in noqa
        # Tags are stored stripped of the ``emend:`` prefix, mirroring
        # PythonCommentHandler's semantics.
        assert "deadcode" in noqa[1]


class TestDocCommentHandlerRust:
    def setup_method(self):
        self.handler = DocCommentHandler("//", doc_style="line")

    def test_find_doc_comment(self):
        source = """/// Does something useful.
/// Returns a value.
fn do_something() -> i32 { 42 }
"""
        docs = self.handler.find_docstrings(source, (0, len(source.encode())))
        assert len(docs) == 1
        assert "useful" in docs[0][2]

    def test_find_inner_doc_comment(self):
        source = """//! Module documentation.
//! This module provides utilities.

fn helper() {}
"""
        docs = self.handler.find_docstrings(source, (0, len(source.encode())))
        assert len(docs) == 1
        assert "Module documentation" in docs[0][2]

    def test_rename_in_doc_comment(self):
        source = """/// Calls old_func to do work.
fn wrapper() { old_func(); }
"""
        result = self.handler.rename_in_docstrings(source, "old_func", "new_func")
        assert result is not None
        assert "new_func" in result

    def test_noqa_comments(self):
        source = "let x = 1; // noqa: emend:deadcode\n"
        noqa = self.handler.find_noqa_comments(source)
        assert 1 in noqa
        # Tags are stored stripped of the ``emend:`` prefix, mirroring
        # PythonCommentHandler's semantics.
        assert "deadcode" in noqa[1]

    def test_regular_comments_not_docs(self):
        source = """// This is a regular comment
fn foo() {}
"""
        docs = self.handler.find_docstrings(source, (0, len(source.encode())))
        assert docs == []


# ============================================================================
# Source root detection
# ============================================================================

class TestSourceRootDetection:
    def test_rust_src_layout(self, tmp_path):
        from emend.transform import _find_source_root
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "foo"\n')
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("fn main() {}")
        result = _find_source_root(str(tmp_path), language="rust")
        assert result == str(tmp_path / "src")

    def test_typescript_src_layout(self, tmp_path):
        import json
        from emend.transform import _find_source_root
        tsconfig = {"compilerOptions": {"rootDir": "src"}}
        (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("export const x = 1;")
        result = _find_source_root(str(tmp_path), language="typescript")
        assert result == str(tmp_path / "src")

    def test_generic_src_fallback(self, tmp_path):
        from emend.transform import _find_source_root
        (tmp_path / "src").mkdir()
        result = _find_source_root(str(tmp_path), language="go")
        assert result == str(tmp_path / "src")

    def test_no_src_returns_root(self, tmp_path):
        from emend.transform import _find_source_root
        result = _find_source_root(str(tmp_path), language="rust")
        assert result == str(tmp_path)


# ============================================================================
# Pattern compiler
# ============================================================================

class TestPatternCompilerTypescript:
    def setup_method(self):
        self.compiler = TreeSitterPatternCompiler("typescript")

    def test_compile_simple_identifier(self):
        ir = self.compiler.compile("console")
        assert ir is not None

    def test_compile_member_expression(self):
        ir = self.compiler.compile("console.log")
        assert ir is not None


class TestPatternCompilerRust:
    def setup_method(self):
        self.compiler = TreeSitterPatternCompiler("rust")

    def test_compile_simple_identifier(self):
        ir = self.compiler.compile("println")
        assert ir is not None

    def test_compile_function_call(self):
        ir = self.compiler.compile("Vec::new()")
        assert ir is not None
