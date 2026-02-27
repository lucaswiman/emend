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
/// Produce a type spec for a file: a map of (line, col) → type descriptor.
/// The type descriptor is a tree structure (not a string) so the pattern
/// matcher can operate on it directly.
#[pyfunction]
fn type_spec_for_file(source: &str, project_root: &str) -> PyResult<TypeSpec> { ... }

/// TypeSpec is the output — position-indexed type descriptors.
/// No inference engine on emend's side. Pyrefly did the inference.
/// emend just indexes the results by source position.
struct TypeSpec {
    /// Map from (line, col) to the type descriptor at that position
    expressions: HashMap<(usize, usize), TypeDescriptor>,
    /// Subtype relationships: "List" → ["Iterable", "Sized", ...]
    /// Used for subtype pattern matching (`:type[Iterable[$T]]`)
    supertypes: HashMap<String, Vec<TypeDescriptor>>,
}

/// A type descriptor is a tree — structurally identical to a PatternNode.
/// This is the key insight: type matching reuses the pattern matcher.
enum TypeDescriptor {
    Named(String),                              // Connection
    Parameterized(String, Vec<TypeDescriptor>), // List<int>
    Function(Vec<TypeDescriptor>, Box<TypeDescriptor>), // (str, int) -> User
    Union(Vec<TypeDescriptor>),                 // str | None
    Unknown,                                    // couldn't infer
}
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

### Why Not LSP?

LSP seems like the obvious universal layer — every language has an LSP
server. But LSP's type information is **display strings, not structured
data**. `textDocument/hover` returns `MarkupContent` (markdown).
`textDocument/signatureHelp` embeds types in label strings. There is no
LSP method that returns "this is a generic type `Map` with type argument
`String` and type argument `List<Int>`" as structured data.

You'd have to parse type-display strings from every language's syntax,
which is the per-language engineering you're trying to avoid. And LSP
is position-based (one query per cursor location) — querying types for
every match in a codebase-wide search would be prohibitively slow.

SCIP (Sourcegraph's Code Intelligence Protocol) has the same problem:
types are stored as rendered strings in `signature_documentation`, not
as queryable trees. Good for code navigation, useless for type pattern
matching.

### Layer 0: Tree-Sitter Type Extraction (No Checker Required)

Before any language adapter runs, tree-sitter can already extract
**declared** types from annotations:

```python
# Tree-sitter can see these types without any type checker
def connect(host: str, port: int) -> Connection: ...
x: list[int] = [1, 2, 3]
```

```typescript
// Same for TypeScript
function connect(host: string, port: number): Connection { ... }
const x: number[] = [1, 2, 3];
```

```go
// And Go
func connect(host string, port int) *Connection { ... }
var x []int = []int{1, 2, 3}
```

The tree-sitter grammar already parses these annotations into typed
nodes (`type_identifier`, `generic_type`, `return_type`, etc.). The
language spec YAML maps these to `TypeDescriptor` trees. No type
checker needed.

This gives you **declared types for free across all 250+ tree-sitter
languages**. It won't resolve inferred types (what's the type of
`x = foo()`?), but it covers:

- Function parameter types (from annotations)
- Return types (from annotations)
- Variable types (from explicit annotations)
- Class/struct field types
- Generic type arguments (from syntax)

For many queries this is enough. `$f:returns[Optional[$T]]` can be
answered purely from syntax if the return type is annotated. Only
`$x:type[Connection]` on an unannotated variable needs the type checker.

Between pure syntax and a full type checker, there's a **Layer 0.5**
that covers most practical queries without invoking any external tool:

**Simple assignment tracking.** `x = Connection()` → `x` is a
`Connection`. `y = x.get_pool()` → if `get_pool` has a return type
annotation, `y` has that type. This is one-hop flow analysis —
follow assignments to constructors and annotated-return-type calls.
No generics resolution, no constraint solving, no understanding of
variance or lifetimes. Just "what did this name get assigned to?"

This is significantly simpler than what Pyrefly or tsc do. It doesn't
need to understand Rust lifetimes, TypeScript conditional types, or
Python's `@overload`. It needs to answer "what is this thing, or what
could it be?" — which is a name resolution + one-hop-flow problem,
not a type theory problem.

#### Can Layer 0.5 cross module boundaries?

Yes — with a caveat. The tracking algorithm is generic, but **module
resolution rules are language-specific data**.

Layer 0.5 cross-module tracking requires three operations:

1. **Resolve an import to a file.** `from foo.bar import Baz` →
   where is `foo/bar.py`? `use crate::db::Connection` → where is
   `src/db.rs`?

2. **Find a symbol in that file.** Look up `Baz` in the file's
   top-level definitions. (Tree-sitter, language-generic.)

