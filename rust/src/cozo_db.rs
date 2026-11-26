//! CozoDB Datalog engine wrapper for the fact graph.
//!
//! Exposes a thin PyO3 API around CozoDB's embedded SQLite-backed
//! Datalog database. Used by `emend.fact_graph.FactGraph` in Python.

use cozo::*;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::BTreeMap;

/// A CozoDB database instance exposed to Python.
///
/// Wraps `cozo::DbInstance` with the SQLite backend (or in-memory).
#[pyclass]
pub struct PyCozoDb {
    db: DbInstance,
}

/// Convert one stored value directly to its Python form.
fn datavalue_to_py(py: Python<'_>, value: &DataValue) -> PyResult<Py<PyAny>> {
    Ok(match value {
        DataValue::Null => py.None(),
        DataValue::Bool(flag) => flag.into_pyobject(py)?.to_owned().into_any().unbind(),
        DataValue::Num(Num::Int(number)) => number.into_pyobject(py)?.into_any().unbind(),
        DataValue::Num(Num::Float(number)) => {
            if number.is_finite() {
                number.into_pyobject(py)?.into_any().unbind()
            } else {
                py.None()
            }
        }
        DataValue::Str(text) => text.as_str().into_pyobject(py)?.into_any().unbind(),
        DataValue::Bytes(bytes) => PyBytes::new(py, bytes).into_any().unbind(),
        DataValue::List(items) => {
            let converted = items
                .iter()
                .map(|item| datavalue_to_py(py, item))
                .collect::<PyResult<Vec<_>>>()?;
            PyList::new(py, &converted)?.into_any().unbind()
        }
        other => format!("{:?}", other).into_pyobject(py)?.into_any().unbind(),
    })
}

fn py_to_datavalue(obj: &Bound<'_, pyo3::PyAny>) -> PyResult<DataValue> {
    // Fact batches contain builtin scalars and lists. Dispatch those without
    // failed coercions (including bool extraction's NumPy compatibility check).
    // Keep the fallback for subclasses, numeric protocols and large integers.
    if obj.is_exact_instance_of::<PyString>() {
        return Ok(DataValue::Str(obj.extract::<String>()?.into()));
    }
    if obj.is_exact_instance_of::<PyInt>() {
        if let Ok(i) = obj.extract::<i64>() {
            return Ok(DataValue::from(i));
        }
    }
    if obj.is_exact_instance_of::<PyFloat>() {
        return Ok(DataValue::from(obj.extract::<f64>()?));
    }
    if let Ok(bytes) = obj.cast_exact::<PyBytes>() {
        return Ok(DataValue::Bytes(bytes.as_bytes().to_vec()));
    }
    if obj.is_exact_instance_of::<PyList>() {
        return Ok(DataValue::List(
            obj.cast::<PyList>()?.iter()
                .map(|item| py_to_datavalue(&item)).collect::<PyResult<_>>()?,
        ));
    }
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
    } else if let Ok(list) = obj.cast::<PyList>() {
        let items: PyResult<Vec<DataValue>> =
            list.iter().map(|item| py_to_datavalue(&item)).collect();
        Ok(DataValue::List(items?))
    } else {
        Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "Cannot convert {} to CozoDB DataValue",
            obj.get_type().name()?
        )))
    }
}

fn py_params(params: Option<&Bound<'_, PyDict>>) -> PyResult<BTreeMap<String, DataValue>> {
    let mut result = BTreeMap::new();
    if let Some(params) = params {
        for (key, value) in params.iter() {
            result.insert(key.extract()?, py_to_datavalue(&value)?);
        }
    }
    Ok(result)
}

fn finish_transaction(transaction: &MultiTransaction, action: TransactionPayload) -> Result<(), String> {
    transaction.sender.send(action).map_err(|e| e.to_string())?;
    let outcome = transaction.receiver.recv().map_err(|e| e.to_string())?;
    // Cozo 0.7's commit()/abort() discard the outcome and return before the
    // worker drops its SQLite statements. Channel closure marks full teardown.
    let _ = transaction.receiver.recv();
    outcome.map(|_| ()).map_err(|e| e.to_string())
}

fn named_rows_to_py(py: Python<'_>, result: NamedRows) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    let headers: Vec<String> = result.headers.iter().map(|h| h.to_string()).collect();
    dict.set_item("headers", headers)?;
    let rows: Vec<Py<PyAny>> = result
        .rows
        .iter()
        .map(|row| {
            let py_row = row
                .iter()
                .map(|value| datavalue_to_py(py, value))
                .collect::<PyResult<Vec<_>>>()?;
            Ok(PyList::new(py, &py_row)?.into_any().unbind())
        })
        .collect::<PyResult<_>>()?;
    dict.set_item("rows", rows)?;
    dict.set_item("next", py.None())?;
    Ok(dict.into_any().unbind())
}


