//! Minimal Python-visible wrapper around tree-sitter `Tree` and `Node`.
//!
//! The goal is to expose tree walking and inspection to Python without
//! shipping the editing / query / cursor APIs. Consumers that need richer
//! structural walks (e.g. the AST canonicalization experiment) can build
//! them in Python on top of `PyNode`.
//!
//! Lifetime handling: `tree_sitter::Node<'a>` borrows from a `Tree`. We
//! cannot hand `Node<'a>` to Python directly, so each `PyNode` stores
//! `Arc<Tree>` alongside a `Node<'static>` that was transmuted from a
//! borrow of that same tree. The `Arc` keeps the underlying tree storage
//! alive for as long as any `PyNode` referencing it exists, so the
//! `'static` lifetime is never actually observed — it is merely a way to
//! store the borrowed node in an owned struct.
//!
//! Invariant enforced by `from_borrowed`: every `PyNode` is constructed
//! with the exact `Arc<Tree>` whose `Tree` the `Node` was borrowed from.
//! No public constructor allows mixing a `Node` from tree `A` with an
//! `Arc<Tree>` for tree `B`.

use std::path::Path;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::exceptions::PyIOError;
use tree_sitter::{Node, Tree};

use crate::pattern::parse_by_extension;

/// Detect the tree-sitter language name from a file extension. Returns
/// `None` for unknown extensions rather than silently falling back to
/// Python, so that `parse_source` can honour the "unknown → None"
/// contract documented in the roadmap.
fn language_from_ext(ext: &str) -> Option<&'static str> {
    Some(match ext {
        "py" | "pyi" => "python",
        "ts" => "typescript",
        "tsx" => "tsx",
        "js" | "jsx" => "typescript",
        "rs" => "rust",
        "html" | "htm" => "html",
        "css" => "css",
        "sql" => "sql",
        "jinja" | "jinja2" | "j2" => "jinja2",
        "dl" | "datalog" => "datalog",
        _ => return None,
    })
}

/// Python-visible parse tree.
#[pyclass(name = "PyTree", module = "emend.emend_core")]
pub struct PyTree {
    tree: Arc<Tree>,
    source: Arc<Vec<u8>>,
    language: String,
}

#[pymethods]
impl PyTree {
    /// Root node of the tree.
    #[getter]
    fn root(&self) -> PyNode {
        let root = self.tree.root_node();
        PyNode::from_borrowed(self.tree.clone(), self.source.clone(), root)
    }

    /// Raw source bytes the tree was parsed from.
    #[getter]
    fn source(&self) -> &[u8] {
        self.source.as_slice()
    }

    /// Tree-sitter language identifier used to parse this tree.
    #[getter]
    fn language(&self) -> &str {
        &self.language
    }
}

/// Python-visible tree-sitter node.
#[pyclass(name = "PyNode", module = "emend.emend_core")]
pub struct PyNode {
    tree: Arc<Tree>,
    source: Arc<Vec<u8>>,
    // SAFETY: see module-level doc. `node` actually borrows from `*tree`,
    // and `tree: Arc<Tree>` is cloned into every PyNode we hand out, so
    // the underlying storage outlives every read of this field. The only
    // constructor, `from_borrowed`, always pairs the node with the Arc
    // that owns its Tree.
    node: Node<'static>,
}

impl PyNode {
    /// Construct a `PyNode` from a borrowed `Node` and the `Arc<Tree>`
    /// that owns the underlying tree storage.
    ///
    /// SAFETY invariant: `node` must have been obtained (directly or
    /// transitively) from `tree.root_node()`. Callers inside this module
    /// satisfy this by construction.
    fn from_borrowed(tree: Arc<Tree>, source: Arc<Vec<u8>>, node: Node<'_>) -> Self {
        // SAFETY: transmuting the lifetime from `'_` to `'static` is
        // sound because `tree` (the Arc we just cloned) keeps the real
        // `Tree` alive for at least as long as this PyNode exists, and
        // the `Node` only ever borrows from that Tree.
        let static_node: Node<'static> = unsafe { std::mem::transmute::<Node<'_>, Node<'static>>(node) };
        PyNode { tree, source, node: static_node }
    }

    fn clone_child(&self, child: Node<'_>) -> PyNode {
        PyNode::from_borrowed(self.tree.clone(), self.source.clone(), child)
    }
}

#[pymethods]
impl PyNode {
    /// Grammar rule name (e.g. `"function_definition"`).
    #[getter]
    fn kind(&self) -> &str {
        self.node.kind()
    }

    /// Whether this node is a named (non-anonymous) grammar node.
    #[getter]
    fn is_named(&self) -> bool {
        self.node.is_named()
    }

    /// Byte offset (inclusive) of the node in the source.
    #[getter]
    fn start_byte(&self) -> usize {
        self.node.start_byte()
    }

    /// Byte offset (exclusive) of the node in the source.
    #[getter]
    fn end_byte(&self) -> usize {
        self.node.end_byte()
    }

    /// `(row, column)` of the first byte (both 0-indexed).
    #[getter]
    fn start_point(&self) -> (usize, usize) {
        let p = self.node.start_position();
        (p.row, p.column)
    }

