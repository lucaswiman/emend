"""Tests for the mapping store (knowledge.py)."""

from __future__ import annotations

import json
import os

import pytest
import yaml

from emend.knowledge import (
    IdentifierMapping,
    MappingStore,
    ModuleMapping,
    mapping_to_dict,
    module_mapping_to_dict,
)


@pytest.fixture
def store(tmp_path):
    """Create a MappingStore rooted in a temp directory."""
    return MappingStore(str(tmp_path))


# ---------------------------------------------------------------------------
# Identifier mappings
# ---------------------------------------------------------------------------


class TestMappings:
    def test_add_and_search(self, store):
        m = IdentifierMapping(
            source_project="user-service",
            source_identifier="users.UserService.create",
            source_kind="function",
            target_project="api-gateway",
            target_identifier="POST /api/v1/users",
            target_kind="endpoint",
            relationship="implements",
            confidence=0.95,
            provenance="llm",
            evidence="LLM observed that create() handles the POST endpoint.",
        )
        store.add_mapping(m)

        results = store.search_mappings("UserService")
        assert len(results) == 1
        assert results[0].source_project == "user-service"
        assert results[0].target_identifier == "POST /api/v1/users"
        assert results[0].confidence == 0.95
        assert results[0].relationship == "implements"

    def test_delete(self, store):
        store.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="x",
            target_project="b", target_identifier="y",
        ))
        assert store.delete_mapping("x") is True
        assert store.search_mappings("x") == []

    def test_delete_nonexistent(self, store):
        assert store.delete_mapping("nonexistent") is False

    def test_search_substring(self, store):
        store.add_mapping(IdentifierMapping(
            source_project="svc-a", source_identifier="OrderService.submit",
            source_kind="method",
            target_project="svc-b", target_identifier="POST /orders",
            target_kind="endpoint",
            evidence="Submit order creates an order via the API.",
        ))
        store.add_mapping(IdentifierMapping(
            source_project="svc-a", source_identifier="OrderService.cancel",
            source_kind="method",
            target_project="svc-b", target_identifier="DELETE /orders/{id}",
            target_kind="endpoint",
        ))

        results = store.search_mappings("submit")
        assert len(results) == 1
        assert "submit" in results[0].source_identifier.lower()

    def test_search_with_filters(self, store):
        store.add_mapping(IdentifierMapping(
            source_project="alpha", source_identifier="foo",
            target_project="beta", target_identifier="bar",
            relationship="calls",
        ))
        store.add_mapping(IdentifierMapping(
            source_project="alpha", source_identifier="baz",
            target_project="gamma", target_identifier="qux",
            relationship="equivalent",
        ))

        results = store.search_mappings("", source_project="alpha", relationship="calls")
        assert all(m.relationship == "calls" for m in results)

    def test_find_mappings_for(self, store):
        store.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="Foo.bar",
            source_kind="method",
            target_project="b", target_identifier="handle_bar",
            target_kind="function",
        ))
        store.add_mapping(IdentifierMapping(
            source_project="c", source_identifier="invoke_bar",
            source_kind="function",
            target_project="a", target_identifier="Foo.bar",
            target_kind="method",
        ))

        # Both directions
        results = store.find_mappings_for("Foo.bar")
        assert len(results) == 2

        # Source only
        results = store.find_mappings_for("Foo.bar", direction="source")
        assert len(results) == 1
        assert results[0].target_identifier == "handle_bar"

        # Target only
        results = store.find_mappings_for("Foo.bar", direction="target")
        assert len(results) == 1
        assert results[0].source_identifier == "invoke_bar"

    def test_find_mappings_for_with_project(self, store):
        store.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="X",
            target_project="b", target_identifier="Y",
        ))
        store.add_mapping(IdentifierMapping(
            source_project="c", source_identifier="X",
            target_project="d", target_identifier="Z",
        ))

        results = store.find_mappings_for("X", project="a", direction="source")
        assert len(results) == 1
        assert results[0].target_identifier == "Y"

    def test_list_mappings(self, store):
        for i in range(5):
            store.add_mapping(IdentifierMapping(
                source_project="p", source_identifier=f"id_{i}",
                target_project="q", target_identifier=f"tid_{i}",
            ))

        all_m = store.list_mappings()
        assert len(all_m) == 5

        limited = store.list_mappings(limit=2)
        assert len(limited) == 2

    def test_mapping_to_dict(self, store):
        m = IdentifierMapping(
            source_project="a", source_identifier="x",
            source_kind="function",
            target_project="b", target_identifier="y",
            target_kind="function",
            metadata={"api_version": "v2"},
        )
        store.add_mapping(m)
        d = mapping_to_dict(m)
        assert d["source_project"] == "a"
        assert d["metadata"] == {"api_version": "v2"}

    def test_yaml_persistence(self, store, tmp_path):
        """Mappings should be persisted to YAML and human-readable."""
        store.add_mapping(IdentifierMapping(
            source_project="svc-a",
            source_identifier="OrderService.submit",
            target_project="svc-b",
            target_identifier="POST /orders",
            relationship="implements",
        ))

        yaml_path = store.yaml_path
        assert yaml_path.exists()
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert len(data["identifier_mappings"]) == 1
        assert data["identifier_mappings"][0]["source_identifier"] == "OrderService.submit"