3. **Read the symbol's type information.** Its return type annotation,
   its class definition, its field types. (Tree-sitter, generic.)

Steps 2 and 3 are language-generic — they use tree-sitter node kinds
from the language spec YAML. Step 1 is language-specific, but it's
**rules, not code**:

```yaml
# lang_specs/python.yaml
module_resolution:
  separator: "."
  file_patterns: ["{path}.py", "{path}/__init__.py"]
  root_markers: ["pyproject.toml", "setup.py", "setup.cfg"]
  src_layout: true        # detect src/ prefix via pyproject.toml

# lang_specs/rust.yaml
module_resolution:
  separator: "::"
  file_patterns: ["{path}.rs", "{path}/mod.rs"]
  root_markers: ["Cargo.toml"]
  strip_prefix: "crate"   # crate::foo::bar → foo/bar

# lang_specs/typescript.yaml
module_resolution:
  relative: true           # './foo/bar', not 'foo.bar'
  file_patterns: ["{path}.ts", "{path}.tsx", "{path}/index.ts"]
  config_file: "tsconfig.json"   # path aliases
  node_modules: true       # resolve from node_modules/

# lang_specs/go.yaml
module_resolution:
  separator: "/"
  root_markers: ["go.mod"]
  package_scope: true      # imports are packages, not files

# lang_specs/java.yaml
module_resolution:
  separator: "."
  file_patterns: ["{path}.java"]
  root_markers: ["pom.xml", "build.gradle"]
  class_per_file: true     # com.foo.Bar → com/foo/Bar.java
```

The resolution *algorithm* is generic:

```
resolve_import(import_node, language_spec) → file_path:
  1. Extract the module path string from the import node
     (tree-sitter, node kinds from language spec)
  2. Strip language-specific prefixes (crate::, ./, etc.)
  3. Split on the language's separator
  4. Try each file_pattern against the project root
  5. Return the first match
```

This is the same structure as emend's existing Python-specific
`_file_to_module()` and `_files_importing_module()`, but
parameterized on the language spec instead of hardcoded.

With cross-module resolution, Layer 0.5 can do multi-hop tracking:

```python
# file: src/db.py
def get_connection() -> Connection: ...

# file: src/app.py
from db import get_connection
conn = get_connection()    # Layer 0.5: conn is Connection
                           # (followed import → found return annotation)
pool = conn.get_pool()     # Layer 0.5: if Connection.get_pool has
                           # a return annotation, pool has that type
```

The tracking chain is: assignment → import resolution → symbol lookup
→ return type annotation → repeat. Each step uses tree-sitter +
language spec data. No language-specific *code* beyond the resolution
rules.

**What Layer 0.5 can't do across modules:**

- Resolve re-exports through complex `__init__.py` / `index.ts`
  re-export chains (these get arbitrarily hairy)
- Track types through generic instantiation (`get_or_create[User]()`)
- Resolve types through dynamic dispatch, decorators, metaclasses
- Handle conditional imports, lazy imports, star imports

These are where Layer 1 (native type checker) earns its keep.

```
Layer 0:   tree-sitter annotations    → declared types         (free)
Layer 0.5: assignment + import tracking → cross-module types    (language spec data)
Layer 1:   native type checker         → full inferred types    (per-language adapter)
```

| Layer | What you get | Cost | Coverage |
|-------|-------------|------|----------|
| 0: tree-sitter | Annotated types | Free (already parsed) | Typed codebases |
| 0.5: assignment + imports | Cross-module types from annotations | Language spec YAML | Most variables |
| 1: native type checker | Full inference, generics, subtyping | Per-language adapter | Everything |

Layer 0 + 0.5 together cover the vast majority of practical type
queries. Layer 1 is for when you need deep generic resolution or
subtype reasoning — the premium tier, not the prerequisite.

This is what makes agent-generated adapters viable for 50+ languages
quickly. Layer 0 + 0.5 are language-generic (same algorithm, different
language spec YAML). Layer 1 is the per-language investment, and
it's optional.

### Prior Art: Semgrep Typed Metavariables

Semgrep is the closest prior art. In Java you can write `(String $X)`
to match expressions of type `String`. Semgrep does this with
**lightweight per-language type inference** — tracking declarations,
assignments, and literals.

Where Semgrep hits a wall: deep generics. You can check `String` but
not `Map<String, List<Integer>>`. The lightweight inference doesn't
resolve generic type arguments or track type information through method
chains. This is the inherent limit of trying to infer types yourself
instead of consuming a real type checker's output.

