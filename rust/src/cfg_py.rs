//! PyO3 bindings for the CFG module.
//!
//! Wraps `crate::cfg::FunctionCfg` as `PyCfg` and exposes `build_cfgs`
//! as a Python-callable function.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::cfg::{build_cfgs_for_source, BlockId, FunctionCfg};

// ─── PyCfg ──────────────────────────────────────────────────────────────────

/// Python-visible wrapper around a single `FunctionCfg`.
#[pyclass]
pub struct PyCfg {
    inner: FunctionCfg,
}

#[pymethods]
impl PyCfg {
    // ── Scalar getters ──────────────────────────────────────────────────────

    /// Function name.
    #[getter]
    fn func_name(&self) -> &str {
        &self.inner.func_name
    }

    /// Start line (0-indexed).
    #[getter]
    fn func_start_line(&self) -> u32 {
        self.inner.func_start_line
    }

    /// End line (0-indexed).
    #[getter]
    fn func_end_line(&self) -> u32 {
        self.inner.func_end_line
    }

    /// Entry block ID.
    #[getter]
    fn entry(&self) -> u32 {
        self.inner.entry.0
    }

    /// Exit block ID.
    #[getter]
    fn exit(&self) -> u32 {
        self.inner.exit.0
    }

    // ── Block / edge accessors ──────────────────────────────────────────────

    /// Return all basic blocks as a Python list of dicts.
    ///
    /// Each dict has keys:
    /// - ``"id"``         – block id (int)
    /// - ``"start_line"`` – first source line in the block (int)
    /// - ``"end_line"``   – last source line in the block (int)
    /// - ``"start_byte"`` – byte offset of the first statement (int)
    /// - ``"end_byte"``   – byte offset past the last statement (int)
    /// - ``"statements"`` – list of ``(start_byte, end_byte)`` tuples
    /// - ``"defs"``       – list of ``(name, line, col, kind)`` tuples
    /// - ``"uses"``       – list of ``(name, line, col, kind)`` tuples
    fn get_blocks(&self, py: Python<'_>) -> PyResult<PyObject> {
        let list = PyList::empty(py);
        for block in &self.inner.blocks {
            let d = PyDict::new(py);
            d.set_item("id", block.id.0)?;
            d.set_item("start_line", block.start_line)?;
            d.set_item("end_line", block.end_line)?;
            d.set_item("start_byte", block.start_byte)?;
            d.set_item("end_byte", block.end_byte)?;

            // statements: list of (start_byte, end_byte)
            let stmts = PyList::empty(py);
            for &(sb, eb) in &block.statements {
                stmts.append(PyTuple::new(py, [sb, eb])?)?;
            }
            d.set_item("statements", stmts)?;

            // defs: list of (name, line, col, kind)
            let defs = PyList::empty(py);
            for (name, line, col, kind) in &block.defs {
                let t = PyTuple::new(py, [name.as_str().into_pyobject(py)?.into_any(), line.into_pyobject(py)?.into_any(), col.into_pyobject(py)?.into_any(), kind.as_str().into_pyobject(py)?.into_any()])?;
                defs.append(t)?;
            }
            d.set_item("defs", defs)?;

            // uses: list of (name, line, col, kind)
            let uses = PyList::empty(py);
            for (name, line, col, kind) in &block.uses {
                let t = PyTuple::new(py, [name.as_str().into_pyobject(py)?.into_any(), line.into_pyobject(py)?.into_any(), col.into_pyobject(py)?.into_any(), kind.as_str().into_pyobject(py)?.into_any()])?;
                uses.append(t)?;
            }
            d.set_item("uses", uses)?;

            list.append(d)?;
        }
        Ok(list.into())
    }

