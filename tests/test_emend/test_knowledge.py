"""Tests for the mapping knowledge base (knowledge.py)."""

from __future__ import annotations

import json

import pytest

from emend.knowledge import (
    IdentifierMapping,
    KnowledgeBase,
    KnowledgeNote,
    mapping_to_dict,
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
