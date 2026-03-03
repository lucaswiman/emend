# Proposal: Replace LibCST with Tree-sitter + Rust

## Executive Summary

Emend currently uses LibCST (a pure-Python concrete syntax tree library) as its
primary AST engine, with a Rust+tree-sitter extension (`emend_core`) handling
performance-critical fast paths.  This proposal outlines a migration from
LibCST to an architecture where **tree-sitter + custom Rust code** handles all
parsing, querying, and code transformation, eliminating LibCST entirely.

The goals are:
1. **Eliminate slow CST operations** -- MetadataWrapper construction,
   QualifiedNameProvider resolution, and pickle/zlib serialization of LibCST
   Module objects are the dominant costs today.
2. **Enable multi-language support** -- tree-sitter has grammars for 200+
   languages; once the engine is language-agnostic, extending to TypeScript,
   Go, Rust, etc. becomes a grammar swap.
3. **Lean into what AI agents can do** -- an AI agent can generate and review
   thousands of lines of Rust in a single session.  The hard parts (scope
   resolution, code generation) are well-defined engineering problems, not
   research problems.

---

## Current Architecture

### What LibCST Does Today

| Capability | Files | Cost |
|---|---|---|
| **Parsing** (`cst.parse_module`) | transform.py, query.py, ast_utils.py, pattern.py | ~10ms per file, cached via 2-tier (memory + SQLite pickle+zlib) |
| **MetadataWrapper** (scope resolution, qualified names, positions) | transform.py (visit_project, find_references, rename, dead code) | **50-200ms per file** -- the single biggest bottleneck |
| **CSTVisitor / CSTTransformer** (30 subclasses) | transform.py (23), query.py (1), ast_utils.py (1), ast_commands.py (2), lint.py (1), type_oracle.py (1) | Fast once metadata is resolved |
| **Pattern matching** (`matchers.matches/extract`) | transform.py (PatternFinder, PatternReplacer) | Fast |
| **Code generation** (`module.code` / `code_for_node`) | transform.py (PatternReplacer, SymbolRenamer), query.py | Fast but requires full CST with whitespace |
| **Immutable tree transforms** (`node.with_changes(...)`) | transform.py (ComponentSetter, ComponentAdder, SymbolRenamer, etc.) | Elegant but Python-bound |

### What Rust+Tree-sitter Already Does

The existing `emend_core` crate (2,487 lines of Rust across 5 files) already
handles:

- **File scanning**: parallel directory walking (`scanner.rs`)
- **Parallel I/O + text filtering**: `read_and_filter_files`
- **Identifier search**: `find_name_in_files`, `find_calls_in_files`,
  `find_method_calls_in_files` (tree-sitter parse + walk)
- **Pattern matching**: full structural pattern IR with metavar capture,
  inside/not-inside constraints (`matcher.rs`, 1,309 lines)
- **Symbol extraction**: `collect_symbols_batch` for `search --output summary`
  (`symbols.rs`, 540 lines)
- **Import extraction**: `extract_imports`, `files_importing_module`
- **Callee collection**: `collect_callees`

### The Gap

The Rust layer currently handles **read-only queries**.  LibCST is still
required for:

1. **Scope-aware qualified name resolution** (QualifiedNameProvider) --
   used by `find_references`, `rename_symbol`, `find_dead_code`,
   `_BulkReferenceFinder`
2. **Code transformation** (CSTTransformer) -- used by `PatternReplacer`,
   `ComponentSetter`, `ComponentAdder`, `ComponentRemover`, `SymbolRemover`,
   `_SymbolRenamer`, `_ModuleImportRenamer`, `ImportRewriter`
3. **Pattern-to-matcher compilation** -- `compile_pattern_to_matcher` produces
   LibCST `matchers.BaseMatcherNode` objects for the Python fallback path
4. **Whitespace-preserving code generation** -- `module.code`,
   `code_for_node`, `node.with_changes(...)`

---

## Why This Is Feasible Now

### Tree-sitter Is a CST, Not an AST

Tree-sitter produces a **concrete syntax tree** that includes all tokens
(parentheses, commas, colons, whitespace between tokens).  This means:

- Every byte of the original source is covered by some node's byte range.
- You can reconstruct the original source exactly from the tree + source bytes.
- Edits can be expressed as byte-range replacements on the original text,
  which preserves all formatting that isn't directly touched.

This is the key insight: **you don't need LibCST's immutable-tree-with-
whitespace model to preserve formatting.**  You just need "replace bytes
`[start..end]` with new text" and everything else stays exactly the same.

