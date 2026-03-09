//! Scope resolver: builds scope trees from tree-sitter and resolves
//! qualified names.
//!
//! Provides a persistent, incremental scope index built on tree-sitter.
//! The scoping rules are driven by a language config file (TOML), making
//! the resolver language-agnostic in principle.
//!
//! # Architecture
//!
//! 1. **Scope tree**: Each function/class/comprehension/module creates a
//!    [`Scope`] node with a parent pointer.
//! 2. **Binding table**: Maps `(scope_id, name)` → definition site.
//! 3. **Import table**: Maps import statements to resolved module paths.
//! 4. **QN index**: Maps qualified name → list of definition/reference sites.
//!
//! The resolver is incremental: re-indexing a file only updates that file's
//! scope tree and binding table, then patches the QN index.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

const PYTHON_CONFIG_TOML: &str = include_str!("../../languages/python/config.toml");
const TS_CONFIG_TOML: &str = include_str!("../../languages/typescript/config.toml");
const RUST_CONFIG_TOML: &str = include_str!("../../languages/rust/config.toml");

/// Return a cached `&'static LanguageConfig` for the given file extension.
/// Defaults to Python config for unknown extensions.
pub fn config_for_ext(ext: &str) -> &'static LanguageConfig {
    static PY_CONFIG: OnceLock<LanguageConfig> = OnceLock::new();
    static TS_CONFIG: OnceLock<LanguageConfig> = OnceLock::new();
    static RUST_CONFIG: OnceLock<LanguageConfig> = OnceLock::new();
    match ext {
        "py" | "pyi" => PY_CONFIG.get_or_init(|| {
            LanguageConfig::from_toml(PYTHON_CONFIG_TOML)
                .unwrap_or_else(|_| LanguageConfig::python_default())
        }),
        "ts" | "tsx" | "js" | "jsx" => TS_CONFIG.get_or_init(|| {
            LanguageConfig::from_toml(TS_CONFIG_TOML)
                .expect("Failed to parse TypeScript config")
        }),
        "rs" => RUST_CONFIG.get_or_init(|| {
            LanguageConfig::from_toml(RUST_CONFIG_TOML)
                .expect("Failed to parse Rust config")
        }),
        _ => PY_CONFIG.get_or_init(|| {
            LanguageConfig::from_toml(PYTHON_CONFIG_TOML)
                .unwrap_or_else(|_| LanguageConfig::python_default())
        }),
    }
}

use crate::symbols::node_text;

// ---------------------------------------------------------------------------
// Scope kinds (language-agnostic)
// ---------------------------------------------------------------------------

/// The kind of scope a tree-sitter node creates.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ScopeKind {
    Module,
    Function,
    Class,
    Comprehension,
    Block,
}

impl ScopeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ScopeKind::Module => "Module",
            ScopeKind::Function => "Function",
            ScopeKind::Class => "Class",
            ScopeKind::Comprehension => "Comprehension",
            ScopeKind::Block => "Block",
        }
    }
}

// ---------------------------------------------------------------------------
// Core data structures
// ---------------------------------------------------------------------------

/// Unique identifier for a scope within a file.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ScopeId(pub u32);

/// A scope in the scope tree.
#[derive(Debug, Clone)]
pub struct Scope {
    pub id: ScopeId,
    pub kind: ScopeKind,
    pub parent: Option<ScopeId>,
    pub start_byte: usize,
    pub end_byte: usize,
    pub start_line: usize,
    pub end_line: usize,
    pub bindings: HashMap<String, Binding>,
}

/// A name binding in a specific scope.
#[derive(Debug, Clone)]
pub struct Binding {
    pub name: String,
    pub kind: BindingKind,
    pub line: usize,
    pub column: usize,
    pub byte_offset: usize,
    pub signature: Option<String>,
    pub type_annotation: Option<String>,
    pub returns: Option<String>,
    pub is_async: bool,
    pub created_scope: Option<ScopeId>,
}


/// How a name was introduced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindingKind {
    Assignment,
    FunctionDef,
    ClassDef,
    Parameter,
    #[allow(dead_code)]
    Import,
    LoopVariable,
    ContextManager,
    ExceptionHandler,
    Walrus,
    Global,
    Nonlocal,
}

impl BindingKind {
    pub fn as_str(self) -> &'static str {
        match self {
            BindingKind::Assignment => "Assignment",
            BindingKind::FunctionDef => "FunctionDef",
            BindingKind::ClassDef => "ClassDef",
            BindingKind::Parameter => "Parameter",
            BindingKind::Import => "Import",
            BindingKind::LoopVariable => "LoopVariable",
            BindingKind::ContextManager => "ContextManager",
            BindingKind::ExceptionHandler => "ExceptionHandler",
            BindingKind::Walrus => "Walrus",
            BindingKind::Global => "Global",
            BindingKind::Nonlocal => "Nonlocal",
        }
    }
}

/// A resolved qualified name.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct QualifiedName {
    pub name: String,
}

/// A reference to a name at a specific location.
#[derive(Debug, Clone)]
pub struct Reference {
    #[allow(dead_code)]
    pub file: PathBuf,
    pub line: usize,
    pub column: usize,
    pub byte_offset: usize,
    pub end_byte: usize,
    pub qn: QualifiedName,
    pub kind: ReferenceKind,
}

/// Whether a reference is a read, write, or call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReferenceKind {
    Read,
    Write,
    Call,
    Import,
    Definition,
}

impl ReferenceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ReferenceKind::Read => "read",
            ReferenceKind::Write => "write",
            ReferenceKind::Call => "call",
            ReferenceKind::Import => "import",
            ReferenceKind::Definition => "definition",
        }
    }
}

/// A location in the codebase.
#[derive(Debug, Clone)]
pub struct Location {
    pub file: PathBuf,
    pub line: usize,
    pub column: usize,
    pub byte_offset: usize,
}

/// Per-file scope data.
#[derive(Debug, Clone)]
pub struct FileScope {
    pub content_hash: [u8; 16],
    pub module_path: String,
    pub scopes: Vec<Scope>,
    pub imports: HashMap<String, ImportBinding>,
    pub definitions: Vec<(QualifiedName, Location)>,
    /// All references (identifiers/attributes) resolved to QNs.
    pub references: Vec<Reference>,
    /// All QN strings mentioned in this file (for pre-filter index).
    pub all_qnames: Vec<String>,
}

/// An import binding.
#[derive(Debug, Clone)]
pub struct ImportBinding {
    pub local_name: String,
    pub module_path: String,
    pub imported_name: Option<String>,
    pub is_star: bool,
}

// ---------------------------------------------------------------------------
// Language config (loaded from TOML)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LanguageConfig {
    pub language: LanguageSection,
    pub scoping: ScopingSection,
    pub bindings: BindingsSection,
    pub imports: ImportsSection,
    pub qualified_names: QualifiedNamesSection,
    pub exports: ExportsSection,
    #[serde(default)]
    pub builtins: BuiltinsSection,
    #[serde(default)]
    pub symbols: SymbolsSection,
    #[serde(default)]
    pub pattern_matching: PatternMatchingSection,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PatternMatchingSection {
    #[serde(default)]
    pub function_def: String,
    #[serde(default)]
    pub class_def: String,
    #[serde(default)]
    pub decorated_def: String,
    #[serde(default)]
    pub call: String,
    #[serde(default)]
    pub attribute: String,
    #[serde(default)]
    pub identifier: String,
    #[serde(default)]
    pub assignment: String,
    #[serde(default)]
    pub augmented_assignment: String,
    #[serde(default)]
    pub annotated_assignment: String,
    #[serde(default)]
    pub return_stmt: String,
    #[serde(default)]
    pub if_stmt: String,
    #[serde(default)]
    pub while_stmt: String,
    #[serde(default)]
    pub for_stmt: String,
    #[serde(default)]
    pub with_stmt: String,
    #[serde(default)]
    pub try_stmt: String,
    #[serde(default)]
    pub except_handler: String,
    #[serde(default)]
    pub for_in_clause: String,
    #[serde(default)]
    pub if_clause: String,
    #[serde(default)]
    pub pair: String,
    #[serde(default)]
    pub parenthesized_expression: String,
    #[serde(default)]
    pub not_operator: String,
    #[serde(default)]
    pub conditional_expression: String,
    #[serde(default)]
    pub true_literal: String,
    #[serde(default)]
    pub false_literal: String,
    #[serde(default)]
    pub none_literal: String,
    #[serde(default)]
    pub as_pattern: String,
    #[serde(default)]
    pub import_stmt: String,
    #[serde(default)]
    pub import_from_stmt: String,
    #[serde(default)]
    pub assert_stmt: String,
    #[serde(default)]
    pub raise_stmt: String,
    #[serde(default)]
    pub delete_stmt: String,
    #[serde(default)]
    pub global_stmt: String,
    #[serde(default)]
    pub nonlocal_stmt: String,
    #[serde(default)]
    pub await_expr: String,
    #[serde(default)]
    pub yield_expr: String,
    #[serde(default)]
    pub lambda: String,
    #[serde(default)]
    pub named_expr: String,
    #[serde(default)]
    pub dict_comprehension: String,
    #[serde(default)]
    pub integer: String,
    #[serde(default)]
    pub float: String,
    #[serde(default)]
    pub string: String,
    #[serde(default)]
    pub list: String,
    #[serde(default)]
    pub tuple: String,
    #[serde(default)]
    pub set: String,
    #[serde(default)]
    pub dict: String,
    #[serde(default)]
    pub binary_operator: String,
    #[serde(default)]
    pub boolean_operator: String,
    #[serde(default)]
    pub unary_operator: String,
    #[serde(default)]
    pub comparison_operator: String,
    #[serde(default)]
    pub subscript: String,
    #[serde(default)]
    pub keyword_argument: String,
    #[serde(default)]
    pub list_splat: String,
    #[serde(default)]
    pub dictionary_splat: String,
    #[serde(default)]
    pub statement_nodes: Vec<String>,
    // Field names
    #[serde(default)]
    pub func_field: String,
    #[serde(default)]
    pub args_field: String,
    #[serde(default)]
    pub object_field: String,
    #[serde(default)]
    pub attr_field: String,
    #[serde(default)]
    pub left_field: String,
    #[serde(default)]
    pub right_field: String,
    #[serde(default)]
    pub operator_field: String,
    #[serde(default)]
    pub annotation_field: String,
    #[serde(default)]
    pub value_field: String,
    #[serde(default)]
    pub condition_field: String,
    #[serde(default)]
    pub target_field: String,
    #[serde(default)]
    pub iter_field: String,
    #[serde(default)]
    pub elt_field: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct BuiltinsSection {
    #[serde(default)]
    pub names: Vec<String>,
}

/// How to extract a single parameter's name from its tree-sitter node.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParamTypeConfig {
    pub node: String,
    /// "self" = the node's own text; "field:<name>" = child by field; "child:<idx>" = nth named child
    pub name_source: String,
    #[serde(default)]
    pub prefix: Option<String>,
}

/// A punctuation-only separator node (e.g. positional-only `/` or keyword-only `*`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParamSeparatorConfig {
    pub node: String,
    pub display: String,
}

/// Statement node type lists used by `get_statement_ranges`.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct StatementsConfig {
    #[serde(default)]
    pub simple: Vec<String>,
    #[serde(default)]
    pub compound: Vec<String>,
    #[serde(default)]
    pub recurse_into: Vec<String>,
}

