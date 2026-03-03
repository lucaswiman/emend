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

use crate::symbols::node_text;

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
    Assignment,
    FunctionDef,
    ClassDef,
    Parameter,
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
    pub content_hash: [u8; 16],
    pub scopes: Vec<Scope>,
    pub imports: HashMap<String, ImportBinding>,
    pub definitions: Vec<(QualifiedName, Location)>,
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

#[derive(Debug, Clone)]
pub struct LanguageConfig {
    pub name: String,
    pub file_extensions: Vec<String>,
    pub scope_creators: Vec<ScopeCreator>,
    pub scope_rules: HashMap<ScopeKind, ScopeRule>,
    pub import_resolution: String,
    pub module_separator: String,
    pub class_member_prefix: bool,
    pub nested_function_prefix: bool,
    pub locals_marker: String,
    pub all_variable: Option<String>,
    pub public_by_default: bool,
    pub private_prefix: String,
}

#[derive(Debug, Clone)]
pub struct ScopeCreator {
    pub node_type: String,
    pub kind: ScopeKind,
}

#[derive(Debug, Clone)]
pub struct ScopeRule {
    pub is_closure_boundary: bool,
    pub names_visible_to_inner: bool,
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

    /// Index a single file.  Incremental: only re-indexes if content hash changed.
    pub fn index_file(&mut self, path: &Path, source: &str, tree: &tree_sitter::Tree) {
        let hash = content_hash(source);

        if let Some(existing) = self.file_scopes.get(path) {
            if existing.content_hash == hash {
                return;
            }
        }

        let file_scope = self.build_file_scope(path, source, tree, hash);

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
        let module_path = derive_module_path(path, &self.project_root, &self.config.module_separator);
        self.import_graph
            .module_to_file
            .insert(module_path.clone(), path.to_path_buf());
        self.import_graph
            .file_to_module
            .insert(path.to_path_buf(), module_path);

        self.file_scopes.insert(path.to_path_buf(), file_scope);
    }

    /// Resolve qualified names for all identifiers in a file.
    pub fn qualified_names(&self, path: &Path) -> Vec<(QualifiedName, Location)> {
        let _ = path;
        Vec::new() // TODO
    }

    /// Find all references to a qualified name across the project.
    pub fn find_references(&self, qn: &str) -> Vec<Reference> {
        let _ = qn;
        Vec::new() // TODO
    }

    /// Find the definition site for a reference at a given position.
    pub fn goto_definition(&self, path: &Path, line: usize, column: usize) -> Option<Location> {
        let _ = (path, line, column);
        None // TODO
    }

