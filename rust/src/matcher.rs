//! Structural pattern IR and tree-sitter-based matcher for Python code.
//!
//! Implements a pattern IR compiled from Python dict IR and matched against
//! tree-sitter ASTs in parallel via rayon.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use tree_sitter::Node;
use std::collections::HashMap;
use crate::scope::LanguageConfig;

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
    /// `*args` — matches a star parameter (vararg). Optional capture name.
    Star(Option<String>),
    /// `**kwargs` — matches a double-star parameter (kwarg). Optional capture name.
    DoubleStar(Option<String>),
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
        name: Option<String>,
    },
    /// Dictionary literal: `{"a": 1, **extras}`.
    Dict(Vec<DictElementPattern>),
    /// Set literal: `{a, b, *c}`.
    Set(Vec<PatternNode>),
    /// Return statement: `return value`.
    Return {
        value: Option<Box<PatternNode>>,
    },
    /// Assert statement: `assert test, msg`.
    Assert {
        test: Box<PatternNode>,
        msg: Option<Box<PatternNode>>,
    },
    /// Raise statement: `raise exc`.
    Raise {
        exc: Option<Box<PatternNode>>,
    },
    /// Delete statement: `del target`.
    Delete {
        target: Box<PatternNode>,
    },
    /// Global statement: `global x, y`.
    Global {
        names: Vec<GlobalName>,
    },
    /// Nonlocal statement: `nonlocal x, y`.
    Nonlocal {
        names: Vec<GlobalName>,
    },
    /// Await expression: `await expr`.
    Await {
        value: Box<PatternNode>,
    },
    /// Ternary/conditional expression: `body if test else orelse`.
    IfExp {
        body: Box<PatternNode>,
        test: Box<PatternNode>,
        orelse: Box<PatternNode>,
    },
    /// Lambda expression: `lambda params: body`.
    Lambda {
        params: Vec<ParamPattern>,
        body: Box<PatternNode>,
    },
    /// Walrus/named expression: `target := value`.
    NamedExpr {
        target: Box<PatternNode>,
        value: Box<PatternNode>,
    },
    /// Import from: `from module import name`.
    ImportFrom {
        module: Option<NameOrMetavar>,
        names: Vec<ImportAlias>,
    },
    /// Import: `import module`.
    Import {
        names: Vec<ImportAlias>,
    },
    /// If statement header: `if condition:`.
    IfStmt {
        test: Box<PatternNode>,
    },
    /// While statement header: `while condition:`.
    WhileStmt {
        test: Box<PatternNode>,
    },
    /// For statement: `for target in iter:`.
    ForStmt {
        target: Box<PatternNode>,
        iter: Box<PatternNode>,
        is_async: bool,
    },
    /// With statement: `with context as var:`.
    WithStmt {
        context: Box<PatternNode>,
        var: Option<Box<PatternNode>>,
        is_async: bool,
    },
    /// Try statement: `try:`.
    TryStmt,
    /// Except handler: `except Type as name:`.
    ExceptHandler {
        exception_type: Option<Box<PatternNode>>,
        name: Option<Box<PatternNode>>,
    },
    /// Match any of these node kinds (used for constraint matching).
    NodeKindMatch(Vec<String>),
}

/// Name in global/nonlocal statement: either literal or metavar.
#[derive(Debug, Clone)]
pub enum GlobalName {
    Literal(String),
    Metavar(String),
}

/// A name that can be either a literal string or a metavar.
#[derive(Debug, Clone)]
pub enum NameOrMetavar {
    Literal(String),
    Metavar(String),
}

