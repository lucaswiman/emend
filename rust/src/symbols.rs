//! Tree-sitter-based symbol extraction for `search --output summary`.
//!
//! Extracts function/class/variable definitions and outer-scope references
//! from Python files in parallel, matching the output of `_ListSymbolsVisitor`.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

/// Internal symbol representation (not a pyclass to avoid recursive Vec issues).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RustSymbol {
    pub name: String,
    pub kind: String, // "function", "async_function", "method", "async_method", "class", "variable", "reference"
    pub signature: Option<String>,
    pub type_annotation: Option<String>,
    pub returns: Option<String>,
    pub is_public: bool,
    pub line: usize,
    pub end_line: usize,
    pub col_offset: usize,
    pub children: Vec<RustSymbol>,
    pub path: Vec<String>,
    pub depth: usize,
    pub decorators: Vec<String>,
    pub decorator_line_start: Option<usize>,
    pub param_names: Vec<String>,
    pub bases: Vec<String>,
}

/// Convert a RustSymbol tree to a Python dict (recursively).
pub fn symbol_to_pydict(_py: Python, sym: &RustSymbol) -> PyResult<PyObject> {
    let d = PyDict::new(_py);
    d.set_item("name", &sym.name)?;
    d.set_item("kind", &sym.kind)?;
    d.set_item("signature", sym.signature.as_deref())?;
    d.set_item("type_annotation", sym.type_annotation.as_deref())?;
    d.set_item("returns", sym.returns.as_deref())?;
    d.set_item("is_public", sym.is_public)?;
    d.set_item("line", sym.line)?;
    d.set_item("end_line", sym.end_line)?;
    d.set_item("col_offset", sym.col_offset)?;
    d.set_item("depth", sym.depth)?;

    let path_list = PyList::new(_py, sym.path.iter().map(|s| s.as_str()))?;
    d.set_item("path", path_list)?;

    let dec_list = PyList::new(_py, sym.decorators.iter().map(|s| s.as_str()))?;
    d.set_item("decorators", dec_list)?;

    d.set_item("decorator_line_start", sym.decorator_line_start)?;

    let param_list = PyList::new(_py, sym.param_names.iter().map(|s| s.as_str()))?;
    d.set_item("param_names", param_list)?;

    let bases_list = PyList::new(_py, sym.bases.iter().map(|s| s.as_str()))?;
    d.set_item("bases", bases_list)?;

    let children_list = PyList::empty(_py);
    for child in &sym.children {
        children_list.append(symbol_to_pydict(_py, child)?)?;
    }
    d.set_item("children", children_list)?;

    Ok(d.into())
}

/// Get node text as a &str from source bytes.
pub fn node_text<'a>(node: tree_sitter::Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

/// Extract decorator strings from a node's children (works for both Python
/// `decorated_definition` and TypeScript `class_declaration` / `method_definition`).
///
/// Returns (decorator_strings, decorator_line_start).
fn extract_decorators(
    node: tree_sitter::Node,
    source: &[u8],
    cfg: &crate::scope::SymbolsSection,
) -> (Vec<String>, Option<usize>) {
    let mut decorators = Vec::new();
    let mut first_line: Option<usize> = None;
    let dec_kind = cfg.decorator_node();

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == dec_kind {
            let line = child.start_position().row + 1;
            if first_line.is_none() {
                first_line = Some(line);
            }
            let text = node_text(child, source).trim().to_string();
            let dec_str = if text.starts_with('@') {
                text[1..].to_string()
            } else {
                text
            };
            decorators.push(dec_str);
        }
    }

    (decorators, first_line)
}

/// Extract parameter name from a single parameter node using the config rule.
fn extract_param_name_from_config(
    child: tree_sitter::Node,
    source: &[u8],
    cfg: &crate::scope::ParamTypeConfig,
) -> String {
    let raw = match cfg.name_source.as_str() {
        "self" => node_text(child, source).to_string(),
        s if s.starts_with("field:") => {
            child
                .child_by_field_name(&s[6..])
                .map(|n| node_text(n, source).to_string())
                .unwrap_or_default()
        }
        s if s.starts_with("child:") => {
            let idx: usize = s[6..].parse().unwrap_or(0);
            child
                .named_child(idx)
                .map(|n| node_text(n, source).to_string())
                .unwrap_or_default()
        }
        _ => String::new(),
    };
    match &cfg.prefix {
        Some(p) => format!("{}{}", p, raw),
        None => raw,
    }
}

/// Extract parameter names from a function definition (config-driven).
fn extract_param_names(
    func_node: tree_sitter::Node,
    source: &[u8],
    cfg: &crate::scope::SymbolsSection,
) -> Vec<String> {
    let params_node = match func_node.child_by_field_name(cfg.parameters_field()) {
        Some(n) => n,
        None => return vec![],
    };

    let param_types = cfg.effective_param_types();
    let param_seps = cfg.effective_param_separators();

    let mut names = Vec::new();
    let mut cursor = params_node.walk();
    for child in params_node.children(&mut cursor) {
        let kind = child.kind();
        if matches!(kind, "(" | ")" | ",") {
            continue;
        }
        if let Some(type_cfg) = param_types.iter().find(|pt| pt.node == kind) {
            names.push(extract_param_name_from_config(child, source, type_cfg));
        } else if let Some(sep_cfg) = param_seps.iter().find(|ps| ps.node == kind) {
            names.push(sep_cfg.display.clone());
        }
    }

    names
}

