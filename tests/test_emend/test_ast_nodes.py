"""Tests for the ``PyTree`` / ``PyNode`` wrapper exposed by ``emend_core``.

These verify the minimal AST exposure landed in Phase 1 of the AST
canonicalization roadmap. The API is intentionally narrow — just enough to
walk a tree-sitter parse in Python.
"""

from __future__ import annotations

import gc

import pytest

from emend import emend_core


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk(node):
    """Yield ``node`` and all descendants in pre-order."""
    yield node
    for child in node.children():
        yield from _walk(child)


# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------


def test_parse_python_module_root():
    tree = emend_core.parse_source("def f(x): return x\n", "py")
    assert tree is not None
    assert tree.language == "python"
    root = tree.root
    assert root.kind == "module"
    # The first named child should be a function_definition.
    assert root.named_child_count >= 1
    func = root.named_child(0)
    assert func is not None
    assert func.kind == "function_definition"


def test_child_by_field_name():
    tree = emend_core.parse_source("def f(x): return x\n", "py")
    assert tree is not None
    func = tree.root.named_child(0)
    assert func is not None
    name_node = func.child_by_field_name("name")
    assert name_node is not None
    assert name_node.kind == "identifier"
    assert name_node.text() == "f"


def test_byte_range_roundtrip():
    source = "def greet(name):\n    return 'hi ' + name\n"
    tree = emend_core.parse_source(source, "py")
    assert tree is not None
    source_bytes = source.encode("utf-8")
    seen = 0
    for node in _walk(tree.root):
        start, end = node.byte_range()
        assert start == node.start_byte
        assert end == node.end_byte
        if node.is_named:
            # For named nodes, text() should match the byte slice.
            expected = source_bytes[start:end].decode("utf-8")
            assert node.text() == expected
            seen += 1
    assert seen > 0


def test_named_children_with_fields():
    tree = emend_core.parse_source("def f(x): return x\n", "py")
    assert tree is not None
    func = tree.root.named_child(0)
    assert func is not None
    fields = func.named_children_with_fields()
    # Every entry is ``(Optional[str], PyNode)``.
    assert all(
        (name is None or isinstance(name, str)) and hasattr(node, "kind")
        for name, node in fields
    )
    name_map = {name: node for name, node in fields if name is not None}
    assert "name" in name_map
    assert name_map["name"].text() == "f"
    assert "body" in name_map


def test_parent_back_reference():
    tree = emend_core.parse_source("def f(x): return x\n", "py")
    assert tree is not None
    func = tree.root.named_child(0)
    assert func is not None
    name_node = func.child_by_field_name("name")
    assert name_node is not None
    parent = name_node.parent()
    assert parent is not None
    assert parent.kind == "function_definition"
    # Root has no parent.
    assert tree.root.parent() is None


def test_node_outlives_tree_variable():
    """A ``PyNode`` should keep the underlying tree alive via its internal
    ``Arc``. We drop the local ``PyTree`` binding and force GC, then keep
    using the node.
    """
    tree = emend_core.parse_source("def f(x): return x + 1\n", "py")
    assert tree is not None
    root = tree.root
    func = root.named_child(0)
    del tree
    gc.collect()
    # Walk still works and text() still returns valid data.
    assert func is not None
    assert func.kind == "function_definition"
    children = func.children()
    assert len(children) > 0
    name = func.child_by_field_name("name")
    assert name is not None
    assert name.text() == "f"


def test_parse_typescript_and_rust():
    ts_tree = emend_core.parse_source("function f(x: number) { return x; }\n", "ts")
    assert ts_tree is not None
    assert ts_tree.language == "typescript"
    assert ts_tree.root.kind == "program"

    rust_tree = emend_core.parse_source("fn f(x: i32) -> i32 { x + 1 }\n", "rs")
    assert rust_tree is not None
    assert rust_tree.language == "rust"
    assert rust_tree.root.kind == "source_file"


def test_parse_source_unsupported_ext_returns_none():
    assert emend_core.parse_source("anything", "xyz-unknown") is None


def test_parse_file_reads_from_disk(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text("def add(a, b):\n    return a + b\n")
    tree = emend_core.parse_file(str(p))
    assert tree is not None
    assert tree.language == "python"
    func = tree.root.named_child(0)
    assert func is not None
    assert func.child_by_field_name("name").text() == "add"


def test_parse_file_unknown_extension_returns_none(tmp_path):
    p = tmp_path / "data.unknownext"
    p.write_text("not source code")
    assert emend_core.parse_file(str(p)) is None


def test_parse_file_missing_file_raises(tmp_path):
    missing = tmp_path / "nope.py"
    with pytest.raises(OSError):
        emend_core.parse_file(str(missing))


# ---------------------------------------------------------------------------
# Re-export test (Phase 1 follow-up)
# ---------------------------------------------------------------------------


def test_pynode_reexported_from_ast_utils():
    """PyNode, PyTree, parse_source, parse_file must be re-exported from ast_utils."""
    from emend import ast_utils

    # Identity checks — the re-exported names must be the exact same objects.
    assert ast_utils.PyNode is emend_core.PyNode
    assert ast_utils.PyTree is emend_core.PyTree
    assert ast_utils.parse_source is emend_core.parse_source
    assert ast_utils.parse_file is emend_core.parse_file

    # Smoke-test: parse a tiny snippet through the re-exported parse_source.
    tree = ast_utils.parse_source("x = 1\n", "py")
    assert tree is not None
    assert tree.root.kind == "module"
