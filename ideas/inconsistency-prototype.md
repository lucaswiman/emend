# Inconsistency detection prototype

The detector finds small differences between near-identical Python, Rust, and TypeScript/JavaScript
function bodies. It is a review-candidate generator, not a bug detector. Source
analysis uses tree-sitter through the existing duplicate detector; no Python AST
parser or source-parsing regexes were added.

Run from the repository's built environment:

```bash
uv run --no-sync emend dupes src/emend --near --json
uv run --no-sync emend dupes tests/test_emend --near --json
make test TESTS="tests/test_emend/test_inconsistency.py tests/test_emend/test_duplicate.py"
```

Output is JSON containing both symbol locations, canonical token changes,
similarity, a category, and a unified source diff. Addition/deletion candidates
appear first; their direction does not establish which implementation is wrong.
Use `emend dupes PATH --near` (or `emend analyze dupes PATH --near`) for text
diffs, with `--json` for structured output and `--limit` to cap results (50 by
default). Exact/sequence-specific filter flags are rejected in near mode.
Lint and MCP integration are not registered yet.

The command accepts a file or directory (default: current directory); missing paths are
errors. Mixed-directory scans use each file's grammar and scope configuration,
and compare candidates only within a language. TSX/JSX, methods, and TypeScript
arrow/function expressions are supported. Parenthesized Python docstrings are
ignored; JavaScript directives remain executable differences.

Rust and TypeScript local identities include their containing function, so
same-named locals do not collide across functions. Local reference selectors
use that scope, e.g. `lib.rs::compute.count`.

## Experiment, 2026-09-07

Measured locally on main `54fb850` plus this prototype, scanning each directory
separately. Times are single-run wall times, not a benchmark distribution.

| Corpus | Python files | Seconds | Pairs | Addition/deletion | Replacement |
|---|---:|---:|---:|---:|---:|
| `src/emend` | 80 | 4.95 | 11 | 1 | 10 |
| `tests/test_emend` | 127 | 9.08 | 321 | 82 | 239 |

Raw results from this run are in `/tmp/emend-inconsistency-results.json`.
That is a local, disposable artifact; the commands above reproduce the scan.

Reviewed examples:

- `_LSPTypeOracle._cache_context_changed` versus `clear_cache`: the latter
  additionally clears `_cache`. This appears intentional: `_refresh_cache_context`
  switches the cache namespace before restarting the analyzer. Clearing old
  namespaces would discard reusable state. The repeated restart code could still
  be shared; the absent clear operation is not evidence of a bug.
- `FactGraph.calls_from` versus `calls_to`, and `flows_from` versus `flows_to`:
  intentional direction/field differences. Similarity alone ranks these highly.
- `PyrightAdapter`, `TyAdapter`, and `RustAnalyzerAdapter` constructors:
  intentional executable names. Their common implementation already lives in a
  base class, so the differences do not warrant another abstraction.
- `FactGraph.stored_path` versus the helper inside `update_files`: similar path
  conversion with different sources of project context. A consolidation candidate,
  but the differing context ownership requires review before treating it as drift.
- Callable type tests change both an input type and assertion polarity. These are
  deliberate positive/negative pairs, not missing checks. A sample of test results
  was reviewed; all 321 pairs have not been individually audited.

No application bug was confirmed from those near-clone findings. Separately,
inspecting the first results exposed a real canonicalizer bug: tree-sitter's
`string_content` can contain escape children without children representing the
intervening text. The canonicalizer visited only those children, causing
`"safe\n"` and `"unsafe\n"` to compare equal. It now preserves the full
`string_content`, with a regression test and duplicate-cache version bump.
Before that correction and block preservation, the test scan produced 556 pairs;
that comparison changes two variables and is not a standalone precision metric.

## Algorithm and deliberate limits

1. Reuse duplicate parsing, scope resolution, and local-name normalization.
2. Compare function bodies, excluding genuine leading docstrings and comments;
   retain f-string evaluation, operators, literals, and block boundaries.
   Normalize optional trailing commas in call argument lists only.
3. Index distinct five-token shingles. Ignore postings spanning more than 40
   functions, and compare pairs sharing at least four indexed shingles.
4. Require at least 32 tokens per function and 85% sequence similarity. Retain
   at most two changed regions totaling at most 20 tokens. Exclude exact matches
   and overlapping parent/nested function pairs.

This is not a calibrated anomaly score. Thresholds trade recall for manageable
output. Very common implementations can be missed by the posting cap; very large
functions can make sequence alignment expensive. Local names are numbered in
encounter order, so inserting a binding may disrupt alignment. Function defaults,
decorators, signatures, cross-function effects, and equivalent alternative
algorithms are outside this experiment. Structural roles and trivia are configured
in the language TOMLs. It reparses on each run; it is not integrated with the
analysis snapshot cache and should not be polled continuously by an editor.

