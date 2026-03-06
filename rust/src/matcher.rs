//! Structural pattern IR and tree-sitter-based matcher for Python code.
//!
//! Implements a pattern IR compiled from Python dict IR and matched against
//! tree-sitter ASTs in parallel via rayon.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use tree_sitter::Node;
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Pattern IR
// ---------------------------------------------------------------------------

/// Pattern for a function call argument.
#[derive(Debug, Clone)]
pub enum ArgPattern {
    /// `$...` — matches zero or more arguments.
    Ellipsis,
    /// `$...ARGS` — matches zero or more arguments and captures them.
    EllipsisMetavar(String),
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
    /// `$...PARAMS` — matches and captures zero or more parameters.
    EllipsisMetavar(String),
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
    /// A metavariable capture: `$X`.
    Metavar(String),
    /// Zero or more items — used in arg/element position only.
    Ellipsis,
    /// Zero or more items capture: `$...ARGS`.
    EllipsisMetavar(String),
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
        is_async: Option<bool>,
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
    /// Dictionary literal: `{"a": 1, **extras}`.
    Dict(Vec<DictElementPattern>),
    /// Set literal: `{a, b, *c}`.
    Set(Vec<PatternNode>),
}

/// Part of a dictionary literal.
#[derive(Debug, Clone)]
pub enum DictElementPattern {
    /// `key: value`
    Pair { key: PatternNode, value: PatternNode },
    /// `**value`
    Spread(PatternNode),
    /// `$...REST` — matches zero or more items.
    Ellipsis,
    /// `$...REST` — matches and captures zero or more items.
    EllipsisMetavar(String),
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
        "metavar" => {
            let name: String = d.get_item("name")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Metavar pattern missing 'name'")
            })?.extract()?;
            Ok(PatternNode::Metavar(name))
        }
        "ellipsis" => {
            if let Some(name_obj) = d.get_item("name")? {
                let name: String = name_obj.extract()?;
                Ok(PatternNode::EllipsisMetavar(name))
            } else {
                Ok(PatternNode::Ellipsis)
            }
        }

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
                    if let Some(name_obj) = item_d.get_item("name")? {
                        let name: String = name_obj.extract()?;
                        args.push(ArgPattern::EllipsisMetavar(name));
                    } else {
                        args.push(ArgPattern::Ellipsis);
                    }
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
                    "ellipsis" => {
                        if let Some(name_obj) = item_d.get_item("name")? {
                            let name: String = name_obj.extract()?;
                            params.push(ParamPattern::EllipsisMetavar(name));
                        } else {
                            params.push(ParamPattern::Ellipsis);
                        }
                    }
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

            let is_async = d
                .get_item("is_async")?
                .map(|obj| obj.extract::<bool>().ok())
                .flatten();

            Ok(PatternNode::FuncDef {
                name: Box::new(name),
                params,
                decorators,
                is_async,
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
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Integer pattern missing 'value'")
                })?
                .extract()?;
            Ok(PatternNode::Integer(value))
        }

        "float" => {
            let value: String = d
                .get_item("value")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Float pattern missing 'value'")
                })?
                .extract()?;
            Ok(PatternNode::Float(value))
        }

        "string" => {
            let value: Option<String> = d
                .get_item("value")?
                .map(|obj| obj.extract::<String>().ok())
                .flatten();
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
                PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    "Subscript pattern missing 'slices'",
                )
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

        "binary_op" => {
            let left_obj = d.get_item("left")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("BinaryOp pattern missing 'left'")
            })?;
            let left = deserialize_pattern(&left_obj)?;
            let right_obj = d.get_item("right")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("BinaryOp pattern missing 'right'")
            })?;
            let right = deserialize_pattern(&right_obj)?;
            let op: String = d
                .get_item("op")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("BinaryOp pattern missing 'op'")
                })?
                .extract()?;
            Ok(PatternNode::BinaryOp {
                left: Box::new(left),
                op,
                right: Box::new(right),
            })
        }

        "keyword_arg" => {
            let key: String = d
                .get_item("key")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("KeywordArg missing 'key'")
                })?
                .extract()?;
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("KeywordArg missing 'value'")
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
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("AugAssign pattern missing 'value'")
            })?;
            let value = deserialize_pattern(&value_obj)?;
            let op: String = d
                .get_item("op")?
                .ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("AugAssign pattern missing 'op'")
                })?
                .extract()?;
            Ok(PatternNode::AugAssign {
                target: Box::new(target),
                op,
                value: Box::new(value),
            })
        }

        "ann_assign" => {
            let target_obj = d.get_item("target")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("AnnAssign pattern missing 'target'")
            })?;
            let target = deserialize_pattern(&target_obj)?;
            let ann_obj = d.get_item("annotation")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    "AnnAssign pattern missing 'annotation'",
                )
            })?;
            let annotation = deserialize_pattern(&ann_obj)?;
            let value = if let Some(v_obj) = d.get_item("value")? {
                Some(Box::new(deserialize_pattern(&v_obj)?))
            } else {
                None
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
                let item_d = item.downcast::<PyDict>()?;
                let op: String = item_d.get_item("op")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("CompareOp missing 'op'")
                })?.extract()?;
                let comp_obj = item_d.get_item("comparator")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("CompareOp missing 'comparator'")
                })?;
                let comp = deserialize_pattern(&comp_obj)?;
                ops.push((op, comp));
            }
            Ok(PatternNode::Compare {
                left: Box::new(left),
                ops,
            })
        }

        "unary_op" => {
            let op: String = d.get_item("op")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("UnaryOp missing 'op'")
            })?.extract()?;
            let operand_obj = d.get_item("operand")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("UnaryOp missing 'operand'")
            })?;
            let operand = deserialize_pattern(&operand_obj)?;
            Ok(PatternNode::UnaryOp {
                op,
                operand: Box::new(operand),
            })
        }

        "comprehension" | "dict_comprehension" => {
            let gens_obj = d.get_item("generators")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Comprehension missing 'generators'")
            })?;
            let gens_list = gens_obj.downcast::<PyList>()?;
            let mut generators = Vec::new();
            for item in gens_list.iter() {
                generators.push(deserialize_generator(&item)?);
            }
            if type_str == "comprehension" {
                let elt_obj = d.get_item("elt")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Comprehension missing 'elt'")
                })?;
                let elt = deserialize_pattern(&elt_obj)?;
                let kind: String = d.get_item("kind")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Comprehension missing 'kind'")
                })?.extract()?;
                Ok(PatternNode::Comprehension {
                    kind,
                    elt: Box::new(elt),
                    generators,
                })
            } else {
                let key_obj = d.get_item("key")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("DictComprehension missing 'key'")
                })?;
                let key = deserialize_pattern(&key_obj)?;
                let val_obj = d.get_item("value")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>(
                        "DictComprehension missing 'value'",
                    )
                })?;
                let value = deserialize_pattern(&val_obj)?;
                Ok(PatternNode::DictComprehension {
                    key: Box::new(key),
                    value: Box::new(value),
                    generators,
                })
            }
        }

        "fstring" => {
            let parts_obj = d.get_item("parts")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("FString missing 'parts'")
            })?;
            let parts_list = parts_obj.downcast::<PyList>()?;
            let mut parts = Vec::new();
            for item in parts_list.iter() {
                let item_d = item.downcast::<PyDict>()?;
                let part_type: String = item_d.get_item("type")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("FString part missing 'type'")
                })?.extract()?;
                if part_type == "text" {
                    let text: String = item_d.get_item("value")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("FString text missing 'value'")
                    })?.extract()?;
                    parts.push(FStringPart::Text(text));
                } else {
                    let expr_obj = item_d.get_item("value")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("FString expr missing 'value'")
                    })?;
                    parts.push(FStringPart::Expr(deserialize_pattern(&expr_obj)?));
                }
            }
            Ok(PatternNode::FString { parts })
        }

        "type_constraint" => {
            let kind: String = d.get_item("kind")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("TypeConstraint missing 'kind'")
            })?.extract()?;
            Ok(PatternNode::TypeConstraint { kind })
        }

        "dict" => {
            let elements_obj = d.get_item("elements")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Dict missing 'elements'")
            })?;
            let elements_list = elements_obj.downcast::<PyList>()?;
            let mut elements = Vec::new();
            for item in elements_list.iter() {
                let item_d = item.downcast::<PyDict>()?;
                let el_type: String = item_d.get_item("type")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("Dict element missing 'type'")
                })?.extract()?;
                if el_type == "pair" {
                    let k = item_d.get_item("key")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("Dict pair missing 'key'")
                    })?;
                    let v = item_d.get_item("value")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("Dict pair missing 'value'")
                    })?;
                    elements.push(DictElementPattern::Pair {
                        key: deserialize_pattern(&k)?,
                        value: deserialize_pattern(&v)?,
                    });
                } else if el_type == "spread" {
                    let v = item_d.get_item("value")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("Dict spread missing 'value'")
                    })?;
                    elements.push(DictElementPattern::Spread(deserialize_pattern(&v)?));
                } else {
                    if let Some(name_obj) = item_d.get_item("name")? {
                        let name: String = name_obj.extract()?;
                        elements.push(DictElementPattern::EllipsisMetavar(name));
                    } else {
                        elements.push(DictElementPattern::Ellipsis);
                    }
                }
            }
            Ok(PatternNode::Dict(elements))
        }

        "set" => {
            let elements_obj = d.get_item("elements")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Set missing 'elements'")
            })?;
            let elements_list = elements_obj.downcast::<PyList>()?;
            let mut elements = Vec::new();
            for item in elements_list.iter() {
                elements.push(deserialize_pattern(&item)?);
            }
            Ok(PatternNode::Set(elements))
        }

        _ => Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
            "Unknown pattern type: {}",
            type_str
        ))),
    }
}

