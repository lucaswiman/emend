"""Tests for language_registry and language-aware resolve_files."""
import pytest

from emend.language_registry import (
    detect_language,
    get_extensions,
    get_all_languages,
    matches_language,
    is_source_file,
    detect_exported_names,
)
from emend.cli import resolve_files


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

def test_detect_language_python():
    assert detect_language("foo.py") == "python"
    assert detect_language("bar.pyi") == "python"


def test_detect_language_typescript():
    assert detect_language("x.ts") == "typescript"
    assert detect_language("x.tsx") == "typescript"
    assert detect_language("x.js") == "typescript"
    assert detect_language("x.jsx") == "typescript"


def test_detect_language_unknown():
    assert detect_language("x.txt") is None
    assert detect_language("x.md") is None
    assert detect_language("x") is None


def test_detect_language_path_object():
    from pathlib import Path
    assert detect_language(Path("foo.py")) == "python"
    assert detect_language(Path("foo.ts")) == "typescript"


# ---------------------------------------------------------------------------
# get_extensions
# ---------------------------------------------------------------------------

def test_get_extensions_python():
    exts = get_extensions("python")
    assert "py" in exts
    assert "pyi" in exts


def test_get_extensions_typescript():
    exts = get_extensions("typescript")
    assert "ts" in exts
    assert "tsx" in exts
    assert "js" in exts
    assert "jsx" in exts


def test_get_extensions_unknown():
    assert get_extensions("cobol") == []


# ---------------------------------------------------------------------------
# get_all_languages / matches_language / is_source_file
# ---------------------------------------------------------------------------

def test_get_all_languages_includes_builtins():
    langs = get_all_languages()
    assert "python" in langs
    assert "typescript" in langs


def test_matches_language():
    assert matches_language("a.py", "python") is True
    assert matches_language("a.ts", "typescript") is True
    assert matches_language("a.py", "typescript") is False
    assert matches_language("a.ts", "python") is False


def test_is_source_file():
    assert is_source_file("foo.py") is True
    assert is_source_file("foo.ts") is True
    assert is_source_file("foo.txt") is False
    assert is_source_file("README.md") is False


# ---------------------------------------------------------------------------
# resolve_files — default (python)
# ---------------------------------------------------------------------------

def test_resolve_files_default_python(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.ts").write_text("let x = 1")
    files, is_multi = resolve_files(str(tmp_path))
    names = {f.name for f in files}
    assert "a.py" in names
    assert "b.ts" not in names
    assert is_multi is True


def test_resolve_files_typescript(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.ts").write_text("let x = 1")
    (tmp_path / "c.tsx").write_text("export default () => <div/>;")
    files, is_multi = resolve_files(str(tmp_path), language="typescript")
    names = {f.name for f in files}
    assert "b.ts" in names
    assert "c.tsx" in names
    assert "a.py" not in names
    assert is_multi is True


def test_resolve_files_single_file(tmp_path):
    p = tmp_path / "foo.py"
    p.write_text("pass")
    files, is_multi = resolve_files(str(p))
    assert len(files) == 1
    assert files[0].name == "foo.py"
    assert is_multi is False


def test_resolve_files_glob_python(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.ts").write_text("let x = 1")
    pattern = str(tmp_path / "*.py")
    files, is_multi = resolve_files(pattern)
    names = {f.name for f in files}
    assert "a.py" in names
    assert "b.ts" not in names
    assert is_multi is True


def test_resolve_files_glob_typescript(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.ts").write_text("let x = 1")
    pattern = str(tmp_path / "*")
    files, is_multi = resolve_files(pattern, language="typescript")
    names = {f.name for f in files}
    assert "b.ts" in names
    assert "a.py" not in names


# ---------------------------------------------------------------------------
# detect_exported_names — TypeScript
# ---------------------------------------------------------------------------

def test_detect_exported_names_ts_function():
    code = "export function foo(): void {}\nfunction internal(): void {}\n"
    result = detect_exported_names(code, "typescript")
    assert "foo" in result
    assert "internal" not in result


def test_detect_exported_names_ts_class():
    code = "export class Bar {}\nclass NotExported {}\n"
    result = detect_exported_names(code, "typescript")
    assert "Bar" in result
    assert "NotExported" not in result


def test_detect_exported_names_ts_const():
    code = "export const baz = 42;\nconst hidden = 1;\n"
    result = detect_exported_names(code, "typescript")
    assert "baz" in result
    assert "hidden" not in result


def test_detect_exported_names_ts_let_var():
    code = "export let x = 3;\nlet y = 4;\nexport var z = 5;\nvar w = 6;\n"
    result = detect_exported_names(code, "typescript")
    assert "x" in result
    assert "z" in result
    assert "y" not in result
    assert "w" not in result


def test_detect_exported_names_ts_interface():
    code = "export interface IFoo { x: number; }\ninterface IBar {}\n"
    result = detect_exported_names(code, "typescript")
    assert "IFoo" in result
    assert "IBar" not in result


def test_detect_exported_names_ts_type():
    code = "export type MyType = string;\ntype Other = number;\n"
    result = detect_exported_names(code, "typescript")
    assert "MyType" in result
    assert "Other" not in result


def test_detect_exported_names_ts_enum():
    code = "export enum Color { Red, Green, Blue }\nenum Status { On, Off }\n"
    result = detect_exported_names(code, "typescript")
    assert "Color" in result
    assert "Status" not in result


def test_detect_exported_names_ts_named_block():
    code = "function foo() {}\nfunction bar() {}\nexport { foo, bar };\n"
    result = detect_exported_names(code, "typescript")
    assert "foo" in result
    assert "bar" in result


def test_detect_exported_names_ts_named_block_with_alias():
    code = "function foo() {}\nexport { foo as f };\n"
    result = detect_exported_names(code, "typescript")
    # Original name (before 'as') should be in the result
    assert "foo" in result
    assert "f" not in result


def test_detect_exported_names_ts_default_class():
    code = "export default class Anonymous {}\n"
    result = detect_exported_names(code, "typescript")
    assert "Anonymous" in result


def test_detect_exported_names_ts_abstract_class():
    code = "export abstract class AbstractFoo {}\n"
    result = detect_exported_names(code, "typescript")
    assert "AbstractFoo" in result


def test_detect_exported_names_ts_reexport_not_included():
    # Re-exports: `export { X } from "module"` — X is from another module,
    # not defined here, so it should NOT be in the result.
    code = 'export { Foo } from "./other";\n'
    result = detect_exported_names(code, "typescript")
    assert "Foo" not in result


def test_detect_exported_names_ts_python_empty():
    # Python always returns empty set
    py_code = "def foo(): pass\n__all__ = ['foo']\n"
    assert detect_exported_names(py_code, "python") == set()


# ---------------------------------------------------------------------------
# detect_exported_names — Rust
# ---------------------------------------------------------------------------

def test_detect_exported_names_rust_pub_fn():
    code = "pub fn exported_fn() {}\nfn private_fn() {}\n"
    result = detect_exported_names(code, "rust")
    assert "exported_fn" in result
    assert "private_fn" not in result


def test_detect_exported_names_rust_pub_struct():
    code = "pub struct ExportedStruct {}\nstruct PrivateStruct {}\n"
    result = detect_exported_names(code, "rust")
    assert "ExportedStruct" in result
    assert "PrivateStruct" not in result


def test_detect_exported_names_rust_pub_crate():
    code = "pub(crate) fn crate_fn() {}\nfn private_fn() {}\n"
    result = detect_exported_names(code, "rust")
    assert "crate_fn" in result
    assert "private_fn" not in result