/// Extract function signature string (config-driven).
///
/// For well-known Python param types the signature is reconstructed verbosely
/// (e.g. `x: int = 5`).  For other languages the raw text of the parameters
/// node is used as a fallback so the output is still useful.
fn extract_signature(
    func_node: tree_sitter::Node,
    source: &[u8],
    cfg: &crate::scope::SymbolsSection,
) -> String {
    let params_node = match func_node.child_by_field_name(cfg.parameters_field()) {
        Some(n) => n,
        None => return "()".to_string(),
    };

    let mut parts: Vec<String> = Vec::new();
    let mut any_known = false;

    let mut cursor = params_node.walk();
    let children: Vec<tree_sitter::Node> = params_node.children(&mut cursor).collect();

    for child in &children {
        match child.kind() {
            "(" | ")" | "," => {}
            // Python-style verbose reconstruction for known types.
            "identifier" => {
                any_known = true;
                parts.push(node_text(*child, source).to_string());
            }
            "typed_parameter" => {
                any_known = true;
                let name = child.named_child(0).map(|n| node_text(n, source)).unwrap_or("");
                let typ = child.child_by_field_name("type").map(|n| node_text(n, source)).unwrap_or("");
                if typ.is_empty() { parts.push(name.to_string()); } else { parts.push(format!("{}: {}", name, typ)); }
            }
            "default_parameter" => {
                any_known = true;
                let name = child.child_by_field_name("name").map(|n| node_text(n, source)).unwrap_or("");
                let value = child.child_by_field_name("value").map(|n| node_text(n, source)).unwrap_or("");
                parts.push(format!("{} = {}", name, value));
            }
            "typed_default_parameter" => {
                any_known = true;
                let name = child.child_by_field_name("name").map(|n| node_text(n, source)).unwrap_or("");
                let typ = child.child_by_field_name("type").map(|n| node_text(n, source)).unwrap_or("");
                let value = child.child_by_field_name("value").map(|n| node_text(n, source)).unwrap_or("");
                if typ.is_empty() { parts.push(format!("{} = {}", name, value)); } else { parts.push(format!("{}: {} = {}", name, typ, value)); }
            }
            "list_splat_pattern" => {
                any_known = true;
                let inner = child.named_child(0).map(|n| node_text(n, source)).unwrap_or("");
                parts.push(format!("*{}", inner));
            }
            "dictionary_splat_pattern" => {
                any_known = true;
                let inner = child.named_child(0).map(|n| node_text(n, source)).unwrap_or("");
                parts.push(format!("**{}", inner));
            }
            "positional_separator" => { any_known = true; parts.push("/".to_string()); }
            "keyword_separator" => { any_known = true; parts.push("*".to_string()); }
            // For other languages (TypeScript etc.), push the raw node text.
            _ => {
                let text = node_text(*child, source).trim().to_string();
                if !text.is_empty() {
                    parts.push(text);
                }
            }
        }
    }

    let _ = any_known; // suppress unused warning

    let mut sig = format!("({})", parts.join(", "));

    // Append return type if present.
    if let Some(ret_node) = func_node.child_by_field_name(cfg.return_type_field()) {
        let ret_text = node_text(ret_node, source);
        let ret_clean = ret_text.trim_start_matches("->").trim();
        sig.push_str(&format!(" -> {}", ret_clean));
    }

    sig
}

/// Extract return type string from a function definition, or None.
fn extract_return_type(
    func_node: tree_sitter::Node,
    source: &[u8],
    cfg: &crate::scope::SymbolsSection,
) -> Option<String> {
    func_node
        .child_by_field_name(cfg.return_type_field())
        .map(|ret_node| {
            let ret_text = node_text(ret_node, source);
            ret_text.trim_start_matches("->").trim().to_string()
        })
}

/// Check if a node at the given path matches the selector path filter.
fn matches_selector(current_path: &[String], selector_path: &Option<Vec<String>>) -> bool {
    let sel = match selector_path {
        None => return true,
        Some(s) => s,
    };
    if current_path.len() < sel.len() {
        sel[..current_path.len()] == *current_path
    } else {
        current_path[..sel.len()] == **sel
    }
}

