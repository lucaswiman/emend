# Trace Manual Testing Pipeline

This document defines the manual command-execution checks for `emend trace`.

Use it when changing:

- intraprocedural trace semantics
- interprocedural trace semantics
- Datalog/Python engine selection
- flow-rule to trace compilation
- trace CLI output or engine-reporting behavior

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
`.read_text()` and `.write_text()` calls. This is useful because `emend`
contains real instances of those shapes in production code.

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

### File-Level Commands

Run both intraprocedural and interprocedural modes on a targeted file first:

```bash
uv run emend trace src/emend/lint.py --config /tmp/manual-trace-rules.yaml --json
uv run emend trace src/emend/lint.py --config /tmp/manual-trace-rules.yaml --interprocedural --json
uv run emend trace src/emend/lint.py --config /tmp/manual-trace-rules.yaml --interprocedural --engine datalog --json
```

Current known-good observations from the self-hosted baseline:

- both commands report findings in `src/emend/lint.py`
- current findings include:
  - `src/emend/lint.py:720` for `$P.read_text()`
  - `src/emend/lint.py:728` for `$P.write_text($DATA)`
- `--engine python` (the default) reports `engine: "python"`
- `--engine datalog` reports `engine: "datalog"`
- both engines should produce the same findings for these simple flows

Those exact findings are not a contract forever, but major unexpected changes
should be investigated.

### Directory-Level Commands

After the file-level check is stable, try a directory slice:

```bash
uv run emend trace src/emend --config /tmp/manual-trace-rules.yaml --json
uv run emend trace src/emend --config /tmp/manual-trace-rules.yaml --interprocedural --json
uv run emend trace src/emend --config /tmp/manual-trace-rules.yaml --interprocedural --engine datalog --json
```

Current caveat:

- repo-wide self-hosted trace can be slow enough that it should be treated as a
  soak/manual check rather than a tight development loop
- a timed run over `src/emend` with the simple `Path(...)` rules did not finish
  within 20 seconds during initial manual probing

That makes targeted-file runs the preferred first-line manual check.

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
   - engine used
   - any obvious false positives or false negatives

Example shape:

```bash
uv run emend trace path/to/project/package --config /tmp/manual-trace-rules.yaml --json
uv run emend trace path/to/project/package --config /tmp/manual-trace-rules.yaml --interprocedural --json
```

## Stage Expectations

### During Parity Work

Before broader CLI/manual runs, execute the focused parity corpus:

```bash
make test TESTS='tests/test_emend/test_phase11_differential.py tests/test_emend/test_phase11_cli_parity.py tests/test_emend/test_phase11_divergence.py tests/test_emend/test_interprocedural_trace.py -k "phase11 or interprocedural_datalog or callee_return_taint_reaches_caller_sink or cross_function_violation or nested_same_named_helpers_are_scoped_to_their_owner or late_sanitizer_does_not_erase_earlier_interprocedural_violation"'
```

That corpus compares the public Python engine against the private Phase 11
Datalog adapter on curated interprocedural fixtures before any cutover work.
The CLI parity tests also verify that `--engine python` and `--engine datalog`
route correctly and that JSON output includes the `engine` field.

When two engines are expected to match, compare:

- finding count
- finding locations
- labels
- sink patterns
- engine metadata

Also manually inspect a few traces/witnesses, not just counts.

### Accepted Divergences

The following differences between the Python and Datalog engines are documented
and accepted (see `tests/test_emend/test_phase11_divergence.py`):

1. **Iteration count**: Datalog always reports `iterations=1` because it uses a
   single transitive closure query. Python may report higher iteration counts
   for multi-hop call chains. This is a representation difference, not a
   semantic one.

2. **Trace step descriptions**: The descriptive text in trace steps may use
   slightly different phrasing between engines. The semantic content (file,
   line, variable) matches; the description is informational only.

### During Cutover Work

After the public engine changes, rerun the same commands and verify:

- the reported engine changed as expected
- finding locations did not drift unexpectedly
- trace/witness formatting remains usable
- performance is not materially worse on the targeted manual corpus

### During Legacy Cleanup

After removing the old path, rerun:

- one self-hosted file-level command
- one self-hosted directory slice
- one external project targeted command

The goal is to confirm that cleanup did not silently remove functionality that
had only been covered by real CLI execution.