    /// `(row, column)` of the byte one past the last byte (both 0-indexed).
    #[getter]
    fn end_point(&self) -> (usize, usize) {
        let p = self.node.end_position();
        (p.row, p.column)
    }

    /// Total number of children, including anonymous nodes.
    #[getter]
    fn child_count(&self) -> usize {
        self.node.child_count()
    }

    /// Number of named (non-anonymous) children.
    #[getter]
    fn named_child_count(&self) -> usize {
        self.node.named_child_count()
    }

    /// All children in source order, including anonymous nodes.
    fn children(&self) -> Vec<PyNode> {
        let mut cursor = self.node.walk();
        self.node
            .children(&mut cursor)
            .map(|c| self.clone_child(c))
            .collect()
    }

    /// Named children in source order.
    fn named_children(&self) -> Vec<PyNode> {
        let mut cursor = self.node.walk();
        self.node
            .named_children(&mut cursor)
            .map(|c| self.clone_child(c))
            .collect()
    }

    /// Named children paired with their field name (if any).
    fn named_children_with_fields(&self) -> Vec<(Option<String>, PyNode)> {
        let mut out = Vec::with_capacity(self.node.named_child_count());
        let mut cursor = self.node.walk();
        if cursor.goto_first_child() {
            loop {
                let child = cursor.node();
                if child.is_named() {
                    let field = cursor.field_name().map(|s| s.to_string());
                    out.push((field, self.clone_child(child)));
                }
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
        }
        out
    }

    /// Return the `i`-th child (including anonymous), or `None` if out of range.
    fn child(&self, i: usize) -> Option<PyNode> {
        self.node.child(i).map(|c| self.clone_child(c))
    }

    /// Return the `i`-th named child, or `None` if out of range.
    fn named_child(&self, i: usize) -> Option<PyNode> {
        self.node.named_child(i).map(|c| self.clone_child(c))
    }

    /// Return the child registered under grammar field `name`, if any.
    fn child_by_field_name(&self, name: &str) -> Option<PyNode> {
        self.node
            .child_by_field_name(name.as_bytes())
            .map(|c| self.clone_child(c))
    }

    /// Return the parent node, or `None` for the root.
    fn parent(&self) -> Option<PyNode> {
        self.node.parent().map(|p| self.clone_child(p))
    }

    /// Source text covered by this node.
    fn text(&self) -> String {
        let start = self.node.start_byte();
        let end = self.node.end_byte();
        let slice = &self.source[start..end];
        match std::str::from_utf8(slice) {
            Ok(s) => s.to_string(),
            Err(_) => String::from_utf8_lossy(slice).into_owned(),
        }
    }

    /// `(start_byte, end_byte)`.
    fn byte_range(&self) -> (usize, usize) {
        (self.node.start_byte(), self.node.end_byte())
    }

    fn __repr__(&self) -> String {
        let (sr, sc) = {
            let p = self.node.start_position();
            (p.row, p.column)
        };
        let (er, ec) = {
            let p = self.node.end_position();
            (p.row, p.column)
        };
        format!(
            "PyNode(kind={:?}, start=({}, {}), end=({}, {}))",
            self.node.kind(),
            sr,
            sc,
            er,
            ec
        )
    }
}

/// Parse `source` according to file extension `ext` and return a `PyTree`.
///
/// Returns `None` if the parser fails (tree-sitter virtually never returns
/// `None`, but the option keeps the API honest). Unknown extensions fall
/// back to the Python grammar, matching `parse_by_extension`.
#[pyfunction]
pub fn parse_source(source: &str, ext: &str) -> Option<PyTree> {
    let lang = language_from_ext(ext)?;
    let tree = parse_by_extension(source, ext)?;
    Some(PyTree {
        tree: Arc::new(tree),
        source: Arc::new(source.as_bytes().to_vec()),
        language: lang.to_string(),
    })
}

/// Read a file from disk and parse it. Returns `None` if the extension is
/// unknown-and-unparseable by the fallback grammar, or if parsing fails.
#[pyfunction]
pub fn parse_file(path: &str) -> PyResult<Option<PyTree>> {
    let p = Path::new(path);
    let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("");
    let bytes = std::fs::read(p).map_err(|e| PyIOError::new_err(format!("{}: {}", path, e)))?;
    let source = match String::from_utf8(bytes) {
        Ok(s) => s,
        Err(e) => {
            // Fall back to lossy decoding; the tree-sitter parser still
            // operates on bytes internally, so we preserve a best-effort
            // round-trip by decoding as UTF-8 lossy and handing that to
            // the parser + keeping the lossy bytes as the canonical
            // source buffer.
            String::from_utf8_lossy(&e.into_bytes()).into_owned()
        }
    };
    let lang = match language_from_ext(ext) {
        Some(l) => l,
        None => return Ok(None),
    };
    let tree = match parse_by_extension(&source, ext) {
        Some(t) => t,
        None => return Ok(None),
    };
    Ok(Some(PyTree {
        tree: Arc::new(tree),
        source: Arc::new(source.into_bytes()),
        language: lang.to_string(),
    }))
}
