# Future Work for emend

## Easy Wins

### `$X:stmt` Type Constraint

Currently accepted by the grammar but not fully implemented. The pattern compiler
replaces `$X` with `__META_X__` (a valid identifier) to parse as Python code, but
this is an expression and cannot appear at statement-level positions. Fix: use a
template-based approach where `$BODY:stmt` is restricted to compound statement
body positions and matched against `body: Sequence[BaseStatement]` directly by
the compound statement matcher.

### `FullRepoManager` + `FullyQualifiedNameProvider`

The `graph`, `callers`, and `refs` commands currently use per-file
`QualifiedNameProvider`. Switching to `FullRepoManager` with
`FullyQualifiedNameProvider` would improve cross-file name resolution accuracy
(e.g. resolving re-exported names through intermediate modules).

### `--calls-only` Optimization for `refs`

The `refs --calls-only` flag exists but still scans all reference types
internally. Could short-circuit by only checking `Call` parent nodes, skipping
the full reference collection.

## Longer-Term Ideas

- **Persistent index / caching** — Cache parsed modules and symbol indexes across invocations for faster repeated operations on large projects.
- **Rust-accelerated core** — Replace hot paths (parsing, pattern matching, scope analysis) with a PyO3 Rust extension for 10-50x speedup on large projects.
- **Semantic type constraints** — Use mypy/pyright or LibCST's `TypeInferenceProvider` for type-aware pattern matching (e.g. `$X:int` checks inferred type, not AST node type).
