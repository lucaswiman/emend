//! emend_core: Fast Python pattern matching engine for emend.
//!
//! Provides parallel file scanning, name-hint pre-filtering, and
//! tree-sitter-based pattern matching exposed to Python via PyO3.

use pyo3::prelude::*;
use pyo3::types::PyList;
use rayon::prelude::*;
use std::path::{Path, PathBuf};

mod scanner;
mod pattern;

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

/// Python module definition.
#[pymodule]
fn emend_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Match>()?;
    m.add_function(wrap_pyfunction!(collect_python_files, m)?)?;
    m.add_function(wrap_pyfunction!(filter_files_by_content, m)?)?;
    m.add_function(wrap_pyfunction!(find_name_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(find_calls_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(find_method_calls_in_files, m)?)?;
    m.add_function(wrap_pyfunction!(extract_imports, m)?)?;
    Ok(())
}
