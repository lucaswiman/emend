//! Structural pattern IR and tree-sitter-based matcher for Python code.
//!
//! Implements a pattern IR compiled from Python dict IR and matched against
//! tree-sitter ASTs in parallel via rayon.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use tree_sitter::Node;

// ---------------------------------------------------------------------------
// Pattern IR
// ---------------------------------------------------------------------------

/// Pattern for a function call argument.
#[derive(Debug, Clone)]
pub enum ArgPattern {
    /// `$...ARGS` — matches zero or more arguments.
    Ellipsis,
    /// A specific argument pattern.
    Pattern(PatternNode),
}

/// Pattern for a function/lambda parameter.
#[derive(Debug, Clone)]
pub enum ParamPattern {
    /// `$...PARAMS` — matches zero or more parameters.
    Ellipsis,
    /// `$P` — matches any single parameter.
    Any,
    /// `$P=default_value` — matches a parameter with specific default.
    WithDefault(Box<PatternNode>),
}

/// Structural pattern node — compiled from a Python dict IR.
#[derive(Debug, Clone)]
pub enum PatternNode {
    /// Literal identifier: `print`, `str`, `isinstance`.
    Name(String),
    /// Glob identifier: `test_*`, `My*`.
    NameGlob(String),
    /// Any single expression (`$X`, `$_`).
    AnyExpr,
    /// Zero or more items — used in arg/element position only.
    Ellipsis,
    /// Function call: `func(args...)`.
    Call {
        func: Box<PatternNode>,
        args: Vec<ArgPattern>,
        exact_args: bool,
    },
    /// Attribute access: `value.attr`.
    Attr {
        value: Box<PatternNode>,
        attr: String,
    },
    /// Function definition (compound statement pattern).
    FuncDef {
        name: Box<PatternNode>,
        params: Vec<ParamPattern>,
    },
    /// Class definition (compound statement pattern).
    ClassDef {
        name: Box<PatternNode>,
        bases: Vec<PatternNode>,
    },
    /// Integer literal.
    Integer(String),
    /// String literal (`None` = any string).
    StringLiteral(Option<String>),
    /// Empty list `[]`.
    EmptyList,
    /// List with elements `[a, b, ...]`.
    List(Vec<PatternNode>),
    /// `None` literal.
    NoneLiteral,
    /// `True` or `False`.
    BoolLiteral(bool),
}

// ---------------------------------------------------------------------------
// Deserialization from Python dicts
// ---------------------------------------------------------------------------

