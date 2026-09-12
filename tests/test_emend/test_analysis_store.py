"""Contracts for the project-scoped analysis snapshot owner."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from emend.analysis_store import AnalysisStore
from emend.type_oracle import (
    FileTypes,
    TypeBinding,
    TypeDescriptor,
    TypeOracle,
    _FileTypeCache,
)


def _symbol_paths(db_path: Path, name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT file_path FROM symbol_index WHERE name = ?", (name,)
            )
        }


def _names(facts):
    return [fact.name for fact in facts]


@pytest.mark.parametrize("fail_save", [False, True])
def test_cold_facts_build_in_memory_then_persist(tmp_path, monkeypatch, fail_save):
    from emend import emend_core
    from emend.fact_graph import FactGraph

    source = tmp_path / "example.py"
    source.write_text("def original():\n    return 1\n")
    store = AnalysisStore(tmp_path)
    builds, saves = [], []
    mutate, backup = FactGraph._run_mutations, emend_core.PyCozoDb.backup

    def tracked_mutate(graph, operations):
        builds.append(graph._db_path)
        return mutate(graph, operations)

    def tracked_backup(client, path):
        saves.append(path)
        if fail_save:
            raise RuntimeError("save failed")
        return backup(client, path)

    monkeypatch.setattr(FactGraph, "_run_mutations", tracked_mutate)
    monkeypatch.setattr(emend_core.PyCozoDb, "backup", tracked_backup)
    if fail_save:
        with pytest.raises(RuntimeError, match="save failed"):
            store.query_facts()
        assert not store.facts_path.exists()
        assert store._disk_graph is None
    else:
        first = store.query_facts()
        assert builds and all(path is None for path in builds)
        assert _names(AnalysisStore(tmp_path).query_facts().symbols()) == ["original"]
        builds.clear()
        source.write_text("def changed():\n    return 2\n")
        assert _names(store.query_facts().symbols()) == ["changed"]
        assert builds and all(path is not None for path in builds)
        assert _names(first.symbols()) == ["original"]
    assert len(saves) == 1


@pytest.fixture
def extracted_files(monkeypatch):
    import emend.analysis_extraction as extraction

    files = []
    original = extraction._extract_file_facts

    def tracked(*args, **kwargs):
        files.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(extraction, "_extract_file_facts", tracked)
    return files


def _linked_worktrees(tmp_path):
    main, linked = tmp_path / "main", tmp_path / "linked"
    worktree_git = main / ".git" / "worktrees" / "linked"
    worktree_git.mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (worktree_git / "commondir").write_text("../..\n")
    linked.mkdir()
    (linked / ".git").write_text(
        f"gitdir: {os.path.relpath(worktree_git, linked)}\n"
    )
    return main, linked


@pytest.mark.parametrize("persistent", [True, False])
def test_symbol_projection_reuses_revisions_without_sharing_mutable_views(tmp_path, monkeypatch, persistent):
    from emend import emend_core
    from emend.ast_utils import find_nested_definitions
    from emend.query import _collect_symbols

    main, linked = _linked_worktrees(tmp_path)
    if not persistent:
        def unavailable(self):
            raise PermissionError("read-only source tree")
        monkeypatch.setattr(AnalysisStore, "artifact_connection", unavailable)
    source = "@decorate\ndef original(x: int):\n    return x\n"
    native = emend_core.collect_symbols_from_str
    calls = []

    def tracked(text, **kwargs):
        calls.append(text)
        return native(text, **kwargs)

    monkeypatch.setattr(emend_core, "collect_symbols_from_str", tracked)
    for root in (main, linked, main):
        path = root / "sample.py"
        symbols = _collect_symbols(path, source)
        assert [(s.path, s.decorators, s.parameters) for s in symbols] == [
            (f"{path}::original", ["@decorate"], ["x: int"]),
        ]
        symbols[0].decorators.clear()
        symbols[0].parameters.clear()
        nested = find_nested_definitions(str(path), source_override=source)
        assert [(s.name, s.parameters) for s in nested] == [("original", ["x"])]
        nested[0].parameters.clear()
        assert _collect_symbols(path, source.replace("original", "edited"))[0].name == "edited"
        AnalysisStore.open(root).close()
    assert calls == [source, source.replace("original", "edited")] * (1 if persistent else 3)


class _FakeTypeOracle(TypeOracle):
    calls = 0

    def __init__(self, root):
        from emend.analysis_store import AnalysisStore

        self._cache = _FileTypeCache(
            db_path=str(AnalysisStore.open(root).artifact_path),
            namespace="fake-context|fake-engine",
        )

    def infer_file(self, path, project_root=None):
        key = self._file_key(path, project_root)
        if (cached := self._cache.get(key, path)) is not None:
            return cached
        type(self).calls += 1
        result = FileTypes(path=str(path.resolve()))
        self._cache.put(key, result)
        return result

    def clear_cache(self):
        self._cache.clear()

    def is_available(self):
        return True


class _TypedOracle(TypeOracle):
    _uses_overlay_source = True
    calls = 0

    def infer_file(self, path, project_root=None):
        type(self).calls += 1
        result = FileTypes(path=str(path), bindings=[TypeBinding(
            name="value", line=1, col_start=1, col_end=6,
            type_descriptor=TypeDescriptor(kind="named", name="int"),
            raw_type="int", binding_kind="inferred",
        )])
        result.build_index()
        return result

    def clear_cache(self):
        pass

    def is_available(self):
        return True


def test_identical_content_files_keep_distinct_rows_and_survive_removal(tmp_path):
    from emend.transform import _ensure_index_fresh, warm_caches

    source = "def shared():\n    return 1\n"
    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_text(source)
    warm_caches(str(tmp_path), type_engine=None)
    second.write_text(source)
    warm_caches(str(tmp_path), type_engine=None)

    db_path = tmp_path / ".emend" / "cache" / "parse.db"
    assert _symbol_paths(db_path, "shared") == {str(first), str(second)}
    store = AnalysisStore.open(tmp_path)
    assert {fact.file_path for fact in store.query_facts().symbols(name="shared")} == {
        "first.py", "second.py",
    }

    first.unlink()
    assert _ensure_index_fresh(str(tmp_path))
    assert _symbol_paths(db_path, "shared") == {str(second)}
    assert {fact.file_path for fact in store.query_facts().symbols(name="shared")} == {
        "second.py",
    }


@pytest.mark.parametrize(
    ("imports", "forbidden"),
    [
        (("emend.analysis_snapshot", "emend.analysis_store"),
         ("emend.fact_graph", "emend.transform")),
        (("emend.analysis_extraction",),
         ("emend.fact_graph", "emend.transform", "emend.transform.cache",
          "emend.transform.index")),
    ],
    ids=("model-and-owner", "extraction"),
)
def test_analysis_layers_keep_query_imports_out_of_lower_layers(imports, forbidden):
    code = "import sys; " + "; ".join(f"import {module}" for module in imports)
    code += "; " + "; ".join(
        f"assert {module!r} not in sys.modules" for module in forbidden
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def _module_imports(module_object):
    from emend import emend_core

    module_path = Path(module_object.__file__).resolve()
    resolver = emend_core.PyScopeResolver(str(module_path.parent))
    resolver.index_file(str(module_path), module_path.read_text())
    return {
        module
        for _local, module, _name, _star
        in resolver.imports_in_file(str(module_path))
    }


def test_analysis_owner_and_graph_dependency_direction():
    import emend.analysis_store as owner
    import emend.fact_graph as fact_graph

    assert not {
        module for module in _module_imports(owner)
        if module.startswith("emend.transform")
    }
    assert "emend.analysis_store" not in _module_imports(fact_graph)


def test_project_stores_and_disk_caches_are_scoped(tmp_path):
    from emend.analysis_store import find_project_root
    from emend.transform.cache import _get_disk_cache

    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left.parent / ".emend" / "cache").mkdir(parents=True)
    left_store, right_store = AnalysisStore.open(left), AnalysisStore.open(right)
    assert left_store is not right_store
    assert left_store.db_path != right_store.db_path
    assert (left_store.project_root, right_store.project_root) == (
        left.resolve(), right.resolve()
    )
    assert find_project_root(left) == left.resolve()
    assert find_project_root(right) == right.resolve()
    left_conn, right_conn = _get_disk_cache(left), _get_disk_cache(right)
    assert left_conn is not right_conn
    assert all((root / ".emend" / "cache" / "parse.db").is_file()
               for root in (left, right))


def test_editor_and_type_cache_use_independent_readers(tmp_path):
    from emend.editor_search import EditorSearchEngine
    from emend.type_oracle import _TypeOracleDiskCache

    store = AnalysisStore.open(tmp_path)
    editor = EditorSearchEngine(str(tmp_path))
    try:
        editor_connection = editor._get_conn()
        type_cache = _TypeOracleDiskCache(str(store.db_path))
        assert editor_connection is not store.connection()
        assert type_cache._conn is not editor_connection
        assert len(store._reader_connections) == 2
        type_cache.close()
    finally:
        editor.close()
    assert not store._reader_connections
    assert store.connection().execute("SELECT 1").fetchone() == (1,)


def test_dropped_type_cache_releases_its_reader(tmp_path):
    import gc
    from emend.type_oracle import _TypeOracleDiskCache

    store = AnalysisStore.open(tmp_path)
    cache = _TypeOracleDiskCache(str(store.artifact_path))
    assert len(store._reader_connections) == 1
    del cache
    gc.collect()
    assert not store._reader_connections


def test_type_cache_reads_current_engine_dependencies_and_config(tmp_path):
    from emend.type_oracle import (
        TypeBinding,
        TypeDescriptor,
        _file_cache_key,
        create_type_oracle,
        load_cached_file_types,
    )

    config = tmp_path / "pyproject.toml"

    def write_type_config(mode):
        config.write_text(
            "[project]\nname = 'types'\n[tool.pyright]\n"
            f"typeCheckingMode = '{mode}'\n"
        )

    write_type_config("basic")
    target, imported, leaf = (
        tmp_path / "target.py", tmp_path / "dependency.py", tmp_path / "leaf.py"
    )
    target.write_text("from dependency import value\nresult = value\n")
    imported.write_text("from leaf import base\nvalue: int = base\n")
    leaf.write_text("base = 1\n")
    pyrefly = create_type_oracle("pyrefly", project_root=tmp_path)
    pyright = create_type_oracle("pyright", project_root=tmp_path)
    changed_args = create_type_oracle(
        "pyrefly", project_root=tmp_path, extra_args=["--ignore-errors"]
    )
    write_type_config("strict")
    changed_config = create_type_oracle("pyrefly", project_root=tmp_path)
    assert len({oracle._cache.namespace for oracle in (
        pyrefly, pyright, changed_args, changed_config
    )}) == 4
    write_type_config("basic")

    def cached(engine):
        return FileTypes(path=str(target), bindings=[TypeBinding(
            name="result", line=2, col_start=1, col_end=7,
            type_descriptor=TypeDescriptor.named(engine), raw_type=engine,
            binding_kind="definition",
        )])

    key = _file_cache_key(target)
    pyrefly._cache.put(key, cached("pyrefly"))
    pyright._cache.put(key, cached("pyright"))
    assert load_cached_file_types(target, project_root=tmp_path,
                                  engine="pyrefly").bindings[0].raw_type == "pyrefly"
    assert load_cached_file_types(target, project_root=tmp_path,
                                  engine="pyright").bindings[0].raw_type == "pyright"
    assert load_cached_file_types(target, project_root=tmp_path) is not None

    write_type_config("strict")
    assert load_cached_file_types(target, project_root=tmp_path) is None
    write_type_config("basic")
    pyright._cache.put(key, cached("pyright"))
    assert load_cached_file_types(target, project_root=tmp_path) is not None
    leaf.write_text("base = 'changed'\n")
    assert load_cached_file_types(target, project_root=tmp_path) is None
    pyright._cache.put(_file_cache_key(target), cached("pyright"))
    imported.write_text("value: str = 'changed'\n")
    assert load_cached_file_types(target, project_root=tmp_path) is None


def test_long_lived_type_oracle_refreshes_config_namespace(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'types'\n[tool.pyright]\ntypeCheckingMode = 'basic'\n"
    )
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    _FakeTypeOracle.calls = 0
    oracle = _FakeTypeOracle(tmp_path)
    oracle.infer_file(source, tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'types'\n[tool.pyright]\ntypeCheckingMode = 'strict'\n"
    )
    oracle.infer_file(source, tmp_path)
    assert _FakeTypeOracle.calls == 2


def _assert_relative_dependency_identity(
    tmp_path, extension, statement, dependency_source, changed_source
):
    for package in ("alpha", "beta"):
        root = tmp_path / package
        root.mkdir()
        (root / "__init__.py").write_text("")
        (root / f"dep.{extension}").write_text(
            dependency_source.format(package=package)
        )
    target = tmp_path / "beta" / f"use.{extension}"
    target.write_text(statement)
    store = AnalysisStore.open(tmp_path)
    initial = store.type_file_identity(target)
    (tmp_path / "alpha" / f"dep.{extension}").write_text(changed_source)
    assert store.type_file_identity(target) == initial
    (tmp_path / "beta" / f"dep.{extension}").write_text(changed_source)
    assert store.type_file_identity(target) != initial


@pytest.mark.parametrize(
    "statement",
    [
        "from .dep import value\nresult = value\n",
        "from . import dep\nresult = dep.value\n",
    ],
)
def test_type_identity_resolves_relative_dependency_in_its_package(
    tmp_path, statement
):
    _assert_relative_dependency_identity(
        tmp_path, "py", statement, "value = {package!r}\n", "value = 'changed'\n"
    )


@pytest.mark.parametrize("specifier", ["./dep", "./dep.js"])
def test_type_identity_resolves_relative_typescript_dependency(tmp_path, specifier):
    _assert_relative_dependency_identity(
        tmp_path,
        "ts",
        f"import {{ value }} from '{specifier}';\nconst result = value;\n",
        "export const value: number = 1;\n",
        "export const value: string = 'changed';\n",
    )


@pytest.mark.parametrize("inherited", [False, True])
def test_type_identity_resolves_typescript_path_alias(tmp_path, inherited):
    options = (
        '{\n"$schema": "https://example.com/a//schema.json",\n// aliases\n'
        '"compilerOptions": '
        '{"baseUrl": ".", "paths": {"@lib/*": ["lib/*"]},},\n}\n'
    )
    config = tmp_path / ("tsconfig.base.json" if inherited else "tsconfig.json")
    config.write_text(options)
    if inherited:
        (tmp_path / "tsconfig.json").write_text(
            '{"extends": "./tsconfig.base.json"}\n'
        )
    (tmp_path / "lib").mkdir()
    dependency = tmp_path / "lib" / "dep.ts"
    target = tmp_path / "app.ts"
    dependency.write_text("export const value: number = 1;\n")
    target.write_text('import { value } from "@lib/dep";\n')
    store = AnalysisStore.open(tmp_path)
    initial = store.type_file_identity(target)
    dependency.write_text("export const value: string = 'changed';\n")
    assert store.type_file_identity(target) != initial


def test_typescript_ambient_declarations_participate_in_type_identity(tmp_path):
    target = tmp_path / "app.ts"
    ambient = tmp_path / "globals.d.ts"
    target.write_text("const value = window.projectValue;\n")
    ambient.write_text("interface Window { projectValue: number }\n")
    store = AnalysisStore.open(tmp_path)
    initial = store.type_file_identity(target)
    ambient.write_text("interface Window { projectValue: string }\n")
    assert store.type_file_identity(target) != initial


def test_deadcode_computes_type_snapshot_context_once(tmp_path, monkeypatch):
    from emend.transform import find_dead_code, warm_caches
    import emend.type_oracle as type_oracle

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'decorators'\n")
    for name in ("one", "two"):
        (tmp_path / f"{name}.py").write_text(
            "from fastapi import APIRouter\n"
            "routes: APIRouter\n"
            f"@routes.get('/{name}')\n"
            f"def {name}():\n    return 1\n"
        )
    warm_caches(str(tmp_path), type_engine="none")
    store = AnalysisStore.open(tmp_path)
    original_context = type_oracle._type_shared_context
    original_scan = store._scan_disk
    original_dependencies = store._type_dependency_state
    calls = {"context": 0, "scan": 0, "dependencies": 0}

    def counted(project_root):
        calls["context"] += 1
        return original_context(project_root)

    def count_scan():
        calls["scan"] += 1
        return original_scan()

    def count_dependencies(*args, **kwargs):
        calls["dependencies"] += 1
        return original_dependencies(*args, **kwargs)

    monkeypatch.setattr(type_oracle, "_type_shared_context", counted)
    monkeypatch.setattr(store, "_scan_disk", count_scan)
    monkeypatch.setattr(store, "_type_dependency_state", count_dependencies)
    list(find_dead_code(str(tmp_path), show_last_reference=False))
    assert calls == {"context": 1, "scan": 1, "dependencies": 1}


def test_editor_overlay_owner_cannot_remove_a_newer_session(tmp_path):
    from emend.editor_search import EditorSearchEngine

    source = tmp_path / "app.py"
    source.write_text("def disk():\n    return 0\n")
    first = EditorSearchEngine(str(tmp_path))
    second = EditorSearchEngine(str(tmp_path))
    try:
        first.buffer_open(str(source), "def first():\n    return 1\n", version=1)
        second.buffer_open(str(source), "def second():\n    return 2\n", version=2)
        first.close()
        assert _names(AnalysisStore.open(tmp_path).query_facts().symbols()) == ["second"]
    finally:
        first.close()
        second.close()
    assert _names(AnalysisStore.open(tmp_path).query_facts().symbols()) == ["disk"]


def test_query_facts_reuses_generation_and_incrementally_matches_full_build(tmp_path):
    from emend.fact_graph import FactGraph

    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_text("def first():\n    return second()\n")
    second.write_text("def second():\n    return 1\n")
    store = AnalysisStore.open(tmp_path)
    cold = store.query_facts()
    cache_mtimes = {
        path: path.stat().st_mtime_ns for path in (store.db_path, store.facts_path)
    }
    warm = store.query_facts()
    assert store.facts_path.is_file()
    assert cold is warm and cold.snapshot is warm.snapshot
    assert cache_mtimes == {path: path.stat().st_mtime_ns for path in cache_mtimes}
    assert cold.snapshot.snapshot_id == cold.published_snapshot_id()
    assert _names(cold.symbols()) == ["first", "second"]

    first.write_text("def changed():\n    return 2\n")
    second.unlink()
    assert cold.source_text(first).startswith("def first")
    assert cold.source_text(second).startswith("def second")
    incremental = store.query_facts()
    assert _names(cold.symbols()) == ["first", "second"]
    assert cold.source_text(first).startswith("def first")
    assert cold.source_text(second).startswith("def second")
    rebuilt = FactGraph()
    rebuilt.update_files(
        [(str(first), first.read_text())],
        project_root=str(tmp_path), resolver_root=str(tmp_path),
    )
    try:
        assert {
            (fact.file_path, fact.qualified_name, fact.kind)
            for fact in incremental.symbols()
        } == {
            (fact.file_path, fact.qualified_name, fact.kind)
            for fact in rebuilt.symbols()
        }
        assert incremental._all_calls() == rebuilt._all_calls()
        assert incremental._all_references() == rebuilt._all_references()
    finally:
        rebuilt.close()
    same_size = tmp_path / "same_size.py"
    same_size.write_text("def before():\n    return 1\n")
    store.query_facts()
    original = same_size.stat()
    same_size.write_text("def after_():\n    return 2\n")
    os.utime(same_size, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert _names(store.query_facts().symbols(file_path="same_size.py")) == ["after_"]


def test_reopened_store_reuses_revision_identity_without_hiding_same_stat_edits(
    tmp_path, monkeypatch
):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    before = AnalysisStore(tmp_path).disk_snapshot().files[0]
    original_stat = source.stat()
    original_read = Path.read_text
    reads = 0

    def counted(path, *args, **kwargs):
        nonlocal reads
        if path.resolve() == source.resolve():
            reads += 1
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    assert AnalysisStore(tmp_path).disk_snapshot().files[0] == before
    assert reads == 0

    source.write_text("value = 2\n")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    after = AnalysisStore(tmp_path).disk_snapshot().files[0]
    assert reads == 1 and after.content_hash != before.content_hash


def test_extraction_artifacts_reuse_across_content_revert(tmp_path, extracted_files):
    source = tmp_path / "app.py"
    first_content = "def first():\n    return 1\n"
    source.write_text(first_content)
    store = AnalysisStore.open(tmp_path)
    store.query_facts()
    assert len(extracted_files) == 1
    source.write_text("def second_name():\n    return 2\n")
    store.query_facts()
    assert len(extracted_files) == 2
    source.write_text(first_content)
    store.query_facts()
    assert len(extracted_files) == 2


def test_exact_project_language_config_invalidates_extracted_facts(
    tmp_path, extracted_files,
):
    from emend.language_registry import language_config_snapshot

    source = tmp_path / "app.py"
    source.write_text("def visible():\n    return 1\n")
    config_dir = tmp_path / "languages" / "python"
    config_dir.mkdir(parents=True)
    payload = language_config_snapshot("python").payload
    config_path = config_dir / "config.toml"
    config_path.write_text(payload)
    store = AnalysisStore.open(tmp_path)
    first = store.query_facts()
    assert _names(first.symbols()) == ["visible"]

    config_path.write_text(payload.replace(
        'function_node = "function_definition"',
        'function_node = "not_a_function_definition"',
        1,
    ))
    second = store.query_facts()
    assert second.snapshot.snapshot_id != first.snapshot.snapshot_id
    assert second.symbols() == []
    assert len(extracted_files) == 2


def test_reopened_store_refreshes_only_the_changed_revision(tmp_path, monkeypatch):
    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_text("def first():\n    return 1\n")
    second.write_text("def second():\n    return 2\n")
    initial = AnalysisStore(tmp_path)
    initial.query_facts()
    initial.close()
    first.write_text("def changed():\n    return 3\n")

    reopened = AnalysisStore(tmp_path)
    original = reopened._extract_revisions
    extracted = []

    def tracked(revisions, contents):
        revisions = list(revisions)
        extracted.extend(revision.file_path for revision in revisions)
        return original(revisions, contents)

    monkeypatch.setattr(reopened, "_extract_revisions", tracked)
    assert {fact.name for fact in reopened.query_facts().symbols()} == {
        "changed", "second",
    }
    assert extracted == [str(first)]


def test_extraction_artifacts_are_shared_by_linked_worktrees(tmp_path, extracted_files):
    main, linked = _linked_worktrees(tmp_path)
    main, linked = main / "packages" / "pkg", linked / "packages" / "pkg"
    content = "def shared():\n    return 1\n"
    for root in (main, linked):
        root.mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname = 'pkg'\n")
        (root / "app.py").write_text(content)
    main_store, linked_store = AnalysisStore.open(main), AnalysisStore.open(linked)
    main_store.query_facts()
    linked_store.query_facts()
    assert main_store.artifact_path == linked_store.artifact_path
    assert main_store.facts_path != linked_store.facts_path
    assert main_store.facts_path.is_file() and linked_store.facts_path.is_file()
    assert len(extracted_files) == 1
    (linked / "app.py").write_text("def linked_only():\n    return 2\n")
    assert _names(linked_store.query_facts().symbols()) == ["linked_only"]
    assert _names(main_store.query_facts().symbols()) == ["shared"]
    assert len(extracted_files) == 2


def test_fact_refresh_is_atomic_and_probes_do_not_hide_new_content(tmp_path, monkeypatch):
    from emend.fact_graph import FactGraph

    source = tmp_path / "app.py"
    source.write_text("def before():\n    return 1\n")
    store = AnalysisStore.open(tmp_path)
    graph = store.query_facts()
    published = graph.published_snapshot_id()
    source.write_text("def after():\n    return 2\n")
    store.source_snapshot()
    store.type_context_id()

    replace_extracted = FactGraph.replace_extracted

    def fail(*args, **kwargs):
        raise RuntimeError("injected refresh failure")

    monkeypatch.setattr(FactGraph, "replace_extracted", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.query_facts()
    assert graph.published_snapshot_id() == published
    assert graph.snapshot.snapshot_id == published
    monkeypatch.setattr(FactGraph, "replace_extracted", replace_extracted)
    assert _names(store.query_facts().symbols()) == ["after"]


def test_sqlite_snapshot_copy_includes_live_wal(tmp_path):
    source, copied = tmp_path / "source.db", tmp_path / "copied.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('committed')")
        connection.commit()
        AnalysisStore._copy_sqlite(source, copied)
        with sqlite3.connect(copied) as snapshot:
            assert snapshot.execute("SELECT value FROM items").fetchall() == [
                ("committed",)
            ]
    finally:
        connection.close()


def test_concurrent_process_publications_bind_matching_snapshots(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def first():\n    return 1\n")
    marker, release = tmp_path / "scanned", tmp_path / "release"
    worker = """
