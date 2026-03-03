//! PyO3 bindings for the scope resolver.
//!
//! Exposes the scope resolver to Python for use by emend's CLI and
//! transform engine.

use pyo3::prelude::*;
use std::path::PathBuf;

use crate::scope::{LanguageConfig, ScopeResolver};

/// Python-visible scope resolver.
///
/// Maintains a persistent index of scopes, bindings, and qualified names.
#[pyclass]
pub struct PyScopeResolver {
    inner: ScopeResolver,
}

#[pymethods]
impl PyScopeResolver {
    /// Create a new scope resolver for a Python project.
    #[new]
    fn new(project_root: &str) -> Self {
        let config = LanguageConfig::python_default();
        Self {
            inner: ScopeResolver::new(config, PathBuf::from(project_root)),
        }
    }

    /// Index a single file.  Re-indexes only if content hash changed.
    fn index_file(&mut self, path: &str, source: &str) -> PyResult<()> {
        let mut parser = tree_sitter::Parser::new();
        let language = tree_sitter_python::LANGUAGE;
        parser
            .set_language(&language.into())
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!("{}", e)))?;
        let tree = parser
            .parse(source, None)
            .ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Failed to parse file")
            })?;
        self.inner
            .index_file(&PathBuf::from(path), source, &tree);
        Ok(())
    }

    /// Index multiple files in parallel.
    fn index_files(&mut self, files_and_sources: Vec<(String, String)>) -> PyResult<()> {
        for (path, source) in &files_and_sources {
            let mut parser = tree_sitter::Parser::new();
            let language = tree_sitter_python::LANGUAGE;
            parser
                .set_language(&language.into())
                .map_err(|e| {
                    PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!("{}", e))
                })?;
            if let Some(tree) = parser.parse(source, None) {
                self.inner
                    .index_file(&PathBuf::from(path), source, &tree);
            }
        }
        Ok(())
    }

    /// Get all qualified names defined in a file.
    fn definitions_in_file(&self, path: &str) -> Vec<(String, usize, usize)> {
        let path = PathBuf::from(path);
        if let Some(file_scope) = self.inner.file_scopes.get(&path) {
            file_scope
                .definitions
                .iter()
                .map(|(qn, loc)| (qn.name.clone(), loc.line, loc.column))
                .collect()
        } else {
            Vec::new()
        }
    }

    /// Get all qualified names in the index.
    fn all_qualified_names(&self) -> Vec<String> {
        self.inner.qn_index.keys().cloned().collect()
    }

    /// Look up definition sites for a qualified name.
    fn lookup_qn(&self, qn: &str) -> Vec<(String, usize, usize)> {
        self.inner
            .qn_index
            .get(qn)
            .map(|locs| {
                locs.iter()
                    .map(|loc| {
                        (
                            loc.file.to_string_lossy().into_owned(),
                            loc.line,
                            loc.column,
                        )
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Get imports for a file.
    fn imports_in_file(&self, path: &str) -> Vec<(String, String, Option<String>, bool)> {
        let path = PathBuf::from(path);
        if let Some(file_scope) = self.inner.file_scopes.get(&path) {
            file_scope
                .imports
                .values()
                .map(|imp| {
                    (
                        imp.local_name.clone(),
                        imp.module_path.clone(),
                        imp.imported_name.clone(),
                        imp.is_star,
                    )
                })
                .collect()
        } else {
            Vec::new()
        }
    }

    /// Get bindings in a specific scope of a file.
    /// Returns (scope_kind, [(name, binding_kind, line, column)]).
    fn scopes_in_file(&self, path: &str) -> Vec<(String, Vec<(String, String, usize, usize)>)> {
        let path = PathBuf::from(path);
        if let Some(file_scope) = self.inner.file_scopes.get(&path) {
            file_scope
                .scopes
                .iter()
                .map(|scope| {
                    let kind = format!("{:?}", scope.kind);
                    let bindings: Vec<_> = scope
                        .bindings
                        .values()
                        .map(|b| {
                            (
                                b.name.clone(),
                                format!("{:?}", b.kind),
                                b.line,
                                b.column,
                            )
                        })
                        .collect();
                    (kind, bindings)
                })
                .collect()
        } else {
            Vec::new()
        }
    }

    /// Get the number of indexed files.
    fn file_count(&self) -> usize {
        self.inner.file_scopes.len()
    }
}
