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
    /// `*args` — matches a starred argument.
    Star(PatternNode),
    /// `**kwargs` — matches a double-starred argument.
    DoubleStar(PatternNode),
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
        decorators: Vec<PatternNode>,
    },
    /// Class definition (compound statement pattern).
    ClassDef {
        name: Box<PatternNode>,
        bases: Vec<PatternNode>,
        decorators: Vec<PatternNode>,
    },
    /// Integer literal.
    Integer(String),
    /// Float literal.
    Float(String),
    /// String literal (`None` = any string).
    StringLiteral(Option<String>),
    /// Empty list `[]`.
    EmptyList,
    /// List with elements `[a, b, ...]`.
    List(Vec<PatternNode>),
    /// Subscript access: `value[slice]` (e.g., `Optional[X]`, `dict[str, int]`).
    Subscript {
        value: Box<PatternNode>,
        slices: Vec<PatternNode>,
    },
    /// Tuple literal: `(a, b, ...)`.
    Tuple(Vec<PatternNode>),
    /// `None` literal.
    NoneLiteral,
    /// `True` or `False`.
    BoolLiteral(bool),
    /// Binary operation: `a + b`, `a | b`, etc.
    BinaryOp {
        left: Box<PatternNode>,
        op: String,
        right: Box<PatternNode>,
    },
    /// Keyword argument: `key=value`.
    KeywordArg {
        key: String,
        value: Box<PatternNode>,
    },
    /// Assignment: `target = value`.
    Assign {
        target: Box<PatternNode>,
        value: Box<PatternNode>,
    },
    /// Augmented assignment: `target += value`.
    AugAssign {
        target: Box<PatternNode>,
        op: String,
        value: Box<PatternNode>,
    },
    /// Annotated assignment: `target: type = value`.
    AnnAssign {
        target: Box<PatternNode>,
        annotation: Box<PatternNode>,
        value: Option<Box<PatternNode>>,
    },
    /// Comparison: `a == b`, `a is b`, etc.
    Compare {
        left: Box<PatternNode>,
        ops: Vec<(String, PatternNode)>,
    },
    /// Unary operation: `not x`, `-x`.
    UnaryOp {
        op: String,
        operand: Box<PatternNode>,
    },
    /// Comprehension: `[elt for ... in ... if ...]`.
    Comprehension {
        kind: String, // "list_comprehension", "set_comprehension", "generator_expression"
        elt: Box<PatternNode>,
        generators: Vec<ComprehensionGenerator>,
    },
    /// Dictionary comprehension: `{key: value for ... in ... if ...]`.
    DictComprehension {
        key: Box<PatternNode>,
        value: Box<PatternNode>,
        generators: Vec<ComprehensionGenerator>,
    },
    /// F-string: `f"hello {name}"`.
    FString {
        parts: Vec<FStringPart>,
    },
    /// Type constraint: `:int`, `:str`, `:call`.
    TypeConstraint {
        kind: String,
    },
}

/// Part of an f-string.
#[derive(Debug, Clone)]
pub enum FStringPart {
    Text(String),
    Expr(PatternNode),
}

/// Generator part of a comprehension: `for target in iter if cond`.
#[derive(Debug, Clone)]
pub struct ComprehensionGenerator {
    pub target: PatternNode,
    pub iter: PatternNode,
    pub ifs: Vec<PatternNode>,
}

// ---------------------------------------------------------------------------
// Deserialization from Python dicts
// ---------------------------------------------------------------------------

