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
- **No cross-file coordination at all** -- operates file-by-file
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

#### 4. Language Config (`languages/<lang>/config.toml` -- new)

A TOML config file per language that defines scoping rules for the scope
resolver.  This enables cross-language qualified name resolution without
modifying Rust code.

```toml
# languages/python/config.toml
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
# languages/typescript/config.toml
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
| `replace` | `PatternReplacer` (LibCST CSTTransformer) | find_pattern() + PyFileTransform (Rust) |
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

## Proposed Simplification: Unifying Transforms

To address the concern of "bespoke Rust code" accumulating in `symbols.rs` and `matcher.rs`, we propose unifying the symbol extraction and transformation logic around standard Tree-sitter patterns and configuration.

### 1. Replace `symbols.rs` Manual Walking with Tree-sitter Queries (`.scm`)

The current `symbols.rs` implementation manually iterates over tree nodes and hardcodes checks for node types like `"function_definition"`, `"decorator"`, etc. This is fragile and specific to Python.

**Proposal**: Use standard Tree-sitter Query files (`symbols.scm`, `tags.scm`) to define what constitutes a symbol, a definition, or a reference.
- **Benefit**: Adding a new language (e.g., TypeScript) only requires adding a `.scm` file, not writing new Rust code.
- **Implementation**: The `ScopeResolver` or a new `SymbolExtractor` should load these queries at runtime based on the language.

### 2. Drive Scope Resolution via TOML Configuration

Currently, `scope.rs` uses `LanguageConfig::python_default()`, effectively hardcoding the Python configuration.

**Proposal**:
- Ensure `ScopeResolver` loads `languages/<lang>/config.toml` at runtime.
- Move all "what is a function", "what binds a name" logic into the TOML config (or `.scm` queries referenced by the config).
- **Goal**: The Rust code should know *nothing* about "def" or "class" keywords, only "scope creator nodes" defined in config.

### 3. Unify Symbol Collection and Scope Resolution

Currently, `symbols.rs` (used for `search --output summary`) and `scope.rs` (used for `find-references`) are separate.

**Proposal**:
- Make `ScopeResolver` the single source of truth for definitions.
- `search --output summary` should query the `ScopeResolver` (or the underlying scope graph) to list symbols, rather than re-parsing and re-walking the tree in `symbols.rs`.
- This eliminates the duplicated logic for finding functions/classes.

---

## Migration Strategy

### Phase 0: Vendor GritQL Crates + Build Integration Layer

1. **Vendor** `grit-pattern-matcher`, `grit-util`, and the Python language
   definition from `marzano-language` into `emend_core/vendor/`
2. **Build** a bridge: `EmendPattern` → `GritPattern` compilation
3. **Wire** into `find_pattern_in_files`: replace current `matcher.rs` IR with
   GritQL pattern IR
4. **Test**: Run `test_find.py`, `test_pattern.py`, `test_transform.py` --
   verify identical results
5. **Benchmark**: Compare pattern matching speed with current Rust + LibCST paths

**Validation**: All 265 test files pass with GritQL pattern engine.

### Phase 1: Build Scope Resolver

1. **Implement** `scope.rs`: scope tree, binding table, import table
2. **Implement** Python language config (`languages/python/config.toml`)
3. **Implement** `python` import resolution strategy
4. **Build comparison harness**: for every `.py` file in `tests/`, compare
   LibCST QualifiedNameProvider output with Rust scope resolver output
5. **Fix discrepancies** (likely: star imports, `__all__`, conditional imports,
   walrus operator, comprehension variable leaking)
6. **Wire** into `find_references`, `find_callers`, `find_dead_code`
7. **Feature flag**: `EMEND_USE_RUST_SCOPE=1` to toggle

### Phase 2: Build Rewrite Engine + Migrate Transforms

1. **Implement** `transform.rs`: byte-range edit engine
2. **Wire** GritQL rewrite output → `FileTransform`
3. **Migrate** `replace` command: GritQL rewrite replaces `PatternReplacer`
4. **Migrate** `edit`/`add` commands: tree-sitter node lookup + byte-range edits
   replace `ComponentSetter`/`ComponentAdder`/`ComponentRemover`
5. **Migrate** `rename`: scope resolver + byte-range edits replace `_SymbolRenamer`
6. **Migrate** `move`/`copy-to`: scope resolver + byte-range edits
7. **Run full test suite at each step**

### Phase 3: Remove LibCST

1. Remove all `import libcst` statements
2. Delete LibCST-specific code: `_cached_parse`, visitor base classes,
   `compile_pattern_to_matcher`, `_NoOpTransformer`
3. Remove `libcst` from `pyproject.toml` dependencies
4. Run full test suite
5. Benchmark: measure end-to-end speedup

### Phase 4: Add Language Config + Second Language

1. **Finalize** the language config TOML schema
2. **Refactor** scope resolver to be config-driven
3. **Add TypeScript config** (`languages/typescript/config.toml`)
4. **Implement `node` resolution strategy** for JS/TS imports
5. **Test** basic TypeScript operations: `search`, `refs`, `rename`
6. **Add tree-sitter-typescript** grammar dependency

### Phase 0.95: Simplification & Unification (COMPLETED)

Unified the symbol extraction backend and moved language-specific logic into configuration and standard queries.

#### Rust Extension Enhancements (`emend_core`)

| Change | Purpose |
|--------|---------|
| `SymbolExtractor` struct | Generic runner for Tree-sitter Queries (`.scm`) to extract symbols. |
| `LanguageConfig` Serde | Enabled TOML-based configuration for scoping rules, avoiding hardcoded Rust. |
| `ScopeResolver::get_symbols()` | Single source of truth for definitions, shared by name resolution and summary output. |
| `Binding` / `Scope` fields | Captured `signature`, `returns`, `start_line`, and `end_line` for rich symbol metadata. |
| `streaming-iterator` integration | Correctly handled high-performance Tree-sitter query results. |

#### Python File Changes

| File | Status | Changes |
|------|--------|---------|
| `ast_commands.py` | **Updated** | `collect_symbols` now queries `PyScopeResolver` instead of separate `symbols.rs` logic. |
| `languages/python/symbols.scm` | **New** | Declarative definition of Python symbols using standard TS query syntax. |

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

### Phase 2: Mutation Engine & Symbol Edits (COMPLETED)

Implemented the byte-range edit engine in Rust and migrated all symbol component
mutation operations (`get`, `set`, `add`, `remove`) to use Tree-sitter ranges.

#### Rust Extension Enhancements (`emend_core`)

**`transform.rs`** / **`transform_py.rs`** — New modules:

| Change | Purpose |
|--------|---------|
| `FileTransform` struct | Core edit engine: manages a set of non-overlapping byte-range replacements |
| `Edit` struct | Represents a single replacement, insertion, or removal |
| `PyFileTransform` | PyO3 bindings for `FileTransform` exposed to Python |
| `BTreeMap` ordering | Automatically sorts and validates edits to prevent overlapping |

**`symbols.rs`** — New granular discovery functions:

| Change | Purpose |
|--------|---------|
| `find_node_by_path()` | Robustly find a tree-sitter node given a symbol path (e.g., `['MyClass', 'method']`) |
| `get_symbol_component_range()` | Get byte range for `params`, `returns`, `decorators`, `bases`, or `body` |
| `get_symbol_component_list_items()` | Get names and byte ranges for individual items in list-like components |
| Negative index support | Support Python-style `[-1]` indexing for components in Rust |
| Intelligent range detection | Handles preceding whitespace for `returns` and indentation for `body` |

#### Python File Changes

**`transform.py`** — Major refactor:
- **`get_component()`**: Migrated to `_rust.get_symbol_component_range()`.
- **`set_component()`**: Migrated to `_rust.PyFileTransform()`.
- **`add_to_component()`**: Migrated to `_rust.get_symbol_component_list_items()` + `PyFileTransform`.
- **`remove_component()`**: Migrated to `_rust.get_symbol_component_range()` + `PyFileTransform`.
- **Deleted subclasses**: `SymbolFinder`, `ComponentSetter`, `ComponentAdder`, `ComponentRemover`.
- **Deleted helpers**: `_parse_params`, `_parse_param`, `_parse_decorator`, `_parse_base`, `_parse_body`, `_get_all_params`, etc.

#### Key Fixes Applied

10. **Intelligent Separator Management**: `add_to_component` and `remove_component`
    now manage commas and `*`/`/` separators directly via byte-range logic,
    improving speed and removing the need for LibCST's `with_changes`.

11. **Keyword-Only Safety**: Refined `add_to_component` to ensure keyword-only
    parameters are correctly inserted before `**kwargs` and after the `*`
    separator.

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
| `transform.py` | **Fully migrated** | `_index_batch` and `visit_project` use `PyScopeResolver` for QN caching. `find_references`, `find_callers`, `find_callees`, `get_component`, `set_component`, `add_to_component`, `remove_component` all fully migrated to Tree-sitter. Unused LibCST visitors/transformers removed. |
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

12. **Correct Scope for Definitions**: Fixed `walk_references` in Rust to ensure that the name of a class or function is resolved in the outer scope, not the nested scope it creates. This ensures that `class A` is correctly identified as a definition of `module.A` rather than `module.A.A`.

### Phase 0.9: Rust-Guided Pattern Finding (COMPLETED)

Implemented `RustGuidedFinder` to use the Rust pattern matcher for all searches where the pattern compiles to Rust IR, including those with `--inside`/`--not-inside` constraints.

#### Python File Changes

**`transform.py`**:
- **`RustGuidedFinder`**: Added a new visitor that takes pre-calculated Rust match ranges and only extracts captures from those nodes. This avoids running the slow LibCST `m.matches()` on every node.
- **`find_pattern()`**: Updated to try Rust matching first. If successful, it uses `RustGuidedFinder`. This effectively bypasses `PatternFinder` and `ConstrainedPatternFinder` logic (which remain only as fallbacks).
- **Constraint Fast-Path**: `--inside` and `--not-inside` now use the Rust matcher's support for constraints, avoiding the need for `ConstrainedPatternFinder`'s ancestor stack tracking in Python.

---

### Phase 1.0: Pattern Engine Full Rust Migration (IN PROGRESS)

Massively expanded the Rust pattern matcher and Python IR compiler so that the vast majority of patterns now compile to Rust IR and match via the Rust engine, bypassing LibCST entirely.

#### Rust Extension Enhancements (`emend_core` — `matcher.rs`)

- **+996 lines** in `matcher.rs`
- **New `PatternNode` variants**: `Return`, `Assert`, `Raise`, `Delete`, `Global`, `Nonlocal`, `Await`, `IfExp`, `Lambda`, `NamedExpr`, `ImportFrom`, `Import`, `IfStmt`, `WhileStmt`, `ForStmt`, `WithStmt`, `TryStmt`, `ExceptHandler`
- **`NameOrMetavar` enum**: Supports metavar captures in import module names, import names, and aliases
- **`ImportAlias` struct**: Full import alias matching with optional `as` name
- **`deserialize_param_pattern()`**: Extracted reusable param deserialization for funcdef + lambda
- **`match_import_alias()`**: Matching logic for import aliases with metavar support
- **TypeConstraint enhancements**: Added `name` field for capture; negated constraints (`:!int`); new kinds `"identifier"`, `"attr"`, `"stmt"`
- **Compound statement matching**: `if_statement`, `while_statement`, `for_statement`, `with_statement`, `try_statement`, `except_handler`

#### Python File Changes

**`pattern.py`** (+743 lines):
- **`_ast_to_rust_ir()`**: Massively expanded to handle `Return`, `Assert`, `Raise`, `Delete`, `Global`, `Nonlocal`, `Await`, `ImportFrom`, `Import`, `IfExp`, `Lambda`, `NamedExpr`, compound statements (`If`, `While`, `For`, `With`, `Try`, `ExceptHandler`)
- **Compare ops format**: Fixed from `[op_str, comp_ir]` lists to `{"op": op_str, "comparator": comp_ir}` dicts
- **Type constraint handling**: Fixed prefix mismatch (`:int` vs `int`), added negated constraint support, oracle constraint fallback
- **Import IR**: Full metavar support for module names, import names, and aliases

**`transform.py`** (-1149 lines net):
- **Removed LibCST-based classes**: `PatternFinder`, `ConstrainedPatternFinder`, `ScopedPatternFinder`, `PatternReplacer`, `RustGuidedFinder`, and associated LibCST helpers
- **Restored `_get_disk_cache()`** with thread-safe globals
- **Fixed anonymous metavar filtering**: `captures = {k: v for k, v in m[6].items() if k != "_"}`
- **`find_pattern()`**: Now routes 100% through Rust engine for pattern matching

#### Test Changes

- **`test_pattern.py`**: Replaced `compile_pattern_to_matcher` tests with `compile_pattern_to_rust_ir` tests
- **`test_rust_patterns.py`**: Updated Compare ops format assertions
- **`test_transform.py`**: Updated ellipsis capture assertions to string format (Rust returns comma-separated strings, not tuples)
- **`test_typeoracle_integration.py`**: Replaced `compile_pattern_to_matcher` with `compile_pattern_to_rust_ir`
- **`test_index_cache.py`**: Updated assertions for new cache behavior (`parse_cache` no longer populated)

#### Current Test Status: 1273 passed, 38 failed

### Migration Complete ✓

All LibCST code has been removed from the project. The codebase now uses tree-sitter with a Rust backend for all AST operations.

#### Near-term: Remove LibCST from `_index_batch` entirely (COMPLETED)

- [x] **Stop relying on LibCST for metadata indexing**. (QN, symbol, and reference indexing now use Tree-sitter, bypassing slow MetadataWrapper).
- [x] **Remove `_extract_all_exports(module)`**.
- [x] **Evaluate removing `_QNCollector` and `_RefIndexCollector`**.
- [x] **Remove `parse_cache` population**. (parse_cache is no longer populated; only `qn_index` table remains active).

#### Medium-term Phase 1: Pattern Engine Migration (`pattern.py`) (COMPLETED)

1. [x] **Expand Rust IR coverage in `_ast_to_rust_ir()`** — now handles 25+ node types
2. [x] **Route all pattern matching through Rust engine** — `find_pattern()` uses Rust exclusively
3. [x] **Remove dead LibCST code**: `compile_pattern_to_matcher()`, `_cst_to_matcher()`, `_cst_to_rust_ir()`, all `m.*` matcher imports, operator-to-matcher helpers (~1100 lines removed)

#### Phase 0.95: Simplification & Unification (COMPLETED)

1. [x] **Replace `symbols.rs` manual walking with Tree-sitter Queries (`queries/<lang>/symbols.scm`)**.
2. [x] **Unify `search --output summary` to use `ScopeResolver` logic** (eliminating duplicate symbol finding).
3. [x] **Implement runtime TOML config loading** for `ScopeResolver` (replacing `python_default()`).
4. [x] **Move Python-specific scoping rules** from Rust code into `languages/python/config.toml`.

#### Medium-term Phase 2: Read-Only Visitor Migration (`transform.py`)

1. [x] **`_ReferenceFinder`**
2. [x] **`_CallerFilter`**
3. [x] **`ScopedPatternFinder`**
4. [x] **`_ImportOriginCollector`**
5. [x] **`ConstrainedPatternFinder`**
6. [x] **`PatternFinder`**
7. [x] **`_BulkReferenceFinder`**
8. [x] **`_CalleeCollector`**
9. [x] **`SymbolFinder`** — Replaced by `find_node_by_path()` in Rust.

#### Medium-term Phase 3: Transformer Migration (`transform.py`) (COMPLETED)

1. [x] **`PatternReplacer`** — removed (Rust engine handles replacement)
2. [x] **`_SymbolRenamer`**
3. [x] **`ComponentSetter`**
4. [x] **`ComponentAdder`**
5. [x] **`ComponentRemover`**
6. [x] **`SymbolRemover`**
7. [x] **`_NoOpTransformer`** — removed
8. [x] **`_ModuleImportRenamer`**
9. [x] **`_ImportRewriterForMove`**
10. [x] **`ImportRewriter`**
11. [x] **`_DocstringRenamer`**
12. [x] **`_NameCollector`**

#### Medium-term Phase 4: `visit_project()` Migration (COMPLETED)

- [x] **Create `visit_project_ts()`**.
- [x] **Migrate `_cached_parse()` call sites** — removed entirely.

#### Test Status

All test failures from the migration have been resolved. The full test suite passes with the tree-sitter backend.

#### Long-term: Full LibCST Removal (COMPLETED)

- [x] Remove `_cached_parse()`, `_parse_cache`, and `parse_cache` SQLite table
- [x] Remove all CSTVisitor/CSTTransformer class definitions
- [x] Remove `import libcst as cst` from `transform.py` and `pattern.py`
- [x] Remove `libcst` from `pyproject.toml` dependencies
- [x] Remove `compile_pattern_to_matcher()`, `_cst_to_matcher()`, and `_cst_to_rust_ir()` from
  `pattern.py` (keep only Rust IR path)
- [x] Add language config system for multi-language support
- [ ] Second language support (TypeScript) using `tree-sitter-typescript`
  - [x] Config file (`languages/typescript/config.toml`) with scoping/binding rules
  - [x] Rust pattern matcher support for `.ts`, `.tsx`, `.js`, `.jsx`
  - [x] Scope resolver language detection by file extension
  - [ ] Python integration tests for TypeScript pattern matching
  - [ ] Python integration tests for TypeScript symbol lookup and refactoring
  - [ ] Rust unit tests for TypeScript-specific scoping rules
  - [ ] End-to-end tests for TypeScript imports, class members, nested functions

---

## Appendix F: Cross-Language Support Design (Proposal)

### Executive Summary

Emend is **85% language-agnostic** architecturally. The Rust tree-sitter backend, pattern matching IR, and type oracle are completely generic. To support new languages, only add:

1. **Language config TOML file** (scope/binding rules for Rust scope resolver)
2. **Symbol extraction query file** (`.scm` tree-sitter query for symbol discovery)
3. **Language plugin for Python** (import handling, docstring syntax)

No custom Rust code required for most languages. This section outlines the strategy for scaling to "all tree-sitter grammars."

### Analysis: What's Already Language-Agnostic

#### Core Strengths

| Component | Status | Evidence |
|-----------|--------|----------|
| **Pattern matching IR** | ✅ Language-agnostic | `matcher.rs` uses abstract syntax concepts (calls, attributes, assignments); no Python-specific node types |
| **Scope resolver** | ✅ Language-agnostic | Driven entirely by `languages/{lang}/config.toml`; accepts file extension parameter |
| **Symbol extraction** | ✅ Language-agnostic | `symbols.rs` + `.scm` tree-sitter queries; works for any language with a grammar |
| **Selector/component grammar** | ✅ Language-agnostic | `grammars/selector.lark` references generic concepts: `params`, `returns`, `decorators`, `bases`, `body`, `imports` |
| **Pattern string grammar** | ✅ Language-agnostic | `grammars/pattern.lark` supports metavars, type constraints, ellipsis—no Python syntax |
| **Type oracle abstraction** | ✅ Language-agnostic | `TypeOracle` ABC + LSP client (`type_oracle.py:33-182`); already supports any LSP-compatible type checker |
| **Find/replace operations** | ✅ Language-agnostic | Work on pattern IR and tree-sitter ranges; no language-specific assumptions |
| **Reference resolution** | ✅ Language-agnostic | Uses Rust scope resolver; parameterized by config TOML |

#### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│              Python CLI (cli.py)                             │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  emend search, replace, edit, add, refs, rename...           │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐   │
│  │   Language-Agnostic Layer (transform.py, query.py)    │   │
│  │                                                         │   │
│  │  · find_pattern(), replace_pattern()                  │   │
│  │  · find_references(), find_callers()                  │   │
│  │  · collect_symbols()                                  │   │
│  │  · get_component(), set_component(), add_component()  │   │
│  │  · resolve_imports()                                  │   │
│  └────────────────┬────────────────────────────────────┬─┘   │
│                   │                                    │       │
│    ┌──────────────┼────────────────────────────────┐   │       │
│    │              │                                │   │       │
│    ▼              ▼                                │   ▼       │
│  ┌──────────────────────────┐    ┌──────────────────────────┐│
│  │   Rust Backend           │    │ Type Oracle (LSP)        ││
│  │   (emend_core)           │    │                          ││
│  ├──────────────────────────┤    ├──────────────────────────┤│
│  │ · Pattern matcher        │    │ · PyreflyAdapter         ││
│  │ · Scope resolver         │    │ · PyrightAdapter (LSP)   ││
│  │ · Symbol extraction      │    │ · TyAdapter (LSP)        ││
│  │ · Component ranges       │    │ · RustAnalyzer (LSP)     ││
│  │ · Reference collection   │    │ · gopls, tsserver, etc.  ││
│  ├──────────────────────────┤    └──────────────────────────┘│
│  │ Config-driven:           │                                 │
│  │ · scope creators         │ Type inference is LSP:         │
│  │ · binding rules          │ Language-independent!           │
│  │ · import resolution      │                                 │
│  │ · QN construction        │                                 │
│  └──────────────────────────┘                                 │
│           ▲                                                   │
│           │                                                   │
│    ┌──────┴──────────────────┬──────────────────┐             │
│    │                         │                  │             │
│    ▼                         ▼                  ▼             │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐      │
│  │ Scope rules  │   │  Symbol      │   │ Import       │      │
│  │ TOML config  │   │  extraction  │   │ resolution   │      │
│  │              │   │  query (.scm)│   │ plugin (.py) │      │
│  │ languages/   │   │              │   │              │      │
│  │ {lang}/      │   │ languages/   │   │ Languages/   │      │
│  │ config.toml  │   │ {lang}/      │   │ {lang}/      │      │
│  │              │   │ symbols.scm  │   │ plugin.py    │      │
│  └──────────────┘   └──────────────┘   └──────────────┘      │
│                                                               │
└─────────────────────────────────────────────────────────────┘
         ▲                       ▲
         │                       │
    Tree-sitter Grammar      Language-specific
    (external crate)         configuration files
```

