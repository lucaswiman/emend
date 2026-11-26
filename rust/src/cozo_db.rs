//! CozoDB Datalog engine wrapper for the fact graph.
//!
//! Exposes a thin PyO3 API around CozoDB's embedded SQLite-backed
//! Datalog database. Used by `emend.fact_graph.FactGraph` in Python.
//!
//! The fact graph stores file paths and qualified names as integer ids.  Its
//! `intern {text => id}` and `interned {id => text}` relations hold each text
//! once, in the same database as the facts.  A write names the cells that hold
//! such text and they are stored as ids; ids in a query result are replaced by
//! their text before the result reaches Python.

use cozo::*;
use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use rayon::iter::{IntoParallelRefIterator, ParallelIterator};
use rayon::slice::ParallelSliceMut;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Condvar, Mutex, RwLock, RwLockReadGuard, RwLockWriteGuard};

/// The first id.  Ids are dense from here, so each is larger than any line,
/// column, block or byte offset in the graph and takes ten bytes in a key.
const ID_BASE: i64 = 1 << 40;
/// A text known not to be stored.
const ABSENT: i64 = -1;
/// Looking a text or id up costs about as much as scanning this many
/// dictionary rows, so a handle that has looked enough up reads the rest of
/// the dictionary at once.
const LOOKUP_COST: usize = 4;
/// Lookups too few to be worth counting the dictionary for.
const FEW_LOOKUPS: usize = 5_000;
/// Results at least this long are sorted on several threads.
const LONG_RESULT: usize = 50_000;

const STORED_IDS: &str = "q[text] <- $texts ?[text, id] := q[text], *intern[text, id]";
const STORED_TEXTS: &str = "q[id] <- $ids ?[id, text] := q[id], *interned[id, text]";
const STORED_TEXT: &str = "?[text] := *interned[$id, text]";
const ALL_IDS: &str = "?[text, id] := *intern[text, id]";
const ALL_TEXTS: &str = "?[id, text] := *interned[id, text]";
/// The dictionary in text order, a page at a time: a result is held in memory
/// whole, and the whole dictionary at once is several times its own size.
const IDS_PAGE: usize = 20_000;
const FIRST_IDS: &str = "?[text, id] := *intern[text, id] :limit 20000";
const NEXT_IDS: &str = "?[text, id] := *intern[text, id], text > $after :limit 20000";
const PUT_INTERN: &str = "?[text, id] <- $entries :put intern {text => id}";
const PUT_INTERNED: &str = "?[text, id] <- $entries :put interned {id => text}";

/// What one handle has learned of its database's dictionary.  Stored pairs
/// never change and only this handle adds any, so nothing here goes stale.
#[derive(Default)]
struct Dictionary {
    /// The id of a text, or `ABSENT` for a text looked up and not found.
    ids: HashMap<String, i64>,
    texts: HashMap<i64, DataValue>,
    /// The first unused id, once read from the database.
    next: Option<i64>,
    /// `ids` holds every stored text, so a text missing from it is new.
    all_ids: bool,
    /// `texts` holds every stored id, so an integer missing from it is not one.
    all_texts: bool,
    /// Texts and ids looked up one by one so far.
    id_lookups: usize,
    text_lookups: usize,
    /// Writes that have added texts.
    additions: u64,
}

/// A CozoDB database instance exposed to Python.
///
/// Wraps `cozo::DbInstance` with the SQLite backend (or in-memory).
#[pyclass]
pub struct PyCozoDb {
    db: DbInstance,
    dictionary: RwLock<Dictionary>,
    /// Whether a write is under way; the next one waits its turn.
    writing: Mutex<bool>,
    written: Condvar,
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

/// The distinct texts of one batch's interned cells.  Such a cell holds its
/// text's position here until the write has an id for every text.
#[derive(Default)]
struct BatchTexts {
    positions: HashMap<String, usize>,
}

impl BatchTexts {
    fn cell(&mut self, cell: &Bound<'_, PyAny>) -> PyResult<DataValue> {
        let Ok(text) = cell.cast::<PyString>() else {
            return Err(PyTypeError::new_err(format!(
                "an interned cell must be str, not {}",
                cell.get_type().name()?
            )));
        };
        let text = text.to_str()?;
        let position = match self.positions.get(text) {
            Some(&position) => position,
            None => {
                let position = self.positions.len();
                self.positions.insert(text.to_owned(), position);
                position
            }
        };
        Ok(DataValue::from(position as i64))
    }

