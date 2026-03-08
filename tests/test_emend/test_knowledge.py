"""Tests for the mapping knowledge base (knowledge.py)."""

from __future__ import annotations

import json

import pytest

from emend.knowledge import (
    IdentifierMapping,
    KnowledgeBase,
    KnowledgeNote,
    ModuleMapping,
    mapping_to_dict,
    module_mapping_to_dict,
    note_to_dict,
)


@pytest.fixture
def kb(tmp_path):
    """Create a KnowledgeBase rooted in a temp directory."""
    kb = KnowledgeBase(str(tmp_path))
    yield kb
    kb.close()


# ---------------------------------------------------------------------------
# Knowledge notes
# ---------------------------------------------------------------------------


class TestNotes:
    def test_add_and_get(self, kb):
        note = KnowledgeNote(
            title="Auth flow",
            content="Users authenticate via OAuth2 with the gateway.",
            category="architecture",
            tags="auth,oauth",
        )
        nid = kb.add_note(note)
        assert nid >= 1

        saved = kb.get_note(nid)
        assert saved is not None
        assert saved.title == "Auth flow"
        assert saved.category == "architecture"
        assert saved.tags == "auth,oauth"
        assert saved.created_at != ""

    def test_get_nonexistent(self, kb):
        assert kb.get_note(9999) is None

    def test_update(self, kb):
        nid = kb.add_note(KnowledgeNote(title="Draft", content="TBD"))
        ok = kb.update_note(nid, title="Final", content="Done", tags="v1")
        assert ok is True

        saved = kb.get_note(nid)
        assert saved.title == "Final"
        assert saved.content == "Done"
        assert saved.tags == "v1"

    def test_update_nonexistent(self, kb):
        assert kb.update_note(9999, title="x") is False

    def test_delete(self, kb):
        nid = kb.add_note(KnowledgeNote(title="Temp", content="..."))
        assert kb.delete_note(nid) is True
        assert kb.get_note(nid) is None

    def test_delete_is_soft(self, kb):
        """Deleted notes are soft-deleted (row still exists with deleted=1)."""
        nid = kb.add_note(KnowledgeNote(title="Soft", content="..."))
        kb.delete_note(nid)
        row = kb._conn.execute(
            "SELECT deleted FROM knowledge_note WHERE id = ?", (nid,)
        ).fetchone()
        assert row is not None
        assert row["deleted"] == 1

    def test_delete_nonexistent(self, kb):
        assert kb.delete_note(9999) is False

    def test_search_fts(self, kb):
        kb.add_note(KnowledgeNote(title="Database schema", content="Uses PostgreSQL with JSONB columns."))
        kb.add_note(KnowledgeNote(title="API design", content="REST endpoints follow OpenAPI 3.0."))
        kb.add_note(KnowledgeNote(title="Caching", content="Redis for session caching."))

        results = kb.search_notes("PostgreSQL")
        assert len(results) == 1
        assert results[0].title == "Database schema"

    def test_search_by_category(self, kb):
        kb.add_note(KnowledgeNote(title="A", content="x", category="architecture"))
        kb.add_note(KnowledgeNote(title="B", content="x", category="convention"))

        results = kb.search_notes("x", category="architecture")
        assert all(n.category == "architecture" for n in results)

    def test_search_by_symbol(self, kb):
        kb.add_note(KnowledgeNote(title="A", content="note about create_user", symbol="create_user"))
        kb.add_note(KnowledgeNote(title="B", content="note about delete_user", symbol="delete_user"))

        results = kb.search_notes("create_user", symbol="create_user")
        assert len(results) == 1

    def test_list_notes(self, kb):
        kb.add_note(KnowledgeNote(title="A", content="x", category="note"))
        kb.add_note(KnowledgeNote(title="B", content="y", category="architecture"))
        kb.add_note(KnowledgeNote(title="C", content="z", category="note"))

        all_notes = kb.list_notes()
        assert len(all_notes) == 3

        filtered = kb.list_notes(category="note")
        assert len(filtered) == 2

    def test_note_metadata(self, kb):
        note = KnowledgeNote(
            title="With meta",
            content="test",
            metadata={"version": 2, "reviewed": True},
        )
        nid = kb.add_note(note)
        saved = kb.get_note(nid)
        assert saved.metadata == {"version": 2, "reviewed": True}

    def test_list_tags(self, kb):
        kb.add_note(KnowledgeNote(title="A", content="x", tags="auth,oauth"))
        kb.add_note(KnowledgeNote(title="B", content="y", tags="auth,db"))
        kb.add_note(KnowledgeNote(title="C", content="z", tags=""))

        tags = kb.list_tags()
        assert tags == ["auth", "db", "oauth"]

    def test_list_tags_excludes_deleted(self, kb):
        nid = kb.add_note(KnowledgeNote(title="D", content="w", tags="secret"))
        kb.delete_note(nid)
        assert "secret" not in kb.list_tags()

    def test_note_to_dict(self, kb):
        nid = kb.add_note(KnowledgeNote(title="T", content="C"))
        saved = kb.get_note(nid)
        d = note_to_dict(saved)
        assert d["title"] == "T"
        assert d["id"] == nid
        assert isinstance(d["metadata"], dict)


