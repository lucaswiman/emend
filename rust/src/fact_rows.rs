//! Canonical per-file fact extraction.

use std::cmp::Reverse;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::cfg::{self, FunctionCfg};
use crate::scope::{FileScope, ImportBinding, LanguageConfig, Reference, ScopeResolver};
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
    kind: String,
    line: usize,
    end_line: usize,
    module_level: bool,
}

struct ImportRow {
    imported_module: String,
    imported_name: Option<String>,
    alias: Option<String>,
    line: usize,
}

#[derive(Debug, Clone)]
struct BlockRange {
    func_qn: String,
    block_id: u32,
    start_line: usize,
    end_line: usize,
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
                    kind: symbol.kind.clone(),
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

fn collect_statement_ranges(
    tree: &tree_sitter::Tree,
    config: &LanguageConfig,
) -> Vec<(usize, usize)> {
    fn walk(
        node: tree_sitter::Node<'_>,
        simple: &[String],
        recurse: &[String],
        output: &mut Vec<(usize, usize)>,
    ) {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if simple.iter().any(|kind| kind == child.kind()) {
                output.push((child.start_position().row + 1, child.end_position().row + 1));
            } else if recurse.iter().any(|kind| kind == child.kind()) {
                walk(child, simple, recurse, output);
            }
        }
    }

    let statements = &config.symbols.statements;
    let mut output = Vec::new();
    walk(
        tree.root_node(),
        &statements.effective_simple(),
        &statements.effective_recurse_into(),
        &mut output,
    );
    output
}

fn identifier_like(value: &str) -> bool {
    let mut chars = value.chars();
    chars
        .next()
        .is_some_and(|ch| ch == '_' || ch.is_alphabetic())
        && chars.all(|ch| ch == '_' || ch.is_alphanumeric())
}

fn ts_name_after_keywords(mut rest: &str) -> &str {
    for prefix in ["async ", "abstract ", "declare "] {
        if rest.starts_with(prefix) {
            rest = rest[prefix.len()..].trim();
        }
    }
    for prefix in [
        "function* ",
        "function ",
        "class ",
        "const ",
        "let ",
        "var ",
        "interface ",
        "type ",
        "enum ",
        "abstract class ",
    ] {
        if rest.starts_with(prefix) {
            rest = &rest[prefix.len()..];
            break;
        }
    }
    let end = rest
        .char_indices()
        .find(|(_, ch)| !(*ch == '_' || ch.is_alphanumeric()))
        .map_or(rest.len(), |(index, _)| index);
    &rest[..end]
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
            let lines: Vec<&str> = source.split('\n').collect();
            let mut names = HashSet::new();
            for (line, _) in collect_statement_ranges(tree, config) {
                let Some(text) = lines.get(line.saturating_sub(1)).map(|line| line.trim()) else {
                    continue;
                };
                if !(text.starts_with("export ") || text.starts_with("export{")) {
                    continue;
                }
                let after_export = text["export".len()..].trim_start();
                if after_export.starts_with('{') {
                    if !text.contains(" from ") {
                        if let (Some(start), Some(end)) = (text.find('{'), text.rfind('}')) {
                            for item in text[start + 1..end].split(',') {
                                let original =
                                    item.trim().split(" as ").next().unwrap_or("").trim();
                                if identifier_like(original) {
                                    names.insert(original.to_string());
                                }
                            }
                        }
                    }
                    continue;
                }
                let mut rest = text["export ".len()..].trim();
                if rest.starts_with("default ") {
                    rest = rest["default ".len()..].trim();
                }
                if !rest.contains(" from ") {
                    let name = ts_name_after_keywords(rest);
                    if !name.is_empty() {
                        names.insert(name.to_string());
                    }
                }
            }
            names
        }
        "rust" => {
            let lines: Vec<&str> = source.split('\n').collect();
            symbols
                .iter()
                .filter(|symbol| {
                    lines
                        .get(symbol.line.saturating_sub(1))
                        .is_some_and(|line| {
                            let line = line.trim_start();
                            line.starts_with("pub ") || line.starts_with("pub(")
                        })
                })
                .map(|symbol| symbol.name.clone())
                .collect()
        }
        _ => HashSet::new(),
    }
}

