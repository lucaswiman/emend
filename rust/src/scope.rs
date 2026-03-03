//! Scope resolver: builds scope trees from tree-sitter CSTs and resolves
//! qualified names.
//!
//! This module replaces LibCST's QualifiedNameProvider with a persistent,
//! incremental scope index built on tree-sitter.  The scoping rules are
//! driven by a language config file (TOML), making the resolver
//! language-agnostic in principle.
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

use std::collections::HashMap;
use std::path::{Path, PathBuf};

// ---------------------------------------------------------------------------
// Scope kinds (language-agnostic)
// ---------------------------------------------------------------------------

/// The kind of scope a tree-sitter node creates.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ScopeKind {
    Module,
    Function,
    Class,
    Comprehension,
    Block, // for block-scoped languages like JS/TS
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
    /// Byte range in the source that this scope covers.
    pub start_byte: usize,
    pub end_byte: usize,
    /// Bindings introduced in this scope: name → Binding.
    pub bindings: HashMap<String, Binding>,
}

/// A name binding (definition site).
#[derive(Debug, Clone)]
pub struct Binding {
    pub name: String,
    pub kind: BindingKind,
    pub line: usize,
    pub column: usize,
    pub byte_offset: usize,
}

/// How a name was introduced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindingKind {
    /// `x = ...` or augmented assignment
    Assignment,
    /// `def f(...):`
    FunctionDef,
    /// `class C:`
    ClassDef,
    /// Function/method parameter
    Parameter,
    /// `import x` or `from x import y`
    Import,
    /// `for x in ...:`
    LoopVariable,
    /// `with ... as x:`
    ContextManager,
    /// `except E as x:`
    ExceptionHandler,
    /// `:= ` walrus operator
    Walrus,
    /// `global x` declaration (modifies lookup, doesn't create new binding)
    Global,
    /// `nonlocal x` declaration
    Nonlocal,
}

/// A resolved qualified name.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct QualifiedName {
    /// Dotted qualified name, e.g. "mypackage.module.ClassName.method"
    pub name: String,
}

/// A reference to a name at a specific location.
#[derive(Debug, Clone)]
pub struct Reference {
    pub file: PathBuf,
    pub line: usize,
    pub column: usize,
    pub byte_offset: usize,
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
    /// Content hash for cache invalidation.
    pub content_hash: [u8; 16],
    /// All scopes in the file.
    pub scopes: Vec<Scope>,
    /// Import table: import name → resolved module path.
    pub imports: HashMap<String, ImportBinding>,
    /// All qualified names defined in this file.
    pub definitions: Vec<(QualifiedName, Location)>,
}

/// An import binding.
#[derive(Debug, Clone)]
pub struct ImportBinding {
    /// The local name (possibly aliased).
    pub local_name: String,
    /// The fully-resolved module path.
    pub module_path: String,
    /// The imported name within the module (None for `import module`).
    pub imported_name: Option<String>,
    /// Whether this is a star import.
    pub is_star: bool,
}

// ---------------------------------------------------------------------------
// Language config (loaded from TOML)
// ---------------------------------------------------------------------------

/// Language-specific scoping configuration, loaded from a TOML file.
/// This drives the scope resolver's behavior without requiring Rust code
/// changes for new languages.
#[derive(Debug, Clone)]
pub struct LanguageConfig {
    pub name: String,
    pub file_extensions: Vec<String>,
    /// Tree-sitter node types that create new scopes.
    pub scope_creators: Vec<ScopeCreator>,
    /// Per-scope-kind rules (closure boundary, name visibility).
    pub scope_rules: HashMap<ScopeKind, ScopeRule>,
    /// Import resolution strategy name.
    pub import_resolution: String,
    /// QN construction rules.
    pub module_separator: String,
    pub class_member_prefix: bool,
    pub nested_function_prefix: bool,
    pub locals_marker: String,
    /// Export rules.
    pub all_variable: Option<String>,
    pub public_by_default: bool,
    pub private_prefix: String,
}

