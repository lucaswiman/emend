# Per-Function Control Flow Graphs

**Status: IMPLEMENTED** — `cfg.py` (Python), `cfg.rs` / `cfg_py.rs` (Rust/PyO3 in
`emend_core`).  CLI: `emend cfg --function --format text|json|dot --unreachable`.
Fact graph integration: `CfgEdgeFact`, `DefUseFact` with CozoDB schema.
Multi-language: Python, TypeScript/JS, Rust.  Dominators and post-dominators
computed iteratively.  See `tests/test_emend/test_cfg.py`,
`test_cfg_typescript.py`, `test_cfg_rust.py`.

The sections below are the original design document, preserved as historical
context.  The "What Exists Today" section describes the pre-implementation
state.

---

## Motivation

Today every intraprocedural analysis in emend (taint, flow-rule lint, typestate
if we build it, None-tracking if we build it) does its own ad-hoc approximation
of control flow by walking assignments in source-line order.  The result is
path-insensitive: a tainted value on one branch can propagate to code on
another branch, creating false positives; a sanitizer on one branch is treated
as sanitizing all branches.

A shared CFG representation built once per function and reused by all analyses
would:

1. Eliminate duplicated control-flow approximation code.
2. Make taint, flow-rule lint, and future analyses path-sensitive.
3. Feed `ControlDependsOn` / `Dominates` / `PostDominates` relations into
   the fact graph (and the Datalog engine).
4. Enable unreachable-code detection within functions (`if False:`, code
   after unconditional `return`).

## What Exists Today

### Taint engine (`taint.py::_analyze_function`)

Extracts assignments via `_find_assignments_in_source()` (which uses
`emend_core.get_statement_ranges()`), then walks them in line-number order:

- **Branches**: Ignored — all branches treated as executed.
- **Loops**: Single-pass; no iteration modeling.
- **Try/except/finally**: All exception paths treated as always taken.
- **With statements**: Context-manager enter/exit not modeled.

### Lint flow rules (`lint.py::_check_flow_rule`)

Same linear walk: finds source match, propagates taint through sorted
assignments, checks sinks.  Uses line-number ordering as a proxy for
"reachable."

### Rust scope resolver (`scope.rs`)

Builds a **scope tree** (Module / Function / Class / Comprehension / Block)
and recurses into compound statements (`if_statement`, `for_statement`,
`while_statement`, `try_statement`, `with_statement`, `match_statement`).
But only for name binding — no control-flow edges, no branch conditions.

### Dead code (`transform.py::find_dead_code`)

Purely interprocedural (reference counting).  No intraprocedural
unreachable-code detection.

## Proposed Data Model

### Basic Blocks

A basic block is a maximal sequence of statements with no internal branches
or join points.  Entry at the top, exit at the bottom.

```rust
struct BasicBlock {
    id: BlockId,
    /// Byte range in source [start, end).
    start_byte: usize,
    end_byte: usize,
    /// Line range (1-indexed, inclusive).
    start_line: u32,
    end_line: u32,
    /// Statements in this block (byte ranges).
    statements: Vec<(usize, usize)>,
    /// Variable definitions (name, line, col).
    defs: Vec<(String, u32, u32)>,
    /// Variable uses (name, line, col).
    uses: Vec<(String, u32, u32)>,
}
```

### Edges

```rust
enum EdgeKind {
    /// Normal sequential flow.
    Fallthrough,
    /// Condition was true.
    TrueBranch,
    /// Condition was false.
    FalseBranch,
    /// Exception raised (from try body to except handler).
    Exception,
    /// finally clause (always taken).
    Finally,
    /// Loop back-edge.
    BackEdge,
    /// Return / break / continue.
    Jump,
}

struct CfgEdge {
    from: BlockId,
    to: BlockId,
    kind: EdgeKind,
    /// For TrueBranch/FalseBranch: byte range of the condition expression.
    condition: Option<(usize, usize)>,
}
```

### Function CFG

```rust
struct FunctionCfg {
    /// Entry block (always id 0).
    entry: BlockId,
    /// Exit block (synthetic; all returns flow here).
    exit: BlockId,
    blocks: Vec<BasicBlock>,
    edges: Vec<CfgEdge>,
}
```

### Python Exposure (via PyO3)

```python
class PyCfg:
    """Per-function control flow graph."""
    entry: int          # entry block id
    exit: int           # exit block id
    blocks: list[dict]  # [{id, start_line, end_line, defs: [...], uses: [...]}]
    edges: list[dict]   # [{from, to, kind, condition_text?}]

    def predecessors(self, block_id: int) -> list[int]: ...
    def successors(self, block_id: int) -> list[int]: ...
    def dominators(self, block_id: int) -> set[int]: ...
    def post_dominators(self, block_id: int) -> set[int]: ...
```

