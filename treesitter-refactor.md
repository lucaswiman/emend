# Proposal: Replace LibCST with Tree-sitter Tooling

## Executive Summary

Emend currently uses LibCST (a pure-Python concrete syntax tree library) as its
primary AST engine, with a Rust+tree-sitter extension (`emend_core`) handling
performance-critical fast paths.  This proposal evaluates three approaches for
migrating away from LibCST entirely:

1. **GritQL** -- use the GritQL engine as a pattern matching / rewrite IR
2. **ast-grep** -- use ast-grep's `ast_grep_core` Rust crate for pattern matching
3. **Custom Rust** -- build everything from scratch on raw tree-sitter (the
   existing proposal)

**Recommendation**: Use **GritQL crates** (`grit-pattern-matcher`, `grit-util`,
and `marzano-language`) for pattern matching and code rewriting, plus a **custom
scope resolver** in Rust for qualified name resolution, plus a **language config
file** for cross-language scoping rules.  This combines the maturity of GritQL's
pattern/rewrite engine with the semantic depth that emend requires.

### Why GritQL over ast-grep or custom Rust?

| Criterion | GritQL | ast-grep | Custom Rust |
|-----------|--------|----------|-------------|
| Pattern matching maturity | Production (4.4k stars, MIT, used by Biome) | Production (very active) | Partial (1,309 LOC in matcher.rs) |
| Rewrite engine | Built-in (`=>` operator), declarative | Built-in ("find & patch") | Must build (~500 LOC) |
| Syntax alignment with emend | Very close (`$X` metavars, `where` clauses) | Similar but YAML-heavy for complex rules | N/A (custom) |
| Multi-language support | 12 languages via tree-sitter grammars | 20+ languages via tree-sitter | Must add per-language |
| Rust crate availability | `grit-pattern-matcher` on crates.io | `ast_grep_core` on crates.io (unstable API) | N/A |
| Scope-aware operations | `imported_from` predicate, `multifile` mode | None | Must build |
| Cross-file patterns | `multifile` + `sequential` built-in | Single-file only (CLI can iterate) | Must build |
| IR for edits | Yes (pattern → rewrite → byte-range edit) | Yes (pattern → fix → edit) | Must design |
| License | MIT | MIT | N/A |
| Documentation | 5.3% of crate documented | Unstable API warning | N/A |

**Key insight**: GritQL's syntax (`$metavar`, `where` clauses, `=>` rewrites,
`within`/`contains` navigation, `imported_from` predicate) is very close to
what emend already has.  We can make emend's pattern syntax compile down to
GritQL as an IR, getting the entire rewrite engine for free while keeping
emend's user-facing syntax stable.

Neither GritQL nor ast-grep provides true scope-aware qualified name resolution
-- both operate at the syntactic/AST level.  Emend needs QualifiedNameProvider
semantics for `find-references`, `rename`, and `dead-code`.  So we must build
the scope resolver regardless.  The question is only: do we also build the
pattern matcher and rewrite engine, or reuse existing crates?

---

## Tool Analysis

### GritQL Deep Dive

**Architecture**: GritQL is a ~15-crate Rust workspace built on tree-sitter.
The key crates are:

| Crate | Published? | Purpose |
|-------|-----------|---------|
| `grit-pattern-matcher` | crates.io v0.5.1 | Core `Matcher` trait + pattern IR |
| `grit-util` | crates.io v0.5.1 | Utilities (no grit deps) |
| `marzano-language` | Workspace only | Language definitions (trait impls per language) |
| `marzano-core` | Workspace only | Main engine: compile GritQL → IR → execute |
| `cli` / `cli_bin` | Workspace only | CLI frontend |

**Syntax alignment with emend**:

| Emend | GritQL | Notes |
|-------|--------|-------|
| `$X` | `$x` | Same metavar concept, different convention |
| `$...ARGS` | `$...` (spread) | GritQL uses unnamed spread |
| `--inside` | `within` | Same semantics |
| `--not-inside` | `not within` | Same semantics |
| `--where` | `where { }` | GritQL's is more expressive |
| `find ... replace` | `` `pattern` => `replacement` `` | GritQL's `=>` is elegant |
| `:type[X]` | No equivalent | Need custom extension |
| `file.py::Sym` | No equivalent | Emend selector syntax is richer |
| `--imported-from` | `imported_from()` predicate | GritQL has this! |
| `--scope-local` | `bubble` | Similar scoping concept |
| `search --output summary` | No equivalent | Emend-specific |

**Rewrite mechanism**: GritQL's rewrite works as:
1. Parse GritQL pattern into pattern IR (tree of `Matcher`-implementing nodes)
2. Match pattern against tree-sitter AST, collecting metavar bindings
3. Substitute metavar bindings into replacement template
4. Apply as byte-range edits on original source

This is exactly the flow emend needs.  The key difference: GritQL expresses
this declaratively (`` `old` => `new` ``) while emend uses CLI flags
(`emend replace 'old' 'new'`).

**Cross-file**: GritQL's `multifile` mode allows gathering info from one file
and applying transforms across others.  The `imported_from(from=includes
$source_file)` predicate tracks cross-file imports -- not full scope resolution,
but useful for many rename/move operations.

**Limitations for emend**:
- No qualified name resolution (no scope graph / symbol table)
- No dead code detection
- No call graph analysis
- The `marzano-language` and `marzano-core` crates are NOT published to
  crates.io -- we'd need to vendor them or use git dependencies
- Only 5.3% API documentation coverage
- Pattern-level operations only -- no symbol-level operations (edit component,
  add parameter, etc.)

### ast-grep Deep Dive

**Architecture**: ast-grep is a standalone Rust CLI tool with a core library
(`ast_grep_core`) on crates.io.

**Key crates**:

| Crate | Purpose |
|-------|---------|
| `ast-grep-core` | Pattern matching, `Matcher` trait, `Pattern`, `NodeMatch` |
| `ast-grep-language` | Language definitions |
| `ast-grep-config` | YAML rule system |
| `ast-grep-py` | Python bindings via PyO3 |

