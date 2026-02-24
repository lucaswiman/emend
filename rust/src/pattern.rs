//! Tree-sitter-based pattern matching for Python code.

use crate::Match;
use tree_sitter::{Language, Parser, Node, Tree};

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
fn parse_python(source: &str) -> Option<Tree> {
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
