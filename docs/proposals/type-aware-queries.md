# Proposal: Type-Aware Queries via Pyrefly Integration

**Status:** Brainstorm
**Date:** 2026-02-27

## Motivation

emend currently operates on syntactic structure: tree-sitter for fast
matching, LibCST for precise refactoring. This is powerful but blind to
types. You can find all calls to `connect()`, but you can't ask "find all
expressions of type `Connection`" or "find all functions that return
`Optional[str]`."

Type information unlocks a class of queries and refactoring operations
that are impossible with syntax alone:

- **Type-constrained search:** `emend search '$x:type[Connection]' src/`
- **Return-type filtering:** `emend search '$f:returns[Optional[str]]' src/`
- **Protocol-aware dead code:** knowing a method is used via a `Protocol`
  constraint even without a direct call
- **Type-aware rename:** renaming a method propagates to structural subtypes,
  not just lexical references
- **Migration queries:** "find all places where a `str` flows into a function
  expecting `bytes`"

## Why Pyrefly

Three Rust-based Python type checkers now exist: **ty** (Astral),
**Pyrefly** (Meta), and **Zuban**. We prefer Pyrefly for the near-term
integration:

1. **Library crate architecture.** Pyrefly is organized as a Rust workspace
   with well-separated crates: `pyrefly_types` (type representation),
   `pyrefly_graph` (indexing/caching), `pyrefly_python` (module modeling),
   `pyrefly_config`, `pyrefly_util`. The main `pyrefly` crate has a
   `[lib]` section in Cargo.toml — it's designed as an embeddable library,
   not just a CLI binary.

2. **Shared infrastructure.** Pyrefly uses `ruff_python_parser` and
   `ruff_python_ast` internally. emend already uses tree-sitter, but the
   ruff parser is well-understood and battle-tested on Instagram-scale
   codebases.

