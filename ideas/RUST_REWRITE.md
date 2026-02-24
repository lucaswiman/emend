# Partial Rust Core for Emend

## Goal

Keep emend's CLI, command definitions, selector parsing, and output formatting
in Python, but accelerate the compute-heavy inner loops by calling into a Rust
library via PyO3. The Rust core handles parsing, pattern matching, AST
traversal, and metadata computation. Python remains the orchestration layer.

## Why Partial Rather Than Full Rewrite

Emend is ~9,000 lines of Python. Roughly 40% is CLI glue, output formatting,
and configuration handling that gains nothing from Rust. The remaining 60% is
AST traversal, pattern compilation, and metadata computation -- the actual hot
paths.

A full rewrite would take months and sacrifice the rapid iteration speed that
Python provides for the non-performance-critical parts. A partial rewrite
targets the 60% that matters while keeping the 40% that doesn't in Python.

Reference point: ruff took this approach successfully. Its CLI, configuration
loading, and rule definitions are in Rust, but it didn't need to replace a
Python codebase -- it was Rust from the start. Our situation is different: we
have a working Python codebase and want to surgically replace the slow parts.

## Current Architecture

```
cli.py (1,200 lines)
  │
  ├── transform.py (4,900 lines) ── Core engine
  │     ├── visit_project()         ── File iteration + parse + metadata
  │     ├── PatternFinder            ── Pattern matching (visitor)
  │     ├── PatternReplacer          ── Pattern replacement (transformer)
  │     ├── _ReferenceFinder         ── Scope-aware reference finding
  │     ├── _SymbolRenamer           ── Scope-aware renaming
  │     ├── _CallerFilter            ── Call site detection
  │     ├── _CalleeCollector         ── Callee analysis
  │     ├── find_pattern()           ── Cross-project pattern search
  │     ├── replace_pattern()        ── Cross-project pattern replace
  │     ├── find_references()        ── Cross-project reference finding
  │     └── rename_symbol()          ── Cross-project renaming
  │
  ├── pattern.py (1,145 lines) ── Pattern compilation
  │     ├── compile_pattern_to_matcher()
  │     └── MetaVar handling
  │
  ├── query.py (530 lines) ── Symbol collection
  │     └── _SymbolCollector
  │
  ├── ast_utils.py (~200 lines) ── AST utilities
  │     └── _NestedDefinitionVisitor
  │
  ├── lint.py (281 lines) ── Lint engine
  │
  └── component_selector.py (235 lines) ── Selector parsing
```

### What's Slow (Profiled)

The dominant costs in a cross-project operation like `find_references`:

1. **QualifiedNameProvider** (~40% of time): LibCST builds full scope chains,
   resolves every name in every file. This is 3-5x the cost of just parsing.

2. **cst.parse_module()** (~25% of time): Parsing Python source into the CST.

3. **Visitor traversal** (~15% of time): Walking every node in the tree.

4. **File I/O** (~10% of time): Reading every `.py` file in the project.

5. **MetadataWrapper setup** (~10% of time): Building metadata provider
   infrastructure for each file.

For pattern matching (`find_pattern`), the dominant cost shifts to:

1. **cst.parse_module()** (~35%): Parsing
2. **m.matches() / m.extract()** (~30%): Checking every node against the pattern
3. **File I/O** (~20%): Reading files
4. **PositionProvider** (~15%): Position metadata

## Proposed Rust/Python Boundary

### Principle: Rust Parses and Traverses, Python Orchestrates

```
Python                          Rust (via PyO3)
──────                          ──────────────
cli.py                          emend_core.so
  │                               │
  │  file paths + options         │
  ├──────────────────────────────>│
  │                               ├── Parse files (parallel)
  │                               ├── Build ASTs
  │                               ├── Compute metadata
  │                               ├── Run visitors/matchers
  │                               ├── Generate diffs
  │  results (matches, refs,      │
  │  diffs, symbols)              │
  │<──────────────────────────────┤
  │                               │
  ├── Format output               │
  ├── Print to stdout             │
  └── Apply changes (if --apply)  │
```

### What Moves to Rust

