"""Cross-language rename uses one configured native module identity."""

import pytest

from emend.component_selector import ExtendedSelector
from emend.transform import rename_symbol, warm_caches


def test_warm_qn_cache_tracks_typescript_root_change(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    target = web / "main.ts"
    caller = web / "app.ts"
    target.write_text("export function greet() {}\n")
    caller.write_text("import { greet } from './main';\ngreet();\n")
    config = tmp_path / "tsconfig.json"
    config.write_text('{"compilerOptions":{"rootDir":"web"}}')
    warm_caches(str(tmp_path), type_engine="none")
    config.write_text('{"compilerOptions":{"rootDir":"."}}')
    rename_symbol(
        ExtendedSelector(str(target), ["greet"]), "welcome",
        project_path=str(tmp_path), apply=True,
    )
    assert "function welcome" in target.read_text()
    assert "welcome();" in caller.read_text()


def test_rust_library_and_binary_roots_are_not_conflated(tmp_path):
    from emend.project_config import module_name_for_file, module_resolution_context

    source = tmp_path / "src"
    source.mkdir()
    (tmp_path / "Cargo.toml").write_text("[package]\nname='both'\nversion='0.1.0'\n")
    library = source / "lib.rs"
    binary = source / "main.rs"
    library.write_text("pub fn greet() {}\n")
    binary.write_text("fn greet() {}\n")
    assert module_resolution_context(tmp_path, "rust")[1] is None
    assert module_name_for_file(library, tmp_path, language="rust") == "lib"
    assert module_name_for_file(binary, tmp_path, language="rust") == "main"

    library.write_text("pub fn greet() {}\npub fn run() { crate::greet(); }\n")
    binary.write_text("fn greet() {}\nfn run() { crate::greet(); }\n")
    rename_symbol(
        ExtendedSelector(str(library), ["greet"]), "welcome",
        project_path=str(tmp_path), apply=True,
    )
    assert "crate::welcome()" in library.read_text()
    assert "crate::greet()" in binary.read_text()


def test_rust_explicit_bin_root_renames_same_file_crate_call(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    tool = code / "tool.rs"
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname='bins'\nversion='0.1.0'\n[[bin]]\nname='tool'\npath='code/tool.rs'\n"
    )
    tool.write_text("fn greet() {}\nfn run() { crate::greet(); }\n")
    rename_symbol(
        ExtendedSelector(str(tool), ["greet"]), "welcome",
        project_path=str(tmp_path), apply=True,
    )
    assert tool.read_text() == "fn welcome() {}\nfn run() { crate::welcome(); }\n"


def test_rust_custom_library_and_main_roots_rename_independently(tmp_path):
    code = tmp_path / "code"
    source = tmp_path / "src"
    code.mkdir()
    source.mkdir()
    library = code / "core.rs"
    binary = source / "main.rs"
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname='both'\nversion='0.1.0'\n[lib]\npath='code/core.rs'\n"
    )
    library.write_text("pub fn greet() {}\npub fn run() { crate::greet(); }\n")
    binary.write_text("fn greet() {}\nfn run() { crate::greet(); }\n")
    rename_symbol(ExtendedSelector(str(library), ["greet"]), "welcome", project_path=str(tmp_path), apply=True)
    assert "crate::welcome()" in library.read_text()
    assert "crate::greet()" in binary.read_text()
    rename_symbol(ExtendedSelector(str(binary), ["greet"]), "salute", project_path=str(tmp_path), apply=True)
    assert "crate::salute()" in binary.read_text()
    assert "crate::welcome()" in library.read_text()


def test_typescript_caller_outside_root_dir_uses_project_fallback(tmp_path):
    web = tmp_path / "web"
    scripts = tmp_path / "scripts"
    web.mkdir()
    scripts.mkdir()
    target = web / "target.ts"
    caller = scripts / "app.ts"
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"web"}}')
    target.write_text("export function greet() {}\n")
    caller.write_text("import { greet } from '../web/target';\ngreet();\n")
    rename_symbol(ExtendedSelector(str(target), ["greet"]), "welcome", project_path=str(tmp_path), apply=True)
    assert "function welcome" in target.read_text()
    assert caller.read_text() == "import { welcome } from '../web/target';\nwelcome();\n"


