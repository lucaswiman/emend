//! Tree-sitter-based symbol extraction for `search --output summary`.
//!
//! Extracts function/class/variable definitions and outer-scope references
//! from Python files in parallel, matching the output of `_ListSymbolsVisitor`.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use std::collections::HashSet;

/// Internal symbol representation (not a pyclass to avoid recursive Vec issues).
struct RustSymbol {
    name: String,
    kind: String, // "function", "async_function", "method", "async_method", "class", "variable", "reference"
    signature: Option<String>,
    type_annotation: Option<String>,
    returns: Option<String>,
    line: usize,
    end_line: usize,
    col_offset: usize,
    children: Vec<RustSymbol>,
    path: Vec<String>,
    depth: usize,
    decorators: Vec<String>,
    decorator_line_start: Option<usize>,
    param_names: Vec<String>,
}

/// Convert a RustSymbol tree to a Python dict (recursively).
fn symbol_to_pydict(py: Python, sym: &RustSymbol) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("name", &sym.name)?;
    d.set_item("kind", &sym.kind)?;
    d.set_item("signature", sym.signature.as_deref())?;
    d.set_item("type_annotation", sym.type_annotation.as_deref())?;
    d.set_item("returns", sym.returns.as_deref())?;
    d.set_item("line", sym.line)?;
    d.set_item("end_line", sym.end_line)?;
    d.set_item("col_offset", sym.col_offset)?;
    d.set_item("depth", sym.depth)?;

    let path_list = PyList::new(py, sym.path.iter().map(|s| s.as_str()))?;
    d.set_item("path", path_list)?;

    let dec_list = PyList::new(py, sym.decorators.iter().map(|s| s.as_str()))?;
    d.set_item("decorators", dec_list)?;

    d.set_item("decorator_line_start", sym.decorator_line_start)?;

    let param_list = PyList::new(py, sym.param_names.iter().map(|s| s.as_str()))?;
    d.set_item("param_names", param_list)?;

    let children_list = PyList::empty(py);
    for child in &sym.children {
        children_list.append(symbol_to_pydict(py, child)?)?;
    }
    d.set_item("children", children_list)?;

    Ok(d.into())
}

/// Get node text as a &str from source bytes.
pub fn node_text<'a>(node: tree_sitter::Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