**Strengths**:
- Very active development (more commits than GritQL)
- Used by Netflix, Shopify
- Built-in fix/rewrite from day one
- Code-snippet patterns ("pattern is code") -- intuitive
- Python bindings exist

**Limitations for emend**:
- **Unstable Rust API** -- docs explicitly warn against depending on it
- **No scope analysis at all** -- purely syntactic
- **No cross-file coordination** -- operates file-by-file
- **YAML-heavy for complex rules** -- less elegant than GritQL for conditions
- **No `imported_from` equivalent** -- can't verify import chains

### Custom Rust (existing proposal) Assessment

The existing proposal is sound but underestimates the effort for the pattern
matcher and rewrite engine (~3,000 additional LOC on top of the existing 1,309
in `matcher.rs`).  The scope resolver estimate (~2,000-3,000 LOC) is accurate.

**What we keep from the existing proposal**:
- Custom scope resolver in Rust (Section 2: `scope.rs`)
- Byte-range edit engine (Section 3: `transform.rs`)
- Performance analysis and migration strategy structure

**What we replace**:
- Pattern IR and matcher (Section 4) → use GritQL crates
- Pattern compilation → compile emend patterns to GritQL IR

---

## Recommended Architecture

### Layer Diagram

```
                    Python CLI (cli.py)
                         |
                 Python API layer (thin)
                         |
            +------------+-------------+
            |            |             |
       emend_core     emend_core    type_oracle.py
       (patterns)     (scope)       (LSP adapters)
            |            |
    +-------+----+   +---+---+
    |            |   |       |
  GritQL      byte  scope   lang
  pattern     range resolver config
  matcher     edits (custom) (TOML)
  (vendored)  (custom)
```

### Core Rust Modules

#### 1. Pattern Engine (vendored GritQL crates)

Vendor `grit-pattern-matcher`, `grit-util`, and the Python language definition
from `marzano-language` into `emend_core`.  This gives us:

- The `Matcher` trait and full pattern IR (metavars, spread, conditions)
- Pattern compilation from code snippets → tree-sitter match
- Replacement template instantiation with metavar substitution
- `within` / `contains` / `not` combinators

**Emend pattern compilation pipeline**:

```
Emend pattern string         "$X.method($...ARGS)"
        |
        v
  Lark parser (pattern.lark)     -- existing
        |
        v
  Emend Pattern AST              -- existing
        |
        v
  GritQL pattern IR              -- NEW: compile to grit-pattern-matcher types
        |
        v
  tree-sitter match execution    -- provided by GritQL crate
        |
        v
  Match results with captures    -- byte ranges + metavar bindings
```

For the `find ... replace` command:

```
emend replace '$X.old_method($A)' '$X.new_method($A, extra=True)'
        |
        v
  Compile both patterns to GritQL IR
        |
        v
  GritQL rewrite: pattern => replacement with metavar substitution
        |
        v
  Byte-range edits on original source
```

**What this replaces**:
- `compile_pattern_to_matcher()` (pattern.py:1038) → GritQL IR compilation
- `compile_pattern_to_rust_ir()` (pattern.py:1486) → GritQL IR compilation
- `PatternFinder` (transform.py:3880) → GritQL match execution
- `ConstrainedPatternFinder` (transform.py:4011) → GritQL `within`/`not`
- `ScopedPatternFinder` (transform.py:4109) → GritQL + scope resolver
- `PatternReplacer` (transform.py:4585) → GritQL rewrite
- `matcher.rs` (1,309 LOC) → replaced by vendored GritQL matcher
- The entire LibCST `matchers` dependency

#### 2. Scope Resolver (`scope.rs` -- custom, ~2,500 LOC)

This is unchanged from the existing proposal.  Neither GritQL nor ast-grep
provides this.  We build a custom Python scope resolver in Rust that:

- Walks tree-sitter CST to build scope tree + binding table
- Handles Python scoping: function, class (non-closure), comprehension,
  global/nonlocal, conditional imports, `__all__`
- Builds import graph for cross-file resolution
- Maintains persistent QN index (keyed by content hash)
- Exposes `qualified_names(file)`, `find_references(qn)`,
  `goto_definition(file, pos)`, `find_dead_code(opts)`

**Cross-language generalization** (see Section below): The scope resolver is
parameterized by a language config file that defines scoping rules, making it
possible to add TypeScript, Go, etc. without modifying Rust code.

```rust
pub struct ScopeResolver {
    config: LanguageConfig,       // loaded from TOML
    file_scopes: HashMap<ContentHash, FileScope>,
    import_graph: ImportGraph,
    qn_index: HashMap<String, Vec<Location>>,
}
```

#### 3. Transform Engine (`transform.rs` -- custom, ~800 LOC)

Byte-range edit engine, extended from the existing proposal to integrate with
GritQL match results:

```rust
pub struct FileTransform {
    source: String,
    edits: Vec<Edit>,
}

impl FileTransform {
    /// From a GritQL match result, apply the rewrite.
    pub fn apply_grit_rewrite(&mut self, match_result: &MatchResult);

    /// Direct byte-range operations for symbol-level edits.
    pub fn replace_range(&mut self, start: usize, end: usize, text: &str);
    pub fn insert_before(&mut self, pos: usize, text: &str);
    pub fn insert_after(&mut self, pos: usize, text: &str);
    pub fn remove_range(&mut self, start: usize, end: usize);

    pub fn apply(self) -> String;
}
```

The direct byte-range operations handle emend-specific operations that don't
map to GritQL patterns:
- `edit` command (modify symbol components by selector)
- `add` command (insert into list components)
- `copy-to` / `move` (whole-symbol operations)
- `rename` (scope-aware, uses QN resolver)

#### 4. Language Config (`languages/*.toml` -- new)