/// Find the tree-sitter node for a given symbol path (config-driven).
pub fn find_node_by_path<'a>(
    root: tree_sitter::Node<'a>,
    source: &[u8],
    target_path: &[String],
    cfg: &crate::scope::SymbolsSection,
) -> Option<tree_sitter::Node<'a>> {
    fn recurse<'a>(
        node: tree_sitter::Node<'a>,
        source: &[u8],
        current_path: &mut Vec<String>,
        target_path: &[String],
        cfg: &crate::scope::SymbolsSection,
    ) -> Option<tree_sitter::Node<'a>> {
        let kind = node.kind();
        let fn_node = cfg.function_node();
        let cls_node = cfg.class_node();
        let dec_node = cfg.decorated_node();
        let method_node = cfg.method_node();

        if kind == fn_node || kind == cls_node || method_node.map_or(false, |m| kind == m) {
            if let Some(name_node) = node.child_by_field_name(cfg.name_field()) {
                let name = node_text(name_node, source);
                current_path.push(name.to_string());

                if current_path == target_path {
                    return Some(node);
                }

                if target_path.starts_with(current_path.as_slice()) {
                    if let Some(body) = node.child_by_field_name(cfg.body_field()) {
                        if let Some(found) = recurse(body, source, current_path, target_path, cfg) {
                            return Some(found);
                        }
                    }
                }
                current_path.pop();
            }
        } else if !dec_node.is_empty() && kind == dec_node {
            if let Some(def) = node.child_by_field_name(cfg.definition_field()) {
                if let Some(found) = recurse(def, source, current_path, target_path, cfg) {
                    return Some(found);
                }
            }
        } else {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if let Some(found) = recurse(child, source, current_path, target_path, cfg) {
                    return Some(found);
                }
            }
        }
        None
    }

    recurse(root, source, &mut Vec::new(), target_path, cfg)
}

/// Get the byte ranges of all items in a list-like component.
#[pyfunction]
#[pyo3(signature = (source, target_path, component, ext=None))]
pub fn get_symbol_component_list_items(
    _py: Python,
    source: &str,
    target_path: Vec<String>,
    component: &str,
    ext: Option<&str>,
) -> PyResult<Option<Vec<(String, usize, usize)>>> {
    let ext = ext.unwrap_or("py");
    let cfg = crate::scope::config_for_ext(ext);
    let tree = match crate::pattern::parse_by_extension(source, ext) {
        Some(t) => t,
        None => return Ok(None),
    };
    let source_bytes = source.as_bytes();
    let root = tree.root_node();

    let node = match find_node_by_path(root, source_bytes, &target_path, &cfg.symbols) {
        Some(n) => n,
        None => return Ok(None),
    };

    let sym_cfg = &cfg.symbols;
    let param_types = sym_cfg.effective_param_types();
    let param_seps = sym_cfg.effective_param_separators();
    let mut items = Vec::new();

    match component {
        "params" => {
            if let Some(params) = node.child_by_field_name(sym_cfg.parameters_field()) {
                let mut cursor = params.walk();
                for child in params.children(&mut cursor) {
                    let kind = child.kind();
                    if matches!(kind, "(" | ")" | ",") { continue; }
                    if let Some(type_cfg) = param_types.iter().find(|pt| pt.node == kind) {
                        let name = extract_param_name_from_config(child, source_bytes, type_cfg);
                        items.push((name, child.start_byte(), child.end_byte()));
                    } else if let Some(sep_cfg) = param_seps.iter().find(|ps| ps.node == kind) {
                        items.push((sep_cfg.display.clone(), child.start_byte(), child.end_byte()));
                    }
                }
                return Ok(Some(items));
            } else {
                return Ok(None);
            }
        }
        "decorators" => {
            let dec_node_kind = sym_cfg.decorated_node();
            let dec_child_kind = sym_cfg.decorator_node();
            let mut decorable = node;
            if let Some(parent) = node.parent() {
                if !dec_node_kind.is_empty() && parent.kind() == dec_node_kind {
                    decorable = parent;
                }
            }
            let mut cursor = decorable.walk();
            for child in decorable.children(&mut cursor) {
                if child.kind() == dec_child_kind {
                    let text = node_text(child, source_bytes).trim_start_matches('@').trim();
                    let name = text.split('(').next().unwrap_or("").trim();
                    items.push((name.to_string(), child.start_byte(), child.end_byte()));
                }
            }
            return Ok(Some(items));
        }
        "bases" => {
            let sc_field = sym_cfg.superclasses_field();
            if let Some(superclasses) = node.child_by_field_name(sc_field) {
                let mut cursor = superclasses.walk();
                for child in superclasses.children(&mut cursor) {
                    if !matches!(child.kind(), "(" | ")" | ",") {
                        items.push((node_text(child, source_bytes).to_string(), child.start_byte(), child.end_byte()));
                    }
                }
                return Ok(Some(items));
            } else if node.kind() == sym_cfg.class_node() {
                return Ok(Some(items)); // Return empty list for class without bases
            } else {
                return Ok(None);
            }
        }
        _ => {}
    }

    Ok(None)
}