    fn into_texts(self) -> Vec<String> {
        let mut texts = vec![String::new(); self.positions.len()];
        for (text, position) in self.positions {
            texts[position] = text;
        }
        texts
    }
}

/// Convert rows, interning the cells at the `interned` positions of each.
fn py_rows(
    rows: &Bound<'_, PyList>,
    interned: &[usize],
    texts: &mut BatchTexts,
) -> PyResult<Vec<Vec<DataValue>>> {
    let mut converted = Vec::with_capacity(rows.len());
    for row in rows.iter() {
        let row = row.cast_into::<PyList>()?;
        if let Some(position) = interned.iter().find(|&&position| position >= row.len()) {
            return Err(PyTypeError::new_err(format!(
                "a row of {} cells has no interned cell {}",
                row.len(),
                position
            )));
        }
        let mut cells = Vec::with_capacity(row.len());
        for (index, cell) in row.iter().enumerate() {
            cells.push(if interned.contains(&index) {
                texts.cell(&cell)?
            } else {
                py_to_datavalue(&cell)?
            });
        }
        converted.push(cells);
    }
    Ok(converted)
}

fn place_ids(rows: &mut [Vec<DataValue>], interned: &[usize], ids: &[i64]) {
    for row in rows {
        for &index in interned {
            if let Some(position) = row[index].get_int() {
                row[index] = DataValue::from(ids[position as usize]);
            }
        }
    }
}

/// One script of a transaction: its parameters, with the row parameters whose
/// interned cells still await ids kept apart.
struct Operation {
    query: String,
    params: BTreeMap<String, DataValue>,
    interned: Vec<(String, Vec<usize>, Vec<Vec<DataValue>>)>,
    texts: Vec<String>,
}

fn py_operation(operation: Bound<'_, PyAny>) -> PyResult<Operation> {
    let operation = operation.cast_into::<PyTuple>()?;
    let query: String = operation.get_item(0)?.extract()?;
    let params = operation.get_item(1)?.cast_into::<PyDict>()?;
    let positions = if operation.len() > 2 {
        Some(operation.get_item(2)?.cast_into::<PyDict>()?)
    } else {
        None
    };
    let mut texts = BatchTexts::default();
    let mut converted = BTreeMap::new();
    let mut interned = Vec::new();
    for (key, value) in params.iter() {
        let name: String = key.extract()?;
        match positions.as_ref().map(|positions| positions.get_item(&name)).transpose()?.flatten() {
            Some(positions) => {
                let positions: Vec<usize> = positions.extract()?;
                let rows = py_rows(&value.cast_into::<PyList>()?, &positions, &mut texts)?;
                interned.push((name, positions, rows));
            }
            None => {
                converted.insert(name, py_to_datavalue(&value)?);
            }
        }
    }
    Ok(Operation { query, params: converted, interned, texts: texts.into_texts() })
}

fn rows_value(rows: Vec<Vec<DataValue>>) -> DataValue {
    DataValue::List(rows.into_iter().map(DataValue::List).collect())
}

fn one_param(name: &str, value: DataValue) -> BTreeMap<String, DataValue> {
    BTreeMap::from([(name.to_string(), value)])
}

/// Stored `(text, id)` pairs: those of the given texts, or all of them.
fn stored_ids<R>(read: &R, texts: Option<&[&str]>) -> Result<Vec<(String, i64)>, String>
where
    R: Fn(&str, BTreeMap<String, DataValue>) -> Result<NamedRows, String>,
{
    let rows = match texts {
        Some(texts) => {
            let wanted = texts.iter().map(|text| vec![DataValue::from(*text)]).collect();
            read(STORED_IDS, one_param("texts", rows_value(wanted)))?.rows
        }
        None => read(ALL_IDS, BTreeMap::new())?.rows,
    };
    Ok(rows
        .iter()
        .filter_map(|row| Some((row[0].get_str()?.to_string(), row[1].get_int()?)))
        .collect())
}

/// Stored `(id, text)` pairs: those of the given ids, or all of them.
fn stored_texts<R>(read: &R, ids: Option<&[i64]>) -> Result<Vec<(i64, DataValue)>, String>
where
    R: Fn(&str, BTreeMap<String, DataValue>) -> Result<NamedRows, String>,
{
    let rows = match ids {
        Some(ids) => {
            let wanted = ids.iter().map(|id| vec![DataValue::from(*id)]).collect();
            read(STORED_TEXTS, one_param("ids", rows_value(wanted)))?.rows
        }
        None => read(ALL_TEXTS, BTreeMap::new())?.rows,
    };
    Ok(rows
        .into_iter()
        .filter_map(|mut row| Some((row[0].get_int()?, row.pop()?)))
        .collect())
}

/// How many texts are stored.  Ids are dense, so probing finds the first
/// unused one without scanning the dictionary.
fn stored_count<R>(read: &R) -> Result<i64, String>
where
    R: Fn(&str, BTreeMap<String, DataValue>) -> Result<NamedRows, String>,
{
    let stored = |offset: i64| -> Result<bool, String> {
        let id = one_param("id", DataValue::from(ID_BASE + offset));
        Ok(!read(STORED_TEXT, id)?.rows.is_empty())
    };
    if !stored(0)? {
        return Ok(0);
    }
    let (mut last, mut step) = (0, 1);
    while stored(last + step)? {
        last += step;
        step *= 2;
    }
    let mut unused = last + step;
    while unused - last > 1 {
        let middle = last + (unused - last) / 2;
        if stored(middle)? {
            last = middle;
        } else {
            unused = middle;
        }
    }
    Ok(unused)
}

/// Replace the ids in a value by their text, noting those with no known
/// text.  Returns whether anything was replaced.
fn place_texts(
    value: &mut DataValue,
    texts: &HashMap<i64, DataValue>,
    unknown: &mut HashSet<i64>,
) -> bool {
    match value {
        DataValue::Num(Num::Int(number)) if *number >= ID_BASE => match texts.get(number) {
            Some(text) => {
                *value = text.clone();
                true
            }
            None => {
                unknown.insert(*number);
                false
            }
        },
        DataValue::List(items) => items
            .iter_mut()
            .fold(false, |placed, item| place_texts(item, texts, unknown) | placed),
        _ => false,
    }
}

/// One write's turn at the handle, held from before it touches the database
/// until it has committed or failed.  The ids it hands out become part of the
/// handle's dictionary only when the write commits, so a failed write leaves
/// no trace and the next one reuses its ids.
struct Allocation<'a> {
    owner: &'a PyCozoDb,
    created: HashMap<String, i64>,
    next: Option<i64>,
}

