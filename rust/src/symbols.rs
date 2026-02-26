//! Tree-sitter-based symbol extraction for `search --output summary`.
//!
//! Extracts function/class/variable definitions and outer-scope references
//! from Python files in parallel, matching the output of `_ListSymbolsVisitor`.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;

/// Internal symbol representation (not a pyclass to avoid recursive Vec issues).
struct RustSymbol {
    name: String,
    kind: String, // "function", "async_function", "class", "variable", "reference"
    signature: Option<String>,
    type_annotation: Option<String>,
    line: usize,
    end_line: usize,
    children: Vec<RustSymbol>,
    path: Vec<String>,
    depth: usize,
}

/// Convert a RustSymbol tree to a Python dict (recursively).
fn symbol_to_pydict(py: Python, sym: &RustSymbol) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("name", &sym.name)?;
    d.set_item("kind", &sym.kind)?;
    d.set_item("signature", sym.signature.as_deref())?;
    d.set_item("type_annotation", sym.type_annotation.as_deref())?;
    d.set_item("line", sym.line)?;
    d.set_item("end_line", sym.end_line)?;
    d.set_item("depth", sym.depth)?;

    let path_list = PyList::new(py, sym.path.iter().map(|s| s.as_str()))?;
    d.set_item("path", path_list)?;

    let children_list = PyList::empty(py);
    for child in &sym.children {
        children_list.append(symbol_to_pydict(py, child)?)?;
    }
    d.set_item("children", children_list)?;

    Ok(d.into())
}

/// Get node text as a &str from source bytes.
fn node_text<'a>(node: tree_sitter::Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

/// Extract function signature string from a `function_definition` node.
fn extract_signature(func_node: tree_sitter::Node, source: &[u8]) -> String {
    // Find `parameters` child
    let params_node = match func_node.child_by_field_name("parameters") {
        Some(n) => n,
        None => return "()".to_string(),
    };

    let mut parts: Vec<String> = Vec::new();

    let mut cursor = params_node.walk();
    let children: Vec<tree_sitter::Node> = params_node.children(&mut cursor).collect();

    for child in &children {
        match child.kind() {
            // Opening/closing parens — skip
            "(" | ")" => {}
            // Comma separator — skip
            "," => {}
            // Simple identifier parameter: `x`
            "identifier" => {
                parts.push(node_text(*child, source).to_string());
            }
            // Typed parameter: `x: int`
            // In tree-sitter-python, typed_parameter has no "name" field —
            // the identifier is the first named child; "type" is a field.
            "typed_parameter" => {
                let name = child
                    .named_child(0)
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                let typ = child
                    .child_by_field_name("type")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                if typ.is_empty() {
                    parts.push(name.to_string());
                } else {
                    parts.push(format!("{}: {}", name, typ));
                }
            }
            // Default parameter: `x=5`
            "default_parameter" => {
                let name = child
                    .child_by_field_name("name")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                let value = child
                    .child_by_field_name("value")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                parts.push(format!("{} = {}", name, value));
            }
            // Typed default parameter: `x: int = 5`
            "typed_default_parameter" => {
                let name = child
                    .child_by_field_name("name")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                let typ = child
                    .child_by_field_name("type")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                let value = child
                    .child_by_field_name("value")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                if typ.is_empty() {
                    parts.push(format!("{} = {}", name, value));
                } else {
                    parts.push(format!("{}: {} = {}", name, typ, value));
                }
            }
            // *args
            "list_splat_pattern" => {
                // The child identifier
                let inner = child
                    .named_child(0)
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                parts.push(format!("*{}", inner));
            }
            // **kwargs
            "dictionary_splat_pattern" => {
                let inner = child
                    .named_child(0)
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                parts.push(format!("**{}", inner));
            }
            // Positional-only separator /
            "positional_separator" => {
                parts.push("/".to_string());
            }
            // Keyword-only separator *
            "keyword_separator" => {
                parts.push("*".to_string());
            }
            _ => {}
        }
    }

    let mut sig = format!("({})", parts.join(", "));

    // Append return type if present
    if let Some(ret_node) = func_node.child_by_field_name("return_type") {
        let ret_text = node_text(ret_node, source);
        // tree-sitter includes the `->` in the return_type field text
        // but we want just the type. Check if it starts with `->`
        let ret_clean = ret_text.trim_start_matches("->").trim();
        sig.push_str(&format!(" -> {}", ret_clean));
    }

    sig
}

/// Check if a node at the given path matches the selector path filter.
///
/// Mirrors `_ListSymbolsVisitor._matches_selector`:
/// - If selector_path is None → always match
/// - If current_path is a prefix of selector_path → match (we're on the way down)
/// - If selector_path is a prefix of current_path → match (we're inside the target)
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

