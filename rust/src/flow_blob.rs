//! Compact per-file storage of flow occurrences and edges.
//!
//! A project has millions of these facts and nothing joins on them: they are
//! only ever read back one file at a time.  One compressed, column-oriented
//! blob per file replaces a database row per occurrence.
//!
//! Layout after the format byte, zlib-compressed:
//! string table, length-prefixed event columns, edge columns.  Integers are
//! LEB128 varints; positions are stored as deltas so that they stay small.

use std::collections::HashMap;

use miniz_oxide::deflate::compress_to_vec_zlib;
use miniz_oxide::inflate::decompress_to_vec_zlib;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};

use crate::cfg::{FlowEdge, FlowEvent};

const FORMAT: u8 = 1;
const EVENT_FIELDS: usize = 19;

#[derive(Default)]
struct Writer {
    bytes: Vec<u8>,
}

impl Writer {
    fn uint(&mut self, mut value: u64) {
        while value >= 0x80 {
            self.bytes.push((value as u8) | 0x80);
            value >>= 7;
        }
        self.bytes.push(value as u8);
    }

    fn int(&mut self, value: i64) {
        self.uint(((value << 1) ^ (value >> 63)) as u64);
    }

    fn block(&mut self, block: &[u8]) {
        self.uint(block.len() as u64);
        self.bytes.extend_from_slice(block);
    }
}

struct Reader<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl<'a> Reader<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, position: 0 }
    }

    fn uint(&mut self) -> Result<u64, String> {
        let mut value = 0u64;
        let mut shift = 0u32;
        loop {
            let byte = *self
                .bytes
                .get(self.position)
                .ok_or("flow blob ends inside a number")?;
            self.position += 1;
            if shift >= 64 {
                return Err("flow blob number is too long".into());
            }
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Ok(value);
            }
            shift += 7;
        }
    }

    fn int(&mut self) -> Result<i64, String> {
        let value = self.uint()?;
        Ok(((value >> 1) as i64) ^ -((value & 1) as i64))
    }

    fn block(&mut self) -> Result<&'a [u8], String> {
        let length = usize::try_from(self.uint()?).map_err(|_| "flow blob block is too long")?;
        let end = self
            .position
            .checked_add(length)
            .filter(|end| *end <= self.bytes.len())
            .ok_or("flow blob ends inside a block")?;
        let block = &self.bytes[self.position..end];
        self.position = end;
        Ok(block)
    }

    fn rest(&self) -> &'a [u8] {
        &self.bytes[self.position..]
    }

    fn count(&mut self, minimum_bytes_each: usize) -> Result<usize, String> {
        let count = usize::try_from(self.uint()?).map_err(|_| "flow blob count is too large")?;
        // Every item needs at least this many bytes; reject absurd counts
        // before allocating for them.
        if count.saturating_mul(minimum_bytes_each) > self.bytes.len() - self.position {
            return Err("flow blob count exceeds its data".into());
        }
        Ok(count)
    }
}

#[derive(Default)]
struct Strings<'a> {
    ids: HashMap<&'a str, u64>,
    values: Vec<&'a str>,
}

impl<'a> Strings<'a> {
    fn id(&mut self, value: &'a str) -> u64 {
        if let Some(id) = self.ids.get(value) {
            return *id;
        }
        let id = self.values.len() as u64;
        self.ids.insert(value, id);
        self.values.push(value);
        id
    }

    /// Zero is "absent"; present strings are shifted by one.
    fn optional(&mut self, value: Option<&'a str>) -> u64 {
        value.map_or(0, |value| self.id(value) + 1)
    }
}

fn present(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|value| !value.is_empty())
}

