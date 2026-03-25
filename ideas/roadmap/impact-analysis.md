# Impact Analysis

## Goal

Given a changed symbol or diff, compute the symbols, files, and tests that may
be affected.

## Why First

This is the nearest-term feature with the best leverage for both humans and LLM
agents:

- helps scope reviews
- narrows test selection
- gives agents a concrete next-search frontier
- builds directly on existing caller/reference infrastructure

## Existing Building Blocks

- `find_references()`
- `find_callers()`
- `visit_project_ts()`
- statement-range mapping via `get_statement_ranges()`
- import-graph filtering already used by caller discovery

One correction to the original combined note: `generate_graph()` is currently
file-scoped, not a ready-made project-wide graph source.

## Proposed Command

```bash
emend impact --diff HEAD
emend impact --diff abc123..def456
emend impact mymodule.py::MyClass.method
```

## Core Algorithm

1. Map a diff or selector to changed symbols.
2. Compute the transitive reverse-caller closure.
3. Map impacted symbols back to test references and test files.
4. Return the impacted symbol set, test set, and optional graph/witness edges.

## Output Modes

- `--output=symbols`
- `--output=tests`
- `--output=graph`
- `--json`

## Important Refinements

- Distinguish "definitely impacted" from "possibly impacted".
- Include witness edges showing why each symbol or test was included.
- Cache symbol-to-test mappings in `parse.db` only after the basic feature is
  proven useful.
- Keep the first version language- and framework-agnostic where possible.

## Suggested v1 Scope

- selector input
- diff input
- reverse-caller closure
- test-file heuristics
- JSON output with witness edges

## Deferred

- confidence scoring
- richer framework-specific test discovery
- CI-focused cache tuning
- project-wide impact graph materialization