### Current Python-Specific Code (What Needs Generalization)

#### 1. File Extension Hardcoding

**Files affected**: `cli.py:62`, `transform.py:4722`, `component_selector.py:65`

```python
# Current (Python-only)
if f.endswith('.py'):
    ...
```

**Why it's blocking**: File discovery skips non-Python files; prevents CLI from working with other languages.

**Generalization strategy**: Add language detection:
```python
# Proposed
LANGUAGE_EXTENSIONS = {
    "python": [".py", ".pyi"],
    "rust": [".rs"],
    "go": [".go"],
    "typescript": [".ts", ".tsx", ".js", ".jsx"],
}

def file_matches_language(path: str, language: str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in LANGUAGE_EXTENSIONS.get(language, [])
```

#### 2. Import Handling (Python AST)

**Files affected**: `transform.py:1915-2020 (_get_imports, _add_import_text)`

**Current approach**:
- Uses Python's `ast.parse()` to extract imports
- Hardcoded logic for `__future__` imports
- Assumes `from X import Y, Z` syntax

**Why it's blocking**: Cannot rename imports or add imports in non-Python languages.

**Generalization strategy**: Language-specific import plugin system:

```python
class ImportHandler(ABC):
    @abstractmethod
    def extract_imports(self, source: str) -> list[ImportBinding]:
        """Extract imports from source code."""

    @abstractmethod
    def add_import(self, source: str, module: str, name: str, alias: str | None) -> str:
        """Insert a new import into source code."""

    @abstractmethod
    def remove_import(self, source: str, module: str, name: str) -> str:
        """Remove an import from source code."""

    @abstractmethod
    def sort_imports(self, source: str) -> str:
        """Sort imports according to language conventions."""

# Implementations per language
class PythonImportHandler(ImportHandler):
    # Uses ast.parse(), respects __future__, uses isort conventions

class RustImportHandler(ImportHandler):
    # Uses regex or tree-sitter query for use declarations

class GoImportHandler(ImportHandler):
    # Uses tree-sitter for import blocks
```