# ---------------------------------------------------------------------------
# Module mappings
# ---------------------------------------------------------------------------


class TestModuleMappings:
    def test_add_and_get(self, store):
        m = ModuleMapping(
            module_prefix="payments",
            repo="org/payments-service",
            subpath="src/payments",
        )
        store.add_module_mapping(m)

        saved = store.get_module_mapping_by_prefix("payments")
        assert saved is not None
        assert saved.module_prefix == "payments"
        assert saved.repo == "org/payments-service"
        assert saved.subpath == "src/payments"

    def test_list(self, store):
        store.add_module_mapping(ModuleMapping(module_prefix="a", local_path="/a"))
        store.add_module_mapping(ModuleMapping(module_prefix="b.c", local_path="/bc"))
        store.add_module_mapping(ModuleMapping(module_prefix="b", local_path="/b"))

        results = store.list_module_mappings()
        assert len(results) == 3
        # Longest prefix first
        assert results[0].module_prefix == "b.c"

    def test_resolve_module_exact(self, store):
        store.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        mm = store.resolve_module("payments")
        assert mm is not None
        assert mm.module_prefix == "payments"

    def test_resolve_module_prefix(self, store):
        store.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        mm = store.resolve_module("payments.models.Order")
        assert mm is not None
        assert mm.module_prefix == "payments"

    def test_resolve_module_longest_prefix_wins(self, store):
        store.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        store.add_module_mapping(ModuleMapping(module_prefix="payments.models", local_path="/pay-models"))

        mm = store.resolve_module("payments.models.Order")
        assert mm.module_prefix == "payments.models"

        mm2 = store.resolve_module("payments.api")
        assert mm2.module_prefix == "payments"

    def test_resolve_module_no_match(self, store):
        store.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        assert store.resolve_module("users.models") is None

    def test_resolve_module_to_path_local(self, store, tmp_path):
        # Create a local directory structure.
        pay_dir = tmp_path / "payments"
        pay_dir.mkdir(parents=True)
        (pay_dir / "models.py").write_text("class Order: pass\n")
        (pay_dir / "__init__.py").write_text("")

        store.add_module_mapping(ModuleMapping(
            module_prefix="payments",
            local_path=str(pay_dir),
        ))

        # Resolve the prefix itself -> the package dir
        path = store.resolve_module_to_path("payments")
        assert path == str(pay_dir)

        # Resolve to submodule file
        path = store.resolve_module_to_path("payments.models")
        assert path.endswith("models.py")

    def test_resolve_module_to_path_with_subpath(self, store, tmp_path):
        repo_dir = tmp_path / "repo"
        src_dir = repo_dir / "src" / "payments"
        src_dir.mkdir(parents=True)
        (src_dir / "api.py").write_text("")

        store.add_module_mapping(ModuleMapping(
            module_prefix="payments",
            local_path=str(repo_dir),
            subpath="src/payments",
        ))

        path = store.resolve_module_to_path("payments.api")
        assert path.endswith("api.py")

    def test_delete(self, store):
        store.add_module_mapping(ModuleMapping(module_prefix="x", local_path="/x"))
        assert store.delete_module_mapping_by_prefix("x") is True
        assert store.get_module_mapping_by_prefix("x") is None

    def test_update(self, store):
        store.add_module_mapping(ModuleMapping(module_prefix="x", local_path="/x"))
        ok = store.update_module_mapping("x", repo="org/new-repo", local_path="")
        assert ok is True
        saved = store.get_module_mapping_by_prefix("x")
        assert saved.repo == "org/new-repo"

    def test_module_mapping_to_dict(self, store):
        m = ModuleMapping(
            module_prefix="test", repo="org/test",
            metadata={"env": "staging"},
        )
        store.add_module_mapping(m)
        saved = store.get_module_mapping_by_prefix("test")
        d = module_mapping_to_dict(saved)
        assert d["module_prefix"] == "test"
        assert d["metadata"] == {"env": "staging"}

    def test_unique_prefix_replaces(self, store):
        """Adding a module mapping with same prefix replaces the old one."""
        store.add_module_mapping(ModuleMapping(module_prefix="dup", local_path="/a"))
        store.add_module_mapping(ModuleMapping(module_prefix="dup", local_path="/b"))
        results = store.list_module_mappings()
        dup_results = [m for m in results if m.module_prefix == "dup"]
        assert len(dup_results) == 1
        assert dup_results[0].local_path == "/b"

    def test_get_by_prefix(self, store):
        """get_module_mapping_by_prefix looks up by exact prefix."""
        store.add_module_mapping(ModuleMapping(module_prefix="byprefix", local_path="/bp"))
        result = store.get_module_mapping_by_prefix("byprefix")
        assert result is not None
        assert result.module_prefix == "byprefix"
        assert store.get_module_mapping_by_prefix("nonexistent") is None

    def test_delete_by_prefix(self, store):
        """delete_module_mapping_by_prefix removes the mapping."""
        store.add_module_mapping(ModuleMapping(module_prefix="delpfx", local_path="/dp"))
        assert store.delete_module_mapping_by_prefix("delpfx") is True
        assert store.get_module_mapping_by_prefix("delpfx") is None
        assert store.delete_module_mapping_by_prefix("delpfx") is False  # already deleted

    def test_yaml_persistence(self, store, tmp_path):
        """Module mappings should be persisted to YAML."""
        store.add_module_mapping(ModuleMapping(
            module_prefix="payments",
            repo="org/payments-service",
            subpath="src/payments",
        ))

        yaml_path = store.yaml_path
        assert yaml_path.exists()
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert len(data["module_mappings"]) == 1
        assert data["module_mappings"][0]["module_prefix"] == "payments"