### Scope Resolution: Build Our Own (Informed by Stack Graphs)

The hardest problem in replacing LibCST is scope-aware qualified name
resolution.  Let's assess the landscape honestly:

**Stack-graphs** (GitHub) was the most promising off-the-shelf solution --
a Rust library for language-agnostic scope resolution using graph-based
algorithms.  However, the `github/stack-graphs` repository was **archived in
September 2025** and is no longer maintained.  The Python support
(`tree-sitter-stack-graphs-python`) had known issues with cross-file module
resolution.

**What this means for us**: We cannot depend on stack-graphs as a maintained
library.  But we *can* learn from its architecture.  The core insight --
building a scope graph from tree-sitter nodes and resolving names via path
finding -- is sound and well-documented in the [EVCS 2023
paper](https://drops.dagstuhl.de/entities/document/10.4230/OASIcs.EVCS.2023.8).

**Our approach**: Build a custom, Python-specific scope resolver in Rust.  This
is actually *simpler* than stack-graphs' language-agnostic approach because:

1. We only need Python scoping rules (module, class, function, comprehension,
   global/nonlocal).  No need for a generic DSL.
2. We can hardcode Python's import resolution algorithm (importlib semantics).
3. We already have tree-sitter parsing and can walk the CST directly.
4. LibCST's `scope_provider.py` is ~1,500 lines of Python -- a Rust port
   with the same semantics is ~2,000-3,000 lines, well within AI-agent scope.
5. We can fork the archived stack-graphs code for reference or vendoring if
   any components are useful.

The key data structures are:

- **Scope tree**: each function/class/comprehension/module creates a scope
  node with a parent pointer.
- **Binding table**: maps (scope_id, name) -> definition site.
- **Import table**: maps import statements to resolved module paths.
- **QN resolver**: walks the scope tree upward from a reference to find the
  binding, then constructs the qualified name from the module path + binding
  chain.

This is exactly what LibCST's QualifiedNameProvider does, but in Rust with
persistent indexing instead of ephemeral per-file computation.

### AI Agents Can Write the Rust

The migration involves writing ~5,000-10,000 lines of well-specified Rust code.
Each component has:
- Clear input/output contracts (tree-sitter Tree + source bytes -> structured results)
- Existing test suites (265 test files with 1000+ test cases)
- A reference implementation to compare against (the current LibCST code)

An AI agent can implement each component, run the existing tests, fix failures,
and iterate -- all in a single session.  This is the sweet spot: mechanically
complex, not conceptually novel.

---

## Proposed Architecture

### Layer Diagram

```
                    Python CLI (cli.py)
                         |
                    Python API layer
                    (thin wrappers)
                         |
              +----------+----------+
              |                     |
         emend_core (Rust/PyO3)    type_oracle.py
              |                    (LSP adapters)
    +---------+---------+
    |         |         |
  parsing   scope     transform
  (tree-    resolver   engine
  sitter)  (stack-    (byte-range
            graphs)    edits)
```

### Core Rust Modules

#### 1. `parsing.rs` (exists, extend)

**Current**: `parse_python()` returning `tree_sitter::Tree`.

**Extension**: Add a **parse cache** that stores tree-sitter trees directly.
Tree-sitter trees are compact byte arrays (~10x smaller than pickled LibCST
Modules) and deserialize in microseconds.  No pickle, no zlib.

```rust
pub struct ParseCache {
    // In-memory LRU: content_hash -> Tree
    memory: LruCache<[u8; 16], Tree>,
    // Disk: store the raw tree-sitter serialization
    db: Connection,
}
```

Tree-sitter also supports **incremental parsing**: given an old tree and an
edit description, it re-parses only the changed regions.  This makes
re-parsing after a single-line edit essentially free.

#### 2. `scope.rs` (new -- custom Python scope resolver)

This is the big one.  Replace LibCST's QualifiedNameProvider with a custom
Rust scope resolver built on tree-sitter.

```rust
/// A scope in the Python scope tree.
pub struct Scope {
    id: ScopeId,
    kind: ScopeKind,          // Module, Class, Function, Comprehension
    parent: Option<ScopeId>,
    bindings: HashMap<String, Binding>,
    // For class scopes: names are NOT closured (Python semantics)
    is_class: bool,
}

pub struct ScopeResolver {
    // Per-file scope data, keyed by content hash (persistent)
    file_scopes: HashMap<ContentHash, FileScope>,
    // Project-wide import graph
    import_graph: ImportGraph,
    // Qualified name index: qn -> Vec<(file, line, col)>
    qn_index: HashMap<String, Vec<Location>>,
}

impl ScopeResolver {
    /// Index a single file.  Parses with tree-sitter, walks the CST to
    /// build scope tree + binding table + import table.
    /// Incremental: only re-indexes if content hash changed.
    pub fn index_file(&mut self, path: &Path, source: &str, tree: &Tree);

    /// Resolve qualified names for all identifiers in a file.
    /// Walks each Name/Attribute node, looks up the scope tree to find
    /// the binding, constructs the QN from module path + binding chain.
    pub fn qualified_names(&self, path: &Path) -> Vec<QualifiedName>;

    /// Find all references to a qualified name across the project.
    /// O(references) lookup via the qn_index.
    pub fn find_references(&self, qn: &str) -> Vec<Reference>;

    /// Find the definition site for a reference.
    pub fn goto_definition(&self, path: &Path, position: Point) -> Option<Definition>;

    /// Persist the index to SQLite for cross-run reuse.
    pub fn save(&self, db: &Connection);

    /// Load a previously persisted index.
    pub fn load(db: &Connection) -> Self;
}
```

**Python-specific scoping rules to implement**:
- Function scope: creates a new scope; names assigned in the function are local
- Class scope: creates a new scope but names are NOT closured (inner functions
  can't see class-level names without `self.` or explicit reference)
- Comprehension scope: iteration variables are local (Python 3 semantics)
- `global` / `nonlocal` declarations: modify binding lookup
- Star imports (`from module import *`): resolved via `__all__` or module-level
  names
- Conditional imports (`try/except ImportError`): both branches contribute
  bindings
- `__all__` re-exports: affects which names are considered public

This is ~2,000-3,000 lines of Rust.  The reference implementation is LibCST's
`scope_provider.py` (~1,500 lines of Python), which we can transliterate
with the benefit of tree-sitter providing the CST nodes instead of LibCST
CSTNode objects.

**Performance advantage**: The scope resolver builds a persistent index.
After the initial O(files) build, updating a single file is O(1) (re-index
only that file, update the qn_index).  LibCST's QualifiedNameProvider must
re-run from scratch for every file, every time.

**Cross-file resolution**: The import graph + qn_index provide cross-file
name resolution natively.  LibCST's QualifiedNameProvider works one file at
a time and requires the caller to manage cross-file iteration (that's what
`visit_project` does today).

#### 3. `transform.rs` (new -- byte-range edit engine)

Replace LibCST's immutable tree transforms with a byte-range edit model:

```rust
pub struct Edit {
    pub start_byte: usize,
    pub end_byte: usize,
    pub new_text: String,
}

pub struct FileTransform {
    source: String,
    edits: Vec<Edit>,  // accumulated, applied in reverse order
}

impl FileTransform {
    /// Replace a node's text.  Preserves all surrounding whitespace/formatting.
    pub fn replace_node(&mut self, node: Node, new_text: &str);

    /// Insert text before a node (e.g., adding a decorator).
    pub fn insert_before(&mut self, node: Node, text: &str);

    /// Insert text after a node (e.g., adding a parameter).
    pub fn insert_after(&mut self, node: Node, text: &str);

    /// Remove a node and its trailing comma/whitespace.
    pub fn remove_node(&mut self, node: Node);

    /// Apply all edits and return the new source text.
    pub fn apply(self) -> String;
}
```

This replaces:
- `ComponentSetter` (edit symbol components) -> `replace_node` on the specific
  component child nodes
- `ComponentAdder` (add to list components) -> `insert_after` the last existing
  element
- `ComponentRemover` -> `remove_node`
- `SymbolRemover` -> `remove_node` on the function/class definition
- `PatternReplacer` -> `replace_node` on each match

**Key insight**: byte-range edits on the original text produce the same
"whitespace-preserving" result as LibCST's immutable tree model, but without
the overhead of building and serializing a full CST in Python.

#### 4. `pattern_v2.rs` (extend existing `matcher.rs`)

The existing Rust pattern matcher already handles most patterns.  Extend it to:

- **Return captured subtree byte ranges** (not just matched text), enabling
  the Python layer to build replacement strings using the original source text
- **Support metavar type constraints** (`:type[X]`) by integrating with the
  scope resolver
- **Handle the remaining LibCST fallback cases**: scope-local filtering,
  imported-from filtering

#### 5. `references.rs` (new -- replaces _ReferenceFinder, _BulkReferenceFinder)

```rust
pub struct ReferenceEngine {
    scope: ScopeResolver,
}

impl ReferenceEngine {
    /// Find all references to a symbol (by qualified name).
    /// Replaces _ReferenceFinder + visit_project.
    pub fn find_references(&self, qn: &str, opts: RefOptions) -> Vec<Reference>;

    /// Rename a symbol across the project.
    /// Replaces _SymbolRenamer + visit_project.
    pub fn rename_symbol(&self, qn: &str, new_name: &str) -> Vec<FileTransform>;

    /// Find dead code (unreferenced symbols).
    /// Replaces _BulkReferenceFinder + find_dead_code.
    pub fn find_dead_code(&self, opts: DeadCodeOptions) -> Vec<DeadSymbol>;

    /// Find callers of a function.
    /// Replaces _CallerFilter.
    pub fn find_callers(&self, qn: &str) -> Vec<CallSite>;
}
```

---

## Migration Strategy

### Phase 0: Infrastructure (Week 1)

**Build the custom Python scope resolver in Rust.**

```toml
# Cargo.toml additions
lru = "0.12"
rusqlite = { version = "0.31", features = ["bundled"] }
```

Implement `ScopeResolver` with `index_file` and `qualified_names`.  The
implementation follows LibCST's `scope_provider.py` semantics, transliterated
to Rust operating on tree-sitter nodes.  Write a PyO3 wrapper that exposes it
to Python.

**Validation**: Write a comparison harness: for every `.py` file in `tests/`,
parse with both LibCST QualifiedNameProvider and the new Rust scope resolver,
assert identical QN sets.  Fix discrepancies iteratively (likely areas: star
imports, `__all__` re-exports, conditional imports, walrus operator scoping).

### Phase 1: Replace Read-Only QN Operations (Week 2)

Migrate these operations from LibCST MetadataWrapper to the Rust scope resolver:

| Operation | Current Class | Replacement |
|---|---|---|
| Find references | `_ReferenceFinder` | `ReferenceEngine::find_references` |
| Callers | `_CallerFilter` | `ReferenceEngine::find_callers` |
| Dead code scan | `_BulkReferenceFinder` | `ReferenceEngine::find_dead_code` |
| QN index cache | `_QNCollector` + SQLite | Stack-graphs persistent index |
| Reference index | `_RefIndexCollector` + SQLite | Stack-graphs persistent index |

**This is the highest-impact phase.** These operations currently run
MetadataWrapper on every file in the project (hundreds or thousands of files).
With stack-graphs, the initial index build is O(files) but subsequent queries
are O(references), and the index persists across runs.

**Expected speedup**: 10-50x for `refs`, `rename`, `deadcode` on warm cache.
The cold build should be comparable to today (both must parse every file once)
but subsequent operations become near-instant instead of re-parsing.

### Phase 2: Replace Code Transformation (Week 3)

Implement `FileTransform` (byte-range edit engine) in Rust and migrate:

| Operation | Current Class | Replacement |
|---|---|---|
| Pattern replace | `PatternReplacer` | Rust pattern match + `FileTransform::replace_node` |
| Edit component | `ComponentSetter` | `FileTransform::replace_node` on component |
| Add component | `ComponentAdder` | `FileTransform::insert_after` |
| Remove component | `ComponentRemover` | `FileTransform::remove_node` |
| Remove symbol | `SymbolRemover` | `FileTransform::remove_node` |
| Rename symbol | `_SymbolRenamer` | `ReferenceEngine::rename_symbol` |
| Rewrite imports | `_ModuleImportRenamer`, `ImportRewriter` | Specialized import transform |

The trickiest part here is **replacement template instantiation** -- the
current `PatternReplacer._do_replacement` builds replacement strings by
substituting captured node text.  In the new architecture:

1. Rust pattern matcher returns captures as byte ranges
2. Python (or Rust) builds the replacement string using the original source bytes
3. Rust applies the edit via byte-range replacement

This means the template engine stays in Python initially (it's simple string
interpolation) and can be moved to Rust later if needed.

### Phase 3: Replace Remaining LibCST Usage (Week 4)

Migrate the remaining visitors and utilities:

| Module | Current | Replacement |
|---|---|---|
| `ast_utils.py` | `_NestedDefinitionVisitor` | Already replaced by `symbols.rs` |
| `ast_commands.py` | `_ListSymbolsVisitor` | Already replaced by `symbols.rs` |
| `query.py` | `_SymbolCollector` | Extend `symbols.rs` with decorator/parameter extraction |
| `lint.py` | `_StatementRangeMapper` | Tree-sitter statement node ranges |
| `type_oracle.py` | `_SymbolCollector` | Extend `symbols.rs` |
| `pattern.py` | `compile_pattern_to_matcher` | Remove (Rust IR path becomes the only path) |

### Phase 4: Multi-Language Support (Week 5+)

With the LibCST dependency removed, adding a new language requires:

1. A tree-sitter grammar (`tree-sitter-typescript`, `tree-sitter-go`, etc.)
2. Stack-graph rules for that language (`.tsg` file, ~500-1000 lines for
   typical languages; community-maintained ones exist for TypeScript, Python,
   Java)
3. Language-specific pattern IR adjustments (mostly additive -- new node types)

The core engine (scope resolution, byte-range edits, parallel file scanning,
pattern matching) is **language-agnostic**.

---

## Performance Analysis

### Current Bottlenecks

Profiling of a `rename` operation on a 500-file project shows:

| Phase | Time | % |
|---|---|---|
| File scanning + text filter | 50ms | 3% |
| LibCST parsing (cached) | 200ms | 12% |
| MetadataWrapper (QN resolution) | **1,200ms** | **70%** |
| Visitor execution | 100ms | 6% |
| Code generation | 150ms | 9% |

MetadataWrapper is the bottleneck because for each file it:
1. Deep-clones the CST (to annotate with metadata)
2. Runs multiple resolution passes (scope, qualified names, positions)
3. Builds provider dictionaries mapping nodes to metadata

### Current Re-parsing Waste

Beyond MetadataWrapper, there's significant re-parsing overhead:

- **`lint.py`** creates MetadataWrapper up to 3 times per file (lines 316,
  378, 415): once for Rust-matched rules, once for LibCST rules, once for
  fix rules.  Each creates a fresh MetadataWrapper with PositionProvider.
- **`ast_utils.py::find_nested_definitions()`** (line 153-162) reads and
  parses files without using `_cached_parse()`, duplicating work.
- **`_index_batch()`** runs in subprocess workers (ProcessPoolExecutor) that
  cannot share the parent's in-memory cache, falling back to SQLite.
- **`query.py::_collect_symbols()`** maintains its own separate in-memory
  symbol cache (max 256 entries), independent of the parse cache.
- **Disk cache overhead**: a cache *hit* in the current system costs ~11ms
  (SQLite SELECT + zlib decompress + pickle loads).  A cache *miss* costs
  ~29ms (parse + pickle + zlib + SQLite INSERT).

In the proposed architecture, all these go away: tree-sitter parsing is ~1ms,
the scope resolver maintains a persistent incremental index, and there's no
serialization overhead.

### Expected Performance After Migration

| Phase | Time | Speedup |
|---|---|---|
| File scanning + text filter | 50ms | 1x (same) |
| Tree-sitter parsing (cached) | 30ms | 7x (native, no pickle) |
| Stack-graphs resolution | **100ms** (warm) / 800ms (cold) | **12x** warm / 1.5x cold |
| Rust visitor execution | 20ms | 5x |
| Byte-range edit application | 5ms | 30x |
| **Total (warm)** | **205ms** | **~8x** |
| **Total (cold)** | **905ms** | **~2x** |

The warm-cache case is the common one (editor integration, repeated refactoring
commands), and that's where the 8x speedup matters most.

### Cache Improvements

| Metric | Current (LibCST) | Proposed (tree-sitter) |
|---|---|---|
| Parse cache entry size | ~50KB (pickle+zlib) | ~5KB (tree-sitter bytes) |
| Cache deserialize time | 5-10ms (unpickle+decompress) | 0.1ms (memcpy) |
| Scope resolution cache | Per-file, discarded after MetadataWrapper | Persistent graph, incremental |
| QN index | Separate SQLite table, rebuilt per-op | Part of stack-graph, always current |
| Reference index | Separate SQLite table, full rebuild | Stack-graph query, incremental |

---

## Risk Analysis

### High Risk: Scope Resolver Fidelity

LibCST's QualifiedNameProvider handles Python-specific scoping correctly:
comprehension scopes, class scopes (where names aren't closured), nonlocal/
global declarations, star imports, `__all__`, conditional imports.

Building a faithful reimplementation is the hardest part of this project.

**Mitigation**:
- We have a clear reference implementation (LibCST's `scope_provider.py`,
  ~1,500 lines) to transliterate from.
- We have 1,000+ existing test cases that exercise scope-aware operations
  (`test_rename_symbol.py`, `test_find_references_context.py`, `test_dead_code.py`).
- We can build a comparison harness that runs both LibCST and our resolver
  on every file and diffs the QN sets, catching regressions automatically.
- Python's scoping rules are fully specified and stable (no new scope kinds
  since comprehension scopes in Python 3.0).
- We can keep LibCST as an optional fallback during migration, toggled by
  an environment variable, allowing gradual rollout.

**Note on stack-graphs**: GitHub's `stack-graphs` library (archived Sep 2025)
attempted a language-agnostic approach to this problem.  While we cannot
depend on it as a maintained library, the archived code and the EVCS 2023
paper remain valuable references for the graph-based resolution algorithm.
We may vendor specific utility code if useful.

### Medium Risk: Code Transformation Correctness

Byte-range edits can produce syntactically invalid code if edits overlap or
if indentation isn't handled correctly.

**Mitigation**:
- Sort edits by position and apply in reverse order (last-to-first) to
  avoid offset invalidation.
- Tree-sitter's `edit()` API can update the tree after edits, allowing
  validation that the result still parses.
- The existing test suite covers edge cases (decorators, multiline
  expressions, nested classes, etc.).

### Low Risk: Pattern Matching Regression

The Rust pattern matcher (`matcher.rs`) already handles most patterns.
The remaining LibCST fallback cases are scope-local and imported-from
filtering, which move to the scope resolver.

### Low Risk: Performance Regression on Cold Start

The initial stack-graphs build is O(files) similar to today's MetadataWrapper
pass.  But stack-graphs persist their index, so cold starts only happen
once per project (or after `git pull` changes many files).

---

## Dependency Changes

### Removed
- `libcst` (pure Python, ~40K lines, significant import time)
- Pickle/zlib serialization of CST objects

### Added (Rust crate dependencies)
- `lru 0.12` (LRU cache for Rust)
- `rusqlite 0.31` (direct SQLite access from Rust for persistent scope index)

### Kept
- `tree-sitter 0.24` (already used)
- `tree-sitter-python 0.23` (already used)
- `rayon 1.10` (already used)
- `pyo3 0.25` (already used)
- `lark` (for selector/pattern grammar parsing -- could also be moved to Rust
  eventually, but low priority)

---

## What an AI Agent Session Looks Like

Here is how an agent (like me) would execute each phase:

### Phase 0 Session (~4 hours)

1. Add `lru` and `rusqlite` deps to `Cargo.toml`
2. Implement Python scope model in Rust: `Scope`, `Binding`, `ScopeTree`
3. Implement tree-sitter CST walker that builds scope trees from Python files
   (handle function/class/comprehension/module scopes, global/nonlocal,
   assignments, imports)
4. Implement QN resolver: walk scope tree upward from each identifier to
   find binding, construct qualified name from module path + binding chain
5. Implement import resolution: parse import statements, resolve dotted
   module paths against the project file tree
6. Write PyO3 wrapper exposing `ScopeResolver` to Python
7. Write a comparison harness: for every `.py` file in `tests/`, parse with
   both LibCST QualifiedNameProvider and the Rust resolver, diff QN sets
8. Fix discrepancies iteratively (likely: star imports, `__all__`, conditional
   imports, class scope non-closure, comprehension variable scoping)
9. Run `make test` -- fix any regressions

### Phase 1 Session (~3 hours)

1. Implement `ReferenceEngine::find_references` in Rust
2. Wire it into `transform.py::find_references()` as an alternative to
   `visit_project` + `_ReferenceFinder`
3. Run `test_find_references_context.py` -- fix failures
4. Repeat for `find_callers`, `find_dead_code`
5. Run full test suite -- fix regressions
6. Add feature flag (`EMEND_USE_RUST_SCOPE=1`) so both paths coexist during
   migration

### Phase 2 Session (~3 hours)

1. Implement `FileTransform` in Rust with `replace_node`, `insert_before`,
   `insert_after`, `remove_node`, `apply`
2. Wire into `PatternReplacer` -- replace `node.with_changes(...)` calls with
   byte-range edits
3. Run `test_transform.py`, `test_edit.py`, `test_add_parameter.py` -- fix
   failures
4. Migrate `ComponentSetter`, `ComponentAdder`, `ComponentRemover`,
   `SymbolRemover`
5. Run full test suite

### Phase 3 Session (~2 hours)

1. Remove all `import libcst` statements
2. Delete `_cached_parse`, `_disk_cache_get/put` (parse cache), `_QNCollector`,
   `_RefIndexCollector`
3. Simplify `visit_project` to use `ScopeResolver` index
4. Run full test suite -- fix any remaining issues
5. Remove `libcst` from `pyproject.toml`

---

## Metrics for Success

| Metric | Current | Target |
|---|---|---|
| `rename` on 500-file project (warm) | ~1.7s | <300ms |
| `refs` on 500-file project (warm) | ~1.5s | <200ms |
| `deadcode` on 500-file project | ~3s | <500ms |
| `find` pattern (Rust fast path coverage) | ~70% of patterns | 95%+ |
| `search --output summary` | Already Rust | No change |
| Parse cache entry size | ~50KB | ~5KB |
| `import emend` time | ~800ms (LibCST import) | ~200ms |
| Languages supported | Python only | Python + TypeScript + Go (Phase 4) |
| Lines of Rust | 2,487 | ~8,000-10,000 |
| Lines of Python removed | ~3,000 (LibCST-specific) | -- |
| External Python deps removed | `libcst` | -- |

---

## Appendix A: Inventory of LibCST Visitors to Replace

### CSTVisitor subclasses (read-only traversal)

| Class | File | Purpose | Replacement |
|---|---|---|---|
| `_QNCollector` | transform.py:361 | Collect all qualified names in a file | Stack-graphs index |
| `_RefIndexCollector` | transform.py:387 | Collect reference index entries | Stack-graphs index |
| `SymbolFinder` | transform.py:2229 | Find a specific symbol definition | Tree-sitter node query |
| `PatternFinder` | transform.py:3880 | Find pattern matches (basic) | Rust `matcher.rs` |
| `ConstrainedPatternFinder` | transform.py:4011 | Pattern match with inside/not-inside | Rust `matcher.rs` |
| `ScopedPatternFinder` | transform.py:4109 | Pattern match within scope | Rust `matcher.rs` + scope resolver |
| `_ImportOriginCollector` | transform.py:4194 | Collect import origins for filtering | Stack-graphs import resolution |
| `_NameCollector` | transform.py:5287 | Collect all Name nodes | Tree-sitter identifier query |
| `_ReferenceFinder` | transform.py:5684 | Scope-aware reference finding | `ReferenceEngine::find_references` |
| `_CallerFilter` | transform.py:6079 | Find call sites | `ReferenceEngine::find_callers` |
| `_CalleeCollector` | transform.py:6204 | Find called functions | Already in Rust (`collect_callees`) |
| `_BulkReferenceFinder` | transform.py:6468 | Bulk dead code reference scan | `ReferenceEngine::find_dead_code` |
| `_NestedDefinitionVisitor` | ast_utils.py:11 | Find nested definitions | Already in Rust (`symbols.rs`) |
| `_NameLoadCollector` | ast_commands.py:173 | Collect loaded names | Already in Rust (`symbols.rs`) |
| `_ListSymbolsVisitor` | ast_commands.py:198 | List symbols for summary | Already in Rust (`symbols.rs`) |
| `_SymbolCollector` | query.py:218 | Collect symbols with query info | Extend `symbols.rs` |
| `_StatementRangeMapper` | lint.py:85 | Map statements to line ranges | Tree-sitter statement ranges |
| `_SymbolCollector` | type_oracle.py:227 | Collect symbols for type inference | Extend `symbols.rs` |

### CSTTransformer subclasses (code modification)

| Class | File | Purpose | Replacement |
|---|---|---|---|
| `ComponentSetter` | transform.py:2640 | Edit symbol components | `FileTransform::replace_node` |
| `ComponentAdder` | transform.py:2963 | Add to list components | `FileTransform::insert_after` |
| `ComponentRemover` | transform.py:3381 | Remove components | `FileTransform::remove_node` |
| `PatternReplacer` | transform.py:4585 | Replace pattern matches | Rust match + `FileTransform` |
| `SymbolRemover` | transform.py:5139 | Remove entire symbol | `FileTransform::remove_node` |
| `_SymbolRenamer` | transform.py:5626 | Scope-aware rename | `ReferenceEngine::rename_symbol` |
| `_DocstringRenamer` | transform.py:5844 | Rename in docstrings | Regex on byte ranges |
| `ImportRewriter` | transform.py:7076 | Rewrite imports after move | `FileTransform` on import nodes |
| `_NoOpTransformer` | transform.py:7260 | No-op (for MetadataWrapper) | Removed |
| `_ImportRewriterForMove` | transform.py:7265 | Move-specific import rewrite | `FileTransform` on import nodes |
| `_ModuleImportRenamer` | transform.py:7298 | Rename module in imports | `FileTransform` on import nodes |

---

## Appendix B: Tree-sitter Ecosystem for Future Languages

| Language | Grammar | Stack-Graph Rules | Status |
|---|---|---|---|
| Python | `tree-sitter-python` 0.23 | `tree-sitter-stack-graphs-python` | Production (GitHub) |
| TypeScript | `tree-sitter-typescript` 0.23 | `tree-sitter-stack-graphs-typescript` | Production (GitHub) |
| JavaScript | `tree-sitter-javascript` 0.23 | `tree-sitter-stack-graphs-javascript` | Production (GitHub) |
| Go | `tree-sitter-go` 0.23 | Community `.tsg` rules | Usable |
| Rust | `tree-sitter-rust` 0.23 | `tree-sitter-stack-graphs-rust` | In development |
| Java | `tree-sitter-java` 0.23 | Community `.tsg` rules | Usable |
| C/C++ | `tree-sitter-c` / `tree-sitter-cpp` | Limited | Partial |
| Ruby | `tree-sitter-ruby` 0.23 | `tree-sitter-stack-graphs-ruby` | In development |

---

## Appendix C: Honest Assessment -- What Tree-sitter Can't Do

Tree-sitter was designed for editors, not code transformation.  Let's be
explicit about the limitations and how we work around them:

1. **Immutable trees**: Tree-sitter trees cannot be mutated.  There is no
   `node.with_changes(...)` equivalent.  Our answer is byte-range edits on
   the original source text, which is actually *simpler* and *faster* than
   LibCST's immutable-tree-copy model.

2. **No code generator**: Tree-sitter has no unparser.  You can't build a
   new tree node and ask "what Python source would produce this?"  Our answer:
   for replacements, we build the replacement string in Python (template
   interpolation with captured byte ranges), not by constructing tree nodes.
   This is what the current `PatternReplacer._do_replacement` already does --
   it builds a replacement string and calls `cst.parse_expression()` on it.

3. **No built-in scope analysis**: Covered above -- we build our own.

4. **Error recovery is imperfect**: Tree-sitter produces `ERROR` nodes for
   invalid syntax, and sometimes a single missing character can collapse a
   large subtree.  **This doesn't affect emend**: we only operate on valid,
   committed Python files.  LibCST also requires valid syntax.

5. **No Python-specific semantic understanding**: Tree-sitter doesn't know
   that `__init__` is a constructor or that `@property` creates a descriptor.
   Our answer: this semantic knowledge lives in our Rust code, not in the
   parser.  The current LibCST code also handles these cases with explicit
   Python logic, not parser magic.

## Appendix D: Alternative Considered -- Hybrid Approach (Keep LibCST for Transforms)

An alternative is to keep LibCST for code transformation only (Phase 2) and
replace everything else with tree-sitter + stack-graphs.  This would:

- Still achieve ~5x speedup (MetadataWrapper elimination)
- Avoid the byte-range edit engine work
- Keep the `libcst` dependency

**Why we rejected this**: The goal is multi-language support.  Keeping LibCST
means keeping a Python-only code transformation engine, which blocks language
expansion.  The byte-range edit engine is straightforward to implement (~500
lines of Rust) and provides a better foundation.

---

## Appendix E: The Parse Cache Problem in Detail

Today's parse cache is one of the most expensive components despite being
designed for speed:

```
Current flow:
  source text
    -> md5 hash (fast)
    -> check in-memory dict (fast)
    -> miss: check SQLite (fast)
    -> miss: cst.parse_module() (~10ms)
    -> pickle.dumps(module) (~5ms)
    -> zlib.compress(pickled) (~3ms)
    -> SQLite INSERT (~1ms)
    -> next access: SQLite SELECT (~1ms)
    -> zlib.decompress (~2ms)
    -> pickle.loads (~8ms)
```

Total cost for a cache **hit**: ~11ms (SQLite + decompress + unpickle).
Total cost for a cache **miss**: ~29ms (parse + serialize + store + read-back).

With tree-sitter:

```
Proposed flow:
  source text
    -> md5 hash (fast)
    -> check in-memory LRU (fast)
    -> miss: tree_sitter::Parser::parse() (~1ms)
    -> (optional: store compact tree bytes ~0.5ms)
    -> next access: in-memory hit (~0.001ms)
```

Total cost for a cache **hit**: ~0.001ms (memory lookup).
Total cost for a cache **miss**: ~1ms (parse).

The 10,000x speedup on cache hits comes from:
1. Tree-sitter parses 10x faster than LibCST
2. No serialization/deserialization overhead
3. No compression overhead
4. Tree-sitter trees are small enough to keep many more in memory