A TOML config file per language that defines scoping rules for the scope
resolver.  This enables cross-language qualified name resolution without
modifying Rust code.

```toml
# languages/python.toml
[language]
name = "python"
tree_sitter_grammar = "tree-sitter-python"
file_extensions = ["py", "pyi"]

[scoping]
# Node types that create new scopes
scope_creators = [
    { node = "function_definition", kind = "function" },
    { node = "class_definition", kind = "class" },
    { node = "lambda", kind = "function" },
    { node = "list_comprehension", kind = "comprehension" },
    { node = "set_comprehension", kind = "comprehension" },
    { node = "dictionary_comprehension", kind = "comprehension" },
    { node = "generator_expression", kind = "comprehension" },
]

[scoping.class]
# Python-specific: class scope does NOT participate in closure
is_closure_boundary = true
names_visible_to_inner = false

[scoping.comprehension]
# Only iteration variables are scoped, not the iterable
scoped_children = ["for_in_clause.left"]

[scoping.declarations]
# Keywords that modify binding scope
global_keyword = "global_statement"
nonlocal_keyword = "nonlocal_statement"

[bindings]
# How names are bound in this language
assignment_nodes = ["assignment", "augmented_assignment"]
target_field = "left"
for_binding = "for_in_clause.left"
with_binding = "with_clause.alias"
except_binding = "except_clause.name"
# Function params are bindings in the function scope
param_nodes = ["parameters.identifier", "default_parameter.name",
               "typed_parameter.name", "typed_default_parameter.name"]

[imports]
# Import statement structure
import_statement = "import_statement"
import_from = "import_from_statement"
module_field = "module_name"
name_field = "name"
alias_field = "alias"
star_import = "wildcard_import"
# How to resolve module paths
resolution = "python"  # Built-in: file-based with src/ detection

[qualified_names]
# How to construct QNs from bindings
module_separator = "."
class_member_prefix = true  # module.Class.method
nested_function_prefix = true  # module.outer.<locals>.inner

[exports]
# What counts as a public export
all_variable = "__all__"
public_by_default = true
private_prefix = "_"
```

```toml
# languages/typescript.toml
[language]
name = "typescript"
tree_sitter_grammar = "tree-sitter-typescript"
file_extensions = ["ts", "tsx"]

[scoping]
scope_creators = [
    { node = "function_declaration", kind = "function" },
    { node = "arrow_function", kind = "function" },
    { node = "class_declaration", kind = "class" },
    { node = "method_definition", kind = "function" },
    { node = "for_statement", kind = "block" },
    { node = "for_in_statement", kind = "block" },
]

[scoping.class]
is_closure_boundary = false
names_visible_to_inner = true  # TypeScript classes DO participate in closure

[scoping.declarations]
# var is function-scoped, let/const are block-scoped
var_declaration = { node = "variable_declaration", scope = "function" }
let_declaration = { node = "lexical_declaration", scope = "block" }

[bindings]
assignment_nodes = ["assignment_expression", "variable_declarator"]
target_field = "left"
# Destructuring adds complexity
destructuring_nodes = ["object_pattern", "array_pattern"]

[imports]
import_statement = "import_statement"
module_field = "source"
name_field = "import_clause"
resolution = "node"  # node_modules resolution

[qualified_names]
module_separator = "/"
class_member_prefix = true

[exports]
export_statement = "export_statement"
default_export = "export_default_declaration"
public_by_default = false
```

The scope resolver reads this config at initialization and uses it to:
1. Walk tree-sitter nodes, creating scopes when it encounters `scope_creators`
2. Bind names according to `bindings` rules
3. Resolve imports using the language-specific `resolution` strategy
4. Construct qualified names using `qualified_names` conventions

**Python-specific resolution strategies** (like `importlib` semantics, `src/`
layout detection) are implemented as named strategies in Rust that the config
selects.  Adding a new strategy (e.g., `"node"` for Node.js resolution) is a
Rust code change, but the scoping rules themselves are data-driven.

---

## Mapping Emend Commands to New Architecture

| Command | Current Implementation | New Implementation |
|---------|----------------------|-------------------|
| `search` (pattern mode) | LibCST matchers + Rust fast path | GritQL pattern matcher (all patterns) |
| `search` (symbol mode) | `_SymbolCollector` (LibCST) | `symbols.rs` (existing Rust) |
| `search --output summary` | `_ListSymbolsVisitor` → already Rust | No change |
| `replace` | `PatternReplacer` (LibCST CSTTransformer) | GritQL rewrite (`=>`) + byte-range edits |
| `edit` | `ComponentSetter` (LibCST CSTTransformer) | tree-sitter node lookup + byte-range edits |
| `add` | `ComponentAdder` (LibCST CSTTransformer) | tree-sitter node lookup + byte-range insert |
| `refs` | `_ReferenceFinder` + MetadataWrapper | Scope resolver `find_references()` |
| `rename` | `_SymbolRenamer` + MetadataWrapper | Scope resolver + byte-range edits |
| `deadcode` | `_BulkReferenceFinder` + MetadataWrapper | Scope resolver `find_dead_code()` |
| `graph` | `_CallerFilter` + `_CalleeCollector` | Scope resolver + `collect_callees` (existing Rust) |
| `move` / `copy-to` | `ImportRewriter` + `SymbolRemover` | Scope resolver + byte-range edits |
| `lint` | LibCST matchers + `_StatementRangeMapper` | GritQL pattern matcher + tree-sitter ranges |
| `lint --fix` | `PatternReplacer` | GritQL rewrite |

---

## Cross-Language Qualified Names Design

The language config approach (Section 4 above) enables cross-language scope
resolution without Rust code changes for most languages.  Here's how it works:

### Generic Algorithm