import hashlib, json, os, time
from pathlib import Path
from emend.analysis_store import AnalysisStore

root = Path(os.environ["EMEND_TEST_ROOT"])
store = AnalysisStore(root)
if os.environ.get("EMEND_TEST_DELAY"):
    original = store._extract_revisions
    def delayed(revisions, contents):
        Path(os.environ["EMEND_TEST_MARKER"]).touch()
        release = Path(os.environ["EMEND_TEST_RELEASE"])
        while not release.exists():
            time.sleep(0.01)
        return original(revisions, contents)
    store._extract_revisions = delayed
graph = store.query_facts()
revision = graph.snapshot.files[0]
print(json.dumps([
    revision.content_hash,
    hashlib.sha256(graph.source_text(revision.file_path).encode()).hexdigest(),
    [fact.name for fact in graph.symbols()],
]))
"""
    env = os.environ.copy()
    env.update({
        "EMEND_TEST_ROOT": str(tmp_path),
        "EMEND_TEST_DELAY": "1",
        "EMEND_TEST_MARKER": str(marker),
        "EMEND_TEST_RELEASE": str(release),
    })
    first = subprocess.Popen(
        [sys.executable, "-c", worker], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 10
    while not marker.exists() and first.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    source.write_text("def second():\n    return 2\n")
    second_env = env | {"EMEND_TEST_DELAY": ""}
    second = subprocess.Popen(
        [sys.executable, "-c", worker], env=second_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    release.touch()
    outputs = [process.communicate(timeout=20) for process in (first, second)]
    assert all(process.returncode == 0 for process in (first, second)), outputs
    results = [json.loads(stdout.strip().splitlines()[-1]) for stdout, _ in outputs]
    assert results[0][0] == results[0][1]
    assert results[1][0] == results[1][1]
    assert results[0][2] == ["first"]
    assert results[1][2] == ["second"]
    assert _names(AnalysisStore(tmp_path).query_facts().symbols()) == ["second"]


def test_query_facts_overlay_is_versioned_and_source_consistent(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def disk_name():\n    return 1\n")
    store = AnalysisStore.open(tmp_path)
    disk = store.query_facts()
    accepted = store.update_overlay(source, "def overlay_name():\n    return 2\n", 2)
    stale = store.update_overlay(source, "def stale_name():\n    return 3\n", 1)
    overlay = store.query_facts()
    assert accepted.accepted is True and stale.accepted is False
    assert overlay is not disk
    assert overlay.source_text(str(source)).startswith("def overlay_name")
    assert _names(overlay.symbols()) == ["overlay_name"]
    store.update_overlay(source, "def newest_name():\n    return 3\n", 3)
    newest = store.query_facts()
    assert newest is not overlay
    assert _names(overlay.symbols()) == ["overlay_name"]
    assert _names(newest.symbols()) == ["newest_name"]
    store.remove_overlay(source)
    assert store.query_facts() is disk


def test_overlay_clones_the_bound_disk_generation_not_latest_publication(tmp_path):
    source = tmp_path / "app.py"
    other = tmp_path / "other.py"
    source.write_text("def original():\n    return 1\n")
    other.write_text("def unchanged():\n    return 1\n")
    first = AnalysisStore(tmp_path)
    second = AnalysisStore(tmp_path)
    try:
        first.query_facts()
        source.write_text("def published_later():\n    return 2\n")
        second.query_facts()
        source.write_text("def original():\n    return 1\n")

        first.update_overlay(other, "def edited():\n    return 3\n", 1)
        graph = first.query_facts()

        assert graph.source_text(source).startswith("def original")
        assert {fact.name for fact in graph.symbols()} == {"original", "edited"}
        first.remove_overlay(other)
        source.write_text("def latest():\n    return 4\n")
        assert {fact.name for fact in first.query_facts().symbols()} == {
            "latest", "unchanged",
        }
    finally:
        first.close()
        second.close()
    assert not list((tmp_path / ".emend" / "cache").glob("facts-open-*.db"))


def test_fact_consumers_reuse_one_generation_without_ad_hoc_graphs(tmp_path, monkeypatch):
    from emend.component_selector import ExtendedSelector
    from emend.fact_graph import FactGraph
    from emend.trace import TraceConfig, TraceSink, TraceSource, run_trace_analysis
    from emend.transform import find_dead_code, find_references

    source = tmp_path / "app.py"
    source.write_text(
        "def used():\n    return 1\n\ndef caller():\n    return used()\n\n"
        "def dead():\n    return 1\n\n"
        "def handler(request, cursor):\n"
        "    value = request.args.get('x')\n"
        "    cursor.execute(value)\n"
    )
    store = AnalysisStore.open(tmp_path)
    seen = []
    original = store.query_facts

    def tracked(**kwargs):
        graph = original(**kwargs)
        seen.append((id(graph), graph.snapshot.snapshot_id))
        return graph

    def forbidden(*args, **kwargs):
        raise AssertionError("normal consumer built an ad-hoc FactGraph")

    monkeypatch.setattr(store, "query_facts", tracked)
    monkeypatch.setattr(FactGraph, "build_from_files", forbidden)
    monkeypatch.setattr(FactGraph, "build_from_project", forbidden)
    list(find_dead_code(str(tmp_path), show_last_reference=False))
    list(find_references(
        ExtendedSelector(file_path=str(source), symbol_path=["used"]),
        project_path=str(tmp_path),
    ))
    run_trace_analysis(
        [str(source)],
        TraceConfig(
            labels=["input"],
            sources=[TraceSource(pattern="request.args.get($X)", label="input")],
            sinks=[TraceSink(pattern="cursor.execute($X)", label="input", message="unsafe")],
        ),
        project_path=str(tmp_path),
    )
    assert len(seen) >= 2 and len(set(seen)) == 1


def test_build_from_project_adapts_owned_facts_without_owning_the_owner(
    tmp_path, monkeypatch
):
    """The legacy builder delegates collection/typing and returns a safe copy."""
    from emend.fact_graph import FactGraph

    source = tmp_path / "app.py"
    source.write_text("def live():\n    return 1\n")
    store = AnalysisStore.open(tmp_path)
    owner_graph = store.query_facts()
    calls = []
    original = store.query_facts

    def tracked(*, include_types=False, type_engine="auto"):
        calls.append((include_types, type_engine))
        return original(include_types=include_types, type_engine=type_engine)

    monkeypatch.setattr(store, "query_facts", tracked)
    legacy_graph = FactGraph.build_from_project(str(tmp_path), include_types=True)
    assert calls[0] == (True, "auto")
    assert _names(legacy_graph.symbols()) == ["live"]

    # Closing a compatibility result must not close the shared owner view.
    legacy_graph.close()
    assert _names(owner_graph.symbols()) == ["live"]


def test_explicit_language_build_finds_deep_source_without_project_marker(tmp_path):
    from emend.fact_graph import FactGraph

    source = tmp_path / "src" / "package" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("def live():\n    return 1\n")
    graph = FactGraph.build_from_project(
        str(tmp_path), language="python", include_types=False
    )
    assert _names(graph.symbols()) == ["live"]


def test_legacy_filtered_build_cannot_overwrite_owner_generation(tmp_path):
    from emend.fact_graph import FactGraph

    (tmp_path / "app.py").write_text("def python_symbol():\n    pass\n")
    (tmp_path / "app.ts").write_text("function tsSymbol() {}\n")
    store = AnalysisStore.open(tmp_path)
    store.query_facts()
    detached = FactGraph.build_from_project(
        str(tmp_path), language="python", db_path=str(store.facts_path),
        include_types=False,
    )
    try:
        assert _names(detached.symbols()) == ["python_symbol"]
    finally:
        detached.close()
    reopened = AnalysisStore(tmp_path).query_facts()
    assert {symbol.name for symbol in reopened.symbols()} == {
        "python_symbol", "tsSymbol",
    }


@pytest.mark.parametrize("inherited_config", [False, True])
def test_type_batch_uses_one_snapshot_and_reuses_linked_worktree_payload(
    tmp_path, monkeypatch, inherited_config
):
    main, linked = _linked_worktrees(tmp_path)
    for root in (main, linked):
        (root / "one.py").write_text("value = 1\n")
        (root / "two.py").write_text("other = 2\n")
        if inherited_config:
            (root / "tsconfig.json").write_text('{"extends":"./base.json"}')
            (root / "base.json").write_text('{"compilerOptions":{"strict":true}}')
    target, dependency, unrelated = (
        main / "target.py", main / "dependency.py", main / "unrelated.py"
    )
    target.write_text("from dependency import value\nresult = value\n")
    dependency.write_text("value = 1\n")
    unrelated.write_text("other = 1\n")

    main_store = AnalysisStore.open(main)
    scan_calls = 0
    original = main_store._scan_disk

    def counted():
        nonlocal scan_calls
        scan_calls += 1
        return original()

    monkeypatch.setattr(main_store, "_scan_disk", counted)
    monkeypatch.setattr(AnalysisStore, "query_facts", lambda *a, **kw: pytest.fail(
        "type cache inputs must not materialize a fact graph"
    ))
    _FakeTypeOracle.calls = 0
    _FakeTypeOracle(main).infer_batch(
        [main / "one.py", main / "two.py"], project_root=main
    )
    assert scan_calls == 1 and _FakeTypeOracle.calls == 2
    linked_paths = [linked / "one.py", linked / "two.py"]
    results = _FakeTypeOracle(linked).infer_batch(linked_paths, project_root=linked)
    assert _FakeTypeOracle.calls == 2
    assert results[str(linked_paths[0].resolve())].path == str(linked_paths[0].resolve())
    before = main_store.type_file_identity(target)
    unrelated.write_text("other = 2\n")
    assert main_store.type_file_identity(target) == before
    dependency.write_text("value = 'changed'\n")
    assert main_store.type_file_identity(target) != before
    dependency.write_text("value = 1\n")
    assert main_store.type_file_identity(target) == before


@pytest.mark.parametrize("extension", ["py", "pyi"])
@pytest.mark.parametrize("package", [False, True])
def test_installed_type_dependencies_refresh_and_share_artifacts(tmp_path, monkeypatch, extension, package):
    main, linked = _linked_worktrees(tmp_path)
    for root in (main, linked):
        (root / "target.py").write_text("from dependency import value\nresult = value\n")
        (root / "pyproject.toml").write_text("[tool.emend.environment_lookup]\nenabled=false\n")
        installed = root / ".venv/lib/python3.14/site-packages"
        installed.mkdir(parents=True)
        dep = installed / "dependency" if package else installed
        dep.mkdir(exist_ok=True)
        (dep / f"{'__init__' if package else 'dependency'}.{extension}").write_text(
            f"from {'.' if package else ''}transitive import value\n"
        )
        (dep / f"transitive.{extension}").write_text("value: int = 1\n")
        (installed / f"unrelated.{extension}").write_text("other = 1\n")
        (root / "unrelated").mkdir()
        (root / "unrelated/dependency.py").write_text("value = False\n")
    target = main / "target.py"
    store = AnalysisStore.open(main)
    initial = store.type_file_identity(target)
    linked_store = AnalysisStore.open(linked)
    assert linked_store.type_file_identity(linked / "target.py") == initial
    with sqlite3.connect(store.artifact_path) as db:
        assert db.execute("SELECT count(*) FROM dependency_import_artifact").fetchone()[0] == 2
    installed = main / ".venv/lib/python3.14/site-packages"
    original_read = Path.read_text
    def no_dependency_read(path, *args, **kwargs):
        assert not path.is_relative_to(installed), "unchanged dependencies must not be reread"
        return original_read(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", no_dependency_read)
        assert store.type_file_identity(target) == initial
    (installed / f"unrelated.{extension}").write_text("other = 2\n")
    assert store.type_file_identity(target) == initial
    dependency = (installed / "dependency" if package else installed) / f"transitive.{extension}"
    dependency.write_text("value: str = 'changed'\n")
    assert store.type_file_identity(target) != initial
    dependency.write_text("value: int = 1\n")
    assert store.type_file_identity(target) == initial
    dependency.unlink()
    assert store.type_file_identity(target) != initial


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("extension", ["ts", "d.ts"])
def test_installed_typescript_relative_dependencies(tmp_path, linked, extension):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "app.ts"
    target.write_text('import {value} from "dep"; const result = value;\n')
    installed = root / "node_modules"
    installed.mkdir()
    package = tmp_path / "linked-package" if linked else installed / "dep"
    package.mkdir()
    if linked:
        (installed / "dep").symlink_to(package, target_is_directory=True)
    (package / f"index.{extension}").write_text('import {value} from "./inner"; export {value};\n')
    dependency = package / f"inner.{extension}"
    dependency.write_text("export const value: number;\n")
    store = AnalysisStore.open(root)
    initial = store.type_file_identity(target)
    dependency.write_text('export const value: string;\n')
    assert store.type_file_identity(target) != initial
    dependency.write_text("export const value: number;\n")
    assert store.type_file_identity(target) == initial
    dependency.unlink()
    assert store.type_file_identity(target) != initial


def test_typed_view_retries_failures_and_refreshes_installed_inputs(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("from dependency import value\n")
    installed = tmp_path / ".venv/lib/python3.14/site-packages"
    installed.mkdir(parents=True)
    dependency = installed / "dependency.py"
    dependency.write_text("value = 1\n")
    class Oracle(_TypedOracle):
        calls = 0
        def infer_file(self, path, project_root=None):
            if not self.calls:
                type(self).calls += 1
                return FileTypes(path=str(path), complete=False)
            return super().infer_file(path, project_root)
    monkeypatch.setattr("emend.type_oracle.create_type_oracle", lambda **_: Oracle())
    store = AnalysisStore.open(tmp_path)
    assert not store.query_facts(include_types=True).types_for("value")
    typed = store.query_facts(include_types=True)
    assert typed.types_for("value") and Oracle.calls == 2
    assert store.query_facts(include_types=True) is typed
    dependency.write_text("value = 'changed'\n")
    assert store.query_facts(include_types=True) is not typed
    assert Oracle.calls == 3


def test_typed_fact_view_is_lazy_cached_and_snapshot_bound(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    _TypedOracle.calls = 0
    monkeypatch.setattr("emend.type_oracle.create_type_oracle", lambda **_: _TypedOracle())
    store = AnalysisStore.open(tmp_path)
    base = store.query_facts()
    typed = store.query_facts(include_types=True)

    assert base.types_for("value") == []
    assert typed.types_for("value")[0].type_str == "int"
    from emend.policy import DatalogCheck, Policy, run_policy_checks

    violations = run_policy_checks([], [Policy(
        "typed", "typed", "error", [DatalogCheck(
            '?[file_path, line] := *type_binding[_, file_path, line, _, "int"]'
        )],
    )], project_path=str(tmp_path))
    assert [(violation.file_path, violation.line) for violation in violations] == [
        ("app.py", 1)
    ]
    assert store.query_facts(include_types=True) is typed
    assert _TypedOracle.calls == 1
    source.write_text("value = 'new'\n")
    refreshed = store.query_facts(include_types=True)
    assert refreshed is not typed
    assert typed.source_text(source) == "value = 1\n"


def test_typed_fact_view_refreshes_when_engine_identity_changes(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("value = 1\n")
    version = ["engine-v1"]

    class VersionedOracle(_TypedOracle):
        @property
        def cache_context_id(self):
            return version[0]

    monkeypatch.setattr(
        "emend.type_oracle.create_type_oracle", lambda **_: VersionedOracle()
    )
    VersionedOracle.calls = 0
    store = AnalysisStore.open(tmp_path)
    first = store.query_facts(include_types=True)
    version[0] = "engine-v2"
    second = store.query_facts(include_types=True)
    assert second is not first
    assert VersionedOracle.calls == 2


def test_trace_reads_overlay_only_file_from_selected_snapshot(tmp_path):
    from emend.trace import TraceConfig, TraceSink, TraceSource, run_trace_analysis

    path = tmp_path / "newdir" / "new.py"
    store = AnalysisStore.open(tmp_path)
    store.update_overlay(
        path, "def f():\n    x = source()\n    sink(x)\n", 1,
    )
    violations = run_trace_analysis(
        [str(path)],
        TraceConfig(
            labels=["value"],
            sources=[TraceSource("source()", "value")],
            sinks=[TraceSink("sink($X)", "value", "unsafe")],
        ),
    )
    assert [(violation.file_path, violation.line) for violation in violations] == [
        (str(path), 3)
    ]


def test_cli_and_mcp_type_queries_use_the_typed_owner_view(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from emend.cli import app

    (tmp_path / "app.py").write_text("value = 1\n")
    monkeypatch.setattr("emend.type_oracle.create_type_oracle", lambda **_: _TypedOracle())
    result = CliRunner().invoke(app, [
        "analyze", "facts", str(tmp_path), "--type", "types",
        "--symbol", "value", "--json",
    ])
    assert result.exit_code == 0, result.output
    cli_data, _ = json.JSONDecoder().raw_decode(result.output)
    assert cli_data[0]["type_str"] == "int"
    if importlib.util.find_spec("mcp") is None:
        return
    from emend.mcp.tooling import facts_query
    assert json.loads(facts_query(
        project=str(tmp_path), fact_type="types", symbol="value"
    ))[0]["type_str"] == "int"


def test_disk_type_engine_returns_empty_overlay_view(tmp_path, monkeypatch):
    from emend.type_oracle import TypeOracle

    class DiskOracle(TypeOracle):
        def infer_file(self, path, project_root=None):
            raise AssertionError("disk-only engine must not inspect an overlay")

        def clear_cache(self):
            pass

        def is_available(self):
            return True

    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    monkeypatch.setattr("emend.type_oracle.create_type_oracle", lambda **_: DiskOracle())
    store = AnalysisStore.open(tmp_path)
    store.update_overlay(source, "value = 'overlay'\n", 1)
    typed = store.query_facts(include_types=True)

    assert typed.source_text(source) == "value = 'overlay'\n"
    assert typed.types_for("value") == []