/// Get the byte range of a component within a symbol node.
#[pyfunction]
#[pyo3(signature = (source, target_path, component, accessor=None, ext=None))]
pub fn get_symbol_component_range(
    _py: Python,
    source: &str,
    target_path: Vec<String>,
    component: &str,
    accessor: Option<PyObject>, // Could be int or str
    ext: Option<&str>,
) -> PyResult<Option<(usize, usize)>> {
    let ext = ext.unwrap_or("py");
    let cfg = crate::scope::config_for_ext(ext);
    let tree = match crate::pattern::parse_by_extension(source, ext) {
        Some(t) => t,
        None => return Ok(None),
    };
    let source_bytes = source.as_bytes();
    let root = tree.root_node();

    let node = match find_node_by_path(root, source_bytes, &target_path, &cfg.symbols) {
        Some(n) => n,
        None => {
            return Ok(None);
        }
    };

    let sym_cfg = &cfg.symbols;
    let param_types = sym_cfg.effective_param_types();

    match component {
        "params" => {
            if let Some(params) = node.child_by_field_name(sym_cfg.parameters_field()) {
                if let Some(acc) = accessor {
                    let mut cursor = params.walk();
                    let children: Vec<tree_sitter::Node> = params.children(&mut cursor).collect();
                    let mut param_nodes = Vec::new();
                    for child in children {
                        if param_types.iter().any(|pt| pt.node == child.kind()) {
                            param_nodes.push(child);
                        }
                    }

                    if let Ok(idx_val) = acc.extract::<isize>(_py) {
                        let idx = if idx_val < 0 {
                            let abs_idx = idx_val.abs() as usize;
                            if abs_idx <= param_nodes.len() {
                                param_nodes.len() - abs_idx
                            } else {
                                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Parameter index {} out of range", idx_val)));
                            }
                        } else {
                            idx_val as usize
                        };

                        if idx < param_nodes.len() {
                            return Ok(Some((param_nodes[idx].start_byte(), param_nodes[idx].end_byte())));
                        } else {
                            return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Parameter index {} out of range", idx_val)));
                        }
                    } else if let Ok(name) = acc.extract::<String>(_py) {
                        for p in &param_nodes {
                            let p_name = if let Some(type_cfg) = param_types.iter().find(|pt| pt.node == p.kind()) {
                                extract_param_name_from_config(*p, source_bytes, type_cfg)
                            } else {
                                String::new()
                            };
                            if p_name == name {
                                return Ok(Some((p.start_byte(), p.end_byte())));
                            }
                        }
                        return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Parameter '{}' not found", name)));
                    }
                }
                // Return range inside parentheses
                let start = params.start_byte() + 1;
                let end = params.end_byte() - 1;
                return Ok(Some((start, end)));
            }
        }
        "returns" => {
            let ret_field = sym_cfg.return_type_field();
            if let Some(ret) = node.child_by_field_name(ret_field) {
                // Return type range includes ->.
                let mut start = ret.start_byte();
                let mut cursor = node.walk();
                for child in node.children(&mut cursor) {
                    if child.kind() == "->" {
                        start = child.start_byte();
                        let mut i = start;
                        while i > 0 && (source_bytes[i-1] == b' ' || source_bytes[i-1] == b'\t') {
                            i -= 1;
                        }
                        start = i;
                        break;
                    }
                }
                return Ok(Some((start, ret.end_byte())));
            } else {
                if let Some(params) = node.child_by_field_name(sym_cfg.parameters_field()) {
                    return Ok(Some((params.end_byte(), params.end_byte())));
                }
            }
        }
        "decorators" => {
            let dec_node_kind = sym_cfg.decorated_node();
            let dec_child_kind = sym_cfg.decorator_node();
            let mut decorable = node;
            if let Some(parent) = node.parent() {
                if !dec_node_kind.is_empty() && parent.kind() == dec_node_kind {
                    decorable = parent;
                }
            }

            let mut cursor = decorable.walk();
            let decorators: Vec<tree_sitter::Node> = decorable
                .children(&mut cursor)
                .filter(|c| c.kind() == dec_child_kind)
                .collect();

            if let Some(acc) = accessor {
                if decorators.is_empty() {
                    return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>("No decorators found"));
                }
                if let Ok(idx_val) = acc.extract::<isize>(_py) {
                    let idx = if idx_val < 0 {
                        let abs_idx = idx_val.abs() as usize;
                        if abs_idx <= decorators.len() {
                            decorators.len() - abs_idx
                        } else {
                            return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Decorator index {} out of range", idx_val)));
                        }
                    } else {
                        idx_val as usize
                    };

                    if idx < decorators.len() {
                        return Ok(Some((decorators[idx].start_byte(), decorators[idx].end_byte())));
                    } else {
                        return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Decorator index {} out of range", idx_val)));
                    }
                } else if let Ok(name) = acc.extract::<String>(_py) {
                    for d in &decorators {
                        let text = node_text(*d, source_bytes).trim_start_matches('@').trim();
                        let d_name = text.split('(').next().unwrap_or("").trim();
                        if d_name == name {
                            return Ok(Some((d.start_byte(), d.end_byte())));
                        }
                    }
                    return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Decorator '{}' not found", name)));
                }
            }

            if decorators.is_empty() {
                return Ok(Some((decorable.start_byte(), decorable.start_byte())));
            }

            return Ok(Some((decorators[0].start_byte(), decorators.last().unwrap().end_byte())));
        }
        "bases" => {
            let sc_field = sym_cfg.superclasses_field();
            if node.kind() == sym_cfg.class_node() {
                if let Some(superclasses) = node.child_by_field_name(sc_field) {
                    if let Some(acc) = accessor {
                        let mut cursor = superclasses.walk();
                        let bases: Vec<tree_sitter::Node> = superclasses
                            .children(&mut cursor)
                            .filter(|c| !matches!(c.kind(), "(" | ")" | ","))
                            .collect();

                        if let Ok(idx_val) = acc.extract::<isize>(_py) {
                            let idx = if idx_val < 0 {
                                let abs_idx = idx_val.abs() as usize;
                                if abs_idx <= bases.len() {
                                    bases.len() - abs_idx
                                } else {
                                    return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Base class index {} out of range", idx_val)));
                                }
                            } else {
                                idx_val as usize
                            };

                            if idx < bases.len() {
                                return Ok(Some((bases[idx].start_byte(), bases[idx].end_byte())));
                            } else {
                                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Base class index {} out of range", idx_val)));
                            }
                        } else if let Ok(name) = acc.extract::<String>(_py) {
                            for b in &bases {
                                if node_text(*b, source_bytes) == name {
                                    return Ok(Some((b.start_byte(), b.end_byte())));
                                }
                            }
                            return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Base class '{}' not found", name)));
                        }
                    }
                    return Ok(Some((superclasses.start_byte() + 1, superclasses.end_byte() - 1)));
                } else {
                    // No superclasses node — position after the name.
                    if let Some(name) = node.child_by_field_name(sym_cfg.name_field()) {
                        return Ok(Some((name.end_byte(), name.end_byte())));
                    }
                }
            }
        }
        "body" => {
            if let Some(body) = node.child_by_field_name(sym_cfg.body_field()) {
                let mut start = body.start_byte();
                let mut cursor = body.walk();
                if let Some(first_stmt) = body.children(&mut cursor).find(|c| !matches!(c.kind(), "comment")) {
                    start = first_stmt.start_byte();
                    let mut i = start;
                    while i > 0 && source_bytes[i-1] != b'\n' && source_bytes[i-1] != b'\r' {
                        i -= 1;
                    }
                    start = i;
                }
                return Ok(Some((start, body.end_byte())));
            }
        }
        _ => {}
    }

    Ok(None)
}

