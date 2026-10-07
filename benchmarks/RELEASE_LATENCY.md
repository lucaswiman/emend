# Release-wide latency review

Review scope: the full change since `v0.5.4` (`71411b2`). The measurements below
isolate the subsequent performance work against `a7b60a3`, which already includes
the merged flow, diff, and picker fixes plus the cache/type compatibility fixes.
They are **not** comparisons against the release's older analysis semantics.

## Method

Linux, Intel Core i7-14700K, CPython 3.14.3 free-threaded, optimized Rust extension.
Processes inherited affinity to CPUs 0–7 so the hybrid CPU's different core types
did not confound comparisons. Baseline and candidate used matching source and
native binaries. Arms ran sequentially in before/after/after/before order, with
five measurements per arm. The table gives the median of each side's samples.
Imports, fixture creation, one lazy-initialization warmup, result destruction,
and garbage collection are outside the timer. Cold indexing closes owners and
removes project caches before every measured call. First picker queries use
fresh engines without an index. Flow evaluator probes reuse a prepared FactGraph.
Index probes disable external type inference.

Validation passed: `make test-ci` with TypeScript 5.9 available, `make test-mcp`,
`make test-package`, and `make docs-html`. Vim and Neovim passed both the Vader
suite and a gated live server test that showed ordinary files before indexing
and refreshed the query afterward. Regression probes reproduced stale Git
inventory publication, disconnected callback handling, and module-context
changes between the parent cache probe and worker preparation before fixing them.

[Raw samples and binary hashes](release_latency_results.json) accompany the
[benchmark harness](bench_release_latency.py). These are bounded synthetic
diagnostics, not editor startup or end-to-end CLI guarantees.

| Workload | Before ms | After ms | Before / after |
|---|---:|---:|---:|
| Extract 1,000 tainted transformations | 17.63 | 12.96 | 1.36x |
| Extract 200 loop finalizers with conditional continues | 42.25 | 21.15 | 2.00x |
| Extract 12 optional empty finalizers | 9.81 | 0.27 | 36.95x |
| Evaluate 200 effect-read candidates | 12.37 | 8.89 | 1.39x |
| Evaluate one existential validator and 100 sinks | 64.78 | 14.26 | 4.54x |
| Evaluate 50 existential validators and 100 sinks | 850.39 | 17.08 | 49.78x |
| Evaluate one sanitized sink beside 300 unrelated functions | 51.75 | 47.53 | 1.09x |
| Repeat file query over 10,000 tracked files | 18.96 | 0.87 | 21.74x |
| First file query over 10,000 tracked files | 18.88 | 19.95 | 0.95x |
| Warm index of 150 modules | 318.71 | 258.25 | 1.23x |
| Cold index of 150 modules | 796.47 | 749.64 | 1.06x |
| Select one changed symbol among 30,000 | 13.59 | 2.57 | 5.28x |
| Read a real Git patch adding 100,000 lines | 33.67 | 35.58 | 0.95x |

The first-query cost includes resolving the actual Git index for correct
invalidation in nested projects and worktrees. Patch spooling trades temporary
file I/O for much lower Python memory. Small differences depend on host load;
neither operation has a demonstrated latency improvement in this panel.

Separate single-run `tracemalloc` probes, with setup excluded:

| Workload | Before peak bytes | After peak bytes |
|---|---:|---:|
| Real patch payload | 11,408,783 | 326,114 |
| Flow beside unrelated functions | 12,187,783 | 6,577,743 |
| Changed-symbol selection | 9,131,160 | 2,576 |

These peaks measure Python allocations, excluding native memory, OS page cache,
and the disk spool. They are not process RSS measurements.

## What removes work

- Empty CFG blocks remain neutral occurrences. This deletes transition
  composition and avoids enumerating optional finalizer combinations during
  extraction. Control edges retain the original sparse graph.
- Reaching definitions bypass transparent occurrences in their private
  projection, share unchanged states, and process segments in reverse postorder.
  Python fact export reuses immutable event IDs and edge-kind strings.
- Endpoint matching uses a span index. Effect candidates are prepared once per
  rule, and clean reachability is shared across sinks and sanitizer states.
  One call-component owner serves finalizer activation and value-state walkers;
  unrelated functions do not inflate each source's identity masks.
- File inventories survive repeated queries while the real Git index or scanned
  directory metadata remains unchanged. Publication checks changes during scans.
  Scope filtering precedes candidate limits, and indexed fuzzy paths are reused
  until SQLite's data version changes.
- QN marker reads are batched. Workers skip repeated schema setup, and warm hits
  avoid worker connections while validating current content and module context.
  Cache initialization belongs to the live connection owner.
- Diff selection sorts only intersecting symbol boundaries. Git output is
  parsed incrementally from a temporary file, and impact test edges use a lookup.

## Remaining costs

Exact live-value correlation can still grow exponentially; its existing budget
retains a conservative warning without claiming a witness. Reaching-definition
output can itself be dense: the 200-finalizer loop emits the same 80,601 reaching
edges on both sides. Protected-region routing still scans overlapping regions,
and span indexing can visit all overlapping spans. No universal linear-time
claim follows from these changes.

Cold indexing still parses and publishes its facts; first inventory collection
still lists the repository. External compiler startup and TypeScript's per-file
program construction are excluded and remain separate costs. Further changes
there require preserving compiler context and cache publication semantics.

## Reproduce

Build each checkout's optimized native extension with
`make .venv/lib/emend_core`. From the candidate checkout, use its interpreter
and select the corresponding built source tree:

```sh
UV_CACHE_DIR=.uv-cache PYTHONPATH=/path/to/built/baseline/src \
  taskset -c 0-7 uv run --no-sync python benchmarks/bench_release_latency.py --repeat 5
UV_CACHE_DIR=.uv-cache PYTHONPATH=src \
  taskset -c 0-7 uv run --no-sync python benchmarks/bench_release_latency.py --repeat 5
```

Repeat in reverse arm order. Add `--case NAME` to isolate workloads and
`--memory` for a separate allocation probe; memory tracing does not affect
the reported timing samples. Choose available CPUs of one core type on other
machines. Both native binaries must match the interpreter ABI.
