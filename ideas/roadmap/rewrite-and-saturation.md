# Rewrite And Saturation

## Goal

Explore whether equality saturation improves multi-step refactoring enough to
justify the implementation and extraction complexity.

## Important Reframe

This is not a prerequisite for impact analysis, taint analysis, or policy
tracking. It is an experimental transformation track.

## Promising Use Cases

- order-sensitive API migrations
- semantic search over small equivalence classes
- normalization of type annotation spellings
- bounded expression rewrites

## High-Risk Areas

- statement-level Python rewrites
- preserving formatting and comments
- imports and side effects
- choosing a cost function that matches real user expectations

## Suggested Scope

Start with:

- expression-level rewrites
- explicit normalization rules
- opt-in `--experimental-saturate`
- extraction compared against current sequential rewrites

## Success Criteria

- handles at least one real migration better than staged rewrite rules
- preserves source quality
- does not produce confusing or unstable diffs
- can explain which rules participated in extraction

## Deferred

- whole-function restructuring
- broad LLM-proposed rewrite ingestion
- "globally optimal" marketing claims before strong evidence