/// Collect identifier names that are loaded (read) inside a node's subtree,
/// skipping the left-hand side of assignment nodes.
fn collect_loaded_names(node: tree_sitter::Node, source: &[u8], out: &mut Vec<String>) {
    match node.kind() {
        "assignment" => {
            if let Some(right) = node.child_by_field_name("right") {
                collect_loaded_names(right, source, out);
            }
            if let Some(type_node) = node.child_by_field_name("type") {
                collect_loaded_names(type_node, source, out);
            }
        }
        "augmented_assignment" => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                collect_loaded_names(child, source, out);
            }
        }
        "identifier" => {
            out.push(node_text(node, source).to_string());
        }
        _ => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                collect_loaded_names(child, source, out);
            }
        }
    }
}

/// Recursively collect symbols from a block/module node (config-driven).
#[allow(clippy::too_many_arguments)]
fn collect_from_body(
    node: tree_sitter::Node,
    source: &[u8],
    depth: usize,
    max_depth: usize,
    path: &[String],
    selector_path: &Option<Vec<String>>,
    defined_names_stack: &mut Vec<std::collections::HashSet<String>>,
    in_class: bool,
    cfg: &crate::scope::SymbolsSection,
) -> Vec<RustSymbol> {
    let mut symbols: Vec<RustSymbol> = Vec::new();
    let fn_node_kind = cfg.function_node();
    let cls_node_kind = cfg.class_node();
    let dec_node_kind = cfg.decorated_node();
    let method_node_kind = cfg.method_node();
    let expr_stmt_kind = cfg.expression_statement_node();

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        let kind = child.kind();

        // --- Decorated wrapper (Python: decorated_definition) ---
        let is_decorated_wrapper = !dec_node_kind.is_empty() && kind == dec_node_kind;
        // --- Direct function/method node ---
        let is_func = kind == fn_node_kind
            || method_node_kind.map_or(false, |m| kind == m);
        // --- Direct class node ---
        let is_class = kind == cls_node_kind;

        if is_decorated_wrapper {
            // Extract decorators from the wrapper, then unwrap to the real def.
            let (decorators, decorator_line_start) = extract_decorators(child, source, cfg);
            let inner = child.child_by_field_name(cfg.definition_field()).unwrap_or(child);
            let inner_kind = inner.kind();

            if inner_kind == fn_node_kind || method_node_kind.map_or(false, |m| inner_kind == m) {
                emit_function(inner, child, source, depth, max_depth, path, selector_path,
                    defined_names_stack, in_class, decorators, decorator_line_start, cfg, &mut symbols);
            } else if inner_kind == cls_node_kind {
                emit_class(inner, child, source, depth, max_depth, path, selector_path,
                    defined_names_stack, decorators, decorator_line_start, cfg, &mut symbols);
            }
        } else if is_func {
            // TypeScript-style: decorators are direct children.
            let (decorators, decorator_line_start) = extract_decorators(child, source, cfg);
            emit_function(child, child, source, depth, max_depth, path, selector_path,
                defined_names_stack, in_class, decorators, decorator_line_start, cfg, &mut symbols);
        } else if is_class {
            let (decorators, decorator_line_start) = extract_decorators(child, source, cfg);
            emit_class(child, child, source, depth, max_depth, path, selector_path,
                defined_names_stack, decorators, decorator_line_start, cfg, &mut symbols);
        } else if !expr_stmt_kind.is_empty() && kind == expr_stmt_kind {
            // Variable assignments inside classes/modules.
            let mut inner_cursor = child.walk();
            for inner_child in child.children(&mut inner_cursor) {
                if inner_child.kind() == "assignment" {
                    if let Some(left) = inner_child.child_by_field_name("left") {
                        if left.kind() == "identifier" {
                            let name = node_text(left, source).to_string();
                            let current_path: Vec<String> = path.iter().cloned()
                                .chain(std::iter::once(name.clone())).collect();

                            if depth < max_depth && matches_selector(&current_path, selector_path) {
                                let start_line = inner_child.start_position().row + 1;
                                let end_line = inner_child.end_position().row + 1;
                                let col_offset = inner_child.start_position().column;
                                let type_annotation = inner_child
                                    .child_by_field_name("type")
                                    .map(|n| node_text(n, source).to_string());

                                symbols.push(RustSymbol {
                                    name: name.clone(),
                                    kind: "variable".to_string(),
                                    signature: None,
                                    type_annotation,
                                    returns: None,
                                    is_public: true,
                                    line: start_line,
                                    end_line,
                                    col_offset,
                                    children: vec![],
                                    path: current_path,
                                    depth,
                                    decorators: vec![],
                                    decorator_line_start: None,
                                    param_names: vec![],
                                    bases: vec![],
                                });
                            }

                            if let Some(top) = defined_names_stack.last_mut() {
                                top.insert(name);
                            }
                        }
                    }
                }
            }
        }
    }

    symbols
}