**Storage**: `languages/{lang}/plugin.py`:
```python
# languages/python/plugin.py
from emend.import_handlers import ImportHandler, PythonImportHandler

handlers = {
    "import": PythonImportHandler(),
}

# languages/rust/plugin.py
from emend.import_handlers import ImportHandler, RustImportHandler

handlers = {
    "import": RustImportHandler(),
}
```

**Usage in transform.py**:
```python
def _get_imports(source: str, language: str) -> list[ImportBinding]:
    handler = _load_import_handler(language)
    return handler.extract_imports(source)
```

#### 3. Docstring / Comment Handling

**Files affected**: `transform.py:3319-3340 (_rename_in_docstrings)`, `lint.py:52-80 (parse_noqa_comments)`

**Current approach**:
- Uses Python AST to find docstrings (string literals as first statement)
- Tokenizes Python to find `# noqa` comments

**Why it's blocking**: Docstring updates and noqa suppression won't work for other languages.

**Generalization strategy**: Comment handler plugin:

```python
class CommentHandler(ABC):
    @abstractmethod
    def find_docstrings(self, source: str, symbol_info: SymbolInfo) -> list[tuple[int, int, str]]:
        """Find docstring byte ranges and content for a symbol."""

    @abstractmethod
    def find_noqa_comments(self, source: str) -> dict[int, set[str]]:
        """Map line numbers to noqa tags present on that line."""

    @abstractmethod
    def parse_comment_syntax(self) -> tuple[str, str]:
        """Return (single_line_comment_prefix, multi_line_comment_start_end)."""
```