/// Encode one file's flow facts.
///
/// The stored form has relation semantics: events are ordered by id with the
/// last one given for an id winning, and edges are an ordered set.  An empty
/// optional string is stored as absent.
pub fn encode(mut events: Vec<FlowEvent>, mut edges: Vec<FlowEdge>) -> Vec<u8> {
    events.sort_by_key(|event| event.id);
    let mut latest: Vec<FlowEvent> = Vec::with_capacity(events.len());
    for event in events {
        match latest.last_mut() {
            Some(previous) if previous.id == event.id => *previous = event,
            _ => latest.push(event),
        }
    }
    let events = &latest;
    edges.sort_by(|left, right| {
        (left.from, left.to, &left.kind).cmp(&(right.from, right.to, &right.kind))
    });
    edges.dedup();
    let edges = &edges;
    let mut strings = Strings::default();
    let mut columns = Writer::default();
    columns.uint(events.len() as u64);
    let mut previous = 0i64;
    for event in events {
        columns.int(i64::from(event.id) - previous);
        previous = i64::from(event.id);
    }
    for event in events {
        columns.uint(strings.id(&event.func_id));
    }
    for event in events {
        columns.uint(strings.id(&event.func_name));
    }
    for event in events {
        columns.uint(event.func_start as u64);
    }
    for event in events {
        columns.uint(strings.id(&event.role));
    }
    for event in events {
        columns.uint(strings.optional(present(&event.var)));
    }
    for event in events {
        columns.uint(strings.optional(present(&event.access_path)));
    }
    for event in events {
        columns.uint(u64::from(event.block));
    }
    previous = 0;
    for event in events {
        columns.int(event.start_byte as i64 - previous);
        previous = event.start_byte as i64;
    }
    for event in events {
        columns.int(event.end_byte as i64 - event.start_byte as i64);
    }
    previous = 0;
    for event in events {
        columns.int(i64::from(event.start_line) - previous);
        previous = i64::from(event.start_line);
    }
    for event in events {
        columns.uint(u64::from(event.start_col));
    }
    for event in events {
        columns.int(i64::from(event.end_line) - i64::from(event.start_line));
    }
    for event in events {
        columns.uint(u64::from(event.end_col));
    }
    previous = 0;
    for event in events {
        columns.int(i64::from(event.ordinal) - previous);
        previous = i64::from(event.ordinal);
    }
    for event in events {
        columns.uint(event.call_id.map_or(0, |id| u64::from(id) + 1));
    }
    for event in events {
        columns.uint(event.arg_index.map_or(0, |index| u64::from(index) + 1));
    }
    for event in events {
        columns.uint(strings.optional(present(&event.arg_name)));
    }
    for event in events {
        columns.uint(strings.id(&event.text));
    }

    let mut links = Writer::default();
    links.uint(edges.len() as u64);
    previous = 0;
    for edge in edges {
        links.int(i64::from(edge.from) - previous);
        previous = i64::from(edge.from);
    }
    for edge in edges {
        links.int(i64::from(edge.to) - i64::from(edge.from));
    }
    for edge in edges {
        links.uint(strings.id(&edge.kind));
    }

    let mut body = Writer::default();
    body.uint(strings.values.len() as u64);
    for value in &strings.values {
        body.block(value.as_bytes());
    }
    body.block(&columns.bytes);
    body.bytes.extend_from_slice(&links.bytes);

    let mut blob = Vec::with_capacity(body.bytes.len() / 4 + 16);
    blob.push(FORMAT);
    blob.extend_from_slice(&compress_to_vec_zlib(&body.bytes, 6));
    blob
}

/// The decompressed sections of one blob.
struct Sections {
    body: Vec<u8>,
    /// Byte ranges of the string table entries within ``body``.
    strings: Vec<(usize, usize)>,
    events: (usize, usize),
    edges: usize,
}

impl Sections {
    fn open(blob: &[u8]) -> Result<Self, String> {
        let (format, compressed) = blob.split_first().ok_or("flow blob is empty")?;
        if *format != FORMAT {
            return Err(format!("unknown flow blob format {format}"));
        }
        let body = decompress_to_vec_zlib(compressed)
            .map_err(|error| format!("flow blob is not readable: {error:?}"))?;
        let mut reader = Reader::new(&body);
        let count = reader.count(1)?;
        let mut strings = Vec::with_capacity(count);
        for _ in 0..count {
            let start_before = reader.position;
            let value = reader.block()?;
            std::str::from_utf8(value).map_err(|_| "flow blob string is not UTF-8")?;
            let end = reader.position;
            let start = end - value.len();
            debug_assert!(start >= start_before);
            strings.push((start, end));
        }
        let events = reader.block()?;
        let events_end = reader.position;
        let events = (events_end - events.len(), events_end);
        let edges = reader.position;
        Ok(Self { body, strings, events, edges })
    }