| Component | Current Python | Rust Replacement |
|-----------|---------------|-----------------|
| Python parser | `cst.parse_module()` | tree-sitter-python or custom |
| Pattern matcher | `m.matches()` / `m.extract()` | Custom matcher on Rust AST |
| Pattern compiler | `compile_pattern_to_matcher()` | Compile to Rust matcher IR |
| Reference finder | `_ReferenceFinder` + QualifiedNameProvider | Scope analysis in Rust |
| Symbol collector | `_SymbolCollector` | Walk AST in Rust |
| File iteration | `visit_project()` inner loop | Parallel file processing |
| Diff generation | `difflib.unified_diff` | `similar` crate |

### What Stays in Python

| Component | Reason |
|-----------|--------|
| `cli.py` command definitions | Typer decorators, argument parsing -- no hot path |
| `component_selector.py` | Lark grammar parsing, called once per command |
| `lint.py` rule loading | YAML parsing, rule configuration |
| Output formatting | Pretty printing, tree rendering |
| `batch.py` operation file parsing | YAML/JSON loading |
| `--apply` file writing | I/O-bound, not compute-bound |
| Error handling / validation | Better error messages in Python |

### Data Structures at the Boundary

The Rust library exposes these Python types via PyO3:

```python
# Returned by pattern matching
class Match:
    file: str
    line: int
    column: int
    end_line: int
    end_column: int
    matched_code: str
    captures: dict[str, str]  # metavar name → captured code

# Returned by reference finding
class Reference:
    file: str
    line: int
    column: int
    kind: str  # "definition", "import", "usage", "call"
    context: str  # the line of code containing the reference
    is_write: bool

# Returned by symbol collection
class Symbol:
    name: str
    qualified_name: str
    kind: str  # "function", "class", "method", "variable"
    file: str
    line: int
    end_line: int
    decorators: list[str]
    parameters: str | None
    return_type: str | None
    parent: str | None

# Returned by replace / rename / edit operations
class FileChange:
    file: str
    original: str
    modified: str
    diff: str  # unified diff
```

### API Surface

```python
import emend_core

# Pattern operations
matches: list[Match] = emend_core.find_pattern(
    pattern="$X.save()",
    files=["src/models.py", "src/views.py"],  # or a directory
    inside="def $func(...):",       # optional constraint
    not_inside="def test_$name(...):",  # optional constraint
)

replaced: list[FileChange] = emend_core.replace_pattern(
    pattern="$X.save()",
    replacement="$X.save(commit=True)",
    files=["src/"],
)

# Reference operations
refs: list[Reference] = emend_core.find_references(
    symbol="src/models.py::User.save",
    project_root=".",
    writes_only=False,
    reads_only=False,
)

changes: list[FileChange] = emend_core.rename_symbol(
    symbol="src/models.py::User",
    new_name="Account",
    project_root=".",
    include_docs=False,
)

# Symbol operations
symbols: list[Symbol] = emend_core.collect_symbols(
    files=["src/models.py"],
    max_depth=2,
)

# Index operations (for daemon mode)
index = emend_core.ProjectIndex(".")
index.build()                    # full build
index.update_file("src/foo.py")  # incremental update
symbols = index.symbols_named("User")
refs = index.find_references("src/models.py::User")
```

## Rust Internals

### Parser Choice: tree-sitter-python

tree-sitter-python is the recommended parser for the Rust core:

- **Speed**: 10-100x faster than LibCST's pure-Python parser
- **Incremental**: Can re-parse only changed regions of a file
- **Battle-tested**: Used in production by GitHub, Neovim, Helix, Zed
- **Rust-native**: First-class Rust bindings via `tree-sitter` crate

The main trade-off: tree-sitter produces a concrete syntax tree, but with a
different node structure than LibCST. We need a mapping layer. However, for
emend's use cases (pattern matching, name resolution, symbol collection), we
don't need the full richness of LibCST's node types -- we need positions,
names, and structural relationships.

#### Alternative: rust-analyzer style custom parser