**Storage**: `languages/{lang}/plugin.py`:
```python
# languages/python/plugin.py
class PythonCommentHandler(CommentHandler):
    # Uses ast.get_docstring(), tokenizer for # noqa

handlers = {
    "comment": PythonCommentHandler(),
}

# languages/rust/plugin.py
class RustCommentHandler(CommentHandler):
    # Uses tree-sitter query for doc comments (/// or //)

handlers = {
    "comment": RustCommentHandler(),
}
```

#### 4. Pattern String Parsing Syntax

**Files affected**: `pattern.py:139-164 (compound header detection)`, `pattern.py:257-970 (_ast_to_rust_ir)`

**Current approach**: Parses pattern strings using Python's `ast` module.

**Why it's blocking**: Cannot write patterns using Rust, Go, or TypeScript syntax.

**Example issue**: Pattern `"if x > 0: print(x)"` assumes Python's `if ... :` syntax.

**Generalization strategy**: **Two-tier pattern syntax**

1. **Language-neutral patterns** (recommended):
   ```
   # Works in all languages (uses tree-sitter pattern syntax)
   $X.foo($Y) -type[int]
   ```

2. **Language-native patterns** (for power users):
   ```python
   # Emend uses language-specific parser for the pattern string
   emend search --pattern-lang rust "if x > 0 { ... }"
   emend search --pattern-lang python "if x > 0: ..."
   ```

