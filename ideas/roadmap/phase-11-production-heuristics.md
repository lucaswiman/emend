# Phase 11 — Production Heuristics and False-Positive Control

## Purpose

Turn the experiment's hand-tuned filters into a narrow production scoring model
that favors actionable duplicate findings and suppresses framework boilerplate.

## Required production changes

1. Keep only the strategies proven useful in production v1:

   - exact canonical Merkle hashes
   - sibling-sequence duplicates

   For exact canonical hashes, production v1 preserves literal constants and
   only alpha-renames variable names.

2. Add production-grade suppressions for the false positives seen in the
   experiment:

   - tiny `isinstance` / raise / return validators
   - abstract stubs (`raise NotImplementedError`)
   - trivial property getters / boolean feature flags
   - same-signature tiny wrapper methods

   Constant tables / constant class bodies should largely stop colliding once
   literals are preserved; they should not be a primary suppression mechanism
   in production unless the new scoring pass still shows they are noisy.

3. Candidate selection must be narrower than the experimental free-for-all:

   - prioritize function/method roots
   - allow block/run findings only when they exceed score/line thresholds
   - same-file tiny fragments should be suppressed by default

4. Introduce one stable score used by CLI/lint/MCP, combining:

   - node/statement count
   - token diversity
   - cross-function / cross-file bonus
   - boilerplate penalties

5. Add a small hand-labeled regression corpus in tests with both:

   - duplicates that must be kept
   - duplicates that must be suppressed

## Deliberately out of scope

- User-configurable per-filter DSL in v1
- Cross-repo heuristics in production
- Full LSH strategy exposure

## Tests

1. Literal-preserving exact hashes distinguish constant tables with different
   values.
2. Abstract stubs do not surface as production findings.
3. The `emend` helper-dup examples still surface.
4. The `sqlalchemy` sessionmaker / dialect examples still surface.
5. Same-file trivial fragments do not dominate the ranked output.

## Checklist

- [ ] Production scoring model exists
- [ ] Boilerplate suppressions cover the audited false positives
- [ ] Hand-labeled regression corpus is added
- [ ] `emend` + `sqlalchemy` representative findings survive the filter