3. **Performance.** 1.8M lines/sec on Meta's internal codebases. Type
   checking PyTorch in 2.4s (vs Pyright's 35.2s). Fast enough for
   interactive use.

4. **MIT license.** No licensing complications for embedding.

5. **Conformance trajectory.** 70%+ conformant with the typing spec as of
   late 2025, improving rapidly.

**ty** is the likely long-term winner (faster, from the ruff team, will
eventually publish stable crates). Once ty ships stable Rust crates on
crates.io, switching makes sense. The integration surface should be
designed with this migration in mind.

## Integration Architecture

### Phase 1: Type Oracle (CLI/subprocess)

Lowest risk. Shell out to `pyrefly` and parse its JSON output.

```
pyrefly check --output json src/
```

Cache results per-file (keyed on file content hash). Expose as:

```python
# In transform.py
def get_type_info(file: Path, project_root: Path) -> dict[str, TypeInfo]:
    """Return inferred types for all symbols in a file."""
    ...
```

This lets us prototype the UX without touching Cargo.toml.

### Phase 2: Rust Crate Integration

Add `pyrefly` as a git dependency in `rust/Cargo.toml`:

```toml
[dependencies]
pyrefly = { git = "https://github.com/facebook/pyrefly", branch = "main" }
# Or specific sub-crates:
pyrefly_types = { git = "https://github.com/facebook/pyrefly", branch = "main" }
pyrefly_graph = { git = "https://github.com/facebook/pyrefly", branch = "main" }
```

New Rust functions exposed via PyO3:

```rust
/// Infer types for all symbols in a file, returning a map of
/// (line, col) -> type_string.
#[pyfunction]
fn infer_types(source: &str, project_root: &str) -> PyResult<Vec<TypedSymbol>> { ... }

/// Check if the type at a position is assignable to a target type.
#[pyfunction]
fn type_matches(source: &str, line: usize, col: usize,
                target_type: &str, project_root: &str) -> PyResult<bool> { ... }

/// Batch: for all matches from a pattern search, filter to those
/// where the matched expression has a type assignable to `target_type`.
#[pyfunction]
fn filter_matches_by_type(
    matches: Vec<Match>, target_type: &str, project_root: &str
) -> PyResult<Vec<Match>> { ... }
```

### Phase 3: LibCST Integration

The interesting synergy: LibCST handles precise refactoring, Pyrefly
provides type information to guide it. Possible integration points:

- **Type-aware `QualifiedNameProvider` replacement.** LibCST's
  `QualifiedNameProvider` does name resolution via scope analysis.
  Pyrefly's type inference is strictly more powerful — it resolves through
  generics, protocols, overloads, and type aliases.

- **Type-annotated CST nodes.** A wrapper that attaches Pyrefly type info
  to LibCST nodes, so visitors can query `node.inferred_type` during
  traversal.

- **Refactoring precondition checks.** Before a rename or move, verify
  type compatibility: "will this rename break any Protocol conformance?"

## UX: New Syntax

### Type constraints in selectors

```bash
# Find all variables of type Connection
emend search '$x:type[Connection]' src/

# Find functions returning Optional[str]
emend search '$f:returns[Optional[str]]' src/

# Find calls where first argument is type bytes
emend search '$f($arg:type[bytes], $...)' src/
```

### Type filters as flags

```bash
# Find all references to 'connect', but only where the receiver is type Pool
emend refs pool.py::connect --receiver-type Pool

# Dead code, but aware of Protocol usage
emend deadcode src/ --type-aware
```

### Type output mode

```bash
# Show inferred types alongside symbols
emend search --output types src/models/
# Output:
# src/models/user.py::User.get_name -> (self) -> str
# src/models/user.py::User.age -> int
# src/models/user.py::create_user -> (name: str, age: int) -> User
```

## Risks

- **Pyrefly API instability.** Pinning to git revisions and wrapping the
  API behind an emend-internal trait allows swapping backends.
- **Build complexity.** Pyrefly pulls in a large dependency tree (ruff
  parser, salsa-like incremental computation, tokio). Build times for
  emend_core will increase significantly.
- **Semantic mismatch.** Tree-sitter and Pyrefly use different AST
  representations. Mapping positions between them needs care (both use
  line/column, but off-by-one conventions differ).
- **Partial type info.** Pyrefly won't always infer types (untyped code,
  dynamic patterns). The UX needs graceful degradation — type constraints
  that can't be resolved should warn, not error.

## Migration Path to ty

The integration should use an internal `TypeOracle` trait:

```rust
trait TypeOracle {
    fn infer_file(&self, path: &Path) -> Result<FileTypes>;
    fn type_at(&self, path: &Path, line: usize, col: usize) -> Result<Option<Type>>;
    fn is_assignable(&self, source: &Type, target: &Type) -> Result<bool>;
}
```

Pyrefly and ty both implement this trait. Switching backends is a
Cargo.toml change + new impl, not a rewrite.

---

# Crazy Idea: Multilingual emend via Tree-Sitter + Universal Type Specs

## The Vision

emend today is Python-only. But the core model — structural pattern
matching + selector-based refactoring — is not inherently
language-specific. Tree-sitter grammars exist for **250+ languages**.
What if emend could do `emend search '$f($x, $...)' src/ --lang rust`?

The missing piece for type-aware operations is that type systems differ
wildly across languages. But the *queries* people want to run are often
the same: "find all expressions of type X", "find functions returning Y",
"find dead code." What if there were a universal type representation that
emend could consume, with language-specific producers?

## Architecture: Three Layers

```
┌──────────────────────────────────────────────────┐
│  emend CLI / Python API                          │
│  (selectors, patterns, refactoring orchestration)│
├──────────────────────────────────────────────────┤
│  emend_core (Rust)                               │
│  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ tree-sitter   │  │ type oracle              │  │
│  │ (parsing,     │  │ (unification engine,     │  │
│  │  matching,    │  │  constraint solver,      │  │
│  │  traversal)   │  │  type spec loader)       │  │
│  └──────────────┘  └──────────────────────────┘  │
├──────────────────────────────────────────────────┤
│  Language Adapters                                │
│  ┌────────┐ ┌────────┐ ┌──────┐ ┌────────────┐  │
│  │ Python │ │  Rust  │ │  TS  │ │    Go      │  │
│  │pyrefly │ │rust-   │ │ tsc  │ │  go/types  │  │
│  │  /ty   │ │analyzer│ │      │ │            │  │
│  └────────┘ └────────┘ └──────┘ └────────────┘  │
└──────────────────────────────────────────────────┘
```