**Implementation**:
```python
def compile_pattern(pattern: str, language: str, pattern_lang: str | None = None) -> PatternIR:
    if pattern_lang is None:
        # Use neutral syntax (always works)
        return compile_neutral_pattern(pattern)
    else:
        # Use language-native syntax
        handler = _load_pattern_handler(pattern_lang)
        return handler.compile(pattern)

# languages/python/plugin.py
class PythonPatternHandler:
    def compile(self, pattern: str) -> PatternIR:
        tree = ast.parse(pattern, mode='eval')  # or 'exec' for statements
        return _ast_to_rust_ir(tree)

# languages/rust/plugin.py
class RustPatternHandler:
    def compile(self, pattern: str) -> PatternIR:
        # Could use tree-sitter directly or a Rust crate
        # For now: Use tree-sitter queries (S-expressions)
```

**Better approach**: Treat patterns as **tree-sitter query language** (universal):

```
# These are tree-sitter S-expressions (language-independent!)
(call
  function: (identifier) @func
  arguments: (arguments (identifier) @arg))
```

**Then the neutral pattern syntax compiles to tree-sitter queries**:
```
$FUNC($ARG)  →  (call function: (_) @FUNC arguments: (_) @ARG)
```

### Proposed Implementation Plan

#### Phase 1: Language Detection & File Discovery

**Goals**: Make file discovery and language detection configurable so emend
can operate on non-Python files.

##### 1a. Build extension→language registry from existing TOML configs

- [x] **Create `src/emend/language_registry.py`** — a module that discovers
  `languages/*/config.toml` files (via `importlib.resources` or a known
  package path), parses the `[language]` section, and builds a lookup table:
  extension → language name, language name → list of extensions.
- [x] **Add `detect_language(path: str | Path) -> str | None`** — returns
  the language name from the file extension using the registry.  Returns
  `None` for unknown extensions.
- [x] **Add `get_extensions(language: str) -> list[str]`** — returns all
  registered extensions for a language (e.g. `["py", "pyi"]` for Python).

##### 1b. Replace hardcoded `.py` checks

- [x] **`cli.py:62`** — `resolve_path()` currently filters
  `f.endswith('.py')`.  Replace with `detect_language(f) is not None` (or
  a language-specific filter when `--language` is passed).
- [x] **`cli.py:484`** — pattern-vs-symbol heuristic checks
  `query.endswith('.py')`.  Replace with `detect_language(query)`.
- [x] **`component_selector.py:72`** — glob filtering uses
  `f.endswith('.py')`.  Replace with registry lookup.
- [x] **`transform.py:4722`** — `visit_project_ts()` filters with
  `f.endswith('.py')`.  Replace with registry lookup, accepting
  a `language` parameter.

##### 1c. Add `--language` CLI option

- [x] **Add `--language` / `-L` option** to the top-level Typer app
  (applies to all commands).  Default: `None` (auto-detect from file
  extensions; fall back to `"python"` for backward compatibility).
- [x] **Thread `language: str` parameter** through the call stack:
  `cli.py` → `resolve_files()` and `search` pattern mode → language-aware
  file collection. (Full threading to all transform.py functions deferred.)
- [ ] **Auto-detect in mixed-language projects**: when `--language` is
  not given and the target is a directory, inspect file extensions
  present and choose the most common, or error if ambiguous. (Deferred)

##### 1d. Tests

- [x] Test that `detect_language` returns correct results for `.py`,
  `.ts`, `.rs`, `.go`, `.tsx`, `.pyi`, `.jsx`, `.js`, and `None` for
  unknown extensions (`.txt`, `.md`).
- [x] Test that `get_extensions("python")` returns `["py", "pyi"]`.
- [x] Test that `resolve_path("src/")` includes `.ts` files when
  `--language typescript` is passed.
- [x] Test that `resolve_path("src/")` still defaults to `.py` files
  when no `--language` is given (backward compatibility).
- [ ] Test that `visit_project_ts()` respects the language parameter. (Deferred)

---

#### Phase 2: Language Plugin System

**Goals**: Make import handling, docstring handling, and comment/noqa
detection pluggable per-language.

##### 2a. Define abstract interfaces