/// Emit a function/method symbol (called from `collect_from_body`).
#[allow(clippy::too_many_arguments)]
fn emit_function(
    func_node: tree_sitter::Node,
    outer_node: tree_sitter::Node, // may differ from func_node when inside decorated wrapper
    source: &[u8],
    depth: usize,
    max_depth: usize,
    path: &[String],
    selector_path: &Option<Vec<String>>,
    defined_names_stack: &mut Vec<std::collections::HashSet<String>>,
    in_class: bool,
    decorators: Vec<String>,
    decorator_line_start: Option<usize>,
    cfg: &crate::scope::SymbolsSection,
    symbols: &mut Vec<RustSymbol>,
) {
    let name_node = match func_node.child_by_field_name(cfg.name_field()) {
        Some(n) => n,
        None => return,
    };
    let name = node_text(name_node, source).to_string();
    let current_path: Vec<String> = path.iter().cloned()
        .chain(std::iter::once(name.clone())).collect();

    if depth < max_depth && matches_selector(&current_path, selector_path) {
        let start_line = func_node.start_position().row + 1;
        let end_line = outer_node.end_position().row + 1;
        let col_offset = func_node.start_position().column;

        let is_async = {
            let async_kw = cfg.async_keyword();
            let mut c = func_node.walk();
            let found = func_node.children(&mut c).any(|ch| ch.kind() == async_kw);
            found
        };

        let kind = match (is_async, in_class) {
            (true, true) => "async_method",
            (true, false) => "async_function",
            (false, true) => "method",
            (false, false) => "function",
        };

        let sig = extract_signature(func_node, source, cfg);
        let returns = extract_return_type(func_node, source, cfg);
        let param_names = extract_param_names(func_node, source, cfg);

        let child_defined: HashSet<String> = HashSet::new();
        defined_names_stack.push(child_defined);

        let mut children = if let Some(body) = func_node.child_by_field_name(cfg.body_field()) {
            collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path,
                defined_names_stack, false, cfg)
        } else {
            vec![]
        };

        // Collect outer-scope references if depth allows.
        if depth + 1 < max_depth {
            if let Some(body) = func_node.child_by_field_name(cfg.body_field()) {
                let mut loaded: Vec<String> = Vec::new();
                collect_loaded_names(body, source, &mut loaded);

                let stack_len = defined_names_stack.len();
                let mut seen: HashSet<String> = HashSet::new();
                for name_ref in &loaded {
                    if seen.contains(name_ref) { continue; }
                    let found_outer = defined_names_stack[..stack_len - 1]
                        .iter().any(|scope| scope.contains(name_ref));
                    if found_outer {
                        if !children.iter().any(|c| c.name == *name_ref && c.kind == "reference") {
                            children.push(RustSymbol {
                                name: name_ref.clone(),
                                kind: "reference".to_string(),
                                signature: None,
                                type_annotation: None,
                                returns: None,
                                is_public: true,
                                line: 0,
                                end_line: 0,
                                col_offset: 0,
                                children: vec![],
                                path: vec![],
                                depth: depth + 1,
                                decorators: vec![],
                                decorator_line_start: None,
                                param_names: vec![],
                                bases: vec![],
                            });
                        }
                        seen.insert(name_ref.clone());
                    }
                }
            }
        }

        defined_names_stack.pop();
        if let Some(top) = defined_names_stack.last_mut() {
            top.insert(name.clone());
        }

        symbols.push(RustSymbol {
            name,
            kind: kind.to_string(),
            signature: Some(sig),
            type_annotation: None,
            returns,
            is_public: true,
            line: start_line,
            end_line,
            col_offset,
            children,
            path: current_path,
            depth,
            decorators,
            decorator_line_start,
            param_names,
            bases: vec![],
        });
    } else {
        if let Some(top) = defined_names_stack.last_mut() {
            top.insert(name);
        }
    }
}

