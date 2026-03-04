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

        // Second pass: resolve all identifier/attribute references to QNs.
        let module_path = self
            .import_graph
            .file_to_module
            .get(path)
            .cloned()
            .unwrap_or_else(|| derive_module_path(path, &self.project_root, &self.config.module_separator));
        let (references, all_qnames) = self.collect_file_references(
            tree, path, source, &module_path, &ctx.scopes, &ctx.scope_index, &ctx.imports,
        );

        FileScope {
            content_hash,
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
        in_import: bool,
        refs: &mut Vec<Reference>,
        qn_set: &mut std::collections::HashSet<String>,
    ) {
        let node = cursor.node();
        let node_kind = node.kind();

        // Track whether we're inside an import statement.
        let child_in_import = in_import
            || node_kind == "import_statement"
            || node_kind == "import_from_statement";

        // Process identifier nodes (but not those that are part of an
        // attribute access — we handle those at the attribute level).
        if node_kind == "identifier" {
            // Skip if parent is an attribute (we handle dotted access separately)
            let parent = node.parent();
            let is_attr_part = parent.map_or(false, |p| p.kind() == "attribute");
            // Skip Python keywords that tree-sitter reports as identifiers
            let name = node_text(node, source);
            let is_keyword = matches!(name, "True" | "False" | "None");

            if !is_attr_part && !is_keyword {
                let kind = self.classify_reference(&node, in_import);
                if let Some(qn) = self.resolve_identifier(
                    name, node.start_byte(), module_path,
                    scopes, scope_index, imports,
                ) {
                    qn_set.insert(qn.clone());
                    refs.push(Reference {
                        file: file_path.to_path_buf(),
                        // Convert tree-sitter 0-indexed row to 1-indexed line.
                        line: node.start_position().row + 1,
                        column: node.start_position().column,
                        byte_offset: node.start_byte(),
                        qn: QualifiedName { name: qn },
                        kind,
                    });
                }
            }
        }

        // Process attribute access nodes (e.g., `obj.attr`)
        if node_kind == "attribute" {
            // Build the full dotted name and resolve.
            let kind = self.classify_reference(&node, in_import);
            if let Some(full_name) = self.collect_dotted_name(&node, source) {
                if let Some(qn) = self.resolve_dotted_name(
                    &full_name, node.start_byte(), module_path,
                    scopes, scope_index, imports,
                ) {
                    qn_set.insert(qn.clone());
                    refs.push(Reference {
                        file: file_path.to_path_buf(),
                        // Convert tree-sitter 0-indexed row to 1-indexed line.
                        line: node.start_position().row + 1,
                        column: node.start_position().column,
                        byte_offset: node.start_byte(),
                        qn: QualifiedName { name: qn },
                        kind,
                    });
                }
            }
        }

        // Recurse into children.
        if cursor.goto_first_child() {
            loop {
                self.walk_references(
                    cursor, file_path, source, module_path,
                    scopes, scope_index, imports, child_in_import,
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
        while let Some(parent) = current.parent() {
            let pk = parent.kind();
            match pk {
                "assignment" | "augmented_assignment" | "annotated_assignment" => {
                    if let Some(left) = parent.child_by_field_name("left") {
                        if left.byte_range().contains(&node.start_byte()) {
                            return true;
                        }
                    }
                    return false; // it's in the value (right) side
                }
                "for_in_clause" | "for_statement" => {
                    if let Some(left) = parent.child_by_field_name("left") {
                        if left.byte_range().contains(&node.start_byte()) {
                            return true;
                        }
                    }
                    return false;
                }
                "as_pattern" | "as_pattern_target" | "with_statement" => {
                    if pk == "with_statement" {
                         // with_statement itself doesn't have a simple 'left'
                         // it contains with_clause which contains as_pattern
                         continue;
                    }
                    return true;
                }
                "named_expression" => {
                    if let Some(name) = parent.child_by_field_name("name") {
                        if name.id() == node.id() {
                            return true;
                        }
                    }
                    return false;
                }
                "except_clause" => {
                    if let Some(name) = parent.child_by_field_name("name") {
                        if name.id() == node.id() {
                            return true;
                        }
                    }
                    return false;
                }
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
        byte_offset: usize,
        module_path: &str,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        imports: &HashMap<String, ImportBinding>,
    ) -> Option<String> {
        // 1. Look up in scope tree (innermost first)
        let enclosing = self.find_enclosing_scope(byte_offset, scopes);
        if let Some(scope_id) = enclosing {
            if let Some(qn) = self.resolve_in_scope_chain(
                name, scope_id, module_path, scopes, scope_index,
            ) {
                return Some(qn);
            }
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
        if is_python_builtin(name) {
            return Some(format!("builtins.{}", name));
        }

        None
    }

    /// Resolve a dotted name (e.g., "os.path.join") to its qualified name.
    fn resolve_dotted_name(
        &self,
        dotted: &str,
        byte_offset: usize,
        module_path: &str,
        scopes: &[Scope],
        scope_index: &HashMap<ScopeId, usize>,
        imports: &HashMap<String, ImportBinding>,
    ) -> Option<String> {
        let parts: Vec<&str> = dotted.splitn(2, '.').collect();
        if parts.len() < 2 {
            return self.resolve_identifier(dotted, byte_offset, module_path, scopes, scope_index, imports);
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
        let enclosing = self.find_enclosing_scope(byte_offset, scopes);
        if let Some(scope_id) = enclosing {
            if let Some(root_qn) = self.resolve_in_scope_chain(
                root, scope_id, module_path, scopes, scope_index,
            ) {
                return Some(format!("{}.{}", root_qn, rest));
            }
        }

        None
    }

    /// Find the innermost scope containing a byte offset.
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
            if let Some(rule) = self.config.scope_rules.get(&scope.kind) {
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
        let sep = &self.config.module_separator;

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
                self.collect_binding_targets(ctx, &left, scope_for_children, BindingKind::Assignment);
            }
        }

        // Collect for loop variables
        if node_kind == "for_in_clause" || node_kind == "for_statement" {
            if let Some(left) = node.child_by_field_name("left") {
                self.collect_binding_targets(ctx, &left, scope_for_children, BindingKind::Assignment);
            }
        }

        // Collect with statement variables
        if node_kind == "as_pattern" || node_kind == "as_pattern_target" || node_kind == "with_statement" {
             // In `with ... as x`, `x` is the target.
             if node_kind == "with_statement" {
                 // Tree-sitter python with_statement has with_clause children
                 // but let's check for as_pattern recursively via walk_node
             } else {
                 self.collect_binding_targets(ctx, &node, scope_for_children, BindingKind::Assignment);
             }
        }

        // Collect walrus operator variables
        if node_kind == "named_expression" {
            if let Some(name) = node.child_by_field_name("name") {
                self.collect_binding_targets(ctx, &name, scope_for_children, BindingKind::Assignment);
            }
        }

        // Collect exception handler variables
        if node_kind == "except_clause" {
            if let Some(name) = node.child_by_field_name("name") {
                self.collect_binding_targets(ctx, &name, scope_for_children, BindingKind::Assignment);
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

    /// Collect recursive binding targets (e.g., in a = (b, c)).
    fn collect_binding_targets(
        &self,
        ctx: &mut BuildContext,
        node: &tree_sitter::Node,
        scope_id: ScopeId,
        kind: BindingKind,
    ) {
        match node.kind() {
            "identifier" => {
                let name = ctx.text(*node);
                let binding = Binding {
                    name: name.to_string(),
                    kind,
                    line: node.start_position().row,
                    column: node.start_position().column,
                    byte_offset: node.start_byte(),
                };
                ctx.add_binding_if_absent(scope_id, binding);
            }
            "pattern_list" | "tuple_pattern" | "list_pattern" | "list_splat_pattern"
            | "as_pattern" | "as_pattern_target" => {
                for_each_child(node, |child| {
                    let ck = child.kind();
                    if ck != "," && ck != "(" && ck != ")" && ck != "[" && ck != "]" && ck != "as" {
                        self.collect_binding_targets(ctx, &child, scope_id, kind);
                    }
                });
            }
            _ => {}
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

/// Check if a name is a Python builtin.
fn is_python_builtin(name: &str) -> bool {
    matches!(name,
        "print" | "len" | "range" | "int" | "str" | "float" | "bool" | "list"
        | "dict" | "set" | "tuple" | "type" | "isinstance" | "issubclass"
        | "hasattr" | "getattr" | "setattr" | "delattr" | "property"
        | "staticmethod" | "classmethod" | "super" | "object" | "Exception"
        | "ValueError" | "TypeError" | "KeyError" | "IndexError" | "RuntimeError"
        | "AttributeError" | "ImportError" | "OSError" | "IOError" | "FileNotFoundError"
        | "NotImplementedError" | "StopIteration" | "GeneratorExit" | "SystemExit"
        | "AssertionError" | "NameError" | "ZeroDivisionError" | "OverflowError"
        | "abs" | "all" | "any" | "bin" | "bytes" | "callable" | "chr" | "complex"
        | "dir" | "divmod" | "enumerate" | "eval" | "exec" | "filter" | "format"
        | "frozenset" | "globals" | "hash" | "hex" | "id" | "input" | "iter"
        | "map" | "max" | "min" | "next" | "oct" | "open" | "ord" | "pow"
        | "repr" | "reversed" | "round" | "slice" | "sorted" | "sum" | "vars"
        | "zip" | "breakpoint" | "memoryview" | "bytearray"
        | "__name__" | "__file__" | "__doc__" | "__package__" | "__spec__"
        | "__import__" | "__build_class__"
    )
}

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