    fn string(&self, id: u64) -> Result<&str, String> {
        let (start, end) = *usize::try_from(id)
            .ok()
            .and_then(|id| self.strings.get(id))
            .ok_or("flow blob names a missing string")?;
        // Validated as UTF-8 when the blob was opened.
        Ok(std::str::from_utf8(&self.body[start..end]).expect("validated UTF-8"))
    }
}

/// One decoded occurrence; string fields are string-table ids.
struct EventRow {
    numbers: [i64; EVENT_FIELDS],
}

// Field positions shared by the decoder and the Python conversion.
const ID: usize = 0;
const FUNC_ID: usize = 1;
const FUNC_NAME: usize = 2;
const FUNC_START: usize = 3;
const ROLE: usize = 4;
const VAR: usize = 5;
const ACCESS_PATH: usize = 6;
const BLOCK: usize = 7;
const START_BYTE: usize = 8;
const END_BYTE: usize = 9;
const START_LINE: usize = 10;
const START_COL: usize = 11;
const END_LINE: usize = 12;
const END_COL: usize = 13;
const ORDINAL: usize = 14;
const CALL_ID: usize = 15;
const ARG_INDEX: usize = 16;
const ARG_NAME: usize = 17;
const TEXT: usize = 18;

fn unsigned(reader: &mut Reader) -> Result<i64, String> {
    i64::try_from(reader.uint()?).map_err(|_| "flow blob number is out of range".to_string())
}

fn decode_events(sections: &Sections) -> Result<Vec<EventRow>, String> {
    let mut reader = Reader::new(&sections.body[sections.events.0..sections.events.1]);
    let count = reader.count(EVENT_FIELDS)?;
    let mut rows: Vec<EventRow> = (0..count)
        .map(|_| EventRow { numbers: [0; EVENT_FIELDS] })
        .collect();
    let mut running = |rows: &mut Vec<EventRow>, field: usize| -> Result<(), String> {
        let mut previous = 0i64;
        for row in rows.iter_mut() {
            previous = previous
                .checked_add(reader.int()?)
                .ok_or("flow blob number is out of range")?;
            row.numbers[field] = previous;
        }
        Ok(())
    };
    running(&mut rows, ID)?;
    drop(running);
    for field in [FUNC_ID, FUNC_NAME, FUNC_START, ROLE, VAR, ACCESS_PATH, BLOCK] {
        for row in rows.iter_mut() {
            row.numbers[field] = unsigned(&mut reader)?;
        }
    }
    let mut previous = 0i64;
    for row in rows.iter_mut() {
        previous = previous.checked_add(reader.int()?).ok_or("flow blob number is out of range")?;
        row.numbers[START_BYTE] = previous;
    }
    for row in rows.iter_mut() {
        row.numbers[END_BYTE] = row.numbers[START_BYTE]
            .checked_add(reader.int()?)
            .ok_or("flow blob number is out of range")?;
    }
    previous = 0;
    for row in rows.iter_mut() {
        previous = previous.checked_add(reader.int()?).ok_or("flow blob number is out of range")?;
        row.numbers[START_LINE] = previous;
    }
    for row in rows.iter_mut() {
        row.numbers[START_COL] = unsigned(&mut reader)?;
    }
    for row in rows.iter_mut() {
        row.numbers[END_LINE] = row.numbers[START_LINE]
            .checked_add(reader.int()?)
            .ok_or("flow blob number is out of range")?;
    }
    for row in rows.iter_mut() {
        row.numbers[END_COL] = unsigned(&mut reader)?;
    }
    previous = 0;
    for row in rows.iter_mut() {
        previous = previous.checked_add(reader.int()?).ok_or("flow blob number is out of range")?;
        row.numbers[ORDINAL] = previous;
    }
    for field in [CALL_ID, ARG_INDEX, ARG_NAME, TEXT] {
        for row in rows.iter_mut() {
            row.numbers[field] = unsigned(&mut reader)?;
        }
    }
    if !reader.rest().is_empty() {
        return Err("flow blob has trailing event data".into());
    }
    let strings = sections.strings.len() as i64;
    for row in &rows {
        let required = [FUNC_ID, FUNC_NAME, ROLE, TEXT];
        let optional = [VAR, ACCESS_PATH, ARG_NAME];
        if required.iter().any(|field| row.numbers[*field] >= strings)
            || optional.iter().any(|field| row.numbers[*field] > strings)
            || row.numbers.iter().any(|number| *number < 0)
        {
            return Err("flow blob event is out of range".into());
        }
    }
    Ok(rows)
}

