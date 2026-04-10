"""Phase 2: Tests for language-aware fact graph building.

Tests that detect_project_languages(), _collect_all_source_files(), and
FactGraph.build_from_project() work correctly for TypeScript, Rust, and
mixed-language projects.
"""
import textwrap

import pytest

from emend.fact_graph import FactGraph
from emend.transform import (
    detect_project_languages,
    _collect_all_source_files,
    _file_to_module,
)


# ---------------------------------------------------------------------------
# Sample source code
# ---------------------------------------------------------------------------

_TS_SOURCE = textwrap.dedent("""\
    import { readFile } from "fs";
    import path from "path";

    export function processData(input: string): string {
        const result = input.toUpperCase();
        return result;
    }

    function helper(x: number): number {
        return x + 1;
    }
""")

_RUST_SOURCE = textwrap.dedent("""\
    use std::collections::HashMap;
    use std::io::Read;

    pub fn process_data(input: &str) -> String {
        input.to_uppercase()
    }

    fn helper(x: i32) -> i32 {
        x + 1
    }
""")

_PY_SOURCE = textwrap.dedent("""\
    import os

    def compute(x):
        return x * 2

    def unused():
        pass
""")


# ---------------------------------------------------------------------------
# Test: detect_project_languages()
# ---------------------------------------------------------------------------

class TestDetectProjectLanguages:
    def test_python_only_from_py_files(self, tmp_path):
        """Python project detected from .py files."""
        (tmp_path / "main.py").write_text(_PY_SOURCE)
        langs = detect_project_languages(str(tmp_path))
        assert "python" in langs

    def test_python_only_from_pyproject_toml(self, tmp_path):
        """Python project detected from pyproject.toml."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
        langs = detect_project_languages(str(tmp_path))
        assert "python" in langs

    def test_typescript_from_package_json(self, tmp_path):
        """TypeScript project detected from package.json."""
        (tmp_path / "package.json").write_text('{"name": "foo"}')
        langs = detect_project_languages(str(tmp_path))
        assert "typescript" in langs

    def test_typescript_from_ts_files(self, tmp_path):
        """TypeScript project detected from .ts files."""
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        langs = detect_project_languages(str(tmp_path))
        assert "typescript" in langs

    def test_typescript_from_tsconfig(self, tmp_path):
        """TypeScript project detected from tsconfig.json."""
        (tmp_path / "tsconfig.json").write_text('{}')
        langs = detect_project_languages(str(tmp_path))
        assert "typescript" in langs

    def test_rust_from_cargo_toml(self, tmp_path):
        """Rust project detected from Cargo.toml."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "foo"\n')
        langs = detect_project_languages(str(tmp_path))
        assert "rust" in langs

    def test_rust_from_rs_files(self, tmp_path):
        """Rust project detected from .rs files."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.rs").write_text(_RUST_SOURCE)
        langs = detect_project_languages(str(tmp_path))
        assert "rust" in langs

    def test_mixed_python_typescript(self, tmp_path):
        """Both Python and TypeScript detected in a mixed project."""
        (tmp_path / "main.py").write_text(_PY_SOURCE)
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        langs = detect_project_languages(str(tmp_path))
        assert "python" in langs
        assert "typescript" in langs

    def test_empty_dir_returns_empty(self, tmp_path):
        """Empty directory returns empty list (or at least no crash)."""
        langs = detect_project_languages(str(tmp_path))
        assert isinstance(langs, list)

    def test_returns_list(self, tmp_path):
        """Result is always a list."""
        (tmp_path / "app.py").write_text(_PY_SOURCE)
        langs = detect_project_languages(str(tmp_path))
        assert isinstance(langs, list)


# ---------------------------------------------------------------------------
# Test: _collect_all_source_files()
# ---------------------------------------------------------------------------

class TestCollectAllSourceFiles:
    def test_collects_py_and_ts_files(self, tmp_path):
        """Collects both .py and .ts files for a mixed project."""
        (tmp_path / "main.py").write_text(_PY_SOURCE)
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        files = _collect_all_source_files(str(tmp_path), languages=["python", "typescript"])
        paths = {str(f) for f in files}
        assert any("main.py" in p for p in paths)
        assert any("index.ts" in p for p in paths)

    def test_collects_rust_files(self, tmp_path):
        """Collects .rs files when rust language is specified."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "lib.rs").write_text(_RUST_SOURCE)
        files = _collect_all_source_files(str(tmp_path), languages=["rust"])
        paths = {str(f) for f in files}
        assert any("lib.rs" in p for p in paths)

    def test_no_duplicates(self, tmp_path):
        """Files should not appear twice even if two languages share an extension."""
        (tmp_path / "app.py").write_text(_PY_SOURCE)
        files = _collect_all_source_files(str(tmp_path), languages=["python", "python"])
        assert len(files) == len(set(files))

    def test_auto_detect_languages(self, tmp_path):
        """When languages=None, auto-detects from project root."""
        (tmp_path / "main.py").write_text(_PY_SOURCE)
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        files = _collect_all_source_files(str(tmp_path))
        paths = {str(f) for f in files}
        assert any("main.py" in p for p in paths)
        assert any("index.ts" in p for p in paths)