A custom hand-written parser (like rust-analyzer's `rowan`-based parser) would
give us more control over the CST representation. However, the implementation
cost is very high and tree-sitter-python is already excellent.

### Scope Analysis

The most complex piece is replacing LibCST's `QualifiedNameProvider`, which
resolves names to their fully-qualified forms by analyzing imports and scope
chains.

Approach: Build a simplified scope resolver in Rust that handles the cases
emend actually needs:

1. **Import tracking**: Parse all `import` and `from ... import` statements
   to build a name → module mapping
2. **Scope chain**: Track function/class nesting to resolve local names
3. **Qualified name resolution**: Given a `Name` node, trace it through
   imports and scope to get its fully-qualified name

This doesn't need to handle every Python edge case (star imports, dynamic
imports, `__all__`, etc.) -- just the common patterns that LibCST's
QualifiedNameProvider handles. Emend already silently skips files that fail
to parse, so graceful degradation is built into the design.

### Pattern Matching Engine

The current pattern matching works in two phases:

1. **Compile**: Pattern string → LibCST matcher (via Lark parser + recursive
   converter)
2. **Match**: Visit every AST node, check if it matches the compiled matcher

In Rust, this becomes:

1. **Compile**: Pattern string → Rust `PatternMatcher` enum
   - Parse the pattern as Python code (via tree-sitter)
   - Identify metavariables (`$X`, `$...args`, `$_`)
   - Build a structural matcher that can be evaluated against AST nodes
2. **Match**: Walk the tree-sitter AST, evaluate the matcher at each node
   - Metavar binding: when `$X` first matches a subtree, bind it; subsequent
     occurrences must match the same code
   - Ellipsis captures: `$...args` matches zero or more items in a sequence
   - Type constraints: `$X:expr`, `$X:stmt` etc.

The Rust matcher can use several optimizations unavailable in Python:

- **Root node type filtering**: If the pattern is `$X.save()`, only check
  `call` nodes (not every node)
- **Name pre-filtering**: Extract literal names from the pattern (e.g. "save")
  and skip files/nodes that don't contain them
- **Parallel matching**: Process multiple files on separate threads
- **SIMD string matching**: Use `memchr` crate for fast literal scanning

### Parallel File Processing

The biggest architectural win: process files in parallel.

```rust
use rayon::prelude::*;

fn find_pattern(pattern: &Pattern, files: &[PathBuf]) -> Vec<Match> {
    files.par_iter()
        .filter_map(|file| {
            let content = std::fs::read_to_string(file).ok()?;

            // Fast pre-filter: check if file could possibly match
            if !pattern.could_match(&content) {
                return None;
            }

            let tree = parser.parse(&content, None)?;
            let matches = pattern.find_all(&tree, &content);

            if matches.is_empty() {
                None
            } else {
                Some(matches)
            }
        })
        .flatten()
        .collect()
}
```

Python's GIL prevents true parallelism for CPU-bound work. Rust's `rayon`
gives us work-stealing parallelism across all cores with zero overhead.

On a 8-core machine, this alone provides ~4-6x speedup for cross-project
operations (not 8x due to I/O contention and load imbalance).

## Implementation Phases

### Phase 1: Rust Pattern Matcher

**Scope**: Replace `find_pattern()` and `replace_pattern()` for the common case
(patterns without `--inside`/`--not-inside` constraints).

**Crate structure**:
```
emend-core/
  Cargo.toml
  src/
    lib.rs           # PyO3 module definition
    parser.rs        # tree-sitter-python wrapper
    pattern.rs       # Pattern compilation
    matcher.rs       # Pattern matching engine
    replacer.rs      # Pattern replacement
    types.rs         # Match, FileChange types
  python/
    emend_core/
      __init__.pyi   # Type stubs
```

**Build**: Use `maturin` for building the PyO3 extension module. This
integrates with pip/setuptools and produces a wheel.

**Integration with emend**:

```python
# In transform.py, add a fast path:

def find_pattern(pattern_str, ...):
    try:
        import emend_core
        if _can_use_rust_path(pattern_str, inside, not_inside, scope):
            return emend_core.find_pattern(
                pattern=pattern_str,
                files=_collect_python_files(project_root),
            )
    except ImportError:
        pass  # Fall back to pure Python

    # Existing pure-Python implementation
    ...
```