fn decode_edges(sections: &Sections) -> Result<Vec<(i64, i64, u64)>, String> {
    let mut reader = Reader::new(&sections.body[sections.edges..]);
    let count = reader.count(3)?;
    let mut edges = vec![(0i64, 0i64, 0u64); count];
    let mut previous = 0i64;
    for edge in edges.iter_mut() {
        previous = previous.checked_add(reader.int()?).ok_or("flow blob number is out of range")?;
        edge.0 = previous;
    }
    for edge in edges.iter_mut() {
        edge.1 = edge.0.checked_add(reader.int()?).ok_or("flow blob number is out of range")?;
    }
    for edge in edges.iter_mut() {
        edge.2 = reader.uint()?;
        if edge.0 < 0 || edge.1 < 0 || edge.2 >= sections.strings.len() as u64 {
            return Err("flow blob edge is out of range".into());
        }
    }
    if !reader.rest().is_empty() {
        return Err("flow blob has trailing edge data".into());
    }
    Ok(edges)
}

fn invalid(error: String) -> PyErr {
    PyValueError::new_err(error)
}

/// Python strings for a blob's string table, each created once.
fn python_strings<'py>(
    py: Python<'py>,
    sections: &Sections,
) -> Result<Vec<Bound<'py, PyString>>, String> {
    (0..sections.strings.len() as u64)
        .map(|id| Ok(PyString::new(py, sections.string(id)?)))
        .collect()
}

/// Encode ``events`` and ``edges`` into one file's flow blob.
///
/// Each event is a 19-item sequence ``(event_id, func_id, func_name,
/// func_start, role, var, access_path, block, start_byte, end_byte,
/// start_line, start_col, end_line, end_col, ordinal, call_id, arg_index,
/// arg_name, text)``; ``var``, ``access_path``, ``call_id``, ``arg_index`` and
/// ``arg_name`` may be ``None``.  Each edge is ``(from_event, to_event, kind)``.
#[pyfunction]
pub fn encode_flow_facts<'py>(
    py: Python<'py>,
    events: &Bound<'py, PyAny>,
    edges: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let mut native_events = Vec::new();
    for item in events.try_iter()? {
        let item = item?;
        native_events.push(FlowEvent {
            id: item.get_item(ID)?.extract()?,
            func_id: item.get_item(FUNC_ID)?.extract()?,
            func_name: item.get_item(FUNC_NAME)?.extract()?,
            func_start: item.get_item(FUNC_START)?.extract()?,
            role: item.get_item(ROLE)?.extract()?,
            var: item.get_item(VAR)?.extract()?,
            access_path: item.get_item(ACCESS_PATH)?.extract()?,
            block: item.get_item(BLOCK)?.extract()?,
            start_byte: item.get_item(START_BYTE)?.extract()?,
            end_byte: item.get_item(END_BYTE)?.extract()?,
            start_line: item.get_item(START_LINE)?.extract()?,
            start_col: item.get_item(START_COL)?.extract()?,
            end_line: item.get_item(END_LINE)?.extract()?,
            end_col: item.get_item(END_COL)?.extract()?,
            ordinal: item.get_item(ORDINAL)?.extract()?,
            call_id: item.get_item(CALL_ID)?.extract()?,
            arg_index: item.get_item(ARG_INDEX)?.extract()?,
            arg_name: item.get_item(ARG_NAME)?.extract()?,
            text: item.get_item(TEXT)?.extract()?,
        });
    }
    let mut native_edges = Vec::new();
    for item in edges.try_iter()? {
        let item = item?;
        native_edges.push(FlowEdge {
            from: item.get_item(0)?.extract()?,
            to: item.get_item(1)?.extract()?,
            kind: item.get_item(2)?.extract()?,
        });
    }
    Ok(PyBytes::new(py, &encode(native_events, native_edges)))
}

