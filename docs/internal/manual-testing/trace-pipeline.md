# Trace Manual Testing Pipeline

This document defines the manual command-execution checks for `emend trace`.

Use it when changing:

- intraprocedural trace semantics
- interprocedural trace semantics
- flow-rule to trace compilation
- trace CLI output

There is one evaluator (`emend.checks.flow`); `emend trace` has no engine
selection flag.  JSON rows carry `"engine": "occurrence"`.

## Goals

- exercise real CLI commands, not only direct Python helpers
- compare targeted runs before broad runs
- keep one self-hosted check against `emend`
- keep one external open-source target for higher-complexity comparisons

## Recommended Progression

Run checks in this order:

1. Small synthetic fixtures in `tests/test_emend/`
2. Self-hosted targeted files in `src/emend/`
3. Self-hosted directory slices in `src/emend/`
4. External open-source project targeted files
5. External open-source project broader directory slices

Do not start with whole-project sweeps. They are slower, harder to interpret,
and poor at isolating regressions.

## Self-Hosted Baseline

Start with a temporary rules file that tracks `Path(...)` values into
`.read_text()` and `.write_text()` calls.

Example temporary config:

```yaml
rules:
  path-to-read-text:
    flow:
      from: 'Path($X)'
      to: '$P.read_text()'
    message: 'Path-derived value reaches read_text()'
  path-to-write-text:
    flow:
      from: 'Path($X)'
      to: '$P.write_text($DATA)'
    message: 'Path-derived value reaches write_text()'
```

Note, verified when this page was last trimmed: these two rules report no
findings, even on a two-line fixture, because a sink that binds the tracked
value as a method *receiver* (`$P.read_text()`) does not currently fire.
Rules whose sink binds a call argument (for example `to: 'open($F)'`) do.
This page therefore no longer lists expected findings; record what you see
before a change and compare after it.

### File-Level Commands

Run both intraprocedural and interprocedural modes on a targeted file first:

```bash
uv run emend trace src/emend/lint.py --config /tmp/manual-trace-rules.yaml --json
uv run emend trace src/emend/lint.py --config /tmp/manual-trace-rules.yaml --interprocedural --json
```

### Directory-Level Commands

After the file-level check is stable, try a directory slice:

```bash
uv run emend trace src/emend --config /tmp/manual-trace-rules.yaml --json
uv run emend trace src/emend --config /tmp/manual-trace-rules.yaml --interprocedural --json
```

Directory runs are slower than targeted-file runs, so targeted-file runs are
the preferred first-line manual check.

## External Comparison Targets

Use one larger Python project to compare behavior outside `emend`.

### Primary Recommendation

Use `django/django`.

Why:

- large, mature Python codebase
- real views, forms, middleware, ORM, templates, and SQL-adjacent surfaces
- broad enough to exercise `trace`, `find`, `refs`, `graph`, and policy checks

### Secondary Recommendation

Use `home-assistant/core` when you want a very large modular Python codebase
with many integrations and broad architectural variety.

## External Project Workflow

For an external target:

1. check out the project into a sibling workspace
2. pick a narrow subtree first
3. use a temporary rules file or preset-backed rules
4. record:
   - command
   - runtime
   - finding count
   - any obvious false positives or false negatives

Example shape:

```bash
uv run emend trace path/to/project/package --config /tmp/manual-trace-rules.yaml --json
uv run emend trace path/to/project/package --config /tmp/manual-trace-rules.yaml --interprocedural --json
```

## After Changing the Evaluator

Rerun:

- one self-hosted file-level command
- one self-hosted directory slice
- one external project targeted command

and compare finding count, finding locations, labels and sink patterns with
the run before the change.  Also manually inspect a few traces (`--trace`),
not just counts.

The goal is to confirm that the change did not silently remove functionality
that had only been covered by real CLI execution.
