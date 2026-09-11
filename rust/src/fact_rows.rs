//! Canonical per-file fact extraction.

use std::cmp::Reverse;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::cfg::{self, FunctionCfg};
use crate::scope::{LanguageConfig, Reference, ScopeResolver};
use crate::symbols::{self, RustSymbol};

const MODULE_LEVEL_FUNC: &str = "<module>";
const MODULE_LEVEL_BLOCK: i64 = 0;

enum Cell {
    String(String),
    Int(i64),
    Bool(bool),
}

impl From<String> for Cell {
    fn from(value: String) -> Self {
        Self::String(value)
    }
}

impl From<&str> for Cell {
    fn from(value: &str) -> Self {
        Self::String(value.to_string())
    }
}

macro_rules! int_cells {
    ($($type:ty),+) => {$(
        impl From<$type> for Cell {
            fn from(value: $type) -> Self { Self::Int(value as i64) }
        }
    )+};
}

int_cells!(usize, u32, i64);

impl From<bool> for Cell {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}

type Relations = BTreeMap<&'static str, Vec<Vec<Cell>>>;

macro_rules! row {
    ($($value:expr),* $(,)?) => {
        vec![$(Cell::from($value)),*]
    };
}

struct FactBatch {
    rows: Relations,
    qnames: Vec<String>,
}

impl FactBatch {
    fn new() -> Self {
        let mut rows = Relations::new();
        for name in [
            "fg_sym",
            "search_sym",
            "dec",
            "cfg_blocks",
            "cfg_edges",
            "reachable_blocks",
            "fg_refs",
            "calls",
            "calls_by_callee",
            "calls_by_file",
            "def_uses",
            "method_calls",
            "source_locs",
            "imports",
            "ref_by_block",
            "noncall_private_member_refs",
            "module_level_refs",
            "exported_qns",
            "flow_events",
            "flow_edges",
            "local_refs",
            "local_imports",
        ] {
            rows.insert(name, Vec::new());
        }
        Self {
            rows,
            qnames: Vec::new(),
        }
    }

    fn relation(&mut self, name: &'static str) -> &mut Vec<Vec<Cell>> {
        self.rows.get_mut(name).expect("all relations initialized")
    }

    fn into_py(self, py: Python<'_>) -> PyResult<PyObject> {
        let result = PyDict::new(py);
        let rows = PyDict::new(py);
        for (name, relation) in self.rows {
            let py_rows = PyList::empty(py);
            for row in relation {
                let values: Vec<PyObject> = row
                    .into_iter()
                    .map(|cell| -> PyResult<PyObject> {
                        Ok(match cell {
                            Cell::String(value) => value.into_pyobject(py)?.into_any().unbind(),
                            Cell::Int(value) => value.into_pyobject(py)?.into_any().unbind(),
                            Cell::Bool(value) => {
                                value.into_pyobject(py)?.to_owned().into_any().unbind()
                            }
                        })
                    })
                    .collect::<PyResult<_>>()?;
                py_rows.append(values)?;
            }
            rows.set_item(name, py_rows)?;
        }
        result.set_item("rows", rows)?;
        result.set_item("qnames", self.qnames)?;
        Ok(result.into())
    }
}

#[derive(Debug, Clone)]
struct SymbolRow {
    name: String,
    qualified_name: String,
    line: usize,
    end_line: usize,
    module_level: bool,
}

#[derive(Debug, Clone)]
struct BlockRange {
    func_qn: String,
    block_id: u32,
    start_line: usize,
    end_line: usize,
    start_byte: usize,
    end_byte: usize,
    has_content: bool,
}

fn normalize_qn(qn: &str) -> String {
    let mut normalized = qn
        .replace(['\'', '"'], "")
        .replace("::", ".")
        .replace('/', ".");
    while normalized.contains("..") {
        normalized = normalized.replace("..", ".");
    }
    normalized.trim_start_matches('.').to_string()
}

