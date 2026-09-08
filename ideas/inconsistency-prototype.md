# Inconsistency detection prototype

The first experiment finds small differences between near-identical Python
function bodies. It is a review-candidate generator, not a bug detector. Source
analysis uses tree-sitter through the existing duplicate detector; no Python AST
parser or source-parsing regexes were added.

Run from the repository's built environment:

```bash
UV_CACHE_DIR=.uv-cache uv run --no-sync python -m emend.inconsistency src/emend
UV_CACHE_DIR=.uv-cache uv run --no-sync python -m emend.inconsistency tests/test_emend
make test TESTS="tests/test_emend/test_inconsistency.py tests/test_emend/test_duplicate.py"
```

Output is JSON containing both symbol locations, canonical token changes,
similarity, a category, and a unified source diff. Addition/deletion candidates
appear first; their direction does not establish which implementation is wrong.
Nothing is registered in the public CLI, lint, or MCP surface yet.

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
algorithms are outside this experiment. The existing duplicate parser limits the
prototype to Python. It reparses on each run; it is not integrated with the
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
