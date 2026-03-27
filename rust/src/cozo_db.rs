//! CozoDB Datalog engine wrapper for the fact graph.
//!
//! Exposes a thin PyO3 API around CozoDB's embedded SQLite-backed
//! Datalog database. Used by `emend.fact_graph.FactGraph` in Python.

use cozo::*;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use serde_json::Value as JsonValue;
use std::collections::BTreeMap;

/// A CozoDB database instance exposed to Python.
///
/// Wraps `cozo::DbInstance` with the SQLite backend (or in-memory).
#[pyclass]
pub struct PyCozoDb {
    db: DbInstance,
}

fn json_value_to_py(py: Python<'_>, val: &JsonValue) -> PyObject {
    match val {
        JsonValue::Null => py.None(),
        JsonValue::Bool(b) => b.into_pyobject(py).unwrap().to_owned().into_any().unbind(),
        JsonValue::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.into_pyobject(py).unwrap().into_any().unbind()
            } else if let Some(f) = n.as_f64() {
                f.into_pyobject(py).unwrap().into_any().unbind()
            } else {
                py.None()
            }
        }
        JsonValue::String(s) => s.into_pyobject(py).unwrap().into_any().unbind(),
        JsonValue::Array(arr) => {
            let items: Vec<PyObject> = arr.iter().map(|v| json_value_to_py(py, v)).collect();
            PyList::new(py, &items).unwrap().into_any().unbind()
        }
        JsonValue::Object(obj) => {
            let dict = PyDict::new(py);
            for (k, v) in obj {
                dict.set_item(k, json_value_to_py(py, v)).unwrap();
            }
            dict.into_any().unbind()
        }
    }
}

fn datavalue_to_json(val: &DataValue) -> JsonValue {
    match val {
        DataValue::Null => JsonValue::Null,
        DataValue::Bool(b) => JsonValue::Bool(*b),
        DataValue::Num(n) => match n {
            Num::Int(i) => JsonValue::Number((*i).into()),
            Num::Float(f) => {
                if let Some(n) = serde_json::Number::from_f64(*f) {
                    JsonValue::Number(n)
                } else {
                    JsonValue::Null
                }
            }
        },
        DataValue::Str(s) => JsonValue::String(s.to_string()),
        DataValue::List(arr) => {
            JsonValue::Array(arr.iter().map(datavalue_to_json).collect())
        }
        _ => JsonValue::String(format!("{:?}", val)),
    }
}

fn py_to_datavalue(obj: &Bound<'_, pyo3::PyAny>) -> PyResult<DataValue> {
    if obj.is_none() {
        Ok(DataValue::Null)
    } else if let Ok(b) = obj.extract::<bool>() {
        Ok(DataValue::Bool(b))
    } else if let Ok(i) = obj.extract::<i64>() {
        Ok(DataValue::from(i))
    } else if let Ok(f) = obj.extract::<f64>() {
        Ok(DataValue::from(f))
    } else if let Ok(s) = obj.extract::<String>() {
        Ok(DataValue::Str(s.into()))
    } else if let Ok(list) = obj.downcast::<PyList>() {
        let items: PyResult<Vec<DataValue>> = list.iter().map(|item| py_to_datavalue(&item)).collect();
        Ok(DataValue::List(items?))
    } else {
        Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "Cannot convert {} to CozoDB DataValue",
            obj.get_type().name()?
        )))
    }
}

#[pymethods]
impl PyCozoDb {
    /// Create a new CozoDB instance.
    ///
    /// - `engine`: "mem" for in-memory, "sqlite" for SQLite-backed
    /// - `path`: database file path (ignored for "mem")
    #[new]
    #[pyo3(signature = (engine="mem", path=""))]
    fn new(engine: &str, path: &str) -> PyResult<Self> {
        let db = DbInstance::new(engine, path, Default::default()).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to create CozoDB: {}", e))
        })?;
        Ok(PyCozoDb { db })
    }

    /// Run a CozoScript query with optional parameters.
    ///
    /// Returns a dict with "headers" (list[str]) and "rows" (list[list]).
    #[pyo3(signature = (query, params=None))]
    fn run(&self, py: Python<'_>, query: &str, params: Option<&Bound<'_, PyDict>>) -> PyResult<PyObject> {
        let mut param_map = BTreeMap::new();
        if let Some(p) = params {
            for (key, val) in p.iter() {
                let k: String = key.extract()?;
                let v = py_to_datavalue(&val)?;
                param_map.insert(k, v);
            }
        }

        let result = self.db.run_script(query, param_map, ScriptMutability::Mutable)
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("CozoDB query error: {}", e))
            })?;

        let dict = PyDict::new(py);

        // Headers
        let headers: Vec<String> = result.headers.iter().map(|h| h.to_string()).collect();
        dict.set_item("headers", headers)?;

        // Rows
        let rows: Vec<PyObject> = result.rows.iter().map(|row| {
            let py_row: Vec<PyObject> = row.iter().map(|val| {
                let jv = datavalue_to_json(val);
                json_value_to_py(py, &jv)
            }).collect();
            PyList::new(py, &py_row).unwrap().into_any().unbind()
        }).collect();
        dict.set_item("rows", rows)?;

        dict.set_item("next", py.None())?;

        Ok(dict.into_any().unbind())
    }

    /// Close the database (no-op for in-memory).
    fn close(&self) -> PyResult<()> {
        Ok(())
    }
}