fn project_symbols(
    batch: &mut FactBatch,
    symbols: &[RustSymbol],
    file_path: &str,
    module_name: &str,
) -> Vec<SymbolRow> {
    fn walk(
        batch: &mut FactBatch,
        output: &mut Vec<SymbolRow>,
        symbols: &[RustSymbol],
        file_path: &str,
        normalized_module: &str,
        parent_qn: Option<&str>,
    ) {
        for symbol in symbols {
            let local_qn = symbol
                .path
                .is_empty()
                .then(|| symbol.name.clone())
                .unwrap_or_else(|| symbol.path.join("."));
            let module_qn = format!("{normalized_module}.{local_qn}");
            let depth = symbol.path.len().max(1);
            let parent = if depth > 1 {
                module_qn
                    .rsplit_once('.')
                    .map(|(prefix, _)| prefix.to_string())
            } else {
                None
            };
            let signature = if matches!(symbol.kind.as_str(), "function" | "method") {
                format!(
                    "def {}{}",
                    symbol.name,
                    symbol.signature.as_deref().unwrap_or("")
                )
            } else {
                symbol.signature.clone().unwrap_or_default()
            };
            batch.relation("search_sym").push(vec![
                file_path.into(),
                module_qn.clone().into(),
                symbol.name.clone().into(),
                local_qn.into(),
                symbol.kind.clone().into(),
                symbol.line.into(),
                symbol.end_line.into(),
                depth.into(),
                parent.clone().unwrap_or_default().into(),
                signature.into(),
                symbol.returns.clone().unwrap_or_default().into(),
                symbol.decorators.join(",").into(),
            ]);

            if !matches!(symbol.kind.as_str(), "variable" | "reference") {
                let qualified_name = module_qn;
                let fact_parent = parent.clone().or_else(|| parent_qn.map(str::to_string));
                batch.relation("fg_sym").push(vec![
                    qualified_name.clone().into(),
                    file_path.into(),
                    symbol.name.clone().into(),
                    symbol.kind.clone().into(),
                    symbol.line.into(),
                    symbol.end_line.into(),
                    fact_parent.clone().unwrap_or_default().into(),
                ]);
                batch.relation("source_locs").push(vec![
                    file_path.into(),
                    "symbol".into(),
                    qualified_name.clone().into(),
                    symbol.line.into(),
                    0_i64.into(),
                    symbol.end_line.into(),
                    0_i64.into(),
                ]);
                for decorator in &symbol.decorators {
                    let mut cleaned = decorator.strip_prefix('@').unwrap_or(decorator).to_string();
                    if let Some(index) = cleaned.find('(') {
                        cleaned.truncate(index);
                    }
                    cleaned = cleaned.trim().to_string();
                    batch
                        .relation("dec")
                        .push(vec![qualified_name.clone().into(), cleaned.clone().into()]);
                    if let Some((_, basename)) = cleaned.rsplit_once('.') {
                        if basename != cleaned {
                            batch
                                .relation("dec")
                                .push(vec![qualified_name.clone().into(), basename.into()]);
                        }
                    }
                }
                output.push(SymbolRow {
                    name: symbol.name.clone(),
                    qualified_name: qualified_name.clone(),
                    line: symbol.line,
                    end_line: symbol.end_line,
                    module_level: parent_qn.is_none() && symbol.path.len() <= 1,
                });
                walk(
                    batch,
                    output,
                    &symbol.children,
                    file_path,
                    normalized_module,
                    Some(&qualified_name),
                );
            }
        }
    }

    let mut output = Vec::new();
    walk(
        batch,
        &mut output,
        symbols,
        file_path,
        &normalize_qn(module_name),
        None,
    );
    output
}