class TestYamlLocation:
    def test_yaml_in_emend_dir(self, tmp_path):
        """mappings.yaml should live in .emend/."""
        store = MappingStore(str(tmp_path))
        store.add_module_mapping(ModuleMapping(module_prefix="test", local_path="/test"))
        assert store.yaml_path == tmp_path / ".emend" / "mappings.yaml"
        assert store.yaml_path.exists()


class TestSqliteMigration:
    def test_migration_from_sqlite(self, tmp_path):
        """If knowledge.db exists, data should be migrated to YAML on first load."""
        import sqlite3

        # Create an old-location SQLite DB with some data.
        emend_dir = tmp_path / ".emend"
        emend_dir.mkdir(parents=True)
        db_path = emend_dir / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS identifier_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_project TEXT NOT NULL,
                source_identifier TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT '',
                target_project TEXT NOT NULL,
                target_identifier TEXT NOT NULL,
                target_kind TEXT NOT NULL DEFAULT '',
                relationship TEXT NOT NULL DEFAULT 'equivalent',
                confidence REAL NOT NULL DEFAULT 1.0,
                provenance TEXT NOT NULL DEFAULT 'manual',
                evidence TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS module_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module_prefix TEXT NOT NULL UNIQUE,
                repo TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL DEFAULT '',
                branch TEXT NOT NULL DEFAULT '',
                subpath TEXT NOT NULL DEFAULT '',
                provenance TEXT NOT NULL DEFAULT 'manual',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.execute(
            "INSERT INTO identifier_mapping "
            "(source_project, source_identifier, target_project, target_identifier, created_at, updated_at) "
            "VALUES ('svc-a', 'Foo.bar', 'svc-b', 'POST /foo', '', '')"
        )
        conn.execute(
            "INSERT INTO module_mapping "
            "(module_prefix, local_path, created_at, updated_at) "
            "VALUES ('payments', '/pay', '', '')"
        )
        conn.commit()
        conn.close()

        # Opening MappingStore should migrate to YAML
        store = MappingStore(str(tmp_path))

        # Verify identifier mappings migrated
        results = store.search_mappings("Foo")
        assert len(results) == 1
        assert results[0].source_identifier == "Foo.bar"

        # Verify module mappings migrated
        mm = store.get_module_mapping_by_prefix("payments")
        assert mm is not None
        assert mm.local_path == "/pay"


