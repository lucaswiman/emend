# Future Work for emend

## Easy Wins

### `FullRepoManager` + `FullyQualifiedNameProvider`

The `graph`, `callers`, and `refs` commands currently use per-file scope analysis.
Switching to a full-repository qualified name provider would improve cross-file
name resolution accuracy (e.g. resolving re-exported names through intermediate
modules and handling package-level `__init__.py` re-exports).

## Longer-Term Ideas

- **Deeper Rust acceleration** — The `emend-core` crate already handles file scanning and content pre-filtering. Extending it to cover hot paths in pattern matching and scope analysis could yield further 10-50x speedup on large projects.