fn exported_names(
    source: &str,
    tree: &tree_sitter::Tree,
    language: &str,
    config: &LanguageConfig,
    symbols: &[RustSymbol],
) -> HashSet<String> {
    match language {
        "python" => crate::pattern::python_all_names_from_tree(source, tree)
            .into_iter()
            .collect(),
        "typescript" | "javascript" => {
            let mut names = HashSet::new();
            let export_kind = config
                .exports
                .export_statement
                .as_deref()
                .unwrap_or("export_statement");
            let mut cursor = tree.root_node().walk();
            for node in tree.root_node().named_children(&mut cursor) {
                if node.kind() != export_kind || node.child_by_field_name("source").is_some() {
                    continue;
                }
                for symbol in symbols.iter().filter(|symbol| {
                    node.start_position().row + 1 <= symbol.line
                        && symbol.line <= node.end_position().row + 1
                }) {
                    names.insert(symbol.name.clone());
                }
                let mut stack = vec![node];
                while let Some(current) = stack.pop() {
                    if matches!(
                        current.kind(),
                        "variable_declarator"
                            | "interface_declaration"
                            | "type_alias_declaration"
                            | "enum_declaration"
                            | "abstract_class_declaration"
                    ) {
                        if let Some(name) = current.child_by_field_name("name") {
                            names.insert(
                                String::from_utf8_lossy(&source.as_bytes()[name.byte_range()])
                                    .into_owned(),
                            );
                        }
                        continue;
                    }
                    if current.kind() == "export_specifier" {
                        if let Some(name) = current.child_by_field_name("name") {
                            names.insert(
                                String::from_utf8_lossy(&source.as_bytes()[name.byte_range()])
                                    .into_owned(),
                            );
                        }
                        continue;
                    }
                    if let Some(value) = current.child_by_field_name("value") {
                        if value.kind() == config.pattern_matching.identifier {
                            names.insert(
                                String::from_utf8_lossy(&source.as_bytes()[value.byte_range()])
                                    .into_owned(),
                            );
                        }
                    }
                    let mut children = current.walk();
                    stack.extend(current.named_children(&mut children));
                }
            }
            names
        }
        "rust" => {
            let visibility = config
                .exports
                .visibility_node
                .as_deref()
                .unwrap_or("visibility_modifier");
            symbols
                .iter()
                .filter(|symbol| {
                    let mut cursor = tree.root_node().walk();
                    let visible = tree.root_node().named_children(&mut cursor).any(|node| {
                        node.start_position().row + 1 == symbol.line
                            && node.child_by_field_name("name").is_some_and(|name| {
                                node.start_position().column == symbol.col_offset
                                    && &source.as_bytes()[name.byte_range()]
                                        == symbol.name.as_bytes()
                            })
                            && {
                                let mut children = node.walk();
                                let found = node.children(&mut children)
                                    .any(|child| child.kind() == visibility);
                                found
                            }
                    });
                    visible
                })
                .map(|symbol| symbol.name.clone())
                .collect()
        }
        _ => HashSet::new(),
    }
}

fn resolve_cfg_func_qn(cfg: &FunctionCfg, symbols: &[SymbolRow], module_name: &str) -> String {
    let start = cfg.func_start_line as usize + 1;
    let matching: Vec<&SymbolRow> = symbols
        .iter()
        .filter(|symbol| symbol.name == cfg.func_name)
        .collect();
    matching
        .iter()
        .find(|symbol| symbol.line <= start && start <= symbol.end_line.max(symbol.line))
        .or_else(|| matching.first())
        .map(|symbol| symbol.qualified_name.clone())
        .unwrap_or_else(|| format!("{module_name}.{}", cfg.func_name))
}

fn containing_block(ranges: &[BlockRange], byte_offset: usize) -> (String, i64) {
    let mut best: Option<&BlockRange> = None;
    for range in ranges.iter().rev().filter(|range| range.has_content) {
        if range.start_byte <= byte_offset && byte_offset < range.end_byte {
            let span = range.end_byte.saturating_sub(range.start_byte);
            if best.is_none_or(|current| {
                span < current.end_byte.saturating_sub(current.start_byte)
            }) {
                best = Some(range);
            }
        }
    }
    best.map_or_else(
        || (String::new(), -1),
        |range| (range.func_qn.clone(), range.block_id as i64),
    )
}

