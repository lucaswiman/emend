# Implementation Roadmap

## Phase 1: Impact Analysis ✅ COMPLETE

Effort: small.

1. ✅ Add `impact` command in `cli.py`.
2. ✅ Map diff hunks to enclosing definitions.
3. ✅ Compute reverse-caller closure with witness edges.
4. ✅ Add test-file heuristics and JSON output.
5. ✅ Validate usefulness against real edit/test workflows.

## Phase 2: Intraprocedural Taint ✅ COMPLETE

Effort: medium.

1. ✅ Add taint config parsing to `.emend/patterns.yaml`.
2. ✅ Build per-function flow analysis.
3. ✅ Emit path traces.
4. ✅ Add suppression support and CI-friendly JSON output.
5. ✅ Measure precision on a small benchmark set.

## Phase 3: Compliance Layer ✅ WON'T DO (subsumed by Phase 2)

Effort: small-medium.

Labeled taint (Phase 2) covers compliance categories (pii, phi, credit_card,
etc.) as a configuration concern, not a separate engine.  See `index.md`.

## Phase 4: Stable Fact Schema ✅ COMPLETE

Effort: medium.

1. ✅ Define a stable internal fact/provenance model (`fact_graph.py`).
2. ✅ Normalize bindings, references, calls, types, and flow edges into that model.
3. ✅ Keep the model independent from any one query syntax.

Impl: `FactGraph` with 8 typed fact kinds, CozoDB-backed, `emend facts` CLI.

## Phase 5: Interprocedural Analysis ✅ COMPLETE

Effort: medium-large.

1. ✅ Add function summaries (`FunctionSummary` in `taint.py`).
2. ✅ Add recursive fixed-point computation (`run_interprocedural_taint_analysis()`).
3. Dynamic dispatch and spread arguments remain coarse-grained approximations.
4. ✅ CLI: `emend taint --interprocedural --max-iterations`.

## Phase 6: Expert Query Interface ✅ COMPLETE

Effort: medium.

1. ✅ Expose a power-user query surface: `policy.py` + `emend policy` CLI.
2. ✅ High-level MCP tools (`impact`, `taint`, `query_facts`, `check_policies`) compile down to the fact model.
3. ✅ Convenience tools remain the primary MCP and CLI surface.

## Phase 7: Rewrite Backend Experiments ✅ COMPLETE (experimental)

Effort: medium-large.

1. ✅ Prototype expression-level saturation (`rewrite_engine.py`, e-graph + union-find).
2. ✅ YAML rule loading (`.emend/rewrites.yaml`), `emend saturate` CLI.
3. Marked experimental pending real-world migration benchmarks.

## Phase 8: Power-User Configuration ✅ COMPLETE

1. ✅ `.emend/config.toml` / `[tool.emend]` in `pyproject.toml`.
2. ✅ `emend policy` with flow, structural, type, deadcode, and custom checks.
3. ✅ Flow rules in lint (`flows-from` / `flows-to` / `not-through`).

## Principle

Do not couple the success of impact and taint features to the success of
egglog-based rewriting. The analysis roadmap should stand on its own.