### Layer 1: Tree-Sitter Multilingual Core

emend_core already uses tree-sitter-python. Generalizing to multiple
languages means:

**Grammar registry.** A mapping from file extension → tree-sitter grammar.
The `tree-sitter-language-pack` bundles 165+ grammars. In Rust, each
grammar is a `tree_sitter::Language` value; loading them dynamically (via
`libloading` or compiled-in features) is straightforward.

**Language-specific node mappings.** The pattern matcher needs to know that
Python's `function_definition` ≈ Rust's `function_item` ≈ Go's
`function_declaration`. This is a finite mapping per language — maybe 20
node kinds that matter (function def, class/struct def, call expression,
identifier, string literal, parameter, return type, import/use
statement). Define it as data:

```yaml
# lang_specs/python.yaml
function_def: function_definition
class_def: class_definition
call_expr: call
identifier: identifier
parameter: parameter
return_type: [type, "->"]  # child of function_definition
import: [import_statement, import_from_statement]

# lang_specs/rust.yaml
function_def: function_item
class_def: [struct_item, enum_item, impl_item]
call_expr: call_expression
identifier: identifier
parameter: parameter
return_type: [type_identifier, "->"]
import: use_declaration
```

**Unified pattern language.** emend's `$METAVAR` patterns already work on
tree-sitter nodes. The generalization: patterns are written in the target
language's syntax but compiled against tree-sitter node kinds using the
language spec above. `$f($x, $...)` means "a `call_expr` node with a
`identifier` child and `parameter` children" regardless of language.

### Layer 2: Universal Type Spec Format

This is the novel part. Define a **language-neutral type representation**
that language-specific type checkers can produce:

```
// type_spec.tsp — a strawman format

// Primitive types (language maps these to native equivalents)
type String
type Int
type Float
type Bool
type Bytes
type None  // / null / nil / unit / void

// Parameterized types
type List<T>
type Dict<K, V>
type Optional<T> = T | None
type Result<T, E>

// Function types
type Fn<(Params...) -> Return>

// Structural types (protocols / interfaces / traits)
protocol Iterable<T> {
    fn __iter__() -> Iterator<T>
}

protocol Callable<(Params...) -> Return> {
    fn __call__(Params...) -> Return
}

// Declarations — what a language adapter produces per file
file "src/models/user.py" {
    class User {
        name: String
        age: Int
        fn get_name(self) -> String
        fn create(name: String, age: Int) -> User  // static
    }
    fn create_user(name: String, age: Int) -> User
    CONNECTION_POOL: Pool<Connection>
}
```

**Key insight:** This format doesn't need to represent the *full* type
system of any language. It represents the *queryable surface* — the types
that a developer might want to filter on. Generics, protocols, and unions
cover 90% of useful queries. Language-specific exotica (Rust lifetimes,
TypeScript conditional types, Haskell type classes) can be erased or
approximated.

### Layer 3: Language Adapters (Type Producers)

Each language adapter is responsible for:

1. Running the language's type checker (or a fast approximation)
2. Producing the universal type spec format
3. Mapping source positions to type spec entries

For Python, the adapter wraps Pyrefly/ty. For other languages:

| Language   | Type Source                | Notes |
|------------|----------------------------|-------|
| Python     | Pyrefly / ty (Rust crate)  | Direct Rust integration |
| Rust       | rust-analyzer / rustc API  | Via `ra_ide` crate or cargo metadata |
| TypeScript | tsc --declaration          | Parse .d.ts output |
| Go         | `go/types` package         | Subprocess, JSON output |
| Java       | Eclipse JDT / javac API    | Subprocess |
| C/C++      | clangd / compile_commands  | LSP or libclang |

