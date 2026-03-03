//! Tree-sitter-based pattern matching for Python code.

use crate::Match;
use tree_sitter::{Parser, Node, Tree};

/// Get a thread-local Python parser.
fn get_parser() -> Parser {
    let mut parser = Parser::new();
    let language = tree_sitter_python::LANGUAGE;
    parser
        .set_language(&language.into())
        .expect("Failed to set Python language");
    parser
}

/// Parse Python source into a tree-sitter Tree.
pub(crate) fn parse_python(source: &str) -> Option<Tree> {
    let mut parser = get_parser();
    parser.parse(source.as_bytes(), None)
}

/// Find all `identifier` nodes in the tree that match `target_name`.
pub fn find_identifiers(source: &str, target_name: &str, file_path: &str) -> Vec<Match> {
    let tree = match parse_python(source) {
        Some(t) => t,
        None => return Vec::new(),
    };

    let mut matches = Vec::new();
    let source_bytes = source.as_bytes();

    walk_tree(tree.root_node(), source_bytes, &mut |node| {
        if node.kind() == "identifier" {
            let text = &source_bytes[node.start_byte()..node.end_byte()];
            if text == target_name.as_bytes() {
                matches.push(Match {
                    file: file_path.to_string(),
                    line: node.start_position().row + 1,
                    column: node.start_position().column,
                    end_line: node.end_position().row + 1,
                    end_column: node.end_position().column,
                    matched_text: target_name.to_string(),
                });
            }
        }
    });

    matches
}

/// Find all call expressions where the function name matches `target_name`.
///
/// Matches both direct calls `foo()` and attribute calls `obj.foo()`.
pub fn find_calls(source: &str, target_name: &str, file_path: &str) -> Vec<Match> {
    let tree = match parse_python(source) {
        Some(t) => t,
        None => return Vec::new(),
    };

    let mut matches = Vec::new();
    let source_bytes = source.as_bytes();

    walk_tree(tree.root_node(), source_bytes, &mut |node| {
        if node.kind() == "call" {
            if let Some(func_node) = node.child_by_field_name("function") {
                let is_match = match func_node.kind() {
                    // Direct call: foo(...)
                    "identifier" => {
                        let text = &source_bytes[func_node.start_byte()..func_node.end_byte()];
                        text == target_name.as_bytes()
                    }
                    // Attribute call: obj.foo(...)
                    "attribute" => {
                        if let Some(attr) = func_node.child_by_field_name("attribute") {
                            let text = &source_bytes[attr.start_byte()..attr.end_byte()];
                            text == target_name.as_bytes()
                        } else {
                            false
                        }
                    }
                    _ => false,
                };

                if is_match {
                    let call_text = std::str::from_utf8(
                        &source_bytes[node.start_byte()..node.end_byte()],
                    )
                    .unwrap_or("")
                    .to_string();

                    matches.push(Match {
                        file: file_path.to_string(),
                        line: node.start_position().row + 1,
                        column: node.start_position().column,
                        end_line: node.end_position().row + 1,
                        end_column: node.end_position().column,
                        matched_text: call_text,
                    });
                }
            }
        }
    });

    matches
}

/// Find method calls: `$X.<method_name>(...)`.
///
/// Returns matches where `method_name` appears as the attribute in a
/// call expression on any object.
pub fn find_method_calls(source: &str, method_name: &str, file_path: &str) -> Vec<Match> {
    let tree = match parse_python(source) {
        Some(t) => t,
        None => return Vec::new(),
    };

    let mut matches = Vec::new();
    let source_bytes = source.as_bytes();

    walk_tree(tree.root_node(), source_bytes, &mut |node| {
        if node.kind() == "call" {
            if let Some(func_node) = node.child_by_field_name("function") {
                if func_node.kind() == "attribute" {
                    if let Some(attr) = func_node.child_by_field_name("attribute") {
                        let text = &source_bytes[attr.start_byte()..attr.end_byte()];
                        if text == method_name.as_bytes() {
                            let call_text = std::str::from_utf8(
                                &source_bytes[node.start_byte()..node.end_byte()],
                            )
                            .unwrap_or("")
                            .to_string();

                            matches.push(Match {
                                file: file_path.to_string(),
                                line: node.start_position().row + 1,
                                column: node.start_position().column,
                                end_line: node.end_position().row + 1,
                                end_column: node.end_position().column,
                                matched_text: call_text,
                            });
                        }
                    }
                }
            }
        }
    });

    matches
}