fn deserialize_generator(obj: &Bound<'_, PyAny>) -> PyResult<ComprehensionGenerator> {
    let d = obj.downcast::<PyDict>()?;
    let target_obj = d.get_item("target")?.ok_or_else(|| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>("Generator missing 'target'")
    })?;
    let target = deserialize_pattern(&target_obj)?;
    let iter_obj = d.get_item("iter")?.ok_or_else(|| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>("Generator missing 'iter'")
    })?;
    let iter = deserialize_pattern(&iter_obj)?;
    let ifs_obj = d.get_item("ifs")?.ok_or_else(|| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>("Generator missing 'ifs'")
    })?;
    let ifs_list = ifs_obj.downcast::<PyList>()?;
    let mut ifs = Vec::new();
    for item in ifs_list.iter() {
        ifs.push(deserialize_pattern(&item)?);
    }
    Ok(ComprehensionGenerator { target, iter, ifs })
}

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
                } else if arg_type == "star" {
                    let v = item_d.get_item("value")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("Star arg missing 'value'")
                    })?;
                    args.push(ArgPattern::Star(deserialize_pattern(&v)?));
                } else if arg_type == "double_star" {
                    let v = item_d.get_item("value")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>(
                            "DoubleStar arg missing 'value'",
                        )
                    })?;
                    args.push(ArgPattern::DoubleStar(deserialize_pattern(&v)?));
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
            let decorators = if let Some(decs_obj) = d.get_item("decorators")? {
                let decs_list = decs_obj.downcast::<PyList>()?;
                let mut decs = Vec::new();
                for item in decs_list.iter() {
                    decs.push(deserialize_pattern(&item)?);
                }
                decs
            } else {
                Vec::new()
            };

            Ok(PatternNode::FuncDef {
                name: Box::new(name),
                params,
                decorators,
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

            let decorators = if let Some(decs_obj) = d.get_item("decorators")? {
                let decs_list = decs_obj.downcast::<PyList>()?;
                let mut decs = Vec::new();
                for item in decs_list.iter() {
                    decs.push(deserialize_pattern(&item)?);
                }
                decs
            } else {
                Vec::new()
            };

            Ok(PatternNode::ClassDef {
                name: Box::new(name),
                bases,
                decorators,
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

        "float" => {
            let value: String = d
                .get_item("value")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>(
                        "Float pattern missing 'value'",
                    )
                })?
                .extract()?;
            Ok(PatternNode::Float(value))
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

        "subscript" => {
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Subscript pattern missing 'value'")
            })?;
            let value = deserialize_pattern(&value_obj)?;

            let slices_obj = d.get_item("slices")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Subscript pattern missing 'slices'")
            })?;
            let slices_list = slices_obj.downcast::<PyList>()?;
            let mut slices = Vec::new();
            for item in slices_list.iter() {
                slices.push(deserialize_pattern(&item)?);
            }
            Ok(PatternNode::Subscript {
                value: Box::new(value),
                slices,
            })
        }

        "tuple" => {
            let elems_obj = d.get_item("elements")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Tuple pattern missing 'elements'")
            })?;
            let elems_list = elems_obj.downcast::<PyList>()?;
            let mut elems = Vec::new();
            for item in elems_list.iter() {
                elems.push(deserialize_pattern(&item)?);
            }
            Ok(PatternNode::Tuple(elems))
        }

        "binary_op" => {
            let left_obj = d.get_item("left")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("BinaryOp pattern missing 'left'")
            })?;
            let left = deserialize_pattern(&left_obj)?;
            let op: String = d.get_item("op")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("BinaryOp pattern missing 'op'")
            })?.extract()?;
            let right_obj = d.get_item("right")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("BinaryOp pattern missing 'right'")
            })?;
            let right = deserialize_pattern(&right_obj)?;
            Ok(PatternNode::BinaryOp {
                left: Box::new(left),
                op,
                right: Box::new(right),
            })
        }

        "keyword_arg" => {
            let key: String = d.get_item("key")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("KeywordArg pattern missing 'key'")
            })?.extract()?;
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("KeywordArg pattern missing 'value'")
            })?;
            let value = deserialize_pattern(&value_obj)?;
            Ok(PatternNode::KeywordArg {
                key,
                value: Box::new(value),
            })
        }

        "assign" => {
            let target_obj = d.get_item("target")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Assign pattern missing 'target'")
            })?;
            let target = deserialize_pattern(&target_obj)?;
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Assign pattern missing 'value'")
            })?;
            let value = deserialize_pattern(&value_obj)?;
            Ok(PatternNode::Assign {
                target: Box::new(target),
                value: Box::new(value),
            })
        }

        "aug_assign" => {
            let target_obj = d.get_item("target")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("AugAssign pattern missing 'target'")
            })?;
            let target = deserialize_pattern(&target_obj)?;
            let op: String = d
                .get_item("op")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("AugAssign pattern missing 'op'")
                })?
                .extract()?;
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("AugAssign pattern missing 'value'")
            })?;
            let value = deserialize_pattern(&value_obj)?;
            Ok(PatternNode::AugAssign {
                target: Box::new(target),
                op,
                value: Box::new(value),
            })
        }

        "comprehension" => {
            let kind: String = d.get_item("kind")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Comprehension missing 'kind'")
            })?.extract()?;
            let elt_obj = d.get_item("elt")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Comprehension missing 'elt'")
            })?;
            let elt = deserialize_pattern(&elt_obj)?;
            let gens_obj = d.get_item("generators")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Comprehension missing 'generators'")
            })?;
            let gens_list = gens_obj.downcast::<PyList>()?;
            let mut generators = Vec::new();
            for item in gens_list.iter() {
                generators.push(deserialize_generator(&item)?);
            }
            Ok(PatternNode::Comprehension { kind, elt: Box::new(elt), generators })
        }

        "dict_comprehension" => {
            let key_obj = d.get_item("key")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("DictComp missing 'key'")
            })?;
            let key = deserialize_pattern(&key_obj)?;
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("DictComp missing 'value'")
            })?;
            let value = deserialize_pattern(&value_obj)?;
            let gens_obj = d.get_item("generators")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("DictComp missing 'generators'")
            })?;
            let gens_list = gens_obj.downcast::<PyList>()?;
            let mut generators = Vec::new();
            for item in gens_list.iter() {
                generators.push(deserialize_generator(&item)?);
            }
            Ok(PatternNode::DictComprehension {
                key: Box::new(key),
                value: Box::new(value),
                generators,
            })
        }

        "fstring" => {
            let parts_obj = d.get_item("parts")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("FString missing 'parts'")
            })?;
            let parts_list = parts_obj.downcast::<PyList>()?;
            let mut parts = Vec::new();
            for item in parts_list.iter() {
                let item_d = item.downcast::<PyDict>()?;
                let part_type: String = item_d
                    .get_item("type")?
                    .ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("FString part missing 'type'")
                    })?
                    .extract()?;
                if part_type == "fstring_text" {
                    let value: String = item_d
                        .get_item("value")?
                        .ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>(
                                "FString text missing 'value'",
                            )
                        })?
                        .extract()?;
                    parts.push(FStringPart::Text(value));
                } else if part_type == "fstring_expr" {
                    let value_obj = item_d.get_item("value")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>(
                            "FString expr missing 'value'",
                        )
                    })?;
                    parts.push(FStringPart::Expr(deserialize_pattern(&value_obj)?));
                }
            }
            Ok(PatternNode::FString { parts })
        }

        "type_constraint" => {
            let kind: String = d
                .get_item("kind")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("TypeConstraint missing 'kind'")
                })?
                .extract()?;
            Ok(PatternNode::TypeConstraint { kind })
        }

        "ann_assign" => {
            let target_obj = d.get_item("target")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("AnnAssign pattern missing 'target'")
            })?;
            let target = deserialize_pattern(&target_obj)?;
            let annotation_obj = d.get_item("annotation")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("AnnAssign pattern missing 'annotation'")
            })?;
            let annotation = deserialize_pattern(&annotation_obj)?;
            let value = match d.get_item("value")? {
                Some(v) if !v.is_none() => Some(Box::new(deserialize_pattern(&v)?)),
                _ => None,
            };
            Ok(PatternNode::AnnAssign {
                target: Box::new(target),
                annotation: Box::new(annotation),
                value,
            })
        }

        "compare" => {
            let left_obj = d.get_item("left")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Compare pattern missing 'left'")
            })?;
            let left = deserialize_pattern(&left_obj)?;
            let ops_obj = d.get_item("ops")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Compare pattern missing 'ops'")
            })?;
            let ops_list = ops_obj.downcast::<PyList>()?;
            let mut ops = Vec::new();
            for item in ops_list.iter() {
                let pair = item.downcast::<PyList>()?;
                let op: String = pair.get_item(0)?.extract()?;
                let comparator = deserialize_pattern(&pair.get_item(1)?)?;
                ops.push((op, comparator));
            }
            Ok(PatternNode::Compare {
                left: Box::new(left),
                ops,
            })
        }

        "unary_op" => {
            let op: String = d.get_item("op")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("UnaryOp pattern missing 'op'")
            })?.extract()?;
            let operand_obj = d.get_item("operand")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("UnaryOp pattern missing 'operand'")
            })?;
            let operand = deserialize_pattern(&operand_obj)?;
            Ok(PatternNode::UnaryOp {
                op,
                operand: Box::new(operand),
            })
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

