//! emend_core: Fast Python pattern matching engine for emend.
//!
//! Provides parallel file scanning, name-hint pre-filtering, and
//! tree-sitter-based pattern matching exposed to Python via PyO3.

use pyo3::prelude::*;
use rayon::prelude::*;
use std::path::Path;

mod scanner;
mod pattern;
mod symbols;
mod matcher;
mod scope;
mod scope_py;
mod transform;
mod transform_py;
mod cozo_db;
mod cfg;
mod cfg_py;
mod tree_py;

/// A match result returned to Python.
#[pyclass]
#[derive(Clone, Debug)]
pub struct Match {
    #[pyo3(get)]
    pub file: String,
    #[pyo3(get)]
    pub line: usize,
    #[pyo3(get)]
    pub column: usize,
    #[pyo3(get)]
    pub end_line: usize,
    #[pyo3(get)]
    pub end_column: usize,
    #[pyo3(get)]
    pub matched_text: String,
}

/// Collect all Python files under `root`, skipping common non-project dirs.
///
/// Uses parallel directory walking for speed.
#[pyfunction]
fn collect_python_files(root: &str) -> PyResult<Vec<String>> {
    let files = scanner::collect_python_files(Path::new(root));
    Ok(files.into_iter().map(|p| p.to_string_lossy().into_owned()).collect())
}

/// Collect all files under `root` with specific extensions, skipping non-project dirs.
///
/// Extensions should NOT include the leading dot.
#[pyfunction]
fn collect_files(root: &str, extensions: Vec<String>) -> PyResult<Vec<String>> {
    let exts_ref: Vec<&str> = extensions.iter().map(|s| s.as_str()).collect();
    let files = scanner::collect_files(Path::new(root), &exts_ref);
    Ok(files.into_iter().map(|p| p.to_string_lossy().into_owned()).collect())
}

/// Return the list of non-dot directory names that are skipped during scanning.
///
/// All directories starting with '.' are also skipped automatically.
#[pyfunction]
fn skip_dirs() -> Vec<&'static str> {
    scanner::SKIP_DIRS.to_vec()
}

/// Pre-filter files: return only those whose content contains `name_hint`.
///
/// Reads files in parallel using rayon.
#[pyfunction]
fn filter_files_by_content(files: Vec<String>, name_hint: &str) -> PyResult<Vec<String>> {
    let hint = name_hint.to_string();
    let result: Vec<String> = files
        .into_par_iter()
        .filter(|path| {
            std::fs::read_to_string(path)
                .map(|content| content.contains(&hint))
                .unwrap_or(false)
        })
        .collect();
    Ok(result)
}

/// Find all occurrences of a literal name in Python files using tree-sitter.
///
/// This is the fast path for simple patterns (no metavariables). It parses
/// each file with tree-sitter and finds all `identifier` nodes matching the
/// target name. Files are processed in parallel.
#[pyfunction]
fn find_name_in_files(files: Vec<String>, name: &str) -> PyResult<Vec<Match>> {
    let target = name.to_string();
    let results: Vec<Vec<Match>> = files
        .into_par_iter()
        .filter_map(|path| {
            let content = std::fs::read_to_string(&path).ok()?;
            // Fast pre-filter: skip files that don't contain the name
            if !content.contains(&target) {
                return None;
            }
            let matches = pattern::find_identifiers(&content, &target, &path);
            if matches.is_empty() {
                None
            } else {
                Some(matches)
            }
        })
        .collect();
    Ok(results.into_iter().flatten().collect())
}

/// Find all function/method call sites of `name` in Python files using tree-sitter.
///
/// Returns matches where `name` appears as the function in a call expression.
/// Files are processed in parallel.
#[pyfunction]
fn find_calls_in_files(files: Vec<String>, name: &str) -> PyResult<Vec<Match>> {
    let target = name.to_string();
    let results: Vec<Vec<Match>> = files
        .into_par_iter()
        .filter_map(|path| {
            let content = std::fs::read_to_string(&path).ok()?;
            if !content.contains(&target) {
                return None;
            }
            let matches = pattern::find_calls(&content, &target, &path);
            if matches.is_empty() {
                None
            } else {
                Some(matches)
            }
        })
        .collect();
    Ok(results.into_iter().flatten().collect())
}