/// Emit a class symbol (called from `collect_from_body`).
#[allow(clippy::too_many_arguments)]
fn emit_class(
    class_node: tree_sitter::Node,
    outer_node: tree_sitter::Node,
    source: &[u8],
    depth: usize,
    max_depth: usize,
    path: &[String],
    selector_path: &Option<Vec<String>>,
    defined_names_stack: &mut Vec<std::collections::HashSet<String>>,
    decorators: Vec<String>,
    decorator_line_start: Option<usize>,
    cfg: &crate::scope::SymbolsSection,
    symbols: &mut Vec<RustSymbol>,
) {
    let name_node = match class_node.child_by_field_name(cfg.name_field()) {
        Some(n) => n,
        None => return,
    };
    let name = node_text(name_node, source).to_string();
    let current_path: Vec<String> = path.iter().cloned()
        .chain(std::iter::once(name.clone())).collect();

    if depth < max_depth && matches_selector(&current_path, selector_path) {
        let start_line = class_node.start_position().row + 1;
        let end_line = outer_node.end_position().row + 1;
        let col_offset = class_node.start_position().column;

        let child_defined: HashSet<String> = HashSet::new();
        defined_names_stack.push(child_defined);

        let children = if let Some(body) = class_node.child_by_field_name(cfg.body_field()) {
            collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path,
                defined_names_stack, true, cfg)
        } else {
            vec![]
        };

        defined_names_stack.pop();
        if let Some(top) = defined_names_stack.last_mut() {
            top.insert(name.clone());
        }

        let mut bases = Vec::new();
        let sc_field = cfg.superclasses_field();
        if let Some(superclasses) = class_node.child_by_field_name(sc_field) {
            let mut cursor = superclasses.walk();
            for child in superclasses.children(&mut cursor) {
                if !matches!(child.kind(), "(" | ")" | ",") {
                    bases.push(node_text(child, source).to_string());
                }
            }
        }

        symbols.push(RustSymbol {
            name,
            kind: "class".to_string(),
            signature: None,
            type_annotation: None,
            returns: None,
            is_public: true,
            line: start_line,
            end_line,
            col_offset,
            children,
            path: current_path,
            depth,
            decorators,
            decorator_line_start,
            param_names: vec![],
            bases,
        });
    } else {
        if let Some(top) = defined_names_stack.last_mut() {
            top.insert(name);
        }
    }
}

/// Collect symbols from a single file's source code (config-driven).
fn collect_symbols_from_source(
    source: &str,
    max_depth: usize,
    selector: &Option<Vec<String>>,
    ext: &str,
) -> Vec<RustSymbol> {
    let tree = match crate::pattern::parse_by_extension(source, ext) {
        Some(t) => t,
        None => return vec![],
    };

    let cfg = crate::scope::config_for_ext(ext);
    let source_bytes = source.as_bytes();
    let root = tree.root_node();
    let mut defined_names_stack: Vec<std::collections::HashSet<String>> =
        vec![std::collections::HashSet::new()];

    collect_from_body(root, source_bytes, 0, max_depth, &[], selector, &mut defined_names_stack, false, &cfg.symbols)
}

/// Collect symbols from multiple files in parallel.
#[pyfunction]
#[pyo3(signature = (files, max_depth=None, selector=None))]
pub fn collect_symbols_batch(
    py: Python,
    files: Vec<String>,
    max_depth: Option<usize>,
    selector: Option<String>,
) -> PyResult<Vec<(String, PyObject)>> {
    let depth = max_depth.unwrap_or(usize::MAX);
    let selector_path: Option<Vec<String>> = selector.map(|s| {
        s.split('.').map(|part| part.to_string()).collect()
    });

    let raw_results: Vec<(String, Vec<RustSymbol>)> = files
        .into_par_iter()
        .filter_map(|path| {
            let ext = std::path::Path::new(&path)
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or("py");
            let content = std::fs::read_to_string(&path).ok()?;
            let syms = collect_symbols_from_source(&content, depth, &selector_path, ext);
            Some((path, syms))
        })
        .collect();

    let mut result = Vec::with_capacity(raw_results.len());
    for (path, rust_syms) in raw_results {
        let py_list = PyList::empty(py);
        for sym in &rust_syms {
            py_list.append(symbol_to_pydict(py, sym)?)?;
        }
        result.push((path, py_list.into()));
    }

    Ok(result)
}

