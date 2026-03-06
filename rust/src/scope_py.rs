//! PyO3 bindings for the scope resolver.

use pyo3::prelude::*;
use std::path::PathBuf;

use crate::pattern::parse_python;
use crate::scope::{LanguageConfig, ScopeResolver};

/// Python-visible scope resolver.
#[pyclass]
pub struct PyScopeResolver {
    inner: ScopeResolver,
}

#[pymethods]
impl PyScopeResolver {
    #[new]
    #[pyo3(signature = (project_root, extension=None))]
    fn new(project_root: &str, extension: Option<&str>) -> PyResult<Self> {
        let root = PathBuf::from(project_root);
        let config = if let Some(ext) = extension {
            LanguageConfig::load_for_extension(ext, &root).map_err(|e| {
                PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!("Failed to load config: {}", e))
            })?
        } else {
            LanguageConfig::python_default()
        };
        Ok(Self {
            inner: ScopeResolver::new(config, root),
        })
    }

    /// Index a single file.  Re-indexes only if content hash changed.
    fn index_file(&mut self, path: &str, source: &str) -> PyResult<()> {
        let path_buf = PathBuf::from(path);
        let ext = path_buf.extension().and_then(|e| e.to_str()).unwrap_or("");
        let tree = crate::pattern::parse_by_extension(source, ext).ok_or_else(|| {
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("Failed to parse file")
        })?;
        self.inner.index_file(&path_buf, source, &tree);
        Ok(())
    }

    /// Index multiple files sequentially.
    fn index_files(&mut self, files_and_sources: Vec<(String, String)>) -> PyResult<()> {
        for (path, source) in &files_and_sources {
            let path_buf = PathBuf::from(path);
            let ext = path_buf.extension().and_then(|e| e.to_str()).unwrap_or("");
            if let Some(tree) = crate::pattern::parse_by_extension(source, ext) {
                self.inner.index_file(&path_buf, source, &tree);
            }
        }
        Ok(())
    }

    fn definitions_in_file(&self, path: &str) -> Vec<(String, usize, usize)> {
        let path = PathBuf::from(path);
        self.inner
            .file_scopes
            .get(&path)
            .map(|fs| {
                fs.definitions
                    .iter()
                    .map(|(qn, loc)| (qn.name.clone(), loc.line, loc.column))
                    .collect()
            })
            .unwrap_or_default()
    }

    fn all_qualified_names(&self) -> Vec<String> {
        self.inner.qn_index.keys().cloned().collect()
    }

    fn lookup_qn(&self, qn: &str) -> Vec<(String, usize, usize)> {
        self.inner
            .qn_index
            .get(qn)
            .map(|locs| {
                locs.iter()
                    .map(|loc| (loc.file.to_string_lossy().into_owned(), loc.line, loc.column))
                    .collect()
            })
            .unwrap_or_default()
    }

    fn imports_in_file(&self, path: &str) -> Vec<(String, String, Option<String>, bool)> {
        let path = PathBuf::from(path);
        self.inner
            .file_scopes
            .get(&path)
            .map(|fs| {
                fs.imports
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
            })
            .unwrap_or_default()
    }

    /// Returns (scope_kind, [(name, binding_kind, line, column)]).
    fn scopes_in_file(&self, path: &str) -> Vec<(&'static str, Vec<(String, &'static str, usize, usize)>)> {
        let path = PathBuf::from(path);
        self.inner
            .file_scopes
            .get(&path)
            .map(|fs| {
                fs.scopes
                    .iter()
                    .map(|scope| {
                        let bindings: Vec<_> = scope
                            .bindings
                            .values()
                            .map(|b| (b.name.clone(), b.kind.as_str(), b.line, b.column))
                            .collect();
                        (scope.kind.as_str(), bindings)
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Returns all references in a file as (target_qn, line, col, start_byte, end_byte, kind).
    fn references_in_file(&self, path: &str) -> Vec<(String, usize, usize, usize, usize, &'static str)> {
        let path = PathBuf::from(path);
        self.inner
            .file_scopes
            .get(&path)
            .map(|fs| {
                fs.references
                    .iter()
                    .map(|r| (r.qn.name.clone(), r.line, r.column, r.byte_offset, r.end_byte, r.kind.as_str()))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Returns all qualified name strings mentioned in a file (for pre-filter index).
    fn all_qnames_in_file(&self, path: &str) -> Vec<String> {
        let path = PathBuf::from(path);
        self.inner
            .file_scopes
            .get(&path)
            .map(|fs| fs.all_qnames.clone())
            .unwrap_or_default()
    }

    fn file_count(&self) -> usize {
        self.inner.file_scopes.len()
    }

    /// Returns all symbols defined in a file as a list of dicts.
    fn get_symbols(&self, py: Python, path: &str) -> PyResult<Vec<PyObject>> {
        let path = std::path::PathBuf::from(path);
        let symbols = self.inner.get_symbols(&path);
        let mut result = Vec::with_capacity(symbols.len());
        for sym in symbols {
            result.push(crate::symbols::symbol_to_pydict(py, &sym)?);
        }
        Ok(result)
    }
}
