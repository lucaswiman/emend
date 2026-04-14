# Phase 1 — Minimal Rust AST exposure

## Motivation

`emend_core` parses tree-sitter trees for `find_pattern`, `PyScopeResolver`,
`PyCfg`, symbol collection, and more — but it never exposes the tree to
Python. Every consumer that needs custom tree walking either hits an API
wall or duplicates a walker on the Rust side. The AST dedup experiment needs
recursive tree walking for Merkle hashing, and several existing features
would simplify if they could share a `PyNode`.

## Scope

Add a narrow `PyTree` / `PyNode` wrapper. No editing, no queries, no cursor
API, no S-expression dump. Just: parse, walk, inspect.

## API

```python
# src/emend/emend_core.pyi (conceptual)

class PyTree:
    @property
    def root(self) -> PyNode: ...
    @property
    def source(self) -> bytes: ...
    @property
    def language(self) -> str: ...

class PyNode:
    @property
    def kind(self) -> str: ...
    @property
    def is_named(self) -> bool: ...
    @property
    def start_byte(self) -> int: ...
    @property
    def end_byte(self) -> int: ...
    @property
    def start_point(self) -> tuple[int, int]: ...
    @property
    def end_point(self) -> tuple[int, int]: ...
    @property
    def child_count(self) -> int: ...
    @property
    def named_child_count(self) -> int: ...
    def children(self) -> list[PyNode]: ...
    def named_children(self) -> list[PyNode]: ...
    def named_children_with_fields(self) -> list[tuple[str | None, PyNode]]: ...
    def child(self, i: int) -> PyNode | None: ...
    def named_child(self, i: int) -> PyNode | None: ...
    def child_by_field_name(self, name: str) -> PyNode | None: ...
    def parent(self) -> PyNode | None: ...
    def text(self) -> str: ...
    def byte_range(self) -> tuple[int, int]: ...

def parse_source(source: str, ext: str) -> PyTree | None: ...
def parse_file(path: str) -> PyTree | None: ...
```

## Implementation notes

New file `rust/src/tree_py.rs`:

```rust
use std::sync::Arc;
use pyo3::prelude::*;
use tree_sitter::{Node, Tree};

#[pyclass(name = "PyTree", module = "emend.emend_core")]
pub struct PyTree {
    tree: Arc<Tree>,
    source: Arc<Vec<u8>>,
    language: String,
}

#[pyclass(name = "PyNode", module = "emend.emend_core")]
pub struct PyNode {
    tree: Arc<Tree>,
    source: Arc<Vec<u8>>,
    // SAFETY: `node` borrows from `tree`. `tree: Arc<Tree>` is held by this
    // struct and every child/parent PyNode we hand out, so the real lifetime
    // outlives every use of the transmuted 'static. Never hand out a Node
    // that was not obtained via the Arc'd Tree.
    node: Node<'static>,
}
```

Construction helper (the *only* place unsafe appears):

```rust
impl PyNode {
    fn from_borrowed(tree: Arc<Tree>, source: Arc<Vec<u8>>, node: Node<'_>) -> Self {
        // SAFETY: see field doc. `tree` Arc is cloned into the new PyNode, so
        // the underlying Tree storage outlives `node`.
        let static_node: Node<'static> = unsafe { std::mem::transmute(node) };
        PyNode { tree, source, node: static_node }
    }
}
```

All methods go through `from_borrowed`. `children()` clones the Arc and
produces new `PyNode`s; `parent()` uses `self.node.parent()` + `from_borrowed`.

`parse_source` reuses `pattern::parse_by_extension` (`rust/src/pattern.rs:76`)
which already handles every supported language. `parse_file` reads bytes from
disk, calls `parse_by_extension`, and detects the extension via
`Path::extension`.

`text()` slices `self.source[self.node.start_byte()..self.node.end_byte()]`
and converts to `&str` (tree-sitter guarantees UTF-8 boundary alignment on
named nodes for valid source; fall back to `from_utf8_lossy` for safety).

Register in `lib.rs::emend_core` module init (follows the pattern used by
`PyScopeResolver` at `rust/src/lib.rs:326`):

```rust
m.add_class::<tree_py::PyTree>()?;
m.add_class::<tree_py::PyNode>()?;
m.add_function(wrap_pyfunction!(tree_py::parse_source, m)?)?;
m.add_function(wrap_pyfunction!(tree_py::parse_file, m)?)?;
```

## Tests

New file `tests/test_emend/test_ast_nodes.py`:

1. `test_parse_python_module_root` — parse `def f(x): return x`, assert
   `tree.root.kind == "module"`, `root.named_child(0).kind ==
   "function_definition"`.
2. `test_child_by_field_name` — assert
   `func.child_by_field_name("name").text == "f"`.
3. `test_byte_range_roundtrip` — for every named node, assert
   `source[start_byte:end_byte] == node.text`.
4. `test_named_children_with_fields` — assert that the `name` field is
   reported on the `function_definition`'s children.
5. `test_parent_back_reference` — assert `func.child_by_field_name("name")
   .parent().kind == "function_definition"`.
6. `test_node_outlives_tree_variable` — parse, save `root`, drop the local
   `tree` binding, access `root.children()` — verifies the Arc keeps the
   tree alive. (Python GC semantics make this implicit, but a named test
   documents the guarantee.)
7. `test_parse_typescript_and_rust` — parse a trivial TS and Rust snippet,
   verify root kind is `program` / `source_file` respectively.
8. `test_parse_source_unsupported_ext_returns_none` — unknown extension →
   `None`.

## Refactoring opportunities (not required for this phase)

Once `PyNode` exists, we can simplify (as follow-ups, not in phase 1):

- `collect_identifier_positions` (`rust/src/lib.rs:244`) — replace with
  Python `walk_nodes(tree.root, kind="identifier")` in consumers.
- `get_statement_ranges` (`rust/src/symbols.rs`) — replaceable with a Python
  helper.
- DSL region heuristics in `dsl.py` — can inspect actual node kinds instead
  of scanning text.
- `ast_utils.py` — currently pivots on `collect_symbols_from_str()` dicts;
  callers that want tree shape can switch to `PyNode`.

None of these are *required* to land Phase 1 — they're cleanup that becomes
possible once the API is in.

## Risk: Arc + transmute soundness

The transmute is the only unsafe block. The invariant is: *every* `PyNode`
holds an `Arc<Tree>` equal to the Arc that created it. We enforce this by
making the field `node: Node<'static>` private and providing no constructor
other than `from_borrowed(tree, source, node)` which always clones the Arc
from an existing `PyTree` or `PyNode`. As long as no code path clones a
`Node` out of one `Tree` and stores it in a `PyNode` bound to a different
`Arc<Tree>`, the 'static lifetime is a harmless lie — the Arc keeps the real
storage alive.

This is structurally the same pattern `py-tree-sitter` (the PyPI package)
uses. Worth a code review from someone else before landing.

## Checklist

- [ ] `rust/src/tree_py.rs` created with `PyTree`, `PyNode`, `parse_source`,
      `parse_file`
- [ ] Registered in `rust/src/lib.rs` module init
- [ ] `tests/test_emend/test_ast_nodes.py` covers tests 1-8 above
- [ ] `make test` green
- [ ] `make clean test` green (confirms the Rust rebuild picks up the new
      symbols in a fresh .venv)
- [ ] Soundness review of the `transmute` by a second reader