fn deserialize_pattern(obj: &Bound<'_, PyAny>) -> PyResult<PatternNode> {
    let d = obj.downcast::<PyDict>()?;
    let type_str: String = d
        .get_item("type")?
        .ok_or_else(|| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>("Pattern dict missing 'type'")
        })?
        .extract()?;

    match type_str.as_str() {
        "name" => {
            let value: String = d
                .get_item("value")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Name pattern missing 'value'")
                })?
                .extract()?;
            Ok(PatternNode::Name(value))
        }

        "name_glob" => {
            let value: String = d
                .get_item("value")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>(
                        "NameGlob pattern missing 'value'",
                    )
                })?
                .extract()?;
            Ok(PatternNode::NameGlob(value))
        }

        "any_expr" => Ok(PatternNode::AnyExpr),
        "ellipsis" => Ok(PatternNode::Ellipsis),

        "call" => {
            let func_obj = d.get_item("func")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Call pattern missing 'func'")
            })?;
            let func = deserialize_pattern(&func_obj)?;

            let args_obj = d.get_item("args")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Call pattern missing 'args'")
            })?;
            let args_list = args_obj.downcast::<PyList>()?;
            let mut args = Vec::new();
            for item in args_list.iter() {
                let item_d = item.downcast::<PyDict>()?;
                let arg_type: String = item_d
                    .get_item("type")?
                    .ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("Arg missing 'type'")
                    })?
                    .extract()?;
                if arg_type == "ellipsis" {
                    args.push(ArgPattern::Ellipsis);
                } else {
                    args.push(ArgPattern::Pattern(deserialize_pattern(&item)?));
                }
            }

            let exact_args: bool = d
                .get_item("exact_args")?
                .and_then(|v| v.extract().ok())
                .unwrap_or(false);

            Ok(PatternNode::Call {
                func: Box::new(func),
                args,
                exact_args,
            })
        }

        "attr" => {
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Attr pattern missing 'value'")
            })?;
            let value = deserialize_pattern(&value_obj)?;
            let attr: String = d
                .get_item("attr")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Attr pattern missing 'attr'")
                })?
                .extract()?;
            Ok(PatternNode::Attr {
                value: Box::new(value),
                attr,
            })
        }

        "funcdef" => {
            let name_obj = d.get_item("name")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("FuncDef pattern missing 'name'")
            })?;
            let name = deserialize_pattern(&name_obj)?;

            let params_obj = d.get_item("params")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("FuncDef pattern missing 'params'")
            })?;
            let params_list = params_obj.downcast::<PyList>()?;
            let mut params = Vec::new();
            for item in params_list.iter() {
                let item_d = item.downcast::<PyDict>()?;
                let param_type: String = item_d
                    .get_item("type")?
                    .ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("Param missing 'type'")
                    })?
                    .extract()?;
                match param_type.as_str() {
                    "ellipsis" => params.push(ParamPattern::Ellipsis),
                    "any" => params.push(ParamPattern::Any),
                    "with_default" => {
                        let dv_obj = item_d.get_item("default_value")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>(
                                "WithDefault missing 'default_value'",
                            )
                        })?;
                        let dv = deserialize_pattern(&dv_obj)?;
                        params.push(ParamPattern::WithDefault(Box::new(dv)));
                    }
                    _ => params.push(ParamPattern::Any),
                }
            }
            Ok(PatternNode::FuncDef {
                name: Box::new(name),
                params,
            })
        }

        "classdef" => {
            let name_obj = d.get_item("name")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("ClassDef pattern missing 'name'")
            })?;
            let name = deserialize_pattern(&name_obj)?;

            let bases_obj = d.get_item("bases")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("ClassDef pattern missing 'bases'")
            })?;
            let bases_list = bases_obj.downcast::<PyList>()?;
            let mut bases = Vec::new();
            for item in bases_list.iter() {
                bases.push(deserialize_pattern(&item)?);
            }
            Ok(PatternNode::ClassDef {
                name: Box::new(name),
                bases,
            })
        }

        "integer" => {
            let value: String = d
                .get_item("value")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>(
                        "Integer pattern missing 'value'",
                    )
                })?
                .extract()?;
            Ok(PatternNode::Integer(value))
        }

        "string" => {
            let value: Option<String> = d
                .get_item("value")?
                .and_then(|v| v.extract().ok());
            Ok(PatternNode::StringLiteral(value))
        }

        "empty_list" => Ok(PatternNode::EmptyList),

        "list" => {
            let elems_obj = d.get_item("elements")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("List pattern missing 'elements'")
            })?;
            let elems_list = elems_obj.downcast::<PyList>()?;
            let mut elems = Vec::new();
            for item in elems_list.iter() {
                elems.push(deserialize_pattern(&item)?);
            }
            Ok(PatternNode::List(elems))
        }

        "none" => Ok(PatternNode::NoneLiteral),

        "bool" => {
            let value: bool = d
                .get_item("value")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Bool pattern missing 'value'")
                })?
                .extract()?;
            Ok(PatternNode::BoolLiteral(value))
        }

        other => Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
            "Unknown pattern type: {}",
            other
        ))),
    }
}

// ---------------------------------------------------------------------------
// Tree-sitter matching helpers
// ---------------------------------------------------------------------------

/// Get node text as a str slice from source bytes.
fn node_text<'a>(node: Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

/// Check if text matches a simple glob pattern (supports `*` wildcard).
///
/// Supports: `test_*`, `*_helper`, `My*Class`, `*`.
fn glob_matches(glob: &str, text: &str) -> bool {
    if glob == "*" {
        return true;
    }
    if let Some(idx) = glob.find('*') {
        let prefix = &glob[..idx];
        let suffix = &glob[idx + 1..];
        // No further wildcards in suffix for simplicity
        if suffix.contains('*') {
            // Multiple wildcards: split and check each segment
            // For now, handle simple prefix* and *suffix cases
            return text.starts_with(prefix) && text.ends_with(suffix)
                && text.len() >= prefix.len() + suffix.len();
        }
        text.starts_with(prefix) && text.ends_with(suffix)
            && text.len() >= prefix.len() + suffix.len()
    } else {
        glob == text
    }
}