/// Decode the events of one flow blob as 19-item tuples (see
/// ``encode_flow_facts``).  Raises ``ValueError`` for unreadable bytes.
#[pyfunction]
pub fn decode_flow_events<'py>(py: Python<'py>, payload: &[u8]) -> PyResult<Bound<'py, PyList>> {
    let sections = Sections::open(payload).map_err(invalid)?;
    let rows = decode_events(&sections).map_err(invalid)?;
    let strings = python_strings(py, &sections).map_err(invalid)?;
    let none = py.None().into_bound(py);
    let result = PyList::empty(py);
    for row in rows {
        let mut items: Vec<Bound<'py, PyAny>> = Vec::with_capacity(EVENT_FIELDS);
        for (index, number) in row.numbers.iter().enumerate() {
            items.push(match index {
                FUNC_ID | FUNC_NAME | ROLE | TEXT => strings[*number as usize].clone().into_any(),
                VAR | ACCESS_PATH | ARG_NAME => {
                    if *number == 0 {
                        none.clone()
                    } else {
                        strings[*number as usize - 1].clone().into_any()
                    }
                }
                CALL_ID | ARG_INDEX => {
                    if *number == 0 {
                        none.clone()
                    } else {
                        (*number - 1).into_pyobject(py)?.into_any()
                    }
                }
                _ => number.into_pyobject(py)?.into_any(),
            });
        }
        result.append(PyTuple::new(py, items)?)?;
    }
    Ok(result)
}

/// Decode the edges of one flow blob as ``(from_event, to_event, kind)``.
/// Raises ``ValueError`` for unreadable bytes.
#[pyfunction]
pub fn decode_flow_edges<'py>(py: Python<'py>, payload: &[u8]) -> PyResult<Bound<'py, PyList>> {
    let sections = Sections::open(payload).map_err(invalid)?;
    let edges = decode_edges(&sections).map_err(invalid)?;
    let strings = python_strings(py, &sections).map_err(invalid)?;
    let result = PyList::empty(py);
    for (from, to, kind) in edges {
        result.append((from, to, strings[kind as usize].clone()))?;
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(id: u32, start: usize) -> FlowEvent {
        FlowEvent {
            id,
            func_id: "f@0".into(),
            func_name: "f".into(),
            func_start: 0,
            role: if id % 2 == 0 { "use".into() } else { "def".into() },
            var: (id % 3 != 0).then(|| format!("v{}", id % 5)),
            access_path: None,
            block: id % 4,
            start_byte: start,
            end_byte: start + 3,
            start_line: id / 2,
            start_col: 4,
            end_line: id / 2 + (id % 2),
            end_col: 7,
            ordinal: id,
            call_id: (id % 7 == 0).then_some(id),
            arg_index: (id % 5 == 0).then_some(0),
            arg_name: None,
            text: format!("v{}", id % 5),
        }
    }

    #[test]
    fn blob_round_trips_events_and_edges() {
        let events: Vec<FlowEvent> = (0..500).map(|id| event(id, id as usize * 9)).collect();
        let edges: Vec<FlowEdge> = (0..499)
            .map(|id| FlowEdge { from: id, to: 499 - id, kind: "control".into() })
            .collect();
        let blob = encode(events.clone(), edges.clone());
        let sections = Sections::open(&blob).unwrap();
        let rows = decode_events(&sections).unwrap();
        assert_eq!(rows.len(), events.len());
        for (row, event) in rows.iter().zip(&events) {
            assert_eq!(row.numbers[ID], i64::from(event.id));
            assert_eq!(row.numbers[START_BYTE], event.start_byte as i64);
            assert_eq!(row.numbers[END_BYTE], event.end_byte as i64);
            assert_eq!(row.numbers[END_LINE], i64::from(event.end_line));
            assert_eq!(sections.string(row.numbers[TEXT] as u64).unwrap(), event.text);
            assert_eq!(row.numbers[VAR] == 0, event.var.is_none());
            assert_eq!(row.numbers[CALL_ID], event.call_id.map_or(0, |id| i64::from(id) + 1));
        }
        let decoded = decode_edges(&sections).unwrap();
        assert_eq!(decoded.len(), edges.len());
        assert_eq!(decoded[7], (7, 492, decoded[0].2));
        assert!(blob.len() < 6 * (events.len() + edges.len()));
    }

    #[test]
    fn truncated_or_foreign_blobs_are_rejected() {
        let blob = encode(vec![event(0, 0)], vec![]);
        assert!(Sections::open(&[]).is_err());
        assert!(Sections::open(&[9, 1, 2, 3]).is_err());
        assert!(Sections::open(&blob[..blob.len() - 2]).is_err());
    }
}