/// Find attribute access patterns like `$X.<name>(...)` in files.
///
/// Matches method calls where the method name matches `name`.
/// Returns the full `obj.method(args)` text.
#[pyfunction]
fn find_method_calls_in_files(files: Vec<String>, method_name: &str) -> PyResult<Vec<Match>> {
    let target = method_name.to_string();
    let results: Vec<Vec<Match>> = files
        .into_par_iter()
        .filter_map(|path| {
            let content = std::fs::read_to_string(&path).ok()?;
            if !content.contains(&target) {
                return None;
            }
            let matches = pattern::find_method_calls(&content, &target, &path);
            if matches.is_empty() {
                None
            } else {
                Some(matches)
            }
        })
        .collect();
    Ok(results.into_iter().flatten().collect())
}

/// Read files in parallel and return (path, content) for files matching all hints.
///
/// If hints is empty, reads all files (parallel I/O only).
/// All hint strings must appear in the file content (AND logic).
#[pyfunction]
fn read_and_filter_files(files: Vec<String>, hints: Vec<String>) -> PyResult<Vec<(String, String)>> {
    let results: Vec<(String, String)> = files
        .into_par_iter()
        .filter_map(|path| {
            let content = std::fs::read_to_string(&path).ok()?;
            for hint in &hints {
                if !content.contains(hint.as_str()) {
                    return None;
                }
            }
            Some((path, content))
        })
        .collect();
    Ok(results)
}

/// Collect callees for all top-level symbols in a Python source file.
///
/// Returns a list of (symbol_name, [callee_names]) tuples.
/// Callee names are bare function/method names (deduplicated).
#[pyfunction]
fn collect_callees(source: &str) -> PyResult<Vec<(String, Vec<String>)>> {
    Ok(pattern::collect_callees_from_source(source))
}

/// Extract all import statements from Python files in parallel.
///
/// Returns a list of (file_path, module_name) tuples representing
/// what each file imports from.
#[pyfunction]
fn extract_imports(files: Vec<String>) -> PyResult<Vec<(String, Vec<String>)>> {
    let results: Vec<(String, Vec<String>)> = files
        .into_par_iter()
        .filter_map(|path| {
            let content = std::fs::read_to_string(&path).ok()?;
            let modules = pattern::extract_import_modules(&content);
            if modules.is_empty() {
                None
            } else {
                Some((path, modules))
            }
        })
        .collect();
    Ok(results)
}

/// Find files that import from a specific target module.
///
/// For each file (in parallel):
/// 1. Read the file
/// 2. Text check: skip files not containing the target module name as a string
/// 3. Parse with tree-sitter, extract import modules (with prefix expansion)
/// 4. Check if target_module is among the extracted modules
///
/// Returns paths of files that import from the target module.
#[pyfunction]
fn files_importing_module(files: Vec<String>, target_module: &str) -> PyResult<Vec<String>> {
    let target = target_module.to_string();
    let result: Vec<String> = files
        .into_par_iter()
        .filter(|path| {
            std::fs::read_to_string(path)
                .map(|content| {
                    if !content.contains(&target) {
                        return false;
                    }
                    pattern::files_importing_module_from_source(&content, &target)
                })
                .unwrap_or(false)
        })
        .collect();
    Ok(result)
}

/// Collect all identifier and attribute positions from Python source.
///
/// Returns a list of (name, line, start_col, end_col) tuples (1-indexed).
#[pyfunction]
fn collect_identifier_positions(source: &str) -> PyResult<Vec<(String, usize, usize, usize)>> {
    Ok(pattern::collect_identifier_positions(source))
}