/// Extract decorator strings from a `decorated_definition` node.
///
/// Returns (decorator_strings, decorator_line_start).
fn extract_decorators(node: tree_sitter::Node, source: &[u8]) -> (Vec<String>, Option<usize>) {
    let mut decorators = Vec::new();
    let mut first_line: Option<usize> = None;

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "decorator" {
            let line = child.start_position().row + 1;
            if first_line.is_none() {
                first_line = Some(line);
            }
            // Get the decorator text without the leading @
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

/// Extract parameter names from a function definition.
fn extract_param_names(func_node: tree_sitter::Node, source: &[u8]) -> Vec<String> {
    let params_node = match func_node.child_by_field_name("parameters") {
        Some(n) => n,
        None => return vec![],
    };

    let mut names = Vec::new();
    let mut cursor = params_node.walk();
    let children: Vec<tree_sitter::Node> = params_node.children(&mut cursor).collect();

    for child in &children {
        match child.kind() {
            "(" | ")" | "," => {}
            "identifier" => {
                names.push(node_text(*child, source).to_string());
            }
            "typed_parameter" => {
                let name = child
                    .named_child(0)
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                names.push(name.to_string());
            }
            "default_parameter" => {
                let name = child
                    .child_by_field_name("name")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                names.push(name.to_string());
            }
            "typed_default_parameter" => {
                let name = child
                    .child_by_field_name("name")
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                names.push(name.to_string());
            }
            "list_splat_pattern" => {
                let inner = child
                    .named_child(0)
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                names.push(format!("*{}", inner));
            }
            "dictionary_splat_pattern" => {
                let inner = child
                    .named_child(0)
                    .map(|n| node_text(n, source))
                    .unwrap_or("");
                names.push(format!("**{}", inner));
            }
            "positional_separator" => {
                names.push("/".to_string());
            }
            "keyword_separator" => {
                names.push("*".to_string());
            }
            _ => {}
        }
    }

    names
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
        let ret_clean = ret_text.trim_start_matches("->").trim();
        sig.push_str(&format!(" -> {}", ret_clean));
    }

    sig
}

/// Extract return type string from a function definition, or None.
fn extract_return_type(func_node: tree_sitter::Node, source: &[u8]) -> Option<String> {
    func_node
        .child_by_field_name("return_type")
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

/// Recursively collect symbols from a block/module node.
fn collect_from_body(
    node: tree_sitter::Node,
    source: &[u8],
    depth: usize,
    max_depth: usize,
    path: &[String],
    selector_path: &Option<Vec<String>>,
    defined_names_stack: &mut Vec<std::collections::HashSet<String>>,
    in_class: bool,
) -> Vec<RustSymbol> {
    let mut symbols: Vec<RustSymbol> = Vec::new();

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        match child.kind() {
            "function_definition" | "decorated_definition" => {
                // Extract decorators if this is a decorated_definition
                let (decorators, decorator_line_start) = if child.kind() == "decorated_definition" {
                    extract_decorators(child, source)
                } else {
                    (vec![], None)
                };

                // For decorated definitions, unwrap to the actual definition
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
                            // Use class_node line (the `class` keyword), not decorated_definition start
                            let start_line = class_node.start_position().row + 1;
                            let end_line = child.end_position().row + 1;
                            let col_offset = class_node.start_position().column;

                            let child_defined: HashSet<String> = HashSet::new();
                            defined_names_stack.push(child_defined);

                            let children = if let Some(body) = class_node.child_by_field_name("body") {
                                collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path, defined_names_stack, true)
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
                                returns: None,
                                line: start_line,
                                end_line,
                                col_offset,
                                children,
                                path: current_path,
                                depth,
                                decorators,
                                decorator_line_start,
                                param_names: vec![],
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
                    // Use func_node line (the `def` line), not the decorated_definition start
                    let start_line = func_node.start_position().row + 1;
                    let end_line = child.end_position().row + 1;
                    let col_offset = func_node.start_position().column;

                    // Detect async: look for "async" keyword
                    let is_async = {
                        let mut c = func_node.walk();
                        let children: Vec<tree_sitter::Node> = func_node.children(&mut c).collect();
                        children.iter().any(|ch| ch.kind() == "async")
                    };

                    // Determine kind based on whether we're inside a class
                    let kind = match (is_async, in_class) {
                        (true, true) => "async_method",
                        (true, false) => "async_function",
                        (false, true) => "method",
                        (false, false) => "function",
                    };

                    let sig = extract_signature(func_node, source);
                    let returns = extract_return_type(func_node, source);
                    let param_names = extract_param_names(func_node, source);

                    // Collect child symbols from function body
                    let child_defined: HashSet<String> = HashSet::new();
                    defined_names_stack.push(child_defined);

                    // Functions inside functions are NOT methods
                    let mut children = if let Some(body) = func_node.child_by_field_name("body") {
                        collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path, defined_names_stack, false)
                    } else {
                        vec![]
                    };

                    // Collect outer-scope references if depth allows
                    if depth + 1 < max_depth {
                        if let Some(body) = func_node.child_by_field_name("body") {
                            let mut loaded: Vec<String> = Vec::new();
                            collect_loaded_names(body, source, &mut loaded);

                            let stack_len = defined_names_stack.len();
                            let mut seen: HashSet<String> = HashSet::new();
                            for name_ref in &loaded {
                                if seen.contains(name_ref) {
                                    continue;
                                }
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
                                            returns: None,
                                            line: 0,
                                            end_line: 0,
                                            col_offset: 0,
                                            children: vec![],
                                            path: vec![],
                                            depth: depth + 1,
                                            decorators: vec![],
                                            decorator_line_start: None,
                                            param_names: vec![],
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
                        line: start_line,
                        end_line,
                        col_offset,
                        children,
                        path: current_path,
                        depth,
                        decorators,
                        decorator_line_start,
                        param_names,
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
                    let col_offset = child.start_position().column;

                    let child_defined: HashSet<String> = HashSet::new();
                    defined_names_stack.push(child_defined);

                    let children = if let Some(body) = child.child_by_field_name("body") {
                        collect_from_body(body, source, depth + 1, max_depth, &current_path, selector_path, defined_names_stack, true)
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
                        returns: None,
                        line: start_line,
                        end_line,
                        col_offset,
                        children,
                        path: current_path,
                        depth,
                        decorators: vec![],
                        decorator_line_start: None,
                        param_names: vec![],
                    });
                } else {
                    if let Some(top) = defined_names_stack.last_mut() {
                        top.insert(name);
                    }
                }
            }

            "expression_statement" => {
                let mut inner_cursor = child.walk();
                for inner_child in child.children(&mut inner_cursor) {
                    if inner_child.kind() == "assignment" {
                        if let Some(left) = inner_child.child_by_field_name("left") {
                            if left.kind() == "identifier" {
                                let name = node_text(left, source).to_string();
                                let current_path: Vec<String> = path.iter().cloned().chain(std::iter::once(name.clone())).collect();

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
                                        line: start_line,
                                        end_line,
                                        col_offset,
                                        children: vec![],
                                        path: current_path,
                                        depth,
                                        decorators: vec![],
                                        decorator_line_start: None,
                                        param_names: vec![],
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

    collect_from_body(root, source_bytes, 0, max_depth, &[], selector, &mut defined_names_stack, false)
}

/// Collect symbols from multiple Python files in parallel.
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
            let content = std::fs::read_to_string(&path).ok()?;
            let syms = collect_symbols_from_source(&content, depth, &selector_path);
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
#[pyo3(signature = (source, max_depth=None, selector=None))]
pub fn collect_symbols_from_str(
    py: Python,
    source: &str,
    max_depth: Option<usize>,
    selector: Option<String>,
) -> PyResult<PyObject> {
    let depth = max_depth.unwrap_or(usize::MAX);
    let selector_path: Option<Vec<String>> = selector.map(|s| {
        s.split('.').map(|part| part.to_string()).collect()
    });

    let rust_syms = collect_symbols_from_source(source, depth, &selector_path);

    let py_list = PyList::empty(py);
    for sym in &rust_syms {
        py_list.append(symbol_to_pydict(py, sym)?)?;
    }

    Ok(py_list.into())
}

/// Get the line ranges of all simple statements in a Python source string.
///
/// Returns a list of (start_line, end_line) tuples, 1-indexed.
/// Used for mapping noqa comments to their enclosing statement ranges.
#[pyfunction]
pub fn get_statement_ranges(source: &str) -> PyResult<Vec<(usize, usize)>> {
    let tree = match crate::pattern::parse_python(source) {
        Some(t) => t,
        None => return Ok(vec![]),
    };

    let source_bytes = source.as_bytes();
    let root = tree.root_node();
    let mut ranges = Vec::new();

    fn collect_simple_stmts(node: tree_sitter::Node, _source: &[u8], ranges: &mut Vec<(usize, usize)>) {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            match child.kind() {
                "expression_statement" | "return_statement" | "delete_statement"
                | "raise_statement" | "pass_statement" | "break_statement"
                | "continue_statement" | "import_statement" | "import_from_statement"
                | "future_import_statement" | "global_statement" | "nonlocal_statement"
                | "assert_statement" | "type_alias_statement" | "print_statement" => {
                    let start = child.start_position().row + 1;
                    let end = child.end_position().row + 1;
                    ranges.push((start, end));
                }
                // Recurse into compound statements to find nested simples
                "if_statement" | "for_statement" | "while_statement" | "try_statement"
                | "with_statement" | "function_definition" | "class_definition"
                | "decorated_definition" | "match_statement" | "block" | "module"
                | "elif_clause" | "else_clause" | "except_clause" | "finally_clause"
                | "case_clause" => {
                    collect_simple_stmts(child, _source, ranges);
                }
                _ => {}
            }
        }
    }

    collect_simple_stmts(root, source_bytes, &mut ranges);
    Ok(ranges)
}
