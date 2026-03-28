//! Per-function control flow graph construction from tree-sitter ASTs.
//!
//! Builds basic-block CFGs for Python functions, supporting if/elif/else,
//! for/while loops (with break/continue), try/except/finally, with statements,
//! match/case, return, raise, and assert.

use std::collections::{HashMap, HashSet};

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
    /// Variable definitions: (name, line, col).
    pub defs: Vec<(String, u32, u32)>,
    /// Variable uses: (name, line, col).
    pub uses: Vec<(String, u32, u32)>,
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
    blocks: Vec<BasicBlock>,
    edges: Vec<CfgEdge>,
    next_id: u32,
    exit_block: BlockId,
    /// Stack of (loop_header, loop_exit) for break/continue.
    loop_stack: Vec<(BlockId, BlockId)>,
}

impl<'a> CfgBuilder<'a> {
    fn new(source: &'a [u8]) -> Self {
        let mut builder = CfgBuilder {
            source,
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
        match kind {
            "assignment" | "augmented_assignment" => {
                // LHS = defs, RHS = uses
                if let Some(left) = node.child_by_field_name("left") {
                    self.collect_defs_from_target(block_id, left);
                }
                if let Some(right) = node.child_by_field_name("right") {
                    self.collect_uses_from_expr(block_id, right);
                }
            }
            "return_statement" => {
                // uses from the return value
                let mut cursor = node.walk();
                for child in node.children(&mut cursor) {
                    if child.kind() != "return" {
                        self.collect_uses_from_expr(block_id, child);
                    }
                }
            }
            "expression_statement" => {
                // expression_statement wraps the real node (assignment, call, etc.)
                if let Some(child) = node.child(0) {
                    self.collect_defs_uses(block_id, child);
                }
            }
            _ => {
                // For other statements, do a generic walk for identifiers
                self.collect_uses_from_expr(block_id, node);
            }
        }
    }

    fn collect_defs_from_target(&mut self, block_id: BlockId, node: tree_sitter::Node) {
        match node.kind() {
            "identifier" => {
                let name = self.node_text(node).to_string();
                let line = node.start_position().row as u32;
                let col = node.start_position().column as u32;
                self.block_mut(block_id).defs.push((name, line, col));
            }
            "tuple" | "list" | "pattern_list" | "tuple_pattern" => {
                let mut cursor = node.walk();
                for child in node.children(&mut cursor) {
                    if child.is_named() {
                        self.collect_defs_from_target(block_id, child);
                    }
                }
            }
            "subscript" | "attribute" => {
                // x[i] = ... or x.attr = ... — x is a use, not a def
                self.collect_uses_from_expr(block_id, node);
            }
            _ => {}
        }
    }

    fn collect_uses_from_expr(&mut self, block_id: BlockId, node: tree_sitter::Node) {
        if node.kind() == "identifier" {
            let name = self.node_text(node).to_string();
            // Skip keywords that tree-sitter might parse as identifiers
            if matches!(
                name.as_str(),
                "True" | "False" | "None" | "self" | "cls"
            ) {
                return;
            }
            let line = node.start_position().row as u32;
            let col = node.start_position().column as u32;
            self.block_mut(block_id).uses.push((name, line, col));
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
        match kind {
            "if_statement" => self.walk_if(node, current),
            "for_statement" => self.walk_for(node, current),
            "while_statement" => self.walk_while(node, current),
            "try_statement" => self.walk_try(node, current),
            "with_statement" => self.walk_with(node, current),
            "match_statement" => self.walk_match(node, current),
            "return_statement" | "raise_statement" => {
                self.add_statement(current, node);
                self.collect_defs_uses(current, node);
                self.add_edge(current, self.exit_block, EdgeKind::Jump);
                None
            }
            "break_statement" => {
                self.add_statement(current, node);
                if let Some(&(_, loop_exit)) = self.loop_stack.last() {
                    self.add_edge(current, loop_exit, EdgeKind::Jump);
                }
                None
            }
            "continue_statement" => {
                self.add_statement(current, node);
                if let Some(&(loop_header, _)) = self.loop_stack.last() {
                    self.add_edge(current, loop_header, EdgeKind::BackEdge);
                }
                None
            }
            "assert_statement" => {
                self.add_statement(current, node);
                self.collect_defs_uses(current, node);
                // Assert can raise AssertionError
                // We model it as: continue on success, exit on failure
                Some(current)
            }
            // Compound statements that don't affect control flow but contain
            // nested definitions (classes, functions) — skip their bodies
            "function_definition" | "async_function_definition" | "class_definition"
            | "decorated_definition" => {
                self.add_statement(current, node);
                Some(current)
            }
            _ => {
                // Simple statement: assignment, expression, import, etc.
                self.add_statement(current, node);
                self.collect_defs_uses(current, node);
                Some(current)
            }
        }
    }

    // ---- Compound statement handlers --------------------------------------

    fn walk_if(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        // Get the condition
        let condition = node.child_by_field_name("condition");
        let cond_range = condition.map(|c| (c.start_byte(), c.end_byte()));

        if let Some(cond) = condition {
            self.collect_uses_from_expr(current, cond);
        }

        // True branch (the "consequence" field)
        if let Some(body) = node.child_by_field_name("consequence") {
            let true_block = self.new_block_from_node(body);
            self.add_edge_cond(current, true_block, EdgeKind::TrueBranch, cond_range);
            match self.walk_body(body, true_block) {
                Some(end) => {
                    self.add_edge(end, join, EdgeKind::Fallthrough);
                    all_terminated = false;
                }
                None => {} // terminated
            }
        }

        // Walk elif/else alternatives
        let mut cursor = node.walk();
        let mut has_else = false;
        let alternatives: Vec<_> = node.children(&mut cursor)
            .filter(|c| c.kind() == "elif_clause" || c.kind() == "else_clause")
            .collect();

        let mut prev_false_from = current;
        let mut prev_cond_range = cond_range;

        for alt in &alternatives {
            match alt.kind() {
                "elif_clause" => {
                    let elif_cond = alt.child_by_field_name("condition");
                    let elif_cond_range = elif_cond.map(|c| (c.start_byte(), c.end_byte()));

                    let elif_test_block = self.new_block_from_node(*alt);
                    self.add_edge_cond(prev_false_from, elif_test_block, EdgeKind::FalseBranch, prev_cond_range);

                    if let Some(cond) = elif_cond {
                        self.collect_uses_from_expr(elif_test_block, cond);
                    }

                    if let Some(body) = alt.child_by_field_name("consequence") {
                        let body_block = self.new_block_from_node(body);
                        self.add_edge_cond(elif_test_block, body_block, EdgeKind::TrueBranch, elif_cond_range);
                        match self.walk_body(body, body_block) {
                            Some(end) => {
                                self.add_edge(end, join, EdgeKind::Fallthrough);
                                all_terminated = false;
                            }
                            None => {}
                        }
                    }

                    prev_false_from = elif_test_block;
                    prev_cond_range = elif_cond_range;
                }
                "else_clause" => {
                    has_else = true;
                    if let Some(body) = alt.child_by_field_name("body") {
                        let else_block = self.new_block_from_node(body);
                        self.add_edge_cond(prev_false_from, else_block, EdgeKind::FalseBranch, prev_cond_range);
                        match self.walk_body(body, else_block) {
                            Some(end) => {
                                self.add_edge(end, join, EdgeKind::Fallthrough);
                                all_terminated = false;
                            }
                            None => {}
                        }
                    }
                }
                _ => {}
            }
        }

        // If no else clause, the false branch goes directly to join
        if !has_else {
            self.add_edge_cond(prev_false_from, join, EdgeKind::FalseBranch, prev_cond_range);
            all_terminated = false;
        }

        if all_terminated {
            None
        } else {
            Some(join)
        }
    }

    fn walk_for(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        // Header: evaluate iterable
        let header = self.new_block_from_node(node);
        self.add_edge(current, header, EdgeKind::Fallthrough);

        // Collect the loop variable as a def in the header
        if let Some(left) = node.child_by_field_name("left") {
            self.collect_defs_from_target(header, left);
        }
        // Collect the iterable as a use in the header
        if let Some(right) = node.child_by_field_name("right") {
            self.collect_uses_from_expr(header, right);
        }

        let exit = self.new_block_from_node(node);

        // Body
        if let Some(body) = node.child_by_field_name("body") {
            let body_block = self.new_block_from_node(body);
            self.add_edge(header, body_block, EdgeKind::TrueBranch);

            self.loop_stack.push((header, exit));
            let body_end = self.walk_body(body, body_block);
            self.loop_stack.pop();

            if let Some(end) = body_end {
                self.add_edge(end, header, EdgeKind::BackEdge);
            }
        }

        // Else clause (executes when loop completes without break)
        let mut cursor = node.walk();
        let else_clause = node.children(&mut cursor)
            .find(|c| c.kind() == "else_clause");

        if let Some(else_node) = else_clause {
            if let Some(body) = else_node.child_by_field_name("body") {
                let else_block = self.new_block_from_node(body);
                self.add_edge(header, else_block, EdgeKind::FalseBranch);
                if let Some(end) = self.walk_body(body, else_block) {
                    self.add_edge(end, exit, EdgeKind::Fallthrough);
                }
            }
        } else {
            self.add_edge(header, exit, EdgeKind::FalseBranch);
        }

        Some(exit)
    }

    fn walk_while(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let header = self.new_block_from_node(node);
        self.add_edge(current, header, EdgeKind::Fallthrough);

        let condition = node.child_by_field_name("condition");
        let cond_range = condition.map(|c| (c.start_byte(), c.end_byte()));

        if let Some(cond) = condition {
            self.collect_uses_from_expr(header, cond);
        }

        let exit = self.new_block_from_node(node);

        if let Some(body) = node.child_by_field_name("body") {
            let body_block = self.new_block_from_node(body);
            self.add_edge_cond(header, body_block, EdgeKind::TrueBranch, cond_range);

            self.loop_stack.push((header, exit));
            let body_end = self.walk_body(body, body_block);
            self.loop_stack.pop();

            if let Some(end) = body_end {
                self.add_edge(end, header, EdgeKind::BackEdge);
            }
        }

        // Else clause
        let mut cursor = node.walk();
        let else_clause = node.children(&mut cursor)
            .find(|c| c.kind() == "else_clause");

        if let Some(else_node) = else_clause {
            if let Some(body) = else_node.child_by_field_name("body") {
                let else_block = self.new_block_from_node(body);
                self.add_edge_cond(header, else_block, EdgeKind::FalseBranch, cond_range);
                if let Some(end) = self.walk_body(body, else_block) {
                    self.add_edge(end, exit, EdgeKind::Fallthrough);
                }
            }
        } else {
            self.add_edge_cond(header, exit, EdgeKind::FalseBranch, cond_range);
        }

        Some(exit)
    }

    fn walk_try(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        // Find children by kind
        let mut cursor = node.walk();
        let children: Vec<_> = node.children(&mut cursor).collect();

        let mut try_body = None;
        let mut except_clauses = Vec::new();
        let mut else_clause = None;
        let mut finally_clause = None;

        for child in &children {
            match child.kind() {
                "block" if try_body.is_none() => try_body = Some(*child),
                "except_clause" | "except_group_clause" => except_clauses.push(*child),
                "else_clause" => else_clause = Some(*child),
                "finally_clause" => finally_clause = Some(*child),
                _ => {}
            }
        }

        // Try body
        let mut try_end = None;
        if let Some(body) = try_body {
            let try_block = self.new_block_from_node(body);
            self.add_edge(current, try_block, EdgeKind::Fallthrough);
            try_end = self.walk_body(body, try_block);
        }

        // Except handlers — each gets an exception edge from the try entry
        let except_target = if !except_clauses.is_empty() {
            let first_except = self.new_block_from_node(except_clauses[0]);

            // Exception edge from try start to first except
            if let Some(body) = try_body {
                let try_entry = self.blocks.iter()
                    .find(|b| b.start_byte == body.start_byte())
                    .map(|b| b.id);
                if let Some(te) = try_entry {
                    self.add_edge(te, first_except, EdgeKind::Exception);
                }
            }

            // Walk each except handler
            for except in &except_clauses {
                let handler_block = self.new_block_from_node(*except);

                // Collect exception variable if present (as x)
                let mut ec = except.walk();
                for c in except.children(&mut ec) {
                    if c.kind() == "identifier" {
                        let name = self.node_text(c).to_string();
                        self.block_mut(handler_block).defs.push((
                            name,
                            c.start_position().row as u32,
                            c.start_position().column as u32,
                        ));
                    }
                    if c.kind() == "block" {
                        match self.walk_body(c, handler_block) {
                            Some(end) => {
                                if finally_clause.is_some() {
                                    // Will be connected to finally
                                } else {
                                    self.add_edge(end, join, EdgeKind::Fallthrough);
                                }
                                all_terminated = false;
                            }
                            None => {}
                        }
                    }
                }
            }

            Some(first_except)
        } else {
            None
        };

        // Else clause (runs if try completed without exception)
        if let Some(else_node) = else_clause {
            if let Some(body) = else_node.child_by_field_name("body") {
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
                        try_end = Some(end); // For finally chaining
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

        // Finally clause
        if let Some(fin_node) = finally_clause {
            let mut fc = fin_node.walk();
            for c in fin_node.children(&mut fc) {
                if c.kind() == "block" {
                    let fin_block = self.new_block_from_node(c);

                    // Connect try end (or else end) to finally
                    if let Some(te) = try_end {
                        self.add_edge(te, fin_block, EdgeKind::Finally);
                    }

                    // Connect except handler ends to finally
                    // (simplified: connect the except entry to finally)
                    if let Some(et) = except_target {
                        self.add_edge(et, fin_block, EdgeKind::Finally);
                    }

                    match self.walk_body(c, fin_block) {
                        Some(end) => {
                            self.add_edge(end, join, EdgeKind::Fallthrough);
                            all_terminated = false;
                        }
                        None => {}
                    }
                }
            }
        }

        if all_terminated {
            None
        } else {
            Some(join)
        }
    }

    fn walk_with(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        // Collect context manager uses and "as" defs
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "with_clause" || child.kind() == "with_item" {
                let mut ic = child.walk();
                for item in child.children(&mut ic) {
                    if item.kind() == "with_item" {
                        // Value is a use, alias is a def
                        if let Some(val) = item.child_by_field_name("value") {
                            self.collect_uses_from_expr(current, val);
                        }
                        if let Some(alias) = item.child_by_field_name("alias") {
                            self.collect_defs_from_target(current, alias);
                        }
                    } else if item.kind() == "as_pattern" {
                        if let Some(val) = item.child(0) {
                            self.collect_uses_from_expr(current, val);
                        }
                        if let Some(alias) = item.child(2) {
                            self.collect_defs_from_target(current, alias);
                        }
                    }
                }
            }
        }

        // Walk the body
        if let Some(body) = node.child_by_field_name("body") {
            self.walk_body(body, current)
        } else {
            Some(current)
        }
    }

    fn walk_match(&mut self, node: tree_sitter::Node, current: BlockId) -> Option<BlockId> {
        let join = self.new_block_from_node(node);
        let mut all_terminated = true;

        // Subject expression
        if let Some(subject) = node.child_by_field_name("subject") {
            self.collect_uses_from_expr(current, subject);
        }

        // Case clauses
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "case_clause" {
                let case_block = self.new_block_from_node(child);
                self.add_edge(current, case_block, EdgeKind::Fallthrough);

                // Find the block body inside the case clause
                let mut cc = child.walk();
                for case_child in child.children(&mut cc) {
                    if case_child.kind() == "block" {
                        match self.walk_body(case_child, case_block) {
                            Some(end) => {
                                self.add_edge(end, join, EdgeKind::Fallthrough);
                                all_terminated = false;
                            }
                            None => {}
                        }
                    }
                }
            }
        }

        // If no cases matched (shouldn't happen with well-formed match),
        // fall through to join
        if all_terminated {
            // All cases terminated — join block is unreachable
            // But we still return it for structural consistency
            self.add_edge(current, join, EdgeKind::Fallthrough);
        }

        Some(join)
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Build a CFG for a single function definition node.
pub fn build_cfg(node: tree_sitter::Node, source: &[u8]) -> FunctionCfg {
    let func_name = node
        .child_by_field_name("name")
        .map(|n| n.utf8_text(source).unwrap_or("").to_string())
        .unwrap_or_else(|| "<anonymous>".to_string());

    let func_start_line = node.start_position().row as u32;
    let func_end_line = node.end_position().row as u32;

    let mut builder = CfgBuilder::new(source);
    let entry = BlockId(0);

    // Find the function body
    if let Some(body) = node.child_by_field_name("body") {
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

    let source_bytes = source.as_bytes();
    let mut cfgs = Vec::new();
    collect_functions(tree.root_node(), source_bytes, &mut cfgs);
    cfgs
}

fn collect_functions(node: tree_sitter::Node, source: &[u8], cfgs: &mut Vec<FunctionCfg>) {
    let kind = node.kind();
    if kind == "function_definition" || kind == "async_function_definition" {
        cfgs.push(build_cfg(node, source));
        // Don't recurse into nested functions — they get their own CFG
        return;
    }

    // For decorated_definition, check if it wraps a function
    if kind == "decorated_definition" {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "function_definition" || child.kind() == "async_function_definition" {
                cfgs.push(build_cfg(child, source));
                return;
            }
            if child.kind() == "class_definition" {
                collect_functions(child, source, cfgs);
                return;
            }
        }
        return;
    }

    // Recurse into all children
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_functions(child, source, cfgs);
    }
}
