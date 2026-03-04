use pyo3::prelude::*;
use crate::transform::FileTransform;

#[pyclass]
pub struct PyFileTransform {
    inner: FileTransform,
}

#[pymethods]
impl PyFileTransform {
    #[new]
    pub fn new(source: String) -> Self {
        Self {
            inner: FileTransform::new(source),
        }
    }

    pub fn replace_range(&mut self, start: usize, end: usize, replacement: String) {
        self.inner.replace_range(start, end, replacement);
    }

    pub fn insert_before(&mut self, pos: usize, text: String) {
        self.inner.insert_before(pos, text);
    }

    pub fn insert_after(&mut self, pos: usize, text: String) {
        self.inner.insert_after(pos, text);
    }

    pub fn remove_range(&mut self, start: usize, end: usize) {
        self.inner.remove_range(start, end);
    }

    pub fn apply(&self) -> PyResult<Option<String>> {
        Ok(self.inner.apply())
    }
}