This makes the Rust core an optional accelerator. If `emend_core` is not
installed (e.g. on an unsupported platform), emend falls back to pure Python
with zero behavior change.

**Expected speedup**: 10-20x for `find_pattern` on large projects.

### Phase 2: Parallel File Processing + Symbol Collection

**Scope**: Add parallel file I/O and parsing. Implement `collect_symbols()`
in Rust for fast `search`/`lookup`.

**New in Rust**:
- `visit_project_parallel()`: Read + parse + visit files across threads
- `collect_symbols()`: Walk AST to extract symbol definitions
- `build_import_graph()`: Extract imports from all files

**Integration**:
```python
# In query.py:
def query_symbols(selector, ...):
    try:
        import emend_core
        symbols = emend_core.collect_symbols(
            files=_collect_python_files(project_root),
            max_depth=depth,
        )
        # Filter in Python (flexible, rarely the bottleneck)
        return _filter_symbols(symbols, selector)
    except ImportError:
        pass
    # Existing implementation
    ...
```

**Expected speedup**: 5-10x for symbol operations, plus foundation for Phase 3.

### Phase 3: Scope-Aware Reference Finding

**Scope**: Implement the equivalent of LibCST's `QualifiedNameProvider` in
Rust. This is the most complex phase but also the highest-value one, since
scope-aware operations (`refs`, `rename`, `callers`) are the slowest commands.

**New in Rust**:
- `ScopeResolver`: Analyze imports and scope chains per file
- `find_references()`: Cross-project reference finding with scope awareness
- `rename_symbol()`: Cross-project renaming with scope-aware replacement

**Complexity**: This requires understanding Python's scoping rules:
- Module-level names (imports, assignments, function/class definitions)
- Function-local names (parameters, assignments, nested functions)
- Class scope (methods, class variables)
- Nonlocal/global declarations
- Star imports (can be handled conservatively)

For emend's purposes, we don't need 100% correctness on edge cases. LibCST's
QualifiedNameProvider itself has limitations (e.g. dynamic imports,
runtime-computed names). A Rust implementation that handles the same common
cases is sufficient.

**Expected speedup**: 10-30x for reference and rename operations.

### Phase 4: Constrained Pattern Matching + Full Feature Parity

**Scope**: Support `--inside`/`--not-inside` constraints, `--scope-local`,
`--where` filters, and the lint engine in Rust.

**New in Rust**:
- `ConstrainedMatcher`: Pattern matching with structural constraints
- `ScopedMatcher`: Pattern matching within a specific symbol's scope
- `lint_files()`: Run multiple pattern rules in a single pass
- `batch_operations()`: Execute multiple find/replace operations efficiently

**Key optimization for lint**: Currently, each lint rule triggers a separate
`find_pattern()` call, scanning all files per rule. In Rust, we can compile
all rules into a single multi-pattern matcher and scan each file once,
checking all rules simultaneously. This changes lint from O(files * rules)
to O(files * max_pattern_complexity).

**Expected speedup**: 15-50x for lint with multiple rules.

## Risk Assessment

### Low Risk

- **PyO3 maturity**: PyO3 is production-grade, used by pydantic, polars,
  cryptography, and many other major Python packages.
- **tree-sitter-python maturity**: Used in production by GitHub, Neovim, etc.
- **Optional dependency**: Rust core is an accelerator, not a requirement.
  Pure Python fallback always available.
- **maturin build system**: Well-supported, produces standard wheels.

### Medium Risk

- **AST compatibility**: tree-sitter's CST structure differs from LibCST's.
  Pattern matching semantics must be carefully translated. Extensive test
  suite (existing tests serve as the spec) mitigates this.
- **Scope analysis correctness**: Matching LibCST's QualifiedNameProvider
  behavior exactly is hard. Start with conservative matching (if unsure,
  fall back to Python) and tighten over time.
- **Distribution**: Need to build wheels for multiple platforms (Linux x86_64,
  Linux aarch64, macOS x86_64, macOS aarch64, Windows). `maturin` +
  `cibuildwheel` handle this but it's operational overhead.

### High Risk

