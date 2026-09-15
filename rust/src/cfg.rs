//! Per-function control flow graph construction from tree-sitter ASTs.
//!
//! Builds basic-block CFGs for functions in any supported language.
//! Language-specific tree-sitter node types and field names are driven by
//! the `[cfg]` section of the language config TOML (see [`CfgSection`]).

use std::collections::{HashMap, HashSet, VecDeque};
use crate::scope::{config_for_ext, CfgSection, LanguageConfig};

// ---------------------------------------------------------------------------
// Data model
// ---------------------------------------------------------------------------

/// Unique identifier for a basic block within a function CFG.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct BlockId(pub u32);

/// The kind of control-flow edge between basic blocks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeKind {
    /// Normal sequential flow (fallthrough).
    Fallthrough,
    /// Condition was true.
    TrueBranch,
    /// Condition was false.
    FalseBranch,
    /// Exception raised (try body → except handler).
    Exception,
    /// Finally clause (always taken).
    Finally,
    /// Loop back-edge (end of body → loop header).
    BackEdge,
    /// Explicit jump (return, raise, break, continue).
    Jump,
}

impl EdgeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            EdgeKind::Fallthrough => "fallthrough",
            EdgeKind::TrueBranch => "true_branch",
            EdgeKind::FalseBranch => "false_branch",
            EdgeKind::Exception => "exception",
            EdgeKind::Finally => "finally",
            EdgeKind::BackEdge => "back_edge",
            EdgeKind::Jump => "jump",
        }
    }
}

/// A basic block: a maximal sequence of statements with no internal branches.
#[derive(Debug, Clone)]
pub struct BasicBlock {
    pub id: BlockId,
    pub start_byte: usize,
    pub end_byte: usize,
    pub start_line: u32,
    pub end_line: u32,
    /// Byte ranges of statements in this block.
    pub statements: Vec<(usize, usize)>,
    /// Variable definitions: (name, line, col, kind).
    /// Kind is "write", "aug_write", or "del".
    pub defs: Vec<(String, u32, u32, String)>,
    /// Variable uses: (name, line, col, kind).
    /// Kind is "read".
    pub uses: Vec<(String, u32, u32, String)>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct FinallyTransition {
    pub scope: u32,
    pub reason: u32,
    pub entering: bool,
}

/// A control-flow edge.
#[derive(Debug, Clone)]
pub struct CfgEdge {
    pub from: BlockId,
    pub to: BlockId,
    pub kind: EdgeKind,
    /// For conditional edges: byte range of the condition expression.
    pub condition: Option<(usize, usize)>,
    /// Immutable syntactic region whose evaluations may enter this handler.
    pub protected_range: Option<(usize, usize)>,
    pub shadows_outer: bool,
    pub resume_from: Option<BlockId>,
    pub finally_transitions: Vec<FinallyTransition>,
    /// Return evaluation block whose value this normal completion commits.
    pub return_value_from: Option<BlockId>,
}

/// The complete CFG for one function.
#[derive(Debug, Clone)]
pub struct FunctionCfg {
    pub func_name: String,
    pub func_start_line: u32,
    pub func_end_line: u32,
    pub entry: BlockId,
    pub exit: BlockId,
    pub blocks: Vec<BasicBlock>,
    pub edges: Vec<CfgEdge>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FlowEvent {
    pub id: u32,
    pub func_id: String,
    pub func_name: String,
    pub func_start: usize,
    pub role: String,
    pub var: Option<String>,
    pub access_path: Option<String>,
    pub block: u32,
    pub start_byte: usize,
    pub end_byte: usize,
    pub start_line: u32,
    pub start_col: u32,
    pub end_line: u32,
    pub end_col: u32,
    pub ordinal: u32,
    pub call_id: Option<u32>,
    pub arg_index: Option<u32>,
    pub arg_name: Option<String>,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct FlowEdge {
    pub from: u32,
    pub to: u32,
    pub kind: String,
}

#[derive(Debug, Clone, Default)]
pub struct FlowFacts {
    pub events: Vec<FlowEvent>,
    pub edges: Vec<FlowEdge>,
}

// ---------------------------------------------------------------------------
// CFG queries (predecessors, successors, dominators)
// ---------------------------------------------------------------------------

impl FunctionCfg {
    pub fn predecessors(&self, block_id: BlockId) -> Vec<BlockId> {
        self.edges
            .iter()
            .filter(|e| e.to == block_id)
            .map(|e| e.from)
            .collect()
    }

    pub fn successors(&self, block_id: BlockId) -> Vec<BlockId> {
        self.edges
            .iter()
            .filter(|e| e.from == block_id)
            .map(|e| e.to)
            .collect()
    }

    /// Build predecessor adjacency list (block_id → list of predecessors).
    fn pred_adj(&self) -> HashMap<BlockId, Vec<BlockId>> {
        let mut adj: HashMap<BlockId, Vec<BlockId>> = HashMap::new();
        for b in &self.blocks {
            adj.insert(b.id, Vec::new());
        }
        for e in &self.edges {
            adj.entry(e.to).or_default().push(e.from);
        }
        adj
    }

    /// Build successor adjacency list (block_id → list of successors).
    fn succ_adj(&self) -> HashMap<BlockId, Vec<BlockId>> {
        let mut adj: HashMap<BlockId, Vec<BlockId>> = HashMap::new();
        for b in &self.blocks {
            adj.insert(b.id, Vec::new());
        }
        for e in &self.edges {
            adj.entry(e.from).or_default().push(e.to);
        }
        adj
    }

    /// Iterative dominator fixpoint using pre-built adjacency lists.
    fn compute_dominator_map(
        blocks: &[BasicBlock],
        adj: &HashMap<BlockId, Vec<BlockId>>,
        root: BlockId,
    ) -> HashMap<BlockId, HashSet<BlockId>> {
        let all_ids: HashSet<BlockId> = blocks.iter().map(|b| b.id).collect();
        let mut dom: HashMap<BlockId, HashSet<BlockId>> = HashMap::new();

        for b in blocks {
            if b.id == root {
                let mut s = HashSet::new();
                s.insert(root);
                dom.insert(b.id, s);
            } else {
                dom.insert(b.id, all_ids.clone());
            }
        }

        let mut changed = true;
        while changed {
            changed = false;
            for b in blocks {
                if b.id == root {
                    continue;
                }
                let neighbors = adj.get(&b.id).map(|v| v.as_slice()).unwrap_or(&[]);
                if neighbors.is_empty() {
                    let mut s = HashSet::new();
                    s.insert(b.id);
                    if dom[&b.id] != s {
                        dom.insert(b.id, s);
                        changed = true;
                    }
                    continue;
                }
                let mut new_dom = dom[&neighbors[0]].clone();
                for p in &neighbors[1..] {
                    let pd = &dom[p];
                    new_dom.retain(|x| pd.contains(x));
                }
                new_dom.insert(b.id);
                if dom[&b.id] != new_dom {
                    dom.insert(b.id, new_dom);
                    changed = true;
                }
            }
        }

        dom
    }

    /// Compute the set of dominators for a given block.
    pub fn dominators(&self, block_id: BlockId) -> HashSet<BlockId> {
        let adj = self.pred_adj();
        let dom = Self::compute_dominator_map(&self.blocks, &adj, self.entry);
        dom.get(&block_id).cloned().unwrap_or_default()
    }

    /// Compute post-dominators: dominators on the reverse CFG from exit.
    pub fn post_dominators(&self, block_id: BlockId) -> HashSet<BlockId> {
        let adj = self.succ_adj();
        let dom = Self::compute_dominator_map(&self.blocks, &adj, self.exit);
        dom.get(&block_id).cloned().unwrap_or_default()
    }

    /// Compute the full dominator map for all blocks at once.
    pub fn all_dominators(&self) -> HashMap<BlockId, HashSet<BlockId>> {
        let adj = self.pred_adj();
        Self::compute_dominator_map(&self.blocks, &adj, self.entry)
    }

    /// Compute the full post-dominator map for all blocks at once.
    pub fn all_post_dominators(&self) -> HashMap<BlockId, HashSet<BlockId>> {
        let adj = self.succ_adj();
        Self::compute_dominator_map(&self.blocks, &adj, self.exit)
    }
}

// ---------------------------------------------------------------------------
// CFG builder
// ---------------------------------------------------------------------------

struct CfgBuilder<'a> {
    source: &'a [u8],
    cfg_sec: &'a CfgSection,
    blocks: Vec<BasicBlock>,
    edges: Vec<CfgEdge>,
    next_id: u32,
    exit_block: BlockId,
    /// Stack of (loop_header, loop_exit) for break/continue.
    loop_stack: Vec<(BlockId, BlockId)>,
    finally_jumps: Vec<usize>,
}

impl<'a> CfgBuilder<'a> {
    fn new(source: &'a [u8], cfg_sec: &'a CfgSection) -> Self {
        let mut builder = CfgBuilder {
            source,
            cfg_sec,
            blocks: Vec::new(),
            edges: Vec::new(),
            next_id: 0,
            exit_block: BlockId(0), // placeholder
            loop_stack: Vec::new(),
            finally_jumps: Vec::new(),
        };
        // Allocate entry block
        let _entry = builder.new_block_at(0, 0, 0);
        // Allocate exit block (synthetic)
        let exit = builder.new_block_at(0, 0, 0);
        builder.exit_block = exit;
        builder
    }

    fn new_block_at(&mut self, start_byte: usize, start_line: u32, end_line: u32) -> BlockId {
        let id = BlockId(self.next_id);
        self.next_id += 1;
        self.blocks.push(BasicBlock {
            id,
            start_byte,
            end_byte: start_byte,
            start_line,
            end_line,
            statements: Vec::new(),
            defs: Vec::new(),
            uses: Vec::new(),
        });
        id
    }

    fn new_block_from_node(&mut self, node: tree_sitter::Node) -> BlockId {
        self.new_block_at(
            node.start_byte(),
            node.start_position().row as u32,
            node.end_position().row as u32,
        )
    }

    fn add_edge(&mut self, from: BlockId, to: BlockId, kind: EdgeKind) {
        self.add_edge_cond(from, to, kind, None);
    }

    fn add_edge_cond(
        &mut self,
        from: BlockId,
        to: BlockId,
        kind: EdgeKind,
        cond: Option<(usize, usize)>,
    ) {
        self.edges.push(CfgEdge {
            from,
            to,
            kind,
            condition: cond,
            protected_range: None,
            shadows_outer: false,
            resume_from: None,
            finally_transitions: Vec::new(),
            return_value_from: None,
        });
    }

    fn add_exception_edge(&mut self, from: BlockId, to: BlockId, region: tree_sitter::Node, shadows_outer: bool) {
        self.edges.push(CfgEdge {
            from, to, kind: EdgeKind::Exception, condition: None,
            protected_range: Some((region.start_byte(), region.end_byte())),
            shadows_outer, resume_from: None, finally_transitions: Vec::new(),
            return_value_from: None,
        });
    }

    fn tag_finally_edge(&mut self, scope: BlockId, reason: u32, entering: bool) {
        self.edges.last_mut().unwrap().finally_transitions.push(FinallyTransition {
            scope: scope.0, reason, entering,
        });
    }