```
resolve_qualified_name(file, position):
    config = load_language_config(file.extension)
    tree = parse(file)
    scopes = build_scope_tree(tree, config.scoping)
    bindings = collect_bindings(tree, config.bindings)
    imports = collect_imports(tree, config.imports)

    # Find the identifier at position
    node = find_node_at(tree, position)
    name = node.text

    # Walk scope tree upward to find binding
    scope = innermost_scope_at(scopes, position)
    while scope:
        if (scope.id, name) in bindings:
            binding = bindings[(scope.id, name)]
            return construct_qn(file, binding, config.qualified_names)
        if config.scoping[scope.kind].is_closure_boundary:
            break  # e.g., class scope in Python
        scope = scope.parent

    # Check imports
    if name in imports:
        return resolve_import(imports[name], config.imports.resolution)

    # Check builtins
    return None  # unresolved
```

### Language-Specific Resolution Strategies

The config's `[imports] resolution = "python"` selects a built-in strategy.
Initial strategies:

| Strategy | Languages | Algorithm |
|----------|-----------|-----------|
| `python` | Python | `importlib` semantics: `sys.path` search, `src/` layout, `__init__.py`, relative imports |
| `node` | JS/TS | `node_modules` resolution: `package.json` main/exports, index.js, `.ts`→`.js` mapping |
| `go` | Go | Module path from `go.mod`, package = directory |
| `rust` | Rust | `mod` declarations + `use` paths, crate root from `Cargo.toml` |

Each strategy is ~200-400 LOC of Rust.  The scoping rules themselves (scope
creation, binding, closure boundaries) are fully config-driven.

### What This Enables

With a language config + resolution strategy, emend can:
- Find references across a TypeScript project
- Rename a Go function across all callers
- Detect dead code in a Rust crate
- Move a Python class to another module and update imports

All using the same scope resolver infrastructure, parameterized by config.

---

## Migration Strategy

### Phase 0: Vendor GritQL Crates + Build Integration Layer (Week 1-2)

1. **Vendor** `grit-pattern-matcher`, `grit-util`, and the Python language
   definition from `marzano-language` into `emend_core/vendor/`
2. **Build** a bridge: `EmendPattern` → `GritPattern` compilation
3. **Wire** into `find_pattern_in_files`: replace current `matcher.rs` IR with
   GritQL pattern IR
4. **Test**: Run `test_find.py`, `test_pattern.py`, `test_transform.py` --
   verify identical results
5. **Benchmark**: Compare pattern matching speed with current Rust + LibCST paths

**Validation**: All 265 test files pass with GritQL pattern engine.

### Phase 1: Build Scope Resolver (Week 2-4)

1. **Implement** `scope.rs`: scope tree, binding table, import table
2. **Implement** Python language config (`languages/python.toml`)
3. **Implement** `python` import resolution strategy
4. **Build comparison harness**: for every `.py` file in `tests/`, compare
   LibCST QualifiedNameProvider output with Rust scope resolver output
5. **Fix discrepancies** (likely: star imports, `__all__`, conditional imports,
   walrus operator, comprehension variable leaking)
6. **Wire** into `find_references`, `find_callers`, `find_dead_code`
7. **Feature flag**: `EMEND_USE_RUST_SCOPE=1` to toggle

### Phase 2: Build Rewrite Engine + Migrate Transforms (Week 4-6)

1. **Implement** `transform.rs`: byte-range edit engine
2. **Wire** GritQL rewrite output → `FileTransform`
3. **Migrate** `replace` command: GritQL rewrite replaces `PatternReplacer`
4. **Migrate** `edit`/`add` commands: tree-sitter node lookup + byte-range edits
   replace `ComponentSetter`/`ComponentAdder`/`ComponentRemover`
5. **Migrate** `rename`: scope resolver + byte-range edits replace `_SymbolRenamer`
6. **Migrate** `move`/`copy-to`: scope resolver + byte-range edits
7. **Run full test suite at each step**

### Phase 3: Remove LibCST (Week 6-7)

1. Remove all `import libcst` statements
2. Delete LibCST-specific code: `_cached_parse`, visitor base classes,
   `compile_pattern_to_matcher`, `_NoOpTransformer`
3. Remove `libcst` from `pyproject.toml` dependencies
4. Run full test suite
5. Benchmark: measure end-to-end speedup

### Phase 4: Add Language Config + Second Language (Week 7-9)

1. **Finalize** the language config TOML schema
2. **Refactor** scope resolver to be config-driven
3. **Add TypeScript config** (`languages/typescript.toml`)
4. **Implement `node` resolution strategy** for JS/TS imports
5. **Test** basic TypeScript operations: `search`, `refs`, `rename`
6. **Add tree-sitter-typescript** grammar dependency

### Phase 1: Pattern Engine Expansion (COMPLETED)

Expanded the Rust structural matcher and Python IR to handle nearly all Python
expression and statement types, reducing LibCST fallback to <10% of patterns.

#### Rust Extension Enhancements (`emend_core`)

**`matcher.rs`** — Significant expansion of `PatternNode` and matching logic:

| Change | Purpose |
|--------|---------|
| `AugAssign` / `AnnAssign` variants | Support for augmented (`+=`) and annotated (`x: int`) assignments |
| `Comprehension` / `DictComprehension` variants | Support for list/set/dict comprehensions and generator expressions |
| `FString` variant + `FStringPart` enum | Support for f-strings matching `string_content` and `interpolation` |
| `decorators` in `FuncDef` / `ClassDef` | Support for matching decorated functions and classes |
| `Star` / `DoubleStar` in `ArgPattern` | Support for `*args` and `**kwargs` in calls |
| `TypeConstraint` variant | Support for `:int`, `:str`, `:call` filters directly in Rust |
| `match_sequence()` helper | Generic subsequence matching with `Ellipsis` support |
| `match_generator()` helper | Matching for `for...in...if` clauses in comprehensions |
| `match_fstring()` helper | Structural matching for f-string parts |

#### Python File Changes