    /// Find dead code (unreferenced definitions) across the project.
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
            bindings: HashMap::new(),
        });

        // Walk the tree-sitter CST and build scopes
        let mut cursor = root_node.walk();
        self.walk_node(&mut cursor, module_id, &mut ctx);

        FileScope {
            content_hash,
            scopes: ctx.scopes,
            imports: ctx.imports,
            definitions: ctx.definitions,
        }
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
            if let Some(creator) = self.config.scope_creators.iter().find(|c| c.node_type == node_kind) {
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
                        bindings: HashMap::new(),
                    });

                    // Function/class names are bound in the ENCLOSING scope
                    if creator.kind == ScopeKind::Function || creator.kind == ScopeKind::Class {
                        if let Some(name_node) = node.child_by_field_name("name") {
                            let name = ctx.text(name_node).to_string();
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
                        if let Some(params) = node.child_by_field_name("parameters") {
                            Self::collect_parameters(ctx, &params, scope_id);
                        }
                    }

                    scope_id
                }
            } else {
                current_scope
            };

        // Collect imports
        if node_kind == "import_statement" || node_kind == "import_from_statement" {
            Self::collect_import(ctx, &node);
        }

        // Collect assignments
        if node_kind == "assignment" || node_kind == "augmented_assignment" || node_kind == "annotated_assignment" {
            if let Some(left) = node.child_by_field_name("left") {
                if left.kind() == "identifier" {
                    let name = ctx.text(left);
                    let binding = Binding {
                        name: name.to_string(),
                        kind: BindingKind::Assignment,
                        line: left.start_position().row,
                        column: left.start_position().column,
                        byte_offset: left.start_byte(),
                    };
                    ctx.add_binding_if_absent(scope_for_children, binding);
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
            for_each_child_of_kind(&node, "identifier", |child| {
                let name = ctx.text(child);
                let binding = Binding {
                    name: name.to_string(),
                    kind,
                    line: child.start_position().row,
                    column: child.start_position().column,
                    byte_offset: child.start_byte(),
                };
                ctx.add_binding(scope_for_children, binding);
            });
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

    /// Collect function parameters as bindings.
    fn collect_parameters(
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
            let name = match child.kind() {
                "identifier" => Some(ctx.text(child)),
                "default_parameter" | "typed_parameter" | "typed_default_parameter" => {
                    child.child_by_field_name("name").map(|n| ctx.text(n))
                }
                "list_splat_pattern" | "dictionary_splat_pattern" => {
                    find_first_child_of_kind(&child, "identifier").map(|n| ctx.text(n))
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
                ctx.add_binding(scope_id, binding);
            }

            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }

    /// Collect imports from an import statement node.
    fn collect_import(ctx: &mut BuildContext, node: &tree_sitter::Node) {
        match node.kind() {
            "import_statement" => {
                for_each_child(node, |child| {
                    if child.kind() != "dotted_name" && child.kind() != "aliased_import" {
                        return;
                    }
                    let (local_name, module_path) = if child.kind() == "aliased_import" {
                        let name_node = child.child_by_field_name("name");
                        let alias_node = child.child_by_field_name("alias");
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
            }
            "import_from_statement" => {
                let module_path = node
                    .child_by_field_name("module_name")
                    .map(|n| ctx.text(n).to_string())
                    .unwrap_or_default();

                for_each_child(node, |child| {
                    match child.kind() {
                        "wildcard_import" => {
                            ctx.add_import(ImportBinding {
                                local_name: "*".to_string(),
                                module_path: module_path.clone(),
                                imported_name: None,
                                is_star: true,
                            });
                        }
                        "dotted_name" | "identifier" => {
                            let name = ctx.text(child).to_string();
                            ctx.add_import(ImportBinding {
                                local_name: name.clone(),
                                module_path: module_path.clone(),
                                imported_name: Some(name),
                                is_star: false,
                            });
                        }
                        "aliased_import" => {
                            if let Some(name_node) = child.child_by_field_name("name") {
                                let imported = ctx.text(name_node).to_string();
                                let local = child
                                    .child_by_field_name("alias")
                                    .map(|a| ctx.text(a).to_string())
                                    .unwrap_or_else(|| imported.clone());
                                ctx.add_import(ImportBinding {
                                    local_name: local,
                                    module_path: module_path.clone(),
                                    imported_name: Some(imported),
                                    is_star: false,
                                });
                            }
                        }
                        _ => {}
                    }
                });
            }
            _ => {}
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
        let sep = &self.config.module_separator;

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
                        .unwrap_or_else(|| derive_module_path(file_path, &self.project_root, sep));
                    parts.push(module_path);
                    break;
                }
                ScopeKind::Function if self.config.nested_function_prefix => {
                    if let Some(parent_id) = scope.parent {
                        if let Some(&pidx) = scope_index.get(&parent_id) {
                            let parent = &scopes[pidx];
                            if let Some(bname) = find_scope_name(parent, scope, BindingKind::FunctionDef) {
                                parts.push(self.config.locals_marker.clone());
                                parts.push(bname.to_string());
                            }
                        }
                    }
                }
                ScopeKind::Class if self.config.class_member_prefix => {
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

/// Find the first child of a node that matches a specific kind.
fn find_first_child_of_kind<'a>(
    node: &tree_sitter::Node<'a>,
    kind: &str,
) -> Option<tree_sitter::Node<'a>> {
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            let child = cursor.node();
            if child.kind() == kind {
                return Some(child);
            }
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
    None
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

/// Derive a Python module path from a file path relative to the project root.
///
/// E.g., `src/mypackage/module.py` → `mypackage.module`
fn derive_module_path(file_path: &Path, project_root: &Path, separator: &str) -> String {
    let relative = file_path
        .strip_prefix(project_root)
        .unwrap_or(file_path);

    let parts: Vec<&str> = relative
        .components()
        .filter_map(|c| c.as_os_str().to_str())
        // Skip leading "src" directory
        .skip_while(|&c| c == "src")
        .collect();

    let mut parts = parts;

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
    pub fn from_toml(toml_str: &str) -> Result<Self, String> {
        let _ = toml_str;
        Ok(Self::python_default())
    }

    pub fn python_default() -> Self {
        LanguageConfig {
            name: "python".to_string(),
            file_extensions: vec!["py".to_string(), "pyi".to_string()],
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
            scope_rules: HashMap::from([
                (ScopeKind::Module, ScopeRule { is_closure_boundary: false, names_visible_to_inner: true }),
                (ScopeKind::Function, ScopeRule { is_closure_boundary: false, names_visible_to_inner: true }),
                (ScopeKind::Class, ScopeRule { is_closure_boundary: true, names_visible_to_inner: false }),
                (ScopeKind::Comprehension, ScopeRule { is_closure_boundary: false, names_visible_to_inner: true }),
            ]),
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
        let rule = config.scope_rules.get(&ScopeKind::Class).unwrap();
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
}
