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
    # KnowledgeBase uses _cache_db_dir which expects .emend/cache under root.
    # We create a minimal structure.
    cache_dir = tmp_path / ".emend" / "cache"
    cache_dir.mkdir(parents=True)
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

    def test_unique_prefix(self, kb):
        """module_prefix has UNIQUE constraint."""
        kb.add_module_mapping(ModuleMapping(module_prefix="dup", local_path="/a"))
        with pytest.raises(Exception):
            kb.add_module_mapping(ModuleMapping(module_prefix="dup", local_path="/b"))


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
    def test_kb_add_and_search(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import kb_add, kb_search

        result = kb_add(
            title="Test note",
            content="This is a test knowledge note about authentication.",
            category="architecture",
            tags="auth,test",
        )
        data = json.loads(result)
        assert data["title"] == "Test note"
        assert "id" in data

        search_result = kb_search(query="authentication")
        results = json.loads(search_result)
        assert len(results) >= 1
        assert results[0]["title"] == "Test note"

    def test_mapping_add_and_search(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".emend" / "cache").mkdir(parents=True)

        from emend.mcp_server import mapping_add, mapping_search, mapping_lookup

        result = mapping_add(
            source_project="user-svc",
            source_identifier="UserService.create_user",
            target_project="gateway",
            target_identifier="POST /users",
            relationship="implements",
            evidence="The create_user method handles POST /users requests.",
        )
        data = json.loads(result)
        assert data["source_identifier"] == "UserService.create_user"

        search_result = mapping_search(query="create_user")
        results = json.loads(search_result)
        assert len(results) >= 1

        lookup_result = mapping_lookup(identifier="UserService.create_user")
        results = json.loads(lookup_result)
        assert len(results) >= 1
