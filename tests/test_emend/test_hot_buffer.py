"""Tests for Phase 3: Hot Buffer Protocol.

Verifies that EditorSearchEngine maintains in-memory buffer snapshots so
editor operations (file_symbols, goto_definition, complete) reflect unsaved
edits without touching the on-disk file.
"""
import textwrap
from pathlib import Path

import pytest

from emend.editor_search import EditorSearchEngine, _dispatch

from conftest import build_indexed_project


# ---------------------------------------------------------------------------
# Sample sources
# ---------------------------------------------------------------------------

SAMPLE_SOURCE = textwrap.dedent("""\
    class UserService:
        def get_user(self, uid):
            return uid

    def helper():
        return UserService()
""")

SOURCE_WITH_FOO = textwrap.dedent("""\
    class Foo:
        def do_foo(self):
            pass

    def standalone():
        pass
""")

SOURCE_WITH_BAR = textwrap.dedent("""\
    class Bar:
        def do_bar(self):
            pass

    def standalone():
        pass
""")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    """Indexed project + live EditorSearchEngine."""
    proj = build_indexed_project(tmp_path, {"app.py": SAMPLE_SOURCE})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


@pytest.fixture
def foo_engine(tmp_path):
    """Indexed project with Foo class."""
    proj = build_indexed_project(tmp_path, {"app.py": SOURCE_WITH_FOO})
    eng = EditorSearchEngine(str(proj))
    yield eng, proj
    eng.close()


# ---------------------------------------------------------------------------
# TestBufferLifecycle
# ---------------------------------------------------------------------------