    fn route_finally_jumps(&mut self, region: tree_sitter::Node, finalizer: BlockId) -> Vec<(BlockId, EdgeKind, u32)> {
        let inside = |block: &BasicBlock| region.start_byte() <= block.start_byte
            && block.start_byte < region.end_byte();
        let mut continuations = Vec::new();
        for &index in &self.finally_jumps {
            let edge = &mut self.edges[index];
            if inside(&self.blocks[edge.from.0 as usize]) && !inside(&self.blocks[edge.to.0 as usize]) {
                let reason = continuations.len() as u32 + 2;
                continuations.push((edge.to, edge.kind, reason));
                edge.finally_transitions.push(FinallyTransition { scope: finalizer.0, reason, entering: true });
                edge.to = finalizer;
                edge.kind = EdgeKind::Finally;
            }
        }
        continuations
    }

    fn resume_finally_jumps(&mut self, end: BlockId, scope: BlockId, continuations: Vec<(BlockId, EdgeKind, u32)>) {
        for (target, kind, reason) in continuations {
            self.add_edge(end, target, kind);
            self.tag_finally_edge(scope, reason, false);
            self.finally_jumps.push(self.edges.len() - 1);
        }
    }

    fn block_mut(&mut self, block_id: BlockId) -> &mut BasicBlock {
        &mut self.blocks[block_id.0 as usize]
    }

    fn update_block_range(&mut self, block_id: BlockId, node: tree_sitter::Node) {
        let b = self.block_mut(block_id);
        if b.start_byte == 0 && b.start_line == 0 && b.statements.is_empty() {
            b.start_byte = node.start_byte();
            b.start_line = node.start_position().row as u32;
        }
        b.end_byte = node.end_byte();
        b.end_line = node.end_position().row as u32;
    }

    fn add_statement(&mut self, block_id: BlockId, node: tree_sitter::Node) {
        self.update_block_range(block_id, node);
        let b = self.block_mut(block_id);
        b.statements.push((node.start_byte(), node.end_byte()));
    }

    fn node_text(&self, node: tree_sitter::Node) -> &str {
        node.utf8_text(self.source).unwrap_or("")
    }

    // ---- Def/use extraction ----------------------------------------------

    fn collect_defs_uses(&mut self, block_id: BlockId, node: tree_sitter::Node) {
        let kind = node.kind();

        // Check delete_nodes first (del x → def with kind="del")
        if self.cfg_sec.delete_nodes.iter().any(|n| n == kind) {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if child.is_named() {
                    self.collect_defs_from_target(block_id, child, "del");
                }
            }
            return;
        }

        // Check def_use_rules from config
        for rule in &self.cfg_sec.def_use_rules {
            if kind == rule.node {
                // Determine the def kind based on the node type.
                // Augmented assignments (+=, -=, etc.) read-then-write the target.
                let is_augmented = kind.contains("augmented");
                let def_kind = if is_augmented { "aug_write" } else { "write" };

                if let Some(target) = node.child_by_field_name(&rule.target) {
                    self.collect_defs_from_target(block_id, target, def_kind);
                    // For augmented assignment, the target is also read.
                    if is_augmented {
                        self.collect_uses_from_expr(block_id, target);
                    }
                }
                if let Some(value) = node.child_by_field_name(&rule.value) {
                    self.collect_uses_from_expr(block_id, value);
                }
                return;
            }
        }

        // Return/throw: uses from the value expression
        if self.cfg_sec.return_nodes.iter().any(|n| n == kind)
            || self.cfg_sec.throw_nodes.iter().any(|n| n == kind)
        {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if child.is_named() {
                    self.collect_uses_from_expr(block_id, child);
                }
            }
            return;
        }

        // Expression statement: unwrap and recurse
        if !self.cfg_sec.expression_statement_node.is_empty()
            && kind == self.cfg_sec.expression_statement_node
        {
            if let Some(child) = node.child(0) {
                self.collect_defs_uses(block_id, child);
            }
            return;
        }

        // Fallback: recurse into named children looking for def_use_rule matches
        // (e.g., lexical_declaration > variable_declarator in TS)
        let mut found_rule = false;
        let rule_nodes: Vec<String> = self.cfg_sec.def_use_rules.iter().map(|r| r.node.clone()).collect();
        let mut cursor = node.walk();
        let children: Vec<_> = node.children(&mut cursor).filter(|c| c.is_named()).collect();
        for child in children {
            if rule_nodes.iter().any(|r| r == child.kind()) {
                self.collect_defs_uses(block_id, child);
                found_rule = true;
            }
        }
        if !found_rule {
            self.collect_uses_from_expr(block_id, node);
        }
    }

    /// Build a dotted qualified name from an attribute access node (e.g. "obj.field").
    /// Returns None if the object is too complex (e.g. a function call).
    fn collect_dotted_name(&self, node: tree_sitter::Node) -> Option<String> {
        let kind = node.kind();
        let id_node = &self.cfg_sec.identifier_node;
        let attr_node = &self.cfg_sec.attribute_node;

        if !id_node.is_empty() && kind == id_node {
            let name = self.node_text(node);
            if self.cfg_sec.skip_identifiers.iter().any(|s| s == name) {
                return None;
            }
            return Some(name.to_string());
        }
        if !attr_node.is_empty() && kind == attr_node {
            let obj_field = &self.cfg_sec.attribute_object_field;
            let name_field = &self.cfg_sec.attribute_name_field;
            if let (Some(obj), Some(attr)) = (
                node.child_by_field_name(obj_field.as_str()),
                node.child_by_field_name(name_field.as_str()),
            ) {
                let prefix = self.collect_dotted_name(obj)?;
                let attr_name = self.node_text(attr);
                return Some(format!("{}.{}", prefix, attr_name));
            }
        }
        None
    }

    /// Build a subscript qualified name like "data['key']" from a subscript node.
    /// Returns None if the index is not a simple string literal.
    fn collect_subscript_name(&self, node: tree_sitter::Node) -> Option<String> {
        let sub_node = &self.cfg_sec.subscript_node;
        if sub_node.is_empty() || node.kind() != sub_node {
            return None;
        }
        let val_field = &self.cfg_sec.subscript_value_field;
        let idx_field = &self.cfg_sec.subscript_index_field;
        if val_field.is_empty() || idx_field.is_empty() {
            return None;
        }
        let obj = node.child_by_field_name(val_field.as_str())?;
        let idx = node.child_by_field_name(idx_field.as_str())?;
        // Only handle string literal keys
        if !self.cfg_sec.string_nodes.iter().any(|s| s == idx.kind()) {
            return None;
        }
        // Get the base name (identifier or dotted attribute)
        let base = if !self.cfg_sec.identifier_node.is_empty() && obj.kind() == self.cfg_sec.identifier_node {
            let name = self.node_text(obj);
            if self.cfg_sec.skip_identifiers.iter().any(|s| s == name) {
                return None;
            }
            name.to_string()
        } else {
            self.collect_dotted_name(obj)?
        };
        let key_text = self.node_text(idx);
        Some(format!("{}[{}]", base, key_text))
    }