/// Collect named children from an `argument_list` node, skipping punctuation.
///
/// In tree-sitter Python, `argument_list` contains:
/// - Regular args: their expression nodes directly
/// - Keyword args: `keyword_argument` nodes
/// - *args: `list_splat` nodes
/// - **kwargs: `dictionary_splat` nodes
fn collect_call_args<'a>(arg_list: Node<'a>) -> Vec<Node<'a>> {
    let mut args = Vec::new();
    let mut cursor = arg_list.walk();
    for child in arg_list.children(&mut cursor) {
        match child.kind() {
            "," | "(" | ")" => {}
            _ if child.is_named() => args.push(child),
            _ => {}
        }
    }
    args
}

/// Collect named children from a `parameters` node, skipping punctuation.
fn collect_params<'a>(params_node: Node<'a>) -> Vec<Node<'a>> {
    let mut params = Vec::new();
    let mut cursor = params_node.walk();
    for child in params_node.children(&mut cursor) {
        match child.kind() {
            "," | "(" | ")" => {}
            _ if child.is_named() => params.push(child),
            _ => {}
        }
    }
    params
}

/// Check if a list of arg patterns matches a list of call arg nodes.
fn match_args(arg_patterns: &[ArgPattern], call_args: &[Node], source: &[u8], exact: bool) -> bool {
    let has_ellipsis = arg_patterns.iter().any(|a| matches!(a, ArgPattern::Ellipsis));

    if !has_ellipsis {
        // No ellipsis: either exact count or at-least count
        if exact && call_args.len() != arg_patterns.len() {
            return false;
        }
        if !exact && call_args.len() < arg_patterns.len() {
            return false;
        }
        for (i, pattern) in arg_patterns.iter().enumerate() {
            if let ArgPattern::Pattern(pnode) = pattern {
                if i >= call_args.len() || !matches_node(call_args[i], source, pnode) {
                    return false;
                }
            }
        }
        return true;
    }

    // Has ellipsis: subsequence matching
    // Simple case: [Ellipsis] only → always match any number of args
    if arg_patterns.len() == 1 && matches!(arg_patterns[0], ArgPattern::Ellipsis) {
        return true;
    }

    // General case: collect non-Ellipsis patterns in order
    let non_ellipsis: Vec<&PatternNode> = arg_patterns
        .iter()
        .filter_map(|a| {
            if let ArgPattern::Pattern(p) = a {
                Some(p)
            } else {
                None
            }
        })
        .collect();

    if non_ellipsis.is_empty() {
        return true; // Only Ellipsis patterns
    }

    if call_args.len() < non_ellipsis.len() {
        return false;
    }

    // Find a contiguous window matching all non-ellipsis patterns in order
    'outer: for start in 0..=(call_args.len() - non_ellipsis.len()) {
        for (j, pnode) in non_ellipsis.iter().enumerate() {
            if !matches_node(call_args[start + j], source, pnode) {
                continue 'outer;
            }
        }
        return true;
    }
    false
}

/// Check if a param node has a default matching `dv`.
fn param_has_default(param: Node, source: &[u8], dv: &PatternNode) -> bool {
    if param.kind() != "default_parameter" && param.kind() != "typed_default_parameter" {
        return false;
    }
    if let Some(value_node) = param.child_by_field_name("value") {
        matches_node(value_node, source, dv)
    } else {
        false
    }
}