/// Import alias: `name as asname`.
#[derive(Debug, Clone)]
pub struct ImportAlias {
    pub name: NameOrMetavar,
    pub asname: Option<NameOrMetavar>,
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

fn deserialize_param_pattern(obj: &Bound<'_, PyAny>) -> PyResult<ParamPattern> {
    let item_d = obj.downcast::<PyDict>()?;
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
                Ok(ParamPattern::EllipsisMetavar(name))
            } else {
                Ok(ParamPattern::Ellipsis)
            }
        }
        "any" => Ok(ParamPattern::Any),
        "with_default" => {
            let dv_obj = item_d.get_item("default_value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>(
                    "WithDefault missing 'default_value'",
                )
            })?;
            let dv = deserialize_pattern(&dv_obj)?;
            Ok(ParamPattern::WithDefault(Box::new(dv)))
        }
        "name" => {
            // Exact name match — treat as Any for now since params match by position
            Ok(ParamPattern::Any)
        }
        "star" => {
            let name = if let Some(name_obj) = item_d.get_item("name")? {
                Some(name_obj.extract::<String>()?)
            } else {
                None
            };
            Ok(ParamPattern::Star(name))
        }
        "double_star" => {
            let name = if let Some(name_obj) = item_d.get_item("name")? {
                Some(name_obj.extract::<String>()?)
            } else {
                None
            };
            Ok(ParamPattern::DoubleStar(name))
        }
        _ => Ok(ParamPattern::Any),
    }
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

        "none_literal" => Ok(PatternNode::NoneLiteral),

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
                if part_type == "text" || part_type == "fstring_text" {
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
            let name: Option<String> = if let Some(n) = d.get_item("name")? {
                if n.is_none() { None } else { Some(n.extract()?) }
            } else {
                None
            };
            Ok(PatternNode::TypeConstraint { kind, name })
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

        "return" => {
            let value = if let Some(val_obj) = d.get_item("value")? {
                if val_obj.is_none() { None } else { Some(Box::new(deserialize_pattern(&val_obj)?)) }
            } else {
                None
            };
            Ok(PatternNode::Return { value })
        }

        "assert" => {
            let test_obj = d.get_item("test")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Assert missing 'test'")
            })?;
            let test = deserialize_pattern(&test_obj)?;
            let msg = if let Some(msg_obj) = d.get_item("msg")? {
                if msg_obj.is_none() { None } else { Some(Box::new(deserialize_pattern(&msg_obj)?)) }
            } else {
                None
            };
            Ok(PatternNode::Assert { test: Box::new(test), msg })
        }

        "raise" => {
            let exc = if let Some(exc_obj) = d.get_item("exc")? {
                if exc_obj.is_none() { None } else { Some(Box::new(deserialize_pattern(&exc_obj)?)) }
            } else {
                None
            };
            Ok(PatternNode::Raise { exc })
        }

        "delete" => {
            let target_obj = d.get_item("target")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Delete missing 'target'")
            })?;
            Ok(PatternNode::Delete { target: Box::new(deserialize_pattern(&target_obj)?) })
        }

        "global" => {
            let names_obj = d.get_item("names")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Global missing 'names'")
            })?;
            let names_list = names_obj.downcast::<PyList>()?;
            let mut names = Vec::new();
            for item in names_list.iter() {
                if let Ok(s) = item.extract::<String>() {
                    names.push(GlobalName::Literal(s));
                } else if let Ok(item_d) = item.downcast::<PyDict>() {
                    let t: String = item_d.get_item("type")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("missing type")
                    })?.extract()?;
                    if t == "metavar" {
                        let name: String = item_d.get_item("name")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>("missing name")
                        })?.extract()?;
                        names.push(GlobalName::Metavar(name));
                    }
                }
            }
            Ok(PatternNode::Global { names })
        }

        "nonlocal" => {
            let names_obj = d.get_item("names")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Nonlocal missing 'names'")
            })?;
            let names_list = names_obj.downcast::<PyList>()?;
            let mut names = Vec::new();
            for item in names_list.iter() {
                if let Ok(s) = item.extract::<String>() {
                    names.push(GlobalName::Literal(s));
                } else if let Ok(item_d) = item.downcast::<PyDict>() {
                    let t: String = item_d.get_item("type")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("missing type")
                    })?.extract()?;
                    if t == "metavar" {
                        let name: String = item_d.get_item("name")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>("missing name")
                        })?.extract()?;
                        names.push(GlobalName::Metavar(name));
                    }
                }
            }
            Ok(PatternNode::Nonlocal { names })
        }

        "await" => {
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Await missing 'value'")
            })?;
            Ok(PatternNode::Await { value: Box::new(deserialize_pattern(&value_obj)?) })
        }

        "ifexp" => {
            let body_obj = d.get_item("body")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("IfExp missing 'body'")
            })?;
            let test_obj = d.get_item("test")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("IfExp missing 'test'")
            })?;
            let orelse_obj = d.get_item("orelse")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("IfExp missing 'orelse'")
            })?;
            Ok(PatternNode::IfExp {
                body: Box::new(deserialize_pattern(&body_obj)?),
                test: Box::new(deserialize_pattern(&test_obj)?),
                orelse: Box::new(deserialize_pattern(&orelse_obj)?),
            })
        }

        "lambda" => {
            let params_obj = d.get_item("params")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Lambda missing 'params'")
            })?;
            let params_list = params_obj.downcast::<PyList>()?;
            let mut params = Vec::new();
            for item in params_list.iter() {
                params.push(deserialize_param_pattern(&item)?);
            }
            let body_obj = d.get_item("body")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Lambda missing 'body'")
            })?;
            Ok(PatternNode::Lambda {
                params,
                body: Box::new(deserialize_pattern(&body_obj)?),
            })
        }

        "named_expr" => {
            let target_obj = d.get_item("target")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("NamedExpr missing 'target'")
            })?;
            let value_obj = d.get_item("value")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("NamedExpr missing 'value'")
            })?;
            Ok(PatternNode::NamedExpr {
                target: Box::new(deserialize_pattern(&target_obj)?),
                value: Box::new(deserialize_pattern(&value_obj)?),
            })
        }

        "import_from" => {
            let module: Option<NameOrMetavar> = if let Some(mod_obj) = d.get_item("module")? {
                if mod_obj.is_none() {
                    None
                } else if let Ok(s) = mod_obj.extract::<String>() {
                    Some(NameOrMetavar::Literal(s))
                } else if let Ok(mod_d) = mod_obj.downcast::<PyDict>() {
                    let t: String = mod_d.get_item("type")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("missing type")
                    })?.extract()?;
                    if t == "metavar" {
                        let n: String = mod_d.get_item("name")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>("missing name")
                        })?.extract()?;
                        Some(NameOrMetavar::Metavar(n))
                    } else {
                        None
                    }
                } else {
                    None
                }
            } else {
                None
            };
            let names_obj = d.get_item("names")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("ImportFrom missing 'names'")
            })?;
            let names_list = names_obj.downcast::<PyList>()?;
            let mut names = Vec::new();
            for item in names_list.iter() {
                let item_d = item.downcast::<PyDict>()?;
                let name_obj = item_d.get_item("name")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("ImportAlias missing 'name'")
                })?;
                let name = if let Ok(s) = name_obj.extract::<String>() {
                    NameOrMetavar::Literal(s)
                } else if let Ok(n_d) = name_obj.downcast::<PyDict>() {
                    let t: String = n_d.get_item("type")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("missing type")
                    })?.extract()?;
                    if t == "metavar" {
                        let n: String = n_d.get_item("name")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>("missing name")
                        })?.extract()?;
                        NameOrMetavar::Metavar(n)
                    } else {
                        NameOrMetavar::Literal(name_obj.str()?.to_string())
                    }
                } else {
                    NameOrMetavar::Literal(name_obj.str()?.to_string())
                };
                let asname: Option<NameOrMetavar> = if let Some(a) = item_d.get_item("asname")? {
                    if a.is_none() {
                        None
                    } else if let Ok(s) = a.extract::<String>() {
                        Some(NameOrMetavar::Literal(s))
                    } else if let Ok(a_d) = a.downcast::<PyDict>() {
                        let t: String = a_d.get_item("type")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>("missing type")
                        })?.extract()?;
                        if t == "metavar" {
                            let n: String = a_d.get_item("name")?.ok_or_else(|| {
                                PyErr::new::<pyo3::exceptions::PyValueError, _>("missing name")
                            })?.extract()?;
                            Some(NameOrMetavar::Metavar(n))
                        } else {
                            None
                        }
                    } else {
                        None
                    }
                } else {
                    None
                };
                names.push(ImportAlias { name, asname });
            }
            Ok(PatternNode::ImportFrom { module, names })
        }

        "import" => {
            let names_obj = d.get_item("names")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Import missing 'names'")
            })?;
            let names_list = names_obj.downcast::<PyList>()?;
            let mut names = Vec::new();
            for item in names_list.iter() {
                let item_d = item.downcast::<PyDict>()?;
                let name_obj = item_d.get_item("name")?.ok_or_else(|| {
                    PyErr::new::<pyo3::exceptions::PyValueError, _>("ImportAlias missing 'name'")
                })?;
                let name = if let Ok(s) = name_obj.extract::<String>() {
                    NameOrMetavar::Literal(s)
                } else if let Ok(n_d) = name_obj.downcast::<PyDict>() {
                    let t: String = n_d.get_item("type")?.ok_or_else(|| {
                        PyErr::new::<pyo3::exceptions::PyValueError, _>("missing type")
                    })?.extract()?;
                    if t == "metavar" {
                        let n: String = n_d.get_item("name")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>("missing name")
                        })?.extract()?;
                        NameOrMetavar::Metavar(n)
                    } else {
                        NameOrMetavar::Literal(name_obj.str()?.to_string())
                    }
                } else {
                    NameOrMetavar::Literal(name_obj.str()?.to_string())
                };
                let asname: Option<NameOrMetavar> = if let Some(a) = item_d.get_item("asname")? {
                    if a.is_none() {
                        None
                    } else if let Ok(s) = a.extract::<String>() {
                        Some(NameOrMetavar::Literal(s))
                    } else if let Ok(a_d) = a.downcast::<PyDict>() {
                        let t: String = a_d.get_item("type")?.ok_or_else(|| {
                            PyErr::new::<pyo3::exceptions::PyValueError, _>("missing type")
                        })?.extract()?;
                        if t == "metavar" {
                            let n: String = a_d.get_item("name")?.ok_or_else(|| {
                                PyErr::new::<pyo3::exceptions::PyValueError, _>("missing name")
                            })?.extract()?;
                            Some(NameOrMetavar::Metavar(n))
                        } else {
                            None
                        }
                    } else {
                        None
                    }
                } else {
                    None
                };
                names.push(ImportAlias { name, asname });
            }
            Ok(PatternNode::Import { names })
        }

        "if_stmt" => {
            let test_obj = d.get_item("test")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("IfStmt missing 'test'")
            })?;
            Ok(PatternNode::IfStmt { test: Box::new(deserialize_pattern(&test_obj)?) })
        }

        "while_stmt" => {
            let test_obj = d.get_item("test")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("WhileStmt missing 'test'")
            })?;
            Ok(PatternNode::WhileStmt { test: Box::new(deserialize_pattern(&test_obj)?) })
        }

        "for_stmt" => {
            let target_obj = d.get_item("target")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("ForStmt missing 'target'")
            })?;
            let iter_obj = d.get_item("iter")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("ForStmt missing 'iter'")
            })?;
            let is_async = if let Some(v) = d.get_item("is_async")? { v.extract::<bool>().unwrap_or(false) } else { false };
            Ok(PatternNode::ForStmt {
                target: Box::new(deserialize_pattern(&target_obj)?),
                iter: Box::new(deserialize_pattern(&iter_obj)?),
                is_async,
            })
        }

        "with_stmt" => {
            let ctx_obj = d.get_item("context")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("WithStmt missing 'context'")
            })?;
            let var = if let Some(v_obj) = d.get_item("var")? {
                if v_obj.is_none() { None } else { Some(Box::new(deserialize_pattern(&v_obj)?)) }
            } else {
                None
            };
            let is_async = if let Some(v) = d.get_item("is_async")? { v.extract::<bool>().unwrap_or(false) } else { false };
            Ok(PatternNode::WithStmt { context: Box::new(deserialize_pattern(&ctx_obj)?), var, is_async })
        }

        "try_stmt" => Ok(PatternNode::TryStmt),

        "except_handler" => {
            let exc_type = if let Some(t_obj) = d.get_item("exception_type")? {
                if t_obj.is_none() { None } else { Some(Box::new(deserialize_pattern(&t_obj)?)) }
            } else {
                None
            };
            let name = if let Some(n_obj) = d.get_item("name")? {
                if n_obj.is_none() { None } else { Some(Box::new(deserialize_pattern(&n_obj)?)) }
            } else {
                None
            };
            Ok(PatternNode::ExceptHandler { exception_type: exc_type, name })
        }

        "node_kind_match" => {
            let kinds_obj = d.get_item("kinds")?.ok_or_else(|| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("NodeKindMatch missing 'kinds'")
            })?;
            let kinds_list: &Bound<'_, PyList> = kinds_obj.downcast::<PyList>().map_err(|_| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("NodeKindMatch 'kinds' must be a list")
            })?;
            let kinds: Vec<String> = kinds_list
                .iter()
                .map(|item| item.extract::<String>())
                .collect::<PyResult<Vec<String>>>()?;
            Ok(PatternNode::NodeKindMatch(kinds))
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
/// like expression_list for tuples).
fn matches_node<'a>(
    node: Node<'a>,
    source: &[u8],
    pattern: &PatternNode,
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
) -> Option<Node<'a>> {
    // Unwrap parenthesized_expression for specific pattern types that look "through" parens.
    // For AnyExpr/Metavar/Ellipsis, we match the outer parenthesized node directly.
    if node.kind() == config.pattern_matching.parenthesized_expression.as_str() {
        match pattern {
            PatternNode::AnyExpr | PatternNode::Metavar(_)
            | PatternNode::Ellipsis | PatternNode::EllipsisMetavar(_) => {}
            _ => {
                // Try matching the inner expression
                let mut cursor = node.walk();
                for child in node.children(&mut cursor) {
                    if child.is_named() {
                        if let Some(result) = matches_node(child, source, pattern, captures, config) {
                            return Some(result);
                        }
                    }
                }
                return None;
            }
        }
    }
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
            if node.kind() == config.pattern_matching.identifier && node_text(node, source) == s.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::NameGlob(g) => {
            if node.kind() == config.pattern_matching.identifier && glob_matches(g, node_text(node, source)) {
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
            if node.kind() != config.pattern_matching.call {
                return None;
            }
            let func_node = match node.child_by_field_name(&config.pattern_matching.func_field) {
                Some(n) => n,
                None => return None,
            };
            if matches_node(func_node, source, func, captures, config).is_none() {
                return None;
            }
            match node.child_by_field_name(&config.pattern_matching.args_field) {
                Some(arg_list) => {
                    // In tree-sitter-python, `any(x for x in items)` uses
                    // a generator_expression directly as the arguments field
                    // instead of an argument_list node.
                    if arg_list.kind() == "generator_expression" {
                        // Treat the genexp as a single argument
                        let call_args = vec![arg_list];
                        if match_args(args, &call_args, source, *exact_args, captures, config) {
                            Some(node)
                        } else {
                            None
                        }
                    } else {
                        let call_args = collect_call_args(arg_list);
                        if match_args(args, &call_args, source, *exact_args, captures, config) {
                            Some(node)
                        } else {
                            None
                        }
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
            if node.kind() != config.pattern_matching.attribute {
                return None;
            }
            let obj_node = match node.child_by_field_name(&config.pattern_matching.object_field) {
                Some(n) => n,
                None => return None,
            };
            let attr_node = match node.child_by_field_name(&config.pattern_matching.attr_field) {
                Some(n) => n,
                None => return None,
            };
            if matches_node(obj_node, source, value, captures, config).is_some() && node_text(attr_node, source) == attr.as_str() {
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
            let (actual_decs, func_node) = if node.kind() == config.pattern_matching.decorated_def {
                let mut cursor = node.walk();
                let decs: Vec<Node> = node
                    .children(&mut cursor)
                    .filter(|n| n.kind() == config.symbols.decorator_node.as_deref().unwrap_or("decorator"))
                    .map(|n| n.named_child(0).unwrap_or(n))
                    .collect();
                let def = node.child_by_field_name(config.symbols.definition_field.as_deref().unwrap_or("definition")).unwrap_or(node);
                (decs, def)
            } else {
                (Vec::new(), node)
            };

            if func_node.kind() != config.pattern_matching.function_def {
                return None;
            }

            if let Some(expected_async) = is_async {
                let mut cursor = func_node.walk();
                let actual_async = func_node.children(&mut cursor).any(|n| n.kind() == config.symbols.async_keyword.as_deref().unwrap_or("async"));
                if actual_async != *expected_async {
                    return None;
                }
            }

            if !match_sequence(decorators, &actual_decs, source, captures, config) {
                return None;
            }

            let name_node = match func_node.child_by_field_name(config.symbols.name_field()) {
                Some(n) => n,
                None => return None,
            };
            if matches_node(name_node, source, name, captures, config).is_none() {
                return None;
            }

            let params_node = match func_node.child_by_field_name(config.symbols.parameters_field()) {
                Some(n) => n,
                None => return None,
            };
            let actual_params = collect_params(params_node);
            if match_params(params, &actual_params, source, captures, config) {
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
            let (actual_decs, class_node) = if node.kind() == config.pattern_matching.decorated_def {
                let mut cursor = node.walk();
                let decs: Vec<Node> = node
                    .children(&mut cursor)
                    .filter(|n| n.kind() == config.symbols.decorator_node.as_deref().unwrap_or("decorator"))
                    .map(|n| n.named_child(0).unwrap_or(n))
                    .collect();
                let def = node.child_by_field_name(config.symbols.definition_field.as_deref().unwrap_or("definition")).unwrap_or(node);
                (decs, def)
            } else {
                (Vec::new(), node)
            };

            if class_node.kind() != config.pattern_matching.class_def {
                return None;
            }

            if !match_sequence(decorators, &actual_decs, source, captures, config) {
                return None;
            }

            let name_node = match class_node.child_by_field_name(config.symbols.name_field()) {
                Some(n) => n,
                None => return None,
            };
            if matches_node(name_node, source, name, captures, config).is_none() {
                return None;
            }

            if bases.is_empty() {
                return Some(node);
            }
            let has_ellipsis = bases.iter().any(|b| matches!(b, PatternNode::Ellipsis | PatternNode::EllipsisMetavar(_)));
            if has_ellipsis && bases.len() == 1 {
                return Some(node);
            }

            let superclasses = match class_node.child_by_field_name(config.symbols.superclasses_field.as_deref().unwrap_or("superclasses")) {
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

            if match_sequence(bases, &base_nodes, source, captures, config) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Integer(v) => {
            if node.kind() == config.pattern_matching.integer && node_text(node, source) == v.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Float(v) => {
            if node.kind() == config.pattern_matching.float && node_text(node, source) == v.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::StringLiteral(v) => {
            if node.kind() != config.pattern_matching.string && node.kind() != "concatenated_string" {
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
            if node.kind() == config.pattern_matching.list && node.named_child_count() == 0 {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::List(elems) => {
            if node.kind() != config.pattern_matching.list {
                return None;
            }
            let list_elems: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            if match_sequence(elems, &list_elems, source, captures, config) {
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
                if matches_node(children[0], source, value, captures, config).is_none() {
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

                if match_sequence(slices, &actual_slices, source, captures, config) {
                    Some(node)
                } else {
                    None
                }
            } else if node.kind() == config.pattern_matching.subscript {
                let value_node = match node.child_by_field_name(&config.pattern_matching.value_field) {
                    Some(n) => n,
                    None => return None,
                };
                if matches_node(value_node, source, value, captures, config).is_none() {
                    return None;
                }
                let mut cursor = node.walk();
                let actual_slices: Vec<Node> = node.children_by_field_name(&config.pattern_matching.subscript, &mut cursor)
                    .collect();
                
                if match_sequence(slices, &actual_slices, source, captures, config) {
                    Some(node)
                } else {
                    None
                }
            } else {
                None
            }
        }

        PatternNode::Tuple(elems) => {
            if node.kind() != config.pattern_matching.tuple && node.kind() != "expression_list" && node.kind() != "pattern_list" && node.kind() != "tuple_pattern" {
                return None;
            }
            let mut tuple_elems: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            
            let mut pos_node = node;
            if node.kind() == config.pattern_matching.tuple && tuple_elems.len() == 1 && tuple_elems[0].kind() == "expression_list" {
                pos_node = tuple_elems[0];
                let mut cursor = pos_node.walk();
                tuple_elems = pos_node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect();
            }
            
            if match_sequence(elems, &tuple_elems, source, captures, config) {
                Some(pos_node)
            } else {
                None
            }
        }

        PatternNode::BinaryOp { left, op, right } => {
            if node.kind() != config.pattern_matching.binary_operator && node.kind() != config.pattern_matching.boolean_operator {
                return None;
            }
            let left_node = match node.child_by_field_name(&config.pattern_matching.left_field) {
                Some(n) => n,
                None => return None,
            };
            let right_node = match node.child_by_field_name(&config.pattern_matching.right_field) {
                Some(n) => n,
                None => return None,
            };
            let op_node = match node.child_by_field_name(&config.pattern_matching.operator_field) {
                Some(n) => n,
                None => return None,
            };
            if node_text(op_node, source) == op.as_str()
                && matches_node(left_node, source, left, captures, config).is_some()
                && matches_node(right_node, source, right, captures, config).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::KeywordArg { key, value } => {
            if node.kind() != config.pattern_matching.keyword_argument {
                return None;
            }
            let name_node = match node.child_by_field_name(config.symbols.name_field()) {
                Some(n) => n,
                None => return None,
            };
            let value_node = match node.child_by_field_name(&config.pattern_matching.value_field) {
                Some(n) => n,
                None => return None,
            };
            if node_text(name_node, source) == key.as_str()
                && matches_node(value_node, source, value, captures, config).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::AugAssign { target, op, value } => {
            if node.kind() != config.pattern_matching.augmented_assignment {
                return None;
            }
            let left_node = match node.child_by_field_name(&config.pattern_matching.left_field) {
                Some(n) => n,
                None => return None,
            };
            let op_node = match node.child_by_field_name(&config.pattern_matching.operator_field) {
                Some(n) => n,
                None => return None,
            };
            let right_node = match node.child_by_field_name(&config.pattern_matching.right_field) {
                Some(n) => n,
                None => return None,
            };
            if node_text(op_node, source) == op.as_str()
                && matches_node(left_node, source, target, captures, config).is_some()
                && matches_node(right_node, source, value, captures, config).is_some() {
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
            if node.kind() != config.pattern_matching.annotated_assignment {
                return None;
            }
            let left_node = match node.child_by_field_name(&config.pattern_matching.left_field) {
                Some(n) => n,
                None => return None,
            };
            let type_node = match node.child_by_field_name(&config.pattern_matching.annotation_field) {
                Some(n) => n,
                None => return None,
            };
            if matches_node(left_node, source, target, captures, config).is_none()
                || matches_node(type_node, source, annotation, captures, config).is_none()
            {
                return None;
            }
            match (value, node.child_by_field_name(&config.pattern_matching.value_field)) {
                (Some(p), Some(n)) => {
                    if matches_node(n, source, p, captures, config).is_some() {
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
            if node.kind() != config.pattern_matching.comparison_operator {
                return None;
            }
            let mut cursor = node.walk();
            let children: Vec<Node> = node.children(&mut cursor)
                .filter(|n| n.is_named() || !n.is_extra())
                .collect();
            if children.is_empty() {
                return None;
            }
            if matches_node(children[0], source, left, captures, config).is_none() {
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
                        if matches_node(child, source, expected_comp, captures, config).is_some() {
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
            if node.kind() != config.pattern_matching.unary_operator && node.kind() != config.pattern_matching.not_operator.as_str() {
                return None;
            }
            if node.kind() == config.pattern_matching.not_operator.as_str() {
                if op != "not" {
                    return None;
                }
                let arg_node = match node.child_by_field_name("argument") {
                    Some(n) => n,
                    None => return None,
                };
                if matches_node(arg_node, source, operand, captures, config).is_some() {
                    return Some(node);
                } else {
                    return None;
                }
            }
            let op_node = match node.child_by_field_name(&config.pattern_matching.operator_field) {
                Some(n) => n,
                None => return None,
            };
            let arg_node = match node.child_by_field_name("argument") {
                Some(n) => n,
                None => return None,
            };
            if node_text(op_node, source) == op.as_str()
                && matches_node(arg_node, source, operand, captures, config).is_some() {
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
            if matches_node(body_node, source, elt, captures, config).is_none() {
                return None;
            }
            // In tree-sitter-python, for_in_clause and if_clause are
            // siblings under the comprehension node.  Group each
            // for_in_clause with the if_clauses that follow it (up to
            // the next for_in_clause).
            let mut cursor = node.walk();
            let clause_children: Vec<Node> = node
                .children(&mut cursor)
                .filter(|n| n.kind() == config.pattern_matching.for_in_clause.as_str() || n.kind() == "if_clause")
                .collect();
            let mut gen_groups: Vec<(Node, Vec<Node>)> = Vec::new();
            for child in &clause_children {
                if child.kind() == config.pattern_matching.for_in_clause.as_str() {
                    gen_groups.push((*child, Vec::new()));
                } else if child.kind() == "if_clause" {
                    if let Some(last) = gen_groups.last_mut() {
                        last.1.push(*child);
                    }
                }
            }
            if match_generators_with_ifs(generators, &gen_groups, source, captures, config) {
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
            if node.kind() != config.pattern_matching.dict_comprehension.as_str() {
                return None;
            }
            // dict comprehension body is a "pair" node with key/value fields
            let body_node = match node.child_by_field_name("body") {
                Some(n) => n,
                None => return None,
            };
            let key_node = match body_node.child_by_field_name("key") {
                Some(n) => n,
                None => return None,
            };
            let value_node = match body_node.child_by_field_name("value") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(key_node, source, key, captures, config).is_none() || matches_node(value_node, source, value, captures, config).is_none() {
                return None;
            }
            let mut cursor = node.walk();
            let clause_children: Vec<Node> = node
                .children(&mut cursor)
                .filter(|n| n.kind() == config.pattern_matching.for_in_clause.as_str() || n.kind() == "if_clause")
                .collect();
            let mut gen_groups: Vec<(Node, Vec<Node>)> = Vec::new();
            for child in &clause_children {
                if child.kind() == config.pattern_matching.for_in_clause.as_str() {
                    gen_groups.push((*child, Vec::new()));
                } else if child.kind() == "if_clause" {
                    if let Some(last) = gen_groups.last_mut() {
                        last.1.push(*child);
                    }
                }
            }
            if match_generators_with_ifs(generators, &gen_groups, source, captures, config) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::FString { parts } => {
            if match_fstring(parts, node, source, captures, config) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::TypeConstraint { kind, name } => {
            let (negated, inner_kind) = if kind.starts_with('!') {
                (true, &kind[1..])
            } else {
                (false, kind.as_str())
            };
            let type_match = match inner_kind {
                "int" => node.kind() == config.pattern_matching.integer.as_str(),
                "str" => node.kind() == config.pattern_matching.string.as_str() || node.kind() == "concatenated_string",
                "call" => node.kind() == config.pattern_matching.call.as_str(),
                "float" => node.kind() == config.pattern_matching.float.as_str(),
                "identifier" => node.kind() == config.pattern_matching.identifier.as_str(),
                "attr" => node.kind() == config.pattern_matching.attribute.as_str(),
                "stmt" => config.pattern_matching.statement_nodes.iter().any(|k| k == node.kind()),
                _ => false,
            };
            let matched = if negated { !type_match } else { type_match };
            if matched {
                if let Some(n) = name {
                    captures.insert(n.clone(), node_text(node, source).to_string());
                }
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Assign { target, value } => {
            if node.kind() != config.pattern_matching.assignment.as_str() {
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
            if matches_node(left_node, source, target, captures, config).is_some() && matches_node(right_node, source, value, captures, config).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::NoneLiteral => {
            if node.kind() == config.pattern_matching.none_literal.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::BoolLiteral(v) => {
            let matched = if *v {
                node.kind() == config.pattern_matching.true_literal.as_str()
            } else {
                node.kind() == config.pattern_matching.false_literal.as_str()
            };
            if matched { Some(node) } else { None }
        }

        PatternNode::Dict(elements) => {
            if node.kind() != config.pattern_matching.dict {
                return None;
            }
            let dict_elements: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            if match_dict_elements(elements, &dict_elements, source, captures, config) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Set(elems) => {
            if node.kind() != config.pattern_matching.set {
                return None;
            }
            let set_elems: Vec<Node> = {
                let mut cursor = node.walk();
                node.children(&mut cursor)
                    .filter(|n| n.is_named())
                    .collect()
            };
            if match_sequence(elems, &set_elems, source, captures, config) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::Return { value } => {
            if node.kind() != config.pattern_matching.return_stmt {
                return None;
            }
            match value {
                Some(val_pattern) => {
                    // Find the value child using config.value_field, or fallback to first named child
                    let val_node = node.child_by_field_name(&config.pattern_matching.value_field)
                        .or_else(|| node.named_child(0));
                    match val_node {
                        Some(vn) => {
                            if matches_node(vn, source, val_pattern, captures, config).is_some() {
                                Some(node)
                            } else {
                                None
                            }
                        }
                        None => None,
                    }
                }
                None => Some(node),
            }
        }

        PatternNode::Assert { test, msg } => {
            if node.kind() != config.pattern_matching.assert_stmt {
                return None;
            }
            // Python assert: test is usually child at index 0 or field 'test'
            let test_node = node.child_by_field_name("test")
                .or_else(|| node.named_child(0));
            
            if let Some(tn) = test_node {
                if matches_node(tn, source, test, captures, config).is_none() {
                    return None;
                }
            } else {
                return None;
            }

            if let Some(msg_pattern) = msg {
                let msg_node = node.child_by_field_name("message")
                    .or_else(|| node.named_child(1));
                if let Some(mn) = msg_node {
                    if matches_node(mn, source, msg_pattern, captures, config).is_none() {
                        return None;
                    }
                } else {
                    return None;
                }
            }
            Some(node)
        }

        PatternNode::Raise { exc } => {
            if node.kind() != config.pattern_matching.raise_stmt {
                return None;
            }
            match exc {
                Some(exc_pattern) => {
                    let exc_node = node.child_by_field_name("exc")
                        .or_else(|| node.named_child(0));
                    match exc_node {
                        Some(en) => {
                            if matches_node(en, source, exc_pattern, captures, config).is_some() {
                                Some(node)
                            } else {
                                None
                            }
                        }
                        None => None,
                    }
                }
                None => Some(node),
            }
        }

        PatternNode::Delete { target } => {
            if node.kind() != config.pattern_matching.delete_stmt.as_str() {
                return None;
            }
            let mut cursor = node.walk();
            let target_node = node.children(&mut cursor)
                .find(|n| n.is_named());
            match target_node {
                Some(tn) => {
                    if matches_node(tn, source, target, captures, config).is_some() {
                        Some(node)
                    } else {
                        None
                    }
                }
                None => None,
            }
        }

        PatternNode::Global { names } => {
            if node.kind() != config.pattern_matching.global_stmt.as_str() {
                return None;
            }
            let mut cursor = node.walk();
            let named_children: Vec<Node> = node.children(&mut cursor)
                .filter(|n| n.is_named())
                .collect();
            if named_children.len() != names.len() {
                return None;
            }
            for (child, expected) in named_children.iter().zip(names.iter()) {
                match expected {
                    GlobalName::Literal(name) => {
                        if node_text(*child, source) != name.as_str() {
                            return None;
                        }
                    }
                    GlobalName::Metavar(name) => {
                        captures.insert(name.clone(), node_text(*child, source).to_string());
                    }
                }
            }
            Some(node)
        }

        PatternNode::Nonlocal { names } => {
            if node.kind() != config.pattern_matching.nonlocal_stmt.as_str() {
                return None;
            }
            let mut cursor = node.walk();
            let named_children: Vec<Node> = node.children(&mut cursor)
                .filter(|n| n.is_named())
                .collect();
            if named_children.len() != names.len() {
                return None;
            }
            for (child, expected) in named_children.iter().zip(names.iter()) {
                match expected {
                    GlobalName::Literal(name) => {
                        if node_text(*child, source) != name.as_str() {
                            return None;
                        }
                    }
                    GlobalName::Metavar(name) => {
                        captures.insert(name.clone(), node_text(*child, source).to_string());
                    }
                }
            }
            Some(node)
        }

        PatternNode::Await { value } => {
            if node.kind() != config.pattern_matching.await_expr.as_str() {
                return None;
            }
            let mut cursor = node.walk();
            let val_node = node.children(&mut cursor)
                .find(|n| n.is_named());
            match val_node {
                Some(vn) => {
                    if matches_node(vn, source, value, captures, config).is_some() {
                        Some(node)
                    } else {
                        None
                    }
                }
                None => None,
            }
        }

        PatternNode::IfExp { body, test, orelse } => {
            if node.kind() != config.pattern_matching.conditional_expression.as_str() {
                return None;
            }
            // tree-sitter: conditional_expression has no named fields
            // Children (named): body_expr, test_expr, orelse_expr
            let mut cursor = node.walk();
            let named_children: Vec<Node> = node.children(&mut cursor)
                .filter(|n| n.is_named())
                .collect();
            if named_children.len() < 3 {
                return None;
            }
            if matches_node(named_children[0], source, body, captures, config).is_none() {
                return None;
            }
            if matches_node(named_children[1], source, test, captures, config).is_none() {
                return None;
            }
            if matches_node(named_children[2], source, orelse, captures, config).is_none() {
                return None;
            }
            Some(node)
        }

        PatternNode::Lambda { params, body } => {
            if node.kind() != config.pattern_matching.lambda.as_str() {
                return None;
            }
            // Match lambda body
            let body_node = match node.child_by_field_name("body") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(body_node, source, body, captures, config).is_none() {
                return None;
            }
            // Match params
            if params.is_empty() {
                // Pattern has no params — lambda must also have no params
                if node.child_by_field_name("parameters").is_some() {
                    return None;
                }
            } else {
                let params_node = match node.child_by_field_name("parameters") {
                    Some(n) => n,
                    None => {
                        // No params in lambda but pattern expects some
                        return None;
                    }
                };
                let param_children = collect_params(params_node);
                if !match_params(params, &param_children, source, captures, config) {
                    return None;
                }
            }
            Some(node)
        }

        PatternNode::NamedExpr { target, value } => {
            if node.kind() != config.pattern_matching.named_expr.as_str() {
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
            if matches_node(name_node, source, target, captures, config).is_none() {
                return None;
            }
            if matches_node(value_node, source, value, captures, config).is_none() {
                return None;
            }
            Some(node)
        }

        PatternNode::ImportFrom { module, names } => {
            if node.kind() != config.pattern_matching.import_from_stmt.as_str() {
                return None;
            }
            // Check module name if specified
            if let Some(mod_ref) = module {
                let mod_node = match node.child_by_field_name("module_name") {
                    Some(n) => n,
                    None => return None,
                };
                let actual_mod = node_text(mod_node, source);
                match mod_ref {
                    NameOrMetavar::Literal(expected) => {
                        if actual_mod != expected.as_str() {
                            return None;
                        }
                    }
                    NameOrMetavar::Metavar(name) => {
                        captures.insert(name.clone(), actual_mod.to_string());
                    }
                }
            }
            // Match import names using the "name" field
            let mut cursor = node.walk();
            let import_names: Vec<Node> = node.children_by_field_name("name", &mut cursor).collect();

            if names.len() > import_names.len() {
                return None;
            }
            for (alias, imp_node) in names.iter().zip(import_names.iter()) {
                if !match_import_alias(alias, *imp_node, source, captures, config) {
                    return None;
                }
            }
            Some(node)
        }

        PatternNode::Import { names } => {
            if node.kind() != config.pattern_matching.import_stmt.as_str() {
                return None;
            }
            let mut cursor = node.walk();
            let import_nodes: Vec<Node> = node.children_by_field_name("name", &mut cursor).collect();
            if names.len() > import_nodes.len() {
                return None;
            }
            for (alias, imp_node) in names.iter().zip(import_nodes.iter()) {
                if !match_import_alias(alias, *imp_node, source, captures, config) {
                    return None;
                }
            }
            Some(node)
        }

        PatternNode::IfStmt { test } => {
            if node.kind() != config.pattern_matching.if_stmt.as_str() {
                return None;
            }
            let cond = match node.child_by_field_name("condition") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(cond, source, test, captures, config).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::WhileStmt { test } => {
            if node.kind() != config.pattern_matching.while_stmt.as_str() {
                return None;
            }
            let cond = match node.child_by_field_name("condition") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(cond, source, test, captures, config).is_some() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::ForStmt { target, iter, is_async } => {
            let expected_kind = if *is_async { "for_statement" } else { "for_statement" };
            if node.kind() != expected_kind {
                return None;
            }
            // Check async: async for is wrapped in a for_statement under an "async" keyword
            // In tree-sitter Python, async for is still "for_statement" but may have
            // an async parent. For pattern matching, we check the node text.
            if *is_async && !node_text(node, source).starts_with("async") {
                return None;
            }
            let left = match node.child_by_field_name("left") {
                Some(n) => n,
                None => return None,
            };
            let right = match node.child_by_field_name("right") {
                Some(n) => n,
                None => return None,
            };
            if matches_node(left, source, target, captures, config).is_none() {
                return None;
            }
            if matches_node(right, source, iter, captures, config).is_none() {
                return None;
            }
            Some(node)
        }

        PatternNode::WithStmt { context, var, is_async } => {
            if node.kind() != config.pattern_matching.with_stmt.as_str() {
                return None;
            }
            if *is_async && !node_text(node, source).starts_with("async") {
                return None;
            }
            // with_statement has with_clause children which contain with_item nodes
            let mut with_items: Vec<Node> = Vec::new();
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if child.kind() == "with_item" {
                    with_items.push(child);
                } else if child.kind() == "with_clause" {
                    let mut c2 = child.walk();
                    for grandchild in child.children(&mut c2) {
                        if grandchild.kind() == "with_item" {
                            with_items.push(grandchild);
                        }
                    }
                }
            }
            if with_items.is_empty() {
                return None;
            }
            let item = with_items[0];
            // with_item has "value" field (context expr); the "as var" part is the
            // second named child (after the expression).
            // with_item has "value" field which is either a plain expression
            // or an as_pattern node (for "with expr as var:" syntax).
            let value_node = match item.child_by_field_name("value") {
                Some(n) => n,
                None => return None,
            };
            if value_node.kind() == config.pattern_matching.as_pattern.as_str() {
                // "with expr as var:" — as_pattern has the expression and alias
                let mut c3 = value_node.walk();
                let named_children: Vec<Node> = value_node.children(&mut c3)
                    .filter(|n| n.is_named())
                    .collect();
                let expr_node = match named_children.first() {
                    Some(n) => *n,
                    None => return None,
                };
                if matches_node(expr_node, source, context, captures, config).is_none() {
                    return None;
                }
                if let Some(var_pattern) = var {
                    let alias_node = value_node.child_by_field_name("alias");
                    match alias_node {
                        Some(an) => {
                            if matches_node(an, source, var_pattern, captures, config).is_none() {
                                return None;
                            }
                        }
                        None => return None,
                    }
                }
            } else {
                // "with expr:" — no as clause
                if matches_node(value_node, source, context, captures, config).is_none() {
                    return None;
                }
                if var.is_some() {
                    // Pattern requires "as var" but statement doesn't have one
                    return None;
                }
            }
            Some(node)
        }

        PatternNode::TryStmt => {
            if node.kind() == config.pattern_matching.try_stmt.as_str() {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::NodeKindMatch(kinds) => {
            if kinds.iter().any(|k| k == node.kind()) {
                Some(node)
            } else {
                None
            }
        }

        PatternNode::ExceptHandler { exception_type, name } => {
            if node.kind() != config.pattern_matching.except_handler.as_str() {
                return None;
            }
            let mut cursor = node.walk();
            let named_children: Vec<Node> = node.children(&mut cursor)
                .filter(|n| n.is_named())
                .collect();
            if let Some(type_pattern) = exception_type {
                if named_children.is_empty() {
                    return None;
                }
                if matches_node(named_children[0], source, type_pattern, captures, config).is_none() {
                    return None;
                }
            }
            if let Some(name_pattern) = name {
                if named_children.len() < 2 {
                    return None;
                }
                if matches_node(named_children[1], source, name_pattern, captures, config).is_none() {
                    return None;
                }
            }
            Some(node)
        }
    }
}

fn match_args(
    arg_patterns: &[ArgPattern],
    call_args: &[Node],
    source: &[u8],
    exact: bool,
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
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
                    if matches_node(call_args[i], source, pnode, captures, config).is_none() {
                        return false;
                    }
                }
                ArgPattern::Star(pnode) => {
                    if call_args[i].kind() != config.pattern_matching.list_splat.as_str() {
                        return false;
                    }
                    let mut cursor = call_args[i].walk();
                    let inner = call_args[i].children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode, captures, config).is_none() {
                            return false;
                        }
                    } else {
                        return false;
                    }
                }
                ArgPattern::DoubleStar(pnode) => {
                    if call_args[i].kind() != config.pattern_matching.dictionary_splat.as_str() {
                        return false;
                    }
                    let mut cursor = call_args[i].walk();
                    let inner = call_args[i].children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode, captures, config).is_none() {
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
                    if matches_node(node, source, pnode, &mut temp_captures, config).is_none() {
                        continue 'outer;
                    }
                }
                ArgPattern::Star(pnode) => {
                    if node.kind() != config.pattern_matching.list_splat.as_str() {
                        continue 'outer;
                    }
                    let mut cursor = node.walk();
                    let inner = node.children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode, &mut temp_captures, config).is_none() {
                            continue 'outer;
                        }
                    } else {
                        continue 'outer;
                    }
                }
                ArgPattern::DoubleStar(pnode) => {
                    if node.kind() != config.pattern_matching.dictionary_splat.as_str() {
                        continue 'outer;
                    }
                    let mut cursor = node.walk();
                    let inner = node.children(&mut cursor).find(|n| n.is_named());
                    if let Some(inner_node) = inner {
                        if matches_node(inner_node, source, pnode, &mut temp_captures, config).is_none() {
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

fn param_has_default(param: Node, source: &[u8], dv: &PatternNode, captures: &mut HashMap<String, String>, config: &LanguageConfig) -> bool {
    if param.kind() != "default_parameter" && param.kind() != "typed_default_parameter" {
        return false;
    }
    if let Some(value_node) = param.child_by_field_name("value") {
        matches_node(value_node, source, dv, captures, config).is_some()
    } else {
        false
    }
}

fn match_params(
    param_patterns: &[ParamPattern],
    params: &[Node],
    source: &[u8],
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
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
                ParamPattern::Any => {
                    // Any matches regular params but NOT star/double-star
                    if params[i].kind() == "list_splat_pattern" || params[i].kind() == "dictionary_splat_pattern" {
                        return false;
                    }
                }
                ParamPattern::Ellipsis | ParamPattern::EllipsisMetavar(_) => unreachable!(),
                ParamPattern::WithDefault(dv) => {
                    if !param_has_default(params[i], source, dv, captures, config) {
                        return false;
                    }
                }
                ParamPattern::Star(ref cap_name) => {
                    if params[i].kind() != "list_splat_pattern" {
                        return false;
                    }
                    if let Some(name) = cap_name {
                        if let Some(id_node) = params[i].named_child(0) {
                            captures.insert(name.clone(), node_text(id_node, source).to_string());
                        }
                    }
                }
                ParamPattern::DoubleStar(ref cap_name) => {
                    if params[i].kind() != "dictionary_splat_pattern" {
                        return false;
                    }
                    if let Some(name) = cap_name {
                        if let Some(id_node) = params[i].named_child(0) {
                            captures.insert(name.clone(), node_text(id_node, source).to_string());
                        }
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
                ParamPattern::Any => {
                    if param.kind() == "list_splat_pattern" || param.kind() == "dictionary_splat_pattern" {
                        continue 'outer;
                    }
                }
                ParamPattern::WithDefault(dv) => {
                    if !param_has_default(param, source, dv, &mut temp_captures, config) {
                        continue 'outer;
                    }
                }
                ParamPattern::Star(ref cap_name) => {
                    if param.kind() != "list_splat_pattern" {
                        continue 'outer;
                    }
                    if let Some(name) = cap_name {
                        if let Some(id_node) = param.named_child(0) {
                            temp_captures.insert(name.clone(), node_text(id_node, source).to_string());
                        }
                    }
                }
                ParamPattern::DoubleStar(ref cap_name) => {
                    if param.kind() != "dictionary_splat_pattern" {
                        continue 'outer;
                    }
                    if let Some(name) = cap_name {
                        if let Some(id_node) = param.named_child(0) {
                            temp_captures.insert(name.clone(), node_text(id_node, source).to_string());
                        }
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
    config: &LanguageConfig,
) -> bool {
    // Find ellipsis position
    let ellipsis_pos = patterns.iter().position(
        |p| matches!(p, PatternNode::Ellipsis | PatternNode::EllipsisMetavar(_))
    );

    if ellipsis_pos.is_none() {
        // No ellipsis — exact element count match
        if nodes.len() != patterns.len() {
            return false;
        }
        for (i, p) in patterns.iter().enumerate() {
            if matches_node(nodes[i], source, p, captures, config).is_none() {
                return false;
            }
        }
        return true;
    }

    let eidx = ellipsis_pos.unwrap();
    let prefix = &patterns[..eidx];
    let suffix = &patterns[eidx + 1..];

    // Need at least prefix.len() + suffix.len() nodes
    if nodes.len() < prefix.len() + suffix.len() {
        return false;
    }

    let mut temp_captures = captures.clone();

    // Match prefix patterns against first prefix.len() nodes
    for (i, p) in prefix.iter().enumerate() {
        if matches_node(nodes[i], source, p, &mut temp_captures, config).is_none() {
            return false;
        }
    }

    // Match suffix patterns against last suffix.len() nodes
    let suffix_start = nodes.len() - suffix.len();
    for (i, p) in suffix.iter().enumerate() {
        if matches_node(nodes[suffix_start + i], source, p, &mut temp_captures, config).is_none() {
            return false;
        }
    }

    // Capture the ellipsis (everything between prefix and suffix)
    if let PatternNode::EllipsisMetavar(name) = &patterns[eidx] {
        let middle_nodes = &nodes[prefix.len()..suffix_start];
        let text: Vec<_> = middle_nodes.iter().map(|n| node_text(*n, source)).collect();
        temp_captures.insert(name.clone(), text.join(", "));
    }

    *captures = temp_captures;
    true
}

fn match_fstring(
    parts: &[FStringPart],
    node: Node,
    source: &[u8],
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
) -> bool {
    if node.kind() != config.pattern_matching.string {
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
                    if matches_node(inner_node, source, pnode, captures, config).is_none() {
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
    config: &LanguageConfig,
) -> bool {
    if node.kind() != config.pattern_matching.for_in_clause {
        return false;
    }
    let target_node = match node.child_by_field_name(&config.pattern_matching.target_field) {
        Some(n) => n,
        None => return false,
    };
    let iter_node = match node.child_by_field_name(&config.pattern_matching.iter_field) {
        Some(n) => n,
        None => return false,
    };
    if matches_node(target_node, source, &pattern.target, captures, config).is_none()
        || matches_node(iter_node, source, &pattern.iter, captures, config).is_none()
    {
        return false;
    }
    let mut cursor = node.walk();
    let if_clause_nodes: Vec<Node> = node
        .children(&mut cursor)
        .filter(|n| n.kind() == config.pattern_matching.if_clause)
        .collect();
    let mut if_nodes: Vec<Node> = Vec::new();
    for ic in &if_clause_nodes {
        let mut c = ic.walk();
        let children: Vec<Node> = ic.children(&mut c).filter(|ch| ch.is_named()).collect();
        if let Some(expr) = children.first() {
            if_nodes.push(*expr);
        }
    }

    match_sequence(&pattern.ifs, &if_nodes, source, captures, config)
}

fn match_generators(
    patterns: &[ComprehensionGenerator],
    nodes: &[Node],
    source: &[u8],
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
) -> bool {
    if nodes.len() != patterns.len() {
        return false;
    }
    for (i, p) in patterns.iter().enumerate() {
        if !match_generator(p, nodes[i], source, captures, config) {
            return false;
        }
    }
    true
}

/// Match generators with their associated if_clauses grouped from the parent node.
/// Each entry in `groups` is (for_in_clause_node, vec_of_if_clause_nodes).
fn match_generators_with_ifs(
    patterns: &[ComprehensionGenerator],
    groups: &[(Node, Vec<Node>)],
    source: &[u8],
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
) -> bool {
    if groups.len() != patterns.len() {
        return false;
    }
    for (i, p) in patterns.iter().enumerate() {
        let (for_node, if_clause_nodes) = &groups[i];
        if for_node.kind() != config.pattern_matching.for_in_clause.as_str() {
            return false;
        }
        let target_node = match for_node.child_by_field_name("left") {
            Some(n) => n,
            None => return false,
        };
        let iter_node = match for_node.child_by_field_name("right") {
            Some(n) => n,
            None => return false,
        };
        if matches_node(target_node, source, &p.target, captures, config).is_none()
            || matches_node(iter_node, source, &p.iter, captures, config).is_none()
        {
            return false;
        }
        // Extract condition expressions from if_clause nodes
        let mut if_exprs: Vec<Node> = Vec::new();
        for ic in if_clause_nodes {
            let mut c = ic.walk();
            let children: Vec<Node> = ic.children(&mut c).filter(|ch| ch.is_named()).collect();
            if let Some(expr) = children.first() {
                if_exprs.push(*expr);
            }
        }
        if !match_sequence(&p.ifs, &if_exprs, source, captures, config) {
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
    config: &LanguageConfig,
) -> bool {
    let has_ellipsis = patterns.iter().any(|p| matches!(p, DictElementPattern::Ellipsis | DictElementPattern::EllipsisMetavar(_)));
    if !has_ellipsis {
        if nodes.len() != patterns.len() {
            return false;
        }
        for (i, p) in patterns.iter().enumerate() {
            if !match_dict_element(p, nodes[i], source, captures, config) {
                return false;
            }
        }
        return true;
    }
    // Set-based matching for dict elements with spread (keys may be in any order)
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
    // For each pattern element, find any matching node (order-independent)
    let mut temp_captures = captures.clone();
    let mut used: Vec<bool> = vec![false; nodes.len()];
    for pnode in &non_ellipsis {
        let mut found = false;
        for (ni, node) in nodes.iter().enumerate() {
            if used[ni] {
                continue;
            }
            let mut try_captures = temp_captures.clone();
            if match_dict_element(pnode, *node, source, &mut try_captures, config) {
                used[ni] = true;
                temp_captures = try_captures;
                found = true;
                break;
            }
        }
        if !found {
            return false;
        }
    }
    // Capture unmatched elements for ellipsis metavar
    for p in patterns {
        if let DictElementPattern::EllipsisMetavar(name) = p {
            let text: Vec<_> = nodes.iter().enumerate()
                .filter(|(i, _)| !used[*i])
                .map(|(_, n)| node_text(*n, source))
                .collect();
            temp_captures.insert(name.clone(), text.join(", "));
        }
    }
    *captures = temp_captures;
    true
}

fn match_dict_element(
    pattern: &DictElementPattern,
    node: Node,
    source: &[u8],
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
) -> bool {
    match pattern {
        DictElementPattern::Pair { key, value } => {
            if node.kind() != config.pattern_matching.pair {
                return false;
            }
            let knode = match node.child_by_field_name(config.symbols.key_field()) {
                Some(n) => n,
                None => return false,
            };
            let vnode = match node.child_by_field_name(&config.pattern_matching.value_field) {
                Some(n) => n,
                None => return false,
            };
            matches_node(knode, source, key, captures, config).is_some()
                && matches_node(vnode, source, value, captures, config).is_some()
        }
        DictElementPattern::Spread(value) => {
            if node.kind() != config.pattern_matching.dictionary_splat {
                return false;
            }
            let mut cursor = node.walk();
            let inner = node.children(&mut cursor).find(|n| n.is_named());
            if let Some(inner_node) = inner {
                matches_node(inner_node, source, value, captures, config).is_some()
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

fn match_import_alias<'a>(
    pattern: &ImportAlias,
    node: Node<'a>,
    source: &[u8],
    captures: &mut HashMap<String, String>,
    config: &LanguageConfig,
) -> bool {
    let imp_text = node_text(node, source);
    // Check if the import node is an aliased import (has " as ")
    let (actual_name, actual_alias) = if node.kind() == config.imports.aliased_import.as_deref().unwrap_or("aliased_import") {
        if let Some(pos) = imp_text.find(" as ") {
            (&imp_text[..pos], Some(&imp_text[pos + 4..]))
        } else {
            (imp_text, None)
        }
    } else {
        (imp_text, None)
    };

    // Match the name part
    match &pattern.name {
        NameOrMetavar::Literal(expected) => {
            if actual_name != expected.as_str() {
                return false;
            }
        }
        NameOrMetavar::Metavar(mv_name) => {
            captures.insert(mv_name.clone(), actual_name.to_string());
        }
    }

    // Match the asname part
    match (&pattern.asname, actual_alias) {
        (None, None) => {} // both have no alias
        (None, Some(_)) => return false, // pattern has no alias but source does
        (Some(_), None) => return false, // pattern expects alias but source doesn't
        (Some(NameOrMetavar::Literal(expected)), Some(actual)) => {
            if actual != expected.as_str() {
                return false;
            }
        }
        (Some(NameOrMetavar::Metavar(mv_name)), Some(actual)) => {
            captures.insert(mv_name.clone(), actual.to_string());
        }
    }
    true
}

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

fn any_ancestor_matches(ancestors: &[Node], source: &[u8], pattern: &PatternNode, config: &LanguageConfig) -> bool {
    let mut dummy_captures = HashMap::new();
    ancestors
        .iter()
        .any(|anc| matches_node(*anc, source, pattern, &mut dummy_captures, config).is_some())
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
    config: &LanguageConfig,
) -> Vec<(String, usize, usize, usize, usize, String, HashMap<String, String>)> {
    let mut results = Vec::new();
    let mut ancestors: Vec<Node> = Vec::new();

    walk_with_ancestors(
        tree.root_node(),
        source_bytes,
        &mut ancestors,
        &mut |node, ancs| {
            let mut captures = HashMap::new();
            let matched_node = match matches_node(node, source_bytes, pattern, &mut captures, config) {
                Some(n) => n,
                None => return,
            };
            if let Some(inside_pat) = inside {
                if !any_ancestor_matches(ancs, source_bytes, inside_pat, config) {
                    return;
                }
            }
            if let Some(not_inside_pat) = not_inside {
                if any_ancestor_matches(ancs, source_bytes, not_inside_pat, config) {
                    return;
                }
            }

            let start = matched_node.start_position();
            let end = matched_node.end_position();
            let text = std::str::from_utf8(&source_bytes[matched_node.start_byte()..matched_node.end_byte()])
                .unwrap_or("")
                .to_string();

            // Deduplicate: skip if we already have a match at this exact position
            if results.iter().any(|r: &(String, usize, usize, usize, usize, String, HashMap<String, String>)| {
                r.1 == start.row + 1 && r.2 == start.column && r.3 == end.row + 1 && r.4 == end.column
            }) {
                return;
            }

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
    config: &LanguageConfig,
) -> Vec<(String, usize, usize, usize, usize, String, HashMap<String, String>)> {
    let path_buf = std::path::PathBuf::from(file_path);
    let ext = path_buf.extension().and_then(|e| e.to_str()).unwrap_or("");
    let tree = match crate::pattern::parse_by_extension(source, ext) {
        Some(t) => t,
        None => return vec![],
    };
    find_pattern_in_tree(&tree, source.as_bytes(), file_path, pattern, inside, not_inside, config)
}

#[pyfunction]
#[pyo3(signature = (file_contents, pattern_ir, inside_ir=None, not_inside_ir=None, extension=None))]
pub fn find_pattern_in_files(
    py: Python,
    file_contents: Vec<(String, String)>,
    pattern_ir: Bound<'_, PyAny>,
    inside_ir: Option<Bound<'_, PyAny>>,
    not_inside_ir: Option<Bound<'_, PyAny>>,
    extension: Option<&str>,
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

    // Load config based on extension (default to python if not specified)
    let config = if let Some(ext) = extension {
        // We don't have a Path to project_root here, but LanguageConfig::load_for_extension 
        // needs it to find languages/ dir.
        // For now, assume current dir or fallback to python_default.
        LanguageConfig::load_for_extension(ext, std::path::Path::new("."))
            .unwrap_or_else(|_| LanguageConfig::python_default())
    } else {
        LanguageConfig::python_default()
    };

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
                    &config,
                )
            })
            .collect();

        Ok(results.into_iter().flatten().collect())
    })
}

#[pyfunction]
#[pyo3(signature = (file_contents, patterns, extension=None))]
pub fn find_multi_patterns_in_files(
    py: Python,
    file_contents: Vec<(String, String)>,
    patterns: Vec<(Bound<'_, PyAny>, Option<Bound<'_, PyAny>>)>,
    extension: Option<&str>,
) -> PyResult<Vec<(usize, String, usize, usize, usize, usize, String)>> {
    let compiled: Vec<(PatternNode, Option<PatternNode>)> = patterns
        .iter()
        .map(|(pat_ir, ni_ir)| {
            let pat = deserialize_pattern(pat_ir)?;
            let ni = ni_ir.as_ref().map(|ir| deserialize_pattern(ir)).transpose()?;
            Ok((pat, ni))
        })
        .collect::<PyResult<_>>()?;

    // Load config based on extension
    let config = if let Some(ext) = extension {
        LanguageConfig::load_for_extension(ext, std::path::Path::new("."))
            .unwrap_or_else(|_| LanguageConfig::python_default())
    } else {
        LanguageConfig::python_default()
    };

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
                            &config,
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

// ---------------------------------------------------------------------------
// Pattern compilation from tree-sitter
// ---------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (source, extension, project_root=None))]
pub fn compile_pattern_treesitter(
    py: Python,
    source: &str,
    extension: &str,
    project_root: Option<&str>,
) -> PyResult<PyObject> {
    let root = project_root
        .map(std::path::Path::new)
        .unwrap_or(std::path::Path::new("."));
    let config = LanguageConfig::load_for_extension(extension, root)
        .unwrap_or_else(|_| LanguageConfig::python_default());

    let tree = crate::pattern::parse_by_extension(source, extension).ok_or_else(|| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>("Failed to parse pattern source")
    })?;

    let root_node = tree.root_node();
    // Usually patterns are single expressions or statements.
    // We want to find the first meaningful node.
    let mut pattern_node = None;
    for i in 0..root_node.named_child_count() {
        let child = root_node.named_child(i).unwrap();
        if config.pattern_matching.statement_nodes.iter().any(|s| s == child.kind()) {
            pattern_node = Some(child);
            break;
        }
    }
    
    if pattern_node.is_none() {
        pattern_node = root_node.named_child(0);
    }

    let pattern_node = pattern_node.ok_or_else(|| {
        PyErr::new::<pyo3::exceptions::PyValueError, _>("No named nodes found in pattern")
    })?;

    Ok(node_to_ir(py, pattern_node, source.as_bytes(), &config).into())
}

fn node_to_ir<'a>(
    py: Python<'a>,
    node: Node,
    source: &[u8],
    config: &LanguageConfig,
) -> Bound<'a, PyDict> {
    let dict = PyDict::new(py);
    let kind = node.kind();

    // Identifier
    if kind == config.pattern_matching.identifier {
        dict.set_item("type", "name").unwrap();
        dict.set_item("value", node_text(node, source)).unwrap();
        return dict;
    }

    // Call
    if kind == config.pattern_matching.call {
        dict.set_item("type", "call").unwrap();
        if let Some(func) = node.child_by_field_name(&config.pattern_matching.func_field) {
            dict.set_item("func", node_to_ir(py, func, source, config))
                .unwrap();
        }
        let args = PyList::empty(py);
        if let Some(arg_list) = node.child_by_field_name(&config.pattern_matching.args_field) {
            let mut cursor = arg_list.walk();
            for child in arg_list.children(&mut cursor).filter(|n| n.is_named()) {
                let arg_ir = node_to_ir(py, child, source, config);
                // Wrap in arg info
                let arg_dict = PyDict::new(py);
                let ck = child.kind();
                if ck == config.pattern_matching.list_splat {
                    arg_dict.set_item("type", "star").unwrap();
                    let inner = child.children(&mut child.walk()).find(|n| n.is_named());
                    if let Some(in_node) = inner {
                        arg_dict.set_item("value", node_to_ir(py, in_node, source, config)).unwrap();
                    }
                } else if ck == config.pattern_matching.dictionary_splat {
                    arg_dict.set_item("type", "double_star").unwrap();
                    let inner = child.children(&mut child.walk()).find(|n| n.is_named());
                    if let Some(in_node) = inner {
                        arg_dict.set_item("value", node_to_ir(py, in_node, source, config)).unwrap();
                    }
                } else if ck == config.pattern_matching.keyword_argument {
                    arg_dict.set_item("type", "keyword_arg").unwrap();
                    let key = child.child_by_field_name(config.symbols.name_field.as_deref().unwrap_or("name")).map(|n| node_text(n, source)).unwrap_or("");
                    let val = child.child_by_field_name(config.pattern_matching.value_field.as_str()).map(|n| node_to_ir(py, n, source, config));
                    arg_dict.set_item("key", key).unwrap();
                    if let Some(v) = val {
                        arg_dict.set_item("value", v).unwrap();
                    }
                } else {
                    // Regular arg
                    arg_dict.set_item("type", "arg").unwrap();
                    arg_dict.set_item("value", arg_ir).unwrap();
                }
                args.append(arg_dict).unwrap();
            }
        }
        dict.set_item("args", args).unwrap();
        return dict;
    }

    // Attribute
    if kind == config.pattern_matching.attribute {
        dict.set_item("type", "attr").unwrap();
        if let Some(obj) = node.child_by_field_name(&config.pattern_matching.object_field) {
            dict.set_item("value", node_to_ir(py, obj, source, config))
                .unwrap();
        }
        if let Some(attr) = node.child_by_field_name(&config.pattern_matching.attr_field) {
            dict.set_item("attr", node_text(attr, source)).unwrap();
        }
        return dict;
    }

    // Literals
    if kind == config.pattern_matching.integer {
        dict.set_item("type", "integer").unwrap();
        dict.set_item("value", node_text(node, source)).unwrap();
        return dict;
    }
    if kind == config.pattern_matching.float {
        dict.set_item("type", "float").unwrap();
        dict.set_item("value", node_text(node, source)).unwrap();
        return dict;
    }
    if kind == config.pattern_matching.string {
        dict.set_item("type", "string").unwrap();
        dict.set_item("value", node_text(node, source)).unwrap();
        return dict;
    }
    if kind == config.pattern_matching.true_literal {
        dict.set_item("type", "bool").unwrap();
        dict.set_item("value", true).unwrap();
        return dict;
    }
    if kind == config.pattern_matching.false_literal {
        dict.set_item("type", "bool").unwrap();
        dict.set_item("value", false).unwrap();
        return dict;
    }
    if kind == config.pattern_matching.none_literal {
        dict.set_item("type", "none_literal").unwrap();
        return dict;
    }

    // Collections
    if kind == config.pattern_matching.list {
        dict.set_item("type", "list").unwrap();
        let elems = PyList::empty(py);
        let mut cursor = node.walk();
        for child in node.children(&mut cursor).filter(|n| n.is_named()) {
            elems.append(node_to_ir(py, child, source, config)).unwrap();
        }
        dict.set_item("elements", elems).unwrap();
        return dict;
    }
    if kind == config.pattern_matching.tuple {
        dict.set_item("type", "tuple").unwrap();
        let elems = PyList::empty(py);
        let mut cursor = node.walk();
        for child in node.children(&mut cursor).filter(|n| n.is_named()) {
            elems.append(node_to_ir(py, child, source, config)).unwrap();
        }
        dict.set_item("elements", elems).unwrap();
        return dict;
    }

    // Subscript
    if kind == config.pattern_matching.subscript {
        dict.set_item("type", "subscript").unwrap();
        if let Some(obj) = node.child_by_field_name(&config.pattern_matching.value_field) {
            dict.set_item("value", node_to_ir(py, obj, source, config)).unwrap();
        }
        let slices = PyList::empty(py);
        // tree-sitter-python: subscript has a 'subscript' field for the index
        // or we can just look for named children that are not the 'value'
        if let Some(index) = node.child_by_field_name("subscript") {
             slices.append(node_to_ir(py, index, source, config)).unwrap();
        } else {
             // Fallback for other languages: look for children after the first one
             let mut cursor = node.walk();
             let named_children: Vec<_> = node.children(&mut cursor).filter(|n| n.is_named()).collect();
             if named_children.len() > 1 {
                 for child in &named_children[1..] {
                     slices.append(node_to_ir(py, *child, source, config)).unwrap();
                 }
             }
        }
        dict.set_item("slices", slices).unwrap();
        return dict;
    }

    // Binary / Boolean Ops
    if kind == config.pattern_matching.binary_operator || kind == config.pattern_matching.boolean_operator {
        dict.set_item("type", "binary_op").unwrap();
        if let Some(left) = node.child_by_field_name(&config.pattern_matching.left_field) {
            dict.set_item("left", node_to_ir(py, left, source, config)).unwrap();
        }
        if let Some(right) = node.child_by_field_name(&config.pattern_matching.right_field) {
            dict.set_item("right", node_to_ir(py, right, source, config)).unwrap();
        }
        if let Some(op) = node.child_by_field_name(&config.pattern_matching.operator_field) {
            dict.set_item("op", node_text(op, source)).unwrap();
        }
        return dict;
    }

    // Unary Ops
    if kind == config.pattern_matching.unary_operator {
        dict.set_item("type", "unary_op").unwrap();
        if let Some(operand) = node.child_by_field_name("argument") {
             dict.set_item("operand", node_to_ir(py, operand, source, config)).unwrap();
        } else {
             let mut cursor = node.walk();
             let named = node.children(&mut cursor).find(|n| n.is_named());
             if let Some(n) = named {
                 dict.set_item("operand", node_to_ir(py, n, source, config)).unwrap();
             }
        }
        if let Some(op) = node.child_by_field_name(&config.pattern_matching.operator_field) {
            dict.set_item("op", node_text(op, source)).unwrap();
        }
        return dict;
    }

    // Comparison Ops
    if kind == config.pattern_matching.comparison_operator {
        dict.set_item("type", "compare").unwrap();
        let mut cursor = node.walk();
        let named_children: Vec<_> = node.children(&mut cursor).filter(|n| n.is_named()).collect();
        if !named_children.is_empty() {
             dict.set_item("left", node_to_ir(py, named_children[0], source, config)).unwrap();
             let ops = PyList::empty(py);
             // Children alternate between operators and operands
             // but tree-sitter-python has comparison_operator as a single node
             // with children: expr, operator, expr, [operator, expr]*
             let mut i = 1;
             while i + 1 < named_children.len() {
                 let pair = PyDict::new(py);
                 pair.set_item("op", node_text(named_children[i], source)).unwrap();
                 pair.set_item("comparator", node_to_ir(py, named_children[i+1], source, config)).unwrap();
                 ops.append(pair).unwrap();
                 i += 2;
             }
             dict.set_item("ops", ops).unwrap();
        }
        return dict;
    }

    // Assignment
    if kind == config.pattern_matching.assignment {
        dict.set_item("type", "assign").unwrap();
        if let Some(left) = node.child_by_field_name(&config.pattern_matching.left_field) {
            dict.set_item("target", node_to_ir(py, left, source, config)).unwrap();
        }
        if let Some(right) = node.child_by_field_name(&config.pattern_matching.right_field) {
            dict.set_item("value", node_to_ir(py, right, source, config)).unwrap();
        }
        return dict;
    }

    // Parenthesized Expression (unwrap it)
    if kind == config.pattern_matching.parenthesized_expression {
        if let Some(child) = node.named_child(0) {
            return node_to_ir(py, child, source, config);
        }
    }

    // Function Definition
    if kind == config.pattern_matching.function_def {
        dict.set_item("type", "func_def").unwrap();
        if let Some(name) = node.child_by_field_name(config.symbols.name_field()) {
            dict.set_item("name", node_to_ir(py, name, source, config)).unwrap();
        }
        let params = PyList::empty(py);
        if let Some(param_list) = node.child_by_field_name(config.symbols.parameters_field()) {
            for i in 0..param_list.named_child_count() {
                if let Some(child) = param_list.named_child(i) {
                    params.append(node_to_ir(py, child, source, config)).unwrap();
                }
            }
        }
        dict.set_item("params", params).unwrap();
        if let Some(body) = node.child_by_field_name(config.symbols.body_field())
            .or_else(|| node.named_child(node.named_child_count() - 1)) {
             dict.set_item("body", node_to_ir(py, body, source, config)).unwrap();
        }
        return dict;
    }

    // Class Definition
    if kind == config.pattern_matching.class_def {
        dict.set_item("type", "class_def").unwrap();
        if let Some(name) = node.child_by_field_name(config.symbols.name_field()) {
            dict.set_item("name", node_to_ir(py, name, source, config)).unwrap();
        }
        let bases = PyList::empty(py);
        if let Some(base_list) = node.child_by_field_name(config.symbols.superclasses_field()) {
            for i in 0..base_list.named_child_count() {
                if let Some(child) = base_list.named_child(i) {
                    bases.append(node_to_ir(py, child, source, config)).unwrap();
                }
            }
        }
        dict.set_item("bases", bases).unwrap();
        if let Some(body) = node.child_by_field_name(config.symbols.body_field())
            .or_else(|| node.named_child(node.named_child_count() - 1)) {
             dict.set_item("body", node_to_ir(py, body, source, config)).unwrap();
        }
        return dict;
    }

    // Expression Statement (unwrap it)
    if kind == "expression_statement" {
        if let Some(child) = node.named_child(0) {
            return node_to_ir(py, child, source, config);
        }
    }

    // If Statement
    if kind == config.pattern_matching.if_stmt {
        dict.set_item("type", "if_stmt").unwrap();
        if let Some(cond) = node.child_by_field_name(&config.pattern_matching.condition_field) {
            dict.set_item("test", node_to_ir(py, cond, source, config)).unwrap();
        }
        if let Some(body) = node.child_by_field_name("consequent") {
             dict.set_item("body", node_to_ir(py, body, source, config)).unwrap();
        } else if let Some(body) = node.child_by_field_name("body") {
             dict.set_item("body", node_to_ir(py, body, source, config)).unwrap();
        }
        if let Some(alternative) = node.child_by_field_name("alternative") {
             dict.set_item("orelse", node_to_ir(py, alternative, source, config)).unwrap();
        }
        return dict;
    }

    // While Statement
    if kind == config.pattern_matching.while_stmt {
        dict.set_item("type", "while_stmt").unwrap();
        if let Some(cond) = node.child_by_field_name(&config.pattern_matching.condition_field) {
            dict.set_item("test", node_to_ir(py, cond, source, config)).unwrap();
        }
        if let Some(body) = node.child_by_field_name("body") {
             dict.set_item("body", node_to_ir(py, body, source, config)).unwrap();
        }
        return dict;
    }

    // Return Statement
    if kind == config.pattern_matching.return_stmt {
        dict.set_item("type", "return").unwrap();
        let val_node = node.child_by_field_name(&config.pattern_matching.value_field)
            .or_else(|| node.named_child(0));
        if let Some(val) = val_node {
            dict.set_item("value", node_to_ir(py, val, source, config)).unwrap();
        }
        return dict;
    }

    // Raise / Throw Statement
    if kind == config.pattern_matching.raise_stmt {
        dict.set_item("type", "raise").unwrap();
        let exc_node = node.named_child(0);
        if let Some(exc) = exc_node {
            dict.set_item("exc", node_to_ir(py, exc, source, config)).unwrap();
        }
        return dict;
    }

    // Assert Statement
    if !config.pattern_matching.assert_stmt.is_empty() && kind == config.pattern_matching.assert_stmt {
        dict.set_item("type", "assert").unwrap();
        let test_node = node.child_by_field_name("test")
            .or_else(|| node.named_child(0));
        if let Some(tn) = test_node {
            dict.set_item("test", node_to_ir(py, tn, source, config)).unwrap();
        }
        let msg_node = node.child_by_field_name("message")
            .or_else(|| node.named_child(1));
        if let Some(mn) = msg_node {
            dict.set_item("msg", node_to_ir(py, mn, source, config)).unwrap();
        }
        return dict;
    }

    // Import Statement
    if kind == config.pattern_matching.import_stmt {
        dict.set_item("type", "import").unwrap();
        let names = PyList::empty(py);
        let mut cursor = node.walk();
        for child in node.children_by_field_name(&config.imports.name_field, &mut cursor) {
             let pair = PyDict::new(py);
             let text = node_text(child, source);
             if child.kind() == config.imports.aliased_import.as_deref().unwrap_or("aliased_import") {
                 if let Some(pos) = text.find(" as ") {
                     pair.set_item("name", &text[..pos]).unwrap();
                     pair.set_item("asname", &text[pos+4..]).unwrap();
                 } else {
                     pair.set_item("name", text).unwrap();
                     pair.set_item("asname", py.None()).unwrap();
                 }
             } else {
                 pair.set_item("name", text).unwrap();
                 pair.set_item("asname", py.None()).unwrap();
             }
             names.append(pair).unwrap();
        }
        dict.set_item("names", names).unwrap();
        return dict;
    }

    // Import From Statement
    if kind == config.pattern_matching.import_from_stmt {
        dict.set_item("type", "import_from").unwrap();
        if let Some(module) = node.child_by_field_name(&config.imports.module_field) {
            dict.set_item("module", node_text(module, source)).unwrap();
        }
        let names = PyList::empty(py);
        let mut cursor = node.walk();
        for child in node.children_by_field_name(&config.imports.name_field, &mut cursor) {
             let pair = PyDict::new(py);
             let text = node_text(child, source);
             if child.kind() == config.imports.aliased_import.as_deref().unwrap_or("aliased_import") {
                 if let Some(pos) = text.find(" as ") {
                     pair.set_item("name", &text[..pos]).unwrap();
                     pair.set_item("asname", &text[pos+4..]).unwrap();
                 } else {
                     pair.set_item("name", text).unwrap();
                     pair.set_item("asname", py.None()).unwrap();
                 }
             } else {
                 pair.set_item("name", text).unwrap();
                 pair.set_item("asname", py.None()).unwrap();
             }
             names.append(pair).unwrap();
        }
        dict.set_item("names", names).unwrap();
        return dict;
    }

    // Block (unwrap it)
    if kind == "block" {
        if node.named_child_count() == 1 {
            if let Some(child) = node.named_child(0) {
                return node_to_ir(py, child, source, config);
            }
        } else {
            // For multiple statements in a block, just return the first one for now.
            if let Some(child) = node.named_child(0) {
                return node_to_ir(py, child, source, config);
            }
        }
    }

    // Fallback: treat as a generic node kind match if we don't know it
    // Actually, for patterns, we probably want to report an error or treat as "any"
    dict.set_item("type", "name").unwrap();
    dict.set_item("value", node_text(node, source)).unwrap();
    dict
}