    /// Get the base object node from an attribute or subscript access node.
    /// Uses pre-copied field names to avoid borrow conflicts with mutable self.
    fn find_access_base<'b>(&self, node: tree_sitter::Node<'b>) -> Option<tree_sitter::Node<'b>> {
        let kind = node.kind();
        let attr_node = &self.cfg_sec.attribute_node;
        let sub_node = &self.cfg_sec.subscript_node;
        if !attr_node.is_empty() && kind == attr_node {
            return node.child_by_field_name(self.cfg_sec.attribute_object_field.as_str());
        }
        if !sub_node.is_empty() && kind == sub_node {
            return node.child_by_field_name(self.cfg_sec.subscript_value_field.as_str());
        }
        None
    }

    fn collect_defs_from_target(&mut self, block_id: BlockId, node: tree_sitter::Node, def_kind: &str) {
        let kind = node.kind();
        let id_node = &self.cfg_sec.identifier_node;

        if !id_node.is_empty() && kind == id_node {
            let name = self.node_text(node).to_string();
            let line = node.start_position().row as u32;
            let col = node.start_position().column as u32;
            self.block_mut(block_id).defs.push((name, line, col, def_kind.to_string()));
            return;
        }

        // Destructuring targets
        if self.cfg_sec.destructure_nodes.iter().any(|n| n == kind) {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if child.is_named() {
                    self.collect_defs_from_target(block_id, child, def_kind);
                }
            }
            return;
        }

        // Attribute access on LHS → emit qualified name as def, base as use
        if self.cfg_sec.attribute_access_nodes.iter().any(|n| n == kind) {
            // Try to build a qualified name for field-level / subscript tracking
            let qualified = self.collect_dotted_name(node)
                .or_else(|| self.collect_subscript_name(node));
            if let Some(qname) = qualified {
                let line = node.start_position().row as u32;
                let col = node.start_position().column as u32;
                // Find base object node before mutable borrow
                let base_node = self.find_access_base(node);
                self.block_mut(block_id).defs.push((qname, line, col, def_kind.to_string()));
                // Also emit the base object as a use (reading obj to set obj.field)
                if let Some(base) = base_node {
                    self.collect_uses_from_expr(block_id, base);
                }
            } else {
                // Fallback: treat as pure uses (complex expressions)
                self.collect_uses_from_expr(block_id, node);
            }
            return;
        }
    }

    fn collect_uses_from_expr(&mut self, block_id: BlockId, node: tree_sitter::Node) {
        let kind = node.kind();
        let id_node = &self.cfg_sec.identifier_node;

        if !id_node.is_empty() && kind == id_node {
            let name = self.node_text(node).to_string();
            if self.cfg_sec.skip_identifiers.iter().any(|s| s == &name) {
                return;
            }
            let line = node.start_position().row as u32;
            let col = node.start_position().column as u32;
            self.block_mut(block_id).uses.push((name, line, col, "read".to_string()));
            return;
        }
        // Attribute/subscript access → emit qualified name as use
        if self.cfg_sec.attribute_access_nodes.iter().any(|n| n == kind) {
            let qualified = self.collect_dotted_name(node)
                .or_else(|| self.collect_subscript_name(node));
            if let Some(qname) = qualified {
                let line = node.start_position().row as u32;
                let col = node.start_position().column as u32;
                let base_node = self.find_access_base(node);
                self.block_mut(block_id).uses.push((qname, line, col, "read".to_string()));
                // Also emit the base object as a use for backward compat
                if let Some(base) = base_node {
                    self.collect_uses_from_expr(block_id, base);
                }
                return;
            }
        }
        // Recurse into children
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.is_named() {
                self.collect_uses_from_expr(block_id, child);
            }
        }
    }

    // ---- Statement dispatch -----------------------------------------------

    /// Walk statements in a body node. Returns the current block after
    /// processing (may differ from `current` if a terminator was hit).
    fn walk_body(&mut self, body: tree_sitter::Node, mut current: BlockId) -> Option<BlockId> {
        // If the body node itself is a control-flow terminator (e.g. a
        // return_expression used as a Rust match-arm value), handle it as a
        // single statement instead of iterating its children.
        let bk = body.kind();
        if self.cfg_sec.return_nodes.iter().any(|n| n == bk)
            || self.cfg_sec.throw_nodes.iter().any(|n| n == bk)
            || self.cfg_sec.break_nodes.iter().any(|n| n == bk)
            || self.cfg_sec.continue_nodes.iter().any(|n| n == bk)
        {
            return self.walk_statement(body, current);
        }

        // Collect children upfront: tree-sitter cursors can't be shared with
        // recursive walks, and the builder mutates self during iteration.
        let mut cursor = body.walk();
        let children: Vec<_> = body.children(&mut cursor)
            .filter(|c| c.is_named() && c.kind() != "comment" && c.kind() != "line_comment"
                    && c.kind() != "block_comment")
            .collect();
        let mut terminated = false;

        for child in children {
            if terminated {
                // After a terminator, remaining statements go into an
                // unreachable block (no incoming edges).
                let unreachable = self.new_block_from_node(child);
                current = unreachable;
                terminated = false;
            }
            match self.walk_statement(child, current) {
                Some(next) => current = next,
                None => {
                    terminated = true;
                }
            }
        }

        if terminated {
            None
        } else {
            Some(current)
        }
    }

    /// Process a single statement. Returns the block to continue from,
    /// or None if control flow was terminated.
    fn walk_statement(
        &mut self,
        node: tree_sitter::Node,
        current: BlockId,
    ) -> Option<BlockId> {
        let kind = node.kind();

        // Unwrap expression_statement to check if inner is a control flow node.
        // Languages like Rust wrap if/match/loop expressions in expression_statement.
        if !self.cfg_sec.expression_statement_node.is_empty()
            && kind == self.cfg_sec.expression_statement_node
        {
            if let Some(inner) = node.named_child(0) {
                let ik = inner.kind();
                let is_cf = self.cfg_sec.if_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.for_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.c_style_for_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.while_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.loop_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.try_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.match_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.return_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.throw_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.break_nodes.iter().any(|n| n == ik)
                    || self.cfg_sec.continue_nodes.iter().any(|n| n == ik);
                if is_cf {
                    return self.walk_statement(inner, current);
                }
            }
        }

        if self.cfg_sec.if_nodes.iter().any(|n| n == kind) {
            return self.walk_if(node, current);
        }
        if self.cfg_sec.for_nodes.iter().any(|n| n == kind) {
            return self.walk_for(node, current);
        }
        if self.cfg_sec.c_style_for_nodes.iter().any(|n| n == kind) {
            return self.walk_c_style_for(node, current);
        }
        if self.cfg_sec.while_nodes.iter().any(|n| n == kind) {
            return self.walk_while(node, current);
        }
        if self.cfg_sec.loop_nodes.iter().any(|n| n == kind) {
            return self.walk_infinite_loop(node, current);
        }
        if self.cfg_sec.try_nodes.iter().any(|n| n == kind) {
            return self.walk_try(node, current);
        }
        if self.cfg_sec.with_nodes.iter().any(|n| n == kind) {
            return self.walk_with(node, current);
        }
        if self.cfg_sec.match_nodes.iter().any(|n| n == kind) {
            return self.walk_match(node, current);
        }
        if self.cfg_sec.return_nodes.iter().any(|n| n == kind)
            || self.cfg_sec.throw_nodes.iter().any(|n| n == kind)
        {
            self.add_statement(current, node);
            self.collect_defs_uses(current, node);
            if self.cfg_sec.return_nodes.iter().any(|n| n == kind) {
                // Outside every protected source span: finalizers intercept the
                // evaluation-to-completion jump, preserving this exact return.
                let completion = self.new_block_at(0, 0, 0);
                self.add_edge(completion, self.exit_block, EdgeKind::Fallthrough);
                self.edges.last_mut().unwrap().return_value_from = Some(current);
                self.add_edge(current, completion, EdgeKind::Jump);
                self.finally_jumps.push(self.edges.len() - 1);
            } else {
                self.add_edge(current, self.exit_block, EdgeKind::Jump);
            }
            return None;
        }
        if self.cfg_sec.break_nodes.iter().any(|n| n == kind) {
            self.add_statement(current, node);
            if let Some(&(_, loop_exit)) = self.loop_stack.last() {
                self.add_edge(current, loop_exit, EdgeKind::Jump);
                self.finally_jumps.push(self.edges.len() - 1);
            }
            return None;
        }
        if self.cfg_sec.continue_nodes.iter().any(|n| n == kind) {
            self.add_statement(current, node);
            if let Some(&(loop_header, _)) = self.loop_stack.last() {
                self.add_edge(current, loop_header, EdgeKind::BackEdge);
                self.finally_jumps.push(self.edges.len() - 1);
            }
            return None;
        }
        if self.cfg_sec.assert_nodes.iter().any(|n| n == kind) {
            self.add_statement(current, node);
            self.collect_defs_uses(current, node);
            return Some(current);
        }
        // Compound definitions (functions, classes) — skip their bodies
        if self.cfg_sec.definition_nodes.iter().any(|n| n == kind) {
            self.add_statement(current, node);
            return Some(current);
        }
        // Decorated definitions
        if !self.cfg_sec.decorated_node.is_empty() && kind == self.cfg_sec.decorated_node {
            self.add_statement(current, node);
            return Some(current);
        }

        // Simple statement: assignment, expression, import, etc.
        self.add_statement(current, node);
        self.collect_defs_uses(current, node);
        Some(current)
    }

    // ---- Compound statement handlers --------------------------------------

    fn walk_if(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        if self.cfg_sec.if_style == "alternative" {
            self.walk_if_alternative(node, current)
        } else {
            self.walk_if_children(node, current)
        }
    }

    /// Python-style if: elif_clause / else_clause as direct children.
    fn walk_if_children(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        let cond_field = &self.cfg_sec.condition_field;
        let cons_field = &self.cfg_sec.consequence_field;
        let elif_kind = &self.cfg_sec.elif_clause;
        let else_kind = &self.cfg_sec.else_clause;
        let body_field = &self.cfg_sec.body_field;

        let condition = node.child_by_field_name(cond_field);
        let cond_range = condition.map(|c| (c.start_byte(), c.end_byte()));

        if let Some(cond) = condition {
            self.collect_uses_from_expr(current, cond);
        }

        // True branch (consequence)
        if let Some(body) = node.child_by_field_name(cons_field) {
            let true_block = self.new_block_from_node(body);
            self.add_edge_cond(current, true_block, EdgeKind::TrueBranch, cond_range);
            if let Some(end) = self.walk_body(body, true_block) {
                self.add_edge(end, join, EdgeKind::Fallthrough);
                all_terminated = false;
            }
        }

        // elif/else children
        let mut cursor = node.walk();
        let mut has_else = false;
        let alternatives: Vec<_> = node.children(&mut cursor)
            .filter(|c| {
                (!elif_kind.is_empty() && c.kind() == elif_kind.as_str())
                    || (!else_kind.is_empty() && c.kind() == else_kind.as_str())
            })
            .collect();

        let mut prev_false_from = current;
        let mut prev_cond_range = cond_range;

        for alt in &alternatives {
            if !elif_kind.is_empty() && alt.kind() == elif_kind.as_str() {
                let elif_cond = alt.child_by_field_name(cond_field);
                let elif_cond_range = elif_cond.map(|c| (c.start_byte(), c.end_byte()));

                let elif_test_block = self.new_block_from_node(*alt);
                self.add_edge_cond(prev_false_from, elif_test_block, EdgeKind::FalseBranch, prev_cond_range);

                if let Some(cond) = elif_cond {
                    self.collect_uses_from_expr(elif_test_block, cond);
                }

                if let Some(body) = alt.child_by_field_name(cons_field) {
                    let body_block = self.new_block_from_node(body);
                    self.add_edge_cond(elif_test_block, body_block, EdgeKind::TrueBranch, elif_cond_range);
                    if let Some(end) = self.walk_body(body, body_block) {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        all_terminated = false;
                    }
                }

                prev_false_from = elif_test_block;
                prev_cond_range = elif_cond_range;
            } else if !else_kind.is_empty() && alt.kind() == else_kind.as_str() {
                has_else = true;
                if let Some(body) = alt.child_by_field_name(body_field) {
                    let else_block = self.new_block_from_node(body);
                    self.add_edge_cond(prev_false_from, else_block, EdgeKind::FalseBranch, prev_cond_range);
                    if let Some(end) = self.walk_body(body, else_block) {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        all_terminated = false;
                    }
                }
            }
        }

        if !has_else {
            self.add_edge_cond(prev_false_from, join, EdgeKind::FalseBranch, prev_cond_range);
            all_terminated = false;
        }

        if all_terminated { None } else { Some(join) }
    }

    /// TS/Rust-style if: else is a nested if in the "alternative" field.
    fn walk_if_alternative(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        let cond_field = &self.cfg_sec.condition_field;
        let cons_field = &self.cfg_sec.consequence_field;
        let alt_field = &self.cfg_sec.alternative_field;
        let else_wrapper = &self.cfg_sec.else_wrapper;

        let condition = node.child_by_field_name(cond_field);
        let cond_range = condition.map(|c| (c.start_byte(), c.end_byte()));

        if let Some(cond) = condition {
            self.collect_uses_from_expr(current, cond);
        }

        // True branch (consequence)
        if let Some(body) = node.child_by_field_name(cons_field) {
            let true_block = self.new_block_from_node(body);
            self.add_edge_cond(current, true_block, EdgeKind::TrueBranch, cond_range);
            if let Some(end) = self.walk_body(body, true_block) {
                self.add_edge(end, join, EdgeKind::Fallthrough);
                all_terminated = false;
            }
        }

        // Alternative branch
        if let Some(alt) = node.child_by_field_name(alt_field) {
            // Unwrap else_wrapper if configured (Rust: else_clause wraps the body)
            let inner = if !else_wrapper.is_empty() && alt.kind() == else_wrapper.as_str() {
                unwrap_first_named_child(alt)
            } else {
                Some(alt)
            };

            if let Some(inner_node) = inner {
                if self.cfg_sec.if_nodes.iter().any(|n| n == inner_node.kind()) {
                    // else-if chain: recurse
                    let elif_block = self.new_block_from_node(inner_node);
                    self.add_edge_cond(current, elif_block, EdgeKind::FalseBranch, cond_range);
                    if self.walk_if_alternative_chain(inner_node, elif_block, join) {
                        all_terminated = false;
                    }
                } else {
                    // else block
                    let else_block = self.new_block_from_node(inner_node);
                    self.add_edge_cond(current, else_block, EdgeKind::FalseBranch, cond_range);
                    if let Some(end) = self.walk_body(inner_node, else_block) {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        all_terminated = false;
                    }
                }
            }
        } else {
            // No alternative — false branch goes to join
            self.add_edge_cond(current, join, EdgeKind::FalseBranch, cond_range);
            all_terminated = false;
        }

        if all_terminated { None } else { Some(join) }
    }

    /// Helper for alternative-style else-if chains. Returns true if any branch is non-terminating.
    fn walk_if_alternative_chain(
        &mut self,
        node: tree_sitter::Node,
        current: BlockId,
        join: BlockId,
    ) -> bool {
        let cond_field = &self.cfg_sec.condition_field;
        let cons_field = &self.cfg_sec.consequence_field;
        let alt_field = &self.cfg_sec.alternative_field;
        let else_wrapper = &self.cfg_sec.else_wrapper;

        let mut any_non_term = false;

        let condition = node.child_by_field_name(cond_field);
        let cond_range = condition.map(|c| (c.start_byte(), c.end_byte()));

        if let Some(cond) = condition {
            self.collect_uses_from_expr(current, cond);
        }

        if let Some(body) = node.child_by_field_name(cons_field) {
            let true_block = self.new_block_from_node(body);
            self.add_edge_cond(current, true_block, EdgeKind::TrueBranch, cond_range);
            if let Some(end) = self.walk_body(body, true_block) {
                self.add_edge(end, join, EdgeKind::Fallthrough);
                any_non_term = true;
            }
        }

        if let Some(alt) = node.child_by_field_name(alt_field) {
            let inner = if !else_wrapper.is_empty() && alt.kind() == else_wrapper.as_str() {
                unwrap_first_named_child(alt)
            } else {
                Some(alt)
            };

            if let Some(inner_node) = inner {
                if self.cfg_sec.if_nodes.iter().any(|n| n == inner_node.kind()) {
                    let elif_block = self.new_block_from_node(inner_node);
                    self.add_edge_cond(current, elif_block, EdgeKind::FalseBranch, cond_range);
                    if self.walk_if_alternative_chain(inner_node, elif_block, join) {
                        any_non_term = true;
                    }
                } else {
                    let else_block = self.new_block_from_node(inner_node);
                    self.add_edge_cond(current, else_block, EdgeKind::FalseBranch, cond_range);
                    if let Some(end) = self.walk_body(inner_node, else_block) {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        any_non_term = true;
                    }
                }
            }
        } else {
            self.add_edge_cond(current, join, EdgeKind::FalseBranch, cond_range);
            any_non_term = true;
        }

        any_non_term
    }

    fn walk_for(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let var_field = &self.cfg_sec.for_variable_field;
        let iter_field = &self.cfg_sec.for_iterable_field;
        let body_field = &self.cfg_sec.body_field;
        let else_kind = &self.cfg_sec.else_clause;
        let has_else = self.cfg_sec.for_has_else;

        let header = self.new_block_from_node(node);
        self.add_edge(current, header, EdgeKind::Fallthrough);

        if let Some(left) = node.child_by_field_name(var_field) {
            self.collect_defs_from_target(header, left, "write");
        }
        if let Some(right) = node.child_by_field_name(iter_field) {
            self.collect_uses_from_expr(header, right);
        }

        let exit = self.new_block_from_node(node);

        if let Some(body) = node.child_by_field_name(body_field) {
            let body_block = self.new_block_from_node(body);
            self.add_edge(header, body_block, EdgeKind::TrueBranch);

            self.loop_stack.push((header, exit));
            let body_end = self.walk_body(body, body_block);
            self.loop_stack.pop();

            if let Some(end) = body_end {
                self.add_edge(end, header, EdgeKind::BackEdge);
            }
        }

        // Else clause (Python only: executes when loop completes without break)
        if has_else && !else_kind.is_empty() {
            let mut cursor = node.walk();
            let else_clause = node.children(&mut cursor)
                .find(|c| c.kind() == else_kind.as_str());

            if let Some(else_node) = else_clause {
                if let Some(body) = else_node.child_by_field_name(body_field) {
                    let else_block = self.new_block_from_node(body);
                    self.add_edge(header, else_block, EdgeKind::FalseBranch);
                    if let Some(end) = self.walk_body(body, else_block) {
                        self.add_edge(end, exit, EdgeKind::Fallthrough);
                    }
                }
            } else {
                self.add_edge(header, exit, EdgeKind::FalseBranch);
            }
        } else {
            self.add_edge(header, exit, EdgeKind::FalseBranch);
        }

        Some(exit)
    }

    /// C-style for (TS `for (init; cond; update) { body }`).
    fn walk_c_style_for(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let body_field = &self.cfg_sec.body_field;
        let cond_field = &self.cfg_sec.condition_field;

        // Initializer: collect defs/uses
        if let Some(init) = node.child_by_field_name("initializer") {
            self.collect_defs_uses(current, init);
        }

        let header = self.new_block_from_node(node);
        self.add_edge(current, header, EdgeKind::Fallthrough);

        let condition = node.child_by_field_name(cond_field);
        let cond_range = condition.map(|c| (c.start_byte(), c.end_byte()));
        if let Some(cond) = condition {
            self.collect_uses_from_expr(header, cond);
        }

        let exit = self.new_block_from_node(node);

        if let Some(body) = node.child_by_field_name(body_field) {
            let body_block = self.new_block_from_node(body);
            self.add_edge_cond(header, body_block, EdgeKind::TrueBranch, cond_range);

            self.loop_stack.push((header, exit));
            let body_end = self.walk_body(body, body_block);
            self.loop_stack.pop();

            if let Some(end) = body_end {
                // Update expression
                if let Some(update) = node.child_by_field_name("increment") {
                    self.collect_defs_uses(end, update);
                }
                self.add_edge(end, header, EdgeKind::BackEdge);
            }
        }

        self.add_edge_cond(header, exit, EdgeKind::FalseBranch, cond_range);
        Some(exit)
    }

    fn walk_while(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let cond_field = &self.cfg_sec.condition_field;
        let body_field = &self.cfg_sec.body_field;
        let else_kind = &self.cfg_sec.else_clause;
        let has_else = self.cfg_sec.while_has_else;

        let header = self.new_block_from_node(node);
        self.add_edge(current, header, EdgeKind::Fallthrough);

        let condition = node.child_by_field_name(cond_field);
        let cond_range = condition.map(|c| (c.start_byte(), c.end_byte()));

        if let Some(cond) = condition {
            self.collect_uses_from_expr(header, cond);
        }

        let exit = self.new_block_from_node(node);

        if let Some(body) = node.child_by_field_name(body_field) {
            let body_block = self.new_block_from_node(body);
            self.add_edge_cond(header, body_block, EdgeKind::TrueBranch, cond_range);

            self.loop_stack.push((header, exit));
            let body_end = self.walk_body(body, body_block);
            self.loop_stack.pop();

            if let Some(end) = body_end {
                self.add_edge(end, header, EdgeKind::BackEdge);
            }
        }

        if has_else && !else_kind.is_empty() {
            let mut cursor = node.walk();
            let else_clause = node.children(&mut cursor)
                .find(|c| c.kind() == else_kind.as_str());

            if let Some(else_node) = else_clause {
                if let Some(body) = else_node.child_by_field_name(body_field) {
                    let else_block = self.new_block_from_node(body);
                    self.add_edge_cond(header, else_block, EdgeKind::FalseBranch, cond_range);
                    if let Some(end) = self.walk_body(body, else_block) {
                        self.add_edge(end, exit, EdgeKind::Fallthrough);
                    }
                }
            } else {
                self.add_edge_cond(header, exit, EdgeKind::FalseBranch, cond_range);
            }
        } else {
            self.add_edge_cond(header, exit, EdgeKind::FalseBranch, cond_range);
        }

        Some(exit)
    }

    /// Rust `loop { ... }` — infinite loop, only exits via break.
    fn walk_infinite_loop(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let body_field = &self.cfg_sec.body_field;

        let header = self.new_block_from_node(node);
        self.add_edge(current, header, EdgeKind::Fallthrough);

        let exit = self.new_block_from_node(node);

        if let Some(body) = node.child_by_field_name(body_field) {
            let body_block = self.new_block_from_node(body);
            self.add_edge(header, body_block, EdgeKind::Fallthrough);

            self.loop_stack.push((header, exit));
            let body_end = self.walk_body(body, body_block);
            self.loop_stack.pop();

            if let Some(end) = body_end {
                self.add_edge(end, header, EdgeKind::BackEdge);
            }
        }

        // No false branch — loop only exits via break (which jumps to exit)
        Some(exit)
    }

    fn walk_try(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        if self.cfg_sec.try_style == "fields" {
            self.walk_try_fields(node, current)
        } else {
            self.walk_try_children(node, current)
        }
    }

    /// Python-style try: except_clause / else_clause / finally_clause as children.
    fn walk_try_children(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        let try_body_kind = &self.cfg_sec.try_body_node;
        let except_kinds = &self.cfg_sec.except_clauses;
        let else_kind = &self.cfg_sec.try_else_clause;
        let finally_kind = &self.cfg_sec.try_finally_clause;
        let id_node = &self.cfg_sec.identifier_node;
        let body_field = &self.cfg_sec.body_field;

        let mut cursor = node.walk();
        let children: Vec<_> = node.children(&mut cursor).collect();

        let mut try_body = None;
        let mut except_clauses = Vec::new();
        let mut else_clause = None;
        let mut finally_clause = None;

        for child in &children {
            let ck = child.kind();
            if !try_body_kind.is_empty() && ck == try_body_kind.as_str() && try_body.is_none() {
                try_body = Some(*child);
            } else if except_kinds.iter().any(|k| k == ck) {
                except_clauses.push(*child);
            } else if !else_kind.is_empty() && ck == else_kind.as_str() {
                else_clause = Some(*child);
            } else if !finally_kind.is_empty() && ck == finally_kind.as_str() {
                finally_clause = Some(*child);
            }
        }

        let mut try_end = None;
        let mut try_entry = None;
        if let Some(body) = try_body {
            let try_block = self.new_block_from_node(body);
            try_entry = Some(try_block);
            self.add_edge(current, try_block, EdgeKind::Fallthrough);
            try_end = self.walk_body(body, try_block);
        }

        let exhaustive_catch = except_clauses.iter().any(|clause| {
            clause.child_by_field_name(&self.cfg_sec.exception_type_field)
                .map(|value| self.cfg_sec.exhaustive_exception_types
                     .iter().any(|name| name == self.node_text(value)))
                .unwrap_or(true)
        });

        let mut handler_ends = Vec::new();
        let except_target = if !except_clauses.is_empty() {
            let first_except = self.new_block_from_node(except_clauses[0]);

            if let Some(te) = try_entry {
                self.add_exception_edge(te, first_except, try_body.unwrap(), exhaustive_catch);
            }

            let mut prev_dispatch: Option<BlockId> = None;
            for (i, except) in except_clauses.iter().enumerate() {
                let dispatch = if i == 0 { first_except }
                    else { self.new_block_from_node(*except) };
                if let Some(previous) = prev_dispatch {
                    self.add_edge(previous, dispatch, EdgeKind::FalseBranch);
                }
                prev_dispatch = Some(dispatch);
                let mut ec = except.walk();
                for c in except.children(&mut ec) {
                    if !id_node.is_empty() && c.kind() == id_node.as_str() {
                        let name = self.node_text(c).to_string();
                        self.block_mut(dispatch).defs.push((
                            name,
                            c.start_position().row as u32,
                            c.start_position().column as u32,
                            "write".to_string(),
                        ));
                    }
                    if !try_body_kind.is_empty() && c.kind() == try_body_kind.as_str() {
                        // A mismatch skips the body and its writes. Keep header
                        // expressions in dispatch, separate from body occurrences.
                        self.block_mut(dispatch).end_byte = c.start_byte();
                        let body = self.new_block_from_node(c);
                        self.add_edge(dispatch, body, EdgeKind::TrueBranch);
                        if let Some(end) = self.walk_body(c, body) {
                            handler_ends.push(end);
                            if finally_clause.is_none() {
                                self.add_edge(end, join, EdgeKind::Fallthrough);
                            }
                            all_terminated = false;
                        }
                    }
                }
            }

            Some(first_except)
        } else {
            None
        };

        if let Some(else_node) = else_clause {
            if let Some(body) = else_node.child_by_field_name(body_field) {
                let else_block = self.new_block_from_node(body);
                if let Some(te) = try_end {
                    self.add_edge(te, else_block, EdgeKind::Fallthrough);
                }
                match self.walk_body(body, else_block) {
                    Some(end) => {
                        if finally_clause.is_none() {
                            self.add_edge(end, join, EdgeKind::Fallthrough);
                        }
                        all_terminated = false;
                        try_end = Some(end);
                    }
                    None => { try_end = None; }
                }
            }
        } else if let Some(te) = try_end {
            if finally_clause.is_none() {
                self.add_edge(te, join, EdgeKind::Fallthrough);
            }
            all_terminated = false;
        }

        if let Some(fin_node) = finally_clause {
            let mut fc = fin_node.walk();
            for c in fin_node.children(&mut fc) {
                if !try_body_kind.is_empty() && c.kind() == try_body_kind.as_str() {
                    let fin_block = self.new_block_from_node(c);
                    if let Some(te) = try_end {
                        self.add_edge(te, fin_block, EdgeKind::Finally);
                        self.tag_finally_edge(fin_block, 0, true);
                    }
                    for &end in &handler_ends {
                        self.add_edge(end, fin_block, EdgeKind::Finally);
                        self.tag_finally_edge(fin_block, 0, true);
                    }
                    let continuations = self.route_finally_jumps(node, fin_block);
                    let exception_start = self.edges.len();
                    if let Some(protected) = except_target.or(try_entry) {
                        for region in except_clauses.iter().copied().chain(else_clause).chain(
                            try_body.into_iter().filter(|_| !exhaustive_catch)
                        ) {
                            self.add_exception_edge(protected, fin_block, region, true);
                            self.tag_finally_edge(fin_block, 1, true);
                        }
                    }
                    let exception_end = self.edges.len();
                    if let Some(end) = self.walk_body(c, fin_block) {
                        self.resume_finally_jumps(end, fin_block, continuations);
                        for edge in &mut self.edges[exception_start..exception_end] {
                            edge.resume_from = Some(end);
                        }
                        all_terminated = try_end.is_none() && handler_ends.is_empty();
                        if !all_terminated {
                            self.add_edge(end, join, EdgeKind::Fallthrough);
                            self.tag_finally_edge(fin_block, 0, false);
                        }
                    } else {
                        all_terminated = true;
                    }
                }
            }
        }

        if all_terminated { None } else { Some(join) }
    }

    /// TS-style try: handler/finalizer as named fields.
    fn walk_try_fields(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        let catch_field = &self.cfg_sec.catch_field;
        let finalizer_field = &self.cfg_sec.finalizer_field;
        let body_field = &self.cfg_sec.body_field;

        // Try body
        let mut try_end = None;
        let mut try_entry = None;
        let mut handler_entry = None;
        let mut handler_end = None;
        if let Some(body) = node.child_by_field_name(body_field) {
            let try_block = self.new_block_from_node(body);
            try_entry = Some(try_block);
            self.add_edge(current, try_block, EdgeKind::Fallthrough);
            try_end = self.walk_body(body, try_block);

            // Exception edge from try entry
            if !catch_field.is_empty() {
                if let Some(handler) = node.child_by_field_name(catch_field) {
                    let handler_block = self.new_block_from_node(handler);
                    handler_entry = Some(handler_block);
                    self.add_exception_edge(try_block, handler_block, body, true);

                    // Walk handler body
                    if let Some(hbody) = handler.child_by_field_name(body_field) {
                        if let Some(end) = self.walk_body(hbody, handler_block) {
                            handler_end = Some(end);
                            if finalizer_field.is_empty()
                                || node.child_by_field_name(finalizer_field).is_none()
                            {
                                self.add_edge(end, join, EdgeKind::Fallthrough);
                            }
                            all_terminated = false;
                        }
                    }
                }
            }
        }

        // Connect try end
        let has_finalizer = !finalizer_field.is_empty()
            && node.child_by_field_name(finalizer_field).is_some();

        if let Some(te) = try_end {
            if !has_finalizer {
                self.add_edge(te, join, EdgeKind::Fallthrough);
            }
            all_terminated = false;
        }

        // Finalizer
        if !finalizer_field.is_empty() {
            if let Some(fin) = node.child_by_field_name(finalizer_field) {
                let fin_block = self.new_block_from_node(fin);
                if let Some(te) = try_end {
                    self.add_edge(te, fin_block, EdgeKind::Finally);
                        self.tag_finally_edge(fin_block, 0, true);
                }
                if let Some(end) = handler_end {
                    self.add_edge(end, fin_block, EdgeKind::Finally);
                        self.tag_finally_edge(fin_block, 0, true);
                }
                let continuations = self.route_finally_jumps(node, fin_block);
                let exception_start = self.edges.len();
                if let Some(protected) = handler_entry.or(try_entry) {
                    let region = node.child_by_field_name(catch_field)
                        .or_else(|| node.child_by_field_name(body_field)).unwrap();
                    self.add_exception_edge(protected, fin_block, region, true);
                            self.tag_finally_edge(fin_block, 1, true);
                }
                let exception_end = self.edges.len();
                let body = fin.child_by_field_name(body_field).unwrap_or(fin);
                if let Some(end) = self.walk_body(body, fin_block) {
                    self.resume_finally_jumps(end, fin_block, continuations);
                    for edge in &mut self.edges[exception_start..exception_end] {
                        edge.resume_from = Some(end);
                    }
                    all_terminated = try_end.is_none() && handler_end.is_none();
                    if !all_terminated {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        self.tag_finally_edge(fin_block, 0, false);
                    }
                } else {
                    all_terminated = true;
                }
            }
        }

        if all_terminated { None } else { Some(join) }
    }

    fn walk_with(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let body_field = &self.cfg_sec.body_field;

        // Collect context manager uses and "as" defs
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "with_clause" || child.kind() == "with_item" {
                let mut ic = child.walk();
                for item in child.children(&mut ic) {
                    if item.kind() == "with_item" {
                        if let Some(val) = item.child_by_field_name("value") {
                            self.collect_uses_from_expr(current, val);
                        }
                        if let Some(alias) = item.child_by_field_name("alias") {
                            self.collect_defs_from_target(current, alias, "write");
                        }
                    } else if item.kind() == "as_pattern" {
                        if let Some(val) = item.child(0) {
                            self.collect_uses_from_expr(current, val);
                        }
                        if let Some(alias) = item.child(2) {
                            self.collect_defs_from_target(current, alias, "write");
                        }
                    }
                }
            }
        }

        if let Some(body) = node.child_by_field_name(body_field) {
            self.walk_body(body, current)
        } else {
            Some(current)
        }
    }

    fn walk_match(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let subject_field = &self.cfg_sec.match_subject_field;
        let body_field_name = &self.cfg_sec.match_body_field;
        let case_kind = &self.cfg_sec.case_clause;
        let default_kind = &self.cfg_sec.case_default;
        let case_body_node = &self.cfg_sec.case_body_node;
        let case_body_field = &self.cfg_sec.case_body_field;
        let fallthrough = self.cfg_sec.switch_fallthrough;

        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        // Subject expression
        if let Some(subject) = node.child_by_field_name(subject_field) {
            self.collect_uses_from_expr(current, subject);
        }

        // Collect case clauses from the match body (or direct children)
        let case_parent = if !body_field_name.is_empty() {
            node.child_by_field_name(body_field_name)
        } else {
            None
        };
        let parent = case_parent.unwrap_or(node);

        let mut cursor = parent.walk();
        let cases: Vec<_> = parent.children(&mut cursor)
            .filter(|c| {
                (!case_kind.is_empty() && c.kind() == case_kind.as_str())
                    || (!default_kind.is_empty() && c.kind() == default_kind.as_str())
            })
            .collect();

        let mut prev_case_end: Option<BlockId> = None;

        for case in &cases {
            let case_block = self.new_block_from_node(*case);
            self.add_edge(current, case_block, EdgeKind::Fallthrough);

            // Switch fallthrough: previous case falls into this one
            if fallthrough {
                if let Some(prev_end) = prev_case_end {
                    self.add_edge(prev_end, case_block, EdgeKind::Fallthrough);
                }
            }

            // Find the case body via field or by scanning children
            let body_node = if !case_body_field.is_empty() {
                case.child_by_field_name(case_body_field)
            } else {
                None
            };

            if let Some(bn) = body_node {
                // Rust match arms: body is a single expression via field
                match self.walk_body(bn, case_block) {
                    Some(end) => {
                        if !fallthrough {
                            self.add_edge(end, join, EdgeKind::Fallthrough);
                        }
                        all_terminated = false;
                        prev_case_end = Some(end);
                    }
                    None => { prev_case_end = None; }
                }
            } else {
                // Scan children for body block (Python case_clause) or
                // walk all statement children (TS switch_case)
                let mut found_body = false;
                let mut cc = case.walk();
                let case_children: Vec<_> = case.children(&mut cc)
                    .filter(|c| c.is_named())
                    .collect();

                if !case_body_node.is_empty() {
                    // Python: find child of type case_body_node
                    for case_child in &case_children {
                        if case_child.kind() == case_body_node.as_str() {
                            found_body = true;
                            match self.walk_body(*case_child, case_block) {
                                Some(end) => {
                                    self.add_edge(end, join, EdgeKind::Fallthrough);
                                    all_terminated = false;
                                    prev_case_end = Some(end);
                                }
                                None => { prev_case_end = None; }
                            }
                        }
                    }
                }

                if !found_body {
                    // TS switch_case: statements are direct children (skip the
                    // case value which is typically the first child)
                    let stmts: Vec<_> = case_children.iter()
                        .filter(|c| {
                            // Skip the case value/pattern nodes
                            let k = c.kind();
                            k != "string" && k != "number" && k != "identifier"
                                && k != "case_pattern" && k != "match_pattern"
                                && k != ":" && k != "case"
                        })
                        .copied()
                        .collect();

                    let mut case_current = case_block;
                    let mut terminated = false;
                    for stmt in stmts {
                        if terminated {
                            let unreachable = self.new_block_from_node(stmt);
                            case_current = unreachable;
                            terminated = false;
                        }
                        match self.walk_statement(stmt, case_current) {
                            Some(next) => case_current = next,
                            None => { terminated = true; }
                        }
                    }

                    if terminated {
                        prev_case_end = None;
                    } else {
                        if !fallthrough {
                            self.add_edge(case_current, join, EdgeKind::Fallthrough);
                        }
                        all_terminated = false;
                        prev_case_end = Some(case_current);
                    }
                }
            }
        }

        // Connect last case to join in fallthrough mode
        if fallthrough {
            if let Some(prev_end) = prev_case_end {
                self.add_edge(prev_end, join, EdgeKind::Fallthrough);
                all_terminated = false;
            }
        }

        if all_terminated { None } else { Some(join) }
    }
}