impl StatementsConfig {
    pub fn effective_simple(&self) -> std::borrow::Cow<'_, [String]> {
        if self.simple.is_empty() {
            std::borrow::Cow::Owned(Self::python_simple_default())
        } else {
            std::borrow::Cow::Borrowed(&self.simple)
        }
    }

    pub fn effective_recurse_into(&self) -> std::borrow::Cow<'_, [String]> {
        if self.recurse_into.is_empty() {
            std::borrow::Cow::Owned(Self::python_recurse_into_default())
        } else {
            std::borrow::Cow::Borrowed(&self.recurse_into)
        }
    }

    fn python_simple_default() -> Vec<String> {
        vec![
            "expression_statement", "return_statement", "delete_statement",
            "raise_statement", "pass_statement", "break_statement",
            "continue_statement", "import_statement", "import_from_statement",
            "future_import_statement", "global_statement", "nonlocal_statement",
            "assert_statement", "type_alias_statement", "print_statement",
        ].into_iter().map(String::from).collect()
    }

    fn python_recurse_into_default() -> Vec<String> {
        vec![
            "if_statement", "for_statement", "while_statement", "try_statement",
            "with_statement", "function_definition", "class_definition",
            "decorated_definition", "match_statement", "block", "module",
            "elif_clause", "else_clause", "except_clause", "finally_clause",
            "case_clause",
        ].into_iter().map(String::from).collect()
    }
}

/// All symbol-extraction settings for `symbols.rs`.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SymbolsSection {
    #[serde(default)] pub function_node: Option<String>,
    #[serde(default)] pub class_node: Option<String>,
    /// Wrapper node around decorated defs (Python: "decorated_definition"; empty = no wrapper).
    #[serde(default)] pub decorated_node: Option<String>,
    /// Field name within `decorated_node` that holds the real def.
    #[serde(default)] pub definition_field: Option<String>,
    /// Keyword token kind that marks async functions.
    #[serde(default)] pub async_keyword: Option<String>,
    #[serde(default)] pub name_field: Option<String>,
    #[serde(default)] pub parameters_field: Option<String>,
    #[serde(default)] pub return_type_field: Option<String>,
    #[serde(default)] pub body_field: Option<String>,
    #[serde(default)] pub superclasses_field: Option<String>,
    #[serde(default)] pub decorator_node: Option<String>,
    #[serde(default)] pub expression_statement_node: Option<String>,
    #[serde(default)] pub key_field: Option<String>,
    /// Extra function-like node used inside class bodies (TypeScript: "method_definition").
    #[serde(default)] pub method_node: Option<String>,
    #[serde(default)] pub param_types: Vec<ParamTypeConfig>,
    #[serde(default)] pub param_separators: Vec<ParamSeparatorConfig>,
    #[serde(default)] pub statements: StatementsConfig,
}

impl SymbolsSection {
    pub fn function_node(&self) -> &str {
        self.function_node.as_deref().unwrap_or("function_definition")
    }
    pub fn class_node(&self) -> &str {
        self.class_node.as_deref().unwrap_or("class_definition")
    }
    pub fn decorated_node(&self) -> &str {
        self.decorated_node.as_deref().unwrap_or("decorated_definition")
    }
    pub fn definition_field(&self) -> &str {
        self.definition_field.as_deref().unwrap_or("definition")
    }
    pub fn async_keyword(&self) -> &str {
        self.async_keyword.as_deref().unwrap_or("async")
    }
    pub fn name_field(&self) -> &str {
        self.name_field.as_deref().unwrap_or("name")
    }
    pub fn parameters_field(&self) -> &str {
        self.parameters_field.as_deref().unwrap_or("parameters")
    }
    pub fn return_type_field(&self) -> &str {
        self.return_type_field.as_deref().unwrap_or("return_type")
    }
    pub fn body_field(&self) -> &str {
        self.body_field.as_deref().unwrap_or("body")
    }
    pub fn superclasses_field(&self) -> &str {
        self.superclasses_field.as_deref().unwrap_or("superclasses")
    }
    pub fn key_field(&self) -> &str {
        self.key_field.as_deref().unwrap_or("key")
    }
    pub fn decorator_node(&self) -> &str {
        self.decorator_node.as_deref().unwrap_or("decorator")
    }
    pub fn expression_statement_node(&self) -> &str {
        self.expression_statement_node.as_deref().unwrap_or("expression_statement")
    }
    pub fn method_node(&self) -> Option<&str> {
        self.method_node.as_deref()
    }

