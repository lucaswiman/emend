//! Per-function control flow graph construction from tree-sitter ASTs.
//!
//! Builds basic-block CFGs for functions in any supported language.
//! Language-specific tree-sitter node types and field names are driven by
//! the `[cfg]` section of the language config TOML (see [`CfgSection`]).

use std::collections::{HashMap, HashSet};
use crate::scope::{config_for_ext, CfgSection};

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

/// A control-flow edge.
#[derive(Debug, Clone)]
pub struct CfgEdge {
    pub from: BlockId,
    pub to: BlockId,
    pub kind: EdgeKind,
    /// For conditional edges: byte range of the condition expression.
    pub condition: Option<(usize, usize)>,
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
        });
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

        // Attribute/subscript access on LHS → use, not def
        if self.cfg_sec.attribute_access_nodes.iter().any(|n| n == kind) {
            self.collect_uses_from_expr(block_id, node);
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
        // Collect children upfront: tree-sitter cursors can't be shared with
        // recursive walks, and the builder mutates self during iteration.
        let mut cursor = body.walk();
        let children: Vec<_> = body.children(&mut cursor).filter(|c| c.is_named()).collect();
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
            self.add_edge(current, self.exit_block, EdgeKind::Jump);
            return None;
        }
        if self.cfg_sec.break_nodes.iter().any(|n| n == kind) {
            self.add_statement(current, node);
            if let Some(&(_, loop_exit)) = self.loop_stack.last() {
                self.add_edge(current, loop_exit, EdgeKind::Jump);
            }
            return None;
        }
        if self.cfg_sec.continue_nodes.iter().any(|n| n == kind) {
            self.add_statement(current, node);
            if let Some(&(loop_header, _)) = self.loop_stack.last() {
                self.add_edge(current, loop_header, EdgeKind::BackEdge);
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
        if let Some(body) = try_body {
            let try_block = self.new_block_from_node(body);
            self.add_edge(current, try_block, EdgeKind::Fallthrough);
            try_end = self.walk_body(body, try_block);
        }

        let except_target = if !except_clauses.is_empty() {
            let first_except = self.new_block_from_node(except_clauses[0]);

            if let Some(body) = try_body {
                let try_entry = self.blocks.iter()
                    .find(|b| b.start_byte == body.start_byte())
                    .map(|b| b.id);
                if let Some(te) = try_entry {
                    self.add_edge(te, first_except, EdgeKind::Exception);
                }
            }

            for except in &except_clauses {
                let handler_block = self.new_block_from_node(*except);
                let mut ec = except.walk();
                for c in except.children(&mut ec) {
                    if !id_node.is_empty() && c.kind() == id_node.as_str() {
                        let name = self.node_text(c).to_string();
                        self.block_mut(handler_block).defs.push((
                            name,
                            c.start_position().row as u32,
                            c.start_position().column as u32,
                            "write".to_string(),
                        ));
                    }
                    if !try_body_kind.is_empty() && c.kind() == try_body_kind.as_str() {
                        if let Some(end) = self.walk_body(c, handler_block) {
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
                    }
                    if let Some(et) = except_target {
                        self.add_edge(et, fin_block, EdgeKind::Finally);
                    }
                    if let Some(end) = self.walk_body(c, fin_block) {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        all_terminated = false;
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
        if let Some(body) = node.child_by_field_name(body_field) {
            let try_block = self.new_block_from_node(body);
            self.add_edge(current, try_block, EdgeKind::Fallthrough);
            try_end = self.walk_body(body, try_block);

            // Exception edge from try entry
            if !catch_field.is_empty() {
                if let Some(handler) = node.child_by_field_name(catch_field) {
                    let handler_block = self.new_block_from_node(handler);
                    self.add_edge(try_block, handler_block, EdgeKind::Exception);

                    // Walk handler body
                    if let Some(hbody) = handler.child_by_field_name(body_field) {
                        if let Some(end) = self.walk_body(hbody, handler_block) {
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
                }
                // Walk finalizer body
                if let Some(fbody) = fin.child_by_field_name(body_field) {
                    if let Some(end) = self.walk_body(fbody, fin_block) {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        all_terminated = false;
                    }
                } else {
                    // Finalizer may have its body as direct children
                    if let Some(end) = self.walk_body(fin, fin_block) {
                        self.add_edge(end, join, EdgeKind::Fallthrough);
                        all_terminated = false;
                    }
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

        if all_terminated {
            self.add_edge(current, join, EdgeKind::Fallthrough);
        }

        Some(join)
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

    let cfg_sec = &config_for_ext(ext).cfg;

    // If cfg section has no function_nodes configured, CFG is not supported
    if cfg_sec.function_nodes.is_empty() {
        return Vec::new();
    }

    let source_bytes = source.as_bytes();
    let mut cfgs = Vec::new();
    collect_functions(tree.root_node(), source_bytes, cfg_sec, &mut cfgs);
    cfgs
}

fn collect_functions(
    node: tree_sitter::Node,
    source: &[u8],
    cfg_sec: &CfgSection,
    cfgs: &mut Vec<FunctionCfg>,
) {
    let kind = node.kind();

    // Is this a function node?
    if cfg_sec.function_nodes.iter().any(|n| n == kind) {
        cfgs.push(build_cfg(node, source, cfg_sec));
        return;
    }

    // Decorated definition wrapping a function or container
    if !cfg_sec.decorated_node.is_empty() && kind == cfg_sec.decorated_node {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            let ck = child.kind();
            if cfg_sec.function_nodes.iter().any(|n| n == ck) {
                cfgs.push(build_cfg(child, source, cfg_sec));
                return;
            }
            if cfg_sec.container_nodes.iter().any(|n| n == ck) {
                collect_functions(child, source, cfg_sec, cfgs);
                return;
            }
        }
        return;
    }

    // Recurse into all children
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_functions(child, source, cfg_sec, cfgs);
    }
}
