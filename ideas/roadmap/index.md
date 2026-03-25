# Roadmap

This directory replaces `ideas/egglog-analysis-and-transformation.md` with
smaller spec documents and a staged implementation order.

## Ordered TODOs

- [ ] Phase 1: Ship impact analysis.
  Spec: [impact-analysis.md](impact-analysis.md)
- [ ] Phase 2: Ship intraprocedural taint analysis with path traces.
  Spec: [taint-analysis.md](taint-analysis.md)
- [x] ~Phase 3: Add compliance-sensitive value tracking as a layer on top of taint.~ **Won't do** — this is a labeled special case of taint analysis (Phase 2).
  Spec: [compliance-sensitive-value-tracking.md](compliance-sensitive-value-tracking.md)
- [ ] Phase 4: Stabilize a relational/query model for code invariants.
  Spec: [query-language-for-code-invariants.md](query-language-for-code-invariants.md)
- [ ] Phase 5: Add interprocedural summaries and recursive fixed-point analysis.
  Specs: [taint-analysis.md](taint-analysis.md), [implementation-roadmap.md](implementation-roadmap.md)
- [ ] Phase 6: Expose an MCP query interface after the relation schema is stable.
  Spec: [mcp-design.md](mcp-design.md)
- [ ] Phase 7: Experiment with rewrite backends, including egglog equality saturation.
  Specs: [backend-options.md](backend-options.md), [rewrite-and-saturation.md](rewrite-and-saturation.md)
- [ ] Phase 8: Add expert-mode policy/query surfaces and power-user configuration.
  Specs: [query-language-for-code-invariants.md](query-language-for-code-invariants.md), [open-questions.md](open-questions.md)

## Design Notes

- The recommended order is intentionally front-loaded toward immediately useful
  analysis features: impact first, then taint, then policy and query work.
- The rewrite/equality-saturation work is kept explicitly experimental until the
  analysis-side facts, provenance, and extraction semantics are well understood.
- The long-term architecture should be driven by a stable internal fact model,
  not by prematurely committing the whole product surface to one backend.

## Documents

- [impact-analysis.md](impact-analysis.md)
- [taint-analysis.md](taint-analysis.md)
- [compliance-sensitive-value-tracking.md](compliance-sensitive-value-tracking.md)
- [query-language-for-code-invariants.md](query-language-for-code-invariants.md)
- [backend-options.md](backend-options.md)
- [rewrite-and-saturation.md](rewrite-and-saturation.md)
- [mcp-design.md](mcp-design.md)
- [implementation-roadmap.md](implementation-roadmap.md)
- [relation-to-existing-tools.md](relation-to-existing-tools.md)
- [open-questions.md](open-questions.md)