Behavioral probes exercise missing guards, changed operators/constants, moved
block boundaries, and added executable f-strings. Negative probes cover local
renaming, documentation, trailing call commas, and unrelated/trivial functions.

## What to prototype next

The useful next question is whether a difference breaks an inferred behavioral
relationship. Form families of at least three similar implementations, align
operation roles, and surface minority omissions with their supporting examples.
Use CFG/flow facts to distinguish a missing guard from equivalent protection
elsewhere, and an absent cleanup from cleanup delegated to a helper.

Test suites need separate ranking: changed inputs accompanied by changed expected
outputs are normal. A more interesting discrepancy is an unchanged operation and
setup accompanied by a missing assertion, or a changed fixture whose asserted
behavior never changes. Do not suppress every changed literal or negation; those
can also be the actual defect.

Before public integration, evaluate this against labeled deliberate variants and
seeded defects in multiple repositories. Report precision and mutation recall,
including omitted candidates, rather than just the number of matches. The
current experiment demonstrates retrieval and useful counterexamples, not that
majority behavior establishes a correct specification.

## Frontrun follow-up

Scanned the clean frontrun checkout at `af0effe` on 2026-09-07. Only Python
package and test directories were included; no Rust, benchmark, or example scan.

| Settings | Source pairs / seconds (89 files) | Test pairs / seconds (155 files) |
|---|---:|---:|
| Defaults: 85%, 2 regions, 20 changed tokens | 26 / 3.20 | 581 / 7.26 |
| Wider: 75%, 5 regions, 60 changed tokens | 64 / 6.55 | 1581 / 16.59 |

`find_inconsistencies` now accepts `max_regions` and `max_changed_tokens` for
controlled experiments; defaults remain unchanged. A three-change behavioral
probe verifies default rejection and retrieval with a wider region limit.

The wider run exposed a useful candidate missed by the defaults:
`_sql_cursor._run_connection_close` has no `external_operation_scope`, whereas
`_sql_cursor_async._intercept_connection_close_sync` wraps both physical close
and modeled-state cleanup in that scope. The scope's documented contract is
holding a cross-process worker's one-actor guard across physical operations.
Inspected sync callers also call the unguarded helper directly.

An isolated probe extracted the actual functions using tree-sitter, supplied a
fake operation guard and close method, and recorded guard depth during physical
close, state reset, and scope unregistration. All three observations were depth
0 for sync close and depth 1 for the async-driver wrapper. This confirms the
guard asymmetry, not an end-to-end cross-process race. The appropriate next
validation is concurrent operations through the real worker proxy; any fix
should share one guarded close operation rather than patch every caller.

Other reviewed findings:

- Random bytecode runner patch setup omits Redis instrumentation present in
  DPOR, although it installs the reporter consumed by Redis instrumentation.
  The wider scan also found async strategy routing forwards `detect_redis` only
  for DPOR. The adapter documentation explicitly describes this split, so treat
  it as a strategy capability question, not a newly proven accidental omission.
- `test_dining_philosophers_two_exhaustive` in
  `tests/test_dpor_scheduling_coarsening.py:63` only prints its result. An isolated
  probe replacing `explore` with a zero-execution, unexhausted, empty-failure
  result passes; the neighboring asserted test rejects the same result. This is
  a real test weakness. It was found by inspecting the candidate's surrounding
  test family, not directly reported as an assertion omission by the detector.
- `test_pragma_reports_opaque_database_write` in `tests/test_sql_cursor.py:935`
  repeats `test_suppress_not_set_when_no_tables_parsed` at line 656, which also
  checks suppression cleanup. They have the same file-wide fixture and execution
  path; the weaker case can be removed. Checking suppression only after execute
  does not establish whether it was active during the physical call.
- The default scan's event/queue reporting difference is two early-return guards
  versus an equivalent `or` condition. It is a consolidation opportunity, not
  evidence of missing protection.

Frontrun was not modified and its test suite was not run. Isolated probes execute
extracted function bodies with fake dependencies, without importing frontrun or
running native code. Local artifacts: `/tmp/frontrun-inconsistency-results.json`,
`/tmp/frontrun-inconsistency-wide.json`, and
`/tmp/frontrun-inconsistency-probes.py`. Rerun the latter from emend's environment:

```bash
UV_CACHE_DIR=.uv-cache uv run --no-sync python /tmp/frontrun-inconsistency-probes.py
```

The wider threshold produces many additional intentional protocol and sync/async
variants. Keep it as an experimental sweep; a production default needs ranking
by behavioral evidence, not merely more permissive matching.