emend's approach (consume native checker output as structured type
specs) avoids this wall entirely. If Pyrefly knows the type is
`dict[str, list[int]]`, emend gets the full parameterized type
descriptor and can match `dict[str, $T]` binding `$T = list[int]`.

The IEEE SCAM 2006 paper on cross-language program analysis explicitly
warns that "unifying or abstracting language semantics is not scalable
because it relies on heavyweight per-language engineering." The right
response: don't unify the semantics, unify the **output format**. Let
each language keep its own semantics. The type spec is a data exchange
format, not a type system.

### You Don't Need a Type Inference Engine

The earlier version of this proposal described building a unification
engine using `rusttyc` / `ena`. That's wrong. It solves a problem emend
doesn't have.

**emend is not inferring types.** Pyrefly, tsc, kotlinc, go/types —
those tools infer types. They've spent person-decades on it. Each
language's type system has enough quirks (Rust lifetimes, TypeScript
conditional types, Kotlin's declaration-site variance, Python's
`@overload`) that a "universal inference engine" would either be too
weak to handle any of them or so complex it reimplements all of them.

**What emend needs is type pattern matching.** Given:
- A concrete type (from the language's type checker): `List[int]`
- A type constraint (from the user's query): `Iterable[$T]`

Answer: does the concrete type satisfy the constraint? Bind metavars.

This is **the same operation emend already does for syntax** — structural
pattern matching on a tree, with metavar binding. The type spec is just
another tree to match against.

#### The three matching operations

**1. Exact match.** `$x:type[Connection]` — is this expression's type
literally `Connection`? Pure string/structural equality on the type
descriptor.

**2. Parameterized match.** `$x:type[List[$T]]` — is this type
`List<something>`? Structural match with metavar binding. Identical to
how `$f($x, $...)` matches syntax.

**3. Subtype match.** `$x:type[Iterable[$T]]` — is this type a subtype
of `Iterable`? This is the only one that needs extra information: the
subtyping relationship. But that information comes FROM the language
adapter. The adapter knows that `List` implements `Iterable` in its
language.

#### How the type spec encodes subtyping

The type spec format should include, for each named type, its
supertypes/protocols/interfaces:

```json
{
  "types": {
    "List": {
      "params": ["T"],
      "supertypes": ["Iterable<T>", "Sized", "Container<T>"]
    }
  },
  "expressions": {
    "src/db.py:42:8": {
      "type": "List<Connection>",
      "resolved": true
    }
  }
}
```

Then matching `$x:type[Iterable[$T]]` against `List[Connection]`:
1. Direct match? `List` vs `Iterable` — no.
2. Check supertypes of `List`: `Iterable<T>`. Yes. Substitute
   `T=Connection`. Bind `$T=Connection`.

This is just tree matching with a fallback lookup. No lattice, no
meet operation, no constraint solver.

#### Why no unification engine

| What unification engines do | What emend needs |
|-|-|
| Infer unknown types from constraints | Types are already known (from language checker) |
| Propagate type information bidirectionally | One-directional: concrete type → matches constraint? |
| Handle polymorphic instantiation | Language checker already monomorphized |
| Solve constraint systems | Single-pair matching |

The `rusttyc` crate is for building type checkers. emend is not a type
checker. It's a type *consumer*. The right analogy: emend is `grep` for
types. `grep` doesn't need to understand the semantics of what it's
matching — it needs a pattern and a target.

#### What this means for the architecture

For each file, emend:

1. Parses with tree-sitter (get syntax) — already works
2. Loads the type spec (from language adapter cache)
3. For each syntactic match, looks up the matched expression's type
   in the type spec (by position)
4. Pattern-matches the type constraint against the concrete type
5. If the constraint has subtype semantics (`:type[Supertype]` vs
   `:exact_type[ConcreteType]`), checks supertypes from the spec

Step 4 can literally reuse `PatternNode` matching from `matcher.rs`,
just operating on type descriptor trees instead of syntax trees.

No new Cargo dependencies. No `ena`. No `rusttyc`. No `Chalk`.
The complexity budget goes into the type spec format and the language
adapters, which is where it belongs.

#### The edge case: partial type information

What if the language checker can't infer a type? (Untyped Python,
`Any`, dynamic dispatch.) The type spec reports `Unknown` for that
position. The pattern match returns `Unknown` — not "yes," not "no."
The query can either skip unknowns (default) or include them
(`--include-unknown`). Graceful degradation, no special machinery.

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

1. ~~**How much type system do we actually need?**~~ Resolved: none.
   emend doesn't infer types; it pattern-matches on type descriptors
   produced by language-native type checkers. The "type system" is a
   data format (the type spec) and a structural matcher (reuse the
   existing `PatternNode` machinery). See "You Don't Need a Type
   Inference Engine" above.

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