fn import_rows(
    language: &str,
    source: &str,
    tree: &tree_sitter::Tree,
    resolver: &ScopeResolver,
    file_scope: &FileScope,
) -> Vec<ImportRow> {
    match language {
        "python" => resolver
            .collect_structured_imports(tree, source)
            .into_iter()
            .flat_map(|import| {
                import.names.into_iter().map(move |name| ImportRow {
                    imported_module: if import.is_plain {
                        name.name.clone()
                    } else {
                        format!("{}{}", ".".repeat(import.level), import.module)
                    },
                    imported_name: (!import.is_plain).then_some(name.name),
                    alias: name.alias,
                    line: import.start_line + 1,
                })
            })
            .collect(),
        "typescript" | "javascript" => file_scope
            .imports
            .values()
            .filter_map(|import| binding_import_row(import, 0, true))
            .collect(),
        "rust" => resolver
            .collect_rust_imports(tree, source)
            .into_iter()
            .filter_map(|(import, line)| binding_import_row(&import, line, false))
            .collect(),
        _ => Vec::new(),
    }
}

fn binding_import_row(import: &ImportBinding, line: usize, typescript: bool) -> Option<ImportRow> {
    let imported_module = if typescript {
        import.module_path.trim_matches(['\'', '"']).to_string()
    } else {
        import.module_path.clone()
    };
    if imported_module.is_empty() && !import.is_star {
        return None;
    }
    Some(ImportRow {
        imported_module,
        imported_name: import
            .is_star
            .then(|| "*".to_string())
            .or_else(|| import.imported_name.clone()),
        alias: (typescript || import.imported_name.is_some())
            .then(|| import.local_name.clone())
            .filter(|local| Some(local.as_str()) != import.imported_name.as_deref()),
        line,
    })
}

fn resolve_relative_name(name: &str, package: &str) -> Option<String> {
    let level = name.chars().take_while(|ch| *ch == '.').count();
    if level == 0 {
        return Some(name.to_string());
    }
    let package_parts: Vec<&str> = package.split('.').filter(|part| !part.is_empty()).collect();
    if package_parts.len() < level {
        return None;
    }
    let base = package_parts[..=package_parts.len() - level].join(".");
    let remainder = &name[level..];
    if remainder.is_empty() {
        Some(base)
    } else {
        Some(format!("{base}.{remainder}"))
    }
}