# ---------------------------------------------------------------------------
# Identifier mappings
# ---------------------------------------------------------------------------


class TestMappings:
    def test_add_and_get(self, kb):
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
        mid = kb.add_mapping(m)
        assert mid >= 1

        saved = kb.get_mapping(mid)
        assert saved is not None
        assert saved.source_project == "user-service"
        assert saved.target_identifier == "POST /api/v1/users"
        assert saved.confidence == 0.95
        assert saved.relationship == "implements"

    def test_get_nonexistent(self, kb):
        assert kb.get_mapping(9999) is None

    def test_update(self, kb):
        mid = kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="x",
            source_kind="function",
            target_project="b", target_identifier="y",
            target_kind="function",
        ))
        ok = kb.update_mapping(mid, confidence=0.5, evidence="updated")
        assert ok is True

        saved = kb.get_mapping(mid)
        assert saved.confidence == 0.5
        assert saved.evidence == "updated"

    def test_update_nonexistent(self, kb):
        assert kb.update_mapping(9999, evidence="x") is False

    def test_delete(self, kb):
        mid = kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="x",
            source_kind="", target_project="b",
            target_identifier="y", target_kind="",
        ))
        assert kb.delete_mapping(mid) is True
        assert kb.get_mapping(mid) is None

    def test_delete_is_soft(self, kb):
        """Deleted mappings are soft-deleted."""
        mid = kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="x",
            source_kind="", target_project="b",
            target_identifier="y", target_kind="",
        ))
        kb.delete_mapping(mid)
        row = kb._conn.execute(
            "SELECT deleted FROM identifier_mapping WHERE id = ?", (mid,)
        ).fetchone()
        assert row is not None
        assert row["deleted"] == 1

    def test_delete_nonexistent(self, kb):
        assert kb.delete_mapping(9999) is False

    def test_search_fts(self, kb):
        kb.add_mapping(IdentifierMapping(
            source_project="svc-a", source_identifier="OrderService.submit",
            source_kind="method",
            target_project="svc-b", target_identifier="POST /orders",
            target_kind="endpoint",
            evidence="Submit order creates an order via the API.",
        ))
        kb.add_mapping(IdentifierMapping(
            source_project="svc-a", source_identifier="OrderService.cancel",
            source_kind="method",
            target_project="svc-b", target_identifier="DELETE /orders/{id}",
            target_kind="endpoint",
        ))

        results = kb.search_mappings("submit")
        assert len(results) == 1
        assert "submit" in results[0].source_identifier.lower()

    def test_search_with_filters(self, kb):
        kb.add_mapping(IdentifierMapping(
            source_project="alpha", source_identifier="foo",
            source_kind="", target_project="beta",
            target_identifier="bar", target_kind="",
            relationship="calls",
        ))
        kb.add_mapping(IdentifierMapping(
            source_project="alpha", source_identifier="baz",
            source_kind="", target_project="gamma",
            target_identifier="qux", target_kind="",
            relationship="equivalent",
        ))

        results = kb.search_mappings("", source_project="alpha", relationship="calls")
        assert all(m.relationship == "calls" for m in results)

    def test_find_mappings_for(self, kb):
        kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="Foo.bar",
            source_kind="method",
            target_project="b", target_identifier="handle_bar",
            target_kind="function",
        ))
        kb.add_mapping(IdentifierMapping(
            source_project="c", source_identifier="invoke_bar",
            source_kind="function",
            target_project="a", target_identifier="Foo.bar",
            target_kind="method",
        ))

        # Both directions
        results = kb.find_mappings_for("Foo.bar")
        assert len(results) == 2

        # Source only
        results = kb.find_mappings_for("Foo.bar", direction="source")
        assert len(results) == 1
        assert results[0].target_identifier == "handle_bar"

        # Target only
        results = kb.find_mappings_for("Foo.bar", direction="target")
        assert len(results) == 1
        assert results[0].source_identifier == "invoke_bar"

    def test_find_mappings_for_with_project(self, kb):
        kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="X",
            source_kind="", target_project="b",
            target_identifier="Y", target_kind="",
        ))
        kb.add_mapping(IdentifierMapping(
            source_project="c", source_identifier="X",
            source_kind="", target_project="d",
            target_identifier="Z", target_kind="",
        ))

        results = kb.find_mappings_for("X", project="a", direction="source")
        assert len(results) == 1
        assert results[0].target_identifier == "Y"

    def test_list_mappings(self, kb):
        for i in range(5):
            kb.add_mapping(IdentifierMapping(
                source_project="p", source_identifier=f"id_{i}",
                source_kind="", target_project="q",
                target_identifier=f"tid_{i}", target_kind="",
            ))

        all_m = kb.list_mappings()
        assert len(all_m) == 5

        limited = kb.list_mappings(limit=2)
        assert len(limited) == 2

    def test_mapping_to_dict(self, kb):
        mid = kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="x",
            source_kind="function",
            target_project="b", target_identifier="y",
            target_kind="function",
            metadata={"api_version": "v2"},
        ))
        saved = kb.get_mapping(mid)
        d = mapping_to_dict(saved)
        assert d["id"] == mid
        assert d["metadata"] == {"api_version": "v2"}