/// Get the first named child of a node without borrowing issues.
/// Tree-sitter's `children()` requires a mutable cursor borrow that conflicts
/// with the returned `Node` lifetime; `named_child(0)` avoids this.
fn unwrap_first_named_child(node: tree_sitter::Node) -> Option<tree_sitter::Node> {
    node.named_child(0)
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Build a CFG for a single function definition node.
pub fn build_cfg(node: tree_sitter::Node, source: &[u8], cfg_sec: &CfgSection) -> FunctionCfg {
    let body_field = &cfg_sec.body_field;

    let func_name = node
        .child_by_field_name("name")
        .map(|n| n.utf8_text(source).unwrap_or("").to_string())
        .unwrap_or_else(|| "<anonymous>".to_string());

    let func_start_line = node.start_position().row as u32;
    let func_end_line = node.end_position().row as u32;

    let mut builder = CfgBuilder::new(source, cfg_sec);
    let entry = BlockId(0);

    // Find the function body
    if let Some(body) = node.child_by_field_name(body_field) {
        builder.update_block_range(entry, body);
        let body_end = builder.walk_body(body, entry);
        if let Some(end) = body_end {
            builder.add_edge(end, builder.exit_block, EdgeKind::Fallthrough);
        }
    } else {
        builder.add_edge(entry, builder.exit_block, EdgeKind::Fallthrough);
    }

    FunctionCfg {
        func_name,
        func_start_line,
        func_end_line,
        entry,
        exit: builder.exit_block,
        blocks: builder.blocks,
        edges: builder.edges,
    }
}

/// Build CFGs for all top-level and nested functions in a source file.
pub fn build_cfgs_for_source(source: &str, ext: &str) -> Vec<FunctionCfg> {
    let tree = match crate::pattern::parse_by_extension(source, ext) {
        Some(t) => t,
        None => return Vec::new(),
    };

    build_cfgs_from_tree(source, &tree, config_for_ext(ext))
}

/// Build all function CFGs from a caller-owned parse tree and configuration.
pub fn build_cfgs_from_tree(
    source: &str,
    tree: &tree_sitter::Tree,
    config: &LanguageConfig,
) -> Vec<FunctionCfg> {
    let cfg_sec = &config.cfg;

    // If cfg section has no function_nodes configured, CFG is not supported
    if cfg_sec.function_nodes.is_empty() {
        return Vec::new();
    }

    let mut scopes = Vec::new();
    flow_scopes(tree.root_node(), cfg_sec, &mut scopes);
    scopes.into_iter().map(|node| build_cfg(node, source.as_bytes(), cfg_sec)).collect()
}

// ---------------------------------------------------------------------------
// Exact occurrence/value graph
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct CallRecord {
    result: u32,
    callee: String,
    args: Vec<u32>,
}

fn flow_scopes<'a>(node: tree_sitter::Node<'a>, cfg: &CfgSection, out: &mut Vec<tree_sitter::Node<'a>>) {
    if cfg.function_nodes.iter().any(|kind| kind == node.kind()) {
        out.push(node);
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        flow_scopes(child, cfg, out);
    }
}