fn add_cfg_rows(
    batch: &mut FactBatch,
    cfgs: &[FunctionCfg],
    symbols: &[SymbolRow],
    rel_path: &str,
    module_name: &str,
) -> Vec<BlockRange> {
    let mut ranges = Vec::new();
    for graph in cfgs {
        let func_qn = resolve_cfg_func_qn(graph, symbols, module_name);
        for block in &graph.blocks {
            batch.relation("cfg_blocks").push(vec![
                rel_path.into(),
                func_qn.clone().into(),
                block.id.0.into(),
                (block.id == graph.entry).into(),
                (block.id == graph.exit).into(),
            ]);
            ranges.push(BlockRange {
                func_qn: func_qn.clone(),
                block_id: block.id.0,
                start_line: block.start_line as usize + 1,
                end_line: block.end_line as usize + 1,
                start_byte: block.start_byte,
                end_byte: block.end_byte,
                has_content: !(block.statements.is_empty()
                    && block.defs.is_empty()
                    && block.uses.is_empty()),
            });
        }
        for edge in &graph.edges {
            batch.relation("cfg_edges").push(vec![
                rel_path.into(),
                func_qn.clone().into(),
                edge.from.0.into(),
                edge.to.0.into(),
                edge.kind.as_str().into(),
                0_i64.into(),
                0_i64.into(),
            ]);
        }

        let mut visited = HashSet::new();
        let mut stack = vec![graph.entry];
        while let Some(block) = stack.pop() {
            if !visited.insert(block) {
                continue;
            }
            batch.relation("reachable_blocks").push(vec![
                rel_path.into(),
                func_qn.clone().into(),
                block.0.into(),
            ]);
            for edge in graph.edges.iter().filter(|edge| edge.from == block) {
                if !visited.contains(&edge.to) {
                    stack.push(edge.to);
                }
            }
        }
    }
    ranges.sort_by_key(|range| (range.start_line, Reverse(range.end_line - range.start_line)));
    for range in &ranges {
        if range.start_line > 0 && range.has_content {
            batch.relation("source_locs").push(vec![
                rel_path.into(),
                "block".into(),
                format!("{}:{}", range.func_qn, range.block_id).into(),
                range.start_line.into(),
                0_i64.into(),
                range.end_line.into(),
                0_i64.into(),
            ]);
        }
    }
    ranges
}

fn add_def_use_rows(
    batch: &mut FactBatch,
    cfgs: &[FunctionCfg],
    symbols: &[SymbolRow],
    rel_path: &str,
    module_name: &str,
) {
    for graph in cfgs {
        let func_qn = resolve_cfg_func_qn(graph, symbols, module_name);
        let mut definitions: HashMap<&str, Vec<(u32, u32, u32, &str)>> = HashMap::new();
        for block in &graph.blocks {
            for (name, line, col, kind) in &block.defs {
                definitions
                    .entry(name)
                    .or_default()
                    .push((block.id.0, *line, *col, kind));
            }
        }
        for block in &graph.blocks {
            for (name, line, col, _) in &block.uses {
                for (def_block, def_line, def_col, kind) in
                    definitions.get(name.as_str()).into_iter().flatten()
                {
                    batch.relation("def_uses").push(vec![
                        rel_path.into(),
                        func_qn.clone().into(),
                        name.clone().into(),
                        (*kind).into(),
                        (*def_block).into(),
                        block.id.0.into(),
                        (*def_line).into(),
                        (*def_col).into(),
                        (*line).into(),
                        (*col).into(),
                    ]);
                }
            }
        }
    }
}

fn add_local_reference_rows(
    batch: &mut FactBatch,
    references: &[Reference],
    rel_path: &str,
    module_name: &str,
    block_ranges: &[BlockRange],
) {
    let mut module_defs: HashMap<String, Vec<(usize, usize)>> = HashMap::new();
    let mut module_uses: HashMap<String, Vec<(usize, usize)>> = HashMap::new();

    for reference in references {
        let target = normalize_qn(&reference.qn.name);
        let line = reference.line;
        let col = reference.column;
        let kind = reference.kind.as_str();
        let (func_qn, block_id) = containing_block(block_ranges, reference.byte_offset);
        let target_kind = if reference.import_binding_id.is_some() {
            "import"
        } else if !reference.resolved {
            "unresolved"
        } else if target.starts_with("builtins.") {
            "builtin"
        } else {
            "local"
        };
        let caller = if func_qn.is_empty() {
            module_name.to_string()
        } else {
            func_qn.clone()
        };
        batch.relation("local_refs").push(vec![
            rel_path.into(),
            reference.lexical_qn.clone().into(),
            target.clone().into(),
            target_kind.into(),
            reference.import_binding_id.clone().unwrap_or_default().into(),
            kind.into(),
            line.into(),
            col.into(),
            func_qn.clone().into(),
            block_id.into(),
            caller.into(),
            reference.byte_offset.into(),
        ]);

        if func_qn.is_empty() && block_id == -1 {
            let name = target.rsplit('.').next().unwrap_or(&target).to_string();
            if kind == "write" {
                module_defs
                    .entry(name)
                    .or_default()
                    .push((line.saturating_sub(1), col));
            } else if matches!(kind, "read" | "call") {
                module_uses
                    .entry(name)
                    .or_default()
                    .push((line.saturating_sub(1), col));
            }
        }
    }

    for (name, uses) in module_uses {
        for (def_line, def_col) in module_defs.get(&name).into_iter().flatten() {
            for (use_line, use_col) in &uses {
                batch.relation("def_uses").push(vec![
                    rel_path.into(),
                    MODULE_LEVEL_FUNC.into(),
                    name.clone().into(),
                    "write".into(),
                    MODULE_LEVEL_BLOCK.into(),
                    MODULE_LEVEL_BLOCK.into(),
                    (*def_line).into(),
                    (*def_col).into(),
                    (*use_line).into(),
                    (*use_col).into(),
                ]);
            }
        }
    }
}