/// Check whether a list of param patterns matches a list of function parameter nodes.
fn match_params(param_patterns: &[ParamPattern], params: &[Node], source: &[u8]) -> bool {
    let has_ellipsis = param_patterns
        .iter()
        .any(|p| matches!(p, ParamPattern::Ellipsis));

    if !has_ellipsis {
        // Exact count required
        if params.len() != param_patterns.len() {
            return false;
        }
        for (i, pattern) in param_patterns.iter().enumerate() {
            match pattern {
                ParamPattern::Any => {} // always matches any param
                ParamPattern::Ellipsis => unreachable!(),
                ParamPattern::WithDefault(dv) => {
                    if !param_has_default(params[i], source, dv) {
                        return false;
                    }
                }
            }
        }
        return true;
    }

    // Has ellipsis: existence check — each non-ellipsis pattern must match at least one param
    let non_ellipsis: Vec<&ParamPattern> = param_patterns
        .iter()
        .filter(|p| !matches!(p, ParamPattern::Ellipsis))
        .collect();

    if non_ellipsis.is_empty() {
        return true; // Only Ellipsis patterns → match any params
    }

    for pp in &non_ellipsis {
        let found = params.iter().any(|param| match pp {
            ParamPattern::Any => true,
            ParamPattern::Ellipsis => unreachable!(),
            ParamPattern::WithDefault(dv) => param_has_default(*param, source, dv),
        });
        if !found {
            return false;
        }
    }
    true
}

// ---------------------------------------------------------------------------
// Core matcher
// ---------------------------------------------------------------------------