**`pattern.py`** — Updated `_cst_to_rust_ir()`:
- Implemented IR generation for all new Rust matcher variants.
- Mapped LibCST nodes (`Assign`, `AugAssign`, `AnnAssign`, `ListComp`, etc.) to Rust-compatible dicts.
- Enabled decorator support in `FunctionDef` and `ClassDef` IR.
- Handled `StarredElement` and `keyword` arguments in `Call` IR.

#### Key Fixes Applied

7. **Subsequence Ellipsis Matching**: Improved the generic `match_sequence`
   to handle multiple ellipsis and complex windows, ensuring patterns like
   `func($...ARGS, last_arg)` match correctly in Rust.

8. **Decorated Definition Handling**: The Rust matcher now correctly unwraps
   `decorated_definition` nodes to match either the decorators or the
   underlying `function_definition` / `class_definition`.

---

## Performance Expectations

| Operation | Current (LibCST) | Expected (GritQL + Rust scope) |
|-----------|------------------|-------------------------------|
| Pattern search (500 files) | ~400ms (Rust fast path) / ~2s (LibCST fallback) | ~400ms (all via GritQL, no fallback) |
| `rename` (500 files, warm) | ~1.7s (MetadataWrapper bottleneck) | ~250ms (scope index lookup + edits) |
| `refs` (500 files, warm) | ~1.5s | ~150ms |
| `deadcode` (500 files) | ~3s | ~400ms |
| `replace` pattern (500 files) | ~800ms | ~300ms |
| Parse cache hit | ~11ms (SQLite + zlib + pickle) | ~0.001ms (in-memory tree-sitter) |
| `import emend` time | ~800ms (LibCST import) | ~200ms |

The biggest win is eliminating MetadataWrapper (50-200ms per file), which
dominates all cross-project operations.

---

## Risk Analysis

### High Risk: GritQL Crate Stability

The `grit-pattern-matcher` and `grit-util` crates haven't been updated in
~12 months.  The core engine crates (`marzano-core`, `marzano-language`) are
not published to crates.io.

**Mitigation**:
- Vendor the code (MIT license) rather than depending on crates.io releases
- The pattern matcher is well-defined: `Matcher` trait + pattern IR.  If GritQL
  stagnates, we own the vendored code and can evolve it.
- The vendored code is likely ~5,000 LOC -- manageable to maintain.
- Alternatively: evaluate Biome's fork of GritQL (`biomejs/gritql`) which may
  be more actively maintained.

### High Risk: Scope Resolver Fidelity

Unchanged from existing proposal.  This is the hardest part regardless of
which pattern matcher we use.

**Mitigation**: Comparison harness, extensive test suite, feature flag for
gradual rollout.

### Medium Risk: GritQL Pattern Coverage

GritQL may not support all of emend's pattern constructs (e.g., `:type[X]`
oracle constraints, `:call` type filters, glob identifiers like `test_*`).

**Mitigation**:
- `:type[X]` / `:returns[X]` → post-filter using type oracle (same as today)
- `:call` / `:str` / `:int` → tree-sitter node type filter (simple to add)
- `test_*` glob → regex pattern in GritQL (`` r"test_.*" ``)
- If a specific pattern is unsupported, add it to the vendored crate

### Low Risk: Byte-Range Edit Correctness

Same as existing proposal.  Well-understood problem, good test coverage.

---

## Dependency Changes

### Removed
- `libcst` (~40K lines, significant import time)
- Custom `matcher.rs` IR (1,309 LOC) -- replaced by GritQL crates

### Added (Rust, vendored)
- `grit-pattern-matcher` (vendored, MIT)
- `grit-util` (vendored, MIT)
- Python language support from `marzano-language` (vendored, MIT)
- `toml` (for language config parsing)

### Added (Rust, crates.io)
- `lru 0.12` (LRU cache)
- `rusqlite 0.31` (persistent scope index)
- `petgraph` (scope graph, optional)

### Kept
- `tree-sitter 0.24`
- `tree-sitter-python 0.23`
- `rayon 1.10`
- `pyo3 0.25`
- `memchr 2.7`
- `lark` (selector/pattern grammar)
- `typer`, `pyyaml`

---

## Appendix A: GritQL Syntax Mapping

How emend's pattern syntax maps to GritQL:

| Emend Pattern | GritQL Equivalent |
|---------------|-------------------|
| `func($X)` | `` `func($x)` `` |
| `$X.method($...ARGS)` | `` `$x.method($...args)` `` |
| `isinstance($X, str)` | `` `isinstance($x, str)` `` |
| `def $FUNC($...PARAMS): $...BODY` | `` `def $func($...params): $...body` `` |
| `class $CLS($...BASES): $...BODY` | `` `class $cls($...bases): $...body` `` |
| `$X = $Y` | `` `$x = $y` `` |
| `$X == $Y` | `` `$x == $y` `` |
| `[$...ITEMS]` | `` `[$...items]` `` |
| `{$KEY: $VALUE}` | `` `{$key: $value}` `` |

**Emend-specific extensions** (not in GritQL, need custom handling):
- `$X:type[Connection]` → post-filter with type oracle
- `$X:returns[Optional[str]]` → post-filter with type oracle
- `$X:call` → tree-sitter `node.kind() == "call"`
- `$X:str` → tree-sitter `node.kind() == "string"`
- `test_*` → GritQL `r"test_.*"` regex pattern
- `$KEY=$VALUE` (keyword arg) → GritQL `` `$key=$value` `` within argument context

## Appendix B: Inventory of LibCST Visitors to Replace

(Preserved from original proposal -- see classes in transform.py, query.py,
ast_utils.py, ast_commands.py, lint.py, type_oracle.py)

### CSTVisitor subclasses (18 total) → replaced by:
- GritQL pattern matcher (PatternFinder variants)
- Rust scope resolver (ReferenceFinder, CallerFilter, BulkReferenceFinder)
- Existing Rust `symbols.rs` (NestedDefinitionVisitor, ListSymbolsVisitor)