# ---------------------------------------------------------------------------
# Module mappings
# ---------------------------------------------------------------------------


class TestModuleMappings:
    def test_add_and_get(self, kb):
        m = ModuleMapping(
            module_prefix="payments",
            repo="org/payments-service",
            subpath="src/payments",
        )
        mid = kb.add_module_mapping(m)
        assert mid >= 1

        saved = kb.get_module_mapping(mid)
        assert saved is not None
        assert saved.module_prefix == "payments"
        assert saved.repo == "org/payments-service"
        assert saved.subpath == "src/payments"

    def test_list(self, kb):
        kb.add_module_mapping(ModuleMapping(module_prefix="a", local_path="/a"))
        kb.add_module_mapping(ModuleMapping(module_prefix="b.c", local_path="/bc"))
        kb.add_module_mapping(ModuleMapping(module_prefix="b", local_path="/b"))

        results = kb.list_module_mappings()
        assert len(results) == 3
        # Longest prefix first
        assert results[0].module_prefix == "b.c"

    def test_resolve_module_exact(self, kb):
        kb.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        mm = kb.resolve_module("payments")
        assert mm is not None
        assert mm.module_prefix == "payments"

    def test_resolve_module_prefix(self, kb):
        kb.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        mm = kb.resolve_module("payments.models.Order")
        assert mm is not None
        assert mm.module_prefix == "payments"

    def test_resolve_module_longest_prefix_wins(self, kb):
        kb.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        kb.add_module_mapping(ModuleMapping(module_prefix="payments.models", local_path="/pay-models"))

        mm = kb.resolve_module("payments.models.Order")
        assert mm.module_prefix == "payments.models"

        mm2 = kb.resolve_module("payments.api")
        assert mm2.module_prefix == "payments"

    def test_resolve_module_no_match(self, kb):
        kb.add_module_mapping(ModuleMapping(module_prefix="payments", local_path="/pay"))
        assert kb.resolve_module("users.models") is None

    def test_resolve_module_to_path_local(self, kb, tmp_path):
        # Create a local directory structure.
        # local_path points directly to the package directory.
        pay_dir = tmp_path / "payments"
        pay_dir.mkdir(parents=True)
        (pay_dir / "models.py").write_text("class Order: pass\n")
        (pay_dir / "__init__.py").write_text("")

        kb.add_module_mapping(ModuleMapping(
            module_prefix="payments",
            local_path=str(pay_dir),
        ))

        # Resolve the prefix itself -> the package dir
        path = kb.resolve_module_to_path("payments")
        assert path == str(pay_dir)

        # Resolve to submodule file
        path = kb.resolve_module_to_path("payments.models")
        assert path.endswith("models.py")

    def test_resolve_module_to_path_with_subpath(self, kb, tmp_path):
        repo_dir = tmp_path / "repo"
        src_dir = repo_dir / "src" / "payments"
        src_dir.mkdir(parents=True)
        (src_dir / "api.py").write_text("")

        kb.add_module_mapping(ModuleMapping(
            module_prefix="payments",
            local_path=str(repo_dir),
            subpath="src/payments",
        ))

        path = kb.resolve_module_to_path("payments.api")
        assert path.endswith("api.py")

    def test_delete(self, kb):
        mid = kb.add_module_mapping(ModuleMapping(module_prefix="x", local_path="/x"))
        assert kb.delete_module_mapping(mid) is True
        assert kb.get_module_mapping(mid) is None

    def test_update(self, kb):
        mid = kb.add_module_mapping(ModuleMapping(module_prefix="x", local_path="/x"))
        ok = kb.update_module_mapping(mid, repo="org/new-repo", local_path="")
        assert ok is True
        saved = kb.get_module_mapping(mid)
        assert saved.repo == "org/new-repo"

    def test_module_mapping_to_dict(self, kb):
        mid = kb.add_module_mapping(ModuleMapping(
            module_prefix="test", repo="org/test",
            metadata={"env": "staging"},
        ))
        saved = kb.get_module_mapping(mid)
        d = module_mapping_to_dict(saved)
        assert d["module_prefix"] == "test"
        assert d["metadata"] == {"env": "staging"}

    def test_delete_is_soft(self, kb):
        """Deleted module mappings are soft-deleted."""
        mid = kb.add_module_mapping(ModuleMapping(module_prefix="soft", local_path="/s"))
        kb.delete_module_mapping(mid)
        row = kb._conn.execute(
            "SELECT deleted FROM module_mapping WHERE id = ?", (mid,)
        ).fetchone()
        assert row is not None
        assert row["deleted"] == 1

    def test_unique_prefix(self, kb):
        """module_prefix has UNIQUE constraint (for non-deleted rows)."""
        kb.add_module_mapping(ModuleMapping(module_prefix="dup", local_path="/a"))
        with pytest.raises(Exception):
            kb.add_module_mapping(ModuleMapping(module_prefix="dup", local_path="/b"))

    def test_add_undeletes_soft_deleted(self, kb):
        """Adding a mapping with a soft-deleted prefix undeletes it."""
        mid = kb.add_module_mapping(ModuleMapping(module_prefix="revive", local_path="/old"))
        kb.delete_module_mapping(mid)
        assert kb.get_module_mapping(mid) is None

        mid2 = kb.add_module_mapping(ModuleMapping(module_prefix="revive", local_path="/new"))
        assert mid2 == mid  # same row reused
        saved = kb.get_module_mapping(mid2)
        assert saved is not None
        assert saved.local_path == "/new"

    def test_get_by_prefix(self, kb):
        """get_module_mapping_by_prefix looks up by exact prefix."""
        kb.add_module_mapping(ModuleMapping(module_prefix="byprefix", local_path="/bp"))
        result = kb.get_module_mapping_by_prefix("byprefix")
        assert result is not None
        assert result.module_prefix == "byprefix"
        assert kb.get_module_mapping_by_prefix("nonexistent") is None

    def test_delete_by_prefix(self, kb):
        """delete_module_mapping_by_prefix soft-deletes by prefix string."""
        kb.add_module_mapping(ModuleMapping(module_prefix="delpfx", local_path="/dp"))
        assert kb.delete_module_mapping_by_prefix("delpfx") is True
        assert kb.get_module_mapping_by_prefix("delpfx") is None
        assert kb.delete_module_mapping_by_prefix("delpfx") is False  # already deleted