/// Check if an f-string pattern matches a node.
fn match_fstring(parts: &[FStringPart], node: Node, source: &[u8]) -> bool {
    if node.kind() != "string" {
        return false;
    }
    // Collect tree-sitter nodes corresponding to f-string parts
    let mut cursor = node.walk();
    let actual_parts: Vec<Node> = node
        .children(&mut cursor)
        .filter(|n| n.kind() == "string_content" || n.kind() == "interpolation")
        .collect();

    // The patterns also need to handle the case where multiple text parts
    // are merged in tree-sitter or vice versa.
    // For now, let's assume a one-to-one mapping if it matches exactly.
    if actual_parts.len() != parts.len() {
        return false;
    }

    for (i, p) in parts.iter().enumerate() {
        let actual = actual_parts[i];
        match p {
            FStringPart::Text(t) => {
                if actual.kind() != "string_content" {
                    return false;
                }
                if node_text(actual, source) != t.as_str() {
                    return false;
                }
            }
            FStringPart::Expr(pnode) => {
                if actual.kind() != "interpolation" {
                    return false;
                }
                // Interpolation contains '{', the expression, then '}'
                let mut c = actual.walk();
                let inner = actual.children(&mut c).find(|n| n.is_named());
                if let Some(inner_node) = inner {
                    if matches_node(inner_node, source, pnode).is_none() {
                        return false;
                    }
                } else {
                    return false;
                }
            }
        }
    }
    true
}