### CSTTransformer subclasses (11 total) → replaced by:
- GritQL rewrite engine (PatternReplacer)
- Byte-range edit engine (ComponentSetter, ComponentAdder, ComponentRemover,
  SymbolRemover, SymbolRenamer, ImportRewriter)

## Appendix C: Honest Assessment of GritQL Limitations

1. **Documentation is sparse**: Only 5.3% coverage.  We'll be reading source
   code more than docs.  But MIT license means we can.

2. **Core crates not published**: We must vendor, not depend.  This means
   tracking upstream changes manually.

3. **No true scope resolution**: GritQL's `imported_from` is pattern-based
   heuristic, not full scope analysis.  For correctness, we need our own.

4. **Last updated ~12 months ago**: The published crates may not track the
   latest tree-sitter versions.  We may need to update vendored code.

5. **Designed for CLI, not library**: GritQL's architecture is CLI-first.
   Extracting the pattern matcher as a library requires understanding the
   crate boundaries.  The `grit-pattern-matcher` crate is the cleanest
   extraction point.

6. **Biome fork complexity**: There are two forks (`getgrit/gritql` and
   `biomejs/gritql`).  Need to evaluate which is more actively maintained
   and which has better library ergonomics.

Despite these limitations, vendoring GritQL's pattern matcher is still
preferable to building our own from scratch:
- The `Matcher` trait and pattern IR are well-designed
- The metavar capture + replacement template system is exactly what we need
- The `within`/`contains`/`not` combinators match emend's `--inside`/`--not-inside`
- MIT license gives us full freedom

## Appendix D: ast-grep as Alternative

If GritQL vendoring proves too complex, ast-grep's `ast_grep_core` crate
(crates.io, MIT license) is a viable fallback:

**Pros**:
- On crates.io (easier dependency management)
- Very actively maintained (updated Jan 2026)
- Code-snippet patterns are natural
- Python bindings (`ast-grep-py`) exist

**Cons**:
- **API explicitly marked unstable** -- breaking changes expected
- No cross-file coordination at all
- No `imported_from` equivalent
- Complex conditions require YAML, not inline syntax
- Would still need our custom scope resolver

If we go this route, we'd use `ast_grep_core` only for pattern matching and
build everything else custom.  The net effort is similar to using GritQL but
with a less expressive pattern language.

---

## Appendix E: Implementation Progress

### Phase 0.5: Symbol Collection Migration (COMPLETED)

Migrated 5 of 7 LibCST-dependent Python files to use tree-sitter via the
Rust extension.  All 1,309 tests pass.

#### Rust Extension Enhancements (`emend_core`)

**`symbols.rs`** — Major enhancement to `collect_symbols_batch()` and new
functions:

| Change | Purpose |
|--------|---------|
| `decorator` strings + `decorator_line_start` fields | Decorator metadata for dead-code entry-point filtering |
| `method` / `async_method` kind distinction | Differentiate methods from standalone functions |
| `col_offset` field | Positional info for metadata output |
| `param_names` field | Parameter names for symbol info |
| `returns` field (separate from signature) | Return type annotation for oracle integration |
| `collect_symbols_from_str()` (new PyO3 function) | In-memory symbol collection without file I/O |
| `get_statement_ranges()` (new PyO3 function) | Statement line ranges for noqa mapping |
| `line_start` fix for decorated definitions | Report `def`/`class` line, not decorator line |

**`pattern.rs`** — New function:

| Change | Purpose |
|--------|---------|
| `collect_identifier_positions()` | Identifier/attribute positions for type oracle |

**`lib.rs`** — Registered 3 new PyO3 functions.

#### Python File Migrations

| File | Status | Changes |
|------|--------|---------|
| `ast_utils.py` | **Fully migrated** | Removed `_NestedDefinitionVisitor` (LibCST). Uses `emend_core.collect_symbols_from_str()`. Filters out module-level variables to match old behavior. |
| `query.py` | **Fully migrated** | Removed `_SymbolCollector` (LibCST). Added `_rust_dict_to_symbol_info_list()` and `_extract_params_from_signature()`. |
| `ast_commands.py` | **Fully migrated** | Removed `_ListSymbolsVisitor`, `_NameLoadCollector`, and LibCST helpers. Added `method`/`async_method` kind handling. |
| `lint.py` | **Fully migrated** | Removed `_StatementRangeMapper` (LibCST). Uses `emend_core.get_statement_ranges()`. Replaced lazy `import libcst` in `_process_file_fallback` with source-based text extraction using match position info. |
| `type_oracle.py` | **Fully migrated** | Removed `_SymbolCollector` (LibCST). Uses `emend_core.collect_identifier_positions()`. |
| `transform.py` | **Mostly migrated** | `_index_batch` and `visit_project` use `PyScopeResolver` for QN caching. `find_references` and `find_callers` migrated to tree-sitter using new `visit_project_ts()`. Deleted `_ReferenceFinder`, `_CallerFilter`, `_QNCollector`, `_RefIndexCollector`. |
| `pattern.py` | **Not started** | Still fully LibCST-dependent. |

#### Key Fixes Applied

1. **Decorated function `line_start`**: Rust now reports the `def`/`class`
   keyword line as `line`, not the first decorator line. `decorator_line_start`
   holds the decorator line. This fixed self-reference exclusion in dead-code
   detection.

2. **Dead-code variable filtering**: Added SQL condition to exclude `variable`
   kind from dead-code analysis, since module-level variables weren't tracked
   by the old LibCST backend and are often configs/constants.

3. **Module-level variable filtering in `find_nested_definitions()`**: Variables
   at depth 0 are filtered out to match old LibCST behavior (only function/class
   definitions). This fixed the line-selector test.

4. **Method/async_method kind handling**: Updated `_print_symbol_flat` and
   `_print_symbol_tree` to handle the new kinds.

### Phase 0.6: Scope Resolver Integration (COMPLETED)

Replaced `MetadataWrapper` in `_index_batch` with `PyScopeResolver` for QN
and reference indexing.  Removed last `import libcst` from `lint.py`.
All 1,309 tests pass.