fn module_cfg(root: tree_sitter::Node, source: &[u8], cfg: &CfgSection) -> FunctionCfg {
    let mut builder = CfgBuilder::new(source, cfg);
    let entry = BlockId(0);
    builder.update_block_range(entry, root);
    if let Some(end) = builder.walk_body(root, entry) {
        builder.add_edge(end, builder.exit_block, EdgeKind::Fallthrough);
    }
    FunctionCfg {
        func_name: "<module>".into(),
        func_start_line: 0,
        func_end_line: root.end_position().row as u32,
        entry,
        exit: builder.exit_block,
        blocks: builder.blocks,
        edges: builder.edges,
    }
}

struct FlowExtractor<'a> {
    source: &'a [u8],
    lang: &'a LanguageConfig,
    cfg: &'a FunctionCfg,
    func_id: String,
    func_name: String,
    func_start: usize,
    next_id: &'a mut u32,
    ordinal: u32,
    events: Vec<FlowEvent>,
    edges: Vec<FlowEdge>,
    calls: Vec<CallRecord>,
}

impl<'a> FlowExtractor<'a> {
    fn text(&self, node: tree_sitter::Node) -> &'a str {
        node.utf8_text(self.source).unwrap_or("")
    }

    fn children(node: tree_sitter::Node) -> Vec<tree_sitter::Node> {
        let mut cursor = node.walk();
        node.children(&mut cursor).filter(|child| child.is_named()).collect()
    }

    fn is_function(&self, node: tree_sitter::Node) -> bool {
        self.lang.cfg.function_nodes.iter().any(|kind| kind == node.kind())
    }

    fn path(&self, node: tree_sitter::Node) -> Option<String> {
        let pm = &self.lang.pattern_matching;
        if node.kind() == pm.identifier || pm.extra_identifiers.iter().any(|k| k == node.kind()) {
            let name = self.text(node);
            return (!self.lang.cfg.skip_identifiers.iter().any(|s| s == name)).then(|| name.into());
        }
        if !pm.attribute.is_empty() && node.kind() == pm.attribute {
            let object = node.child_by_field_name(&pm.object_field)?;
            let attr = node.child_by_field_name(&pm.attr_field)?;
            return Some(format!("{}.{}", self.path(object)?, self.text(attr)));
        }
        if !pm.subscript.is_empty() && node.kind() == pm.subscript {
            let object = node.child_by_field_name(&pm.value_field)
                .or_else(|| node.child_by_field_name(&pm.object_field))?;
            let index = node.child_by_field_name("subscript")
                .or_else(|| node.child_by_field_name("index"))?;
            return Some(format!("{}[{}]", self.path(object)?, self.text(index)));
        }
        None
    }

    fn block(&self, node: tree_sitter::Node) -> u32 {
        let (start, end) = (node.start_byte(), node.end_byte());
        let statement = self.cfg.blocks.iter().flat_map(|block| {
            block.statements.iter().map(move |&(s, e)| (e.saturating_sub(s), block.id.0, s, e))
        }).filter(|(_, _, s, e)| *s <= start && end <= *e).min_by_key(|row| row.0);
        if let Some((_, id, _, _)) = statement { return id; }
        if let Some(edge) = self.cfg.edges.iter().find(|edge| {
            edge.condition.is_some_and(|(s, e)| s <= start && end <= e)
        }) { return edge.from.0; }
        self.cfg.blocks.iter()
            .filter(|block| block.start_byte <= start && end <= block.end_byte)
            .min_by_key(|block| block.end_byte.saturating_sub(block.start_byte))
            .map(|block| block.id.0).unwrap_or(self.cfg.entry.0)
    }

    fn emit(&mut self, node: tree_sitter::Node, role: &str, path: Option<String>, call_id: Option<u32>, arg_index: Option<u32>) -> u32 {
        let id = *self.next_id;
        *self.next_id += 1;
        let start = node.start_position();
        let end = node.end_position();
        let var = path.as_deref().map(|p| p.split(['.', '[']).next().unwrap_or(p).to_string());
        self.events.push(FlowEvent {
            id,
            func_id: self.func_id.clone(),
            func_name: self.func_name.clone(),
            func_start: self.func_start,
            role: role.into(),
            var,
            access_path: path,
            block: self.block(node),
            start_byte: node.start_byte(),
            end_byte: node.end_byte(),
            start_line: start.row as u32,
            start_col: start.column as u32,
            end_line: end.row as u32,
            end_col: end.column as u32,
            ordinal: self.ordinal,
            call_id,
            arg_index,
            arg_name: None,
            text: self.text(node).into(),
        });
        self.ordinal += 1;
        id
    }

    fn edge(&mut self, from: u32, to: u32, kind: &str) {
        if from != to { self.edges.push(FlowEdge { from, to, kind: kind.into() }); }
    }

    fn parameter_name<'b>(&self, node: tree_sitter::Node<'b>) -> Option<tree_sitter::Node<'b>> {
        if node.kind() == self.lang.cfg.identifier_node { return Some(node); }
        let field = &self.lang.bindings.parameters.name_field;
        if !field.is_empty() {
            if let Some(found) = node.child_by_field_name(field).and_then(|n| self.parameter_name(n)) {
                return Some(found);
            }
        }
        Self::children(node).into_iter().find_map(|child| self.parameter_name(child))
    }

    fn parameters(&mut self, function: tree_sitter::Node) {
        let field = self.lang.symbols.parameters_field();
        let params = function.child_by_field_name(field).or_else(|| {
            Self::children(function).into_iter().find(|child| {
                child.kind() == field || matches!(child.kind(), "parameters" | "formal_parameters")
            })
        });
        let Some(params) = params else { return };
        let mut index = 0;
        for param in Self::children(params) {
            if let Some(name) = self.parameter_name(param) {
                if let Some(path) = self.path(name) {
                    self.emit(name, "param_in", Some(path), None, Some(index));
                    self.events.last_mut().unwrap().arg_name = Some(self.text(name).to_string());
                    index += 1;
                }
            }
        }
    }

    fn targets(&mut self, node: tree_sitter::Node, inputs: &[u32], mutation: bool) -> Vec<u32> {
        if let Some(path) = self.path(node) {
            let role = if node.kind() == self.lang.cfg.identifier_node && !mutation { "def" } else { "mutation" };
            let id = self.emit(node, role, Some(path), None, None);
            for &input in inputs { self.edge(input, id, "transfer"); }
            return vec![id];
        }
        Self::children(node).into_iter().flat_map(|child| self.targets(child, inputs, mutation)).collect()
    }

    fn assignment(&mut self, node: tree_sitter::Node) -> Vec<u32> {
        let Some(rule) = self.lang.cfg.def_use_rules.iter().find(|r| r.node == node.kind()) else {
            return self.generic(node);
        };
        let augmented = node.kind() == self.lang.pattern_matching.augmented_assignment
            || node.kind().contains("augmented") || node.kind().contains("compound_assignment");
        let target = node.child_by_field_name(&rule.target);
        let mut inputs = if augmented { target.map(|n| self.expr(n)).unwrap_or_default() } else { Vec::new() };
        if let Some(value) = node.child_by_field_name(&rule.value) { inputs.extend(self.expr(value)); }
        target.map(|n| self.targets(n, &inputs, augmented)).unwrap_or(inputs)
    }

    fn call(&mut self, node: tree_sitter::Node) -> Vec<u32> {
        let pm = &self.lang.pattern_matching;
        let callee_node = node.child_by_field_name(&pm.func_field).or_else(|| node.named_child(0));
        let mut callee_values = Vec::new();
        let callee = callee_node.and_then(|n| { callee_values.extend(self.expr(n)); self.path(n) })
            .unwrap_or_else(|| "<dynamic>".into());
        let mut args = node.child_by_field_name(&pm.args_field).map(Self::children).unwrap_or_default();
        args.sort_by_key(|arg| arg.start_byte());
        let values: Vec<Vec<u32>> = args.iter().map(|arg| self.expr(*arg)).collect();
        let mut arg_events = Vec::new();
        for (index, (arg, inputs)) in args.iter().zip(values).enumerate() {
            let path = self.path(*arg);
            let id = self.emit(*arg, "call_arg", path, None, Some(index as u32));
            self.events.last_mut().unwrap().arg_name = arg.child_by_field_name("name")
                .map(|name| self.text(name).to_string());
            for input in inputs { self.edge(input, id, "transfer"); }
            arg_events.push(id);
        }
        let call = self.emit(node, "call", Some(callee.clone()), None, None);
        let call_event = self.events.last_mut().unwrap();
        call_event.call_id = Some(call);
        // Calls expose the full structural callee path for project-level
        // linking; ordinary value events keep ``var`` as the root binding.
        call_event.var = Some(callee.clone());
        for event in &mut self.events {
            if arg_events.contains(&event.id) { event.call_id = Some(call); }
        }
        // Callee/argument evaluation is sequenced by control edges. It is not
        // an unconditional value transfer through a known function: resolved
        // calls flow through param_in/return_out instead.
        let _ = callee_values;
        let result = self.emit(node, "call_result", None, Some(call), None);
        for &arg in &arg_events { self.edge(arg, result, "call_input"); }
        self.calls.push(CallRecord { result, callee, args: arg_events });
        vec![result]
    }

    fn generic(&mut self, node: tree_sitter::Node) -> Vec<u32> {
        let pm = &self.lang.pattern_matching;
        if !pm.keyword_argument.is_empty() && node.kind() == pm.keyword_argument {
            return node.child_by_field_name(&pm.value_field).map(|n| self.expr(n)).unwrap_or_default();
        }
        Self::children(node).into_iter().flat_map(|child| self.expr(child)).collect()
    }

    fn expr(&mut self, node: tree_sitter::Node) -> Vec<u32> {
        if self.is_function(node) { return Vec::new(); }
        if !self.lang.pattern_matching.call.is_empty() && node.kind() == self.lang.pattern_matching.call {
            return self.call(node);
        }
        if self.lang.cfg.def_use_rules.iter().any(|r| r.node == node.kind()) {
            return self.assignment(node);
        }
        if let Some(path) = self.path(node) {
            return vec![self.emit(node, "use", Some(path), None, None)];
        }
        let pm = &self.lang.pattern_matching;
        // Composite results are not definite aliases of their tainted inputs.
        // Selection expressions may return an untainted alternative; retaining
        // a distinct identity is conservative until their branches are modeled.
        if [&pm.binary_operator, &pm.unary_operator, &pm.boolean_operator,
            &pm.not_operator, &pm.comparison_operator, &pm.conditional_expression,
            &pm.list, &pm.tuple, &pm.set, &pm.dict, &pm.dict_comprehension,
            &pm.string].iter().any(|kind| kind.as_str() == node.kind())
            || self.lang.cfg.string_nodes.iter().any(|kind| kind == node.kind())
            || self.lang.symbols.receiver_constructor_node() == Some(node.kind())
            || self.lang.scoping.scope_creators.iter().any(|creator| {
                creator.kind == crate::scope::ScopeKind::Comprehension && creator.node_type == node.kind()
            }) {
            let inputs = self.generic(node);
            let result = self.emit(node, "evaluate", None, None, None);
            for input in inputs { self.edge(input, result, "transfer"); }
            return vec![result];
        }
        self.generic(node)
    }

    fn walk(&mut self, node: tree_sitter::Node) {
        if self.is_function(node) { return; }
        let catch_binding = self.lang.bindings.exception.iter()
            .find(|rule| rule.node == node.kind())
            .and_then(|rule| node.child_by_field_name(&rule.target));
        if catch_binding.is_some() || self.lang.cfg.except_clauses.iter().any(|kind| kind == node.kind()) {
            let named_type = node.child_by_field_name(&self.lang.cfg.exception_type_field)
                .filter(|value| self.lang.cfg.nonthrowing_exception_types
                        .iter().any(|name| name == self.text(*value)));
            // Known built-in catch types describe dispatch. Custom and dynamic
            // type expressions remain evaluated; catch parameters are definitions.
            for child in Self::children(node) {
                if Some(child) == catch_binding { self.targets(child, &[], false); }
                else if Some(child) != named_type { self.walk(child); }
            }
            return;
        }
        if self.lang.cfg.throw_nodes.iter().any(|kind| kind == node.kind()) {
            for child in Self::children(node) { self.expr(child); }
            self.emit(node, "throw", None, None, None);
            return;
        }
        if self.lang.cfg.return_nodes.iter().any(|kind| kind == node.kind()) {
            let value = node.child_by_field_name(&self.lang.pattern_matching.value_field).or_else(|| node.named_child(0));
            let inputs = value.map(|n| self.expr(n)).unwrap_or_default();
            let out_node = value.unwrap_or(node);
            let path = self.path(out_node);
            let output = self.emit(out_node, "return_out", path, None, None);
            for input in inputs { self.edge(input, output, "transfer"); }
            return;
        }
        if self.lang.cfg.for_nodes.iter().any(|kind| kind == node.kind()) {
            let iter = node.child_by_field_name(&self.lang.cfg.for_iterable_field);
            let target = node.child_by_field_name(&self.lang.cfg.for_variable_field);
            let inputs = iter.map(|n| self.expr(n)).unwrap_or_default();
            if let Some(target) = target { self.targets(target, &inputs, false); }
            for child in Self::children(node) {
                if Some(child) != iter && Some(child) != target { self.walk(child); }
            }
            return;
        }
        if self.lang.cfg.def_use_rules.iter().any(|r| r.node == node.kind())
            || node.kind() == self.lang.pattern_matching.call || self.path(node).is_some() {
            self.expr(node); return;
        }
        for child in Self::children(node) { self.walk(child); }
    }

    fn control_edges(&mut self) {
        let mut by_block: HashMap<u32, Vec<(u32, u32)>> = HashMap::new();
        for event in &self.events { by_block.entry(event.block).or_default().push((event.ordinal, event.id)); }
        let mut additions = Vec::new();
        for values in by_block.values_mut() {
            values.sort_unstable();
            additions.extend(values.windows(2).map(|p| FlowEdge { from: p[0].1, to: p[1].1, kind: "control".into() }));
        }
        type Target = (u32, Vec<FinallyTransition>);
        let mut successors: HashMap<u32, Vec<Target>> = self.cfg.blocks.iter().map(|b| {
            (b.id.0, self.cfg.edges.iter().filter(|edge| edge.from == b.id && edge.kind != EdgeKind::Exception)
             .map(|edge| (edge.to.0, edge.finally_transitions.clone())).collect())
        }).collect();
        let regions: Vec<_> = self.cfg.edges.iter()
            .filter_map(|edge| edge.protected_range.map(|range| (range, edge)))
            .collect();
        let handlers = |start: usize, end: usize, enclosing: bool| {
            let contains = |left: usize, right: usize| left <= start && end <= right
                && (!enclosing || right - left > end - start);
            let nearest = regions.iter().filter(|((left, right), edge)|
                contains(*left, *right) && edge.shadows_outer)
                .map(|((left, right), _)| right - left).min().unwrap_or(usize::MAX);
            regions.iter().filter(|((left, right), _)|
                contains(*left, *right) && right - left <= nearest)
                .map(|(_, edge)| (edge.to.0, edge.finally_transitions.clone())).collect::<Vec<_>>()
        };
        for ((start, end), edge) in &regions {
            if let Some(resume) = edge.resume_from {
                let entered = edge.finally_transitions.last().unwrap();
                for (target, mut actions) in handlers(*start, *end, true) {
                    actions.insert(0, FinallyTransition { entering: false, ..*entered });
                    successors.entry(resume.0).or_default().push((target, actions));
                }
            }
        }
        // The public CFG shares an exit block for return and throw, but a
        // throwing occurrence can only take an exceptional continuation.
        let throws: HashSet<_> = self.events.iter().filter(|event| event.role == "throw")
            .map(|event| event.id).collect();
        let mut exits: Vec<(u32, Vec<Target>)> = by_block.iter().filter_map(|(&block, values)| {
            values.last().filter(|(_, last)| !throws.contains(last))
                .map(|&(_, last)| (last, successors.get(&block).cloned().unwrap_or_default()))
        }).collect();
        for event in &self.events {
            if matches!(event.role.as_str(), "call" | "throw" | "use" | "evaluate") {
                let targets = handlers(event.start_byte, event.end_byte, false);
                if !targets.is_empty() { exits.push((event.id, targets)); }
            }
        }
        // Canonicalize transitions through empty blocks. Each scope is an
        // independent pending reason; a balanced empty finalizer clears it.
        // This bounds empty-loop traversal without dropping distinct reasons.
        let compose = |actions: Vec<FinallyTransition>| -> Option<Vec<FinallyTransition>> {
            let mut scopes = std::collections::BTreeMap::new();
            for action in actions {
                let state = scopes.entry(action.scope).or_insert((None, None, false));
                if action.entering {
                    state.1 = Some(action.reason);
                } else {
                    if action.reason != u32::MAX {
                        if state.2 && state.1.is_some_and(|reason| reason != action.reason) { return None; }
                        if !state.2 { state.0 = Some(action.reason); }
                    }
                    state.1 = None;
                }
                state.2 = true;
            }
            let mut result = Vec::new();
            for (scope, (required, current, _)) in scopes {
                if let Some(reason) = required {
                    result.push(FinallyTransition { scope, reason, entering: false });
                }
                if let Some(reason) = current {
                    result.push(FinallyTransition { scope, reason, entering: true });
                } else if required.is_none() {
                    result.push(FinallyTransition { scope, reason: u32::MAX, entering: false });
                }
            }
            Some(result)
        };
        for (last, targets) in exits {
            let mut queue: VecDeque<Target> = targets.into();
            let mut seen = HashSet::new();
            while let Some((next, actions)) = queue.pop_front() {
                let Some(actions) = compose(actions) else { continue; };
                if !seen.insert((next, actions.clone())) { continue; }
                if let Some(&(_, first)) = by_block.get(&next).and_then(|v| v.first()) {
                    let mut kind = String::from("control");
                    for action in actions {
                        let operation = if action.entering { "enter" }
                            else if action.reason == u32::MAX { "clear" } else { "resume" };
                        kind.push_str(&format!(";{},{},{}", operation, action.scope, action.reason));
                    }
                    additions.push(FlowEdge { from: last, to: first, kind });
                } else if let Some(more) = successors.get(&next) {
                    queue.extend(more.iter().map(|(target, extra)| {
                        let mut combined = actions.clone();
                        combined.extend(extra);
                        (*target, combined)
                    }));
                }
            }
        }
        self.edges.extend(additions);
    }

    fn binding(event: &FlowEvent) -> Option<&str> { event.access_path.as_deref().or(event.var.as_deref()) }
    fn is_def(event: &FlowEvent) -> bool { matches!(event.role.as_str(), "def" | "param_in" | "mutation") }

    fn reaching_edges(&mut self) {
        use std::rc::Rc;
        let indices: HashMap<_, _> = self.events.iter().enumerate().map(|(i, event)| (event.id, i)).collect();
        let mut successors = vec![Vec::new(); self.events.len()];
        let mut predecessors = vec![Vec::new(); self.events.len()];
        for edge in self.edges.iter().filter(|edge| edge.kind == "control" || edge.kind.starts_with("control;")) {
            let (from, to) = (indices[&edge.from], indices[&edge.to]);
            successors[from].push(to);
            predecessors[to].push(from);
        }
        // Collapse straight-line event chains before dataflow. Exceptional
        // exits split chains at the evaluation, while large ordinary blocks
        // still need one state transfer, not one traversal per definition.
        let mut owner = vec![usize::MAX; self.events.len()];
        let mut segments = Vec::new();
        for start in 0..self.events.len() {
            if owner[start] != usize::MAX { continue; }
            let mut segment = Vec::new();
            let mut current = start;
            while owner[current] == usize::MAX {
                owner[current] = segments.len();
                segment.push(current);
                if successors[current].len() != 1 { break; }
                let next = successors[current][0];
                if predecessors[next].len() != 1 { break; }
                current = next;
            }
            segments.push(segment);
        }
        let mut incoming_segments = vec![HashSet::new(); segments.len()];
        for (from, targets) in successors.iter().enumerate() {
            for &to in targets {
                if owner[from] != owner[to] || to == segments[owner[to]][0] {
                    incoming_segments[owner[to]].insert(owner[from]);
                }
            }
        }
        type State = HashMap<String, HashSet<u32>>;
        // Most evaluation exits do not change bindings; share their state
        // instead of copying a large environment at every possible throw.
        let mut incoming = vec![Rc::new(State::new()); segments.len()];
        let mut outgoing = incoming.clone();
        let mut changed = true;
        while changed {
            changed = false;
            for (index, segment) in segments.iter().enumerate() {
                let mut state = incoming_segments[index].iter().next()
                    .map(|&pred| outgoing[pred].clone()).unwrap_or_else(|| Rc::new(State::new()));
                for &pred in &incoming_segments[index] {
                    if !Rc::ptr_eq(&state, &outgoing[pred]) {
                        for (name, defs) in outgoing[pred].iter() {
                            Rc::make_mut(&mut state).entry(name.clone()).or_default().extend(defs);
                        }
                    }
                }
                incoming[index] = state.clone();
                for &i in segment {
                    let event = &self.events[i];
                    if Self::is_def(event) {
                        if let Some(name) = Self::binding(event) { Rc::make_mut(&mut state).insert(name.into(), HashSet::from([event.id])); }
                    }
                }
                if !Rc::ptr_eq(&outgoing[index], &state) && outgoing[index] != state {
                    outgoing[index] = state; changed = true;
                }
            }
        }
        let mut additions = Vec::new();
        for (segment, mut state) in segments.iter().zip(incoming) {
            for &i in segment {
                let event = &self.events[i];
                if matches!(event.role.as_str(), "use" | "mutation") {
                    for name in [Self::binding(event), event.var.as_deref()].into_iter().flatten() {
                        for &def in state.get(name).into_iter().flatten() {
                            additions.push(FlowEdge { from: def, to: event.id, kind: "reaching".into() });
                        }
                    }
                }
                if Self::is_def(event) {
                    if let Some(name) = Self::binding(event) { Rc::make_mut(&mut state).insert(name.into(), HashSet::from([event.id])); }
                }
            }
        }
        self.edges.extend(additions);
    }

    fn emit_completion(&mut self, body: tree_sitter::Node, role: &str, block: BlockId) -> u32 {
        let id = self.emit(body, role, None, None, None);
        let event = self.events.last_mut().unwrap();
        event.block = block.0;
        event.text.clear();
        event.start_byte = body.end_byte();
        event.start_line = body.end_position().row as u32;
        event.start_col = body.end_position().column as u32;
        id
    }

    fn finish(mut self) -> (Vec<FlowEvent>, Vec<FlowEdge>, Vec<CallRecord>) {
        self.control_edges(); self.reaching_edges();
        let mut seen = HashSet::new();
        self.edges.retain(|e| seen.insert((e.from, e.to, e.kind.clone())));
        (self.events, self.edges, self.calls)
    }
}