class TestKnowledgeDbLocation:
    def test_db_in_emend_dir(self, tmp_path):
        """knowledge.db should live in .emend/, not .emend/cache/."""
        kb = KnowledgeBase(str(tmp_path))
        try:
            assert kb.db_path == tmp_path / ".emend" / "knowledge.db"
            assert kb.db_path.exists()
        finally:
            kb.close()

    def test_migration_from_cache(self, tmp_path):
        """If knowledge.db exists in .emend/cache/, it gets migrated."""
        import sqlite3

        # Create an old-location DB with some data.
        old_dir = tmp_path / ".emend" / "cache"
        old_dir.mkdir(parents=True)
        old_path = old_dir / "knowledge.db"
        conn = sqlite3.connect(str(old_path))
        conn.execute("CREATE TABLE test_marker (val TEXT)")
        conn.execute("INSERT INTO test_marker VALUES ('migrated')")
        conn.commit()
        conn.close()

        # Opening KnowledgeBase should migrate it.
        kb = KnowledgeBase(str(tmp_path))
        try:
            new_path = tmp_path / ".emend" / "knowledge.db"
            assert new_path.exists()
            assert not old_path.exists()  # moved, not copied
            # Verify migrated data survived.
            row = kb._conn.execute("SELECT val FROM test_marker").fetchone()
            assert row[0] == "migrated"
        finally:
            kb.close()


