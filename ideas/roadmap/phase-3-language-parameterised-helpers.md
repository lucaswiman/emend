# Phase 3: Language-Parameterised Helpers

## Goal

Replace hardcoded Python-specific helper functions in `trace.py` and
`transform.py` with language-driven equivalents that read their configuration
from `languages/<lang>/config.toml`.

## Why

Several helper functions used by the trace engine contain hardcoded Python
assumptions — keyword sets, container mutation method names, for-loop regexes,
and identifier extraction.  These are the final barriers to running the
existing Datalog trace rules on non-Python facts.

## Scope

- `src/emend/trace.py` — `_PYTHON_KEYWORDS`, `_extract_identifiers()`,
  `_find_assignments_in_source()`
- `src/emend/transform.py` — `_ENTRY_POINT_DECORATORS`,
  `_ENTRY_POINT_DECORATOR_BASENAMES`, `_ENTRY_POINT_NAMES`,
  `_is_likely_entry_point()`
- `languages/python/config.toml` — add new sections
- `languages/typescript/config.toml` — add new sections
- `languages/rust/config.toml` — add new sections

## Current Hardcoded Items

### `_PYTHON_KEYWORDS` (trace.py:434)

```python
_PYTHON_KEYWORDS = frozenset({
    "True", "False", "None", "and", "or", "not", "is", "in",
    "if", "else", "elif", "for", "while", ...
})
```

Already partially config-driven: `languages/<lang>/config.toml` has a
`keywords` field in `[language]`, but `_extract_identifiers()` uses the
hardcoded frozenset instead of reading from config.

### Container mutation methods

The Datalog engine tracks container mutation via `MethodCallFact`.  The
methods tracked are populated by the Rust CFG builder, which already reads
from config.  However, `trace.py` has residual Python-specific assumptions
in helper functions that synthesise extra def-use relationships.

### Entry point heuristics (transform.py:5175)

`_ENTRY_POINT_DECORATORS` and `_ENTRY_POINT_NAMES` are Python-specific
(pytest.fixture, app.route, `__init__`, etc.).

## Todo

### Keyword parameterisation

- [x] Add a `[trace]` or `[analysis]` section to each language config with a
  `keywords` list (or reuse the existing `[language].keywords`).
- [x] Update `_extract_identifiers()` to accept a language parameter and load
  keywords from the config instead of `_PYTHON_KEYWORDS`.
- [x] Add TypeScript keywords (`undefined`, `null`, `true`, `false`, `this`,
  `super`, `new`, `typeof`, `instanceof`, `void`, `delete`, `in`, `of`, etc.).
- [x] Add Rust keywords (`let`, `fn`, `struct`, `enum`, `impl`, `trait`,
  `self`, `Self`, `true`, `false`, `None`, `Some`, `Ok`, `Err`, etc.).

### Container mutation methods

- [x] Add a `[trace.container_mutations]` section to each language config:
  - Python: `append`, `extend`, `update`, `insert`, `add`
  - TypeScript: `push`, `splice`, `unshift`, `concat`, `set`, `add`
  - Rust: `push`, `insert`, `extend`, `push_back`, `push_front`
- [x] Update any residual Python-specific container mutation handling in
  `trace.py` to read from config. (Container mutation tracking is handled by
  the Rust CFG builder from config; `_extract_identifiers` was the only
  Python-side gap, now fixed.)

### Entry point heuristics

- [x] Add a `[dead_code.entry_points]` section to each language config:
  - Python: existing `_ENTRY_POINT_DECORATORS`, `_ENTRY_POINT_NAMES`
  - TypeScript: `export default`, `module.exports`, test framework decorators
    (`describe`, `it`, `test`), Express/Fastify route handlers
  - Rust: `#[test]`, `#[tokio::main]`, `fn main`, `#[no_mangle]`,
    `#[export_name]`, `pub` visibility
- [x] Update `_is_likely_entry_point()` to load heuristics from config.
- [x] Update dunder detection (`_is_dunder()`) to be Python-only (skip for
  TS/Rust). Controlled by `has_dunders` key in `[dead_code]` config section.

### Assignment extraction

- [x] Update `_find_assignments_in_source()` (trace.py:464) to accept
  `ext` parameter and handle TypeScript/Rust assignment forms, or remove
  it entirely if the Datalog engine's def-use facts make it redundant.
  (Already accepts `ext` parameter; Datalog engine handles per-language facts.)

### Tests

- [x] Test that `_extract_identifiers()` with `language="typescript"` correctly
  filters TypeScript keywords.
- [x] Test that `_extract_identifiers()` with `language="rust"` correctly
  filters Rust keywords.
- [x] Test that entry point detection works for TypeScript export patterns.
- [x] Test that entry point detection works for Rust `#[test]` and `fn main`.

## Exit Criteria

- No hardcoded `_PYTHON_KEYWORDS` frozenset — keywords come from config.
- No hardcoded entry point lists — heuristics come from config.
- Container mutation method names come from config.
- All three languages have complete `[trace]` / `[dead_code]` config sections.
- All existing Python tests still pass.