/// Collect all string literal nodes from source code.
///
/// Returns a list of (start_byte, end_byte, start_line, start_col, end_line, end_col, content)
/// tuples where content is the unquoted inner text of the string.
///
/// The `ext` parameter selects the tree-sitter language (e.g. "py", "ts", "rs").
/// Defaults to "py" if not provided.
#[pyfunction]
#[pyo3(signature = (source, ext="py"))]
fn collect_string_literals(source: &str, ext: &str) -> PyResult<Vec<(u32, u32, u32, u32, u32, u32, String)>> {
    Ok(pattern::collect_string_literals(source, ext))
}

/// Collect all comment nodes from source code.
///
/// Returns a list of (start_line, start_col, text) tuples where text is the
/// full comment text including the leading comment prefix (e.g. `# ...`).
///
/// The `ext` parameter selects the tree-sitter language (e.g. "py", "ts", "rs").
/// Defaults to "py" if not provided.
#[pyfunction]
#[pyo3(signature = (source, ext="py"))]
fn collect_comments(source: &str, ext: &str) -> PyResult<Vec<(u32, u32, String)>> {
    Ok(pattern::collect_comments(source, ext))
}

/// Parse a source-text fragment as a single string literal and return its content.
///
/// If `text` (after trimming whitespace) is a single string literal in the given
/// language, returns `Some(content)` with the inner content (quotes stripped).
/// Otherwise returns `None` — e.g. for f-strings, concatenated strings, or
/// non-string expressions.
///
/// The `ext` parameter selects the tree-sitter language (e.g. "py", "ts", "rs").
/// Defaults to "py" if not provided.
#[pyfunction]
#[pyo3(signature = (text, ext="py"))]
fn parse_string_literal(text: &str, ext: &str) -> PyResult<Option<String>> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }
    let literals = pattern::collect_string_literals(trimmed, ext);
    // Require exactly one literal that spans the whole trimmed input.
    if literals.len() != 1 {
        return Ok(None);
    }
    let (start, end, _sl, _sc, _el, _ec, content) = &literals[0];
    if *start as usize != 0 || *end as usize != trimmed.len() {
        return Ok(None);
    }
    Ok(Some(content.clone()))
}