- **Feature drift**: As emend evolves in Python, keeping the Rust core in
  sync requires discipline. Mitigated by: comprehensive tests, optional
  fallback (new features can ship Python-only first, then get Rust
  acceleration later).

## Performance Expectations

### Baseline (Current Pure Python)

| Operation | 100 files | 1,000 files | 10,000 files |
|-----------|-----------|-------------|--------------|
| `search "MyClass"` | 0.5s | 2s | 15s |
| `find "$X.save()"` | 1s | 4s | 30s |
| `refs ::MyClass` | 2s | 8s | 60s |
| `rename ::MyClass --to Foo` | 3s | 12s | 90s |
| `lint` (10 rules) | 5s | 25s | 200s |

### With Rust Core (All Phases)

| Operation | 100 files | 1,000 files | 10,000 files |
|-----------|-----------|-------------|--------------|
| `search "MyClass"` | 10ms | 50ms | 300ms |
| `find "$X.save()"` | 20ms | 100ms | 500ms |
| `refs ::MyClass` | 50ms | 200ms | 1s |
| `rename ::MyClass --to Foo` | 100ms | 300ms | 2s |
| `lint` (10 rules) | 50ms | 200ms | 1s |

These estimates assume 8-core machine, warm filesystem cache, and all four
phases implemented. Actual numbers will vary but the order-of-magnitude
improvement (10-50x) is realistic based on comparisons with similar tools
(ruff achieves 10-100x over flake8/pylint).

## Compatibility Strategy

### Gradual Migration

```python
# Every Rust-accelerated function follows this pattern:

def find_pattern(pattern_str, project_root, **kwargs):
    # Try Rust fast path
    if _rust_available and _rust_supports(pattern_str, **kwargs):
        return _rust_find_pattern(pattern_str, project_root, **kwargs)

    # Fall back to pure Python
    return _python_find_pattern(pattern_str, project_root, **kwargs)
```

### Testing Strategy

The existing test suite is the compatibility spec. Every test that passes
against the Python implementation must also pass against the Rust
implementation:

```python
@pytest.fixture(params=["python", "rust"])
def backend(request, monkeypatch):
    if request.param == "rust":
        try:
            import emend_core
        except ImportError:
            pytest.skip("emend_core not installed")
        monkeypatch.setattr(transform, "_rust_available", True)
    else:
        monkeypatch.setattr(transform, "_rust_available", False)
    return request.param
```

This runs every test twice: once with the Python backend, once with the Rust
backend. Any divergence is a bug.

### Distribution

- **pip install emend**: Pure Python, works everywhere
- **pip install emend[fast]**: Includes Rust-accelerated `emend_core`
- Wheels built via GitHub Actions using `maturin` + `cibuildwheel`
- Platforms: Linux (x86_64, aarch64), macOS (x86_64, aarch64), Windows (x86_64)

## Alternatives Considered

### PyPy

Running emend under PyPy could provide 2-5x speedup with zero code changes.
However:
- LibCST doesn't officially support PyPy
- MetadataWrapper relies on CPython-specific internals
- 2-5x is not enough for editor-integration latency targets

### Cython

Compiling hot paths with Cython could provide 2-10x speedup:
- Requires type annotations on hot paths
- Still limited by LibCST's Python-level abstractions
- Doesn't enable parallelism (still GIL-bound)
- Good intermediate step but ceiling is lower than Rust

### Multiprocessing

Using `multiprocessing` or `concurrent.futures.ProcessPoolExecutor`:
- Enables parallelism without Rust
- But: high per-process overhead (each process re-imports LibCST)
- Memory-intensive (each process gets full copy of module caches)
- IPC serialization overhead for large ASTs
- Practical speedup: 2-4x (not 8x due to overhead)
- Could be useful as a Python-only stopgap

### Keeping Pure Python + Caching Only

See CACHING.md for the pure-Python caching approach. This is complementary
to the Rust rewrite, not an alternative. Caching eliminates repeated work
across invocations; Rust makes each invocation faster. The best result comes
from combining both: a Rust core that processes files in parallel, backed by
a persistent index that avoids re-processing unchanged files.