# ---------------------------------------------------------------------------
# Repo checkout layout
# ---------------------------------------------------------------------------


def _make_test_repo(path, branch="main"):
    """Create a minimal git repo for testing (with signing disabled)."""
    import subprocess
    path.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
    }
    run = lambda cmd, **kw: subprocess.run(
        cmd, cwd=str(path), check=True, capture_output=True, env=env, **kw
    )
    run(["git", "init"])
    run(["git", "checkout", "-b", branch])
    run(["git", "config", "commit.gpgsign", "false"])
    (path / "hello.py").write_text("print('hello')\n")
    run(["git", "add", "."])
    run(["git", "commit", "--no-gpg-sign", "-m", "init"])


def _setup_bare_clone(tmp_path, repo_id):
    """Create a source repo and bare-clone it into the cache layout for testing."""
    import subprocess
    from emend.knowledge import _repo_id
    src_repo = tmp_path / "source"
    _make_test_repo(src_repo)
    cache = tmp_path / "cache"
    rid = _repo_id(repo_id)
    contents_dir = cache / rid / "contents"
    contents_dir.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "--bare", str(src_repo), str(contents_dir)],
                   check=True, capture_output=True)
    return cache


class TestRepoCheckouts:
    def test_repo_id_normalizes_slashes(self):
        from emend.knowledge import _repo_id
        assert _repo_id("org/repo-name") == "org--repo-name"
        assert _repo_id("deep/nested/repo") == "deep--nested--repo"

    def test_repo_checkouts_root_default(self, monkeypatch, tmp_path):
        from pathlib import Path as _Path
        from emend.knowledge import _global_cache_dir, _repo_checkouts_root
        monkeypatch.delenv("EMEND_CACHE_DIR", raising=False)
        _global_cache_dir.cache_clear()
        monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))
        root = _repo_checkouts_root()
        assert root == tmp_path / ".cache" / "emend" / "repo-checkouts"

    def test_repo_checkouts_root_env_var(self, monkeypatch, tmp_path):
        from emend.knowledge import _global_cache_dir, _repo_checkouts_root
        monkeypatch.setenv("EMEND_CACHE_DIR", str(tmp_path / "custom"))
        _global_cache_dir.cache_clear()
        root = _repo_checkouts_root()
        assert root == tmp_path / "custom" / "repo-checkouts"

    def test_repo_checkouts_root_override(self):
        from pathlib import Path as _Path
        from emend.knowledge import _repo_checkouts_root
        root = _repo_checkouts_root(cache_dir="/custom/cache")
        assert root == _Path("/custom/cache")

    def test_maybe_fetch_branch_ttl(self, tmp_path):
        """_maybe_fetch_branch respect TTL and skips if recent."""
        from emend.knowledge import _maybe_fetch_branch
        import time
        from unittest.mock import patch

        bare_dir = tmp_path / "bare"
        bare_dir.mkdir()
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        last_fetched = worktree_dir / ".last_fetched"
        # 1 hour ago
        last_fetched.touch()
        mtime = time.time() - 3600
        os.utime(last_fetched, (mtime, mtime))

        with patch("subprocess.run") as mock_run:
            _maybe_fetch_branch(bare_dir, worktree_dir, "main", ttl_hours=24)
            # Should NOT have run any git commands
            assert mock_run.call_count == 0

            # Older than TTL
            mtime = time.time() - (25 * 3600)
            os.utime(last_fetched, (mtime, mtime))
            _maybe_fetch_branch(bare_dir, worktree_dir, "main", ttl_hours=24)
            # Should HAVE run git commands
            assert mock_run.call_count >= 1

    def test_ensure_repo_cloned_worktree_layout(self, tmp_path):
        """Test the full clone+worktree flow using a local git repo as source."""
        from emend.knowledge import _ensure_repo_cloned, _repo_id

        cache = _setup_bare_clone(tmp_path, "test/repo")
        result = _ensure_repo_cloned(
            "test/repo", branch="main", cache_dir=str(cache),
        )

        expected = cache / _repo_id("test/repo") / "checkouts" / "main"
        assert result == str(expected)
        assert expected.is_dir()
        assert (expected / "hello.py").is_file()

    def test_ensure_repo_cloned_reuses_worktree(self, tmp_path):
        """Calling _ensure_repo_cloned twice returns the same worktree dir."""
        from emend.knowledge import _ensure_repo_cloned

        cache = _setup_bare_clone(tmp_path, "org/proj")
        r1 = _ensure_repo_cloned("org/proj", branch="main", cache_dir=str(cache))
        r2 = _ensure_repo_cloned("org/proj", branch="main", cache_dir=str(cache))
        assert r1 == r2