/// Match a tree-sitter node against a pattern node.
fn matches_node(node: Node, source: &[u8], pattern: &PatternNode) -> bool {
    match pattern {
        PatternNode::AnyExpr => true,
        PatternNode::Ellipsis => true, // Ellipsis in node position: always match

        PatternNode::Name(s) => {
            node.kind() == "identifier" && node_text(node, source) == s.as_str()
        }

        PatternNode::NameGlob(g) => {
            node.kind() == "identifier" && glob_matches(g, node_text(node, source))
        }

        PatternNode::Call {
            func,
            args,
            exact_args,
        } => {
            if node.kind() != "call" {
                return false;
            }
            let func_node = match node.child_by_field_name("function") {
                Some(n) => n,
                None => return false,
            };
            if !matches_node(func_node, source, func) {
                return false;
            }
            match node.child_by_field_name("arguments") {
                Some(arg_list) => {
                    let call_args = collect_call_args(arg_list);
                    match_args(args, &call_args, source, *exact_args)
                }
                None => args.is_empty(),
            }
        }

        PatternNode::Attr { value, attr } => {
            if node.kind() != "attribute" {
                return false;
            }
            let obj_node = match node.child_by_field_name("object") {
                Some(n) => n,
                None => return false,
            };
            let attr_node = match node.child_by_field_name("attribute") {
                Some(n) => n,
                None => return false,
            };
            matches_node(obj_node, source, value) && node_text(attr_node, source) == attr.as_str()
        }

        PatternNode::FuncDef { name, params } => {
            if node.kind() != "function_definition" {
                return false;
            }
            let name_node = match node.child_by_field_name("name") {
                Some(n) => n,
                None => return false,
            };
            if !matches_node(name_node, source, name) {
                return false;
            }
            let params_node = match node.child_by_field_name("parameters") {
                Some(n) => n,
                None => return false,
            };
            let param_nodes = collect_params(params_node);
            match_params(params, &param_nodes, source)
        }

        PatternNode::ClassDef { name, bases } => {
            if node.kind() != "class_definition" {
                return false;
            }
            let name_node = match node.child_by_field_name("name") {
                Some(n) => n,
                None => return false,
            };
            if !matches_node(name_node, source, name) {
                return false;
            }
            if bases.is_empty() {
                return true; // No base constraint
            }
            // Check whether the class has any Ellipsis-only bases constraint
            let has_ellipsis = bases.iter().any(|b| matches!(b, PatternNode::Ellipsis));
            if has_ellipsis && bases.len() == 1 {
                return true; // [Ellipsis] → any bases ok
            }

            // Get superclasses from the `superclasses` field
            let superclasses = match node.child_by_field_name("superclasses") {
                Some(n) => n,
                None => return false, // Pattern requires bases but none found
            };
            let base_nodes: Vec<Node> = {
                let mut cursor = superclasses.walk();
                superclasses
                    .children(&mut cursor)
                    .filter(|n| n.is_named() && n.kind() != ",")
                    .collect()
            };

            if base_nodes.len() != bases.len() {
                return false;
            }
            for (i, base_pattern) in bases.iter().enumerate() {
                if !matches_node(base_nodes[i], source, base_pattern) {
                    return false;
                }
            }
            true
        }

        PatternNode::Integer(v) => {
            node.kind() == "integer" && node_text(node, source) == v.as_str()
        }

        PatternNode::StringLiteral(v) => {
            if node.kind() != "string" && node.kind() != "concatenated_string" {
                return false;
            }
            match v {
                None => true,
                Some(expected) => node_text(node, source) == expected.as_str(),
            }
        }

        PatternNode::EmptyList => {
            node.kind() == "list" && node.named_child_count() == 0
        }

        PatternNode::List(elems) => {
            if node.kind() != "list" {
                return false;
            }
            if elems.is_empty() {
                return node.named_child_count() == 0;
            }
            let list_elems: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            if list_elems.len() != elems.len() {
                return false;
            }
            for (i, ep) in elems.iter().enumerate() {
                if !matches_node(list_elems[i], source, ep) {
                    return false;
                }
            }
            true
        }

        PatternNode::NoneLiteral => node.kind() == "none",

        PatternNode::BoolLiteral(v) => {
            if *v {
                node.kind() == "true"
            } else {
                node.kind() == "false"
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tree walker with ancestor tracking
// ---------------------------------------------------------------------------

/// Walk a tree tracking the ancestor stack, calling `callback` on each node.
fn walk_with_ancestors<'a, F>(
    node: Node<'a>,
    source: &[u8],
    ancestors: &mut Vec<Node<'a>>,
    callback: &mut F,
) where
    F: FnMut(Node, &[Node]),
{
    callback(node, ancestors);
    ancestors.push(node);
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            walk_with_ancestors(cursor.node(), source, ancestors, callback);
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
    ancestors.pop();
}

/// Check if any ancestor in the stack matches the given pattern.
fn any_ancestor_matches(ancestors: &[Node], source: &[u8], pattern: &PatternNode) -> bool {
    ancestors
        .iter()
        .any(|anc| matches_node(*anc, source, pattern))
}

// ---------------------------------------------------------------------------
// Per-file matching
// ---------------------------------------------------------------------------

/// Find all pattern matches against a pre-parsed tree.
///
/// Separating tree parsing from matching lets callers reuse a single parse
/// across multiple patterns (see `find_multi_patterns_in_files`).
fn find_pattern_in_tree(
    tree: &tree_sitter::Tree,
    source_bytes: &[u8],
    file_path: &str,
    pattern: &PatternNode,
    inside: Option<&PatternNode>,
    not_inside: Option<&PatternNode>,
) -> Vec<(String, usize, usize, usize, usize, String)> {
    let mut results = Vec::new();
    let mut ancestors: Vec<Node> = Vec::new();

    walk_with_ancestors(
        tree.root_node(),
        source_bytes,
        &mut ancestors,
        &mut |node, ancs| {
            if !matches_node(node, source_bytes, pattern) {
                return;
            }
            if let Some(inside_pat) = inside {
                if !any_ancestor_matches(ancs, source_bytes, inside_pat) {
                    return;
                }
            }
            if let Some(not_inside_pat) = not_inside {
                if any_ancestor_matches(ancs, source_bytes, not_inside_pat) {
                    return;
                }
            }

            let start = node.start_position();
            let end = node.end_position();
            let text =
                std::str::from_utf8(&source_bytes[node.start_byte()..node.end_byte()])
                    .unwrap_or("")
                    .to_string();

            results.push((
                file_path.to_string(),
                start.row + 1,
                start.column,
                end.row + 1,
                end.column,
                text,
            ));
        },
    );

    results
}

/// Find all pattern matches in a single source file.
fn find_pattern_in_source(
    source: &str,
    file_path: &str,
    pattern: &PatternNode,
    inside: Option<&PatternNode>,
    not_inside: Option<&PatternNode>,
) -> Vec<(String, usize, usize, usize, usize, String)> {
    let tree = match crate::pattern::parse_python(source) {
        Some(t) => t,
        None => return vec![],
    };
    find_pattern_in_tree(&tree, source.as_bytes(), file_path, pattern, inside, not_inside)
}

// ---------------------------------------------------------------------------
// PyO3 export
// ---------------------------------------------------------------------------

/// Find pattern matches across multiple files in parallel.
///
/// `file_contents`: list of `(path, source_text)` pairs.
/// `pattern_ir`: Python dict IR describing the pattern.
/// `inside_ir`: optional Python dict IR for the inside constraint.
/// `not_inside_ir`: optional Python dict IR for the not_inside constraint.
///
/// Returns a flat list of `(file, line, col, end_line, end_col, matched_text)`.
#[pyfunction]
#[pyo3(signature = (file_contents, pattern_ir, inside_ir=None, not_inside_ir=None))]
pub fn find_pattern_in_files(
    py: Python,
    file_contents: Vec<(String, String)>,
    pattern_ir: Bound<'_, PyAny>,
    inside_ir: Option<Bound<'_, PyAny>>,
    not_inside_ir: Option<Bound<'_, PyAny>>,
) -> PyResult<Vec<(String, usize, usize, usize, usize, String)>> {
    // Deserialize patterns on the main thread (requires GIL)
    let pattern = deserialize_pattern(&pattern_ir)?;
    let inside = inside_ir
        .as_ref()
        .map(|ir| deserialize_pattern(ir))
        .transpose()?;
    let not_inside = not_inside_ir
        .as_ref()
        .map(|ir| deserialize_pattern(ir))
        .transpose()?;

    // Release GIL for parallel processing
    py.allow_threads(|| {
        let results: Vec<Vec<(String, usize, usize, usize, usize, String)>> = file_contents
            .par_iter()
            .map(|(path, source)| {
                find_pattern_in_source(
                    source,
                    path,
                    &pattern,
                    inside.as_ref(),
                    not_inside.as_ref(),
                )
            })
            .collect();

        Ok(results.into_iter().flatten().collect())
    })
}

/// Find multiple patterns across multiple files in a single parallel pass.
///
/// Parses each file **once** with tree-sitter and applies all patterns to
/// that single parse tree, saving N_rules × parse overhead compared to
/// calling `find_pattern_in_files` once per rule.
///
/// `file_contents`: list of `(path, source_text)` pairs.
/// `patterns`: list of `(pattern_ir, not_inside_ir_or_None)` pairs, one per rule.
///
/// Returns a flat list of `(rule_idx, file, line, col, end_line, end_col, text)`.
#[pyfunction]
pub fn find_multi_patterns_in_files(
    py: Python,
    file_contents: Vec<(String, String)>,
    patterns: Vec<(Bound<'_, PyAny>, Option<Bound<'_, PyAny>>)>,
) -> PyResult<Vec<(usize, String, usize, usize, usize, usize, String)>> {
    // Deserialize all patterns on the main thread (requires access to Python objects)
    let compiled: Vec<(PatternNode, Option<PatternNode>)> = patterns
        .iter()
        .map(|(pat_ir, ni_ir)| {
            let pat = deserialize_pattern(pat_ir)?;
            let ni = ni_ir.as_ref().map(|ir| deserialize_pattern(ir)).transpose()?;
            Ok((pat, ni))
        })
        .collect::<PyResult<_>>()?;

    py.allow_threads(|| {
        let results: Vec<Vec<(usize, String, usize, usize, usize, usize, String)>> =
            file_contents
                .par_iter()
                .map(|(path, source)| {
                    let tree = match crate::pattern::parse_python(source) {
                        Some(t) => t,
                        None => return vec![],
                    };
                    let source_bytes = source.as_bytes();
                    let mut file_results = Vec::new();
                    for (rule_idx, (pattern, not_inside)) in compiled.iter().enumerate() {
                        for hit in find_pattern_in_tree(
                            &tree,
                            source_bytes,
                            path,
                            pattern,
                            None,
                            not_inside.as_ref(),
                        ) {
                            file_results.push((rule_idx, hit.0, hit.1, hit.2, hit.3, hit.4, hit.5));
                        }
                    }
                    file_results
                })
                .collect();

        Ok(results.into_iter().flatten().collect())
    })
}