/// Collect identifier names that are loaded (read) inside a node's subtree,
/// skipping the left-hand side of assignment nodes.
fn collect_loaded_names(node: tree_sitter::Node, source: &[u8], out: &mut Vec<String>) {
    match node.kind() {
        "assignment" => {
            // Skip the `left` field (assignment target), visit `right` and `type`
            if let Some(right) = node.child_by_field_name("right") {
                collect_loaded_names(right, source, out);
            }
            if let Some(type_node) = node.child_by_field_name("type") {
                collect_loaded_names(type_node, source, out);
            }
        }
        "augmented_assignment" => {
            // left and right are both reads in augmented assignment (x += y)
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

/// Recursively collect symbols from a block/module node.
fn collect_from_body(
    node: tree_sitter::Node,
    source: &[u8],
    depth: usize,
    max_depth: usize,
    path: &[String],
    selector_path: &Option<Vec<String>>,
    defined_names_stack: &mut Vec<std::collections::HashSet<String>>,
) -> Vec<RustSymbol> {
    let mut symbols: Vec<RustSymbol> = Vec::new();

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        match child.kind() {
            "function_definition" | "decorated_definition" => {
                // For decorated functions, unwrap to the actual function_definition
                let func_node = if child.kind() == "decorated_definition" {
                    child.child_by_field_name("definition").unwrap_or(child)
                } else {
                    child
                };

                if func_node.kind() != "function_definition" {
                    // It's a decorated class — handle below
                    let class_node = func_node;
                    if class_node.kind() == "class_definition" {
                        let name_node = match class_node.child_by_field_name("name") {
                            Some(n) => n,
                            None => continue,
                        };
                        let name = node_text(name_node, source).to_string();
                        let current_path: Vec<String> = path.iter().cloned().chain(std::iter::once(name.clone())).collect();

                        if depth < max_depth && matches_selector(&current_path, selector_path) {
                            let start_line = child.start_position().row + 1;
                            let end_line = child.end_position().row + 1;

                            let mut child_defined: std::collections::HashSet<String> = std::collections::HashSet::new();
                            defined_names_stack.push(child_defined);

                            let children = if let Some(body) = class_node.child_by_field_name("body") {
                                let ch = collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path, defined_names_stack);
                                ch
                            } else {
                                vec![]
                            };

                            defined_names_stack.pop();

                            if let Some(top) = defined_names_stack.last_mut() {
                                top.insert(name.clone());
                            }

                            symbols.push(RustSymbol {
                                name,
                                kind: "class".to_string(),
                                signature: None,
                                type_annotation: None,
                                line: start_line,
                                end_line,
                                children,
                                path: current_path,
                                depth,
                            });
                        } else {
                            if let Some(top) = defined_names_stack.last_mut() {
                                top.insert(name);
                            }
                        }
                        continue;
                    }
                    continue;
                }

                let name_node = match func_node.child_by_field_name("name") {
                    Some(n) => n,
                    None => continue,
                };
                let name = node_text(name_node, source).to_string();
                let current_path: Vec<String> = path.iter().cloned().chain(std::iter::once(name.clone())).collect();

                if depth < max_depth && matches_selector(&current_path, selector_path) {
                    let start_line = child.start_position().row + 1;
                    let end_line = child.end_position().row + 1;

                    // Detect async: look for "async" keyword as first non-whitespace child
                    let is_async = {
                        let mut c = func_node.walk();
                        let found = func_node.children(&mut c).any(|ch| ch.kind() == "async");
                        found
                    };
                    let kind = if is_async { "async_function" } else { "function" };

                    let sig = extract_signature(func_node, source);

                    // Collect child symbols from function body
                    let mut child_defined: std::collections::HashSet<String> = std::collections::HashSet::new();
                    defined_names_stack.push(child_defined);

                    let mut children = if let Some(body) = func_node.child_by_field_name("body") {
                        collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path, defined_names_stack)
                    } else {
                        vec![]
                    };

                    // Collect outer-scope references if depth allows
                    if depth + 1 < max_depth {
                        if let Some(body) = func_node.child_by_field_name("body") {
                            let mut loaded: Vec<String> = Vec::new();
                            collect_loaded_names(body, source, &mut loaded);

                            // Check which loaded names are in outer scopes
                            // defined_names_stack still has the inner scope on top
                            // We need to check all scopes BELOW the current top
                            let stack_len = defined_names_stack.len();
                            let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
                            for name_ref in &loaded {
                                if seen.contains(name_ref) {
                                    continue;
                                }
                                // Check outer scopes (all except the innermost we just pushed)
                                let found_outer = defined_names_stack[..stack_len - 1]
                                    .iter()
                                    .any(|scope| scope.contains(name_ref));
                                if found_outer {
                                    let already = children.iter().any(|c| c.name == *name_ref && c.kind == "reference");
                                    if !already {
                                        children.push(RustSymbol {
                                            name: name_ref.clone(),
                                            kind: "reference".to_string(),
                                            signature: None,
                                            type_annotation: None,
                                            line: 0,
                                            end_line: 0,
                                            children: vec![],
                                            path: vec![],
                                            depth: depth + 1,
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
                        line: start_line,
                        end_line,
                        children,
                        path: current_path,
                        depth,
                    });
                } else {
                    if let Some(top) = defined_names_stack.last_mut() {
                        top.insert(name);
                    }
                }
            }

            "class_definition" => {
                let name_node = match child.child_by_field_name("name") {
                    Some(n) => n,
                    None => continue,
                };
                let name = node_text(name_node, source).to_string();
                let current_path: Vec<String> = path.iter().cloned().chain(std::iter::once(name.clone())).collect();

                if depth < max_depth && matches_selector(&current_path, selector_path) {
                    let start_line = child.start_position().row + 1;
                    let end_line = child.end_position().row + 1;

                    let mut child_defined: std::collections::HashSet<String> = std::collections::HashSet::new();
                    defined_names_stack.push(child_defined);

                    let children = if let Some(body) = child.child_by_field_name("body") {
                        collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path, defined_names_stack)
                    } else {
                        vec![]
                    };

                    defined_names_stack.pop();

                    if let Some(top) = defined_names_stack.last_mut() {
                        top.insert(name.clone());
                    }

                    symbols.push(RustSymbol {
                        name,
                        kind: "class".to_string(),
                        signature: None,
                        type_annotation: None,
                        line: start_line,
                        end_line,
                        children,
                        path: current_path,
                        depth,
                    });
                } else {
                    if let Some(top) = defined_names_stack.last_mut() {
                        top.insert(name);
                    }
                }
            }

            "expression_statement" => {
                // In tree-sitter-python 0.23, both plain and annotated assignments
                // appear as `expression_statement` → `assignment`.
                // Annotated `x: int = 5` has a `type` field on the assignment node.
                let mut inner_cursor = child.walk();
                for inner_child in child.children(&mut inner_cursor) {
                    if inner_child.kind() == "assignment" {
                        // Check if left side is a simple identifier
                        if let Some(left) = inner_child.child_by_field_name("left") {
                            if left.kind() == "identifier" {
                                let name = node_text(left, source).to_string();
                                let current_path: Vec<String> = path.iter().cloned().chain(std::iter::once(name.clone())).collect();

                                if depth < max_depth && matches_selector(&current_path, selector_path) {
                                    let start_line = inner_child.start_position().row + 1;
                                    let end_line = inner_child.end_position().row + 1;

                                    // Check for type annotation (annotated assignment: `x: int = 5`)
                                    let type_annotation = inner_child
                                        .child_by_field_name("type")
                                        .map(|n| node_text(n, source).to_string());

                                    symbols.push(RustSymbol {
                                        name: name.clone(),
                                        kind: "variable".to_string(),
                                        signature: None,
                                        type_annotation,
                                        line: start_line,
                                        end_line,
                                        children: vec![],
                                        path: current_path,
                                        depth,
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

            _ => {}
        }
    }

    symbols
}

/// Collect symbols from a single file's source code.
fn collect_symbols_from_source(
    source: &str,
    max_depth: usize,
    selector: &Option<Vec<String>>,
) -> Vec<RustSymbol> {
    let tree = match crate::pattern::parse_python(source) {
        Some(t) => t,
        None => return vec![],
    };

    let source_bytes = source.as_bytes();
    let root = tree.root_node();

    let mut defined_names_stack: Vec<std::collections::HashSet<String>> = vec![std::collections::HashSet::new()];

    collect_from_body(root, source_bytes, 0, max_depth, &[], selector, &mut defined_names_stack)
}

/// Collect symbols from multiple Python files in parallel.
///
/// Returns a list of `(file_path, symbols_list)` where each `symbols_list`
/// is a Python list of dicts matching the `TreeSymbol` dataclass fields.
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

    // Parse all files in parallel (rayon), collect Vec<(String, Vec<RustSymbol>)>
    let raw_results: Vec<(String, Vec<RustSymbol>)> = files
        .into_par_iter()
        .filter_map(|path| {
            let content = std::fs::read_to_string(&path).ok()?;
            // Skip non-UTF-8 or binary files
            if !content.is_ascii() {
                // Still process — UTF-8 is fine; tree-sitter handles it
            }
            let syms = collect_symbols_from_source(&content, depth, &selector_path);
            Some((path, syms))
        })
        .collect();

    // Convert to Python dicts on the main thread (requires GIL)
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