def test_rust_inline_module_self_reference_uses_owner_qn(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    root = source / "lib.rs"
    (tmp_path / "Cargo.toml").write_text("[package]\nname='inline'\nversion='0.1.0'\n")
    root.write_text(
        "fn root() {}\nmod nested {\n"
        "    fn greet() {}\n    fn run() { self::greet(); super::root(); }\n}\n"
        "fn outer() { crate::nested::greet(); }\n"
    )
    rename_symbol(
        ExtendedSelector(str(root), ["nested", "greet"]), "welcome",
        project_path=str(tmp_path), apply=True,
    )
    text = root.read_text()
    assert "fn welcome()" in text
    assert "self::welcome()" in text
    assert "crate::nested::welcome()" in text
    assert "super::root()" in text


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_typescript_rename_relative_import_under_configured_root(tmp_path, warm):
    project = tmp_path / "project"
    root = project / "web"
    target = root / "pkg" / "foo.bar.ts"
    caller = root / "features" / "app.ts"
    dotted_collision = root / "pkg" / "foo" / "bar.ts"
    unrelated = root / "other.ts"
    for path in (target, caller, dotted_collision, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
    (project / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"web"}}\n')
    target.write_text("export function greet() { return 1; }\n")
    caller.write_text("import { greet } from '../pkg/foo.bar';\nconst x = greet();\n")
    original = "function greet() { return 2; }\nconst x = greet();\n"
    unrelated.write_text(original)
    dotted_collision.write_text(original)
    if warm:
        warm_caches(str(project), type_engine="none")

    diffs = rename_symbol(
        ExtendedSelector(str(target), ["greet"]), "welcome",
        project_path=str(project), apply=True,
    )

    assert set(diffs) == {str(target), str(caller)}
    assert "function welcome" in target.read_text()
    assert caller.read_text() == (
        "import { welcome } from '../pkg/foo.bar';\nconst x = welcome();\n"
    )
    assert unrelated.read_text() == original
    assert dotted_collision.read_text() == original


@pytest.mark.parametrize(
    ("consumer_source", "expected"),
    [
        ("import { greet as hello } from './target';\nhello();\n",
         "import { welcome as hello } from './target';\nhello();\n"),
        ("import * as ns from './target';\nns.greet();\n",
         "import * as ns from './target';\nns.welcome();\n"),
        ("export { greet } from './target';\n",
         "export { welcome as greet } from './target';\n"),
        ("export { greet as hello } from './target';\n",
         "export { welcome as hello } from './target';\n"),
    ],
)
def test_typescript_rename_updates_import_forms(tmp_path, consumer_source, expected):
    target = tmp_path / "target.ts"
    consumer = tmp_path / "consumer.ts"
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"."}}')
    target.write_text("export function greet() {}\n")
    consumer.write_text(consumer_source)
    rename_symbol(
        ExtendedSelector(str(target), ["greet"]), "welcome",
        project_path=str(tmp_path), apply=True,
    )
    assert "function welcome" in target.read_text()
    assert consumer.read_text() == expected


def test_typescript_unaliased_reexport_preserves_downstream_api(tmp_path):
    target = tmp_path / "target.ts"
    barrel = tmp_path / "barrel.ts"
    consumer = tmp_path / "consumer.ts"
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"."}}')
    target.write_text("export function greet() {}\n")
    barrel.write_text("export { greet } from './target';\n")
    consumer.write_text("import { greet } from './barrel';\ngreet();\n")
    rename_symbol(
        ExtendedSelector(str(target), ["greet"]), "welcome",
        project_path=str(tmp_path), apply=True,
    )
    assert barrel.read_text() == "export { welcome as greet } from './target';\n"
    assert consumer.read_text() == "import { greet } from './barrel';\ngreet();\n"


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_typescript_wildcard_reexport_fails_before_writes(tmp_path, warm):
    target = tmp_path / "target.ts"
    barrel = tmp_path / "barrel.ts"
    consumer = tmp_path / "consumer.ts"
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"."}}')
    target_source = "export function greet() {}\n"
    target.write_text(target_source)
    barrel.write_text("export * from './target';\n")
    consumer.write_text("import { greet } from './barrel';\ngreet();\n")
    if warm:
        warm_caches(str(tmp_path), type_engine="none")
    with pytest.raises(ValueError, match="wildcard re-export"):
        rename_symbol(ExtendedSelector(str(target), ["greet"]), "welcome", project_path=str(tmp_path), apply=True)
    assert target.read_text() == target_source
    assert barrel.read_text() == "export * from './target';\n"
    assert consumer.read_text() == "import { greet } from './barrel';\ngreet();\n"


