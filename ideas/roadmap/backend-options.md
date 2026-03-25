# Backend Options

## Summary

egglog/datalog is a plausible long-term component, but it should not be the
first architectural commitment. The safer long-term plan is:

1. define a stable internal fact model with provenance
2. implement the first analyses directly against that model
3. add a relational backend once the schema is proven

## What egglog Is Good At

- recursive relational queries
- expressing derived facts cleanly
- equality saturation for rewrite exploration
- unifying some analysis and transformation concepts

## Where egglog Is Risky

- statement-level Python rewrite extraction is hard
- source fidelity matters: comments, imports, formatting, trivia
- many practical analyses need provenance and witness paths, not just derived facts
- one backend for both analysis and rewriting may force awkward compromises

## Long-Term Alternative I Prefer

Use a layered architecture:

- front end: scope/type/pattern extraction
- middle layer: typed relational/fact graph with provenance
- engines:
  - native Python/Rust fixed-point solver for early analyses
  - optional Datalog-style backend for advanced queries
  - optional rewrite engine for saturating carefully scoped transforms

This leaves room to adopt egglog later without forcing all semantics through it
now.

## Better Than egglog?

Not categorically. For pure analysis, a simpler recursive-dataflow engine may be
better operationally. For rewrites, egglog is stronger than plain Datalog. The
main question is whether emend really needs one engine to do both.

My current bias:

- impact and taint: build first without egglog
- expert queries: maybe compile to Datalog later
- rewrite saturation: experiment separately, with tight scope

## Candidate Long-Term Shapes

### Native fact engine first

Best for iteration speed and explainability.

### Datafrog/Ascent-style relational engine

Potentially a better fit than full egglog if recursive analysis is the real
need and equality saturation is secondary.

### egglog for a rewrite sub-system

Likely the best place for egglog if saturation turns out to matter in practice.