    /// Returns param type configs; falls back to Python defaults when empty.
    pub fn effective_param_types(&self) -> std::borrow::Cow<'_, [ParamTypeConfig]> {
        if self.param_types.is_empty() {
            std::borrow::Cow::Owned(Self::python_param_types_default())
        } else {
            std::borrow::Cow::Borrowed(&self.param_types)
        }
    }

    /// Returns param separator configs; falls back to Python defaults when empty.
    pub fn effective_param_separators(&self) -> std::borrow::Cow<'_, [ParamSeparatorConfig]> {
        if self.param_separators.is_empty() {
            std::borrow::Cow::Owned(Self::python_param_separators_default())
        } else {
            std::borrow::Cow::Borrowed(&self.param_separators)
        }
    }

    fn python_param_types_default() -> Vec<ParamTypeConfig> {
        vec![
            ParamTypeConfig { node: "identifier".to_string(), name_source: "self".to_string(), prefix: None },
            ParamTypeConfig { node: "typed_parameter".to_string(), name_source: "child:0".to_string(), prefix: None },
            ParamTypeConfig { node: "default_parameter".to_string(), name_source: "field:name".to_string(), prefix: None },
            ParamTypeConfig { node: "typed_default_parameter".to_string(), name_source: "field:name".to_string(), prefix: None },
            ParamTypeConfig { node: "list_splat_pattern".to_string(), name_source: "child:0".to_string(), prefix: Some("*".to_string()) },
            ParamTypeConfig { node: "dictionary_splat_pattern".to_string(), name_source: "child:0".to_string(), prefix: Some("**".to_string()) },
        ]
    }

    fn python_param_separators_default() -> Vec<ParamSeparatorConfig> {
        vec![
            ParamSeparatorConfig { node: "positional_separator".to_string(), display: "/".to_string() },
            ParamSeparatorConfig { node: "keyword_separator".to_string(), display: "*".to_string() },
        ]
    }

    pub fn python_default() -> Self {
        SymbolsSection {
            function_node: Some("function_definition".to_string()),
            class_node: Some("class_definition".to_string()),
            decorated_node: Some("decorated_definition".to_string()),
            definition_field: Some("definition".to_string()),
            async_keyword: Some("async".to_string()),
            name_field: Some("name".to_string()),
            parameters_field: Some("parameters".to_string()),
            return_type_field: Some("return_type".to_string()),
            body_field: Some("body".to_string()),
            superclasses_field: Some("superclasses".to_string()),
            key_field: Some("key".to_string()),
            decorator_node: Some("decorator".to_string()),
            expression_statement_node: Some("expression_statement".to_string()),
            method_node: None,
            param_types: Self::python_param_types_default(),
            param_separators: Self::python_param_separators_default(),
            statements: StatementsConfig {
                simple: vec![
                    "expression_statement", "return_statement", "delete_statement",
                    "raise_statement", "pass_statement", "break_statement",
                    "continue_statement", "import_statement", "import_from_statement",
                    "future_import_statement", "global_statement", "nonlocal_statement",
                    "assert_statement", "type_alias_statement", "print_statement",
                ].into_iter().map(String::from).collect(),
                compound: vec![
                    "if_statement", "for_statement", "while_statement", "try_statement",
                    "with_statement", "function_definition", "class_definition",
                    "decorated_definition", "match_statement",
                ].into_iter().map(String::from).collect(),
                recurse_into: vec![
                    "block", "module", "elif_clause", "else_clause",
                    "except_clause", "finally_clause", "case_clause",
                ].into_iter().map(String::from).collect(),
            },
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LanguageSection {
    pub name: String,
    pub tree_sitter_grammar: String,
    pub file_extensions: Vec<String>,
    #[serde(default)]
    pub keywords: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScopingSection {
    pub scope_creators: Vec<ScopeCreator>,
    pub rules: HashMap<ScopeKind, ScopeRule>,
    pub declarations: DeclarationsSection,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScopeCreator {
    #[serde(rename = "node")]
    pub node_type: String,
    pub kind: ScopeKind,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScopeRule {
    pub is_closure_boundary: bool,
    pub names_visible_to_inner: bool,
    pub scoped_children: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeclarationsSection {
    pub global_keyword: Option<String>,
    pub nonlocal_keyword: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BindingsSection {
    pub assignment: Vec<AssignmentRule>,
    #[serde(rename = "loop")]
    pub loop_rules: Vec<AssignmentRule>,
    #[serde(default)]
    pub context_manager: Vec<AssignmentRule>,
    #[serde(default)]
    pub exception: Vec<AssignmentRule>,
    pub parameters: ParametersSection,
    pub definitions: DefinitionsSection,
    #[serde(default)]
    pub walrus: Vec<AssignmentRule>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssignmentRule {
    pub node: String,
    pub target: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParametersSection {
    pub param_nodes: Vec<String>,
    pub name_field: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DefinitionsSection {
    pub function_def: String,
    pub class_def: String,
    pub name_field: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportsSection {
    pub import_statement: String,
    #[serde(default)]
    pub import_from: String,
    pub module_field: String,
    pub name_field: String,
    #[serde(default)]
    pub alias_field: String,
    #[serde(default)]
    pub star_import: String,
    pub resolution: String,
    /// Child node type for dotted module paths (e.g. "dotted_name" in Python).
    #[serde(default)]
    pub dotted_name: Option<String>,
    /// Child node type for aliased imports (e.g. "aliased_import" in Python).
    #[serde(default)]
    pub aliased_import: Option<String>,
    /// Child node type for plain identifiers in from-imports (e.g. "identifier" in Python).
    #[serde(default)]
    pub identifier: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QualifiedNamesSection {
    pub module_separator: String,
    pub class_member_prefix: bool,
    pub nested_function_prefix: bool,
    pub locals_marker: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExportsSection {
    pub all_variable: Option<String>,
    pub public_by_default: bool,
    pub private_prefix: String,
    #[serde(default)]
    pub dunder_is_public: bool,
}

// ---------------------------------------------------------------------------
// Import graph
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct ImportGraph {
    pub module_to_file: HashMap<String, PathBuf>,
    pub file_to_module: HashMap<PathBuf, String>,
}

// ---------------------------------------------------------------------------
// Build context -- collects mutable state during tree walk
// ---------------------------------------------------------------------------

/// Mutable state threaded through the recursive tree walk.
struct BuildContext<'a> {
    source: &'a [u8],
    file_path: &'a Path,
    scopes: Vec<Scope>,
    /// O(1) scope lookup: ScopeId → index in `scopes`.
    scope_index: HashMap<ScopeId, usize>,
    next_scope_id: u32,
    imports: HashMap<String, ImportBinding>,
    definitions: Vec<(QualifiedName, Location)>,
}

impl<'a> BuildContext<'a> {
    fn new(source: &'a str, file_path: &'a Path) -> Self {
        Self {
            source: source.as_bytes(),
            file_path,
            scopes: Vec::new(),
            scope_index: HashMap::new(),
            next_scope_id: 0,
            imports: HashMap::new(),
            definitions: Vec::new(),
        }
    }

    /// Get a mutable reference to a scope by ID (O(1)).
    fn scope_mut(&mut self, id: ScopeId) -> Option<&mut Scope> {
        self.scope_index.get(&id).map(|&idx| &mut self.scopes[idx])
    }

    /// Get an immutable reference to a scope by ID (O(1)).
    fn scope(&self, id: ScopeId) -> Option<&Scope> {
        self.scope_index.get(&id).map(|&idx| &self.scopes[idx])
    }

    /// Push a new scope and register it in the index.
    fn push_scope(&mut self, scope: Scope) -> ScopeId {
        let id = scope.id;
        let idx = self.scopes.len();
        self.scopes.push(scope);
        self.scope_index.insert(id, idx);
        id
    }

    /// Allocate the next scope ID.
    fn alloc_scope_id(&mut self) -> ScopeId {
        let id = ScopeId(self.next_scope_id);
        self.next_scope_id += 1;
        id
    }

    /// Extract text from a tree-sitter node.
    fn text(&self, node: tree_sitter::Node) -> &str {
        node_text(node, self.source)
    }

    /// Insert a binding into a scope.
    fn add_binding(&mut self, scope_id: ScopeId, binding: Binding) {
        if let Some(scope) = self.scope_mut(scope_id) {
            scope.bindings.insert(binding.name.clone(), binding);
        }
    }

    /// Insert a binding only if the name is not already bound.
    fn add_binding_if_absent(&mut self, scope_id: ScopeId, binding: Binding) {
        if let Some(scope) = self.scope_mut(scope_id) {
            scope.bindings.entry(binding.name.clone()).or_insert(binding);
        }
    }

    /// Insert an import binding.
    fn add_import(&mut self, binding: ImportBinding) {
        self.imports.insert(binding.local_name.clone(), binding);
    }
}

// ---------------------------------------------------------------------------
// Scope Resolver
// ---------------------------------------------------------------------------

/// The main scope resolver.  Maintains a persistent, incremental index
/// of all scopes, bindings, and qualified names in the project.
#[derive(Debug)]
pub struct ScopeResolver {
    pub config: LanguageConfig,
    pub file_scopes: HashMap<PathBuf, FileScope>,
    pub import_graph: ImportGraph,
    pub qn_index: HashMap<String, Vec<Location>>,
    /// Reverse index: file → QN names defined in that file (for O(1) removal).
    file_qns: HashMap<PathBuf, Vec<String>>,
    pub project_root: PathBuf,
}

impl FileScope {
    pub fn to_rust_symbols(&self, config: &LanguageConfig) -> Vec<crate::symbols::RustSymbol> {
        let mut result = Vec::new();
        let sep = &config.qualified_names.module_separator;
        
        // Map scope IDs to their qualified names for nesting references
        let mut scope_qns: HashMap<ScopeId, String> = HashMap::new();
        if !self.scopes.is_empty() {
            // Find the root scope (usually the first one, Module)
            for scope in &self.scopes {
                if scope.parent.is_none() {
                    scope_qns.insert(scope.id, self.module_path.clone());
                }
            }
            
            // Build QNs for all other scopes
            let mut changed = true;
            while changed {
                changed = false;
                for scope in &self.scopes {
                    if scope_qns.contains_key(&scope.id) { continue; }
                    if let Some(parent_id) = scope.parent {
                        if let Some(parent_qn) = scope_qns.get(&parent_id) {
                            let parent = self.scopes.iter().find(|s| s.id == parent_id).unwrap();
                            if let Some(name) = find_scope_name(parent, scope, BindingKind::FunctionDef)
                                .or_else(|| find_scope_name(parent, scope, BindingKind::ClassDef))
                            {
                                scope_qns.insert(scope.id, format!("{}{}{}", parent_qn, sep, name));
                                changed = true;
                            }
                        }
                    }
                }
            }
        }

        for (qn, loc) in &self.definitions {
            // Find the binding in scopes to get more info (kind, params, etc)
            let mut kind = "variable".to_string();
            let mut line = loc.line + 1;
            let mut end_line = loc.line + 1;
            let mut col_offset = loc.column;
            let mut signature = None;
            let mut type_annotation = None;
            let mut returns = None;

            let parts: Vec<&str> = qn.name.split(sep).collect();
            let last_name = parts.last().unwrap_or(&"");

            // Search for the definition in the scopes
            for scope in &self.scopes {
                if let Some(binding) = scope.bindings.get(*last_name) {
                    if binding.byte_offset == loc.byte_offset {
                        kind = match (binding.kind, binding.is_async, scope.kind) {
                            (BindingKind::FunctionDef, true, ScopeKind::Class) => "async_method".to_string(),
                            (BindingKind::FunctionDef, false, ScopeKind::Class) => "method".to_string(),
                            (BindingKind::FunctionDef, true, _) => "async_function".to_string(),
                            (BindingKind::FunctionDef, false, _) => "function".to_string(),
                            (BindingKind::ClassDef, _, _) => "class".to_string(),
                            (BindingKind::Parameter, _, _) => "parameter".to_string(),
                            _ => "variable".to_string(),
                        };
                        line = binding.line + 1;
                        col_offset = binding.column;
                        signature = binding.signature.clone();
                        type_annotation = binding.type_annotation.clone();
                        returns = binding.returns.clone();
                        
                        // Use the created scope for accurate end line
                        if let Some(sid) = binding.created_scope {
                            if let Some(s) = self.scopes.iter().find(|s| s.id == sid) {
                                end_line = s.end_line + 1;
                                // For functions/classes, line should be the def line
                                line = s.start_line + 1;
                            }
                        }
                        break;
                    }
                }
            }

            let mut is_public = config.exports.public_by_default;
            if last_name.starts_with(&config.exports.private_prefix) {
                is_public = false;
            }
            if config.exports.dunder_is_public && last_name.starts_with("__") && last_name.ends_with("__") {
                is_public = true;
            }

            result.push(crate::symbols::RustSymbol {
                name: qn.name.clone(),
                kind,
                signature,
                type_annotation,
                returns,
                is_public,
                line,
                end_line,
                col_offset,
                children: Vec::new(),
                path: qn.name.split(sep).map(|s| s.to_string()).collect(),
                depth: qn.name.split(sep).count() - 1,
                decorators: Vec::new(),
                decorator_line_start: None,
                param_names: Vec::new(),
                bases: Vec::new(),
            });
        }

        // Add outer-scope references
        for r in &self.references {
            if r.kind == ReferenceKind::Read || r.kind == ReferenceKind::Call {
                // Find enclosing scope to build the path
                let mut enclosing_scope_id = self.scopes[0].id; // Default to Module
                let mut best_size = usize::MAX;
                for scope in &self.scopes {
                    if r.byte_offset >= scope.start_byte && r.byte_offset <= scope.end_byte {
                        let size = scope.end_byte - scope.start_byte;
                        if size < best_size {
                            best_size = size;
                            enclosing_scope_id = scope.id;
                        }
                    }
                }
                
                let enclosing_scope = self.scopes.iter().find(|s| s.id == enclosing_scope_id).unwrap();
                let parts: Vec<&str> = r.qn.name.split(sep).collect();
                let last_name = parts.last().unwrap_or(&"");
                
                // Only include if it's NOT bound in the immediate scope
                if !enclosing_scope.bindings.contains_key(*last_name) {
                     let enclosing_qn = scope_qns.get(&enclosing_scope_id).cloned().unwrap_or(self.module_path.clone());
                     let mut path: Vec<String> = enclosing_qn.split(sep).map(|s| s.to_string()).collect();
                     path.push(last_name.to_string());
                     
                     result.push(crate::symbols::RustSymbol {
                         name: r.qn.name.clone(),
                         kind: "reference".to_string(),
                         signature: None,
                         type_annotation: None,
                         returns: None,
                         is_public: false,
                         line: r.line,
                         end_line: r.line,
                         col_offset: r.column,
                         children: Vec::new(),
                         depth: path.len() - 1,
                         path,
                         decorators: Vec::new(),
                         decorator_line_start: None,
                         param_names: Vec::new(),
                         bases: Vec::new(),
                     });
                }
            }
        }

        result
    }
}

impl ScopeResolver {
    pub fn new(config: LanguageConfig, project_root: PathBuf) -> Self {
        Self {
            config,
            file_scopes: HashMap::new(),
            import_graph: ImportGraph::default(),
            qn_index: HashMap::new(),
            file_qns: HashMap::new(),
            project_root,
        }
    }

    fn derive_module_path(&self, file_path: &Path, separator: &str) -> String {
        derive_module_path(
            file_path,
            &self.project_root,
            separator,
            &self.config.imports.resolution,
            &self.config.language.file_extensions,
        )
    }

    pub fn get_symbols(&self, path: &Path) -> Vec<crate::symbols::RustSymbol> {
        if let Some(file_scope) = self.file_scopes.get(path) {
            file_scope.to_rust_symbols(&self.config)
        } else {
            Vec::new()
        }
    }

    /// Index a single file.  Incremental: only re-indexes if content hash changed.
    pub fn index_file(&mut self, path: &Path, source: &str, tree: &tree_sitter::Tree) {
        let hash = content_hash(source);

        if let Some(existing) = self.file_scopes.get(path) {
            if existing.content_hash == hash {
                return;
            }
        }

        let module_path = self.derive_module_path(path, &self.config.qualified_names.module_separator);
        let file_scope = self.build_file_scope(path, source, tree, hash, module_path.clone());

        // Update QN index: remove old entries for this file, add new ones
        self.remove_file_from_qn_index(path);
        let mut qns_for_file = Vec::with_capacity(file_scope.definitions.len());
        for (qn, loc) in &file_scope.definitions {
            qns_for_file.push(qn.name.clone());
            self.qn_index
                .entry(qn.name.clone())
                .or_default()
                .push(loc.clone());
        }
        self.file_qns.insert(path.to_path_buf(), qns_for_file);

        // Update import graph
        self.import_graph
            .module_to_file
            .insert(module_path.clone(), path.to_path_buf());
        self.import_graph
            .file_to_module
            .insert(path.to_path_buf(), module_path);

        self.file_scopes.insert(path.to_path_buf(), file_scope);
    }

    #[allow(dead_code)]
    pub fn qualified_names(&self, path: &Path) -> Vec<(QualifiedName, Location)> {
        let _ = path;
        Vec::new() // TODO
    }

    #[allow(dead_code)]
    pub fn find_references(&self, qn: &str) -> Vec<Reference> {
        let _ = qn;
        Vec::new() // TODO
    }

    #[allow(dead_code)]
    pub fn goto_definition(&self, path: &Path, line: usize, column: usize) -> Option<Location> {
        let _ = (path, line, column);
        None // TODO
    }

    #[allow(dead_code)]
    pub fn find_dead_code(&self) -> Vec<(QualifiedName, Location)> {
        Vec::new() // TODO
    }

    // -----------------------------------------------------------------------
    // Internal methods
    // -----------------------------------------------------------------------

    fn build_file_scope(
        &self,
        path: &Path,
        source: &str,
        tree: &tree_sitter::Tree,
        content_hash: [u8; 16],
        module_path: String,
    ) -> FileScope {
        let mut ctx = BuildContext::new(source, path);

        // Create the module-level scope
        let root_node = tree.root_node();
        let module_id = ctx.alloc_scope_id();
        ctx.push_scope(Scope {
            id: module_id,
            kind: ScopeKind::Module,
            parent: None,
            start_byte: root_node.start_byte(),
            end_byte: root_node.end_byte(),
            start_line: root_node.start_position().row,
            end_line: root_node.end_position().row,
            bindings: HashMap::new(),
        });

        // Walk the tree-sitter CST and build scopes
        let mut cursor = root_node.walk();
        self.walk_node(&mut cursor, module_id, &mut ctx);

        // Second pass: resolve all identifier/attribute references to QNs.
        let (references, all_qnames) = self.collect_file_references(
            tree, path, source, &module_path, &ctx.scopes, &ctx.scope_index, &ctx.imports,
        );

        FileScope {
            content_hash,
            module_path,
            scopes: ctx.scopes,
            imports: ctx.imports,
            definitions: ctx.definitions,
            references,
            all_qnames,
        }
    }

    // -----------------------------------------------------------------------
    // Reference collection (second pass)
    // -----------------------------------------------------------------------

    /// Collect all identifier/attribute references in a file and resolve
    /// them to qualified names.  Returns (references, all_qnames).
    fn collect_file_references(
        &self,
        tree: &tree_sitter::Tree,
        file_path: &Path,
        source: &str,
        module_path: &str,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        imports: &HashMap<String, ImportBinding>,
    ) -> (Vec<Reference>, Vec<String>) {
        use std::collections::HashSet;
        let source_bytes = source.as_bytes();

        let mut refs = Vec::new();
        let mut qn_set = HashSet::new();

        // Add definition QNs to the set too.
        // (Definitions are already recorded but their QNs should be in all_qnames.)

        let root = tree.root_node();
        self.walk_references(
            &mut root.walk(), file_path, source_bytes,
            module_path, scopes, scope_index, imports,
            ScopeId(0), // Start with Module scope
            false, // not in import
            &mut refs, &mut qn_set,
        );

        let all_qnames: Vec<String> = qn_set.into_iter().collect();
        (refs, all_qnames)
    }

    /// Recursively walk tree-sitter nodes to collect references.
    fn walk_references(
        &self,
        cursor: &mut tree_sitter::TreeCursor,
        file_path: &Path,
        source: &[u8],
        module_path: &str,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        imports: &HashMap<String, ImportBinding>,
        current_scope: ScopeId,
        in_import: bool,
        refs: &mut Vec<Reference>,
        qn_set: &mut std::collections::HashSet<String>,
    ) {
        let node = cursor.node();
        let node_kind = node.kind();

        // Check if this node created a new scope in Pass 1.
        let is_scope_creator = self.config.scoping.scope_creators.iter().any(|c| c.node_type == node_kind);
        let scope_for_children = if is_scope_creator {
            self.find_scope_at_node(&node, scopes).unwrap_or(current_scope)
        } else {
            current_scope
        };

        let cfg_imports = &self.config.imports;
        let is_plain_import = node_kind == cfg_imports.import_statement
            && (cfg_imports.import_from.is_empty() || node_kind != cfg_imports.import_from);
        let is_from_import = !cfg_imports.import_from.is_empty()
            && node_kind == cfg_imports.import_from;

        // Track whether we're inside an import statement.
        let child_in_import = in_import || is_plain_import || is_from_import;

        // Special handling for imports: record module name references
        if is_plain_import {
            for_each_child(&node, |child| {
                let ck = child.kind();
                if cfg_imports.dotted_name.as_deref() == Some(ck) {
                    let qn = node_text(child, source).to_string();
                    qn_set.insert(qn.clone());
                    refs.push(Reference {
                        file: file_path.to_path_buf(),
                        line: child.start_position().row + 1,
                        column: child.start_position().column,
                        byte_offset: child.start_byte(),
                        end_byte: child.end_byte(),
                        qn: QualifiedName { name: qn },
                        kind: ReferenceKind::Import,
                    });
                } else if cfg_imports.aliased_import.as_deref() == Some(ck) {
                    if let Some(name_node) = child.child_by_field_name("name") {
                        let qn = node_text(name_node, source).to_string();
                        qn_set.insert(qn.clone());
                        refs.push(Reference {
                            file: file_path.to_path_buf(),
                            line: name_node.start_position().row + 1,
                            column: name_node.start_position().column,
                            byte_offset: name_node.start_byte(),
                            end_byte: name_node.end_byte(),
                            qn: QualifiedName { name: qn },
                            kind: ReferenceKind::Import,
                        });
                    }
                    // Alias part will be processed as a definition in collect_bindings,
                    // and we don't want a reference for the alias name here.
                }
            });
            // We've handled the children we care about
            return;
        }

        if is_from_import {
            let mod_node_id = if let Some(mod_node) = node.child_by_field_name(&cfg_imports.module_field) {
                let qn = node_text(mod_node, source).to_string();
                qn_set.insert(qn.clone());
                refs.push(Reference {
                    file: file_path.to_path_buf(),
                    line: mod_node.start_position().row + 1,
                    column: mod_node.start_position().column,
                    byte_offset: mod_node.start_byte(),
                    end_byte: mod_node.end_byte(),
                    qn: QualifiedName { name: qn },
                    kind: ReferenceKind::Import,
                });
                Some(mod_node.id())
            } else {
                None
            };

            // For the imported names, we still want to process them
            for_each_child(&node, |child| {
                if mod_node_id == Some(child.id()) {
                    return;
                }
                let ck = child.kind();
                let is_name_node = cfg_imports.identifier.as_deref() == Some(ck)
                    || cfg_imports.dotted_name.as_deref() == Some(ck)
                    || cfg_imports.aliased_import.as_deref() == Some(ck);
                if is_name_node {
                    // These resolve to symbols in the module
                    self.walk_references(
                        &mut child.walk(), file_path, source, module_path,
                        scopes, scope_index, imports, current_scope, true,
                        refs, qn_set,
                    );
                }
            });
            return;
        }

        // Process identifier nodes (but not those that are the name part of an
        // attribute access — we handle those at the attribute level).
        if node_kind == self.config.pattern_matching.identifier {
            // Skip if it's the 'attribute' field of an 'attribute' node
            let attr_kind = &self.config.pattern_matching.attribute;
            let attr_field = &self.config.pattern_matching.attr_field;
            let is_attr_name = node.parent().map_or(false, |p| {
                !attr_kind.is_empty() && p.kind() == attr_kind && p.child_by_field_name(attr_field) == Some(node)
            });
            // Skip language keywords that tree-sitter reports as identifiers
            let name = node_text(node, source);
            let is_keyword = self.config.language.keywords.iter().any(|k| k == name);

            if !is_attr_name && !is_keyword {
                let kind = self.classify_reference(&node, in_import);
                
                if let Some(qn) = self.resolve_identifier(
                    name, node.start_byte(), module_path,
                    scopes, scope_index, imports,
                    current_scope,
                ) {
                    qn_set.insert(qn.clone());
                    refs.push(Reference {
                        file: file_path.to_path_buf(),
                        // Convert tree-sitter 0-indexed row to 1-indexed line.
                        line: node.start_position().row + 1,
                        column: node.start_position().column,
                        byte_offset: node.start_byte(),
                        end_byte: node.end_byte(),
                        qn: QualifiedName { name: qn },
                        kind,
                    });
                }
            }
        }

        // Process attribute access nodes (e.g., `obj.attr`)
        if !self.config.pattern_matching.attribute.is_empty() && node_kind == self.config.pattern_matching.attribute {
            // Build the full dotted name and resolve.
            let kind = self.classify_reference(&node, in_import);
            if let Some(full_name) = self.collect_dotted_name(&node, source) {
                if let Some(qn) = self.resolve_dotted_name(
                    &full_name, node.start_byte(), module_path,
                    scopes, scope_index, imports,
                    current_scope,
                ) {
                    qn_set.insert(qn.clone());
                    refs.push(Reference {
                        file: file_path.to_path_buf(),
                        // Convert tree-sitter 0-indexed row to 1-indexed line.
                        line: node.start_position().row + 1,
                        column: node.start_position().column,
                        byte_offset: node.start_byte(),
                        end_byte: node.end_byte(),
                        qn: QualifiedName { name: qn },
                        kind,
                    });
                }
            }
        }

        // Recurse into children.
        if cursor.goto_first_child() {
            loop {
                let child = cursor.node();
                let next_scope = if is_scope_creator {
                    // Switch to new scope only for children that are truly "inside" or part of parameters.
                    // Most languages bind parameters in the function scope.
                    // The name itself is bound in the outer scope.
                    let name_field = self.config.symbols.name_field();
                    if node.child_by_field_name(name_field) == Some(child) {
                        current_scope
                    } else if child.kind() == "identifier" && node_kind != "module" && node_kind != "program" {
                        // Heuristic: if it's an identifier but not the 'name' field, it might be the name.
                        // Let's check if it IS the name.
                        let mut name_cursor = node.walk();
                        let first_id = node.children(&mut name_cursor).find(|c| c.kind() == "identifier");
                        if first_id == Some(child) {
                            current_scope
                        } else {
                            scope_for_children
                        }
                    } else {
                        scope_for_children
                    }
                } else {
                    current_scope
                };

                self.walk_references(
                    cursor, file_path, source, module_path,
                    scopes, scope_index, imports, next_scope, child_in_import,
                    refs, qn_set,
                );
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
            cursor.goto_parent();
        }
    }

    /// Classify a reference's kind based on its parent context.
    fn classify_reference(&self, node: &tree_sitter::Node, in_import: bool) -> ReferenceKind {
        if in_import {
            return ReferenceKind::Import;
        }
        if let Some(parent) = node.parent() {
            let pk = parent.kind();
            // Call target: direct function call
            if pk == "call" {
                if let Some(func) = parent.child_by_field_name("function") {
                    if func.id() == node.id() {
                        return ReferenceKind::Call;
                    }
                }
            }
            // Attribute is the call target (e.g., obj.method())
            if pk == "attribute" {
                if let Some(grandparent) = parent.parent() {
                    if grandparent.kind() == "call" {
                        if let Some(func) = grandparent.child_by_field_name("function") {
                            if func.id() == parent.id() {
                                return ReferenceKind::Call;
                            }
                        }
                    }
                }
            }
            // Function/class definitions (the name itself is a definition)
            if pk == "function_definition" || pk == "class_definition" {
                if let Some(name) = parent.child_by_field_name("name") {
                    if name.id() == node.id() {
                        return ReferenceKind::Definition;
                    }
                }
            }
        }
        if self.is_write_context(node) {
            ReferenceKind::Write
        } else {
            ReferenceKind::Read
        }
    }

    /// Check if a node is in a write (assignment) context.
    fn is_write_context(&self, node: &tree_sitter::Node) -> bool {
        let mut current = *node;
        let mut in_parameter = false;
        
        while let Some(parent) = current.parent() {
            let pk = parent.kind();

            // Check if we are inside a parameter container
            let params_field = self.config.symbols.parameters_field();
            if pk == params_field || pk == "parameters" || pk == "lambda_parameters" || pk == "formal_parameters" {
                in_parameter = true;
                break;
            }

            // Check if any intermediate node is a parameter node
            if self.config.bindings.parameters.param_nodes.contains(&pk.to_string()) {
                 in_parameter = true;
                 break;
            }

            // Check configured binding rules
            let rules = [
                &self.config.bindings.assignment,
                &self.config.bindings.loop_rules,
                &self.config.bindings.context_manager,
                &self.config.bindings.exception,
                &self.config.bindings.walrus,
            ];

            for rule_list in rules {
                if let Some(rule) = rule_list.iter().find(|r| r.node == pk) {
                    if let Some(target) = parent.child_by_field_name(&rule.target) {
                        if target.byte_range().contains(&node.start_byte()) {
                            return true;
                        }
                    }
                    return false;
                }
            }

            match pk {
                "function_definition" | "class_definition" | "call" | "attribute" => {
                    // These are boundaries where we know it's not a general write context
                    return false;
                }
                _ => {
                    // Continue walking up
                }
            }
            current = parent;
        }

        if in_parameter {
            // In a parameter context, identifiers are usually definitions/writes
            // unless they are part of a type annotation or default value.
            // But for simple cases, returning true here is better than false.
            return true;
        }

        false
    }

    /// Collect a dotted attribute name from an `attribute` node.
    /// Returns the full dotted string (e.g., "os.path.join") or None.
    fn collect_dotted_name(&self, node: &tree_sitter::Node, source: &[u8]) -> Option<String> {
        // tree-sitter attribute node: `object.attribute`
        // object can be another attribute (for chained access) or identifier
        let attr_name = node.child_by_field_name("attribute")?;
        let attr_text = node_text(attr_name, source);

        let object = node.child_by_field_name("object")?;
        match object.kind() {
            "identifier" => {
                let obj_text = node_text(object, source);
                Some(format!("{}.{}", obj_text, attr_text))
            }
            "attribute" => {
                let prefix = self.collect_dotted_name(&object, source)?;
                Some(format!("{}.{}", prefix, attr_text))
            }
            _ => None, // Can't resolve (e.g., `foo()[0].bar`)
        }
    }

    /// Resolve a simple identifier to its qualified name.
    fn resolve_identifier(
        &self,
        name: &str,
        _byte_offset: usize,
        module_path: &str,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        imports: &HashMap<String, ImportBinding>,
        scope_id: ScopeId,
    ) -> Option<String> {
        // 1. Look up in scope tree (innermost first)
        if let Some(qn) = self.resolve_in_scope_chain(
            name, scope_id, module_path, scopes, scope_index,
        ) {
            return Some(qn);
        }

        // 2. Check imports
        if let Some(imp) = imports.get(name) {
            if imp.is_star {
                return None; // Can't resolve star imports
            }
            return Some(if let Some(ref imported_name) = imp.imported_name {
                // `from foo import bar` → foo.bar
                format!("{}.{}", imp.module_path, imported_name)
            } else {
                // `import foo` → foo
                imp.module_path.clone()
            });
        }

        // 3. Builtins (common ones)
        if self.config.builtins.names.iter().any(|b| b == name) {
            return Some(format!("builtins.{}", name));
        }

        None
    }

    /// Resolve a dotted name (e.g., "os.path.join") to its qualified name.
    fn resolve_dotted_name(
        &self,
        dotted: &str,
        _byte_offset: usize,
        module_path: &str,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        imports: &HashMap<String, ImportBinding>,
        scope_id: ScopeId,
    ) -> Option<String> {
        let parts: Vec<&str> = dotted.splitn(2, '.').collect();
        if parts.len() < 2 {
            return self.resolve_identifier(dotted, _byte_offset, module_path, scopes, scope_index, imports, scope_id);
        }
        let root = parts[0];
        let rest = parts[1];

        // Check imports for root
        if let Some(imp) = imports.get(root) {
            if imp.is_star {
                return None;
            }
            return Some(if let Some(ref imported_name) = imp.imported_name {
                // `from foo import bar` + `bar.baz` → `foo.bar.baz`
                format!("{}.{}.{}", imp.module_path, imported_name, rest)
            } else {
                // `import os` + `os.path.join` → `os.path.join`
                format!("{}.{}", imp.module_path, rest)
            });
        }

        // Check local bindings for root
        if let Some(root_qn) = self.resolve_in_scope_chain(
            root, scope_id, module_path, scopes, scope_index,
        ) {
            return Some(format!("{}.{}", root_qn, rest));
        }

        None
    }

    /// Find the innermost scope containing a byte offset.
    #[allow(dead_code)]
    fn find_enclosing_scope(&self, byte_offset: usize, scopes: &[Scope]) -> Option<ScopeId> {
        // Search from the end (innermost scopes are added last)
        let mut best: Option<(ScopeId, usize)> = None; // (id, byte_range_size)
        for scope in scopes {
            if byte_offset >= scope.start_byte && byte_offset <= scope.end_byte {
                let size = scope.end_byte - scope.start_byte;
                if best.is_none() || size < best.unwrap().1 {
                    best = Some((scope.id, size));
                }
            }
        }
        best.map(|(id, _)| id)
    }

    /// Walk up the scope chain to resolve a name, respecting closure boundaries.
    fn resolve_in_scope_chain(
        &self,
        name: &str,
        start_scope: ScopeId,
        module_path: &str,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
    ) -> Option<String> {
        let mut current = Some(start_scope);
        while let Some(sid) = current {
            let idx = *scope_index.get(&sid)?;
            let scope = &scopes[idx];

            if let Some(binding) = scope.bindings.get(name) {
                // Found the binding. Compute QN based on binding kind.
                match binding.kind {
                    BindingKind::Import => {
                        // Import bindings are handled separately
                        return None;
                    }
                    BindingKind::FunctionDef | BindingKind::ClassDef => {
                        // Compute QN from the scope chain
                        return Some(self.compute_qn_str(
                            scopes, scope_index, module_path, sid, name,
                        ));
                    }
                    _ => {
                        // Local variable — compute QN if at module level
                        if scope.kind == ScopeKind::Module {
                            return Some(format!("{}.{}", module_path, name));
                        }
                        // Local variables in functions get <locals> QN
                        return Some(self.compute_qn_str(
                            scopes, scope_index, module_path, sid, name,
                        ));
                    }
                }
            }

            // Check closure boundary
            if let Some(rule) = self.config.scoping.rules.get(&scope.kind) {
                if rule.is_closure_boundary && !rule.names_visible_to_inner {
                    // Class scope in Python: stop searching
                    break;
                }
            }

            current = scope.parent;
        }
        None
    }

    /// Compute a QN string from a scope chain + name (without creating QualifiedName).
    fn compute_qn_str(
        &self,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        module_path: &str,
        scope_id: ScopeId,
        name: &str,
    ) -> String {
        let mut parts = Vec::new();
        parts.push(name.to_string());
        let mut current = Some(scope_id);
        let sep = &self.config.qualified_names.module_separator;

        while let Some(sid) = current {
            let idx = match scope_index.get(&sid) {
                Some(&i) => i,
                None => break,
            };
            let scope = &scopes[idx];

            match scope.kind {
                ScopeKind::Module => {
                    parts.push(module_path.to_string());
                    break;
                }
                ScopeKind::Function if self.config.qualified_names.nested_function_prefix => {
                    if let Some(parent_id) = scope.parent {
                        if let Some(&pidx) = scope_index.get(&parent_id) {
                            let parent = &scopes[pidx];
                            if let Some(bname) = find_scope_name(parent, scope, BindingKind::FunctionDef) {
                                parts.push(bname.to_string());
                            }
                        }
                    }
                }
                ScopeKind::Class if self.config.qualified_names.class_member_prefix => {
                    if let Some(parent_id) = scope.parent {
                        if let Some(&pidx) = scope_index.get(&parent_id) {
                            let parent = &scopes[pidx];
                            if let Some(bname) = find_scope_name(parent, scope, BindingKind::ClassDef) {
                                parts.push(bname.to_string());
                            }
                        }
                    }
                }
                _ => {}
            }
            current = scope.parent;
        }

        parts.reverse();
        parts.join(sep)
    }

    /// Recursively walk tree-sitter nodes to build scope tree and collect
    /// bindings and imports.
    fn walk_node(
        &self,
        cursor: &mut tree_sitter::TreeCursor,
        current_scope: ScopeId,
        ctx: &mut BuildContext,
    ) {
        let node = cursor.node();
        let node_kind = node.kind();

        // Check if this node creates a new scope
        let scope_for_children =
            if let Some(creator) = self.config.scoping.scope_creators.iter().find(|c| c.node_type == node_kind) {
                if creator.kind == ScopeKind::Module {
                    current_scope
                } else {
                    let scope_id = ctx.alloc_scope_id();
                    ctx.push_scope(Scope {
                        id: scope_id,
                        kind: creator.kind,
                        parent: Some(current_scope),
                        start_byte: node.start_byte(),
                        end_byte: node.end_byte(),
                        start_line: node.start_position().row,
                        end_line: node.end_position().row,
                        bindings: HashMap::new(),
                    });

                    // Function/class names are bound in the ENCLOSING scope
                    if creator.kind == ScopeKind::Function || creator.kind == ScopeKind::Class {
                        let name_field = self.config.symbols.name_field();
                        let mut name_node = node.child_by_field_name(name_field);
                        
                        // Fallback: search for identifier child if field not found (e.g. TS)
                        if name_node.is_none() {
                            let mut cursor = node.walk();
                            name_node = node.children(&mut cursor).find(|c| c.kind() == "identifier");
                        }

                        if let Some(name_node) = name_node {
                            let name = ctx.text(name_node).to_string();
                            let mut signature = None;
                            let mut returns = None;

                            // Detect async: look for "async" keyword
                            let is_async = {
                                let mut c = node.walk();
                                let mut found = false;
                                if c.goto_first_child() {
                                    loop {
                                        if c.node().kind() == "async" {
                                            found = true;
                                            break;
                                        }
                                        if !c.goto_next_sibling() {
                                            break;
                                        }
                                    }
                                }
                                found
                            };

                            if creator.kind == ScopeKind::Function {
                                let params_field = self.config.symbols.parameters_field();
                                let mut params_node = node.child_by_field_name(params_field);
                                // TS fallback
                                if params_node.is_none() {
                                    let mut cursor = node.walk();
                                    params_node = node.children(&mut cursor).find(|c| c.kind() == "formal_parameters");
                                }

                                if let Some(params) = params_node {
                                    signature = Some(ctx.text(params).to_string());
                                }
                                if let Some(ret) = node.child_by_field_name("return_type") {
                                    returns = Some(ctx.text(ret).trim_start_matches("->").trim().to_string());
                                    // Append return type to signature for consistency with old behavior
                                    if let Some(ref mut sig) = signature {
                                        sig.push_str(&format!(" -> {}", returns.as_ref().unwrap()));
                                    }
                                }
                            }

                            let binding = Binding {
                                name: name.clone(),
                                kind: if creator.kind == ScopeKind::Function {
                                    BindingKind::FunctionDef
                                } else {
                                    BindingKind::ClassDef
                                },
                                line: name_node.start_position().row,
                                column: name_node.start_position().column,
                                byte_offset: name_node.start_byte(),
                                signature,
                                type_annotation: None,
                                returns,
                                is_async,
                                created_scope: Some(scope_id),
                            };

                            ctx.add_binding(current_scope, binding);

                            let qn = self.compute_qn(&ctx.scopes, &ctx.scope_index, ctx.file_path, current_scope, &name);
                            let loc = Location {
                                file: ctx.file_path.to_path_buf(),
                                line: name_node.start_position().row,
                                column: name_node.start_position().column,
                                byte_offset: name_node.start_byte(),
                            };
                            ctx.definitions.push((qn, loc));
                        }
                    }

                    if creator.kind == ScopeKind::Function {
                        let params_field = self.config.symbols.parameters_field();
                        let mut params_node = node.child_by_field_name(params_field);
                        // TS fallback
                        if params_node.is_none() {
                            let mut cursor = node.walk();
                            params_node = node.children(&mut cursor).find(|c| c.kind() == "formal_parameters");
                        }

                        if let Some(params) = params_node {
                            self.collect_parameters(ctx, &params, scope_id);
                        }
                    }

                    scope_id
                }
            } else {
                current_scope
            };


        // Collect imports
        if node_kind == self.config.imports.import_statement
            || (!self.config.imports.import_from.is_empty()
                && node_kind == self.config.imports.import_from)
        {
            self.collect_import(ctx, &node);
        }

        // Collect assignments and other bindings
        let binding_rules = [
            (&self.config.bindings.assignment, BindingKind::Assignment),
            (&self.config.bindings.loop_rules, BindingKind::LoopVariable),
            (&self.config.bindings.context_manager, BindingKind::ContextManager),
            (&self.config.bindings.exception, BindingKind::ExceptionHandler),
            (&self.config.bindings.walrus, BindingKind::Walrus),
        ];

        for (rule_list, kind) in binding_rules {
            if let Some(rule) = rule_list.iter().find(|r| r.node == node_kind) {
                if let Some(target) = node.child_by_field_name(&rule.target) {
                    let type_annotation = node.child_by_field_name("type").map(|n| ctx.text(n).to_string());
                    self.collect_binding_targets(ctx, &target, scope_for_children, kind, type_annotation);
                }
            }
        }

        // Collect global/nonlocal declarations
        if let Some(ref global_kw) = self.config.scoping.declarations.global_keyword {
            if node_kind == *global_kw {
                for_each_child_of_kind(&node, "identifier", |child| {
                    let name = ctx.text(child);
                    let binding = Binding {
                        name: name.to_string(),
                        kind: BindingKind::Global,
                        line: child.start_position().row,
                        column: child.start_position().column,
                        byte_offset: child.start_byte(),
                        signature: None,
                        type_annotation: None,
                        returns: None,
                        is_async: false,
                        created_scope: None,
                    };
                    ctx.add_binding(scope_for_children, binding);
                });
            }
        }
        if let Some(ref nonlocal_kw) = self.config.scoping.declarations.nonlocal_keyword {
            if node_kind == *nonlocal_kw {
                for_each_child_of_kind(&node, "identifier", |child| {
                    let name = ctx.text(child);
                    let binding = Binding {
                        name: name.to_string(),
                        kind: BindingKind::Nonlocal,
                        line: child.start_position().row,
                        column: child.start_position().column,
                        byte_offset: child.start_byte(),
                        signature: None,
                        type_annotation: None,
                        returns: None,
                        is_async: false,
                        created_scope: None,
                    };
                    ctx.add_binding(scope_for_children, binding);
                });
            }
        }

        // Recurse into children
        if cursor.goto_first_child() {
            loop {
                self.walk_node(cursor, scope_for_children, ctx);
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
            cursor.goto_parent();
        }
    }

    /// Collect recursive binding targets (e.g., in a = (b, c)).
    fn collect_binding_targets(
        &self,
        ctx: &mut BuildContext,
        node: &tree_sitter::Node,
        scope_id: ScopeId,
        kind: BindingKind,
        type_annotation: Option<String>,
    ) {
        match node.kind() {
            "identifier" => {
                let name = ctx.text(*node).to_string();

                let binding = Binding {
                    name: name.clone(),
                    kind,
                    line: node.start_position().row,
                    column: node.start_position().column,
                    byte_offset: node.start_byte(),
                    signature: None,
                    type_annotation,
                    returns: None,
                    is_async: false,
                    created_scope: None,
                };
                
                let is_new = if let Some(scope) = ctx.scope(scope_id) {
                    !scope.bindings.contains_key(&binding.name)
                } else {
                    false
                };
                
                ctx.add_binding_if_absent(scope_id, binding);
                
                if is_new {
                    let qn = self.compute_qn(&ctx.scopes, &ctx.scope_index, ctx.file_path, scope_id, &name);
                    let loc = Location {
                        file: ctx.file_path.to_path_buf(),
                        line: node.start_position().row,
                        column: node.start_position().column,
                        byte_offset: node.start_byte(),
                    };
                    ctx.definitions.push((qn, loc));
                }
            }
            "pattern_list" | "tuple_pattern" | "list_pattern" | "list_splat_pattern"
            | "as_pattern" | "as_pattern_target" => {
                for_each_child(node, |child| {
                    let ck = child.kind();
                    if ck != "," && ck != "(" && ck != ")" && ck != "[" && ck != "]" && ck != "as" {
                        self.collect_binding_targets(ctx, &child, scope_id, kind, None);
                    }
                });
            }
            _ => {}
        }
    }

    /// Collect function parameters as bindings.
    fn collect_parameters(
        &self,
        ctx: &mut BuildContext,
        params_node: &tree_sitter::Node,
        scope_id: ScopeId,
    ) {
        let mut cursor = params_node.walk();
        if !cursor.goto_first_child() {
            return;
        }
        loop {
            let child = cursor.node();
            let ck = child.kind();

            if self.config.bindings.parameters.param_nodes.contains(&ck.to_string()) {
                let mut name_node = if ck == "identifier" {
                    Some(child)
                } else {
                    child.child_by_field_name(&self.config.bindings.parameters.name_field)
                };
                
                // Fallback for TS: required_parameter often has identifier as first named child without field
                if name_node.is_none() {
                    name_node = child.named_child(0);
                }

                if let Some(n) = name_node {
                    let name = ctx.text(n).to_string();
                    let binding = Binding {
                        name: name.clone(),
                        kind: BindingKind::Parameter,
                        line: n.start_position().row,
                        column: n.start_position().column,
                        byte_offset: n.start_byte(),
                        signature: None,
                        type_annotation: None,
                        returns: None,
                        is_async: false,
                        created_scope: None,
                    };
                    ctx.add_binding(scope_id, binding);

                    // Add to definitions so it appears in symbol index
                    let qn = self.compute_qn(&ctx.scopes, &ctx.scope_index, ctx.file_path, scope_id, &name);
                    let loc = Location {
                        file: ctx.file_path.to_path_buf(),
                        line: n.start_position().row + 1,
                        column: n.start_position().column + 1,
                        byte_offset: n.start_byte(),
                    };
                    ctx.definitions.push((qn, loc));
                }
            }

            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }

    /// Collect imports from an import statement node.
    fn collect_import(&self, ctx: &mut BuildContext, node: &tree_sitter::Node) {
        let imports = &self.config.imports;
        let node_kind = node.kind();

        // Is this a bare `import X` style (no "from")?
        let is_plain_import = node_kind == imports.import_statement
            && (imports.import_from.is_empty() || node_kind != imports.import_from);

        // Is this a `from X import Y` style?
        let is_from_import = !imports.import_from.is_empty()
            && node_kind == imports.import_from;

        if is_plain_import {
            for_each_child(node, |child| {
                let ck = child.kind();
                let is_aliased = imports.aliased_import.as_deref() == Some(ck);
                let is_dotted = imports.dotted_name.as_deref() == Some(ck);

                if !is_aliased && !is_dotted {
                    return;
                }

                let (local_name, module_path) = if is_aliased {
                    let name_node = child.child_by_field_name("name");
                    let alias_node = if !imports.alias_field.is_empty() {
                        child.child_by_field_name(&imports.alias_field)
                    } else {
                        None
                    };
                    match (name_node, alias_node) {
                        (Some(n), Some(a)) => (ctx.text(a).to_string(), ctx.text(n).to_string()),
                        (Some(n), None) => {
                            let text = ctx.text(n).to_string();
                            let local = text.split('.').next().unwrap_or(&text).to_string();
                            (local, text)
                        }
                        _ => return,
                    }
                } else {
                    let text = ctx.text(child).to_string();
                    let local = text.split('.').next().unwrap_or(&text).to_string();
                    (local, text)
                };

                ctx.add_import(ImportBinding {
                    local_name,
                    module_path,
                    imported_name: None,
                    is_star: false,
                });
            });
        } else if is_from_import {
            let module_path = node
                .child_by_field_name(&imports.module_field)
                .map(|n| ctx.text(n).to_string())
                .unwrap_or_default();

            for_each_child(node, |child| {
                let ck = child.kind();
                let is_star = !imports.star_import.is_empty() && ck == imports.star_import;
                let is_aliased = imports.aliased_import.as_deref() == Some(ck);
                let is_plain_name = imports.dotted_name.as_deref() == Some(ck)
                    || imports.identifier.as_deref() == Some(ck);

                if is_star {
                    ctx.add_import(ImportBinding {
                        local_name: "*".to_string(),
                        module_path: module_path.clone(),
                        imported_name: None,
                        is_star: true,
                    });
                } else if is_plain_name {
                    let name = ctx.text(child).to_string();
                    ctx.add_import(ImportBinding {
                        local_name: name.clone(),
                        module_path: module_path.clone(),
                        imported_name: Some(name),
                        is_star: false,
                    });
                } else if is_aliased {
                    if let Some(name_node) = child.child_by_field_name("name") {
                        let imported = ctx.text(name_node).to_string();
                        let local = if !imports.alias_field.is_empty() {
                            child
                                .child_by_field_name(&imports.alias_field)
                                .map(|a| ctx.text(a).to_string())
                                .unwrap_or_else(|| imported.clone())
                        } else {
                            imported.clone()
                        };
                        ctx.add_import(ImportBinding {
                            local_name: local,
                            module_path: module_path.clone(),
                            imported_name: Some(imported),
                            is_star: false,
                        });
                    }
                }
            });
        }
    }

    /// Compute a qualified name for a binding in a given scope.
    fn compute_qn(
        &self,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        file_path: &Path,
        scope_id: ScopeId,
        name: &str,
    ) -> QualifiedName {
        // Build parts in reverse order, then reverse once (avoids Vec::insert(0))
        let mut parts = Vec::new();
        parts.push(name.to_string());
        let mut current = Some(scope_id);
        let sep = &self.config.qualified_names.module_separator;

        while let Some(sid) = current {
            let idx = match scope_index.get(&sid) {
                Some(&i) => i,
                None => break,
            };
            let scope = &scopes[idx];

            match scope.kind {
                ScopeKind::Module => {
                    let module_path = self
                        .import_graph
                        .file_to_module
                        .get(file_path)
                        .cloned()
                        .unwrap_or_else(|| self.derive_module_path(file_path, sep));
                    parts.push(module_path);
                    break;
                }
                ScopeKind::Function if self.config.qualified_names.nested_function_prefix => {
                    if let Some(parent_id) = scope.parent {
                        if let Some(&pidx) = scope_index.get(&parent_id) {
                            let parent = &scopes[pidx];
                            if let Some(bname) = find_scope_name(parent, scope, BindingKind::FunctionDef) {
                                parts.push(bname.to_string());
                            }
                        }
                    }
                }
                ScopeKind::Class if self.config.qualified_names.class_member_prefix => {
                    if let Some(parent_id) = scope.parent {
                        if let Some(&pidx) = scope_index.get(&parent_id) {
                            let parent = &scopes[pidx];
                            if let Some(bname) = find_scope_name(parent, scope, BindingKind::ClassDef) {
                                parts.push(bname.to_string());
                            }
                        }
                    }
                }
                _ => {}
            }
            current = scope.parent;
        }

        parts.reverse();
        QualifiedName {
            name: parts.join(sep),
        }
    }

    /// Remove all QN index entries for a file using the reverse index (O(entries) not O(total)).
    fn remove_file_from_qn_index(&mut self, path: &Path) {
        if let Some(qns) = self.file_qns.remove(path) {
            for qn in qns {
                if let Some(locs) = self.qn_index.get_mut(&qn) {
                    locs.retain(|loc| loc.file != path);
                    if locs.is_empty() {
                        self.qn_index.remove(&qn);
                    }
                }
            }
        }
    }

    /// Find the scope created for a specific node in Pass 1.
    fn find_scope_at_node(&self, node: &tree_sitter::Node, scopes: &[Scope]) -> Option<ScopeId> {
        let start = node.start_byte();
        let end = node.end_byte();
        // Innermost scopes are towards the end of the vec
        for scope in scopes.iter().rev() {
            if scope.start_byte == start && scope.end_byte == end {
                return Some(scope.id);
            }
        }
        None
    }
}

// ---------------------------------------------------------------------------
// Tree-sitter traversal helpers
// ---------------------------------------------------------------------------

/// Iterate over direct children of a node, calling `f` for each.
fn for_each_child(node: &tree_sitter::Node, mut f: impl FnMut(tree_sitter::Node)) {
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            f(cursor.node());
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
}

/// Iterate over direct children of a node that match a specific kind.
fn for_each_child_of_kind(
    node: &tree_sitter::Node,
    kind: &str,
    mut f: impl FnMut(tree_sitter::Node),
) {
    for_each_child(node, |child| {
        if child.kind() == kind {
            f(child);
        }
    });
}

/// Find the binding name in `parent` that defines `scope` (by byte range overlap).
fn find_scope_name<'a>(parent: &'a Scope, scope: &Scope, expected_kind: BindingKind) -> Option<&'a str> {
    for (bname, binding) in &parent.bindings {
        if binding.kind == expected_kind
            && binding.byte_offset >= scope.start_byte
            && binding.byte_offset <= scope.end_byte
        {
            return Some(bname.as_str());
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------



/// Compute a content hash for cache invalidation (single-pass FNV-1a variant).
fn content_hash(source: &str) -> [u8; 16] {
    let bytes = source.as_bytes();
    let mut h1: u64 = 0xcbf29ce484222325;
    let mut h2: u64 = 0x6c62272e07bb0142;
    for &b in bytes {
        h1 ^= b as u64;
        h1 = h1.wrapping_mul(0x100000001b3);
        h2 ^= b as u64;
        h2 = h2.wrapping_mul(0x100000001b3);
    }
    let mut hash = [0u8; 16];
    hash[0..8].copy_from_slice(&h1.to_le_bytes());
    hash[8..16].copy_from_slice(&h2.to_le_bytes());
    hash
}

/// Derive a module path from a file path relative to the project root.
fn derive_module_path(
    file_path: &Path,
    project_root: &Path,
    separator: &str,
    strategy: &str,
    extensions: &[String],
) -> String {
    let relative = file_path
        .strip_prefix(project_root)
        .unwrap_or(file_path);

    let parts: Vec<&str> = relative
        .components()
        .filter_map(|c| c.as_os_str().to_str())
        .collect();
    
    // Strategy-specific pre-processing
    let parts = match strategy {
        "python" => {
            // Python: skip leading "src"
            parts.into_iter().skip_while(|&c| c == "src").collect()
        }
        _ => parts,
    };

    let mut parts = parts;

    // Strip file extension from last component
    if let Some(last) = parts.last_mut() {
        for ext in extensions {
            if let Some(stem) = last.strip_suffix(&format!(".{}", ext)) {
                *last = stem;
                break;
            }
        }
    }

    // Strategy-specific post-processing
    match strategy {
        "python" => {
             // Strip __init__ from end (package init files)
            if parts.last() == Some(&"__init__") {
                parts.pop();
            }
        }
        "node" => {
             // Strip index from end
            if parts.last() == Some(&"index") {
                parts.pop();
            }
        }
        _ => {}
    }

    parts.join(separator)
}

// ---------------------------------------------------------------------------
// Language config loading
// ---------------------------------------------------------------------------

impl LanguageConfig {
    pub fn from_toml(toml_str: &str) -> Result<Self, String> {
        toml::from_str(toml_str).map_err(|e| e.to_string())
    }

    pub fn load_for_extension(ext: &str, project_root: &Path) -> Result<Self, String> {
        // Try to load from languages/<name>/config.toml in the project root
        let lang_dir = project_root.join("languages");
        let lang_name = match ext {
            "py" | "pyi" => "python",
            "ts" | "tsx" | "js" | "jsx" => "typescript",
            "rs" => "rust",
            _ => return Err(format!("Unsupported extension: {}", ext)),
        };
        let config_path = lang_dir.join(lang_name).join("config.toml");
        if config_path.exists() {
            let toml_str = std::fs::read_to_string(config_path).map_err(|e| e.to_string())?;
            return Self::from_toml(&toml_str);
        }

        // Fallback to embedded configs
        match lang_name {
            "python" => Ok(Self::from_toml(PYTHON_CONFIG_TOML).unwrap_or_else(|_| Self::python_default())),
            "typescript" => Ok(Self::from_toml(TS_CONFIG_TOML).expect("Failed to parse embedded TypeScript config")),
            "rust" => Ok(Self::from_toml(RUST_CONFIG_TOML).expect("Failed to parse embedded Rust config")),
            _ => Err(format!("No config found for {}", lang_name)),
        }
    }

    pub fn python_default() -> Self {
        LanguageConfig {
            language: LanguageSection {
                name: "python".to_string(),
                tree_sitter_grammar: "tree-sitter-python".to_string(),
                file_extensions: vec!["py".to_string(), "pyi".to_string()],
                keywords: vec!["True".to_string(), "False".to_string(), "None".to_string()],
            },
            scoping: ScopingSection {
                scope_creators: vec![
                    ScopeCreator { node_type: "module".to_string(), kind: ScopeKind::Module },
                    ScopeCreator { node_type: "function_definition".to_string(), kind: ScopeKind::Function },
                    ScopeCreator { node_type: "class_definition".to_string(), kind: ScopeKind::Class },
                    ScopeCreator { node_type: "lambda".to_string(), kind: ScopeKind::Function },
                    ScopeCreator { node_type: "list_comprehension".to_string(), kind: ScopeKind::Comprehension },
                    ScopeCreator { node_type: "set_comprehension".to_string(), kind: ScopeKind::Comprehension },
                    ScopeCreator { node_type: "dictionary_comprehension".to_string(), kind: ScopeKind::Comprehension },
                    ScopeCreator { node_type: "generator_expression".to_string(), kind: ScopeKind::Comprehension },
                ],
                rules: HashMap::from([
                    (ScopeKind::Module, ScopeRule { is_closure_boundary: false, names_visible_to_inner: true, scoped_children: None }),
                    (ScopeKind::Function, ScopeRule { is_closure_boundary: false, names_visible_to_inner: true, scoped_children: None }),
                    (ScopeKind::Class, ScopeRule { is_closure_boundary: true, names_visible_to_inner: false, scoped_children: None }),
                    (ScopeKind::Comprehension, ScopeRule { 
                        is_closure_boundary: false, 
                        names_visible_to_inner: true, 
                        scoped_children: Some(vec!["for_in_clause.left".to_string()]) 
                    }),
                ]),
                declarations: DeclarationsSection {
                    global_keyword: Some("global_statement".to_string()),
                    nonlocal_keyword: Some("nonlocal_statement".to_string()),
                },
            },
            bindings: BindingsSection {
                assignment: vec![
                    AssignmentRule { node: "assignment".to_string(), target: "left".to_string() },
                    AssignmentRule { node: "augmented_assignment".to_string(), target: "left".to_string() },
                    AssignmentRule { node: "annotated_assignment".to_string(), target: "left".to_string() },
                ],
                loop_rules: vec![
                    AssignmentRule { node: "for_statement".to_string(), target: "left".to_string() },
                    AssignmentRule { node: "for_in_clause".to_string(), target: "left".to_string() },
                ],
                context_manager: vec![AssignmentRule { node: "with_clause".to_string(), target: "alias".to_string() }],
                exception: vec![AssignmentRule { node: "except_clause".to_string(), target: "name".to_string() }],
                parameters: ParametersSection {
                    param_nodes: vec![
                        "identifier".to_string(),
                        "default_parameter".to_string(),
                        "typed_parameter".to_string(),
                        "typed_default_parameter".to_string(),
                        "list_splat_pattern".to_string(),
                        "dictionary_splat_pattern".to_string(),
                    ],
                    name_field: "name".to_string(),
                },
                definitions: DefinitionsSection {
                    function_def: "function_definition".to_string(),
                    class_def: "class_definition".to_string(),
                    name_field: "name".to_string(),
                },
                walrus: vec![AssignmentRule { node: "named_expression".to_string(), target: "name".to_string() }],
            },
            imports: ImportsSection {
                import_statement: "import_statement".to_string(),
                import_from: "import_from_statement".to_string(),
                module_field: "module_name".to_string(),
                name_field: "name".to_string(),
                alias_field: "alias".to_string(),
                star_import: "wildcard_import".to_string(),
                resolution: "python".to_string(),
                dotted_name: Some("dotted_name".to_string()),
                aliased_import: Some("aliased_import".to_string()),
                identifier: Some("identifier".to_string()),
            },
            qualified_names: QualifiedNamesSection {
                module_separator: ".".to_string(),
                class_member_prefix: true,
                nested_function_prefix: true,
                locals_marker: "<locals>".to_string(),
            },
            exports: ExportsSection {
                all_variable: Some("__all__".to_string()),
                public_by_default: true,
                private_prefix: "_".to_string(),
                dunder_is_public: true,
            },
            builtins: BuiltinsSection {
                names: vec![
                    "print", "len", "range", "int", "str", "float", "bool", "list",
                    "dict", "set", "tuple", "type", "isinstance", "issubclass",
                    "hasattr", "getattr", "setattr", "delattr", "property",
                    "staticmethod", "classmethod", "super", "object", "Exception",
                    "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError",
                    "AttributeError", "ImportError", "OSError", "IOError", "FileNotFoundError",
                    "NotImplementedError", "StopIteration", "GeneratorExit", "SystemExit",
                    "AssertionError", "NameError", "ZeroDivisionError", "OverflowError",
                    "abs", "all", "any", "bin", "bytes", "callable", "chr", "complex",
                    "dir", "divmod", "enumerate", "eval", "exec", "filter", "format",
                    "frozenset", "globals", "hash", "hex", "id", "input", "iter",
                    "map", "max", "min", "next", "oct", "open", "ord", "pow",
                    "repr", "reversed", "round", "slice", "sorted", "sum", "vars",
                    "zip", "breakpoint", "memoryview", "bytearray",
                    "__name__", "__file__", "__doc__", "__package__", "__spec__",
                    "__import__", "__build_class__",
                ].into_iter().map(String::from).collect(),
            },
            symbols: SymbolsSection::python_default(),
            pattern_matching: PatternMatchingSection {
                function_def: "function_definition".to_string(),
                class_def: "class_definition".to_string(),
                decorated_def: "decorated_definition".to_string(),
                call: "call".to_string(),
                attribute: "attribute".to_string(),
                identifier: "identifier".to_string(),
                assignment: "assignment".to_string(),
                augmented_assignment: "augmented_assignment".to_string(),
                annotated_assignment: "annotated_assignment".to_string(),
                return_stmt: "return_statement".to_string(),
                if_stmt: "if_statement".to_string(),
                while_stmt: "while_statement".to_string(),
                for_stmt: "for_statement".to_string(),
                with_stmt: "with_statement".to_string(),
                try_stmt: "try_statement".to_string(),
                except_handler: "except_clause".to_string(),
                for_in_clause: "for_in_clause".to_string(),
                if_clause: "if_clause".to_string(),
                pair: "pair".to_string(),
                parenthesized_expression: "parenthesized_expression".to_string(),
                not_operator: "not_operator".to_string(),
                conditional_expression: "conditional_expression".to_string(),
                true_literal: "true".to_string(),
                false_literal: "false".to_string(),
                none_literal: "none".to_string(),
                as_pattern: "as_pattern".to_string(),
                import_stmt: "import_statement".to_string(),
                import_from_stmt: "import_from_statement".to_string(),
                assert_stmt: "assert_statement".to_string(),
                raise_stmt: "raise_statement".to_string(),
                delete_stmt: "delete_statement".to_string(),
                global_stmt: "global_statement".to_string(),
                nonlocal_stmt: "nonlocal_statement".to_string(),
                await_expr: "await".to_string(),
                yield_expr: "yield".to_string(),
                lambda: "lambda".to_string(),
                named_expr: "named_expression".to_string(),
                dict_comprehension: "dictionary_comprehension".to_string(),
                integer: "integer".to_string(),

                float: "float".to_string(),
                string: "string".to_string(),
                list: "list".to_string(),
                tuple: "tuple".to_string(),
                set: "set".to_string(),
                dict: "dictionary".to_string(),
                binary_operator: "binary_operator".to_string(),
                boolean_operator: "boolean_operator".to_string(),
                unary_operator: "unary_operator".to_string(),
                comparison_operator: "comparison_operator".to_string(),
                subscript: "subscript".to_string(),
                keyword_argument: "keyword_argument".to_string(),
                list_splat: "list_splat".to_string(),
                dictionary_splat: "dictionary_splat".to_string(),
                statement_nodes: vec![
                    "expression_statement".to_string(),
                    "return_statement".to_string(),
                    "if_statement".to_string(),
                    "for_statement".to_string(),
                    "while_statement".to_string(),
                    "try_statement".to_string(),
                    "with_statement".to_string(),
                    "function_definition".to_string(),
                    "class_definition".to_string(),
                    "decorated_definition".to_string(),
                    "assignment".to_string(),
                    "augmented_assignment".to_string(),
                    "import_statement".to_string(),
                    "import_from_statement".to_string(),
                    "assert_statement".to_string(),
                    "raise_statement".to_string(),
                    "delete_statement".to_string(),
                    "global_statement".to_string(),
                    "nonlocal_statement".to_string(),
                ],
                func_field: "function".to_string(),
                args_field: "arguments".to_string(),
                object_field: "object".to_string(),
                attr_field: "attribute".to_string(),
                left_field: "left".to_string(),
                right_field: "right".to_string(),
                operator_field: "operator".to_string(),
                annotation_field: "type".to_string(),
                value_field: "value".to_string(),
                condition_field: "condition".to_string(),
                target_field: "left".to_string(),
                iter_field: "right".to_string(),
                elt_field: "body".to_string(),
            },
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    fn parse(source: &str) -> tree_sitter::Tree {
        crate::pattern::parse_python(source).expect("parse failed")
    }

    #[test]
    fn test_basic_scope_building() {
        let source = r#"
x = 1

def foo(a, b):
    y = a + b
    return y

class Bar:
    z = 10
    def method(self):
        return self.z
"#;
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();

        // Should have scopes: module, foo, Bar, method
        assert!(
            file_scope.scopes.len() >= 4,
            "Expected at least 4 scopes, got {}",
            file_scope.scopes.len()
        );

        // Module scope should have bindings: x, foo, Bar
        let module_scope = &file_scope.scopes[0];
        assert_eq!(module_scope.kind, ScopeKind::Module);
        assert!(module_scope.bindings.contains_key("x"));
        assert!(module_scope.bindings.contains_key("foo"));
        assert!(module_scope.bindings.contains_key("Bar"));
    }

    #[test]
    fn test_function_parameters() {
        let source = "def greet(name, greeting='hello'):\n    return f'{greeting} {name}'\n";
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();
        let func_scope = file_scope
            .scopes
            .iter()
            .find(|s| s.kind == ScopeKind::Function)
            .expect("Should have a function scope");

        assert!(func_scope.bindings.contains_key("name"));
        assert!(func_scope.bindings.contains_key("greeting"));
    }

    #[test]
    fn test_import_collection() {
        let source = r#"
import os
import os.path as osp
from collections import OrderedDict
from typing import Optional, List
from . import sibling
"#;
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();

        assert!(file_scope.imports.contains_key("os"));
        assert!(file_scope.imports.contains_key("OrderedDict"));
        assert!(file_scope.imports.contains_key("Optional"));
        assert!(file_scope.imports.contains_key("List"));
    }

    #[test]
    fn test_class_scope() {
        let source = r#"
class MyClass:
    class_var = 42

    def method(self, x):
        return self.class_var + x
"#;
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();

        let class_scope = file_scope
            .scopes
            .iter()
            .find(|s| s.kind == ScopeKind::Class)
            .expect("Should have a class scope");

        let config = LanguageConfig::python_default();
        let rule = config.scoping.rules.get(&ScopeKind::Class).unwrap();
        assert!(rule.is_closure_boundary);
        assert!(!rule.names_visible_to_inner);

        assert!(class_scope.bindings.contains_key("class_var"));
        assert!(class_scope.bindings.contains_key("method"));
    }

    #[test]
    fn test_qn_index() {
        let source = r#"
def top_level():
    pass

class MyClass:
    def method(self):
        pass
"#;
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/src/mymod/example.py");
        resolver.index_file(&path, source, &tree);

        assert!(!resolver.qn_index.is_empty());

        let has_top_level = resolver.qn_index.keys().any(|k| k.contains("top_level"));
        assert!(has_top_level, "QN index missing 'top_level'");

        let has_class = resolver.qn_index.keys().any(|k| k.contains("MyClass"));
        assert!(has_class, "QN index missing 'MyClass'");
    }

    #[test]
    fn test_derive_module_path() {
        let py_exts = vec!["py".to_string(), "pyi".to_string()];
        assert_eq!(
            derive_module_path(Path::new("/project/src/mypackage/module.py"), Path::new("/project"), ".", "python", &py_exts),
            "mypackage.module"
        );
        assert_eq!(
            derive_module_path(Path::new("/project/mypackage/__init__.py"), Path::new("/project"), ".", "python", &py_exts),
            "mypackage"
        );
        assert_eq!(
            derive_module_path(Path::new("/project/src/pkg/sub/file.py"), Path::new("/project"), ".", "python", &py_exts),
            "pkg.sub.file"
        );

        let ts_exts = vec!["ts".to_string(), "tsx".to_string(), "js".to_string()];
        assert_eq!(
            derive_module_path(Path::new("/project/src/components/Button.tsx"), Path::new("/project"), "/", "node", &ts_exts),
            "src/components/Button"
        );
        assert_eq!(
            derive_module_path(Path::new("/project/src/utils/index.ts"), Path::new("/project"), "/", "node", &ts_exts),
            "src/utils"
        );
    }

    #[test]
    fn test_incremental_reindex() {
        let source1 = "x = 1\n";
        let source2 = "x = 1\ny = 2\n";
        let tree1 = parse(source1);
        let tree2 = parse(source2);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");

        resolver.index_file(&path, source1, &tree1);
        let hash1 = resolver.file_scopes.get(&path).unwrap().content_hash;

        resolver.index_file(&path, source1, &tree1);

        resolver.index_file(&path, source2, &tree2);
        let hash2 = resolver.file_scopes.get(&path).unwrap().content_hash;
        assert_ne!(hash1, hash2, "Hash should change with different content");
    }

    #[test]
    fn test_reference_collection() {
        let source = r#"
import os
from collections import OrderedDict

def foo(x):
    y = x + 1
    print(y)
    return os.path.join("/tmp", str(y))

class Bar:
    def method(self):
        d = OrderedDict()
        return d
"#;
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();

        // Should have references
        assert!(!file_scope.references.is_empty(), "Should have references");

        // Should have all_qnames
        assert!(!file_scope.all_qnames.is_empty(), "Should have QN strings");

        // Check that import-resolved QNs are present
        let qns: std::collections::HashSet<_> = file_scope.all_qnames.iter().cloned().collect();
        assert!(qns.iter().any(|q| q.contains("os")), "Should have os QN, got: {:?}", qns);
        assert!(qns.iter().any(|q| q.contains("collections.OrderedDict")),
            "Should have OrderedDict QN, got: {:?}", qns);

        // Check reference classification
        let call_refs: Vec<_> = file_scope.references.iter()
            .filter(|r| r.kind == super::ReferenceKind::Call)
            .collect();
        assert!(!call_refs.is_empty(), "Should have call references");

        // `print(y)` should be classified as a call
        let has_print_call = call_refs.iter().any(|r| r.qn.name.contains("print"));
        assert!(has_print_call, "Should have print() call, got: {:?}",
            call_refs.iter().map(|r| &r.qn.name).collect::<Vec<_>>());

        // Import refs should be classified as import
        let import_refs: Vec<_> = file_scope.references.iter()
            .filter(|r| r.kind == super::ReferenceKind::Import)
            .collect();
        assert!(!import_refs.is_empty(), "Should have import references");
    }

    #[test]
    fn test_type_annotation_and_no_locals() {
        let source = r#"
x: int = 1

def foo(a: str) -> bool:
    y: float = 2.0
    return True
"#;
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let symbols = resolver.get_symbols(&path);
        
        // x should have type_annotation: int
        let x_sym = symbols.iter().find(|s| s.name.contains("x")).expect("Missing x");
        assert_eq!(x_sym.type_annotation, Some("int".to_string()));

        // y should have type_annotation: float
        let y_sym = symbols.iter().find(|s| s.name.contains("y")).expect("Missing y");
        assert_eq!(y_sym.type_annotation, Some("float".to_string()));
        
        // y's QN should not contain <locals>
        // It should be test.foo.y
        assert_eq!(y_sym.name, "test.foo.y");
        assert!(!y_sym.name.contains("<locals>"));
    }

    #[test]
    fn test_reference_classification() {
        let source = r#"
x = 10
y = x + 1
print(x)
x = 20
"#;
        let tree = parse(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();

        // Check we have write, read, and call references
        let ref_kinds: Vec<_> = file_scope.references.iter()
            .map(|r| (r.qn.name.clone(), r.kind))
            .collect();

        let has_write = ref_kinds.iter().any(|(_, k)| *k == super::ReferenceKind::Write);
        let has_read = ref_kinds.iter().any(|(_, k)| *k == super::ReferenceKind::Read);
        let has_call = ref_kinds.iter().any(|(_, k)| *k == super::ReferenceKind::Call);

        assert!(has_write, "Should have write references, got: {:?}", ref_kinds);
        assert!(has_read, "Should have read references, got: {:?}", ref_kinds);
        assert!(has_call, "Should have call references (print), got: {:?}", ref_kinds);
    }
}