/// A tree-sitter node type that creates a scope.
#[derive(Debug, Clone)]
pub struct ScopeCreator {
    pub node_type: String,
    pub kind: ScopeKind,
}

/// Rules for a specific scope kind.
#[derive(Debug, Clone)]
pub struct ScopeRule {
    /// Whether this scope acts as a closure boundary.
    /// In Python, class scopes are closure boundaries (inner functions
    /// cannot see class-level names).
    pub is_closure_boundary: bool,
    /// Whether names in this scope are visible to inner scopes.
    pub names_visible_to_inner: bool,
}

// ---------------------------------------------------------------------------
// Import graph
// ---------------------------------------------------------------------------

/// Project-wide import graph for cross-file resolution.
#[derive(Debug, Clone, Default)]
pub struct ImportGraph {
    /// Module path → file path.
    pub module_to_file: HashMap<String, PathBuf>,
    /// File path → module path.
    pub file_to_module: HashMap<PathBuf, String>,
}

// ---------------------------------------------------------------------------
// Scope Resolver
// ---------------------------------------------------------------------------

/// The main scope resolver.  Maintains a persistent, incremental index
/// of all scopes, bindings, and qualified names in the project.
#[derive(Debug)]
pub struct ScopeResolver {
    /// Language configuration (loaded from TOML).
    pub config: LanguageConfig,
    /// Per-file scope data, keyed by file path.
    pub file_scopes: HashMap<PathBuf, FileScope>,
    /// Project-wide import graph.
    pub import_graph: ImportGraph,
    /// Qualified name index: QN → list of definition sites.
    pub qn_index: HashMap<String, Vec<Location>>,
    /// Project root directory.
    pub project_root: PathBuf,
}

impl ScopeResolver {
    /// Create a new scope resolver with the given language config and project root.
    pub fn new(config: LanguageConfig, project_root: PathBuf) -> Self {
        Self {
            config,
            file_scopes: HashMap::new(),
            import_graph: ImportGraph::default(),
            qn_index: HashMap::new(),
            project_root,
        }
    }

    /// Index a single file.  Parses with tree-sitter, walks the CST to
    /// build scope tree + binding table + import table.
    ///
    /// Incremental: only re-indexes if content hash changed.
    pub fn index_file(&mut self, path: &Path, source: &str, tree: &tree_sitter::Tree) {
        let hash = md5_hash(source);

        // Check if already indexed with same content
        if let Some(existing) = self.file_scopes.get(path) {
            if existing.content_hash == hash {
                return;
            }
        }

        // Build scope tree from tree-sitter CST
        let file_scope = self.build_file_scope(path, source, tree, hash);

        // Update QN index: remove old entries for this file, add new ones
        self.remove_file_from_qn_index(path);
        for (qn, loc) in &file_scope.definitions {
            self.qn_index
                .entry(qn.name.clone())
                .or_default()
                .push(loc.clone());
        }

        // Update import graph
        self.update_import_graph(path, &file_scope);

        self.file_scopes.insert(path.to_path_buf(), file_scope);
    }

    /// Resolve qualified names for all identifiers in a file.
    pub fn qualified_names(&self, path: &Path) -> Vec<(QualifiedName, Location)> {
        // TODO: Walk each Name/Attribute node, look up the scope tree to
        // find the binding, construct the QN from module path + binding chain.
        let _ = path;
        Vec::new()
    }

    /// Find all references to a qualified name across the project.
    pub fn find_references(&self, qn: &str) -> Vec<Reference> {
        // TODO: Look up QN in the index, then scan files that contain
        // the base name for actual references.
        let _ = qn;
        Vec::new()
    }

    /// Find the definition site for a reference at a given position.
    pub fn goto_definition(&self, path: &Path, line: usize, column: usize) -> Option<Location> {
        let _ = (path, line, column);
        None
    }