/// Collect symbols from source code directly (without file I/O).
#[pyfunction]
#[pyo3(signature = (source, max_depth=None, selector=None, ext=None))]
pub fn collect_symbols_from_str(
    py: Python,
    source: &str,
    max_depth: Option<usize>,
    selector: Option<String>,
    ext: Option<&str>,
) -> PyResult<PyObject> {
    let ext = ext.unwrap_or("py");
    let depth = max_depth.unwrap_or(usize::MAX);
    let selector_path: Option<Vec<String>> = selector.map(|s| {
        s.split('.').map(|part| part.to_string()).collect()
    });

    let rust_syms = collect_symbols_from_source(source, depth, &selector_path, ext);

    let py_list = PyList::empty(py);
    for sym in &rust_syms {
        py_list.append(symbol_to_pydict(py, sym)?)?;
    }

    Ok(py_list.into())
}

/// Get the line ranges of all simple statements in a source string.
///
/// Returns a list of (start_line, end_line) tuples, 1-indexed.
/// Used for mapping noqa comments to their enclosing statement ranges.
#[pyfunction]
#[pyo3(signature = (source, ext=None))]
pub fn get_statement_ranges(source: &str, ext: Option<&str>) -> PyResult<Vec<(usize, usize)>> {
    let ext = ext.unwrap_or("py");
    let tree = match crate::pattern::parse_by_extension(source, ext) {
        Some(t) => t,
        None => return Ok(vec![]),
    };

    let cfg = crate::scope::config_for_ext(ext);
    let stmts = &cfg.symbols.statements;
    let simple_owned = stmts.effective_simple();
    let recurse_owned = stmts.effective_recurse_into();
    let simple_stmts: Vec<&str> = simple_owned.iter().map(|s| s.as_str()).collect();
    let recurse_into: Vec<&str> = recurse_owned.iter().map(|s| s.as_str()).collect();

    let source_bytes = source.as_bytes();
    let root = tree.root_node();
    let mut ranges = Vec::new();

    fn collect_simple_stmts(
        node: tree_sitter::Node,
        _source: &[u8],
        ranges: &mut Vec<(usize, usize)>,
        simple_stmts: &[&str],
        recurse_into: &[&str],
    ) {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            let k = child.kind();
            if simple_stmts.contains(&k) {
                let start = child.start_position().row + 1;
                let end = child.end_position().row + 1;
                ranges.push((start, end));
            } else if recurse_into.contains(&k) {
                collect_simple_stmts(child, _source, ranges, simple_stmts, recurse_into);
            }
        }
    }

    collect_simple_stmts(root, source_bytes, &mut ranges, &simple_stmts, &recurse_into);
    Ok(ranges)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_python_symbol_extraction() {
        let source = r#"
class MyClass:
    def method(self, x: int) -> str:
        pass

def top_func(a, b=1):
    pass
"#;
        let syms = collect_symbols_from_source(source, usize::MAX, &None, "py");
        let names: Vec<&str> = syms.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"MyClass"), "Expected MyClass in {:?}", names);
        assert!(names.contains(&"top_func"), "Expected top_func in {:?}", names);

        let class_sym = syms.iter().find(|s| s.name == "MyClass").unwrap();
        assert_eq!(class_sym.kind, "class");
        let method = class_sym.children.iter().find(|s| s.name == "method").unwrap();
        assert_eq!(method.kind, "method");
        assert_eq!(method.param_names, vec!["self", "x"]);

        let top = syms.iter().find(|s| s.name == "top_func").unwrap();
        assert_eq!(top.kind, "function");
        assert_eq!(top.param_names, vec!["a", "b"]);
    }

    #[test]
    fn test_typescript_config_loads() {
        let cfg = crate::scope::config_for_ext("ts");
        assert_eq!(cfg.symbols.function_node(), "function_declaration");
        assert_eq!(cfg.symbols.class_node(), "class_declaration");
        assert_eq!(cfg.symbols.parameters_field(), "formal_parameters");
        assert_eq!(cfg.symbols.body_field(), "statement_block");
        assert!(cfg.symbols.method_node().is_some());
        assert_eq!(cfg.symbols.method_node().unwrap(), "method_definition");
    }

    #[test]
    fn test_typescript_symbol_extraction() {
        let source = r#"
function greet(name: string): string {
    return "hello " + name;
}

class Animal {
    constructor(public name: string) {}
    speak(): void {}
}
"#;
        let syms = collect_symbols_from_source(source, usize::MAX, &None, "ts");
        let names: Vec<&str> = syms.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"greet"), "Expected greet in {:?}", names);
        assert!(names.contains(&"Animal"), "Expected Animal in {:?}", names);

        let func = syms.iter().find(|s| s.name == "greet").unwrap();
        assert_eq!(func.kind, "function");

        let class_sym = syms.iter().find(|s| s.name == "Animal").unwrap();
        assert_eq!(class_sym.kind, "class");
    }

    #[test]
    fn test_python_statement_ranges() {
        let _source = "x = 1\ny = 2\n";
        let cfg = crate::scope::config_for_ext("py");
        let stmts = &cfg.symbols.statements;
        let simple_owned = stmts.effective_simple();
        assert!(simple_owned.iter().any(|s| s == "expression_statement"));
    }
}