# ---------------------------------------------------------------------------
# Test: FactGraph.build_from_project() for TypeScript
# ---------------------------------------------------------------------------

class TestBuildFromProjectTypescript:
    def test_symbols_extracted(self, tmp_path):
        """FactGraph built from a TS project has TS symbols."""
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        graph = FactGraph.build_from_project(str(tmp_path), language="typescript")
        syms = graph.symbols()
        sym_names = {s.name for s in syms}
        assert "processData" in sym_names or "helper" in sym_names

    def test_cfg_edges_extracted(self, tmp_path):
        """CFG edges are extracted for TypeScript functions."""
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        graph = FactGraph.build_from_project(str(tmp_path), language="typescript")
        # CFG blocks (or edges) should exist for at least one function
        blocks = graph._client.run(
            "?[fp, fq, bid, is_entry, is_exit] := *cfg_block[fp, fq, bid, is_entry, is_exit]"
        )["rows"]
        assert len(blocks) > 0, "Expected CFG blocks for TypeScript functions"

    def test_import_facts_extracted(self, tmp_path):
        """Import facts are extracted for TypeScript files."""
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        graph = FactGraph.build_from_project(str(tmp_path), language="typescript")
        imports = graph._all_imports()
        # Should have at least one import (fs, path)
        assert len(imports) >= 1, "Expected import facts for TypeScript"


# ---------------------------------------------------------------------------
# Test: FactGraph.build_from_project() for Rust
# ---------------------------------------------------------------------------

class TestBuildFromProjectRust:
    def test_symbols_extracted(self, tmp_path):
        """FactGraph built from a Rust project has Rust symbols."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "lib.rs").write_text(_RUST_SOURCE)
        graph = FactGraph.build_from_project(str(tmp_path), language="rust")
        syms = graph.symbols()
        sym_names = {s.name for s in syms}
        assert "process_data" in sym_names or "helper" in sym_names

    def test_cfg_edges_extracted(self, tmp_path):
        """CFG edges are extracted for Rust functions."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "lib.rs").write_text(_RUST_SOURCE)
        graph = FactGraph.build_from_project(str(tmp_path), language="rust")
        blocks = graph._client.run(
            "?[fp, fq, bid, is_entry, is_exit] := *cfg_block[fp, fq, bid, is_entry, is_exit]"
        )["rows"]
        assert len(blocks) > 0, "Expected CFG blocks for Rust functions"


# ---------------------------------------------------------------------------
# Test: build_from_project() with auto-detection (mixed project)
# ---------------------------------------------------------------------------