/// Extract all module names from import statements.
///
/// Returns a list of dotted module names that the file imports from.
pub fn extract_import_modules(source: &str) -> Vec<String> {
    let tree = match parse_python(source) {
        Some(t) => t,
        None => return Vec::new(),
    };

    let mut modules = Vec::new();
    let source_bytes = source.as_bytes();

    walk_tree(tree.root_node(), source_bytes, &mut |node| {
        match node.kind() {
            // `import foo.bar` or `import foo.bar as baz`
            "import_statement" => {
                for i in 0..node.named_child_count() {
                    if let Some(child) = node.named_child(i) {
                        if child.kind() == "dotted_name" || child.kind() == "aliased_import" {
                            let name_node = if child.kind() == "aliased_import" {
                                child.child_by_field_name("name")
                            } else {
                                Some(child)
                            };
                            if let Some(n) = name_node {
                                let text = std::str::from_utf8(
                                    &source_bytes[n.start_byte()..n.end_byte()],
                                )
                                .unwrap_or("");
                                if !text.is_empty() {
                                    // Add full module and all prefixes
                                    let parts: Vec<&str> = text.split('.').collect();
                                    for j in 1..=parts.len() {
                                        modules.push(parts[..j].join("."));
                                    }
                                }
                            }
                        }
                    }
                }
            }
            // `from foo.bar import baz`
            "import_from_statement" => {
                if let Some(mod_node) = node.child_by_field_name("module_name") {
                    let text = std::str::from_utf8(
                        &source_bytes[mod_node.start_byte()..mod_node.end_byte()],
                    )
                    .unwrap_or("");
                    // Skip relative imports (starting with .)
                    if !text.is_empty() && !text.starts_with('.') {
                        let parts: Vec<&str> = text.split('.').collect();
                        for j in 1..=parts.len() {
                            modules.push(parts[..j].join("."));
                        }
                    }
                }
            }
            _ => {}
        }
    });

    modules.sort();
    modules.dedup();
    modules
}

/// Check if a Python source file imports from a specific target module.
///
/// Returns true if the source contains an import that matches `target_module`
/// (including prefix expansion: `from a.b.c import x` matches `a.b.c`).
pub fn files_importing_module_from_source(source: &str, target_module: &str) -> bool {
    let modules = extract_import_modules(source);
    modules.contains(&target_module.to_string())
}