#[pymethods]
impl PyCozoDb {
    /// Create a new CozoDB instance.
    ///
    /// - `engine`: "mem" for in-memory, "sqlite" for SQLite-backed
    /// - `path`: database file path (ignored for "mem")
    #[new]
    #[pyo3(signature = (engine="mem", path=""))]
    fn new(py: Python<'_>, engine: &str, path: &str) -> PyResult<Self> {
        let db = py.detach(|| DbInstance::new(engine, path, Default::default())).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to create CozoDB: {}", e))
        })?;
        Ok(PyCozoDb { db })
    }

    /// Run a CozoScript query with optional parameters.
    ///
    /// Returns a dict with "headers" (list[str]) and "rows" (list[list]).
    #[pyo3(signature = (query, params=None, *, read_only=false))]
    fn run(
        &self,
        py: Python<'_>,
        query: &str,
        params: Option<&Bound<'_, PyDict>>,
        read_only: bool,
    ) -> PyResult<Py<PyAny>> {
        let params = py_params(params)?;
        // Detach for native work: attached threads also block stop-the-world
        // garbage collection on free-threaded Python, even without a GIL.
        let result = py.detach(|| self.db.run_script(query, params, if read_only {
                ScriptMutability::Immutable
            } else {
                ScriptMutability::Mutable
            }))
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("CozoDB query error: {}", e))
            })?;
        named_rows_to_py(py, result)
    }

    /// Run several scripts in one transaction, converting each parameter
    /// batch only when its script is ready to execute.
    fn run_transaction(&self, py: Python<'_>, operations: &Bound<'_, PyList>) -> PyResult<()> {
        let transaction = py.detach(|| self.db.multi_transaction(true));
        for operation in operations.iter() {
            let converted = (|| -> PyResult<_> {
                let operation = operation.cast_into::<PyTuple>()?;
                let query: String = operation.get_item(0)?.extract()?;
                let params = operation.get_item(1)?.cast_into::<PyDict>()?;
                Ok((query, py_params(Some(&params))?))
            })();
            let (query, params) = match converted {
                Ok(converted) => converted,
                Err(error) => {
                    let _ = py.detach(|| finish_transaction(&transaction, TransactionPayload::Abort));
                    return Err(error);
                }
            };
            if let Err(error) = py.detach(|| transaction.run_script(&query, params)) {
                let _ = py.detach(|| finish_transaction(&transaction, TransactionPayload::Abort));
                return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "CozoDB query error: {}",
                    error
                )));
            }
        }
        py.detach(|| finish_transaction(&transaction, TransactionPayload::Commit)).map_err(|error| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "CozoDB transaction error: {}",
                error
            ))
        })
    }

    /// Put rows into existing relations in one transaction, without scripts.
    ///
    /// ``data`` maps a relation name to ``(column_names, rows)``; every column
    /// of the relation must be named.  A row replaces the stored row with the
    /// same key.  Evaluating a ``:put`` script costs several times more per
    /// row, which dominates building a large fact graph.
    fn import_relations(&self, py: Python<'_>, data: &Bound<'_, PyDict>) -> PyResult<()> {
        let mut relations = BTreeMap::new();
        for (name, value) in data.iter() {
            let (headers, rows): (Vec<String>, Bound<'_, PyList>) = value.extract()?;
            let mut converted = Vec::with_capacity(rows.len());
            for row in rows.iter() {
                let row = row.cast_into::<PyList>()?;
                converted.push(
                    row.iter()
                        .map(|cell| py_to_datavalue(&cell))
                        .collect::<PyResult<Vec<_>>>()?,
                );
            }
            relations.insert(name.extract::<String>()?, NamedRows::new(headers, converted));
        }
        py.detach(|| self.db.import_relations(relations)).map_err(|error| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("CozoDB import error: {}", error))
        })
    }

    /// Save a consistent database snapshot to a new SQLite file.
    fn backup(&self, py: Python<'_>, path: &str) -> PyResult<()> {
        py.detach(|| self.db.backup_db(path)).map_err(|error| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("CozoDB backup error: {}", error))
        })
    }

    /// Close the database (no-op for in-memory).
    fn close(&self) -> PyResult<()> {
        Ok(())
    }
}