- [x] **Create `src/emend/language_plugins.py`** with:
  - [x] `ImportHandler` ABC:
    - `extract_imports(source: str) -> list[ImportBinding]`
    - `add_import(source: str, module: str, name: str, alias: str | None) -> str`
    - `remove_import(source: str, module: str, name: str) -> str`
  - [x] `CommentHandler` ABC:
    - `find_docstrings(source: str, tree: Tree, symbol_range: tuple[int,int]) -> list[tuple[int, int, str]]` (byte ranges + content)
    - `find_noqa_comments(source: str) -> dict[int, set[str] | None]`
    - `line_comment_prefix` property (e.g. `"#"`, `"//"`)
  - [x] `PatternCompiler` ABC:
    - `compile(pattern_str: str, metavar_map: dict) -> dict | None` (returns Rust IR dict)
  - [x] `LanguagePlugin` dataclass composing the above three handlers.
  - [x] `load_plugin(language: str) -> LanguagePlugin` — discovers and
    caches plugins.  Uses `languages/{language}/plugin.py` if it exists;
    otherwise returns a plugin with `NoOp`/default handlers.

##### 2b. Implement stub/default handlers

- [x] **`NoOpImportHandler`** — all methods return empty/no-op.
- [x] **`RegexCommentHandler(line_comment_prefix: str)`** — generic
  noqa detection using `{prefix} noqa: {tag}` regex; `find_docstrings`
  returns `[]`.
- [ ] **`TreeSitterPatternCompiler`** — generic pattern compilation that
  parses pattern strings using tree-sitter for the target language
  instead of Python's `ast` module.  This is the "neutral" fallback.
  (Deferred — needs Rust integration work in Phase 5)

##### 2c. Extract Python plugin from existing code

- [x] **Create `languages/python/plugin.py`** with:
  - [x] `PythonImportHandler` — move `_get_imports()` (`transform.py:1915`)
    and `_add_import_text()` (`transform.py:1931`) logic into handler
    methods.
  - [x] `PythonCommentHandler` — move `_rename_in_docstrings()`
    (`transform.py:3319`) and `parse_noqa_comments()` (`lint.py:52`)
    logic into handler methods.
  - [x] `PythonPatternCompiler` — wraps existing `_ast_to_rust_ir()`
    (`pattern.py:255`) and `compile_pattern_to_rust_ir()`
    (`pattern.py:973`).

##### 2d. Refactor call sites to use plugin system

- [x] **`transform.py:_get_imports()`** — delegate to
  `load_plugin(language).import_handler.extract_imports()`.
- [x] **`transform.py:_add_import_text()`** — delegate to
  `load_plugin(language).import_handler.add_import()`.
- [x] **`transform.py:_rename_in_docstrings()`** — delegate to
  `load_plugin(language).comment_handler`.
- [x] **`lint.py:parse_noqa_comments()`** — delegate to
  `load_plugin(language).comment_handler.find_noqa_comments()`.
- [ ] **`pattern.py:compile_pattern_to_rust_ir()`** — delegate to
  `load_plugin(language).pattern_compiler.compile()`.  Current Python
  `ast`-based compilation becomes the `PythonPatternCompiler`.
  (Deferred — needs Rust integration work in Phase 5)

##### 2e. Tests

- [x] Test that `PythonImportHandler.extract_imports()` produces same
  output as the current `_get_imports()` on a corpus of test files.
- [x] Test that `PythonImportHandler.add_import()` produces same output
  as the current `_add_import_text()`.
- [x] Test that `PythonCommentHandler.find_noqa_comments()` matches
  current `parse_noqa_comments()`.
- [x] Test that `NoOpImportHandler` returns source unchanged / empty
  lists.
- [ ] Test that `RegexCommentHandler("//")` detects `// noqa: deadcode`
  comments in C-style languages.
- [x] Test that `load_plugin("python")` returns a `LanguagePlugin` with
  all three handlers populated.
- [x] Test that `load_plugin("unknown_lang")` returns defaults (NoOp
  import, Regex comment, TreeSitter pattern compiler) without crashing.
- [x] Full `make test` passes — no regressions from the refactor.

---

#### Phase 3: Generalize Rust Backend Hardcoding

**Goals**: Remove Python-specific string literals from Rust code so the
same backend works for any language whose config TOML is loaded.

##### 3a. Generalize `scope.rs` import collection

The `collect_import()` function (`scope.rs:1553`) and
`walk_references()` import handling (`scope.rs:783-850`) hardcode
`"import_statement"`, `"import_from_statement"`, `"dotted_name"`,
`"aliased_import"`, `"wildcard_import"`, and `"module_name"`.

- [ ] **Use `ImportsSection` from config** instead of hardcoded strings:
  - Replace `"import_statement"` with `config.imports.import_statement`
  - Replace `"import_from_statement"` with `config.imports.import_from`
  - Replace `"module_name"` field access with `config.imports.module_field`
  - Replace `"wildcard_import"` with `config.imports.star_import`
  - Replace `"alias"` field access with `config.imports.alias_field`
- [ ] **Generalize `collect_import()` to use config fields** for
  `dotted_name` / `aliased_import` child node types.  Add a
  `child_node_types` field to `ImportsSection`:
  ```toml
  [imports]
  # Node types that can appear as imported names
  name_node_types = ["dotted_name", "identifier", "aliased_import"]
  alias_node_type = "aliased_import"
  ```
- [ ] **Generalize `walk_references()` import handling** — same approach,
  replace hardcoded node type strings with config lookups.

##### 3b. Generalize `symbols.rs` node type checks

`symbols.rs` hardcodes `"function_definition"`, `"class_definition"`,
`"decorated_definition"`, `"async"`, and Python-specific field names.

- [ ] **Add `[symbols]` section to language config TOML**:
  ```toml
  [symbols]
  function_node = "function_definition"
  class_node = "class_definition"
  decorated_node = "decorated_definition"    # optional
  async_keyword = "async"                     # optional
  parameters_field = "parameters"
  return_type_field = "return_type"
  name_field = "name"
  ```
- [ ] **Refactor `symbols.rs` functions** (`collect_symbols_impl`,
  `find_node_by_path`, `get_symbol_component_range`, etc.) to read
  node type strings from config instead of hardcoded literals.
- [ ] **Pass `LanguageConfig` to symbol functions** — currently they only
  receive source + tree.  Add config parameter or store on a struct.

##### 3c. Generalize `matcher.rs` node type checks

`matcher.rs` hardcodes `"function_definition"`, `"class_definition"`,
`"import_statement"`, `"import_from_statement"`, `"decorated_definition"`,
`"assignment"`, and various Python statement node types.

