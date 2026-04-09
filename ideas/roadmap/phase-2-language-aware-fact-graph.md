# Phase 2: Language-Aware Fact Graph Building

## Goal

Make `FactGraph.build_from_project()`, `update_files()`, and
`_get_or_build_fact_graph()` language-aware so they correctly populate facts
for TypeScript and Rust source files alongside Python files.

## Why

Today `_get_or_build_fact_graph()` defaults to `language="python"` and the
project file collection only scans `.py` files.  Even though the Rust backend
can parse TS/Rust and build CFGs, the Python orchestration layer doesn't feed
non-Python files into the fact graph.

## Scope

- `src/emend/transform.py` — `_get_or_build_fact_graph()`,
  `_collect_source_files()`, `_collect_source_files_scandir()`,
  `_collect_git_tracked_source_files()`, `visit_project_ts()`,
  `_ensure_index_fresh()`, `warm_caches()`
- `src/emend/fact_graph.py` — `build_from_project()`, `update_files()`,
  `_populate_*` methods
- `src/emend/cli.py` — thread `--language` flag to commands that build fact graphs

## Current Hardcoded Assumptions

| Location | Issue |
|----------|-------|
| `_collect_source_files_scandir()` | Filters by `language="python"` extensions |
| `_collect_git_tracked_source_files()` | Same |
| `visit_project_ts()` | `language: str = "python"` default |
| `_get_or_build_fact_graph()` | No language parameter at all |
| `_ensure_index_fresh()` | `language: str = "python"` default |
| `build_from_project()` | Reads `ext` from file paths but only processes matching files |

## Todo

### Multi-language file collection

- [ ] Add a `detect_project_languages()` function that inspects the project root
  for language markers (`.py` files, `package.json`/`.ts` files, `Cargo.toml`/
  `.rs` files) and returns a set of languages.
- [ ] Update `_collect_source_files()` to accept `languages: set[str]` and
  collect files for all detected languages using their configured extensions.
- [ ] Update `_collect_git_tracked_source_files()` to filter by multiple
  extension sets.

### Fact graph language threading

- [ ] Add a `language` (or `languages`) parameter to `_get_or_build_fact_graph()`
  so it can build facts for the right language(s).
- [ ] Update `build_from_project()` to iterate over files of all detected
  languages, calling `collect_symbols_from_str(content, ext=ext)` and
  `build_cfgs_for_source(content, ext=ext)` with the correct extension.
- [ ] Update `update_files()` to handle mixed-language incremental updates.
- [ ] Verify that `_file_to_module()` works correctly for non-Python paths
  (TypeScript uses `/` separator, Rust uses `::`).

### CLI plumbing

- [ ] Add `--language` option to `emend trace`, `emend deadcode`, `emend refs`,
  `emend graph`, `emend impact`, `emend facts`.  Default to auto-detection.
- [ ] Auto-detection: if a project has both Python and TypeScript files, build
  facts for both.  Single-language projects should work without `--language`.

### Tests

- [ ] Test that `_get_or_build_fact_graph()` on a project with `.ts` files
  produces `SymbolFact` and `CfgEdgeFact` entries for TypeScript symbols.
- [ ] Same for `.rs` files.
- [ ] Test that a mixed Python+TypeScript project builds facts for both
  languages in one graph.
- [ ] Test `_file_to_module()` for TypeScript (`src/utils/helper.ts` →
  `utils/helper`) and Rust (`src/lib.rs` → `crate`, `src/foo/mod.rs` → `foo`).

## Exit Criteria

- `_get_or_build_fact_graph("/path/to/ts-project")` returns a FactGraph with
  TypeScript symbols, calls, references, CFG edges, and imports.
- Same for a Rust project.
- Mixed-language projects get facts for all languages in one graph.
- `_file_to_module()` produces correct module paths for all three languages.
- All existing Python tests still pass.