fn extract_scope(
    source: &[u8], lang: &LanguageConfig, cfg: &FunctionCfg,
    func_name: String, func_start: usize, body: tree_sitter::Node,
    function: Option<tree_sitter::Node>, next_id: &mut u32,
) -> (Vec<FlowEvent>, Vec<FlowEdge>, Vec<CallRecord>) {
    let func_id = format!("{}@{}", func_name, func_start);
    let mut extractor = FlowExtractor {
        source, lang, cfg, func_id, func_name, func_start, next_id,
        ordinal: 0, events: Vec::new(), edges: Vec::new(), calls: Vec::new(),
    };
    if let Some(function) = function { extractor.parameters(function); }
    extractor.walk(body);
    // Each return completes only after its finalizers. A nested return may be
    // canceled by a caught exception, leaving an earlier pending return intact.
    let returns: HashMap<_, _> = extractor.events.iter().filter(|event| event.role == "return_out")
        .map(|event| (event.block, event.id)).collect();
    let mut completed = Vec::new();
    for edge in &cfg.edges {
        if let Some(returned) = edge.return_value_from.and_then(|block| returns.get(&block.0)) {
            let completion = extractor.emit_completion(body, "return_complete", edge.from);
            extractor.edge(*returned, completion, "transfer");
            completed.push(completion);
        }
    }
    let exit = extractor.emit_completion(body, "function_exit", cfg.exit);
    for returned in completed { extractor.edge(returned, exit, "transfer"); }
    extractor.finish()
}