    /// Return all CFG edges as a Python list of dicts.
    ///
    /// Each dict has keys:
    /// - ``"from"``            – source block id (int)
    /// - ``"to"``              – target block id (int)
    /// - ``"kind"``            – lowercase edge kind string
    /// - ``"condition_bytes"`` – ``(start_byte, end_byte)`` tuple or ``None``
    fn get_edges(&self, py: Python<'_>) -> PyResult<PyObject> {
        let list = PyList::empty(py);
        for edge in &self.inner.edges {
            let d = PyDict::new(py);
            d.set_item("from", edge.from.0)?;
            d.set_item("to", edge.to.0)?;
            d.set_item("kind", edge.kind.as_str())?;
            match edge.condition {
                Some((sb, eb)) => {
                    d.set_item("condition_bytes", PyTuple::new(py, [sb, eb])?)?;
                }
                None => {
                    d.set_item("condition_bytes", py.None())?;
                }
            }
            list.append(d)?;
        }
        Ok(list.into())
    }

    // ── Graph-theoretic queries ─────────────────────────────────────────────

    /// Return the IDs of all predecessor blocks of ``block_id``.
    fn predecessors(&self, block_id: u32) -> Vec<u32> {
        self.inner
            .predecessors(BlockId(block_id))
            .into_iter()
            .map(|b| b.0)
            .collect()
    }

    /// Return the IDs of all successor blocks of ``block_id``.
    fn successors(&self, block_id: u32) -> Vec<u32> {
        self.inner
            .successors(BlockId(block_id))
            .into_iter()
            .map(|b| b.0)
            .collect()
    }

    /// Return the IDs of all dominators of ``block_id`` (sorted).
    fn dominators(&self, block_id: u32) -> Vec<u32> {
        let mut v: Vec<u32> = self
            .inner
            .dominators(BlockId(block_id))
            .into_iter()
            .map(|b| b.0)
            .collect();
        v.sort();
        v
    }

    /// Return the IDs of all post-dominators of ``block_id`` (sorted).
    fn post_dominators(&self, block_id: u32) -> Vec<u32> {
        let mut v: Vec<u32> = self
            .inner
            .post_dominators(BlockId(block_id))
            .into_iter()
            .map(|b| b.0)
            .collect();
        v.sort();
        v
    }

    /// Compute the full dominator map for all blocks at once.
    ///
    /// Returns a dict mapping block_id → sorted list of dominator block_ids.
    fn all_dominators(&self) -> HashMap<u32, Vec<u32>> {
        self.inner
            .all_dominators()
            .into_iter()
            .map(|(k, v)| {
                let mut ids: Vec<u32> = v.into_iter().map(|b| b.0).collect();
                ids.sort();
                (k.0, ids)
            })
            .collect()
    }

    /// Compute the full post-dominator map for all blocks at once.
    ///
    /// Returns a dict mapping block_id → sorted list of post-dominator block_ids.
    fn all_post_dominators(&self) -> HashMap<u32, Vec<u32>> {
        self.inner
            .all_post_dominators()
            .into_iter()
            .map(|(k, v)| {
                let mut ids: Vec<u32> = v.into_iter().map(|b| b.0).collect();
                ids.sort();
                (k.0, ids)
            })
            .collect()
    }

    // ── Counts ──────────────────────────────────────────────────────────────

    /// Number of basic blocks in this CFG.
    fn block_count(&self) -> usize {
        self.inner.blocks.len()
    }

    /// Number of edges in this CFG.
    fn edge_count(&self) -> usize {
        self.inner.edges.len()
    }

    // ── Rendering ───────────────────────────────────────────────────────────