def test_typescript_alias_resolution_uses_enclosing_import(tmp_path):
    target = tmp_path / "target.ts"
    other = tmp_path / "other.ts"
    consumer = tmp_path / "consumer.ts"
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"."}}')
    target.write_text("export function greet() {}\n")
    other.write_text("export function greet() {}\n")
    consumer.write_text(
        "import { greet as first } from './other';\n"
        "import { greet as second } from './target';\nfirst(); second();\n"
    )
    rename_symbol(
        ExtendedSelector(str(target), ["greet"]), "welcome",
        project_path=str(tmp_path), apply=True,
    )
    assert consumer.read_text() == (
        "import { greet as first } from './other';\n"
        "import { welcome as second } from './target';\nfirst(); second();\n"
    )
    assert "function greet" in other.read_text()


@pytest.mark.parametrize(
    ("specifier", "target_relative"),
    [("./target", "target.ts"), ("./target.js", "target.ts"),
     ("./target.ts", "target.ts"), ("./pkg", "pkg/index.ts"),
     ("./index", "index.ts")],
)
@pytest.mark.parametrize("declaration", ["export function greet() {}", "export const greet = () => 1;"])
def test_typescript_relative_module_projection_matches_target(tmp_path, specifier, target_relative, declaration):
    target = tmp_path / target_relative
    target.parent.mkdir(parents=True, exist_ok=True)
    consumer = tmp_path / "consumer.ts"
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"."}}')
    target.write_text(declaration + "\n")
    consumer.write_text(f"import {{ greet }} from '{specifier}';\ngreet();\n")
    rename_symbol(ExtendedSelector(str(target), ["greet"]), "welcome", project_path=str(tmp_path), apply=True)
    assert "welcome" in target.read_text() and "greet" not in target.read_text()
    assert "welcome();" in consumer.read_text()


def test_typescript_bare_package_does_not_alias_local_file(tmp_path):
    target = tmp_path / "react.ts"
    consumer = tmp_path / "consumer.ts"
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"rootDir":"."}}')
    target.write_text("export function greet() {}\n")
    original = "import { greet } from 'react';\ngreet();\n"
    consumer.write_text(original)
    rename_symbol(ExtendedSelector(str(target), ["greet"]), "welcome", project_path=str(tmp_path), apply=True)
    assert "function welcome" in target.read_text()
    assert consumer.read_text() == original


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("target_kind", ["crate_root", "nested"])
def test_rust_rename_crate_identity_under_custom_lib_path(tmp_path, warm, target_kind):
    project = tmp_path / "project"
    code = project / "code"
    code.mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        "[package]\nname='rename-test'\nversion='0.1.0'\n[lib]\npath='code/core.rs'\n"
    )
    root = code / "core.rs"
    nested = code / "nested.rs"
    caller = code / "consumer.rs"
    unrelated = code / "other.rs"
    root.write_text("pub fn greet() {}\npub mod nested;\npub mod consumer;\n")
    nested.write_text("pub fn greet() {}\n")
    target = root if target_kind == "crate_root" else nested
    imported = "crate::greet" if target_kind == "crate_root" else "crate::nested::greet"
    caller.write_text(f"use {imported};\npub fn run() {{ greet(); }}\n")
    original = "fn greet() {}\nfn run() { greet(); }\n"
    unrelated.write_text(original)
    if warm:
        warm_caches(str(project), type_engine="none")

    diffs = rename_symbol(
        ExtendedSelector(str(target), ["greet"]), "welcome",
        project_path=str(project), apply=True,
    )

    assert set(diffs) == {str(target), str(caller)}
    assert "fn welcome" in target.read_text()
    assert "use " + imported.replace("greet", "welcome") in caller.read_text()
    assert "welcome();" in caller.read_text()
    assert unrelated.read_text() == original