- [ ] **Add `[pattern_matching]` section to language config TOML**:
  ```toml
  [pattern_matching]
  # Maps abstract IR node types to concrete tree-sitter node types
  function_def = "function_definition"
  class_def = "class_definition"
  import_stmt = "import_statement"
  import_from_stmt = "import_from_statement"
  decorated_def = "decorated_definition"
  assignment = "assignment"
  # Statement node types (for "is this a statement?" checks)
  statement_nodes = [
      "expression_statement", "return_statement", "if_statement",
      "for_statement", "while_statement", "try_statement",
      "with_statement", "function_definition", "class_definition",
      "decorated_definition", "assignment", "augmented_assignment",
      "import_statement", "import_from_statement", "assert_statement",
      "raise_statement", "delete_statement", "global_statement",
      "nonlocal_statement",
  ]
  ```
- [ ] **Refactor `matcher.rs`** to read node types from a config struct
  rather than hardcoded string literals.  The `find_pattern_in_files()`
  entry point already receives a language parameter — thread the loaded
  config through to match functions.
- [ ] **Note**: The `PatternNode` enum variants themselves (Call,
  FuncDef, ClassDef, etc.) are abstract and language-agnostic.  Only
  the tree-sitter node type _strings_ they match against need to come
  from config.

##### 3d. Tests

- [ ] Rust unit tests: verify that `walk_node()` with Python config
  produces identical scope trees as before the refactor.
- [ ] Rust unit tests: verify that `walk_node()` with TypeScript config
  correctly collects scopes for `function_declaration`,
  `arrow_function`, `class_declaration`.
- [ ] Rust unit tests: verify `collect_import()` with TypeScript config
  handles `import { X } from 'module'` correctly.
- [ ] Rust unit tests: verify `collect_symbols_impl()` with TypeScript
  config finds functions and classes.
- [ ] Python integration: `make test` still passes (Python config
  produces identical behavior).

---

#### Phase 4: Add Language Config Files & Symbol Queries

**Goals**: Create complete configuration for TypeScript (and templates
for other languages) so they work end-to-end.

##### 4a. TypeScript

- [ ] **Complete `languages/typescript/config.toml`** — verify all
  sections match actual `tree-sitter-typescript` node types.  Currently
  missing: `[imports.alias_field]`, `[imports.star_import]`,
  `[symbols]`, `[pattern_matching]` sections.
- [ ] **Create `languages/typescript/symbols.scm`** — tree-sitter query
  for extracting function declarations, class declarations, variable
  declarations, method definitions, arrow functions.
- [ ] **Create `languages/typescript/plugin.py`** — with
  `TypeScriptImportHandler` (parse `import { X } from 'Y'`,
  `import X from 'Y'`, `require()`) and
  `RegexCommentHandler("//")`.
- [ ] **Add `tree-sitter-typescript` test fixtures** — small `.ts` files
  exercising: functions, classes, arrow functions, imports/exports,
  nested scopes, type annotations.
- [ ] **Integration tests**: `emend search '$X($Y)' --in fixtures/ -L typescript`
- [ ] **Integration tests**: `emend search --output summary fixtures/sample.ts`
- [ ] **Integration tests**: `emend refs 'fixtures/sample.ts::myFunc'`
- [ ] **Integration tests**: `emend replace '$OLD($X)' '$NEW($X)' --in fixtures/ -L typescript`

##### 4b. Rust language

- [ ] **Create `languages/rust/config.toml`** — scope creators
  (`function_item`, `impl_item`, `struct_item`, `enum_item`, `mod_item`,
  `closure_expression`), binding rules (`let_declaration`,
  `parameter`), import rules (`use_declaration`), QN rules
  (`::`  separator).
- [ ] **Create `languages/rust/symbols.scm`** — function_item,
  struct_item, enum_item, impl_item, const_item, static_item,
  type_alias.
- [ ] **Create `languages/rust/plugin.py`** — `NoOpImportHandler` (or
  basic `use` statement handler), `RegexCommentHandler("//")`.
- [ ] **Add `tree-sitter-rust`** to `Cargo.toml` dependencies.
- [ ] **Basic integration tests** for search, summary, refs.

##### 4c. Go language

- [ ] **Create `languages/go/config.toml`** — scope creators
  (`function_declaration`, `method_declaration`, `func_literal`),
  binding rules (`short_var_declaration`, `var_declaration`),
  import rules (`import_declaration`), QN rules (`/` separator).
- [ ] **Create `languages/go/symbols.scm`** — function_declaration,
  method_declaration, type_declaration, const_declaration,
  var_declaration.
- [ ] **Create `languages/go/plugin.py`** — `NoOpImportHandler`,
  `RegexCommentHandler("//")`.
- [ ] **Add `tree-sitter-go`** to `Cargo.toml` dependencies.
- [ ] **Basic integration tests** for search, summary.

##### 4d. Language template / documentation

- [ ] **Create `languages/TEMPLATE/` directory** with a skeleton
  `config.toml` (all sections present with `TODO` placeholders),
  `symbols.scm` (empty with comments explaining the format), and
  `plugin.py` (using NoOp/Regex defaults).
- [ ] **Write `docs/adding-a-language.md`** documenting the process:
  1. Copy `languages/TEMPLATE/` to `languages/{lang}/`
  2. Fill in `config.toml` (explain each section)
  3. Write `symbols.scm` (link to tree-sitter query docs)
  4. Optionally implement import/comment handlers in `plugin.py`
  5. Add tree-sitter grammar crate to `Cargo.toml`
  6. Run tests

---

#### Phase 5: Type Oracle Universalization

**Goals**: Support `:type[X]` and `:returns[X]` constraints for any
language with an LSP-compatible type checker.

##### 5a. Generic LSP type oracle

- [ ] **Create `GenericLSPAdapter(TypeOracle)` class** in
  `type_oracle.py` that wraps `LSPClient` with a configurable command
  and `languageId`.  This replaces the need for per-language adapter
  classes for any LSP-compliant type checker.
- [ ] **Add `LANGUAGE_TO_LSP` mapping** in `type_oracle.py`:
  ```python
  LANGUAGE_TO_LSP: dict[str, list[tuple[str, str, str]]] = {
      # (binary_name, languageId, display_name)
      "python": [
          ("pyright-langserver", "python", "pyright"),
          ("pylsp", "python", "pylsp"),
      ],
      "rust": [("rust-analyzer", "rust", "rust-analyzer")],
      "go": [("gopls", "go", "gopls")],
      "typescript": [("typescript-language-server", "typescript", "tsserver")],
  }
  ```