/// Check if a comprehension generator pattern matches a node.
fn match_generator(pattern: &ComprehensionGenerator, node: Node, source: &[u8]) -> bool {
    if node.kind() != "for_in_clause" {
        return false;
    }
    let target_node = match node.child_by_field_name("left") {
        Some(n) => n,
        None => return false,
    };
    let iter_node = match node.child_by_field_name("right") {
        Some(n) => n,
        None => return false,
    };
    if matches_node(target_node, source, &pattern.target).is_none()
        || matches_node(iter_node, source, &pattern.iter).is_none()
    {
        return false;
    }
    // Collect all if_clause conditions
    let mut cursor = node.walk();
    let if_nodes: Vec<Node> = node
        .children(&mut cursor)
        .filter(|n| n.kind() == "if_clause")
        .filter_map(|n| n.child_by_field_name("condition"))
        .collect();

    match_sequence(&pattern.ifs, &if_nodes, source)
}

/// Check if a list of comprehension generator patterns matches a list of nodes.
fn match_generators(patterns: &[ComprehensionGenerator], nodes: &[Node], source: &[u8]) -> bool {
    if nodes.len() != patterns.len() {
        return false;
    }
    for (i, p) in patterns.iter().enumerate() {
        if !match_generator(p, nodes[i], source) {
            return false;
        }
    }
    true
}