class TestBufferLifecycle:
    """Test the buffer_open / buffer_update / buffer_close life-cycle."""

    def test_buffer_open_stores_content(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "# hot content\n")
        assert eng.get_hot_content(app) == "# hot content\n"

    def test_buffer_update_replaces_content(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "# version 1\n")
        assert eng.get_hot_content(app) == "# version 1\n"
        eng.buffer_update(app, "# version 2\n")
        assert eng.get_hot_content(app) == "# version 2\n"

    def test_buffer_close_removes_content(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "# buffered\n")
        assert eng.get_hot_content(app) is not None
        eng.buffer_close(app)
        assert eng.get_hot_content(app) is None

    def test_buffer_close_nonexistent(self, engine):
        """Closing a file that was never opened should not raise."""
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        # Should not raise
        result = eng.buffer_close(app)
        assert result.mode == "buffer"

    def test_buffer_version_tracked(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        result1 = eng.buffer_open(app, "# v1\n", version=1)
        assert result1.items[0]["version"] == 1
        result2 = eng.buffer_update(app, "# v2\n", version=2)
        assert result2.items[0]["version"] == 2
        # Internal version dict should reflect latest
        assert eng._hot_buffer_versions[app] == 2

    def test_buffer_open_returns_buffer_mode(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        result = eng.buffer_open(app, "x = 1\n")
        assert result.mode == "buffer"

    def test_buffer_update_returns_buffer_mode(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "x = 1\n")
        result = eng.buffer_update(app, "x = 2\n")
        assert result.mode == "buffer"

    def test_buffer_close_returns_buffer_mode(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "x = 1\n")
        result = eng.buffer_close(app)
        assert result.mode == "buffer"

    def test_buffer_close_removed_flag_true(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "x = 1\n")
        result = eng.buffer_close(app)
        assert result.items[0]["removed"] is True

    def test_buffer_close_removed_flag_false_for_unknown(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        result = eng.buffer_close(app)
        assert result.items[0]["removed"] is False

    def test_multiple_buffers_independent(self, tmp_path):
        """Two different files can be buffered independently."""
        proj = build_indexed_project(
            tmp_path,
            {"a.py": "x = 1\n", "b.py": "y = 2\n"},
        )
        eng = EditorSearchEngine(str(proj))
        try:
            a = str((proj / "a.py").resolve())
            b = str((proj / "b.py").resolve())
            eng.buffer_open(a, "# a hot\n")
            eng.buffer_open(b, "# b hot\n")
            assert eng.get_hot_content(a) == "# a hot\n"
            assert eng.get_hot_content(b) == "# b hot\n"
            eng.buffer_close(a)
            assert eng.get_hot_content(a) is None
            assert eng.get_hot_content(b) == "# b hot\n"
        finally:
            eng.close()


# ---------------------------------------------------------------------------
# TestBufferDispatch
# ---------------------------------------------------------------------------


class TestBufferDispatch:
    """Test RPC _dispatch routing for buffer_open / update / close."""

    def test_dispatch_buffer_open(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        result = _dispatch(eng, "buffer_open", {"file": app, "content": "# rpc open\n"})
        assert result["mode"] == "buffer"
        assert eng.get_hot_content(app) == "# rpc open\n"

    def test_dispatch_buffer_update(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        _dispatch(eng, "buffer_open", {"file": app, "content": "# initial\n"})
        result = _dispatch(eng, "buffer_update", {"file": app, "content": "# updated\n"})
        assert result["mode"] == "buffer"
        assert eng.get_hot_content(app) == "# updated\n"

    def test_dispatch_buffer_close(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        _dispatch(eng, "buffer_open", {"file": app, "content": "# to close\n"})
        result = _dispatch(eng, "buffer_close", {"file": app})
        assert result["mode"] == "buffer"
        assert eng.get_hot_content(app) is None

    def test_dispatch_buffer_open_with_version(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        result = _dispatch(
            eng, "buffer_open", {"file": app, "content": "# v\n", "version": 7}
        )
        assert result["items"][0]["version"] == 7

    def test_dispatch_buffer_update_with_version(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        _dispatch(eng, "buffer_open", {"file": app, "content": "# v1\n", "version": 1})
        result = _dispatch(
            eng, "buffer_update", {"file": app, "content": "# v2\n", "version": 2}
        )
        assert result["items"][0]["version"] == 2


# ---------------------------------------------------------------------------
# TestFileSymbolsHotBuffer
# ---------------------------------------------------------------------------


class TestFileSymbolsHotBuffer:
    """file_symbols() should prefer the hot buffer over the disk index."""

    def test_file_symbols_uses_hot_buffer(self, foo_engine):
        """After buffer_open with different content, file_symbols returns hot symbols."""
        eng, proj = foo_engine
        app = str((proj / "app.py").resolve())

        # Disk/index has Foo; hot buffer has Bar
        eng.buffer_open(app, SOURCE_WITH_BAR)

        result = eng.file_symbols(app)
        names = {item["name"] for item in result.items}
        assert "Bar" in names
        assert "Foo" not in names

    def test_file_symbols_falls_back_to_index(self, foo_engine):
        """Without a hot buffer, file_symbols returns indexed symbols."""
        eng, proj = foo_engine
        app = str((proj / "app.py").resolve())

        result = eng.file_symbols(app)
        names = {item["name"] for item in result.items}
        assert "Foo" in names

    def test_file_symbols_explicit_content(self, foo_engine):
        """Explicit content param bypasses both hot buffer and disk index."""
        eng, proj = foo_engine
        app = str((proj / "app.py").resolve())

        explicit = textwrap.dedent("""\
            class Explicit:
                pass
        """)
        result = eng.file_symbols(app, content=explicit)
        names = {item["name"] for item in result.items}
        assert "Explicit" in names
        assert "Foo" not in names

    def test_file_symbols_explicit_content_overrides_hot_buffer(self, foo_engine):
        """Explicit content param wins even when a hot buffer is open."""
        eng, proj = foo_engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, SOURCE_WITH_BAR)

        explicit = "class Override: pass\n"
        result = eng.file_symbols(app, content=explicit)
        names = {item["name"] for item in result.items}
        assert "Override" in names
        assert "Bar" not in names
        assert "Foo" not in names

    def test_file_symbols_after_buffer_close_returns_index(self, foo_engine):
        """After closing the hot buffer, file_symbols falls back to the index."""
        eng, proj = foo_engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, SOURCE_WITH_BAR)

        # Verify hot is active
        hot_names = {item["name"] for item in eng.file_symbols(app).items}
        assert "Bar" in hot_names

        eng.buffer_close(app)
        cold_names = {item["name"] for item in eng.file_symbols(app).items}
        assert "Foo" in cold_names

    def test_file_symbols_dispatch_with_content(self, foo_engine):
        """_dispatch passes content param through to file_symbols."""
        eng, proj = foo_engine
        app = str((proj / "app.py").resolve())
        inline = "class InlineClass: pass\n"
        result = _dispatch(eng, "file_symbols", {"file": app, "content": inline})
        assert result["mode"] == "file_symbols"
        names = {item["name"] for item in result["items"]}
        assert "InlineClass" in names

    def test_file_symbols_mode(self, foo_engine):
        eng, proj = foo_engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, SOURCE_WITH_BAR)
        result = eng.file_symbols(app)
        assert result.mode == "file_symbols"


# ---------------------------------------------------------------------------
# TestGotoDefinitionHotBuffer
# ---------------------------------------------------------------------------


class TestGotoDefinitionHotBuffer:
    """goto_definition uses _read_file_or_hot to see unsaved edits."""

    def test_goto_definition_uses_hot_buffer(self, tmp_path):
        """A definition added only in the hot buffer is found by goto_definition."""
        # Write a file with just 'def foo', index it, then buffer_update
        # with content that also defines 'bar' and calls it.
        source_on_disk = textwrap.dedent("""\
            def foo():
                pass
        """)
        proj = build_indexed_project(tmp_path, {"mod.py": source_on_disk})
        eng = EditorSearchEngine(str(proj))
        try:
            mod = str((proj / "mod.py").resolve())

            # Hot buffer replaces disk; now bar is defined too.
            hot_source = textwrap.dedent("""\
                def foo():
                    pass

                def bar():
                    pass

                bar()
            """)
            eng.buffer_open(mod, hot_source)

            # goto_definition on line 7, col 1 (the 'bar()' call)
            result = eng.goto_definition(mod, 7, 1)
            # Should find bar defined in the hot content
            items = result.items
            found_bar = any(item.get("name") == "bar" for item in items)
            # Also acceptable: returns a definition with the correct line
            found_line = any(item.get("line") == 4 for item in items)
            assert found_bar or found_line, (
                f"Expected goto_definition to resolve 'bar' from hot buffer, got: {items}"
            )
        finally:
            eng.close()

    def test_goto_definition_works_without_hot_buffer(self, tmp_path):
        """goto_definition falls back to disk content when no hot buffer."""
        source = textwrap.dedent("""\
            def foo():
                pass

            foo()
        """)
        proj = build_indexed_project(tmp_path, {"mod.py": source})
        eng = EditorSearchEngine(str(proj))
        try:
            mod = str((proj / "mod.py").resolve())
            # No hot buffer — should use disk
            result = eng.goto_definition(mod, 4, 1)
            items = result.items
            # Expect at least an attempt (may or may not resolve 'foo' depending on scope
            # resolver, but should not crash and should return symbol mode)
            assert result.mode in ("symbol", "definition")
        finally:
            eng.close()


# ---------------------------------------------------------------------------
# TestCompleteHotBuffer
# ---------------------------------------------------------------------------


class TestCompleteHotBuffer:
    """complete() reads from hot buffer via _read_file_or_hot."""

    def test_complete_uses_hot_buffer(self, tmp_path):
        """Local variables added in the hot buffer appear in completions."""
        disk_source = textwrap.dedent("""\
            class Foo:
                pass
        """)
        proj = build_indexed_project(tmp_path, {"mod.py": disk_source})
        eng = EditorSearchEngine(str(proj))
        try:
            mod = str((proj / "mod.py").resolve())

            hot_source = textwrap.dedent("""\
                class Foo:
                    pass

                class FooBar:
                    pass

                def run():
                    foobar_inst = FooBar()
                    return foobar_inst
            """)
            eng.buffer_open(mod, hot_source)

            # Complete "foo" with file context; locals from hot content should rank
            result = eng.complete("foo", file=mod, line=9, col=11)
            words = {item.get("word", "") for item in result.items}
            # 'foobar_inst' is a local variable visible at line 9 in the hot source
            assert any("foobar" in w.lower() for w in words), (
                f"Expected foobar-prefixed completion from hot buffer locals; got: {words}"
            )
        finally:
            eng.close()

    def test_complete_no_hot_buffer_still_works(self, foo_engine):
        """complete() works normally when there is no hot buffer."""
        eng, proj = foo_engine
        result = eng.complete("Foo")
        words = {item.get("word", "") for item in result.items}
        # Index has 'Foo' from SOURCE_WITH_FOO
        assert any("Foo" in w for w in words), (
            f"Expected 'Foo' in completions without hot buffer; got: {words}"
        )

    def test_complete_uses_hot_buffer_imports(self, tmp_path):
        """Import names are extracted from the hot buffer, not the on-disk file."""
        disk_source = textwrap.dedent("""\
            from services import alpha

            def run():
                return alpha
        """)
        proj = build_indexed_project(tmp_path, {"mod.py": disk_source})
        eng = EditorSearchEngine(str(proj))
        try:
            mod = str((proj / "mod.py").resolve())

            hot_source = textwrap.dedent("""\
                from services import beta

                def run():
                    return beta
            """)
            eng.buffer_open(mod, hot_source)

            result = eng.complete("be", file=mod, line=4, col=12)
            words = {item.get("word", "") for item in result.items}
            assert "beta" in words
            assert "alpha" not in words
        finally:
            eng.close()

    def test_complete_caches_cfg_construction(self, monkeypatch, tmp_path):
        """Repeated completions for the same source should reuse cached CFGs."""
        source = textwrap.dedent("""\
            def run(flag):
                value = 1
                return value
        """)
        proj = build_indexed_project(tmp_path, {"mod.py": source})
        eng = EditorSearchEngine(str(proj))
        try:
            mod = str((proj / "mod.py").resolve())

            from emend import cfg as cfg_module

            calls = {"count": 0}
            real_builder = cfg_module.build_cfgs_for_source

            def wrapped_builder(source_text):
                calls["count"] += 1
                return real_builder(source_text)

            monkeypatch.setattr(cfg_module, "build_cfgs_for_source", wrapped_builder)

            eng.complete("val", file=mod, line=2, col=12)
            eng.complete("val", file=mod, line=2, col=12)

            assert calls["count"] == 1
        finally:
            eng.close()

    def test_complete_prefers_hot_buffer_attribute_usage(self, tmp_path):
        """Scoped attribute accesses from the hot buffer outrank generic members."""
        disk_source = textwrap.dedent("""\
            class Workflow:
                def alpha(self):
                    pass

                def archive(self):
                    pass

            def run():
                wf = Workflow()
                return wf.
        """)
        proj = build_indexed_project(tmp_path, {"mod.py": disk_source})
        eng = EditorSearchEngine(str(proj))
        try:
            mod = str((proj / "mod.py").resolve())
            hot_source = textwrap.dedent("""\
                class Workflow:
                    def alpha(self):
                        pass

                    def archive(self):
                        pass

                def run():
                    wf = Workflow()
                    wf.archive()
                    return wf.
            """)
            eng.buffer_open(mod, hot_source)

            result = eng.complete("wf.", file=mod, line=11, col=18)
            words = [item["word"] for item in result.items]
            menus = {item["word"]: item.get("menu", "") for item in result.items}

            assert "archive" in words
            assert "alpha" in words
            assert menus["archive"] == "[scope:wf]"
            assert words.index("archive") < words.index("alpha")
        finally:
            eng.close()


# ---------------------------------------------------------------------------
# TestReadFileOrHot
# ---------------------------------------------------------------------------


class TestReadFileOrHot:
    """_read_file_or_hot returns hot content, disk content, or None."""

    def test_read_hot_preferred_over_disk(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "# hot wins\n")
        content = eng._read_file_or_hot(app)
        assert content == "# hot wins\n"

    def test_read_falls_back_to_disk(self, engine):
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        # No hot buffer registered
        content = eng._read_file_or_hot(app)
        # Should return the on-disk content
        assert content is not None
        assert "UserService" in content

    def test_read_returns_none_for_missing(self, tmp_path):
        """No disk file and no hot buffer → None."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".emend" / "cache").mkdir(parents=True)
        eng = EditorSearchEngine(str(proj))
        try:
            nonexistent = str(proj / "ghost.py")
            content = eng._read_file_or_hot(nonexistent)
            assert content is None
        finally:
            eng.close()

    def test_read_hot_after_close_returns_disk(self, engine):
        """After buffer_close, _read_file_or_hot falls back to disk."""
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "# temporary\n")
        assert eng._read_file_or_hot(app) == "# temporary\n"
        eng.buffer_close(app)
        content = eng._read_file_or_hot(app)
        assert content is not None
        assert "UserService" in content

    def test_read_hot_file_not_on_disk(self, tmp_path):
        """A hot buffer for a file that doesn't exist on disk is still readable."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".emend" / "cache").mkdir(parents=True)
        eng = EditorSearchEngine(str(proj))
        try:
            ghost = str(proj / "ghost.py")
            eng.buffer_open(ghost, "# in memory only\n")
            content = eng._read_file_or_hot(ghost)
            assert content == "# in memory only\n"
        finally:
            eng.close()

    def test_get_hot_content_returns_none_without_buffer(self, engine):
        """get_hot_content returns None for a file that was never opened."""
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        assert eng.get_hot_content(app) is None

    def test_get_hot_content_after_update(self, engine):
        """get_hot_content reflects the most recent update."""
        eng, proj = engine
        app = str((proj / "app.py").resolve())
        eng.buffer_open(app, "v1\n")
        eng.buffer_update(app, "v2\n")
        eng.buffer_update(app, "v3\n")
        assert eng.get_hot_content(app) == "v3\n"
