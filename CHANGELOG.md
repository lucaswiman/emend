# Changelog

## Unreleased (0.2.0)

### Features

#### Lint Engine

- **`# noqa` inline suppression** (PR #6): suppress violations per-line with `# noqa`, `# noqa: emend:rule-name`, or mixed with other linter tags (`# noqa: E501, emend:no-print`).
- **pre-commit integration** (PR #4): emend can run as a pre-commit hook via `.pre-commit-hooks.yaml`. Add `emend-lint` to your `.pre-commit-config.yaml`.

#### Pattern Replace: `${NAME.content}` String Interpolation (PR #10)

Replace patterns can now extract the inner content of a captured string literal by appending `.content`:

```bash
# Captured $X = "foo" → substitutes foo (quotes stripped)
emend replace 'Union["$X", $Y]' '${X.content} | $Y' src/ --apply
```

Use `${NAME.content}` in a replacement template to strip surrounding quotes from a captured string node. If the captured node is not a string literal, the replacement is skipped for that match. Useful for migrating `Union["X", Y]` (deferred-annotation style) to PEP 604 `X | Y` union syntax.

#### Performance

- **Parse cache** — LRU cache of 256 parsed modules, keyed by source hash.
- **File list cache** — per-project-root cache with mtime-based invalidation.
- **Symbol collection cache** (PR #4) — cached per file to avoid re-scanning.
- **Import graph pre-filtering** — skip files that cannot possibly match a pattern.
- **Lint batching** — process all rules in one pass per file.
- **Rust accelerator** (PR #7, `emend-core` crate) — optional PyO3 extension for:
  - Fast recursive Python file discovery (replaces `os.walk`)
  - Content-based pre-filtering using `memchr` (skips MetadataWrapper for files that can't match)
  - Import extraction for call-graph operations
  - Builds via `make venv`; gracefully absent if not built.

### Fixes

- Fix publish workflow: `fetch-depth: 0` on checkout so `hatch-vcs` can read the release tag and produce the correct version (without it, PyPI rejects the upload due to the local version label).
- `emend-core` excluded from `pip install emend` dependencies (not on PyPI; built locally via Makefile).

---

## 0.1.0 (2026-02-22)

Initial public release.

### Features

#### CLI Consolidation

The command surface was unified to reduce cognitive overhead:

- **Merged** `find`, `lookup`, `list-symbols`, `query`, `show`, `get` → unified `search` command that auto-detects mode from the query (`$` → pattern mode; `::` → lookup mode; bare path → summary mode). All old names remain as aliases.
- **Merged** `callers`, `find-references` → `refs`. Added `--calls-only` to filter to call sites; `--writes-only` and `--reads-only` for read/write context.
- **Merged** `rename-symbol` and `rename-module` → `rename`. Mode auto-detected: selector with `::` renames a symbol; bare path renames a module.
- **Merged** `move-module` → `move`. Same auto-detection.
- **Unified** `--output` flag: `code`, `location`, `selector`, `summary`, `metadata`, `json`, `count`, with optional `::flat` and `::dedent` modifiers.
- **Unified** `--where` flag for scope/structural constraints.

#### Documentation

- Full Sphinx documentation at [lucaswiman.github.io/emend](https://lucaswiman.github.io/emend/) covering commands, patterns, selectors, linting, installation, quickstart, and recipes.
- GitHub Pages CI/CD for automatic doc publishing.

#### Versioning

- Git tag-based versioning via `hatch-vcs`; no hardcoded version numbers.

### Known Limitations / Future Work

See [`TODOS.md`](TODOS.md) and [`ideas/FUTURE_WORK.md`](ideas/FUTURE_WORK.md). Short summary:

- `$X:stmt` type constraint is parsed by the grammar but not fully implemented.
- `graph`, `callers`, and `refs` use per-file `QualifiedNameProvider`; switching to `FullRepoManager` + `FullyQualifiedNameProvider` would improve cross-file name resolution.