/// Validate that `code` parses without syntax errors in the given language.
///
/// Uses tree-sitter to parse the code. Mirrors the permissive "is this a
/// reasonable replacement snippet?" contract: accepts any of (a) parse as
/// top-level, (b) parse wrapped in an expression context, (c) parse wrapped
/// in a statement context. Returns `true` if any form parses cleanly.
///
/// The `ext` parameter selects the tree-sitter language (e.g. "py", "ts", "rs").
/// Defaults to "py" if not provided.
#[pyfunction]
#[pyo3(signature = (code, ext="py"))]
fn validate_syntax(code: &str, ext: &str) -> PyResult<bool> {
    if parses_clean(code, ext) {
        return Ok(true);
    }
    if let Some(wrapped) = wrap_as_expression(code, ext) {
        if parses_clean(&wrapped, ext) {
            return Ok(true);
        }
    }
    if let Some(wrapped) = wrap_as_statement(code, ext) {
        if parses_clean(&wrapped, ext) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn parses_clean(code: &str, ext: &str) -> bool {
    match pattern::parse_by_extension(code, ext) {
        Some(tree) => !has_error_or_missing(tree.root_node()),
        None => false,
    }
}

fn wrap_as_expression(code: &str, ext: &str) -> Option<String> {
    match ext {
        "py" | "pyi" => Some(format!("_ = ({})", code)),
        "rs" => Some(format!("fn __v() {{ let _ = ({}); }}", code)),
        "ts" | "tsx" | "js" | "jsx" => Some(format!("const __v = ({});", code)),
        _ => None,
    }
}

fn wrap_as_statement(code: &str, ext: &str) -> Option<String> {
    match ext {
        "py" | "pyi" => {
            let indented = code.replace('\n', "\n    ");
            Some(format!("def __v():\n    {}", indented))
        }
        "rs" => Some(format!("fn __v() {{ {} }}", code)),
        "ts" | "tsx" | "js" | "jsx" => Some(format!("function __v() {{ {} }}", code)),
        _ => None,
    }
}

fn has_error_or_missing(node: tree_sitter::Node) -> bool {
    if node.is_error() || node.is_missing() {
        return true;
    }
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            if has_error_or_missing(cursor.node()) {
                return true;
            }
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
    false
}

/// Extract all call sites from source code using tree-sitter.
///
/// Returns a list of (callee, [arg1, arg2, ...], line, col, is_method) tuples.
/// Each argument is the exact text of a single argument node, correctly handling
/// nested expressions like tuples, dicts, and function calls.
///
/// Parameters:
/// - source: the source code text
/// - lang_name: tree-sitter language name ("python", "typescript", "rust")
/// - call_node_type: the tree-sitter node type for calls (e.g. "call", "call_expression")
/// - args_field_name: the field name for the arguments node (e.g. "arguments")
#[pyfunction]
fn extract_call_sites(
    source: &str,
    lang_name: &str,
    call_node_type: &str,
    args_field_name: &str,
) -> PyResult<Vec<(String, Vec<String>, usize, usize, bool)>> {
    let sites = pattern::extract_call_sites(source, lang_name, call_node_type, args_field_name);
    Ok(sites
        .into_iter()
        .map(|s| (s.callee, s.args, s.line, s.col, s.is_method))
        .collect())
}

/// Python module definition.
#[pymodule(gil_used = false)]
fn emend_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Match>()?;
    m.add_function(wrap_pyfunction!(collect_python_files, m)?)?;
    m.add_function(wrap_pyfunction!(collect_files, m)?)?;
    m.add_function(wrap_pyfunction!(skip_dirs, m)?)?;
    m.add_function(wrap_pyfunction!(filter_files_by_content, m)?)?;
    m.add_function(wrap_pyfunction!(find_name_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(find_calls_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(find_method_calls_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(read_and_filter_files, m)?)?;
    m.add_function(wrap_pyfunction!(extract_imports, m)?)?;
    m.add_function(wrap_pyfunction!(collect_callees, m)?)?;
    m.add_function(wrap_pyfunction!(files_importing_module, m)?)?;
    m.add_function(wrap_pyfunction!(symbols::collect_symbols_batch, m)?)?;
    m.add_function(wrap_pyfunction!(symbols::collect_symbols_from_str, m)?)?;
    m.add_function(wrap_pyfunction!(symbols::get_symbol_component_range, m)?)?;
    m.add_function(wrap_pyfunction!(symbols::get_symbol_component_list_items, m)?)?;
    m.add_function(wrap_pyfunction!(symbols::get_statement_ranges, m)?)?;
    m.add_function(wrap_pyfunction!(collect_identifier_positions, m)?)?;
    m.add_function(wrap_pyfunction!(collect_string_literals, m)?)?;
    m.add_function(wrap_pyfunction!(collect_comments, m)?)?;
    m.add_function(wrap_pyfunction!(parse_string_literal, m)?)?;
    m.add_function(wrap_pyfunction!(validate_syntax, m)?)?;
    m.add_function(wrap_pyfunction!(extract_call_sites, m)?)?;
    m.add_function(wrap_pyfunction!(matcher::find_pattern_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(matcher::find_multi_patterns_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(matcher::compile_pattern_treesitter, m)?)?;
    m.add_class::<scope_py::PyScopeResolver>()?;
    m.add_class::<transform_py::PyFileTransform>()?;
    m.add_class::<cozo_db::PyCozoDb>()?;
    m.add_class::<cfg_py::PyCfg>()?;
    m.add_function(wrap_pyfunction!(cfg_py::build_cfgs, m)?)?;
    m.add_class::<tree_py::PyTree>()?;
    m.add_class::<tree_py::PyNode>()?;
    m.add_function(wrap_pyfunction!(tree_py::parse_source, m)?)?;
    m.add_function(wrap_pyfunction!(tree_py::parse_file, m)?)?;
    Ok(())
}
