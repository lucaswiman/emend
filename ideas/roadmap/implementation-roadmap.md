# Implementation Roadmap

## Phase 1: Impact Analysis

Effort: small.

1. Add `impact` command in `cli.py`.
2. Map diff hunks to enclosing definitions.
3. Compute reverse-caller closure with witness edges.
4. Add test-file heuristics and JSON output.
5. Validate usefulness against real edit/test workflows.

## Phase 2: Intraprocedural Taint

Effort: medium.

1. Add taint config parsing to `.emend/patterns.yaml`.
2. Build per-function flow analysis.
3. Emit path traces.
4. Add suppression support and CI-friendly JSON output.
5. Measure precision on a small benchmark set.

## Phase 3: Compliance Layer

Effort: small-medium.

1. Add policy labels and policy rules on top of taint.
2. Support audit mode and policy filtering.
3. Reuse trace output from taint rather than inventing a new reporting path.

## Phase 4: Stable Fact Schema

Effort: medium.

1. Define a stable internal fact/provenance model.
2. Normalize bindings, references, calls, types, and flow edges into that model.
3. Keep the model independent from any one query syntax.

## Phase 5: Interprocedural Analysis

Effort: medium-large.

1. Add function summaries.
2. Add recursive fixed-point computation.
3. Improve handling for dynamic dispatch and spread arguments.
4. Re-benchmark false positives and runtime.

## Phase 6: Expert Query Interface

Effort: medium.

1. Expose a power-user query surface once the schema is stable.
2. Prefer compiling high-level features down to the fact model.
3. Keep convenience tools as the primary MCP and CLI surface.

## Phase 7: Rewrite Backend Experiments

Effort: medium-large.

1. Prototype expression-level saturation.
2. Compare against sequential rewrites on real migrations.
3. Only expand scope if extraction quality is clearly better.

## Principle

Do not couple the success of impact and taint features to the success of
egglog-based rewriting. The analysis roadmap should stand on its own.