The adapter doesn't need to be in-process. The type spec is a
serialization format — produce it however you want, emend consumes it.

### The Unification Engine

For **cross-language** type queries and for **inference within a file
where the language adapter provides partial info**, emend needs its own
lightweight type inference engine. This is where it gets fun.

**Existing Rust crates for this:**

- **[`ena`](https://crates.io/crates/ena)** — The union-find crate used
  by rustc and Chalk. Provides efficient unification tables. This is the
  foundational data structure.

- **[`rusttyc`](https://crates.io/crates/rusttyc)** — A higher-level
  library built on `ena` that provides lattice-based type checking with
  Hindley-Milner-style inference. You define a `Variant` trait with a
  `meet` operation (greatest lower bound), and `rusttyc` handles
  constraint collection, unification, and error reporting. This is almost
  exactly what we'd need.

- **[Chalk](https://github.com/rust-lang/chalk)** — Rust's trait solver,
  extracted as a library. Overkill for our needs but proves the model
  works.

**How it fits together:**

```rust
use rusttyc::{TypeChecker, TcKey, Variant};

/// emend's universal type representation
enum EmendType {
    Unknown,                              // top of lattice
    Primitive(PrimitiveType),             // str, int, etc.
    Named(String),                        // User, Connection
    Parameterized(String, Vec<EmendType>),// List<int>, Dict<str, User>
    Function(Vec<EmendType>, Box<EmendType>), // (params) -> return
    Union(Vec<EmendType>),                // str | int
    Protocol(String, Vec<Signature>),     // structural type
}

impl Variant for EmendType {
    type Err = TypeError;
    fn top() -> Self { EmendType::Unknown }
    fn meet(lhs: Partial<Self>, rhs: Partial<Self>) -> Result<Partial<Self>, TypeError> {
        // Greatest lower bound: Unknown meets anything = that thing.
        // Named("X") meets Named("X") = Named("X").
        // Named("X") meets Named("Y") = error (or Union if we want).
        // Parameterized("List", [T]) meets Parameterized("List", [U]) =
        //   Parameterized("List", [meet(T, U)]).
        ...
    }
}
```

For each file, emend would:

1. Parse with tree-sitter (get syntax)
2. Load or compute the type spec (get types from language adapter)
3. Build a `TypeChecker` and assign `TcKey`s to AST nodes
4. Import constraints from the type spec
5. Run unification
6. Answer queries: "is the expression at line 42 of type `Connection`?"

### What This Enables

**Cross-language refactoring.** Rename a Python function and update its
TypeScript callers (in a monorepo with a shared API contract). The type
spec format acts as the bridge — both languages agree on the function's
type signature.

**Polyglot dead code detection.** A Go function is only called from
Python via gRPC. The gRPC proto defines the type contract. emend can
trace the reference through the proto definition.

**Universal pattern matching.** One pattern language, many target
languages:

```bash
# Find all functions returning Optional<T> in any language
emend search '$f:returns[Optional<$T>]' src/ --lang python,rust,typescript

# Find all expressions of type Connection across the monorepo
emend search '$x:type[Connection]' . --lang python,go
```

### Implementation Phases

**Phase A: Multilingual syntax matching (no types).**

Generalize emend_core to load tree-sitter grammars by language. Add
language spec YAML files mapping node kinds. Get `emend search '$f($...)'
src/ --lang rust` working — pure structural matching, no types.

This is already close to feasible: emend_core's `matcher.rs` operates on
tree-sitter `Node` types. The main work is parameterizing it on the
grammar and node-kind mapping instead of hardcoding Python.

Rough scope: ~2 weeks of Rust work. The pattern IR (`PatternNode` in
`matcher.rs`) needs a language-generic representation, and
`find_pattern_in_files` needs a grammar parameter.

**Phase B: Type spec format + Python adapter.**

Define the type spec format (probably as a Rust struct hierarchy
serialized to JSON/MessagePack). Build the Python adapter using Pyrefly.
Wire it into emend's query engine so `$x:type[Foo]` constraints work for
Python.

This is the hardest phase. The type spec format needs to be expressive
enough for real queries but simple enough to implement adapters for. The
`rusttyc` crate does the heavy lifting for unification.

**Phase C: Agent-generated language adapters.**

New language support is added by LLM agents, not human contributors.
The inputs to the agent are well-defined and self-contained:

1. The type spec format definition (a schema)
2. The tree-sitter grammar for the target language (node kinds, structure)
3. The language's type checker CLI and output format (or LSP)
4. A few reference adapters (Python, Rust) as examples

An agent can read the tree-sitter grammar, understand the node-kind
mapping, read the type checker's output format, and generate both the
language spec YAML and the adapter code. This is a **bounded,
well-specified code generation task** — exactly the kind of thing agents
are good at.

The workflow:

```bash
# Human says:
emend adapter generate --lang kotlin

# Agent (via emend's own infrastructure):
# 1. Fetches tree-sitter-kotlin grammar, inspects node kinds
# 2. Reads type spec schema + Python/Rust adapter as examples
# 3. Identifies Kotlin's type checker (kotlinc with -Xrender-internal-diagnostic-names,
#    or the kotlin LSP)
# 4. Generates lang_specs/kotlin.yaml (node-kind mapping)
# 5. Generates adapters/kotlin.rs (type checker output → type spec)
# 6. Generates test cases from the existing test patterns
# 7. Runs tests, iterates
```

This changes the scaling model: instead of "N languages requires N
human contributors," it's "N languages requires one good prompt and
a CI pipeline that validates the output." Adding a new language
becomes a PR that an agent generates and a human reviews.

The type spec format should be designed with this in mind — clear,
regular, well-documented, with a JSON schema. The reference adapters
should be heavily commented explaining *why* each mapping exists, not
just *what* it is. Agents learn from examples; good examples compound.

Even the language spec YAML files (the tree-sitter node-kind mappings)
are agent-generatable. A tree-sitter grammar is a JSON file. An agent
can read it, identify which node kinds correspond to "function definition,"
"class definition," "call expression," etc., and produce the mapping.
The grammar *is* the documentation.

**Phase D: Cross-language operations.**

Monorepo support: resolve references across language boundaries using
shared type specs (from protobuf/gRPC definitions, OpenAPI specs, or
shared type declaration files). Agents can also generate the bridge
adapters that map proto/OpenAPI definitions to type specs — another
bounded code generation task.

## Open Questions

1. **How much type system do we actually need?** Full Hindley-Milner
   inference is probably overkill. Most useful queries are "is this
   expression of type X" — which is a subtype check, not inference. Maybe
   the unification engine is simpler than described above.

2. **Incremental computation.** Pyrefly uses Salsa-style incremental
   recomputation. If emend embeds Pyrefly, do we get incrementality for
   free? Or do we need our own caching layer?

3. **Tree-sitter vs language-native AST.** Tree-sitter gives us a
   universal parser, but its ASTs are concrete syntax trees (not
   abstract). The node kinds are grammar-specific. The language spec
   mapping is an abstraction layer that could get leaky for complex
   patterns. Is there a better universal AST representation?

4. **Refactoring precision.** Tree-sitter can *find* code across
   languages, but can it *transform* it precisely? LibCST's strength is
   whitespace-preserving transformation. Tree-sitter edit operations are
   lower-level (byte ranges). Do we need a LibCST equivalent per
   language, or is byte-range replacement sufficient?

5. **Adapter quality validation.** Agent-generated adapters need automated
   validation. A test suite of "canonical queries" per language — find a
   function, find a class, find a call, find an expression of type X —
   that the adapter must pass. The test inputs can be generated from
   each language's tree-sitter corpus (the `corpus/` directory that every
   tree-sitter grammar ships with). An agent generates the adapter, CI
   runs the canonical tests, failures get fed back to the agent for
   iteration.
