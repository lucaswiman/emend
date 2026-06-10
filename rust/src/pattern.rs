//! Tree-sitter-based pattern matching for Python code.

use crate::Match;
use tree_sitter::{Parser, Node, Tree};

/// Get a thread-local parser for a specific language.
fn get_parser(lang_name: &str) -> Parser {
    let mut parser = Parser::new();
    let language = match lang_name {
        "python" => tree_sitter_python::LANGUAGE,
        "typescript" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT,
        "tsx" => tree_sitter_typescript::LANGUAGE_TSX,
        "rust" => tree_sitter_rust::LANGUAGE,
        "html" => tree_sitter_html::LANGUAGE,
        "css" => tree_sitter_css::LANGUAGE,
        "sql" => tree_sitter_sequel::LANGUAGE,
        "jinja2" => tree_sitter_jinja2::LANGUAGE,
        "datalog" => tree_sitter_souffle::LANGUAGE,
        _ => tree_sitter_python::LANGUAGE,
    };
    parser
        .set_language(&language.into())
        .expect("Failed to set language");
    parser
}

/// Parse Python source into a tree-sitter Tree.
pub(crate) fn parse_python(source: &str) -> Option<Tree> {
    let mut parser = get_parser("python");
    parser.parse(source.as_bytes(), None)
}

/// Parse TypeScript/TSX source into a tree-sitter Tree.
pub(crate) fn parse_typescript(source: &str, is_tsx: bool) -> Option<Tree> {
    let mut parser = if is_tsx { get_parser("tsx") } else { get_parser("typescript") };
    parser.parse(source.as_bytes(), None)
}

/// Parse Rust source into a tree-sitter Tree.
pub(crate) fn parse_rust(source: &str) -> Option<Tree> {
    let mut parser = get_parser("rust");
    parser.parse(source.as_bytes(), None)
}

/// Parse HTML source into a tree-sitter Tree.
pub(crate) fn parse_html(source: &str) -> Option<Tree> {
    let mut parser = get_parser("html");
    parser.parse(source.as_bytes(), None)
}

/// Parse CSS source into a tree-sitter Tree.
pub(crate) fn parse_css(source: &str) -> Option<Tree> {
    let mut parser = get_parser("css");
    parser.parse(source.as_bytes(), None)
}

/// Parse SQL source into a tree-sitter Tree.
pub(crate) fn parse_sql(source: &str) -> Option<Tree> {
    let mut parser = get_parser("sql");
    parser.parse(source.as_bytes(), None)
}

/// Parse Jinja2 source into a tree-sitter Tree.
pub(crate) fn parse_jinja2(source: &str) -> Option<Tree> {
    let mut parser = get_parser("jinja2");
    parser.parse(source.as_bytes(), None)
}

/// Parse Datalog (Soufflé) source into a tree-sitter Tree.
pub(crate) fn parse_datalog(source: &str) -> Option<Tree> {
    let mut parser = get_parser("datalog");
    parser.parse(source.as_bytes(), None)
}

