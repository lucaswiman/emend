# Roadmap

All 8 phases of the original roadmap are complete.  This directory now holds
design notes and deferred work only.

## Completed Phases

- [x] Phase 1: Impact analysis — `transform.py`, `emend impact`
- [x] Phase 2: Intraprocedural taint — `taint.py`, `emend taint`
- [x] Phase 3: Compliance layer — **Won't do separately**; taint labels cover this
- [x] Phase 4: Stable fact schema — `fact_graph.py`, `emend facts`
- [x] Phase 5: Interprocedural taint — `taint.py`, `emend taint --interprocedural`
- [x] Phase 6: MCP query interface — `mcp_server.py`, `emend mcp`
- [x] Phase 7: Rewrite/equality-saturation experiment — `rewrite_engine.py`, `emend saturate`
- [x] Phase 8: Expert-mode policy/query surfaces — `policy.py`, `emend policy`, `emend query`

## Design Notes

- The recommended order was intentionally front-loaded toward immediately useful
  analysis features: impact first, then taint, then policy and query work.
- The rewrite/equality-saturation work is kept explicitly experimental until the
  analysis-side facts, provenance, and extraction semantics are well understood.
- The long-term architecture is driven by a stable internal fact model rather
  than premature commitment to one backend.

## Remaining Documents

- [taint-analysis.md](taint-analysis.md) — deferred precision improvements (field sensitivity, object dispatch)
- [query-language-for-code-invariants.md](query-language-for-code-invariants.md) — ongoing witness-quality requirement
- [rewrite-and-saturation.md](rewrite-and-saturation.md) — open design questions for the experimental rewrite engine
- [backend-options.md](backend-options.md) — architecture rationale (CozoDB vs egglog)
- [relation-to-existing-tools.md](relation-to-existing-tools.md) — positioning vs Semgrep, CodeQL, Pysa
- [open-questions.md](open-questions.md) — ongoing design trade-offs
