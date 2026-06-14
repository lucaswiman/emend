//! PyO3 bindings for the scope resolver.

use pyo3::prelude::*;
use std::path::PathBuf;

use crate::scope::{LanguageConfig, ScopeResolver, StructuredImport};

/// Convert a `StructuredImport` to a Python dict.
fn structured_import_to_pydict(py: Python, si: &StructuredImport) -> PyResult<PyObject> {
    let dict = pyo3::types::PyDict::new(py);
    dict.set_item("module", &si.module)?;
    dict.set_item("level", si.level)?;
    let names: Vec<(String, Option<String>)> = si
        .names
        .iter()
        .map(|n| (n.name.clone(), n.alias.clone()))
        .collect();
    dict.set_item("names", names)?;
    dict.set_item("start_byte", si.start_byte)?;
    dict.set_item("end_byte", si.end_byte)?;
    dict.set_item("start_line", si.start_line)?;
    dict.set_item("end_line", si.end_line)?;
    dict.set_item("is_plain", si.is_plain)?;
    Ok(dict.into())
}

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
            // Use the same fallback behaviour as find_pattern_in_files: silently
            // fall back to python_default() when the project-local config fails
            // to load (e.g. malformed TOML), rather than propagating a
            // RuntimeError that would silently empty all goto_definition results.
            LanguageConfig::load_for_extension(ext, &root)
                .unwrap_or_else(|_| LanguageConfig::python_default())
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

    /// Returns (name, line, column) for each definition.  ``line`` and
    /// ``column`` are **0-indexed** (matching ``scopes_in_file``).
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

    /// Returns (scope_kind, start_line, end_line, [(name, binding_kind, line, column)]).
    fn scopes_in_file(&self, path: &str) -> Vec<(&'static str, usize, usize, Vec<(String, &'static str, usize, usize)>)> {
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
                        (scope.kind.as_str(), scope.start_line, scope.end_line, bindings)
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Returns all references in a file as (target_qn, line, col, start_byte, end_byte, kind, in_annotation).
    fn references_in_file(&self, path: &str) -> Vec<(String, usize, usize, usize, usize, &'static str, bool)> {
        let path = PathBuf::from(path);
        self.inner
            .file_scopes
            .get(&path)
            .map(|fs| {
                fs.references
                    .iter()
                    .map(|r| (r.qn.name.clone(), r.line, r.column, r.byte_offset, r.end_byte, r.kind.as_str(), r.in_annotation))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Returns structured import statements in a file.
    ///
    /// Each entry is a dict with keys: module, level, names, start_byte,
    /// end_byte, start_line, end_line, is_plain.  ``start_line``/``end_line``
    /// are **0-indexed** (matching ``scopes_in_file``).  ``names`` is a list of
    /// (name, alias) tuples where alias may be None.  ``is_plain`` is true
    /// for plain ``import X`` statements, false for ``from X import Y``.
    fn structured_imports_in_file(&self, py: Python, path: &str) -> PyResult<Vec<PyObject>> {
        let path_buf = PathBuf::from(path);
        let fs = match self.inner.file_scopes.get(&path_buf) {
            Some(fs) => fs,
            None => return Ok(Vec::new()),
        };
        // Re-parse the file to get the tree (FileScope doesn't store it).
        let ext = path_buf.extension().and_then(|e| e.to_str()).unwrap_or("");
        let source = std::fs::read_to_string(&path_buf).unwrap_or_default();
        let tree = match crate::pattern::parse_by_extension(&source, ext) {
            Some(t) => t,
            None => return Ok(Vec::new()),
        };
        let _ = fs; // we only needed to verify the file was indexed

        let structured = self.inner.collect_structured_imports(&tree, &source);
        structured.iter().map(|si| structured_import_to_pydict(py, si)).collect()
    }

    /// Returns all identifier names with their annotation context for a file.
    ///
    /// Each entry is ``(name, in_annotation)``.  Includes every identifier
    /// node that is not the attribute-field of an attribute access and not
    /// a language keyword.  Used to replace ``ast.walk()`` for collecting
    /// referenced names and classifying them as runtime vs annotation.
    fn all_identifiers_in_file(&self, path: &str) -> Vec<(String, bool)> {
        let path_buf = PathBuf::from(path);
        let _fs = match self.inner.file_scopes.get(&path_buf) {
            Some(fs) => fs,
            None => return Vec::new(),
        };
        let ext = path_buf.extension().and_then(|e| e.to_str()).unwrap_or("");
        let source = std::fs::read_to_string(&path_buf).unwrap_or_default();
        let tree = match crate::pattern::parse_by_extension(&source, ext) {
            Some(t) => t,
            None => return Vec::new(),
        };
        self.inner.collect_identifiers_with_annotation(&tree, &source)
    }

    /// Returns all identifier names with annotation context from a source string.
    ///
    /// Like ``all_identifiers_in_file`` but works on a source string directly
    /// without requiring the file to be indexed.
    #[pyo3(signature = (source, ext=None))]
    fn collect_identifiers_from_source(
        &self,
        source: &str,
        ext: Option<&str>,
    ) -> Vec<(String, bool)> {
        let ext = ext.unwrap_or("py");
        let tree = match crate::pattern::parse_by_extension(source, ext) {
            Some(t) => t,
            None => return Vec::new(),
        };
        self.inner
            .collect_identifiers_with_annotation(&tree, source)
    }

    /// Returns structured imports from a source string.
    ///
    /// Like ``structured_imports_in_file`` but works on a source string
    /// directly without requiring the file to be indexed.
    #[pyo3(signature = (source, ext=None))]
    fn collect_structured_imports_from_source(
        &self,
        py: Python,
        source: &str,
        ext: Option<&str>,
    ) -> PyResult<Vec<PyObject>> {
        let ext = ext.unwrap_or("py");
        let tree = match crate::pattern::parse_by_extension(source, ext) {
            Some(t) => t,
            None => return Ok(Vec::new()),
        };
        let structured = self.inner.collect_structured_imports(&tree, source);
        structured.iter().map(|si| structured_import_to_pydict(py, si)).collect()
    }

    /// Collect Rust `use` and `mod` declarations from a source string.
    ///
    /// This is a language-specific parser for Rust that handles the recursive
    /// use-tree grammar (``scoped_use_list``, ``use_wildcard``, ``use_as_clause``…).
    /// Returns a list of ``(local_name, module_path, imported_name, is_star, line)``
    /// tuples, where ``line`` is 1-based.
    ///
    /// Use this instead of ``imports_in_file`` or
    /// ``collect_structured_imports_from_source`` for Rust source files.
    #[pyo3(signature = (source, ext=None))]
    fn collect_rust_imports_from_source(
        &self,
        source: &str,
        ext: Option<&str>,
    ) -> Vec<(String, String, Option<String>, bool, usize)> {
        let ext = ext.unwrap_or("rs");
        let tree = match crate::pattern::parse_by_extension(source, ext) {
            Some(t) => t,
            None => return Vec::new(),
        };
        self.inner
            .collect_rust_imports(&tree, source)
            .into_iter()
            .map(|(b, line)| (b.local_name, b.module_path, b.imported_name, b.is_star, line))
            .collect()
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