impl<'a> Allocation<'a> {
    /// Wait for any other write.  Call only while detached from the
    /// interpreter.
    fn begin(owner: &'a PyCozoDb) -> Self {
        let mut writing = owner.writing.lock().unwrap_or_else(|error| error.into_inner());
        while *writing {
            writing = owner.written.wait(writing).unwrap_or_else(|error| error.into_inner());
        }
        *writing = true;
        Allocation { owner, created: HashMap::new(), next: None }
    }

    /// The id of each text, and `[text, id]` rows for those new to the
    /// dictionary, which the caller must store in the same write.
    fn resolve<R>(
        &mut self,
        texts: &[String],
        read: &R,
    ) -> Result<(Vec<i64>, Vec<Vec<DataValue>>), String>
    where
        R: Fn(&str, BTreeMap<String, DataValue>) -> Result<NamedRows, String>,
    {
        let known = |dictionary: &Dictionary, text: &String| {
            self.created.get(text).or_else(|| dictionary.ids.get(text)).copied()
        };
        let unknown: Vec<&str> = {
            let dictionary = self.owner.dictionary();
            texts
                .iter()
                .filter(|text| !dictionary.all_ids && known(&dictionary, text).is_none())
                .map(String::as_str)
                .collect()
        };
        if !unknown.is_empty() {
            self.owner.learn_ids(read, &unknown, &self.created)?;
        }
        let mut ids = vec![ABSENT; texts.len()];
        // Positions of the texts to create, in the order the write names them.
        let mut missing = Vec::new();
        {
            let dictionary = self.owner.dictionary();
            for (position, text) in texts.iter().enumerate() {
                match known(&dictionary, text) {
                    Some(id) if id != ABSENT => ids[position] = id,
                    _ => missing.push(position),
                }
            }
        }
        let mut entries = Vec::new();
        if !missing.is_empty() {
            let mut next = match self.next {
                Some(next) => next,
                None => self.owner.next_id(read)?,
            };
            // The empty string is always the first text, so its id is fixed.
            let first = (next == ID_BASE).then_some("");
            let new = missing.iter().map(|&position| texts[position].as_str());
            for text in first.into_iter().chain(new) {
                if !self.created.contains_key(text) {
                    self.created.insert(text.to_string(), next);
                    entries.push(vec![DataValue::from(text), DataValue::from(next)]);
                    next += 1;
                }
            }
            for position in missing {
                ids[position] = self.created[&texts[position]];
            }
            self.next = Some(next);
        }
        Ok((ids, entries))
    }