/// Collect callees for all top-level functions/classes in a Python source file.
///
/// Returns Vec<(symbol_name, Vec<callee_name>)> where each callee_name is
/// the bare function/method name extracted from call expressions.
/// Deduplicates callee names within each symbol (matching _CalleeCollector._seen behavior).
pub fn collect_callees_from_source(source: &str) -> Vec<(String, Vec<String>)> {
    let tree = match parse_python(source) {
        Some(t) => t,
        None => return Vec::new(),
    };

    let source_bytes = source.as_bytes();
    let root = tree.root_node();
    let mut results = Vec::new();

    // Walk direct children of the module (top-level statements)
    let mut cursor = root.walk();
    if cursor.goto_first_child() {
        loop {
            let node = cursor.node();
            let sym_name = match node.kind() {
                "function_definition" | "class_definition" => {
                    node.child_by_field_name("name")
                        .and_then(|n| std::str::from_utf8(&source_bytes[n.start_byte()..n.end_byte()]).ok())
                        .map(|s| s.to_string())
                }
                // decorated definition: decorator* + function_definition/class_definition
                "decorated_definition" => {
                    // The last child should be the actual function/class def
                    let child_count = node.child_count();
                    let mut def_name = None;
                    for i in 0..child_count {
                        if let Some(child) = node.child(i) {
                            if child.kind() == "function_definition" || child.kind() == "class_definition" {
                                def_name = child.child_by_field_name("name")
                                    .and_then(|n| std::str::from_utf8(&source_bytes[n.start_byte()..n.end_byte()]).ok())
                                    .map(|s| s.to_string());
                            }
                        }
                    }
                    def_name
                }
                _ => None,
            };

            if let Some(name) = sym_name {
                let mut callees: Vec<String> = Vec::new();
                let mut seen = std::collections::HashSet::new();

                walk_tree(node, source_bytes, &mut |n| {
                    if n.kind() == "call" {
                        if let Some(func_node) = n.child_by_field_name("function") {
                            let callee_name = match func_node.kind() {
                                "identifier" => {
                                    std::str::from_utf8(&source_bytes[func_node.start_byte()..func_node.end_byte()])
                                        .ok()
                                        .map(|s| s.to_string())
                                }
                                "attribute" => {
                                    func_node.child_by_field_name("attribute")
                                        .and_then(|attr| std::str::from_utf8(&source_bytes[attr.start_byte()..attr.end_byte()]).ok())
                                        .map(|s| s.to_string())
                                }
                                _ => None,
                            };
                            if let Some(cn) = callee_name {
                                if seen.insert(cn.clone()) {
                                    callees.push(cn);
                                }
                            }
                        }
                    }
                });

                results.push((name, callees));
            }

            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }

    results
}

/// Walk all nodes in a tree-sitter tree, calling `f` on each node.
fn walk_tree<F>(node: Node, source: &[u8], f: &mut F)
where
    F: FnMut(Node),
{
    f(node);
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            walk_tree(cursor.node(), source, f);
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
}

/// Python keywords that look like identifiers in tree-sitter but aren't.
const PYTHON_KEYWORDS: &[&str] = &["True", "False", "None"];

/// Collect all identifier and attribute positions from Python source.
///
/// Returns a list of (name, line, start_col_1indexed, end_col_1indexed) tuples.
/// Used by type_oracle to replace LibCST's _SymbolCollector.
pub fn collect_identifier_positions(source: &str) -> Vec<(String, usize, usize, usize)> {
    let tree = match parse_python(source) {
        Some(t) => t,
        None => return vec![],
    };

    let source_bytes = source.as_bytes();
    let root = tree.root_node();
    let mut results: Vec<(String, usize, usize, usize)> = Vec::new();

    fn collect(node: Node, source: &[u8], results: &mut Vec<(String, usize, usize, usize)>) {
        match node.kind() {
            "identifier" => {
                let text = std::str::from_utf8(&source[node.start_byte()..node.end_byte()])
                    .unwrap_or("");
                if !PYTHON_KEYWORDS.contains(&text) {
                    let line = node.start_position().row + 1;
                    let start_col = node.start_position().column + 1;
                    let end_col = node.end_position().column + 1;
                    results.push((text.to_string(), line, start_col, end_col));
                }
            }
            "attribute" => {
                // For `obj.attr`, emit the full dotted name
                let text = std::str::from_utf8(&source[node.start_byte()..node.end_byte()])
                    .unwrap_or("");
                let line = node.start_position().row + 1;
                let start_col = node.start_position().column + 1;
                let end_col = node.end_position().column + 1;
                results.push((text.to_string(), line, start_col, end_col));
                // Don't recurse into children — we've captured the whole attribute
                return;
            }
            _ => {}
        }
        let mut cursor = node.walk();
        if cursor.goto_first_child() {
            loop {
                collect(cursor.node(), source, results);
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
        }
    }

    collect(root, source_bytes, &mut results);
    results
}