/// Parse source based on file extension.
pub(crate) fn parse_by_extension(source: &str, ext: &str) -> Option<Tree> {
    match ext {
        "py" | "pyi" => parse_python(source),
        "ts" => parse_typescript(source, false),
        "tsx" => parse_typescript(source, true),
        "js" | "jsx" => parse_typescript(source, false), // JS uses TS grammar
        "rs" => parse_rust(source),
        "html" | "htm" => parse_html(source),
        "css" => parse_css(source),
        "sql" => parse_sql(source),
        "jinja" | "jinja2" | "j2" => parse_jinja2(source),
        "dl" | "datalog" => parse_datalog(source),
        _ => parse_python(source),
    }
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

/// A single call site extracted from source code.
#[derive(Clone, Debug)]
pub struct CallSiteInfo {
    /// The callee expression text (e.g. "foo", "obj.method").
    pub callee: String,
    /// Individual argument texts, parsed via tree-sitter.
    pub args: Vec<String>,
    /// 1-based line number.
    pub line: usize,
    /// 0-based column offset.
    pub col: usize,
    /// True if the callee is an attribute access (e.g. `obj.method()`).
    pub is_method: bool,
}

/// Extract all call sites from source code using tree-sitter.
///
/// Walks the parse tree to find call expression nodes, then extracts the
/// callee name and individual arguments as separate strings.  This avoids
/// error-prone regex or string-splitting approaches for argument extraction.
///
/// The `call_node_type` and `args_field_name` parameters are read from
/// the language config (e.g. `"call"` / `"arguments"` for Python,
/// `"call_expression"` / `"arguments"` for TypeScript/Rust).
pub fn extract_call_sites(
    source: &str,
    lang_name: &str,
    call_node_type: &str,
    args_field_name: &str,
) -> Vec<CallSiteInfo> {
    let mut parser = get_parser(lang_name);
    let tree = match parser.parse(source.as_bytes(), None) {
        Some(t) => t,
        None => return Vec::new(),
    };

    let source_bytes = source.as_bytes();
    let mut results = Vec::new();

    walk_tree(tree.root_node(), source_bytes, &mut |node| {
        if node.kind() != call_node_type {
            return;
        }

        // Extract the callee (function field)
        let func_node = match node.child_by_field_name("function") {
            Some(n) => n,
            None => return,
        };
        let callee = std::str::from_utf8(&source_bytes[func_node.start_byte()..func_node.end_byte()])
            .unwrap_or("")
            .to_string();
        if callee.is_empty() {
            return;
        }

        let is_method = func_node.kind() == "attribute"
            || func_node.kind() == "field_expression"  // Rust
            || func_node.kind() == "member_expression"; // TS

        // Extract individual arguments from the arguments node
        let args_node = match node.child_by_field_name(args_field_name) {
            Some(n) => n,
            None => {
                // Fallback: try "arguments" directly if field name didn't work
                match node.child_by_field_name("arguments") {
                    Some(n) => n,
                    None => return,
                }
            }
        };

        let mut args = Vec::new();
        let child_count = args_node.named_child_count();
        for i in 0..child_count {
            if let Some(arg_node) = args_node.named_child(i) {
                let arg_text = std::str::from_utf8(
                    &source_bytes[arg_node.start_byte()..arg_node.end_byte()],
                )
                .unwrap_or("")
                .to_string();
                if !arg_text.is_empty() {
                    args.push(arg_text);
                }
            }
        }

        results.push(CallSiteInfo {
            callee,
            args,
            line: node.start_position().row + 1,
            col: node.start_position().column,
            is_method,
        });
    });

    results
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
/// Returns a list of (name, line, start_col, end_col) tuples, all 0-indexed
/// (tree-sitter native rows and byte columns).  Used by type_oracle to collect
/// symbol positions from the parse tree.
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
                    let line = node.start_position().row;
                    let start_col = node.start_position().column;
                    let end_col = node.end_position().column;
                    results.push((text.to_string(), line, start_col, end_col));
                }
            }
            "attribute" => {
                // For `obj.attr`, emit the full dotted name
                let text = std::str::from_utf8(&source[node.start_byte()..node.end_byte()])
                    .unwrap_or("");
                let line = node.start_position().row;
                let start_col = node.start_position().column;
                let end_col = node.end_position().column;
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


/// Collect all string literal nodes from source code.
///
/// Returns a list of `(start_byte, end_byte, start_line, start_col, end_line, end_col, content)`
/// tuples where `content` is the unquoted string content (inner text only, no surrounding
/// quote characters).  Both single-quoted and triple-quoted strings are handled correctly.
///
/// Convention exception: lines are **1-indexed**, columns are **0-indexed** (matches the
/// `DslRegion` contract consumed by `dsl.py`).
///
/// The `ext` parameter selects the tree-sitter language (e.g. `"py"`, `"ts"`, `"rs"`).
pub fn collect_string_literals(source: &str, ext: &str) -> Vec<(u32, u32, u32, u32, u32, u32, String)> {
    let tree = match parse_by_extension(source, ext) {
        Some(t) => t,
        None => return vec![],
    };
    let source_bytes = source.as_bytes();
    let root = tree.root_node();
    let mut results: Vec<(u32, u32, u32, u32, u32, u32, String)> = Vec::new();

    fn extract_string_content(node: Node, source: &[u8]) -> String {
        // Try to find string_content children (Python grammar).
        // For each child, collect the text of nodes that are NOT the quote nodes
        // (i.e. not string_start / string_end).
        let mut content_parts: Vec<&[u8]> = Vec::new();
        let mut cursor = node.walk();
        let mut has_content_children = false;
        if cursor.goto_first_child() {
            loop {
                let child = cursor.node();
                let kind = child.kind();
                // Skip opening/closing quote tokens
                if kind != "string_start" && kind != "string_end"
                    && kind != "\"\"\"" && kind != "'''"
                    && kind != "\"" && kind != "'"
                {
                    has_content_children = true;
                    content_parts.push(&source[child.start_byte()..child.end_byte()]);
                }
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
        }

        if has_content_children {
            String::from_utf8_lossy(&content_parts.concat()).into_owned()
        } else {
            // Fallback: strip quotes from the full node text
            let full = std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("");
            strip_string_quotes(full).to_string()
        }
    }

    fn collect_strings(node: Node, source: &[u8], results: &mut Vec<(u32, u32, u32, u32, u32, u32, String)>) {
        let kind = node.kind();
        if kind == "string" || kind == "string_literal" || kind == "raw_string_literal"
            || kind == "interpreted_string_literal"
        {
            let content = extract_string_content(node, source);
            results.push((
                node.start_byte() as u32,
                node.end_byte() as u32,
                node.start_position().row as u32 + 1,
                node.start_position().column as u32,
                node.end_position().row as u32 + 1,
                node.end_position().column as u32,
                content,
            ));
            // Don't recurse into string children — content already captured
            return;
        }
        let mut cursor = node.walk();
        if cursor.goto_first_child() {
            loop {
                collect_strings(cursor.node(), source, results);
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
        }
    }

    collect_strings(root, source_bytes, &mut results);
    results
}

/// Strip surrounding quote characters from a string literal token.
///
/// Handles single-quoted (`'...'`, `"..."`), triple-quoted (`"""..."""`, `'''...'''`),
/// and prefix-quoted (`r'...'`, `b"..."`, `f'''...'''`, etc.) strings.
fn strip_string_quotes(s: &str) -> &str {
    // Skip prefix characters (r, b, f, u, rb, br, etc.)
    let after_prefix = s.trim_start_matches(|c: char| matches!(c, 'r' | 'R' | 'b' | 'B' | 'f' | 'F' | 'u' | 'U'));
    // Detect triple-quoted
    if after_prefix.starts_with("\"\"\"") && after_prefix.ends_with("\"\"\"") && after_prefix.len() >= 6 {
        return &after_prefix[3..after_prefix.len()-3];
    }
    if after_prefix.starts_with("'''") && after_prefix.ends_with("'''") && after_prefix.len() >= 6 {
        return &after_prefix[3..after_prefix.len()-3];
    }
    // Single-quoted
    if after_prefix.starts_with('"') && after_prefix.ends_with('"') && after_prefix.len() >= 2 {
        return &after_prefix[1..after_prefix.len()-1];
    }
    if after_prefix.starts_with('\'') && after_prefix.ends_with('\'') && after_prefix.len() >= 2 {
        return &after_prefix[1..after_prefix.len()-1];
    }
    s
}

/// Collect all comment nodes from source code.
///
/// Returns a list of `(start_line, start_col, text)` tuples where `text` is the
/// full comment text including the leading comment prefix (e.g. `# ...`).
///
/// Convention exception: `start_line` is **1-indexed**, `start_col` is **0-indexed**.
///
/// The `ext` parameter selects the tree-sitter language (e.g. `"py"`, `"ts"`, `"rs"`).
pub fn collect_comments(source: &str, ext: &str) -> Vec<(u32, u32, String)> {
    let tree = match parse_by_extension(source, ext) {
        Some(t) => t,
        None => return vec![],
    };
    let source_bytes = source.as_bytes();
    let root = tree.root_node();
    let mut results: Vec<(u32, u32, String)> = Vec::new();

    fn collect_comment_nodes(node: Node, source: &[u8], results: &mut Vec<(u32, u32, String)>) {
        let kind = node.kind();
        if kind == "comment" || kind == "line_comment" || kind == "block_comment" {
            let text = std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("");
            results.push((
                node.start_position().row as u32 + 1,
                node.start_position().column as u32,
                text.to_string(),
            ));
            return;
        }
        let mut cursor = node.walk();
        if cursor.goto_first_child() {
            loop {
                collect_comment_nodes(cursor.node(), source, results);
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
        }
    }

    collect_comment_nodes(root, source_bytes, &mut results);
    results
}