/// Build exact occurrence, value-transfer, reaching-definition, control, and
/// same-buffer call facts. Coordinates are zero-based; byte ranges are exact
/// half-open UTF-8 offsets.
pub fn build_flow_facts(source: &str, ext: &str) -> FlowFacts {
    let Some(tree) = crate::pattern::parse_by_extension(source, ext) else { return FlowFacts::default() };
    let lang = config_for_ext(ext);
    build_flow_facts_from_tree(source, &tree, lang)
}

/// Build flow facts from a caller-owned parse tree and configuration.
pub fn build_flow_facts_from_tree(
    source: &str,
    tree: &tree_sitter::Tree,
    lang: &LanguageConfig,
) -> FlowFacts {
    build_analysis_from_tree(source, tree, lang).1
}

/// Derive control-flow and value-flow facts together, building each CFG once.
pub fn build_analysis_from_tree(
    source: &str,
    tree: &tree_sitter::Tree,
    lang: &LanguageConfig,
) -> (Vec<FunctionCfg>, FlowFacts) {
    if lang.cfg.function_nodes.is_empty() { return (Vec::new(), FlowFacts::default()); }
    let bytes = source.as_bytes();
    let root = tree.root_node();
    let mut events = Vec::new();
    let mut edges = Vec::new();
    let mut calls = Vec::new();
    let mut next_id = 0;
    let cfg = module_cfg(root, bytes, &lang.cfg);
    let (e, d, c) = extract_scope(bytes, lang, &cfg, "<module>".into(), 0, root, None, &mut next_id);
    events.extend(e); edges.extend(d); calls.extend(c);

    let mut scopes = Vec::new();
    flow_scopes(root, &lang.cfg, &mut scopes);
    let cfgs: Vec<_> = scopes.iter().map(|&node| build_cfg(node, bytes, &lang.cfg)).collect();
    let mut functions: HashMap<String, Vec<(Vec<u32>, Vec<u32>)>> = HashMap::new();
    for (node, cfg) in scopes.into_iter().zip(&cfgs) {
        let Some(body) = node.child_by_field_name(&lang.cfg.body_field) else { continue };
        let name = cfg.func_name.clone();
        let start = node.start_byte();
        let (e, d, c) = extract_scope(bytes, lang, cfg, name.clone(), start, body, Some(node), &mut next_id);
        let params = e.iter().filter(|v| v.role == "param_in").map(|v| v.id).collect();
        let returns = e.iter().filter(|v| v.role == "function_exit").map(|v| v.id).collect();
        functions.entry(name).or_default().push((params, returns));
        events.extend(e); edges.extend(d); calls.extend(c);
    }
    for call in calls {
        let name = call.callee.rsplit('.').next().unwrap_or(&call.callee);
        let Some(target) = functions.get(name).filter(|v| v.len() == 1).map(|v| &v[0]) else { continue };
        for (&arg, &param) in call.args.iter().zip(&target.0) {
            edges.push(FlowEdge { from: arg, to: param, kind: "call_arg".into() });
        }
        for &ret in &target.1 {
            edges.push(FlowEdge { from: ret, to: call.result, kind: "call_return".into() });
        }
    }
    let mut seen = HashSet::new();
    edges.retain(|e| seen.insert((e.from, e.to, e.kind.clone())));
    (cfgs, FlowFacts { events, edges })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decorated_functions_retain_nested_control_flow() {
        let source = "@decorate\ndef outer(x):\n    def inner(y):\n        if y:\n            return y\n        return 0\n    return inner(x)\n";
        let cfgs = build_cfgs_for_source(source, "py");
        assert_eq!(cfgs.iter().map(|cfg| cfg.func_name.as_str()).collect::<Vec<_>>(), ["outer", "inner"]);
        let flow = build_flow_facts(source, "py");
        for event in flow.events.iter().filter(|event| event.func_name == "inner") {
            assert!(cfgs[1].blocks.iter().any(|block| block.id.0 == event.block));
        }
    }

    #[test]
    fn test_field_level_defs() {
        let src = "def f():\n    obj.dirty = 1\n    obj.clean = 'safe'\n";
        let cfgs = build_cfgs_for_source(src, "py");
        assert_eq!(cfgs.len(), 1, "Expected 1 CFG");
        let cfg = &cfgs[0];
        let all_defs: Vec<&str> = cfg.blocks.iter()
            .flat_map(|b| b.defs.iter().map(|(name, _, _, _)| name.as_str()))
            .collect();
        assert!(all_defs.contains(&"obj.dirty"), "Expected obj.dirty in defs, got {:?}", all_defs);
        assert!(all_defs.contains(&"obj.clean"), "Expected obj.clean in defs, got {:?}", all_defs);
    }

    #[test]
    fn test_field_level_uses() {
        let src = "def f():\n    x = obj.dirty\n";
        let cfgs = build_cfgs_for_source(src, "py");
        assert_eq!(cfgs.len(), 1);
        let all_uses: Vec<&str> = cfgs[0].blocks.iter()
            .flat_map(|b| b.uses.iter().map(|(name, _, _, _)| name.as_str()))
            .collect();
        assert!(all_uses.contains(&"obj.dirty"), "Expected obj.dirty in uses, got {:?}", all_uses);
        assert!(all_uses.contains(&"obj"), "Expected obj in uses, got {:?}", all_uses);
    }

    #[test]
    fn test_subscript_defs() {
        let src = "def f():\n    data['key'] = 1\n";
        let cfgs = build_cfgs_for_source(src, "py");
        assert_eq!(cfgs.len(), 1);
        let all_defs: Vec<&str> = cfgs[0].blocks.iter()
            .flat_map(|b| b.defs.iter().map(|(name, _, _, _)| name.as_str()))
            .collect();
        assert!(all_defs.iter().any(|d| d.contains("data[") && d.contains("key")),
                "Expected data['key'] in defs, got {:?}", all_defs);
    }

    #[test]
    fn test_subscript_uses() {
        let src = "def f():\n    x = data['key']\n";
        let cfgs = build_cfgs_for_source(src, "py");
        assert_eq!(cfgs.len(), 1);
        let all_uses: Vec<&str> = cfgs[0].blocks.iter()
            .flat_map(|b| b.uses.iter().map(|(name, _, _, _)| name.as_str()))
            .collect();
        assert!(all_uses.iter().any(|u| u.contains("data[") && u.contains("key")),
                "Expected data['key'] in uses, got {:?}", all_uses);
    }

    #[test]
    fn flow_occurrences_preserve_overwrites_and_nested_scopes() {
        let source = "def f(a):\n    x = a; x = 0; y = x\n    def inner(q):\n        return q\n    return y\n";
        let facts = build_flow_facts(source, "py");
        assert!(facts.events.iter().any(|e| e.func_name == "inner" && e.role == "param_in"));
        let defs: Vec<_> = facts.events.iter().filter(|e| {
            e.func_name == "f" && e.role == "def" && e.var.as_deref() == Some("x")
        }).collect();
        let used = facts.events.iter().find(|e| {
            e.func_name == "f" && e.role == "use" && e.var.as_deref() == Some("x")
        }).unwrap();
        let reaching: Vec<_> = facts.edges.iter().filter(|e| {
            e.kind == "reaching" && e.to == used.id
        }).map(|e| e.from).collect();
        assert_eq!(reaching, vec![defs[1].id]);
        assert!(facts.events.iter().all(|e| &source[e.start_byte..e.end_byte] == e.text));
    }

    #[test]
    fn resolved_calls_do_not_implicitly_copy_arguments_to_results() {
        let source = "def safe(value):\n    return 0\ndef f():\n    return safe(source())\n";
        let facts = build_flow_facts(source, "py");
        let safe_call = facts.events.iter().find(|e| {
            e.role == "call" && e.var.as_deref() == Some("safe")
        }).unwrap();
        let result = facts.events.iter().find(|e| {
            e.role == "call_result" && e.call_id == Some(safe_call.id)
        }).unwrap();
        assert!(!facts.edges.iter().any(|e| e.to == result.id && e.kind == "transfer"));
        assert!(facts.edges.iter().any(|e| e.to == result.id && e.kind == "call_input"));
        assert!(facts.edges.iter().any(|e| e.to == result.id && e.kind == "call_return"));
    }

    #[test]
    fn keyword_arguments_retain_parameter_identity() {
        let facts = build_flow_facts("def f(value):\n    return value\nf(value=1)\n", "py");
        assert!(facts.events.iter().any(|e| {
            e.role == "param_in" && e.arg_name.as_deref() == Some("value")
        }));
        assert!(facts.events.iter().any(|e| {
            e.role == "call_arg" && e.arg_name.as_deref() == Some("value")
        }));
    }
}