// ---------------------------------------------------------------------------
// Matching logic
// ---------------------------------------------------------------------------

fn node_text<'a>(node: Node<'a>, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

fn glob_matches(pattern: &str, text: &str) -> bool {
    if !pattern.contains('*') {
        return pattern == text;
    }
    let parts: Vec<&str> = pattern.split('*').collect();
    if parts.len() == 2 {
        if pattern.starts_with('*') {
            return text.ends_with(parts[1]);
        }
        if pattern.ends_with('*') {
            return text.starts_with(parts[0]);
        }
    }
    // Simple fallback: check if it contains all parts in order
    let mut current = text;
    for part in parts {
        if let Some(idx) = current.find(part) {
            current = &current[idx + part.len()..];
        } else {
            return false;
        }
    }
    true
}

/// Match a tree-sitter node against a pattern node.
/// Returns Some(node) if it matches, where 'node' is the node to use for
/// positional information (usually the node itself, but may be a child
/// like expression_list for tuples to match LibCST's positions).
fn matches_node<'a>(
    node: Node<'a>,
    source: &[u8],
    pattern: &PatternNode,
    captures: &mut HashMap<String, String>,
) -> Option<Node<'a>> {
    match pattern {
        PatternNode::AnyExpr => Some(node),
        PatternNode::Metavar(name) => {
            let text = node_text(node, source).to_string();
            if let Some(existing) = captures.get(name) {
                if existing != &text {
                    return None;
                }
            } else {
                captures.insert(name.clone(), text);
            }
            Some(node)
        }
        PatternNode::Ellipsis => Some(node),
        PatternNode::EllipsisMetavar(name) => {
            let text = node_text(node, source).to_string();
            if let Some(existing) = captures.get(name) {
                if existing != &text {
                    return None;
                }
            } else {
                captures.insert(name.clone(), text);
            }
            Some(node)
        }

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
            if matches_node(func_node, source, func, captures).is_none() {
                return None;
            }
            match node.child_by_field_name("arguments") {
                Some(arg_list) => {
                    let call_args = collect_call_args(arg_list);
                    if match_args(args, &call_args, source, *exact_args, captures) {
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
            if matches_node(obj_node, source, value, captures).is_some() && node_text(attr_node, source) == attr.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::FuncDef {
            name,
            params,
            decorators,
            is_async,
        } => {
            let (actual_decs, func_node) = if node.kind() == "decorated_definition" {
                let mut cursor = node.walk();
                let decs: Vec<Node> = node
                    .children(&mut cursor)
                    .filter(|n| n.kind() == "decorator")
                    .map(|n| n.named_child(0).unwrap_or(n))
                    .collect();
                let def = node.child_by_field_name("definition").unwrap_or(node);
                (decs, def)
            } else {
                (Vec::new(), node)
            };

            if func_node.kind() != "function_definition" {
                return None;
            }

            if let Some(expected_async) = is_async {
                let mut cursor = func_node.walk();
                let actual_async = func_node.children(&mut cursor).any(|n| n.kind() == "async");
                if actual_async != *expected_async {
                    return None;
                }
            }

            if !match_sequence(decorators, &actual_decs, source, captures) {
                return None;
            }

            let name_node = match func_node.child_by_field_name("name") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(name_node, source, name, captures).is_none() {
                return None;
            }

            let params_node = match func_node.child_by_field_name("parameters") {
                Some(n) => n,
                None => return None,
            };
            let actual_params = collect_params(params_node);
            if match_params(params, &actual_params, source, captures) {
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
                    .map(|n| n.named_child(0).unwrap_or(n))
                    .collect();
                let def = node.child_by_field_name("definition").unwrap_or(node);
                (decs, def)
            } else {
                (Vec::new(), node)
            };

            if class_node.kind() != "class_definition" {
                return None;
            }

            if !match_sequence(decorators, &actual_decs, source, captures) {
                return None;
            }

            let name_node = match class_node.child_by_field_name("name") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(name_node, source, name, captures).is_none() {
                return None;
            }

            if bases.is_empty() {
                return Some(node);
            }
            let has_ellipsis = bases.iter().any(|b| matches!(b, PatternNode::Ellipsis | PatternNode::EllipsisMetavar(_)));
            if has_ellipsis && bases.len() == 1 {
                return Some(node);
            }

            let superclasses = match class_node.child_by_field_name("superclasses") {
                Some(n) => n,
                None => return None,
            };
            let base_nodes: Vec<Node> = {
                let mut cursor = superclasses.walk();
                superclasses
                    .children(&mut cursor)
                    .filter(|n| n.is_named() && n.kind() != ",")
                    .collect()
            };

            if match_sequence(bases, &base_nodes, source, captures) {
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
            if match_sequence(elems, &list_elems, source, captures) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Subscript { value, slices } => {
            if node.kind() == "generic_type" {
                let mut cursor = node.walk();
                let children: Vec<Node> = node.named_children(&mut cursor).collect();
                if children.is_empty() {
                    return None;
                }
                if matches_node(children[0], source, value, captures).is_none() {
                    return None;
                }
                if children.len() < 2 || children[1].kind() != "type_parameter" {
                    return if slices.is_empty() { Some(node) } else { None };
                }
                let type_param = children[1];
                let mut tp_cursor = type_param.walk();
                let actual_slices: Vec<Node> = type_param.named_children(&mut tp_cursor)
                    .filter(|n| n.kind() == "type")
                    .map(|n| if n.named_child_count() == 1 { n.named_child(0).unwrap() } else { n })
                    .collect();

                if match_sequence(slices, &actual_slices, source, captures) {
                    Some(node)
                } else {
                    None
                }
            } else if node.kind() == "subscript" {
                let value_node = match node.child_by_field_name("value") {
                    Some(n) => n,
                    None => return None,
                };
                if matches_node(value_node, source, value, captures).is_none() {
                    return None;
                }
                let mut cursor = node.walk();
                let actual_slices: Vec<Node> = node.children_by_field_name("subscript", &mut cursor)
                    .collect();
                
                if match_sequence(slices, &actual_slices, source, captures) {
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
            
            let mut pos_node = node;
            if node.kind() == "tuple" && tuple_elems.len() == 1 && tuple_elems[0].kind() == "expression_list" {
                pos_node = tuple_elems[0];
                let mut cursor = pos_node.walk();
                tuple_elems = pos_node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect();
            }
            
            if match_sequence(elems, &tuple_elems, source, captures) {
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
                && matches_node(left_node, source, left, captures).is_some()
                && matches_node(right_node, source, right, captures).is_some() {
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
                && matches_node(value_node, source, value, captures).is_some() {
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
                && matches_node(left_node, source, target, captures).is_some()
                && matches_node(right_node, source, value, captures).is_some() {
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
            if matches_node(left_node, source, target, captures).is_none()
                || matches_node(type_node, source, annotation, captures).is_none()
            {
                return None;
            }
            match (value, node.child_by_field_name("value")) {
                (Some(p), Some(n)) => {
                    if matches_node(n, source, p, captures).is_some() {
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
            let mut cursor = node.walk();
            let children: Vec<Node> = node.children(&mut cursor)
                .filter(|n| n.is_named() || !n.is_extra())
                .collect();
            if children.is_empty() {
                return None;
            }
            if matches_node(children[0], source, left, captures).is_none() {
                return None;
            }

            let mut current_child_idx = 1;
            for (expected_op, expected_comp) in ops {
                let mut found_op = false;
                while current_child_idx < children.len() {
                    let child = children[current_child_idx];
                    if !child.is_named() {
                        if node_text(child, source) == expected_op.as_str() {
                            found_op = true;
                            current_child_idx += 1;
                            break;
                        }
                    }
                    current_child_idx += 1;
                }
                if !found_op { return None; }

                let mut found_comp = false;
                while current_child_idx < children.len() {
                    let child = children[current_child_idx];
                    if child.is_named() {
                        if matches_node(child, source, expected_comp, captures).is_some() {
                            found_comp = true;
                            current_child_idx += 1;
                            break;
                        } else {
                            return None;
                        }
                    }
                    current_child_idx += 1;
                }
                if !found_comp { return None; }
            }
            Some(node)
        }

        PatternNode::UnaryOp { op, operand } => {
            if node.kind() != "unary_operator" && node.kind() != "not_operator" {
                return None;
            }
            if node.kind() == "not_operator" {
                if op != "not" {
                    return None;
                }
                let arg_node = match node.child_by_field_name("argument") {
                    Some(n) => n,
                    None => return None,
                };
                if matches_node(arg_node, source, operand, captures).is_some() {
                    return Some(node);
                } else {
                    return None;
                }
            }
            let op_node = match node.child_by_field_name("operator") {
                Some(n) => n,
                None => return None,
            };
            let arg_node = match node.child_by_field_name("argument") {
                Some(n) => n,
                None => return None,
            };
            if node_text(op_node, source) == op.as_str()
                && matches_node(arg_node, source, operand, captures).is_some() {
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
            if matches_node(body_node, source, elt, captures).is_none() {
                return None;
            }
            let mut cursor = node.walk();
            let gen_nodes: Vec<Node> = node
                .children(&mut cursor)
                .filter(|n| n.kind() == "for_in_clause")
                .collect();
            if match_generators(generators, &gen_nodes, source, captures) {
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
            if matches_node(key_node, source, key, captures).is_none() || matches_node(value_node, source, value, captures).is_none() {
                return None;
            }
            let mut cursor = node.walk();
            let gen_nodes: Vec<Node> = node
                .children(&mut cursor)
                .filter(|n| n.kind() == "for_in_clause")
                .collect();
            if match_generators(generators, &gen_nodes, source, captures) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::FString { parts } => {
            if match_fstring(parts, node, source, captures) {
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
            if matches_node(left_node, source, target, captures).is_some() && matches_node(right_node, source, value, captures).is_some() {
                Some(node)
            } else {
                None
            }
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

        PatternNode::Dict(elements) => {
            if node.kind() != "dictionary" {
                return None;
            }
            let dict_elements: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            if match_dict_elements(elements, &dict_elements, source, captures) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Set(elems) => {
            if node.kind() != "set" {
                return None;
            }
            let set_elems: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            if match_sequence(elems, &set_elems, source, captures) {
                Some(node)
            } else {
                None
            }
        }
    }
}

fn match_args(
    arg_patterns: &[ArgPattern],
    call_args: &[Node],
    source: &[u8],
    exact: bool,
    captures: &mut HashMap<String, String>,
) -> bool {
    let has_ellipsis = arg_patterns.iter().any(|a| matches!(a, ArgPattern::Ellipsis | ArgPattern::EllipsisMetavar(_)));

    if !has_ellipsis {
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
                    if matches_node(call_args[i], source, pnode, captures).is_none() {
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
                        if matches_node(inner_node, source, pnode, captures).is_none() {
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
                        if matches_node(inner_node, source, pnode, captures).is_none() {
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

    if arg_patterns.len() == 1 && matches!(arg_patterns[0], ArgPattern::Ellipsis | ArgPattern::EllipsisMetavar(_)) {
        if let ArgPattern::EllipsisMetavar(name) = &arg_patterns[0] {
             let text: Vec<_> = call_args.iter().map(|n| node_text(*n, source)).collect();
             captures.insert(name.clone(), text.join(", "));
        }
        return true;
    }

    let non_ellipsis_count = arg_patterns.iter().filter(|a| !matches!(a, ArgPattern::Ellipsis | ArgPattern::EllipsisMetavar(_))).count();
    if call_args.len() < non_ellipsis_count {
        return false;
    }

    let non_ellipsis: Vec<&ArgPattern> = arg_patterns
        .iter()
        .filter(|a| !matches!(a, ArgPattern::Ellipsis | ArgPattern::EllipsisMetavar(_)))
        .collect();

    if non_ellipsis.is_empty() {
        return true;
    }

    'outer: for start in 0..=(call_args.len() - non_ellipsis.len()) {
        let mut temp_captures = captures.clone();
        for (j, pattern) in non_ellipsis.iter().enumerate() {
            let node = call_args[start + j];
            match pattern {
                ArgPattern::Pattern(pnode) => {
                    if matches_node(node, source, pnode, &mut temp_captures).is_none() {
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
                        if matches_node(inner_node, source, pnode, &mut temp_captures).is_none() {
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
                        if matches_node(inner_node, source, pnode, &mut temp_captures).is_none() {
                            continue 'outer;
                        }
                    } else {
                        continue 'outer;
                    }
                }
                _ => continue 'outer,
            }
        }
        // Capture prefix/suffix if metavars were used for ellipsis
        if let ArgPattern::EllipsisMetavar(name) = &arg_patterns[0] {
             let text: Vec<_> = call_args[..start].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        if let ArgPattern::EllipsisMetavar(name) = &arg_patterns[arg_patterns.len()-1] {
             let text: Vec<_> = call_args[start+non_ellipsis.len()..].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        *captures = temp_captures;
        return true;
    }
    false
}

fn param_has_default(param: Node, source: &[u8], dv: &PatternNode, captures: &mut HashMap<String, String>) -> bool {
    if param.kind() != "default_parameter" && param.kind() != "typed_default_parameter" {
        return false;
    }
    if let Some(value_node) = param.child_by_field_name("value") {
        matches_node(value_node, source, dv, captures).is_some()
    } else {
        false
    }
}

fn match_params(
    param_patterns: &[ParamPattern],
    params: &[Node],
    source: &[u8],
    captures: &mut HashMap<String, String>,
) -> bool {
    let has_ellipsis = param_patterns
        .iter()
        .any(|p| matches!(p, ParamPattern::Ellipsis | ParamPattern::EllipsisMetavar(_)));

    if !has_ellipsis {
        if params.len() != param_patterns.len() {
            return false;
        }
        for (i, pattern) in param_patterns.iter().enumerate() {
            match pattern {
                ParamPattern::Any => {}
                ParamPattern::Ellipsis | ParamPattern::EllipsisMetavar(_) => unreachable!(),
                ParamPattern::WithDefault(dv) => {
                    if !param_has_default(params[i], source, dv, captures) {
                        return false;
                    }
                }
            }
        }
        return true;
    }
    
    // Subsequence matching for params
    let non_ellipsis_count = param_patterns.iter().filter(|p| !matches!(p, ParamPattern::Ellipsis | ParamPattern::EllipsisMetavar(_))).count();
    if params.len() < non_ellipsis_count {
        return false;
    }
    
    let non_ellipsis: Vec<&ParamPattern> = param_patterns
        .iter()
        .filter(|p| !matches!(p, ParamPattern::Ellipsis | ParamPattern::EllipsisMetavar(_)))
        .collect();

    'outer: for start in 0..=(params.len() - non_ellipsis.len()) {
        let mut temp_captures = captures.clone();
        for (j, pattern) in non_ellipsis.iter().enumerate() {
            let param = params[start + j];
            match pattern {
                ParamPattern::Any => {}
                ParamPattern::WithDefault(dv) => {
                    if !param_has_default(param, source, dv, &mut temp_captures) {
                        continue 'outer;
                    }
                }
                _ => continue 'outer,
            }
        }
        // Capture prefix/suffix if metavars were used for ellipsis
        if let ParamPattern::EllipsisMetavar(name) = &param_patterns[0] {
             let text: Vec<_> = params[..start].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        if let ParamPattern::EllipsisMetavar(name) = &param_patterns[param_patterns.len()-1] {
             let text: Vec<_> = params[start+non_ellipsis.len()..].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        *captures = temp_captures;
        return true;
    }
    false
}

fn match_sequence(
    patterns: &[PatternNode],
    nodes: &[Node],
    source: &[u8],
    captures: &mut HashMap<String, String>,
) -> bool {
    let has_ellipsis = patterns.iter().any(|p| matches!(p, PatternNode::Ellipsis | PatternNode::EllipsisMetavar(_)));
    if has_ellipsis && patterns.len() == 1 {
        if let PatternNode::EllipsisMetavar(name) = &patterns[0] {
             let text: Vec<_> = nodes.iter().map(|n| node_text(*n, source)).collect();
             captures.insert(name.clone(), text.join(", "));
        }
        return true;
    }
    if !has_ellipsis {
        if nodes.len() != patterns.len() {
            return false;
        }
        for (i, p) in patterns.iter().enumerate() {
            if matches_node(nodes[i], source, p, captures).is_none() {
                return false;
            }
        }
        return true;
    }
    
    let non_ellipsis: Vec<&PatternNode> = patterns
        .iter()
        .filter(|p| !matches!(p, PatternNode::Ellipsis | PatternNode::EllipsisMetavar(_)))
        .collect();
    if non_ellipsis.is_empty() {
        return true;
    }
    if nodes.len() < non_ellipsis.len() {
        return false;
    }
    'outer: for start in 0..=(nodes.len() - non_ellipsis.len()) {
        let mut temp_captures = captures.clone();
        for (j, pnode) in non_ellipsis.iter().enumerate() {
            if matches_node(nodes[start + j], source, pnode, &mut temp_captures).is_none() {
                continue 'outer;
            }
        }
        // Capture prefix/suffix
        if let PatternNode::EllipsisMetavar(name) = &patterns[0] {
             let text: Vec<_> = nodes[..start].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        if let PatternNode::EllipsisMetavar(name) = &patterns[patterns.len()-1] {
             let text: Vec<_> = nodes[start+non_ellipsis.len()..].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        *captures = temp_captures;
        return true;
    }
    false
}

fn match_fstring(
    parts: &[FStringPart],
    node: Node,
    source: &[u8],
    captures: &mut HashMap<String, String>,
) -> bool {
    if node.kind() != "string" {
        return false;
    }
    let mut cursor = node.walk();
    let actual_parts: Vec<Node> = node
        .children(&mut cursor)
        .filter(|n| n.kind() == "string_content" || n.kind() == "interpolation")
        .collect();

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
                let mut c = actual.walk();
                let inner = actual.children(&mut c).find(|n| n.is_named());
                if let Some(inner_node) = inner {
                    if matches_node(inner_node, source, pnode, captures).is_none() {
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

fn match_generator(
    pattern: &ComprehensionGenerator,
    node: Node,
    source: &[u8],
    captures: &mut HashMap<String, String>,
) -> bool {
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
    if matches_node(target_node, source, &pattern.target, captures).is_none()
        || matches_node(iter_node, source, &pattern.iter, captures).is_none()
    {
        return false;
    }
    let mut cursor = node.walk();
    let if_nodes: Vec<Node> = node
        .children(&mut cursor)
        .filter(|n| n.kind() == "if_clause")
        .filter_map(|n| n.child_by_field_name("condition"))
        .collect();

    match_sequence(&pattern.ifs, &if_nodes, source, captures)
}

fn match_generators(
    patterns: &[ComprehensionGenerator],
    nodes: &[Node],
    source: &[u8],
    captures: &mut HashMap<String, String>,
) -> bool {
    if nodes.len() != patterns.len() {
        return false;
    }
    for (i, p) in patterns.iter().enumerate() {
        if !match_generator(p, nodes[i], source, captures) {
            return false;
        }
    }
    true
}

fn match_dict_elements(
    patterns: &[DictElementPattern],
    nodes: &[Node],
    source: &[u8],
    captures: &mut HashMap<String, String>,
) -> bool {
    let has_ellipsis = patterns.iter().any(|p| matches!(p, DictElementPattern::Ellipsis | DictElementPattern::EllipsisMetavar(_)));
    if !has_ellipsis {
        if nodes.len() != patterns.len() {
            return false;
        }
        for (i, p) in patterns.iter().enumerate() {
            if !match_dict_element(p, nodes[i], source, captures) {
                return false;
            }
        }
        return true;
    }
    // Subsequence matching for dict elements
    let non_ellipsis: Vec<&DictElementPattern> = patterns
        .iter()
        .filter(|p| !matches!(p, DictElementPattern::Ellipsis | DictElementPattern::EllipsisMetavar(_)))
        .collect();
    if non_ellipsis.is_empty() {
        return true;
    }
    if nodes.len() < non_ellipsis.len() {
        return false;
    }
    'outer: for start in 0..=(nodes.len() - non_ellipsis.len()) {
        let mut temp_captures = captures.clone();
        for (j, pnode) in non_ellipsis.iter().enumerate() {
            if !match_dict_element(pnode, nodes[start + j], source, &mut temp_captures) {
                continue 'outer;
            }
        }
        // Capture prefix/suffix
        if let DictElementPattern::EllipsisMetavar(name) = &patterns[0] {
             let text: Vec<_> = nodes[..start].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        if let DictElementPattern::EllipsisMetavar(name) = &patterns[patterns.len()-1] {
             let text: Vec<_> = nodes[start+non_ellipsis.len()..].iter().map(|n| node_text(*n, source)).collect();
             temp_captures.insert(name.clone(), text.join(", "));
        }
        *captures = temp_captures;
        return true;
    }
    false
}

fn match_dict_element(
    pattern: &DictElementPattern,
    node: Node,
    source: &[u8],
    captures: &mut HashMap<String, String>,
) -> bool {
    match pattern {
        DictElementPattern::Pair { key, value } => {
            if node.kind() != "pair" {
                return false;
            }
            let knode = match node.child_by_field_name("key") {
                Some(n) => n,
                None => return false,
            };
            let vnode = match node.child_by_field_name("value") {
                Some(n) => n,
                None => return false,
            };
            matches_node(knode, source, key, captures).is_some()
                && matches_node(vnode, source, value, captures).is_some()
        }
        DictElementPattern::Spread(value) => {
            if node.kind() != "dictionary_splat" {
                return false;
            }
            let mut cursor = node.walk();
            let inner = node.children(&mut cursor).find(|n| n.is_named());
            if let Some(inner_node) = inner {
                matches_node(inner_node, source, value, captures).is_some()
            } else {
                false
            }
        }
        DictElementPattern::Ellipsis | DictElementPattern::EllipsisMetavar(_) => true,
    }
}

// ---------------------------------------------------------------------------
// Tree walker with ancestor tracking
// ---------------------------------------------------------------------------

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

fn any_ancestor_matches(ancestors: &[Node], source: &[u8], pattern: &PatternNode) -> bool {
    let mut dummy_captures = HashMap::new();
    ancestors
        .iter()
        .any(|anc| matches_node(*anc, source, pattern, &mut dummy_captures).is_some())
}

// ---------------------------------------------------------------------------
// Per-file matching
// ---------------------------------------------------------------------------

fn find_pattern_in_tree(
    tree: &tree_sitter::Tree,
    source_bytes: &[u8],
    file_path: &str,
    pattern: &PatternNode,
    inside: Option<&PatternNode>,
    not_inside: Option<&PatternNode>,
) -> Vec<(String, usize, usize, usize, usize, String, HashMap<String, String>)> {
    let mut results = Vec::new();
    let mut ancestors: Vec<Node> = Vec::new();

    walk_with_ancestors(
        tree.root_node(),
        source_bytes,
        &mut ancestors,
        &mut |node, ancs| {
            let mut captures = HashMap::new();
            let matched_node = match matches_node(node, source_bytes, pattern, &mut captures) {
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
                captures,
            ));
        },
    );

    results
}

fn find_pattern_in_source(
    source: &str,
    file_path: &str,
    pattern: &PatternNode,
    inside: Option<&PatternNode>,
    not_inside: Option<&PatternNode>,
) -> Vec<(String, usize, usize, usize, usize, String, HashMap<String, String>)> {
    let path_buf = std::path::PathBuf::from(file_path);
    let ext = path_buf.extension().and_then(|e| e.to_str()).unwrap_or("");
    let tree = match crate::pattern::parse_by_extension(source, ext) {
        Some(t) => t,
        None => return vec![],
    };
    find_pattern_in_tree(&tree, source.as_bytes(), file_path, pattern, inside, not_inside)
}

#[pyfunction]
#[pyo3(signature = (file_contents, pattern_ir, inside_ir=None, not_inside_ir=None))]
pub fn find_pattern_in_files(
    py: Python,
    file_contents: Vec<(String, String)>,
    pattern_ir: Bound<'_, PyAny>,
    inside_ir: Option<Bound<'_, PyAny>>,
    not_inside_ir: Option<Bound<'_, PyAny>>,
) -> PyResult<Vec<(String, usize, usize, usize, usize, String, HashMap<String, String>)>> {
    let pattern = deserialize_pattern(&pattern_ir)?;
    let inside = inside_ir
        .as_ref()
        .map(|ir| deserialize_pattern(ir))
        .transpose()?;
    let not_inside = not_inside_ir
        .as_ref()
        .map(|ir| deserialize_pattern(ir))
        .transpose()?;

    py.allow_threads(|| {
        let results: Vec<Vec<(String, usize, usize, usize, usize, String, HashMap<String, String>)>> = file_contents
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

#[pyfunction]
pub fn find_multi_patterns_in_files(
    py: Python,
    file_contents: Vec<(String, String)>,
    patterns: Vec<(Bound<'_, PyAny>, Option<Bound<'_, PyAny>>)>,
) -> PyResult<Vec<(usize, String, usize, usize, usize, usize, String)>> {
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
                    let path_buf = std::path::PathBuf::from(path);
                    let ext = path_buf.extension().and_then(|e| e.to_str()).unwrap_or("");
                    let tree = match crate::pattern::parse_by_extension(source, ext) {
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

fn collect_call_args(arg_list: Node) -> Vec<Node> {
    let mut cursor = arg_list.walk();
    arg_list
        .children(&mut cursor)
        .filter(|n| n.is_named())
        .collect()
}

fn collect_params(params_node: Node) -> Vec<Node> {
    let mut cursor = params_node.walk();
    params_node
        .children(&mut cursor)
        .filter(|n| n.is_named())
        .collect()
}