#### Rust Extension Enhancements (`emend_core`)

**`scope.rs`** — Reference collection and resolution:

| Change | Purpose |
|--------|---------|
| `ReferenceKind::as_str()` method | String conversion for Python bindings |
| `FileScope.references` + `FileScope.all_qnames` fields | Store resolved references and QN strings per file |
| `collect_file_references()` (second-pass walk) | Resolve all identifiers/attributes to qualified names |
| `walk_references()` recursive walker | Walk tree-sitter nodes collecting references |
| `classify_reference()` | Classify references as read/write/call/import/definition based on parent context |
| `collect_dotted_name()` | Build full dotted name from attribute chains (e.g., `os.path.join`) |
| `resolve_identifier()` | Resolve simple name through scope chain + imports + builtins |
| `resolve_dotted_name()` | Resolve dotted attribute access through imports |
| `find_enclosing_scope()` | Find innermost scope by byte offset |
| `resolve_in_scope_chain()` | Walk scope chain upward respecting closure boundaries |
| `compute_qn_str()` | Compute QN string from scope chain (non-allocating variant) |
| `is_python_builtin()` | Recognize common Python builtins for QN resolution |
| 1-indexed line numbers in references | Convert tree-sitter 0-indexed rows to match Python conventions |

**`scope_py.rs`** — New Python API methods:

| Method | Purpose |
|--------|---------|
| `references_in_file(path)` → `list[(qn, line, col, kind)]` | All resolved references with classification |
| `all_qnames_in_file(path)` → `list[str]` | All QN strings for pre-filter index |

#### Python File Changes

**`lint.py`** — Removed last LibCST dependency:
- Replaced `cst.Module([]).code_for_node(match.node).strip()` with source-based
  text extraction using `match.line`/`match.col`/`match.end_line`/`match.end_col`
- Builds line-offset table lazily (once per file) for efficient extraction
- Falls back to `match.matched_text` when available (Rust fast path)

**`transform.py`** — `_index_batch` migration:
- Uses `PyScopeResolver` (one per batch) for QN and reference collection
- Replaces `MetadataWrapper` + `_QNCollector` + `_RefIndexCollector`
- `cst.parse_module()` is now conditional (only for `parse_cache`, not for QN/ref)
- Added `_extract_all_exports_text()` regex-based `__all__` extraction
  (replaces `_extract_all_exports(module)` which required LibCST module)

#### Key Fixes Applied

5. **1-indexed reference lines**: Rust scope resolver references now use
   1-indexed lines (matching Python/LibCST convention). Without this,
   the dead-code self-reference exclusion (`ri.line = si.line`) failed
   because tree-sitter uses 0-indexed rows.

### Phase 0.7: Project Search Migration (COMPLETED)

Migrated `find_references()` and `find_callers()` to tree-sitter, bypassing
LibCST's `visit_project()` loop. All 1,309 tests pass.

#### Rust Extension Enhancements (`emend_core`)

**`scope.rs`** — Write context detection:

| Change | Purpose |
|--------|---------|
| `is_write_context()` helper | Robustly identify store context by walking up parent tree |
| `for`/`with`/`walrus` write detection | Handle loop variables, as-clauses, and named expressions |
| `collect_binding_targets()` recursion | Correctly bind names in nested patterns like `for (a, b) in items` |

#### Python File Changes

**`transform.py`** — `find_references`/`find_callers` migration:
- Introduced `visit_project_ts()`: tree-sitter based project iteration with parallel read + pre-filtering
- `find_references()` cold path uses `visit_project_ts()` + `references_in_file()`
- `find_callers()` uses `visit_project_ts()` + `references_in_file()` filtered to `kind == "call"`
- Removed `_ReferenceFinder` and `_CallerFilter` CST visitors

#### Key Fixes Applied

6. **Write context parity**: Rust `is_write_context` now correctly classifies
   `for` targets, `with...as` targets, and `walrus` targets, matching LibCST's
   MetadataWrapper behavior. This fixed `test_writes_only_for_target`.

### Phase 0.8: Read-Only Visitor & Core Command Migration (COMPLETED)

Migrated more read-only visitors and core symbol-related commands to tree-sitter.
Enabled fast-path for scoped searches. All 1,309 tests pass.

#### Python File Changes

**`transform.py`** — Major cleanup and migration:
- **`find_callees()`**: Fully migrated to tree-sitter using `PyScopeResolver.references_in_file()` + `find_nested_definitions()`. Deleted `_CalleeCollector`.
- **Scoped search**: Refactored `find_pattern()` to handle `scope` as a post-filter using tree-sitter ranges. This allows scoped searches to use the Rust matcher fast-path. Deleted `ScopedPatternFinder`.
- **Import/Local filters**: Refactored `_filter_matches_by_import` and `_filter_matches_by_scope_local` to use `PyScopeResolver` instead of `MetadataWrapper` + `_ImportOriginCollector`. Deleted `_ImportOriginCollector`.
- **`get_symbol_source()`**: Refactored to use tree-sitter symbol discovery and direct line extraction.
- **`remove_symbol()`**: Refactored to use tree-sitter for validation and byte-range line deletion. Deleted `SymbolRemover`.
- **Cleanup**: Deleted `_BulkReferenceFinder` (already unused).

#### Key Fixes Applied

9. **Fast-path for Scoped Search**: By moving `scope` handling to a post-filter, `find_pattern` can now use the Rust accelerator for queries like `emend search 'print($X)' --in file.py::MyClass.method`, which previously fell back to LibCST.

### Phase 0.9: Rust-Guided Pattern Finding (COMPLETED)

Implemented `RustGuidedFinder` to use the Rust pattern matcher for all searches where the pattern compiles to Rust IR, including those with `--inside`/`--not-inside` constraints.

#### Python File Changes