/// Check if a list of patterns matches a list of nodes, supporting Ellipsis.
fn match_sequence(patterns: &[PatternNode], nodes: &[Node], source: &[u8]) -> bool {
    let has_ellipsis = patterns.iter().any(|p| matches!(p, PatternNode::Ellipsis));
    if has_ellipsis && patterns.len() == 1 {
        return true;
    }
    if !has_ellipsis {
        if nodes.len() != patterns.len() {
            return false;
        }
        for (i, p) in patterns.iter().enumerate() {
            if matches_node(nodes[i], source, p).is_none() {
                return false;
            }
        }
        return true;
    }
    // Subsequence matching for mixed ellipsis
    let non_ellipsis: Vec<&PatternNode> = patterns
        .iter()
        .filter(|p| !matches!(p, PatternNode::Ellipsis))
        .collect();
    if non_ellipsis.is_empty() {
        return true;
    }
    if nodes.len() < non_ellipsis.len() {
        return false;
    }
    'outer: for start in 0..=(nodes.len() - non_ellipsis.len()) {
        for (j, pnode) in non_ellipsis.iter().enumerate() {
            if matches_node(nodes[start + j], source, pnode).is_none() {
                continue 'outer;
            }
        }
        return true;
    }
    false
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
            if i >= call_args.len() {
                return false;
            }
            match pattern {
                ArgPattern::Pattern(pnode) => {
                    if matches_node(call_args[i], source, pnode).is_none() {
                        return false;
                    }
                }
                ArgPattern::Star(pnode) => {
                    if call_args[i].kind() != "list_splat" {
                        return false;
                    }
                    let mut cursor = call_args[i].walk();
                    let inner = call_args[i].children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode).is_none() {
                            return false;
                        }
                    } else {
                        return false;
                    }
                }
                ArgPattern::DoubleStar(pnode) => {
                    if call_args[i].kind() != "dictionary_splat" {
                        return false;
                    }
                    let mut cursor = call_args[i].walk();
                    let inner = call_args[i].children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode).is_none() {
                            return false;
                        }
                    } else {
                        return false;
                    }
                }
                _ => return false,
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
    // (Note: Star/DoubleStar patterns are also included in the subsequence check)
    if call_args.len() < arg_patterns.iter().filter(|a| !matches!(a, ArgPattern::Ellipsis)).count() {
        return false;
    }

    let non_ellipsis: Vec<&ArgPattern> = arg_patterns
        .iter()
        .filter(|a| !matches!(a, ArgPattern::Ellipsis))
        .collect();

    if non_ellipsis.is_empty() {
        return true;
    }

    // Find a contiguous window matching all non-ellipsis patterns in order
    'outer: for start in 0..=(call_args.len() - non_ellipsis.len()) {
        for (j, pattern) in non_ellipsis.iter().enumerate() {
            let node = call_args[start + j];
            match pattern {
                ArgPattern::Pattern(pnode) => {
                    if matches_node(node, source, pnode).is_none() {
                        continue 'outer;
                    }
                }
                ArgPattern::Star(pnode) => {
                    if node.kind() != "list_splat" {
                        continue 'outer;
                    }
                    let mut cursor = node.walk();
                    let inner = node.children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode).is_none() {
                            continue 'outer;
                        }
                    } else {
                        continue 'outer;
                    }
                }
                ArgPattern::DoubleStar(pnode) => {
                    if node.kind() != "dictionary_splat" {
                        continue 'outer;
                    }
                    let mut cursor = node.walk();
                    let inner = node.children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode).is_none() {
                            continue 'outer;
                        }
                    } else {
                        continue 'outer;
                    }
                }
                _ => continue 'outer,
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
        matches_node(value_node, source, dv).is_some()
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
/// Returns Some(node) if it matches, where 'node' is the node to use for
/// positional information (usually the node itself, but may be a child
/// like expression_list for tuples to match LibCST's positions).
fn matches_node<'a>(node: Node<'a>, source: &[u8], pattern: &PatternNode) -> Option<Node<'a>> {
    match pattern {
        PatternNode::AnyExpr => Some(node),
        PatternNode::Ellipsis => Some(node), // Ellipsis in node position: always match

        PatternNode::Name(s) => {
            if node.kind() == "identifier" && node_text(node, source) == s.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::NameGlob(g) => {
            if node.kind() == "identifier" && glob_matches(g, node_text(node, source)) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Call {
            func,
            args,
            exact_args,
        } => {
            if node.kind() != "call" {
                return None;
            }
            let func_node = match node.child_by_field_name("function") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(func_node, source, func).is_none() {
                return None;
            }
            match node.child_by_field_name("arguments") {
                Some(arg_list) => {
                    let call_args = collect_call_args(arg_list);
                    if match_args(args, &call_args, source, *exact_args) {
                        Some(node)
                    } else {
                        None
                    }
                }
                None => {
                    if args.is_empty() {
                        Some(node)
                    } else {
                        None
                    }
                }
            }
        }

        PatternNode::Attr { value, attr } => {
            if node.kind() != "attribute" {
                return None;
            }
            let obj_node = match node.child_by_field_name("object") {
                Some(n) => n,
                None => return None,
            };
            let attr_node = match node.child_by_field_name("attribute") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(obj_node, source, value).is_some() && node_text(attr_node, source) == attr.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::FuncDef {
            name,
            params,
            decorators,
        } => {
            let (actual_decs, func_node) = if node.kind() == "decorated_definition" {
                let mut cursor = node.walk();
                let decs: Vec<Node> = node
                    .children(&mut cursor)
                    .filter(|n| n.kind() == "decorator")
                    .collect();
                let def = node.child_by_field_name("definition").unwrap_or(node);
                (decs, def)
            } else {
                (Vec::new(), node)
            };

            if func_node.kind() != "function_definition" {
                return None;
            }

            if !match_sequence(decorators, &actual_decs, source) {
                return None;
            }

            let name_node = match func_node.child_by_field_name("name") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(name_node, source, name).is_none() {
                return None;
            }

            let params_node = match func_node.child_by_field_name("parameters") {
                Some(n) => n,
                None => return None,
            };
            let actual_params = collect_params(params_node);
            if match_params(params, &actual_params, source) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::ClassDef {
            name,
            bases,
            decorators,
        } => {
            let (actual_decs, class_node) = if node.kind() == "decorated_definition" {
                let mut cursor = node.walk();
                let decs: Vec<Node> = node
                    .children(&mut cursor)
                    .filter(|n| n.kind() == "decorator")
                    .collect();
                let def = node.child_by_field_name("definition").unwrap_or(node);
                (decs, def)
            } else {
                (Vec::new(), node)
            };

            if class_node.kind() != "class_definition" {
                return None;
            }

            if !match_sequence(decorators, &actual_decs, source) {
                return None;
            }

            let name_node = match class_node.child_by_field_name("name") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(name_node, source, name).is_none() {
                return None;
            }

            if bases.is_empty() {
                return Some(node); // No base constraint
            }
            // Check whether the class has any Ellipsis-only bases constraint
            let has_ellipsis = bases.iter().any(|b| matches!(b, PatternNode::Ellipsis));
            if has_ellipsis && bases.len() == 1 {
                return Some(node); // [Ellipsis] → any bases ok
            }

            // Get superclasses from the `superclasses` field
            let superclasses = match class_node.child_by_field_name("superclasses") {
                Some(n) => n,
                None => return None, // Pattern requires bases but none found
            };
            let base_nodes: Vec<Node> = {
                let mut cursor = superclasses.walk();
                superclasses
                    .children(&mut cursor)
                    .filter(|n| n.is_named() && n.kind() != ",")
                    .collect()
            };

            if match_sequence(bases, &base_nodes, source) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Integer(v) => {
            if node.kind() == "integer" && node_text(node, source) == v.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Float(v) => {
            if node.kind() == "float" && node_text(node, source) == v.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::StringLiteral(v) => {
            if node.kind() != "string" && node.kind() != "concatenated_string" {
                return None;
            }
            match v {
                None => Some(node),
                Some(expected) => {
                    if node_text(node, source) == expected.as_str() {
                        Some(node)
                    } else {
                        None
                    }
                }
            }
        }

        PatternNode::EmptyList => {
            if node.kind() == "list" && node.named_child_count() == 0 {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::List(elems) => {
            if node.kind() != "list" {
                return None;
            }
            let list_elems: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            if match_sequence(elems, &list_elems, source) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Subscript { value, slices } => {
            // In tree-sitter Python, subscripts can appear as:
            // 1. "subscript" node (in expression context): `Optional[int]`
            //    fields: value (the object), subscript (the index/slice)
            // 2. "generic_type" node (in type annotation context): `Optional[int]`
            //    children: identifier + type_parameter (containing type children)
            if node.kind() == "generic_type" {
                // Generic type in annotation context
                let mut cursor = node.walk();
                let children: Vec<Node> = node.named_children(&mut cursor).collect();
                if children.is_empty() {
                    return None;
                }
                // First child is the name (identifier)
                if matches_node(children[0], source, value).is_none() {
                    return None;
                }
                // Second child is type_parameter containing the type args
                if children.len() < 2 || children[1].kind() != "type_parameter" {
                    return if slices.is_empty() { Some(node) } else { None };
                }
                let type_param = children[1];
                // Collect the type children inside type_parameter
                // type_parameter contains `type` nodes, each wrapping an expression
                let mut tp_cursor = type_param.walk();
                let actual_slices: Vec<Node> = type_param.named_children(&mut tp_cursor)
                    .filter(|n| n.kind() == "type")
                    .map(|n| if n.named_child_count() == 1 { n.named_child(0).unwrap() } else { n })
                    .collect();

                if match_sequence(slices, &actual_slices, source) {
                    Some(node)
                } else {
                    None
                }
            } else if node.kind() == "subscript" {
                let value_node = match node.child_by_field_name("value") {
                    Some(n) => n,
                    None => return None,
                };
                if matches_node(value_node, source, value).is_none() {
                    return None;
                }
                // Get all subscript fields (multiple: true in grammar)
                let mut cursor = node.walk();
                let actual_slices: Vec<Node> = node.children_by_field_name("subscript", &mut cursor)
                    .collect();
                
                if match_sequence(slices, &actual_slices, source) {
                    Some(node)
                } else {
                    None
                }
            } else {
                None
            }
        }

        PatternNode::Tuple(elems) => {
            if node.kind() != "tuple" && node.kind() != "expression_list" {
                return None;
            }
            let mut tuple_elems: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            
            // In tree-sitter Python, a 'tuple' node usually has one child: 'expression_list'.
            // If so, we want to match against the children of THAT list.
            let mut pos_node = node;
            if node.kind() == "tuple" && tuple_elems.len() == 1 && tuple_elems[0].kind() == "expression_list" {
                pos_node = tuple_elems[0];
                let mut cursor = pos_node.walk();
                tuple_elems = pos_node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect();
            }
            
            if match_sequence(elems, &tuple_elems, source) {
                Some(pos_node)
            } else {
                None
            }
        }

        PatternNode::BinaryOp { left, op, right } => {
            if node.kind() != "binary_operator" && node.kind() != "boolean_operator" {
                return None;
            }
            let left_node = match node.child_by_field_name("left") {
                Some(n) => n,
                None => return None,
            };
            let right_node = match node.child_by_field_name("right") {
                Some(n) => n,
                None => return None,
            };
            let op_node = match node.child_by_field_name("operator") {
                Some(n) => n,
                None => return None,
            };
            if node_text(op_node, source) == op.as_str()
                && matches_node(left_node, source, left).is_some()
                && matches_node(right_node, source, right).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::KeywordArg { key, value } => {
            if node.kind() != "keyword_argument" {
                return None;
            }
            let name_node = match node.child_by_field_name("name") {
                Some(n) => n,
                None => return None,
            };
            let value_node = match node.child_by_field_name("value") {
                Some(n) => n,
                None => return None,
            };
            if node_text(name_node, source) == key.as_str()
                && matches_node(value_node, source, value).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Assign { target, value } => {
            if node.kind() != "assignment" {
                return None;
            }
            let left_node = match node.child_by_field_name("left") {
                Some(n) => n,
                None => return None,
            };
            let right_node = match node.child_by_field_name("right") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(left_node, source, target).is_some() && matches_node(right_node, source, value).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::AugAssign { target, op, value } => {
            if node.kind() != "augmented_assignment" {
                return None;
            }
            let left_node = match node.child_by_field_name("left") {
                Some(n) => n,
                None => return None,
            };
            let op_node = match node.child_by_field_name("operator") {
                Some(n) => n,
                None => return None,
            };
            let right_node = match node.child_by_field_name("right") {
                Some(n) => n,
                None => return None,
            };
            if node_text(op_node, source) == op.as_str()
                && matches_node(left_node, source, target).is_some()
                && matches_node(right_node, source, value).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::AnnAssign {
            target,
            annotation,
            value,
        } => {
            if node.kind() != "annotated_assignment" {
                return None;
            }
            let left_node = match node.child_by_field_name("left") {
                Some(n) => n,
                None => return None,
            };
            let type_node = match node.child_by_field_name("type") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(left_node, source, target).is_none()
                || matches_node(type_node, source, annotation).is_none()
            {
                return None;
            }
            match (value, node.child_by_field_name("value")) {
                (Some(p), Some(n)) => {
                    if matches_node(n, source, p).is_some() {
                        Some(node)
                    } else {
                        None
                    }
                }
                (None, None) => Some(node),
                _ => None,
            }
        }

        PatternNode::Compare { left, ops } => {
            if node.kind() != "comparison_operator" {
                return None;
            }
            // tree-sitter comparison: children are [left, op1, comp1, op2, comp2, ...]
            let mut cursor = node.walk();
            let children: Vec<Node> = node.children(&mut cursor)
                .filter(|n| n.is_named() || !n.is_extra())
                .collect();
            // First named child is the left operand
            if children.is_empty() {
                return None;
            }
            if matches_node(children[0], source, left).is_none() {
                return None;
            }
            // Remaining children come in pairs: (operator, comparator)
            // unnamed children are operators, named children are comparators
            let named_children: Vec<Node> = node.children(&mut node.walk())
                .collect();
            // Just check we have at least the right structure
            // For simple cases (single comparison), this is straightforward
            if ops.len() == 1 {
                // Find the operator text and comparator
                let mut found_op = false;
                for i in 0..named_children.len() {
                    let child = named_children[i];
                    let text = node_text(child, source);
                    if text == ops[0].0 {
                        found_op = true;
                        // Next named child should be comparator
                        for j in (i+1)..named_children.len() {
                            if named_children[j].is_named() {
                                if matches_node(named_children[j], source, &ops[0].1).is_some() {
                                    return Some(node);
                                } else {
                                    return None;
                                }
                            }
                        }
                    }
                }
                if found_op { return Some(node); }
            }
            // Multi-comparison not yet supported in fast path
            None
        }

        PatternNode::UnaryOp { op, operand } => {
            if node.kind() != "unary_operator" && node.kind() != "not_operator" {
                return None;
            }
            // For "not" operator: tree-sitter uses "not_operator" kind
            if node.kind() == "not_operator" {
                if op != "not" {
                    return None;
                }
                let arg_node = match node.child_by_field_name("argument") {
                    Some(n) => n,
                    None => return None,
                };
                if matches_node(arg_node, source, operand).is_some() {
                    return Some(node);
                } else {
                    return None;
                }
            }
            // For other unary operators (-x, ~x, +x)
            let op_node = match node.child_by_field_name("operator") {
                Some(n) => n,
                None => return None,
            };
            let arg_node = match node.child_by_field_name("argument") {
                Some(n) => n,
                None => return None,
            };
            if node_text(op_node, source) == op.as_str()
                && matches_node(arg_node, source, operand).is_some() {
                Some(node)
            } else {
                None
            }
        }


        PatternNode::Comprehension {
            kind,
            elt,
            generators,
        } => {
            if node.kind() != kind.as_str() {
                return None;
            }
            let body_node = match node.child_by_field_name("body") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(body_node, source, elt).is_none() {
                return None;
            }
            // Collect generators
            let mut cursor = node.walk();
            let gen_nodes: Vec<Node> = node
                .children(&mut cursor)
                .filter(|n| n.kind() == "for_in_clause")
                .collect();
            if match_generators(generators, &gen_nodes, source) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::DictComprehension {
            key,
            value,
            generators,
        } => {
            if node.kind() != "dictionary_comprehension" {
                return None;
            }
            let key_node = match node.child_by_field_name("key") {
                Some(n) => n,
                None => return None,
            };
            let value_node = match node.child_by_field_name("value") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(key_node, source, key).is_none() || matches_node(value_node, source, value).is_none() {
                return None;
            }
            // Collect generators
            let mut cursor = node.walk();
            let gen_nodes: Vec<Node> = node
                .children(&mut cursor)
                .filter(|n| n.kind() == "for_in_clause")
                .collect();
            if match_generators(generators, &gen_nodes, source) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::FString { parts } => {
            if match_fstring(parts, node, source) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::TypeConstraint { kind } => {
            let matched = match kind.as_str() {
                "int" => node.kind() == "integer",
                "str" => node.kind() == "string" || node.kind() == "concatenated_string",
                "call" => node.kind() == "call",
                "float" => node.kind() == "float",
                _ => false,
            };
            if matched { Some(node) } else { None }
        }

        PatternNode::NoneLiteral => {
            if node.kind() == "none" {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::BoolLiteral(v) => {
            let matched = if *v {
                node.kind() == "true"
            } else {
                node.kind() == "false"
            };
            if matched { Some(node) } else { None }
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
        .any(|anc| matches_node(*anc, source, pattern).is_some())
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
            let matched_node = match matches_node(node, source_bytes, pattern) {
                Some(n) => n,
                None => return,
            };
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

            let start = matched_node.start_position();
            let end = matched_node.end_position();
            let text = std::str::from_utf8(&source_bytes[matched_node.start_byte()..matched_node.end_byte()])
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