    /// Record what a committed write stored.
    fn commit(mut self) {
        let mut dictionary = self.owner.dictionary_mut();
        if let Some(next) = self.next {
            dictionary.next = Some(next);
        }
        if !self.created.is_empty() {
            // The new ids' texts are read back when a result first holds them.
            dictionary.all_texts = false;
            dictionary.additions += 1;
        }
        dictionary.ids.extend(std::mem::take(&mut self.created));
    }
}

impl Drop for Allocation<'_> {
    fn drop(&mut self) {
        *self.owner.writing.lock().unwrap_or_else(|error| error.into_inner()) = false;
        self.owner.written.notify_one();
    }
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

impl PyCozoDb {
    fn dictionary(&self) -> RwLockReadGuard<'_, Dictionary> {
        self.dictionary.read().unwrap_or_else(|error| error.into_inner())
    }

    fn dictionary_mut(&self) -> RwLockWriteGuard<'_, Dictionary> {
        self.dictionary.write().unwrap_or_else(|error| error.into_inner())
    }

    fn read(&self, script: &str, params: BTreeMap<String, DataValue>) -> Result<NamedRows, String> {
        self.db
            .run_script(script, params, ScriptMutability::Immutable)
            .map_err(|error| error.to_string())
    }

    /// The first unused id, read through `read` the first time.
    fn next_id<R>(&self, read: &R) -> Result<i64, String>
    where
        R: Fn(&str, BTreeMap<String, DataValue>) -> Result<NamedRows, String>,
    {
        if let Some(next) = self.dictionary().next {
            return Ok(next);
        }
        let count = stored_count(read)?;
        let mut dictionary = self.dictionary_mut();
        dictionary.next = Some(ID_BASE + count);
        if count <= 1 {
            // Nothing but the empty string is stored: every other text is new.
            dictionary.all_ids = true;
            if count == 1 {
                dictionary.ids.insert(String::new(), ID_BASE);
            }
        }
        Ok(ID_BASE + count)
    }

    /// Whether a handle that will have made `lookups` dictionary lookups in
    /// all has paid as much for them as for reading the whole dictionary.
    fn reads_all<R>(&self, read: &R, lookups: usize) -> Result<bool, String>
    where
        R: Fn(&str, BTreeMap<String, DataValue>) -> Result<NamedRows, String>,
    {
        if lookups <= FEW_LOOKUPS {
            return Ok(false);
        }
        let stored = (self.next_id(read)? - ID_BASE) as usize;
        Ok(lookups * LOOKUP_COST >= stored)
    }

    /// Read the stored ids of `texts` into the dictionary, noting the texts
    /// that have none.  `uncommitted` holds what a write under way has put in
    /// the database: a read inside its transaction sees that too, and it is
    /// not the handle's to remember yet.
    fn learn_ids<R>(
        &self,
        read: &R,
        texts: &[&str],
        uncommitted: &HashMap<String, i64>,
    ) -> Result<(), String>
    where
        R: Fn(&str, BTreeMap<String, DataValue>) -> Result<NamedRows, String>,
    {
        let looked_up = self.dictionary().id_lookups;
        let everything = self.reads_all(read, looked_up + texts.len())?;
        let found = stored_ids(read, (!everything).then_some(texts))?;
        let mut dictionary = self.dictionary_mut();
        dictionary.ids.extend(found.into_iter().filter(|(text, _)| !uncommitted.contains_key(text)));
        dictionary.all_ids |= everything;
        dictionary.id_lookups += texts.len();
        for text in texts {
            // Only this handle's own writes can end a miss, and they record
            // what they store.
            if !dictionary.ids.contains_key(*text) {
                dictionary.ids.insert(text.to_string(), ABSENT);
            }
        }
        Ok(())
    }

    /// The stored id of each text, or `ABSENT`.
    fn lookup_ids(&self, texts: &[String]) -> Result<Vec<i64>, String> {
        let unknown: Vec<&str> = {
            let dictionary = self.dictionary();
            texts
                .iter()
                // The empty string's id is fixed, stored or not.
                .filter(|text| !text.is_empty())
                .filter(|text| !dictionary.all_ids && !dictionary.ids.contains_key(*text))
                .map(String::as_str)
                .collect()
        };
        if !unknown.is_empty() {
            let read = |script: &str, params| self.read(script, params);
            self.learn_ids(&read, &unknown, &HashMap::new())?;
        }
        let dictionary = self.dictionary();
        Ok(texts
            .iter()
            .map(|text| match dictionary.ids.get(text) {
                _ if text.is_empty() => ID_BASE,
                Some(&id) => id,
                None => ABSENT,
            })
            .collect())
    }

    /// The first `limit` stored texts, in text order, that contain any of
    /// `fragments`, with their ids.
    ///
    /// The query engine tests a substring one row at a time, and slowly: it
    /// spends longer on that than on reading the row.  The whole dictionary
    /// is read into this handle once instead and searched here.
    fn search_texts(
        &self,
        fragments: &[String],
        limit: usize,
        ignore_case: bool,
    ) -> Result<Vec<(String, i64)>, String> {
        if fragments.is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        if !self.dictionary().all_ids {
            let mut after: Option<String> = None;
            loop {
                let page = match &after {
                    None => self.read(FIRST_IDS, BTreeMap::new())?,
                    Some(text) => {
                        self.read(NEXT_IDS, one_param("after", DataValue::from(text.as_str())))?
                    }
                }
                .rows;
                let full = page.len() == IDS_PAGE;
                let mut dictionary = self.dictionary_mut();
                for row in &page {
                    if let (Some(text), Some(id)) = (row[0].get_str(), row[1].get_int()) {
                        dictionary.ids.insert(text.to_string(), id);
                        if after.as_deref().is_none_or(|last| last < text) {
                            after = Some(text.to_string());
                        }
                    }
                }
                if !full {
                    // A write commits its texts to this map itself, so nothing
                    // that finished meanwhile is missing from it.
                    dictionary.all_ids = true;
                    break;
                }
            }
        }
        let needles: Vec<String> = if ignore_case {
            fragments.iter().map(|fragment| fragment.to_lowercase()).collect()
        } else {
            fragments.to_vec()
        };
        let contains = |text: &str| needles.iter().any(|needle| text.contains(needle.as_str()));
        let dictionary = self.dictionary();
        let mut found: Vec<(&String, i64)> = dictionary
            .ids
            .par_iter()
            // A text looked up and not found is remembered here without an id.
            .filter(|(text, &id)| {
                id != ABSENT
                    && if ignore_case { contains(&text.to_lowercase()) } else { contains(text) }
            })
            .map(|(text, &id)| (text, id))
            .collect();
        if found.len() > limit {
            found.select_nth_unstable(limit);
            found.truncate(limit);
        }
        found.sort_unstable();
        Ok(found.into_iter().map(|(text, id)| (text.clone(), id)).collect())
    }

    /// Replace the stored ids in a result by their text.  Unless the script
    /// orders its rows itself, they are then put in the order they would have
    /// had if the relations held the text.
    fn decode(&self, result: &mut NamedRows, ordered: bool) {
        let mut unknown = HashSet::new();
        let mut placed = false;
        let (all_texts, looked_up, additions) = {
            let dictionary = self.dictionary();
            for value in result.rows.iter_mut().flatten() {
                placed |= place_texts(value, &dictionary.texts, &mut unknown);
            }
            (dictionary.all_texts, dictionary.text_lookups, dictionary.additions)
        };
        if !unknown.is_empty() && !all_texts {
            let read = |script: &str, params| self.read(script, params);
            let wanted: Vec<i64> = unknown.drain().collect();
            // A database with no dictionary has nothing to decode.
            let found = self.reads_all(&read, looked_up + wanted.len()).and_then(|everything| {
                Ok((stored_texts(&read, (!everything).then_some(&wanted))?, everything))
            });
            if let Ok((found, everything)) = found {
                {
                    let mut dictionary = self.dictionary_mut();
                    dictionary.texts.extend(found);
                    // A write that finished meanwhile added ids not read here.
                    dictionary.all_texts |= everything && dictionary.additions == additions;
                    dictionary.text_lookups += wanted.len();
                }
                let dictionary = self.dictionary();
                for value in result.rows.iter_mut().flatten() {
                    placed |= place_texts(value, &dictionary.texts, &mut unknown);
                }
            }
        }
        if placed && !ordered {
            if result.rows.len() >= LONG_RESULT {
                result.rows.par_sort_unstable();
            } else {
                result.rows.sort_unstable();
            }
        }
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
    fn new(py: Python<'_>, engine: &str, path: &str) -> PyResult<Self> {
        let db = py.detach(|| DbInstance::new(engine, path, Default::default())).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to create CozoDB: {}", e))
        })?;
        Ok(PyCozoDb {
            db,
            dictionary: RwLock::default(),
            writing: Mutex::default(),
            written: Condvar::new(),
        })
    }

    /// The smallest interned id.
    #[classattr]
    #[pyo3(name = "ID_BASE")]
    fn id_base() -> i64 {
        ID_BASE
    }

    /// Run a CozoScript query with optional parameters.
    ///
    /// Returns a dict with "headers" (list[str]) and "rows" (list[list]).
    /// Interned ids in the rows are returned as their text, and the rows in
    /// the order of that text unless the script has its own `:order`; pass
    /// `decode=False` for the stored integers.
    #[pyo3(signature = (query, params=None, *, read_only=false, decode=true))]
    fn run(
        &self,
        py: Python<'_>,
        query: &str,
        params: Option<&Bound<'_, PyDict>>,
        read_only: bool,
        decode: bool,
    ) -> PyResult<Py<PyAny>> {
        let params = py_params(params)?;
        // Detach for native work: attached threads also block stop-the-world
        // garbage collection on free-threaded Python, even without a GIL.
        let result = py
            .detach(|| {
                let mutability = if read_only {
                    ScriptMutability::Immutable
                } else {
                    ScriptMutability::Mutable
                };
                self.db.run_script(query, params, mutability).map(|mut result| {
                    if decode {
                        let ordered = query.contains(":order") || query.contains(":sort");
                        self.decode(&mut result, ordered);
                    }
                    result
                })
            })
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("CozoDB query error: {}", e))
            })?;
        named_rows_to_py(py, result)
    }

    /// The stored id of each text, or -1 for a text the database does not
    /// hold.  Nothing is added.
    fn intern_ids(&self, py: Python<'_>, texts: Vec<String>) -> PyResult<Vec<i64>> {
        py.detach(|| self.lookup_ids(&texts)).map_err(|error| {
            PyRuntimeError::new_err(format!("CozoDB query error: {}", error))
        })
    }

    /// The first `limit` stored texts, in text order, that contain any of
    /// `fragments`, as `(text, id)` pairs.  The first call reads the whole
    /// dictionary into memory; later ones search it there.
    #[pyo3(signature = (fragments, limit, *, ignore_case=false))]
    fn texts_containing(
        &self,
        py: Python<'_>,
        fragments: Vec<String>,
        limit: usize,
        ignore_case: bool,
    ) -> PyResult<Vec<(String, i64)>> {
        py.detach(|| self.search_texts(&fragments, limit, ignore_case)).map_err(|error| {
            PyRuntimeError::new_err(format!("CozoDB query error: {}", error))
        })
    }

    /// Run several scripts in one transaction, converting each parameter
    /// batch only when its script is ready to execute.
    ///
    /// An operation is `(script, params)` or `(script, params, interned)`.
    /// `interned` maps the name of a parameter holding rows to the positions,
    /// in each row, of the cells that hold text to store as ids.  New texts
    /// are added to the dictionary in the same transaction.
    fn run_transaction(&self, py: Python<'_>, operations: &Bound<'_, PyList>) -> PyResult<()> {
        // Take the turn before opening the transaction: a writer waiting for
        // it with the database held would block the writer whose turn it is.
        let mut allocation = py.detach(|| Allocation::begin(self));
        let transaction = py.detach(|| self.db.multi_transaction(true));
        let abort = |error: PyErr| {
            let _ = py.detach(|| finish_transaction(&transaction, TransactionPayload::Abort));
            error
        };
        for operation in operations.iter() {
            let Operation { query, mut params, interned, texts } =
                py_operation(operation).map_err(&abort)?;
            let outcome = py.detach(|| -> Result<(), String> {
                let run = |script: &str, params| {
                    transaction.run_script(script, params).map_err(|error| error.to_string())
                };
                let (ids, entries) = allocation.resolve(&texts, &run)?;
                if !entries.is_empty() {
                    let entries = rows_value(entries);
                    run(PUT_INTERN, one_param("entries", entries.clone()))?;
                    run(PUT_INTERNED, one_param("entries", entries))?;
                }
                for (name, positions, mut rows) in interned {
                    place_ids(&mut rows, &positions, &ids);
                    params.insert(name, rows_value(rows));
                }
                run(&query, params).map(|_| ())
            });
            outcome
                .map_err(|error| PyRuntimeError::new_err(format!("CozoDB query error: {}", error)))
                .map_err(&abort)?;
        }
        py.detach(|| {
            finish_transaction(&transaction, TransactionPayload::Commit)?;
            allocation.commit();
            Ok(())
        })
        .map_err(|error: String| {
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
    ///
    /// ``interned`` maps a relation name to the positions, in each of its
    /// rows, of the cells that hold text to store as ids.  New texts are
    /// added to the dictionary by the same import.
    #[pyo3(signature = (data, interned=None))]
    fn import_relations(
        &self,
        py: Python<'_>,
        data: &Bound<'_, PyDict>,
        interned: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        let mut texts = BatchTexts::default();
        let mut relations = Vec::new();
        for (name, value) in data.iter() {
            let name: String = name.extract()?;
            let (headers, rows): (Vec<String>, Bound<'_, PyList>) = value.extract()?;
            let positions: Vec<usize> = match interned
                .map(|interned| interned.get_item(&name))
                .transpose()?
                .flatten()
            {
                Some(positions) => positions.extract()?,
                None => Vec::new(),
            };
            let rows = py_rows(&rows, &positions, &mut texts)?;
            relations.push((name, headers, positions, rows));
        }
        let texts = texts.into_texts();
        py.detach(|| -> Result<(), String> {
            let mut allocation = Allocation::begin(self);
            let read = |script: &str, params| self.read(script, params);
            let (ids, entries) = allocation.resolve(&texts, &read)?;
            let mut import = BTreeMap::new();
            if !entries.is_empty() {
                let headers = vec!["text".to_string(), "id".to_string()];
                import.insert("intern".to_string(), NamedRows::new(headers.clone(), entries.clone()));
                import.insert("interned".to_string(), NamedRows::new(headers, entries));
            }
            for (name, headers, positions, mut rows) in relations {
                place_ids(&mut rows, &positions, &ids);
                import.insert(name, NamedRows::new(headers, rows));
            }
            self.db.import_relations(import).map_err(|error| error.to_string())?;
            allocation.commit();
            Ok(())
        })
        .map_err(|error| {
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