**`transform.py`**:
- **`RustGuidedFinder`**: Added a new visitor that takes pre-calculated Rust match ranges and only extracts captures from those nodes. This avoids running the slow LibCST `m.matches()` on every node.
- **`find_pattern()`**: Updated to try Rust matching first. If successful, it uses `RustGuidedFinder`. This effectively bypasses `PatternFinder` and `ConstrainedPatternFinder` logic (which remain only as fallbacks).
- **Constraint Fast-Path**: `--inside` and `--not-inside` now use the Rust matcher's support for constraints, avoiding the need for `ConstrainedPatternFinder`'s ancestor stack tracking in Python.

---

### Remaining Work

#### Current LibCST Footprint in `transform.py`

**12 CSTVisitor/CSTTransformer subclasses** (remaining):

| Class | Type | Line | Metadata | Used By | Purpose |
|-------|------|------|----------|---------|---------|
| `SymbolFinder` | CSTVisitor | 2172 | — | `cmd_edit()`, `remove_component()` | Find symbol by path for lookup/edit |
| `ComponentSetter` | CSTTransformer | 2583 | — | `cmd_edit()` | Modify symbol components (body, decorator, params, bases, returns) |
| `ComponentAdder` | CSTTransformer | 2906 | — | `cmd_add()` | Insert items into list components |
| `ComponentRemover` | CSTTransformer | 3324 | — | `remove_component()` | Remove symbol components |
| `PatternFinder` | CSTVisitor | 3823 | — | `find_pattern()` (fallback) | Find patterns (no constraints) |
| `ConstrainedPatternFinder` | CSTVisitor | 3954 | PositionProvider | `find_pattern()` (fallback) | Find patterns with `--inside`/`--not-inside` |
| `PatternReplacer` | CSTTransformer | 4528 | — | `replace_in_file()` | Replace matched patterns with replacement code |
| `_NameCollector` | CSTVisitor | 5230 | — | `copy_to()` | Collect all names used in a code fragment |
| `_SymbolRenamer` | CSTTransformer | 5569 | QualifiedNameProvider | `rename_symbol()` | Scope-aware rename using QN matching |
| `_DocstringRenamer` | CSTTransformer | 5712 | — | `rename_symbol(docs=True)` | Replace names in docstrings |
| `ImportRewriter` | CSTTransformer | 6942 | — | `move_symbol()` | Rewrite imports to use new module path |
| `_ModuleImportRenamer` | CSTTransformer | 7164 | — | `rename_module()` | Rewrite all imports for module rename (817 lines) |

**24 `cst.parse_module()` calls** in `transform.py`.

**13 `MetadataWrapper` usages** (1 in `_index_batch` bypassed, 12 in commands).

#### Current LibCST Footprint in `pattern.py`

**1,610 lines**, 163 `cst.*` usages, 212 `m.*` matcher usages.

#### Near-term: Remove LibCST from `_index_batch` entirely (COMPLETED)

- [x] **Stop relying on LibCST for metadata indexing**. (QN, symbol, and reference indexing now use Tree-sitter, bypassing slow MetadataWrapper).
- [x] **Remove `_extract_all_exports(module)`**.
- [x] **Evaluate removing `_QNCollector` and `_RefIndexCollector`**.
- [ ] **Optional: Remove `parse_cache` population**. (Deferred: the persistent LibCST parse cache must be maintained for refactoring performance until LibCST is completely excised from the project).

#### Medium-term Phase 1: Pattern Engine Migration (`pattern.py`)

1. [x] **Expand Rust IR coverage in `_cst_to_rust_ir()`**
2. [ ] **Port `_cst_to_matcher()` to Rust** (`matcher.rs`).
3. [ ] **Remove LibCST matcher dependency** once 100% of patterns go through Rust.

#### Medium-term Phase 2: Read-Only Visitor Migration (`transform.py`)

1. [x] **`_ReferenceFinder`**
2. [x] **`_CallerFilter`**
3. [x] **`ScopedPatternFinder`**
4. [x] **`_ImportOriginCollector`**
5. [x] **`ConstrainedPatternFinder`** (line 4019, ~90 lines) — Replaced by `RustGuidedFinder` (logic moved to Rust).
6. [x] **`PatternFinder`** (line 3888, ~120 lines) — Replaced by `RustGuidedFinder`.
7. [x] **`_BulkReferenceFinder`**
8. [x] **`_CalleeCollector`**
9. [ ] **`SymbolFinder`** (line 2237).

#### Medium-term Phase 3: Transformer Migration (`transform.py`)

1. [ ] **`PatternReplacer`**
2. [ ] **`_SymbolRenamer`**
3. [ ] **`ComponentSetter`**
4. [ ] **`ComponentAdder`**
5. [ ] **`ComponentRemover`**
6. [x] **`SymbolRemover`**
7. [ ] **`_ModuleImportRenamer`**
8. [ ] **`_ImportRewriterForMove`**
9. [ ] **`ImportRewriter`**
10. [ ] **`_DocstringRenamer`**
11. [ ] **`_NoOpTransformer`**
12. [ ] **`_NameCollector`**

#### Medium-term Phase 4: `visit_project()` Migration

- [x] **Create `visit_project_ts()`**.
- [ ] **Migrate `_cached_parse()` call sites**.

#### Long-term: Full LibCST Removal

*Note: The `parse_cache` can only be removed once LibCST is totally excised from the project.*

- [ ] Remove `_cached_parse()`, `_parse_cache`, and `parse_cache` SQLite table
- [ ] Remove all CSTVisitor/CSTTransformer class definitions
- [ ] Remove `import libcst as cst` from `transform.py` and `pattern.py`
- [ ] Remove `libcst` from `pyproject.toml` dependencies
- [ ] Remove `compile_pattern_to_matcher()` and `_cst_to_matcher()` from
  `pattern.py` (keep only Rust IR path)
- [ ] Add language config system for multi-language support
- [ ] Second language support (TypeScript) using `tree-sitter-typescript`