## Construction Algorithm

### Input

A tree-sitter `function_definition` (or `async_function_definition`) node.

### Block-splitting rules

Walk the function body's tree-sitter AST.  Start a new basic block at:

| Tree-sitter node | New block(s) |
|-------------------|--------------|
| `if_statement` | Condition block; true-body block; false/elif/else block; join block |
| `for_statement` | Header block (iterable eval); loop-body block; else block; join block |
| `while_statement` | Condition block; loop-body block; else block; join block |
| `try_statement` | Try-body block; one block per except clause; else block; finally block; join block |
| `with_statement` | Enter block; body block; exit block (implicit finally) |
| `match_statement` | Subject block; one block per case clause; join block |
| `return_statement` | Terminates current block; edge to exit |
| `raise_statement` | Terminates current block; edge to enclosing except or exit |
| `break` / `continue` | Terminates current block; edge to loop exit / loop header |
| `assert_statement` | Split: true branch continues; false branch to exit (AssertionError) |

Everything else (assignments, expressions, function calls) stays in the
current block.

### Edge construction

After splitting, add edges:

- **Sequential**: last block of one statement → first block of next.
- **Branches**: condition block → true block (TrueBranch), condition block →
  false block (FalseBranch).
- **Loops**: end of loop body → condition block (BackEdge); condition false →
  join (or else block).
- **Exceptions**: every statement in try body gets an implicit Exception edge
  to the first matching except handler.  (Coarse-grained: one edge per try
  body, not per statement.  Finer granularity is possible later.)
- **Finally**: try/except/else all edge to finally; finally edges to join.
- **Return/raise/break/continue**: edge to exit / enclosing handler / loop
  exit / loop header.

### Complexity

**Time:** O(n) in the number of tree-sitter nodes — single pass, constant
work per node.  Dominators are O(n * d) where d is the dominator-tree depth
(typically small for Python functions).

**Space:** O(b + e) where b = number of basic blocks and e = number of edges.
For a typical Python function (10-50 statements), this is tens of blocks and
tens of edges — negligible.

**Cost at scale:** Building CFGs for all functions in a 100k-line project is
< 100ms if done in Rust alongside the existing tree-sitter parse.  The
scope resolver already visits every function; adding CFG construction is
incremental work on the same pass.

## Caching

CFGs are keyed by (file content hash, function qualified name) in `parse.db`,
same as the scope resolver's symbol index.  They can be serialized as
compact binary (block count + edge list) or as JSON for debugging.

Since `emend_core` already caches parsed tree-sitter trees per file, CFG
construction only needs the tree — no re-parse.

## Fact Graph Integration

Once CFGs exist, the following relations can be emitted into the fact graph:

```
ControlDependsOn(stmt_line, branch_line)
Dominates(block_a, block_b)
PostDominates(block_a, block_b)
Reaches(block_a, block_b)        # transitive reachability
DefUse(def_line, def_var, use_line, use_var)  # per-block precision
```

These feed directly into the Datalog engine, enabling slicing, typestate,
and None-tracking as rule sets.

## What It Unblocks

| Analysis | How CFGs help |
|----------|---------------|
| **Taint analysis** | Path-sensitive: taint on one branch doesn't leak to the other |
| **Flow-rule lint** | Sanitizer on one branch doesn't protect the other |
| **Typestate** | Must-close-on-all-paths: check that all paths to exit pass through `close()` |
| **None/Optional** | `if x is not None:` guard narrows the domain on the true branch only |
| **Unreachable code** | Code after unconditional return, `if False:`, dead except clauses |
| **Spec mining** | Temporal call ordering: A always before B on all paths |
| **Datalog queries** | `ControlDependsOn`, `Dominates`, `DefUse` as input relations |

## Implementation Plan

1. **Rust: `build_cfg(node: tree_sitter::Node) -> FunctionCfg`** — tree-sitter
   AST to basic blocks + edges.  ~500-800 lines.  Add to `emend_core`.
2. **Rust: dominator/post-dominator computation** — standard iterative
   algorithm.  ~100 lines.
3. **PyO3: `PyCfg` wrapper** — expose to Python.  ~100 lines.
4. **Integration: `_analyze_function()` in taint.py** — replace linear walk
   with CFG-guided dataflow.  Iterate over blocks in reverse-postorder,
   propagate taint state along edges, merge at join points (union).
5. **Integration: `_check_flow_rule()` in lint.py** — same pattern.
6. **Fact graph: emit `ControlDependsOn` / `DefUse` facts** — from CFG + defs/uses.
7. **Cache in parse.db** — alongside symbol_index and reference_index.

Steps 1-3 are the foundation (~1000 lines of Rust).  Steps 4-7 are
incremental consumers (~200-300 lines of Python each).