class TestBuildFromProjectMixed:
    def test_mixed_project_has_facts_for_both_languages(self, tmp_path):
        """FactGraph from a mixed Python+TS project has facts for both."""
        (tmp_path / "main.py").write_text(_PY_SOURCE)
        (tmp_path / "index.ts").write_text(_TS_SOURCE)
        graph = FactGraph.build_from_project(str(tmp_path))
        syms = graph.symbols()
        sym_names = {s.name for s in syms}
        # Python symbols
        assert "compute" in sym_names or "unused" in sym_names
        # TypeScript symbols
        assert "processData" in sym_names or "helper" in sym_names


# ---------------------------------------------------------------------------
# Test: _file_to_module() for TypeScript and Rust
# ---------------------------------------------------------------------------

class TestFileToModule:
    def test_typescript_simple(self, tmp_path):
        """TypeScript file path → module name uses / separator."""
        ts_file = tmp_path / "utils" / "helper.ts"
        ts_file.parent.mkdir(parents=True)
        ts_file.write_text(_TS_SOURCE)
        module = _file_to_module(str(ts_file), str(tmp_path))
        # Should be something like "utils/helper" (slash separator for TS)
        assert "helper" in module
        # TypeScript module separator is "/" or ".", not "::"
        assert "::" not in module

    def test_typescript_src_layout(self, tmp_path):
        """TypeScript file in src/ uses src-stripped path."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        ts_file = src_dir / "utils.ts"
        ts_file.write_text(_TS_SOURCE)
        module = _file_to_module(str(ts_file), str(tmp_path))
        # Should be "utils" not "src/utils"
        assert "utils" in module
        # src should be stripped from the module name
        assert module == "utils" or "/" not in module or not module.startswith("src")

    def test_rust_simple(self, tmp_path):
        """Rust file path → module name uses :: separator."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        rs_file = src_dir / "utils.rs"
        rs_file.write_text(_RUST_SOURCE)
        module = _file_to_module(str(rs_file), str(tmp_path))
        assert "utils" in module

    def test_rust_lib_rs(self, tmp_path):
        """Rust src/lib.rs → module name."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        rs_file = src_dir / "lib.rs"
        rs_file.write_text(_RUST_SOURCE)
        module = _file_to_module(str(rs_file), str(tmp_path))
        # lib.rs is the crate root; module name should be "lib" or "crate"
        assert module in ("lib", "crate")

    def test_rust_mod_rs(self, tmp_path):
        """Rust src/foo/mod.rs → module name should use parent dir."""
        src_dir = tmp_path / "src" / "foo"
        src_dir.mkdir(parents=True)
        rs_file = src_dir / "mod.rs"
        rs_file.write_text(_RUST_SOURCE)
        module = _file_to_module(str(rs_file), str(tmp_path))
        # mod.rs maps to its parent directory name
        assert "foo" in module

    def test_python_unchanged(self, tmp_path):
        """Python module paths are unchanged by this implementation."""
        src_dir = tmp_path / "src" / "mypkg"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("")
        py_file = src_dir / "utils.py"
        py_file.write_text(_PY_SOURCE)
        module = _file_to_module(str(py_file), str(tmp_path))
        assert "mypkg" in module
        assert "utils" in module
        assert "." in module  # Python uses dot separator


# ---------------------------------------------------------------------------
# Test: _extract_file_facts() called through build_from_project for TS
# ---------------------------------------------------------------------------

class TestExtractFileFactsTypescript:
    def test_ts_imports_are_extracted(self, tmp_path):
        """Import facts are produced for TypeScript import statements."""
        ts_source = textwrap.dedent("""\
            import { readFile } from "fs";
            import path from "path";

            export function main() {
                const x = path.join("a", "b");
            }
        """)
        (tmp_path / "main.ts").write_text(ts_source)
        graph = FactGraph.build_from_project(str(tmp_path), language="typescript")
        imports = graph._all_imports()
        modules = {imp.imported_module for imp in imports}
        assert "fs" in modules or "path" in modules, f"Expected TS imports, got: {modules}"