# ---------------------------------------------------------------------------
# Repo checkout layout
# ---------------------------------------------------------------------------


def _make_test_repo(path, branch="main"):
    """Create a minimal git repo for testing (with signing disabled)."""
    import os
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
    def test_kb_search_rpc(self, kb):
        """Test the kb_search RPC handler via _dispatch."""
        from emend.editor_search import _kb_search

        # We need a mock engine with project_root
        class FakeEngine:
            project_root = str(kb.db_path.parent.parent.parent)
        engine = FakeEngine()

        kb.add_note(KnowledgeNote(title="Auth flow", content="OAuth2 based auth"))
        # Attach the kb to the engine (simulating _get_kb)
        engine._kb = kb

        result = _kb_search(engine, {"query": "OAuth2"})
        assert result["mode"] == "kb_search"
        assert len(result["items"]) == 1
        assert result["items"][0]["title"] == "Auth flow"

    def test_mapping_lookup_rpc(self, kb):
        from emend.editor_search import _mapping_lookup

        class FakeEngine:
            project_root = str(kb.db_path.parent.parent.parent)
        engine = FakeEngine()
        engine._kb = kb

        kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="Foo.bar",
            source_kind="method",
            target_project="b", target_identifier="handle_bar",
            target_kind="function",
        ))

        result = _mapping_lookup(engine, {"identifier": "Foo.bar"})
        assert result["mode"] == "mapping_lookup"
        assert len(result["items"]) == 1

    def test_module_resolve_rpc(self, kb, tmp_path):
        from emend.editor_search import _module_resolve

        class FakeEngine:
            project_root = str(kb.db_path.parent.parent.parent)
        engine = FakeEngine()
        engine._kb = kb

        pay_dir = tmp_path / "external"
        pay_dir.mkdir()
        kb.add_module_mapping(ModuleMapping(
            module_prefix="payments", local_path=str(pay_dir),
        ))

        result = _module_resolve(engine, {"module": "payments"})
        assert result["mode"] == "module_resolve"
        assert len(result["items"]) == 1
        assert result["items"][0]["resolved_path"] == str(pay_dir)

    def test_module_resolve_no_match(self, kb):
        from emend.editor_search import _module_resolve

        class FakeEngine:
            project_root = str(kb.db_path.parent.parent.parent)
        engine = FakeEngine()
        engine._kb = kb

        result = _module_resolve(engine, {"module": "nonexistent"})
        assert result["items"] == []

    def test_mapping_goto_local_first(self, kb):
        """mapping_goto returns local results when symbol exists in project index."""
        from emend.editor_search import SearchResult, _mapping_goto

        class FakeEngine:
            project_root = str(kb.db_path.parent.parent.parent)

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
        engine._kb = kb

        result = _mapping_goto(engine, {"identifier": "mymod.MyClass"})
        assert result["source"] == "local"
        assert len(result["items"]) == 1
        assert result["items"][0]["file_path"] == "/proj/mymod.py"
        assert result["items"][0]["line"] == 10

    def test_mapping_goto_falls_back_to_kb(self, kb):
        """mapping_goto falls back to KB when no local match is found."""
        from emend.editor_search import SearchResult, _mapping_goto

        class FakeEngine:
            project_root = str(kb.db_path.parent.parent.parent)

            def search_symbols(self, query, *, limit=10):
                # Return a result whose name doesn't match the identifier.
                return SearchResult(items=[], elapsed_ms=1.0, mode="symbol")

        engine = FakeEngine()
        engine._kb = kb

        kb.add_mapping(IdentifierMapping(
            source_project="a", source_identifier="OrderService.create",
            source_kind="method",
            target_project="b", target_identifier="order_handler.create",
            target_kind="function",
        ))

        result = _mapping_goto(engine, {"identifier": "OrderService.create"})
        assert result["source"] == "kb"
        assert len(result["items"]) == 1
        assert result["items"][0]["source_identifier"] == "OrderService.create"

    def test_mapping_goto_filters_fuzzy_local_hits(self, kb):
        """mapping_goto only returns local results that match the identifier exactly."""
        from emend.editor_search import SearchResult, _mapping_goto

        class FakeEngine:
            project_root = str(kb.db_path.parent.parent.parent)

            def search_symbols(self, query, *, limit=10):
                # Return a fuzzy hit that doesn't exactly match.
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
        engine._kb = kb

        result = _mapping_goto(engine, {"identifier": "MyClass"})
        # Fuzzy hit filtered out, falls back to KB (empty since no mapping).
        assert result["source"] == "kb"
        assert len(result["items"]) == 0

    def test_mapping_goto_import_resolution(self, kb, tmp_path):
        """mapping_goto resolves a symbol via local imports and KB module mapping."""
        from emend.editor_search import SearchResult, _mapping_goto
        from emend.knowledge import ModuleMapping

        # 1. Setup KB module mapping: 'common.models' -> '/repo/common/models'
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "common").mkdir()
        (repo_path / "common" / "models").mkdir()
        user_py = repo_path / "common" / "models" / "user.py"
        user_py.write_text("class User: pass\n")

        kb.add_module_mapping(ModuleMapping(
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
        engine._kb = kb

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

    def test_mapping_goto_import_reexport(self, kb, tmp_path):
        """mapping_goto follows re-exports in __init__.py via module mapping."""
        from emend.editor_search import SearchResult, _mapping_goto
        from emend.knowledge import ModuleMapping

        repo_path = tmp_path / "repo"
        (repo_path / "common").mkdir(parents=True)
        init_py = repo_path / "common" / "__init__.py"
        init_py.write_text("from .models import User\n")
        (repo_path / "common" / "models.py").write_text("class User: pass\n")

        kb.add_module_mapping(ModuleMapping(
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
        engine._kb = kb

        result = _mapping_goto(engine, {
            "identifier": "User",
            "file": str(local_file)
        })
        
        assert len(result["items"]) == 1
        assert result["items"][0]["file_path"] == str(repo_path / "common" / "models.py")

    def test_mapping_goto_import_reexport_submodule(self, kb, tmp_path):
        """mapping_goto follows 'from . import submodule as alias' re-exports."""
        from emend.editor_search import SearchResult, _mapping_goto
        from emend.knowledge import ModuleMapping

        repo_path = tmp_path / "repo"
        (repo_path / "common").mkdir(parents=True)
        init_py = repo_path / "common" / "__init__.py"
        # 'from . import models as mod' 
        init_py.write_text("from . import models as mod\n")
        (repo_path / "common" / "models.py").write_text("class User: pass\n")

        kb.add_module_mapping(ModuleMapping(
            module_prefix="common",
            local_path=str(repo_path / "common"),
        ))

        local_file = tmp_path / "app.py"
        local_file.write_text("from common import mod\n")

        class FakeEngine:
            project_root = str(tmp_path)
            def search_symbols(self, query, *, limit=10):
                return SearchResult(items=[], elapsed_ms=1.0, mode="symbol")

        engine = FakeEngine()
        engine._kb = kb

        result = _mapping_goto(engine, {
            "identifier": "mod",
            "file": str(local_file)
        })
        
        assert len(result["items"]) == 1
        assert result["items"][0]["file_path"] == str(repo_path / "common" / "models.py")

    def test_mapping_goto_no_file_param(self, kb):
        """mapping_goto skips Tier 3 if 'file' param is missing (backward compat)."""
        from emend.editor_search import SearchResult, _mapping_goto
        class FakeEngine:
            def search_symbols(self, query, *, limit=10):
                return SearchResult(items=[], elapsed_ms=1.0, mode="symbol")
        engine = FakeEngine()
        engine._kb = kb

        result = _mapping_goto(engine, {"identifier": "User"})
        assert result["source"] == "kb"
        assert len(result["items"]) == 0

    def test_extract_import_binding(self, tmp_path):
        from emend.editor_search import _extract_import_binding
        f = tmp_path / "test.py"
        f.write_text("\n".join([
            "import os",
            "from path import Path as P",
            "if TYPE_CHECKING:",
            "    from models import User",
        ]))
        
        assert _extract_import_binding(str(f), "os") == ("os", "os")
        assert _extract_import_binding(str(f), "P") == ("path", "Path")
        assert _extract_import_binding(str(f), "User") == ("models", "User")
        assert _extract_import_binding(str(f), "Missing") is None

    def test_resolve_selector_to_goto_item(self, tmp_path):
        from emend.editor_search import _resolve_selector_to_goto_item
        f = tmp_path / "mod.py"
        f.write_text("class MyClass:\n    pass\n")
        
        class FakeEngine: pass
        engine = FakeEngine()
        
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
    def test_kb_write_add_and_read_note(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import kb_read, kb_write

        result = kb_write(
            kind="note", op="add",
            title="Test note",
            content="This is a test knowledge note about authentication.",
            category="architecture",
            tags="auth,test",
        )
        data = json.loads(result)
        assert data["title"] == "Test note"
        assert "id" in data

        search_result = kb_read(kind="note", query="authentication")
        results = json.loads(search_result)
        assert len(results) >= 1
        assert results[0]["title"] == "Test note"

    def test_kb_write_add_and_read_mapping(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import kb_read, kb_write

        result = kb_write(
            kind="mapping", op="add",
            source_project="user-svc",
            source_identifier="UserService.create_user",
            target_project="gateway",
            target_identifier="POST /users",
            relationship="implements",
            evidence="The create_user method handles POST /users requests.",
        )
        data = json.loads(result)
        assert data["source_identifier"] == "UserService.create_user"

        search_result = kb_read(kind="mapping", query="create_user")
        results = json.loads(search_result)
        assert len(results) >= 1

        lookup_result = kb_read(kind="mapping", identifier="UserService.create_user")
        results = json.loads(lookup_result)
        assert len(results) >= 1

    def test_kb_read_tags(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import kb_read, kb_write

        kb_write(kind="note", op="add", title="A", content="x", tags="auth,db")
        kb_write(kind="note", op="add", title="B", content="y", tags="auth,api")

        tags = json.loads(kb_read(kind="tag"))
        assert "auth" in tags
        assert "db" in tags
        assert "api" in tags

    def test_kb_write_delete_is_soft(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import kb_read, kb_write

        result = kb_write(kind="note", op="add", title="Gone", content="bye")
        nid = json.loads(result)["id"]

        kb_write(kind="note", op="delete", id=nid)
        got = json.loads(kb_read(kind="note", id=nid))
        assert "error" in got