    /// Render the CFG as a Graphviz DOT string.
    fn to_dot(&self) -> String {
        let mut out = String::new();
        out.push_str(&format!("digraph \"{}\" {{\n", escape_dot_string(&self.inner.func_name)));
        out.push_str("  node [shape=box];\n");

        for block in &self.inner.blocks {
            // Build the label: block id, line range, defs, uses
            let mut label_parts = vec![format!("B{}", block.id.0)];
            label_parts.push(format!("lines {}-{}", block.start_line, block.end_line));

            if !block.defs.is_empty() {
                let names: Vec<&str> = block.defs.iter().map(|(n, _, _, _)| n.as_str()).collect();
                label_parts.push(format!("defs: {}", names.join(", ")));
            }
            if !block.uses.is_empty() {
                let names: Vec<&str> = block.uses.iter().map(|(n, _, _, _)| n.as_str()).collect();
                label_parts.push(format!("uses: {}", names.join(", ")));
            }

            let label = label_parts.join("\\n");
            out.push_str(&format!(
                "  {} [label=\"{}\"];\n",
                block.id.0,
                escape_dot_string(&label)
            ));
        }

        for edge in &self.inner.edges {
            let kind = edge.kind.as_str();
            out.push_str(&format!(
                "  {} -> {} [label=\"{}\"];\n",
                edge.from.0, edge.to.0, kind
            ));
        }

        out.push_str("}\n");
        out
    }

    /// Serialize the CFG to a JSON string.
    fn to_json(&self) -> String {
        // Build a serde_json::Value manually so we don't need a Serialize impl
        // on the cfg types (they may not derive it).
        let blocks_arr: Vec<serde_json::Value> = self
            .inner
            .blocks
            .iter()
            .map(|b| {
                serde_json::json!({
                    "id": b.id.0,
                    "start_byte": b.start_byte,
                    "end_byte": b.end_byte,
                    "start_line": b.start_line,
                    "end_line": b.end_line,
                    "statements": b.statements.iter().map(|&(s, e)| serde_json::json!([s, e])).collect::<Vec<_>>(),
                    "defs": b.defs.iter().map(|(n, l, c, k)| serde_json::json!([n, l, c, k])).collect::<Vec<_>>(),
                    "uses": b.uses.iter().map(|(n, l, c, k)| serde_json::json!([n, l, c, k])).collect::<Vec<_>>(),
                })
            })
            .collect();

        let edges_arr: Vec<serde_json::Value> = self
            .inner
            .edges
            .iter()
            .map(|e| {
                let cond = match e.condition {
                    Some((s, end)) => serde_json::json!([s, end]),
                    None => serde_json::Value::Null,
                };
                serde_json::json!({
                    "from": e.from.0,
                    "to": e.to.0,
                    "kind": e.kind.as_str(),
                    "condition_bytes": cond,
                })
            })
            .collect();

        let root = serde_json::json!({
            "func_name": self.inner.func_name,
            "func_start_line": self.inner.func_start_line,
            "func_end_line": self.inner.func_end_line,
            "entry": self.inner.entry.0,
            "exit": self.inner.exit.0,
            "blocks": blocks_arr,
            "edges": edges_arr,
        });

        serde_json::to_string_pretty(&root).unwrap_or_else(|e| format!("{{\"error\":\"{}\"}}", e))
    }
}

// ─── Module-level function ───────────────────────────────────────────────────

/// Build CFGs for all functions found in *source*.
///
/// Parameters
/// ----------
/// source : str
///     Source code to analyse.
/// ext : str, optional
///     File extension (without leading dot) used to select the tree-sitter
///     grammar.  Defaults to ``"py"``.
///
/// Returns
/// -------
/// list[PyCfg]
///     One ``PyCfg`` object per function/method discovered in *source*.
#[pyfunction]
#[pyo3(signature = (source, ext=None))]
pub fn build_cfgs(source: &str, ext: Option<&str>) -> PyResult<Vec<PyCfg>> {
    let extension = ext.unwrap_or("py");
    let cfgs = build_cfgs_for_source(source, extension);
    Ok(cfgs.into_iter().map(|c| PyCfg { inner: c }).collect())
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/// Escape a string for use inside a DOT label or graph name (double-quoted).
/// Backslash-escapes `"` and `\`; literal `\n` sequences are left intact so
/// that DOT renders them as newlines.
fn escape_dot_string(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}