    /// Find dead code (unreferenced definitions) across the project.
    pub fn find_dead_code(&self) -> Vec<(QualifiedName, Location)> {
        // TODO: For each definition in the QN index, check if it has
        // any references other than the definition itself.
        Vec::new()
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
    ) -> FileScope {
        let mut scopes = Vec::new();
        let mut imports = HashMap::new();
        let mut definitions = Vec::new();
        let mut next_scope_id: u32 = 0;

        // Create the module-level scope
        let root_node = tree.root_node();
        let module_scope = Scope {
            id: ScopeId(next_scope_id),
            kind: ScopeKind::Module,
            parent: None,
            start_byte: root_node.start_byte(),
            end_byte: root_node.end_byte(),
            bindings: HashMap::new(),
        };
        scopes.push(module_scope);
        next_scope_id += 1;

        // Walk the tree-sitter CST and build scopes
        let mut cursor = root_node.walk();
        self.walk_node(
            &mut cursor,
            source,
            path,
            ScopeId(0), // module scope
            &mut scopes,
            &mut next_scope_id,
            &mut imports,
            &mut definitions,
        );

        FileScope {
            content_hash,
            scopes,
            imports,
            definitions,
        }
    }

    /// Recursively walk tree-sitter nodes to build scope tree and collect
    /// bindings and imports.
    fn walk_node(
        &self,
        cursor: &mut tree_sitter::TreeCursor,
        source: &str,
        file_path: &Path,
        current_scope: ScopeId,
        scopes: &mut Vec<Scope>,
        next_scope_id: &mut u32,
        imports: &mut HashMap<String, ImportBinding>,
        definitions: &mut Vec<(QualifiedName, Location)>,
    ) {
        let node = cursor.node();
        let node_kind = node.kind();

        // Check if this node creates a new scope
        let scope_for_children =
            if let Some(creator) = self.config.scope_creators.iter().find(|c| c.node_type == node_kind) {
                // Skip module -- we already created it
                if creator.kind == ScopeKind::Module {
                    current_scope
                } else {
                    let scope_id = ScopeId(*next_scope_id);
                    *next_scope_id += 1;
                    scopes.push(Scope {
                        id: scope_id,
                        kind: creator.kind,
                        parent: Some(current_scope),
                        start_byte: node.start_byte(),
                        end_byte: node.end_byte(),
                        bindings: HashMap::new(),
                    });

                    // If this is a function/class def, the NAME is bound in
                    // the ENCLOSING scope (not the new scope).
                    if creator.kind == ScopeKind::Function || creator.kind == ScopeKind::Class {
                        if let Some(name_node) = node.child_by_field_name("name") {
                            let name = &source[name_node.start_byte()..name_node.end_byte()];
                            let binding = Binding {
                                name: name.to_string(),
                                kind: if creator.kind == ScopeKind::Function {
                                    BindingKind::FunctionDef
                                } else {
                                    BindingKind::ClassDef
                                },
                                line: name_node.start_position().row,
                                column: name_node.start_position().column,
                                byte_offset: name_node.start_byte(),
                            };

                            // Add to enclosing scope
                            if let Some(scope) = scopes.iter_mut().find(|s| s.id == current_scope) {
                                scope.bindings.insert(name.to_string(), binding);
                            }

                            // Compute qualified name and add to definitions
                            let qn = self.compute_qn(file_path, scopes, current_scope, name);
                            let loc = Location {
                                file: file_path.to_path_buf(),
                                line: name_node.start_position().row,
                                column: name_node.start_position().column,
                                byte_offset: name_node.start_byte(),
                            };
                            definitions.push((qn, loc));
                        }
                    }

                    // Collect function parameters as bindings in the new scope
                    if creator.kind == ScopeKind::Function {
                        if let Some(params) = node.child_by_field_name("parameters") {
                            self.collect_parameters(source, &params, scope_id, scopes);
                        }
                    }

                    scope_id
                }
            } else {
                current_scope
            };

        // Collect imports
        if node_kind == "import_statement" || node_kind == "import_from_statement" {
            self.collect_import(source, &node, imports);
        }

        // Collect assignments as bindings in current scope
        if node_kind == "assignment" || node_kind == "augmented_assignment" || node_kind == "annotated_assignment" {
            if let Some(left) = node.child_by_field_name("left") {
                if left.kind() == "identifier" {
                    let name = &source[left.start_byte()..left.end_byte()];
                    let binding = Binding {
                        name: name.to_string(),
                        kind: BindingKind::Assignment,
                        line: left.start_position().row,
                        column: left.start_position().column,
                        byte_offset: left.start_byte(),
                    };
                    if let Some(scope) = scopes.iter_mut().find(|s| s.id == current_scope) {
                        // Don't overwrite existing bindings (first assignment wins
                        // for the purpose of scope analysis)
                        scope.bindings.entry(name.to_string()).or_insert(binding);
                    }
                }
            }
        }

        // Collect global/nonlocal declarations
        if node_kind == "global_statement" || node_kind == "nonlocal_statement" {
            let kind = if node_kind == "global_statement" {
                BindingKind::Global
            } else {
                BindingKind::Nonlocal
            };
            // Iterate over children to find all identifiers
            let mut child_cursor = node.walk();
            if child_cursor.goto_first_child() {
                loop {
                    let child = child_cursor.node();
                    if child.kind() == "identifier" {
                        let name = &source[child.start_byte()..child.end_byte()];
                        let binding = Binding {
                            name: name.to_string(),
                            kind,
                            line: child.start_position().row,
                            column: child.start_position().column,
                            byte_offset: child.start_byte(),
                        };
                        if let Some(scope) = scopes.iter_mut().find(|s| s.id == current_scope) {
                            scope.bindings.insert(name.to_string(), binding);
                        }
                    }
                    if !child_cursor.goto_next_sibling() {
                        break;
                    }
                }
            }
        }

        // Recurse into children
        if cursor.goto_first_child() {
            loop {
                self.walk_node(
                    cursor,
                    source,
                    file_path,
                    scope_for_children,
                    scopes,
                    next_scope_id,
                    imports,
                    definitions,
                );
                if !cursor.goto_next_sibling() {
                    break;
                }
            }
            cursor.goto_parent();
        }
    }

