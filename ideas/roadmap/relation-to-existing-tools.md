# Relation To Existing Tools

## Semgrep

Closest comparison for taint and pattern-driven policy checks.

Relevant lessons:

- familiar source/sink/sanitizer terminology is good
- intraprocedural taint can already be valuable
- explainability and suppression ergonomics matter

## CodeQL

Best comparison for relational query power.

Relevant lessons:

- stable schema matters
- query composability is valuable
- power-user query systems are strong when built on rich extracted facts

## Pysa

Best comparison for Python-specific interprocedural taint.

Relevant lessons:

- interprocedural taint is useful but operationally complex
- framework modeling matters a lot
- false-positive control determines adoption

## egg / egglog

Best comparison for rewrite-oriented equality saturation.

Relevant lessons:

- strong fit for equivalence-driven rewrite search
- less obviously a complete answer for source-faithful developer tooling

## Emend Positioning

Emend is strongest where structural matching, scope-aware symbol knowledge, and
practical source-to-source edits meet. The roadmap should preserve that
pragmatic center of gravity.
