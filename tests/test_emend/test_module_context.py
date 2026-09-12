"""Freshness and isolation tests for canonical module-resolution context."""

import json

import pytest

from emend import project_config
from emend.project_config import find_source_root, module_resolution_context


@pytest.fixture(autouse=True)
def clear_context_cache():
    find_source_root.cache_clear()
    yield
    find_source_root.cache_clear()


def test_inherited_tsconfig_change_refreshes_source_root(tmp_path):
    (tmp_path / "tsconfig.json").write_text(json.dumps({"extends": "./base.json"}))
    base = tmp_path / "base.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert find_source_root(str(tmp_path), "typescript") == tmp_path
    base.write_text(json.dumps({"compilerOptions": {"rootDir": "first"}}))
    assert find_source_root(str(tmp_path), "typescript") == first
    base.write_text(json.dumps({"compilerOptions": {"rootDir": "second"}}))
    assert find_source_root(str(tmp_path), "typescript") == second


def test_setup_cfg_change_refreshes_source_root(tmp_path):
    first = tmp_path / "python_one"
    second = tmp_path / "python_two"
    setup_cfg = tmp_path / "setup.cfg"
    setup_cfg.write_text("[options]\npackage_dir =\n    = python_one\n")

    assert find_source_root(str(tmp_path)) == tmp_path
    first.mkdir()
    assert find_source_root(str(tmp_path)) == first
    second.mkdir()
    setup_cfg.write_text("[options]\npackage_dir =\n    = python_two\n")
    assert find_source_root(str(tmp_path)) == second


def test_conventional_python_package_addition_refreshes_source_root(tmp_path):
    package = tmp_path / "src" / "package"
    package.mkdir(parents=True)

    assert find_source_root(str(tmp_path)) == tmp_path
    (package / "__init__.py").write_text("")
    assert find_source_root(str(tmp_path)) == tmp_path / "src"


def test_refresh_is_isolated_to_changed_project(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "src").mkdir(parents=True)
        (root / "Cargo.toml").write_text("[package]\nname = 'demo'\n")

    original = project_config._build_module_context
    rebuilt = []

    def record_build(root, language):
        rebuilt.append(root)
        return original(root, language)

    monkeypatch.setattr(project_config, "_build_module_context", record_build)
    module_resolution_context(first, "rust")
    module_resolution_context(second, "rust")
    rebuilt.clear()
    (first / "src" / "lib.rs").write_text("")

    module_resolution_context(first, "rust")
    module_resolution_context(second, "rust")
    assert rebuilt == [first]


def test_conventional_cargo_roots_refresh_on_add_and_delete(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")
    lib = src / "lib.rs"
    main = src / "main.rs"

    assert module_resolution_context(tmp_path, "rust")[1] is None
    lib.write_text("")
    assert module_resolution_context(tmp_path, "rust")[1] == lib
    main.write_text("")
    assert module_resolution_context(tmp_path, "rust")[1] is None
    lib.unlink()
    assert module_resolution_context(tmp_path, "rust")[1] == main


def test_cached_rust_context_does_not_reparse_cargo(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    cargo = tmp_path / "Cargo.toml"
    cargo.write_text("[lib]\npath = 'src/lib.rs'\n")
    calls = []
    original = project_config._load_toml

    def count_load(path):
        if path == cargo:
            calls.append(path)
        return original(path)

    monkeypatch.setattr(project_config, "_load_toml", count_load)
    for _ in range(3):
        module_resolution_context(tmp_path, "rust")
    assert calls == [cargo]
