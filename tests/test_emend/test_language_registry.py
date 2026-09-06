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
from emend.cli_base import resolve_files


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


def test_detect_language_uppercase_extension():
    # Case-insensitive filesystems can yield uppercase suffixes.
    assert detect_language("SCRIPT.PY") == "python"
    assert detect_language("app.TS") == "typescript"
    assert detect_language("Component.TSX") == "typescript"
    assert detect_language("lib.RS") == "rust"
    assert detect_language("Mixed.Py") == "python"


def test_matches_language_uppercase_extension():
    assert matches_language("a.PY", "python") is True
    assert matches_language("a.TS", "typescript") is True


def test_is_source_file_uppercase_extension():
    assert is_source_file("foo.PY") is True
    assert is_source_file("foo.TS") is True
    assert is_source_file("README.TXT") is False


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


@pytest.fixture
def mixed_project(tmp_path):
    for name, content in [
        ("a.py", "x = 1"), ("b.ts", "let x = 1"),
        ("c.tsx", "export default () => <div/>;"),
    ]:
        (tmp_path / name).write_text(content)
    return tmp_path


@pytest.mark.parametrize("target, options, expected, is_multi", [
    ("", {}, {"a.py"}, True),
    ("", {"language": "typescript"}, {"b.ts", "c.tsx"}, True),
    ("a.py", {}, {"a.py"}, False),
    ("*.py", {}, {"a.py"}, True),
    ("*", {"language": "typescript"}, {"b.ts", "c.tsx"}, True),
])
def test_resolve_files(mixed_project, target, options, expected, is_multi):
    files, actual_multi = resolve_files(str(mixed_project / target), **options)
    assert {f.name for f in files} == expected
    assert len(files) == len(expected)
    assert actual_multi is is_multi


@pytest.mark.parametrize("code, expected", [
    ("export function foo(): void {}\nfunction internal(): void {}\n", {"foo"}),
    ("export class Bar {}\nclass NotExported {}\n", {"Bar"}),
    ("export const baz = 42;\nconst hidden = 1;\n", {"baz"}),
    ("export let x = 3;\nlet y = 4;\nexport var z = 5;\nvar w = 6;\n", {"x", "z"}),
    ("export interface IFoo { x: number; }\ninterface IBar {}\n", {"IFoo"}),
    ("export type MyType = string;\ntype Other = number;\n", {"MyType"}),
    ("export enum Color { Red, Green, Blue }\nenum Status { On, Off }\n", {"Color"}),
    ("function foo() {}\nfunction bar() {}\nexport { foo, bar };\n", {"foo", "bar"}),
    # A local export alias retains the original declaration name.
    ("function foo() {}\nexport { foo as f };\n", {"foo"}),
    ("export default class Anonymous {}\n", {"Anonymous"}),
    ("export abstract class AbstractFoo {}\n", {"AbstractFoo"}),
    # Re-exports do not declare names in this module.
    ('export { Foo } from "./other";\n', set()),
])
def test_detect_exported_names_typescript(code, expected):
    assert detect_exported_names(code, "typescript") == expected


def test_detect_exported_names_python_empty():
    assert detect_exported_names("def foo(): pass\n__all__ = ['foo']\n", "python") == set()


@pytest.mark.parametrize("code, expected", [
    ("pub fn exported_fn() {}\nfn private_fn() {}\n", {"exported_fn"}),
    ("pub struct ExportedStruct {}\nstruct PrivateStruct {}\n", {"ExportedStruct"}),
    ("pub(crate) fn crate_fn() {}\nfn private_fn() {}\n", {"crate_fn"}),
])
def test_detect_exported_names_rust(code, expected):
    assert detect_exported_names(code, "rust") == expected