fn resolve_reference_qn(qn: &str, relative_bindings: &[(String, String)]) -> String {
    let mut resolved = qn.to_string();
    if qn.starts_with('.') {
        for (relative, absolute) in relative_bindings {
            if qn == relative
                || qn
                    .strip_prefix(relative)
                    .is_some_and(|suffix| suffix.starts_with('.'))
            {
                resolved = format!("{absolute}{}", &qn[relative.len()..]);
                break;
            }
        }
    }
    normalize_qn(&resolved)
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

fn containing_block(ranges: &[BlockRange], line: usize) -> (String, i64) {
    let mut best: Option<&BlockRange> = None;
    for range in ranges.iter().rev().filter(|range| range.has_content) {
        if range.start_line <= line && line <= range.end_line {
            let span = range.end_line - range.start_line;
            if best.is_none_or(|current| span < current.end_line - current.start_line) {
                best = Some(range);
            }
        }
    }
    best.map_or_else(
        || (String::new(), -1),
        |range| (range.func_qn.clone(), range.block_id as i64),
    )
}

fn enclosing_symbol(symbols: &[SymbolRow], line: usize) -> Option<String> {
    let mut functions: Vec<&SymbolRow> = symbols
        .iter()
        .filter(|symbol| {
            matches!(
                symbol.kind.as_str(),
                "function" | "async_function" | "method" | "async_method"
            )
        })
        .collect();
    functions.sort_by_key(|symbol| Reverse(symbol.line));
    functions
        .into_iter()
        .find(|symbol| symbol.line <= line && line <= symbol.end_line)
        .map(|symbol| symbol.qualified_name.clone())
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

fn add_reference_rows(
    batch: &mut FactBatch,
    references: &[Reference],
    symbols: &[SymbolRow],
    rel_path: &str,
    module_name: &str,
    block_ranges: &[BlockRange],
    relative_bindings: &[(String, String)],
) {
    let definitions: HashSet<(String, usize)> = symbols
        .iter()
        .map(|symbol| (symbol.qualified_name.clone(), symbol.line))
        .collect();
    let mut blocks_by_line: HashMap<usize, (String, i64)> = HashMap::new();
    let mut module_defs: HashMap<String, Vec<(usize, usize)>> = HashMap::new();
    let mut module_uses: HashMap<String, Vec<(usize, usize)>> = HashMap::new();

    for reference in references {
        let target = resolve_reference_qn(&reference.qn.name, relative_bindings);
        let line = reference.line;
        let col = reference.column;
        let kind = reference.kind.as_str();
        let block = blocks_by_line
            .entry(line)
            .or_insert_with(|| containing_block(block_ranges, line))
            .clone();
        let (func_qn, block_id) = block;
        batch.relation("fg_refs").push(vec![
            target.clone().into(),
            rel_path.into(),
            line.into(),
            col.into(),
            kind.into(),
            func_qn.clone().into(),
            block_id.into(),
        ]);
        if !func_qn.is_empty() && block_id >= 0 && !definitions.contains(&(target.clone(), line)) {
            batch.relation("ref_by_block").push(vec![
                rel_path.into(),
                func_qn.clone().into(),
                block_id.into(),
                target.clone().into(),
            ]);
            let member = target.rsplit('.').next().unwrap_or(&target);
            if kind != "call"
                && target.contains('.')
                && member.starts_with('_')
                && !member.starts_with("__")
            {
                batch.relation("noncall_private_member_refs").push(vec![
                    rel_path.into(),
                    func_qn.clone().into(),
                    block_id.into(),
                    member.into(),
                ]);
            }
        } else {
            batch.relation("module_level_refs").push(vec![
                target.clone().into(),
                rel_path.into(),
                line.into(),
            ]);
        }

        if kind == "call" {
            let caller = enclosing_symbol(symbols, line).unwrap_or_else(|| module_name.to_string());
            let row = vec![
                caller.clone().into(),
                target.clone().into(),
                rel_path.into(),
                line.into(),
                col.into(),
                func_qn.clone().into(),
                block_id.into(),
            ];
            batch.relation("calls").push(row);
            batch.relation("calls_by_callee").push(vec![
                target.clone().into(),
                caller.clone().into(),
                rel_path.into(),
                line.into(),
                col.into(),
                func_qn.clone().into(),
                block_id.into(),
            ]);
            batch.relation("calls_by_file").push(vec![
                rel_path.into(),
                caller.into(),
                target.clone().into(),
                line.into(),
                col.into(),
                func_qn.clone().into(),
                block_id.into(),
            ]);
        }

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

fn add_method_call_rows(
    batch: &mut FactBatch,
    references: &[Reference],
    rel_path: &str,
    block_ranges: &[BlockRange],
) {
    for reference in references {
        if reference.kind.as_str() != "call" {
            continue;
        }
        let target = normalize_qn(&reference.lexical_qn);
        let Some((receiver, method)) = target.rsplit_once('.') else {
            continue;
        };
        let (mut func_qn, mut block_id) = containing_block(block_ranges, reference.line);
        if func_qn.is_empty() && block_id == -1 {
            func_qn = MODULE_LEVEL_FUNC.to_string();
            block_id = MODULE_LEVEL_BLOCK;
        }
        batch.relation("method_calls").push(vec![
            rel_path.into(),
            func_qn.into(),
            receiver.rsplit('.').next().unwrap_or(receiver).into(),
            method.into(),
            block_id.into(),
            reference.line.saturating_sub(1).into(),
        ]);
    }
}

fn load_config(ext: &str, project_root: &str, language: &str) -> Result<LanguageConfig, String> {
    let config = LanguageConfig::load_for_extension(ext, Path::new(project_root))?;
    if config.language.name != language
        && !(config.language.name == "typescript" && language == "javascript")
    {
        return Err(format!(
            "language {language:?} does not match extension {ext:?} ({:?})",
            config.language.name
        ));
    }
    Ok(config)
}

fn extract_batch(
    source: &str,
    ext: &str,
    abs_path: &str,
    rel_path: &str,
    project_root: &str,
    module_name: &str,
    language: &str,
) -> Result<FactBatch, String> {
    let config = load_config(ext, project_root, language)?;
    let tree = crate::pattern::parse_by_extension(source, ext)
        .ok_or_else(|| format!("failed to parse {abs_path}"))?;
    let resolver = ScopeResolver::new(config.clone(), PathBuf::from(project_root));
    let file_scope = resolver.build_file_scope_for_module(
        Path::new(abs_path),
        source,
        &tree,
        module_name.to_string(),
    );
    let raw_symbols = symbols::collect_symbols_from_tree(source, &tree, usize::MAX, &None, &config);
    let cfgs = cfg::build_cfgs_from_tree(source, &tree, &config);
    let flow = cfg::build_flow_facts_from_tree(source, &tree, &config);

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

    let imports = import_rows(language, source, &tree, &resolver, &file_scope);
    for import in &imports {
        batch.relation("imports").push(vec![
            rel_path.into(),
            import.imported_module.clone().into(),
            import.imported_name.clone().unwrap_or_default().into(),
            import.line.into(),
            import.alias.clone().unwrap_or_default().into(),
        ]);
    }
    let package = if Path::new(abs_path)
        .file_stem()
        .and_then(|stem| stem.to_str())
        == Some("__init__")
    {
        module_name
    } else {
        module_name
            .rsplit_once('.')
            .map_or("", |(package, _)| package)
    };
    let relative_bindings: Vec<(String, String)> = if ext == "py" {
        imports
            .iter()
            .filter_map(|import| {
                if !import.imported_module.starts_with('.') || import.imported_name.is_none() {
                    return None;
                }
                let name = import.imported_name.as_deref().unwrap_or_default();
                let absolute = resolve_relative_name(&import.imported_module, package)?;
                Some((
                    format!("{}.{}", import.imported_module, name),
                    format!("{absolute}.{name}"),
                ))
            })
            .collect()
    } else {
        Vec::new()
    };

    let block_ranges = add_cfg_rows(&mut batch, &cfgs, &symbols, rel_path, module_name);
    add_reference_rows(
        &mut batch,
        &file_scope.references,
        &symbols,
        rel_path,
        module_name,
        &block_ranges,
        &relative_bindings,
    );
    add_method_call_rows(&mut batch, &file_scope.references, rel_path, &block_ranges);
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
#[pyo3(signature = (source, ext, abs_path, rel_path, project_root, module_name, language))]
pub fn extract_file_fact_rows(
    py: Python<'_>,
    source: &str,
    ext: &str,
    abs_path: &str,
    rel_path: &str,
    project_root: &str,
    module_name: &str,
    language: &str,
) -> PyResult<PyObject> {
    let batch = py
        .allow_threads(|| {
            extract_batch(
                source,
                ext,
                abs_path,
                rel_path,
                project_root,
                module_name,
                language,
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
#[pyo3(signature = (source, ext, project_root, language))]
pub fn extract_exported_names(
    py: Python<'_>,
    source: &str,
    ext: &str,
    project_root: &str,
    language: &str,
) -> PyResult<Vec<String>> {
    py.allow_threads(|| -> Result<Vec<String>, String> {
        let config = load_config(ext, project_root, language)?;
        let tree = crate::pattern::parse_by_extension(source, ext)
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
            "/tmp",
            "pkg.mod",
            "python",
        )
        .unwrap();
        assert_eq!(batch.rows.len(), 20);
        assert!(batch.qnames.iter().any(|name| name == "pkg.mod.main"));
        assert!(batch.rows["exported_qns"]
            .iter()
            .any(|row| { matches!(&row[1], Cell::String(name) if name == "pkg.mod.main") }));
        assert!(batch.rows["fg_refs"]
            .iter()
            .any(|row| { matches!(&row[0], Cell::String(name) if name == "pkg.helpers.run") }));
        assert!(!batch.rows["reachable_blocks"].is_empty());
    }
}