fn load_config(ext: &str, language: &str, config_toml: &str) -> Result<LanguageConfig, String> {
    let config = LanguageConfig::from_toml(config_toml)?;
    if config.language.name != language
        && !(config.language.name == "typescript" && language == "javascript")
    {
        return Err(format!(
            "language {language:?} does not match extension {ext:?} ({:?})",
            config.language.name
        ));
    }
    if !config.language.file_extensions.iter().any(|item| item == ext) {
        return Err(format!(
            "extension {ext:?} is not registered by language {language:?}"
        ));
    }
    Ok(config)
}

fn extract_batch(
    source: &str,
    ext: &str,
    abs_path: &str,
    rel_path: &str,
    module_name: &str,
    language: &str,
    config_toml: &str,
) -> Result<FactBatch, String> {
    let config = load_config(ext, language, config_toml)?;
    let tree = crate::pattern::parse_for_config(source, ext, &config)?
        .ok_or_else(|| format!("failed to parse {abs_path}"))?;
    let resolver = ScopeResolver::new(config.clone(), PathBuf::new());
    let file_scope = resolver.build_file_scope_for_module(
        Path::new(abs_path),
        source,
        &tree,
        module_name.to_string(),
    );
    let raw_symbols = symbols::collect_symbols_from_tree(source, &tree, usize::MAX, &None, &config);
    let (cfgs, flow) = cfg::build_analysis_from_tree(source, &tree, &config);

    let mut batch = FactBatch::new();
    for event in flow.events {
        batch.relation("flow_events").push(row![
            rel_path,
            event.id,
            event.func_id,
            event.func_name,
            event.func_start,
            event.role,
            event.var.unwrap_or_default(),
            event.access_path.unwrap_or_default(),
            event.block,
            event.start_byte,
            event.end_byte,
            event.start_line,
            event.start_col,
            event.end_line,
            event.end_col,
            event.ordinal,
            event.call_id.map_or(-1, i64::from),
            event.arg_index.map_or(-1, i64::from),
            event.arg_name.unwrap_or_default(),
            event.text,
        ]);
    }
    for edge in flow.edges {
        batch
            .relation("flow_edges")
            .push(row![rel_path, edge.from, edge.to, edge.kind]);
    }

    let symbols = project_symbols(&mut batch, &raw_symbols, rel_path, module_name);
    let exports = exported_names(source, &tree, language, &config, &raw_symbols);
    for symbol in &symbols {
        if symbol.module_level && exports.contains(&symbol.name) {
            batch
                .relation("exported_qns")
                .push(vec![rel_path.into(), symbol.qualified_name.clone().into()]);
        }
    }

    for scoped in &file_scope.scoped_imports {
        let import = &scoped.binding;
        let imported_module = import.module_path.trim_matches(['\'', '"']);
        let local_name = import.local_name.as_str();
        let imported_name = if matches!(language, "typescript" | "javascript")
            && (import.is_star || import.imported_name.as_deref() == Some("default"))
        {
            local_name
        } else if import.is_star {
            "*"
        } else {
            import.imported_name.as_deref().unwrap_or("")
        };
        let unaliased_local = import.imported_name.as_deref().unwrap_or_else(|| {
            imported_module.split(['.', ':']).next().unwrap_or(imported_module)
        });
        let alias = (!import.is_star && local_name != unaliased_local)
            .then_some(local_name)
            .unwrap_or("");
        let public_line = if matches!(language, "typescript" | "javascript") {
            0
        } else {
            scoped.line
        };
        batch.relation("imports").push(vec![
            rel_path.into(),
            imported_module.into(),
            imported_name.into(),
            public_line.into(),
            alias.into(),
        ]);
        let scope = file_scope.scopes.iter().find(|scope| scope.id == scoped.scope_id);
        batch.relation("local_imports").push(row![
            rel_path,
            scoped.binding_id.clone(),
            import.local_name.clone(),
            imported_module,
            import.imported_name.as_deref().unwrap_or(""),
            import.is_star,
            scoped.scope_id.0,
            scope.map_or(0, |scope| scope.start_byte),
            scope.map_or(0, |scope| scope.end_byte),
            scoped.byte_offset,
            scoped.line,
            language,
            module_name,
        ]);
    }

    let block_ranges = add_cfg_rows(&mut batch, &cfgs, &symbols, rel_path, module_name);
    add_local_reference_rows(
        &mut batch,
        &file_scope.references,
        rel_path,
        module_name,
        &block_ranges,
    );
    add_def_use_rows(&mut batch, &cfgs, &symbols, rel_path, module_name);
    batch.qnames = symbols
        .iter()
        .map(|symbol| symbol.qualified_name.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    Ok(batch)
}

/// Parse and extract one exact source revision into canonical relation rows.
#[pyfunction]
#[pyo3(signature = (source, ext, abs_path, rel_path, module_name, language, config_toml))]
pub fn extract_file_fact_rows(
    py: Python<'_>,
    source: &str,
    ext: &str,
    abs_path: &str,
    rel_path: &str,
    module_name: &str,
    language: &str,
    config_toml: &str,
) -> PyResult<PyObject> {
    let batch = py
        .allow_threads(|| {
            extract_batch(
                source,
                ext,
                abs_path,
                rel_path,
                module_name,
                language,
                config_toml,
            )
        })
        .map_err(|message| {
            if message.starts_with("language ") {
                PyValueError::new_err(message)
            } else {
                PyRuntimeError::new_err(message)
            }
        })?;
    batch.into_py(py)
}

/// Extract explicit module export names with the same parser and project
/// configuration used by whole-file fact extraction.
#[pyfunction]
#[pyo3(signature = (source, ext, language, config_toml))]
pub fn extract_exported_names(
    py: Python<'_>,
    source: &str,
    ext: &str,
    language: &str,
    config_toml: &str,
) -> PyResult<Vec<String>> {
    py.allow_threads(|| -> Result<Vec<String>, String> {
        let config = load_config(ext, language, config_toml)?;
        let tree = crate::pattern::parse_for_config(source, ext, &config)?
            .ok_or_else(|| format!("failed to parse source with extension {ext:?}"))?;
        let symbols = symbols::collect_symbols_from_tree(source, &tree, usize::MAX, &None, &config);
        Ok(exported_names(source, &tree, language, &config, &symbols)
            .into_iter()
            .collect())
    })
    .map_err(PyRuntimeError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_batch_contains_all_relations_and_resolves_relative_imports() {
        let source =
            "from .helpers import run\n__all__ = ['main']\ndef main(x):\n    return run(x)\n";
        let batch = extract_batch(
            source,
            "py",
            "/tmp/pkg/mod.py",
            "pkg/mod.py",
            "pkg.mod",
            "python",
            include_str!("../../languages/python/config.toml"),
        )
        .unwrap();
        assert_eq!(batch.rows.len(), 22);
        assert!(batch.qnames.iter().any(|name| name == "pkg.mod.main"));
        assert!(batch.rows["exported_qns"]
            .iter()
            .any(|row| { matches!(&row[1], Cell::String(name) if name == "pkg.mod.main") }));
        assert!(batch.rows["local_refs"]
            .iter()
            .any(|row| { matches!(&row[1], Cell::String(name) if name == "run") }));
        assert!(!batch.rows["reachable_blocks"].is_empty());
    }
}