# ---------------------------------------------------------------------------
# Editor-server RPC integration
# ---------------------------------------------------------------------------


class TestEditorServerRPC:
    def test_mapping_lookup_rpc(self, store):
        from emend.editor_search import _mapping_lookup

        class FakeEngine:
            project_root = str(store.yaml_path.parent.parent)
        engine = FakeEngine()
        engine._kb = store

        store.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="Foo.bar",
            source_kind="method",
            target_project="b", target_identifier="handle_bar",
            target_kind="function",
        ))

        result = _mapping_lookup(engine, {"identifier": "Foo.bar"})
        assert result["mode"] == "mapping_lookup"
        assert len(result["items"]) == 1

    def test_module_resolve_rpc(self, store, tmp_path):
        from emend.editor_search import _module_resolve

        class FakeEngine:
            project_root = str(store.yaml_path.parent.parent)
        engine = FakeEngine()
        engine._kb = store

        pay_dir = tmp_path / "external"
        pay_dir.mkdir()
        store.add_module_mapping(ModuleMapping(
            module_prefix="payments", local_path=str(pay_dir),
        ))

        result = _module_resolve(engine, {"module": "payments"})
        assert result["mode"] == "module_resolve"
        assert len(result["items"]) == 1
        assert result["items"][0]["resolved_path"] == str(pay_dir)

    def test_module_resolve_no_match(self, store):
        from emend.editor_search import _module_resolve

        class FakeEngine:
            project_root = str(store.yaml_path.parent.parent)
        engine = FakeEngine()
        engine._kb = store

        result = _module_resolve(engine, {"module": "nonexistent"})
        assert result["items"] == []

    def test_mapping_goto_definition_first(self, store):
        """mapping_goto returns local results when symbol exists in project index."""
        from emend.editor_search import SearchResult, _mapping_goto

        class FakeEngine:
            project_root = str(store.yaml_path.parent.parent)

            def search_symbols(self, query, *, limit=10):
                return SearchResult(
                    items=[{
                        "name": "MyClass",
                        "qualified_name": "mymod.MyClass",
                        "kind": "class",
                        "file_path": "/proj/mymod.py",
                        "line": 10,
                        "end_line": 50,
                    }],
                    elapsed_ms=1.0,
                    mode="symbol",
                )

        engine = FakeEngine()
        engine._kb = store

        result = _mapping_goto(engine, {"identifier": "mymod.MyClass"})
        assert result["source"] == "local"
        assert len(result["items"]) == 1
        assert result["items"][0]["file_path"] == "/proj/mymod.py"
        assert result["items"][0]["line"] == 10

    def test_mapping_goto_falls_back_to_mappings(self, store):
        """mapping_goto falls back to mappings when no local match is found."""
        from emend.editor_search import SearchResult, _mapping_goto

        class FakeEngine:
            project_root = str(store.yaml_path.parent.parent)

            def search_symbols(self, query, *, limit=10):
                return SearchResult(items=[], elapsed_ms=1.0, mode="symbol")

        engine = FakeEngine()
        engine._kb = store

        store.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="OrderService.create",
            source_kind="method",
            target_project="b", target_identifier="order_handler.create",
            target_kind="function",
        ))

        result = _mapping_goto(engine, {"identifier": "OrderService.create"})
        assert result["source"] == "kb"
        assert len(result["items"]) == 1
        assert result["items"][0]["source_identifier"] == "OrderService.create"

    def test_mapping_goto_filters_fuzzy_local_hits(self, store):
        """mapping_goto only returns local results that match the identifier exactly."""
        from emend.editor_search import SearchResult, _mapping_goto

        class FakeEngine:
            project_root = str(store.yaml_path.parent.parent)

            def search_symbols(self, query, *, limit=10):
                return SearchResult(
                    items=[{
                        "name": "MyClassHelper",
                        "qualified_name": "mymod.MyClassHelper",
                        "kind": "class",
                        "file_path": "/proj/mymod.py",
                        "line": 10,
                        "end_line": 50,
                    }],
                    elapsed_ms=1.0,
                    mode="symbol",
                )

        engine = FakeEngine()
        engine._kb = store

        result = _mapping_goto(engine, {"identifier": "MyClass"})
        assert result["source"] == "kb"
        assert len(result["items"]) == 0

    def test_mapping_goto_import_resolution(self, store, tmp_path):
        """mapping_goto resolves a symbol via local imports and module mapping."""
        from emend.editor_search import SearchResult, _mapping_goto
        from emend.knowledge import ModuleMapping

        # 1. Setup module mapping: 'common.models' -> '/repo/common/models'
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "common").mkdir()
        (repo_path / "common" / "models").mkdir()
        user_py = repo_path / "common" / "models" / "user.py"
        user_py.write_text("class User: pass\n")

        store.add_module_mapping(ModuleMapping(
            module_prefix="common.models",
            local_path=str(repo_path / "common" / "models"),
        ))

        # 2. Setup local file with import
        local_file = tmp_path / "app.py"
        local_file.write_text("from common.models.user import User\n")

        class FakeEngine:
            project_root = str(tmp_path)
            def search_symbols(self, query, *, limit=10):
                return SearchResult(items=[], elapsed_ms=1.0, mode="symbol")

        engine = FakeEngine()
        engine._kb = store

        # 3. Request 'User' from 'app.py'
        result = _mapping_goto(engine, {
            "identifier": "User",
            "file": str(local_file)
        })

        assert result["source"] == "kb"
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["name"] == "User"
        assert item["file_path"] == str(user_py)
        assert item["line"] == 1

    def test_mapping_goto_import_reexport(self, store, tmp_path):
        """mapping_goto follows re-exports in __init__.py via module mapping."""
        from emend.editor_search import SearchResult, _mapping_goto
        from emend.knowledge import ModuleMapping

        repo_path = tmp_path / "repo"
        (repo_path / "common").mkdir(parents=True)
        init_py = repo_path / "common" / "__init__.py"
        init_py.write_text("from .models import User\n")
        (repo_path / "common" / "models.py").write_text("class User: pass\n")

        store.add_module_mapping(ModuleMapping(
            module_prefix="common",
            local_path=str(repo_path / "common"),
        ))

        local_file = tmp_path / "app.py"
        local_file.write_text("from common import User\n")

        class FakeEngine:
            project_root = str(tmp_path)
            def search_symbols(self, query, *, limit=10):
                return SearchResult(items=[], elapsed_ms=1.0, mode="symbol")

        engine = FakeEngine()
        engine._kb = store

        result = _mapping_goto(engine, {
            "identifier": "User",
            "file": str(local_file)
        })

        assert len(result["items"]) == 1
        assert result["items"][0]["file_path"] == str(repo_path / "common" / "models.py")

    def test_mapping_goto_no_file_param(self, store):
        """mapping_goto skips Tier 3 if 'file' param is missing."""
        from emend.editor_search import SearchResult, _mapping_goto
        class FakeEngine:
            def search_symbols(self, query, *, limit=10):
                return SearchResult(items=[], elapsed_ms=1.0, mode="symbol")
        engine = FakeEngine()
        engine._kb = store

        result = _mapping_goto(engine, {"identifier": "User"})
        assert result["source"] == "kb"
        assert len(result["items"]) == 0

    def test_resolve_selector_to_goto_item(self, tmp_path):
        from emend.editor_search import _resolve_selector_to_goto_item
        f = tmp_path / "mod.py"
        f.write_text("class MyClass:\n    pass\n")

        class FakeEngine:
            def __init__(self, root):
                self.project_root = str(root)
        engine = FakeEngine(tmp_path)
        # Need a store on the engine for make_resolve_module_cb
        engine._kb = MappingStore(str(tmp_path))

        # Exact match
        res = _resolve_selector_to_goto_item(engine, f"{f}::MyClass")
        assert res["file_path"] == str(f)
        assert res["line"] == 1

        # Missing file
        assert _resolve_selector_to_goto_item(engine, "/none::X") is None
        # Invalid selector
        assert _resolve_selector_to_goto_item(engine, "not-a-selector") is None