    /// Collect function parameters as bindings.
    fn collect_parameters(
        &self,
        source: &str,
        params_node: &tree_sitter::Node,
        scope_id: ScopeId,
        scopes: &mut Vec<Scope>,
    ) {
        let mut cursor = params_node.walk();
        if !cursor.goto_first_child() {
            return;
        }
        loop {
            let child = cursor.node();
            let name = match child.kind() {
                "identifier" => Some(&source[child.start_byte()..child.end_byte()]),
                "default_parameter" | "typed_parameter" | "typed_default_parameter" => {
                    child
                        .child_by_field_name("name")
                        .map(|n| &source[n.start_byte()..n.end_byte()])
                }
                "list_splat_pattern" | "dictionary_splat_pattern" => {
                    // *args / **kwargs -- the name is the child identifier
                    let mut inner = child.walk();
                    if inner.goto_first_child() {
                        loop {
                            let n = inner.node();
                            if n.kind() == "identifier" {
                                break Some(&source[n.start_byte()..n.end_byte()]);
                            }
                            if !inner.goto_next_sibling() {
                                break None;
                            }
                        }
                    } else {
                        None
                    }
                }
                _ => None,
            };

            if let Some(name) = name {
                let binding = Binding {
                    name: name.to_string(),
                    kind: BindingKind::Parameter,
                    line: child.start_position().row,
                    column: child.start_position().column,
                    byte_offset: child.start_byte(),
                };
                if let Some(scope) = scopes.iter_mut().find(|s| s.id == scope_id) {
                    scope.bindings.insert(name.to_string(), binding);
                }
            }

            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }

    /// Collect imports from an import statement node.
    fn collect_import(
        &self,
        source: &str,
        node: &tree_sitter::Node,
        imports: &mut HashMap<String, ImportBinding>,
    ) {
        match node.kind() {
            "import_statement" => {
                // `import foo` or `import foo.bar` or `import foo as f`
                let mut cursor = node.walk();
                if cursor.goto_first_child() {
                    loop {
                        let child = cursor.node();
                        if child.kind() == "dotted_name" || child.kind() == "aliased_import" {
                            let (local_name, module_path) = if child.kind() == "aliased_import" {
                                let name_node = child.child_by_field_name("name");
                                let alias_node = child.child_by_field_name("alias");
                                match (name_node, alias_node) {
                                    (Some(n), Some(a)) => (
                                        source[a.start_byte()..a.end_byte()].to_string(),
                                        source[n.start_byte()..n.end_byte()].to_string(),
                                    ),
                                    (Some(n), None) => {
                                        let text = source[n.start_byte()..n.end_byte()].to_string();
                                        // For `import foo.bar`, local name is `foo`
                                        let local = text.split('.').next().unwrap_or(&text).to_string();
                                        (local, text)
                                    }
                                    _ => continue,
                                }
                            } else {
                                let text = source[child.start_byte()..child.end_byte()].to_string();
                                let local = text.split('.').next().unwrap_or(&text).to_string();
                                (local, text)
                            };

                            imports.insert(
                                local_name.clone(),
                                ImportBinding {
                                    local_name,
                                    module_path,
                                    imported_name: None,
                                    is_star: false,
                                },
                            );
                        }
                        if !cursor.goto_next_sibling() {
                            break;
                        }
                    }
                }
            }
            "import_from_statement" => {
                // `from foo import bar` or `from foo import *`
                let module_node = node.child_by_field_name("module_name");
                let module_path = module_node
                    .map(|n| source[n.start_byte()..n.end_byte()].to_string())
                    .unwrap_or_default();

                let mut cursor = node.walk();
                if cursor.goto_first_child() {
                    loop {
                        let child = cursor.node();
                        match child.kind() {
                            "wildcard_import" => {
                                imports.insert(
                                    "*".to_string(),
                                    ImportBinding {
                                        local_name: "*".to_string(),
                                        module_path: module_path.clone(),
                                        imported_name: None,
                                        is_star: true,
                                    },
                                );
                            }
                            "dotted_name" | "identifier" => {
                                let name = source[child.start_byte()..child.end_byte()].to_string();
                                imports.insert(
                                    name.clone(),
                                    ImportBinding {
                                        local_name: name.clone(),
                                        module_path: module_path.clone(),
                                        imported_name: Some(name),
                                        is_star: false,
                                    },
                                );
                            }
                            "aliased_import" => {
                                let name_node = child.child_by_field_name("name");
                                let alias_node = child.child_by_field_name("alias");
                                if let Some(name_node) = name_node {
                                    let imported =
                                        source[name_node.start_byte()..name_node.end_byte()]
                                            .to_string();
                                    let local = alias_node
                                        .map(|a| source[a.start_byte()..a.end_byte()].to_string())
                                        .unwrap_or_else(|| imported.clone());
                                    imports.insert(
                                        local.clone(),
                                        ImportBinding {
                                            local_name: local,
                                            module_path: module_path.clone(),
                                            imported_name: Some(imported),
                                            is_star: false,
                                        },
                                    );
                                }
                            }
                            _ => {}
                        }
                        if !cursor.goto_next_sibling() {
                            break;
                        }
                    }
                }
            }
            _ => {}
        }
    }

    /// Compute a qualified name for a binding in a given scope.
    fn compute_qn(
        &self,
        file_path: &Path,
        scopes: &[Scope],
        scope_id: ScopeId,
        name: &str,
    ) -> QualifiedName {
        // Build the QN by walking from the binding's scope up to the module
        let mut parts = vec![name.to_string()];
        let mut current = Some(scope_id);
        let sep = &self.config.module_separator;

        while let Some(sid) = current {
            if let Some(scope) = scopes.iter().find(|s| s.id == sid) {
                match scope.kind {
                    ScopeKind::Module => {
                        // Prepend module path
                        let module_path = self
                            .import_graph
                            .file_to_module
                            .get(file_path)
                            .cloned()
                            .unwrap_or_else(|| {
                                // Derive module path from file path
                                derive_module_path(file_path, &self.project_root, sep)
                            });
                        parts.insert(0, module_path);
                        break;
                    }
                    ScopeKind::Function if self.config.nested_function_prefix => {
                        // Find the function name in the parent scope's bindings
                        if let Some(parent_id) = scope.parent {
                            if let Some(parent) = scopes.iter().find(|s| s.id == parent_id) {
                                for (bname, binding) in &parent.bindings {
                                    if binding.kind == BindingKind::FunctionDef
                                        && binding.byte_offset >= scope.start_byte
                                        && binding.byte_offset <= scope.end_byte
                                    {
                                        parts.insert(0, self.config.locals_marker.clone());
                                        parts.insert(0, bname.clone());
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    ScopeKind::Class if self.config.class_member_prefix => {
                        // Find the class name in the parent scope's bindings
                        if let Some(parent_id) = scope.parent {
                            if let Some(parent) = scopes.iter().find(|s| s.id == parent_id) {
                                for (bname, binding) in &parent.bindings {
                                    if binding.kind == BindingKind::ClassDef
                                        && binding.byte_offset >= scope.start_byte
                                        && binding.byte_offset <= scope.end_byte
                                    {
                                        parts.insert(0, bname.clone());
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    _ => {}
                }
                current = scope.parent;
            } else {
                break;
            }
        }

        QualifiedName {
            name: parts.join(sep),
        }
    }

    fn remove_file_from_qn_index(&mut self, path: &Path) {
        // Remove all entries that point to this file
        self.qn_index.retain(|_, locs| {
            locs.retain(|loc| loc.file != path);
            !locs.is_empty()
        });
    }

    fn update_import_graph(&mut self, path: &Path, file_scope: &FileScope) {
        // Derive module path for this file
        let module_path = derive_module_path(path, &self.project_root, &self.config.module_separator);
        self.import_graph
            .module_to_file
            .insert(module_path.clone(), path.to_path_buf());
        self.import_graph
            .file_to_module
            .insert(path.to_path_buf(), module_path);

        // Register imported modules (for cross-file resolution)
        for import in file_scope.imports.values() {
            if !import.is_star {
                // We don't resolve the import target here -- that happens
                // lazily when a reference is looked up.
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

/// Compute an MD5 hash of source content (for cache invalidation).
fn md5_hash(source: &str) -> [u8; 16] {
    // Simple FNV-like hash for now; can use real MD5 later.
    // Using a simple hash to avoid adding a dependency.
    let mut hash = [0u8; 16];
    let bytes = source.as_bytes();
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    hash[0..8].copy_from_slice(&h.to_le_bytes());
    // Second pass with different seed for more bits
    let mut h2: u64 = 0x6c62272e07bb0142;
    for &b in bytes {
        h2 ^= b as u64;
        h2 = h2.wrapping_mul(0x100000001b3);
    }
    hash[8..16].copy_from_slice(&h2.to_le_bytes());
    hash
}

/// Derive a Python module path from a file path relative to the project root.
///
/// E.g., `src/mypackage/module.py` → `mypackage.module`
fn derive_module_path(file_path: &Path, project_root: &Path, separator: &str) -> String {
    let relative = file_path
        .strip_prefix(project_root)
        .unwrap_or(file_path);

    let mut parts: Vec<&str> = relative
        .components()
        .filter_map(|c| c.as_os_str().to_str())
        .collect();

    // Strip src/ prefix if present (common Python layout)
    if parts.first() == Some(&"src") {
        parts.remove(0);
    }

    // Strip file extension from last component
    if let Some(last) = parts.last_mut() {
        if let Some(stem) = last.strip_suffix(".py") {
            *last = stem;
        } else if let Some(stem) = last.strip_suffix(".pyi") {
            *last = stem;
        }
    }

    // Strip __init__ from end (package init files)
    if parts.last() == Some(&"__init__") {
        parts.pop();
    }

    parts.join(separator)
}

// ---------------------------------------------------------------------------
// Language config loading
// ---------------------------------------------------------------------------

impl LanguageConfig {
    /// Load a language config from a TOML string.
    pub fn from_toml(toml_str: &str) -> Result<Self, String> {
        // Minimal TOML parsing -- in production we'd use the `toml` crate.
        // For now, provide defaults for Python.
        let _ = toml_str;
        Ok(Self::python_default())
    }

    /// Default Python config (matches languages/python.toml).
    pub fn python_default() -> Self {
        LanguageConfig {
            name: "python".to_string(),
            file_extensions: vec!["py".to_string(), "pyi".to_string()],
            scope_creators: vec![
                ScopeCreator {
                    node_type: "module".to_string(),
                    kind: ScopeKind::Module,
                },
                ScopeCreator {
                    node_type: "function_definition".to_string(),
                    kind: ScopeKind::Function,
                },
                ScopeCreator {
                    node_type: "class_definition".to_string(),
                    kind: ScopeKind::Class,
                },
                ScopeCreator {
                    node_type: "lambda".to_string(),
                    kind: ScopeKind::Function,
                },
                ScopeCreator {
                    node_type: "list_comprehension".to_string(),
                    kind: ScopeKind::Comprehension,
                },
                ScopeCreator {
                    node_type: "set_comprehension".to_string(),
                    kind: ScopeKind::Comprehension,
                },
                ScopeCreator {
                    node_type: "dictionary_comprehension".to_string(),
                    kind: ScopeKind::Comprehension,
                },
                ScopeCreator {
                    node_type: "generator_expression".to_string(),
                    kind: ScopeKind::Comprehension,
                },
            ],
            scope_rules: {
                let mut rules = HashMap::new();
                rules.insert(
                    ScopeKind::Module,
                    ScopeRule {
                        is_closure_boundary: false,
                        names_visible_to_inner: true,
                    },
                );
                rules.insert(
                    ScopeKind::Function,
                    ScopeRule {
                        is_closure_boundary: false,
                        names_visible_to_inner: true,
                    },
                );
                rules.insert(
                    ScopeKind::Class,
                    ScopeRule {
                        is_closure_boundary: true,
                        names_visible_to_inner: false,
                    },
                );
                rules.insert(
                    ScopeKind::Comprehension,
                    ScopeRule {
                        is_closure_boundary: false,
                        names_visible_to_inner: true,
                    },
                );
                rules
            },
            import_resolution: "python".to_string(),
            module_separator: ".".to_string(),
            class_member_prefix: true,
            nested_function_prefix: true,
            locals_marker: "<locals>".to_string(),
            all_variable: Some("__all__".to_string()),
            public_by_default: true,
            private_prefix: "_".to_string(),
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_python(source: &str) -> tree_sitter::Tree {
        let mut parser = tree_sitter::Parser::new();
        let language = tree_sitter_python::LANGUAGE;
        parser
            .set_language(&language.into())
            .expect("Error loading Python grammar");
        parser.parse(source, None).expect("Error parsing")
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
        let tree = parse_python(source);
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
        assert!(
            module_scope.bindings.contains_key("x"),
            "Module scope missing 'x'"
        );
        assert!(
            module_scope.bindings.contains_key("foo"),
            "Module scope missing 'foo'"
        );
        assert!(
            module_scope.bindings.contains_key("Bar"),
            "Module scope missing 'Bar'"
        );
    }

    #[test]
    fn test_function_parameters() {
        let source = "def greet(name, greeting='hello'):\n    return f'{greeting} {name}'\n";
        let tree = parse_python(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();
        // Find the function scope
        let func_scope = file_scope
            .scopes
            .iter()
            .find(|s| s.kind == ScopeKind::Function)
            .expect("Should have a function scope");

        assert!(
            func_scope.bindings.contains_key("name"),
            "Function scope missing 'name' parameter"
        );
        assert!(
            func_scope.bindings.contains_key("greeting"),
            "Function scope missing 'greeting' parameter"
        );
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
        let tree = parse_python(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();

        assert!(
            file_scope.imports.contains_key("os"),
            "Missing 'os' import"
        );
        assert!(
            file_scope.imports.contains_key("OrderedDict"),
            "Missing 'OrderedDict' import"
        );
        assert!(
            file_scope.imports.contains_key("Optional"),
            "Missing 'Optional' import"
        );
        assert!(
            file_scope.imports.contains_key("List"),
            "Missing 'List' import"
        );
    }

    #[test]
    fn test_class_scope() {
        let source = r#"
class MyClass:
    class_var = 42

    def method(self, x):
        return self.class_var + x
"#;
        let tree = parse_python(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");
        resolver.index_file(&path, source, &tree);

        let file_scope = resolver.file_scopes.get(&path).unwrap();

        // Should have: module, MyClass, method
        let class_scope = file_scope
            .scopes
            .iter()
            .find(|s| s.kind == ScopeKind::Class)
            .expect("Should have a class scope");

        // Class scope should be marked as closure boundary
        let config = LanguageConfig::python_default();
        let rule = config.scope_rules.get(&ScopeKind::Class).unwrap();
        assert!(rule.is_closure_boundary);
        assert!(!rule.names_visible_to_inner);

        // class_var should be in the class scope
        assert!(
            class_scope.bindings.contains_key("class_var"),
            "Class scope missing 'class_var'"
        );
        // method should be in the class scope (function name bound in enclosing scope)
        assert!(
            class_scope.bindings.contains_key("method"),
            "Class scope missing 'method'"
        );
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
        let tree = parse_python(source);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/src/mymod/example.py");
        resolver.index_file(&path, source, &tree);

        // Check that definitions were registered in the QN index
        assert!(
            !resolver.qn_index.is_empty(),
            "QN index should not be empty"
        );

        // Should have entries for top_level, MyClass, and method
        let has_top_level = resolver
            .qn_index
            .keys()
            .any(|k| k.contains("top_level"));
        assert!(has_top_level, "QN index missing 'top_level'");

        let has_class = resolver.qn_index.keys().any(|k| k.contains("MyClass"));
        assert!(has_class, "QN index missing 'MyClass'");
    }

    #[test]
    fn test_derive_module_path() {
        assert_eq!(
            derive_module_path(Path::new("/project/src/mypackage/module.py"), Path::new("/project"), "."),
            "mypackage.module"
        );
        assert_eq!(
            derive_module_path(Path::new("/project/mypackage/__init__.py"), Path::new("/project"), "."),
            "mypackage"
        );
        assert_eq!(
            derive_module_path(Path::new("/project/src/pkg/sub/file.py"), Path::new("/project"), "."),
            "pkg.sub.file"
        );
    }

    #[test]
    fn test_incremental_reindex() {
        let source1 = "x = 1\n";
        let source2 = "x = 1\ny = 2\n";
        let tree1 = parse_python(source1);
        let tree2 = parse_python(source2);
        let config = LanguageConfig::python_default();
        let mut resolver = ScopeResolver::new(config, PathBuf::from("/project"));
        let path = PathBuf::from("/project/test.py");

        // Index once
        resolver.index_file(&path, source1, &tree1);
        let hash1 = resolver.file_scopes.get(&path).unwrap().content_hash;

        // Re-index with same content (should be a no-op)
        resolver.index_file(&path, source1, &tree1);

        // Re-index with different content
        resolver.index_file(&path, source2, &tree2);
        let hash2 = resolver.file_scopes.get(&path).unwrap().content_hash;
        assert_ne!(hash1, hash2, "Hash should change with different content");
    }
}