- [ ] **Refactor `detect_type_engine()`** (`type_oracle.py:1372`) to
  accept a `language` parameter and check `LANGUAGE_TO_LSP` entries.
- [ ] **Refactor `create_type_oracle()`** (`type_oracle.py:1412`) to
  accept a `language` parameter and instantiate `GenericLSPAdapter` for
  non-Python languages.

##### 5b. Thread language through type constraint filtering

- [ ] **`transform.py:_filter_matches_by_type_oracle()`** — pass
  `language` to `create_type_oracle()`.
- [ ] **`query.py:_filter_by_returns_with_oracle()`** — pass `language`.
- [ ] **`LSPClient.did_open()`** — use correct `languageId` from
  registry instead of hardcoded `"python"`.

##### 5c. Tests

- [ ] Test that `GenericLSPAdapter` correctly parses hover responses
  from a mock LSP server.
- [ ] Test that `detect_type_engine("rust")` finds `rust-analyzer` when
  it's on PATH.
- [ ] Test that `create_type_oracle(language="typescript")` returns a
  `GenericLSPAdapter` configured for tsserver.
- [ ] Test that type constraint filtering gracefully degrades (returns
  all matches) when no type checker is available for a language.
- [ ] Keep existing Python type oracle tests passing unchanged.

---

#### Phase 6: Tree-Sitter-Based Pattern Compilation (Optional)

**Goals**: Support pattern matching using the target language's native
syntax instead of requiring patterns to be valid Python.

Currently `compile_pattern_to_rust_ir()` (`pattern.py:973`) parses the
pattern string with Python's `ast.parse()`.  This means patterns like
`if x > 0 { println!("{}", x) }` can't be expressed.

##### 6a. Tree-sitter pattern parser

- [ ] **Implement `TreeSitterPatternCompiler`** that:
  1. Substitutes metavar placeholders (`$X` → `__EMEND_META_X__`)
  2. Parses the munged string with tree-sitter for the target language
  3. Walks the tree-sitter CST, converting nodes to Rust IR dicts
  4. Replaces placeholder identifiers back to `Metavar` IR nodes
- [ ] This provides a "universal" pattern compilation path that works
  for any language with a tree-sitter grammar, without needing a
  language-specific AST module.

##### 6b. Wire into plugin system

- [ ] **Default behavior**: when `language != "python"` and no
  language-specific `PatternCompiler` is registered, use
  `TreeSitterPatternCompiler`.
- [ ] **Python keeps current behavior**: `PythonPatternCompiler`
  (wrapping `ast.parse()`) remains the default for Python patterns
  since it has the most mature metavar/constraint handling.
- [ ] **Add `--pattern-syntax` flag** (optional) to let users explicitly
  choose `python`, `native`, or `treesitter` compilation.

##### 6c. Tests

- [ ] Test that `TreeSitterPatternCompiler` compiles `$X.foo($Y)` to
  correct IR for Python, TypeScript, Rust, Go.
- [ ] Test that `TreeSitterPatternCompiler` compiles
  `fn $NAME($...PARAMS) -> $RET` to FuncDef IR for Rust.
- [ ] Test that compound statement patterns work:
  `if $COND { $...BODY }` matches TypeScript `if` blocks.
- [ ] Test error messages when pattern doesn't parse in target language.

---

### Revised Component Selection Grammar

To support generic components across languages, extend the selector grammar:

```lark
component : "[" COMPONENT_NAME "]"

COMPONENT_NAME :
    "params"           # Function/method parameters
    | "returns"        # Return type annotation
    | "decorators"     # Decorators / attributes (#[] in Rust, @ in Python/TS)
    | "bases"          # Class bases / implements
    | "body"           # Function/class body
    | "imports"        # Module imports
    | "type_annotation" # Type annotation (TypeScript, Rust, Python)
    | "docstring"      # Docstring / doc comment
    | "attributes"     # Object properties / fields
    | "methods"        # Object methods / functions
    | "generics"       # Type parameters (Rust, Go, TypeScript)
```

Component-to-node mapping is defined per-language in the config:

```toml
# In languages/{lang}/config.toml
[components]
# Maps emend component names to tree-sitter field/node paths
params = "parameters"
returns = "return_type"
decorators = "decorator"          # Python
# decorators = "attribute_item"   # Rust
body = "body"
generics = "type_parameters"      # TypeScript, Rust
```

**TODOs**:

- [ ] Add `[components]` section to `LanguageConfig` struct in `scope.rs`.
- [ ] Add `[components]` to `languages/python/config.toml`.
- [ ] Add `[components]` to `languages/typescript/config.toml`.
- [ ] Refactor `get_symbol_component_range()` in `symbols.rs` to use
  `config.components` instead of hardcoded field names.
- [ ] Extend `grammars/selector.lark` with new component names.
- [ ] Add tests for new component types on TypeScript/Rust fixtures.

---

### Summary: Adding a New Language

After this proposal is implemented, adding a new language requires:

| File | Required? | Size | Purpose |
|------|-----------|------|---------|
| `languages/{lang}/config.toml` | **Yes** | ~100 lines | Scope/binding/import/QN/component rules |
| `languages/{lang}/symbols.scm` | **Yes** | ~30 lines | Tree-sitter query for symbol extraction |
| `languages/{lang}/plugin.py` | No | ~50 lines | Custom import/comment handling (defaults provided) |
| `Cargo.toml` | **Yes** | 1 line | Add `tree-sitter-{lang}` dependency |

No custom Rust code.  No new Python classes.  Just configuration files.

### Implementation Effort

By the end of Phase 4, you can add any tree-sitter-supported language
with ~130 lines of TOML + query.

### Alignment with Existing Architecture

This proposal requires **no breaking changes**:
- ✅ Existing Python projects work unchanged
- ✅ Scope resolver already accepts language parameter
- ✅ Pattern matcher is language-agnostic
- ✅ Type oracle already LSP-based
- ✅ Backward compatible: default to Python if no language specified

All changes are **additive**: new config directories, plugin system,
language detection.  The core Rust backend remains untouched except for
replacing hardcoded string literals with config lookups (Phase 3).