# ---------------------------------------------------------------------------
# MCP tool integration
# ---------------------------------------------------------------------------


def _has_pydantic():
    try:
        import pydantic  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_pydantic(), reason="pydantic not installed (MCP optional dep)")
class TestMCPTools:
    def test_map_write_add_and_read_mapping(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import map_read, map_write

        result = map_write(
            kind="mapping", op="add",
            entry={
                "source_project": "user-svc",
                "source_identifier": "UserService.create_user",
                "target_project": "gateway",
                "target_identifier": "POST /users",
                "relationship": "implements",
                "evidence": "The create_user method handles POST /users requests.",
            },
        )
        data = json.loads(result)
        assert data["source_identifier"] == "UserService.create_user"

        search_result = map_read(kind="mapping", query="create_user")
        results = json.loads(search_result)
        assert len(results) >= 1

        # Exact identifier lookup via query (no spaces → exact lookup)
        lookup_result = map_read(kind="mapping", query="UserService.create_user")
        results = json.loads(lookup_result)
        assert len(results) >= 1

    def test_map_write_delete(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import map_read, map_write

        map_write(
            kind="mapping", op="add",
            entry={
                "source_project": "svc",
                "source_identifier": "Foo.bar",
                "target_project": "gw",
                "target_identifier": "POST /foo",
            },
        )

        result = map_write(
            kind="mapping", op="delete",
            entry={"source_identifier": "Foo.bar"},
        )
        data = json.loads(result)
        assert data["deleted"] is True

        results = json.loads(map_read(kind="mapping", query="Foo"))
        assert len(results) == 0
