# Phase 7 — Evaluation writeup

## Purpose

Synthesize the Phase 6 reports into a single `EVALUATION.md` that answers
concrete questions posed in the design. This is the document an agent (or
a human) reads to decide whether the experiment is worth productizing.

## Questions the writeup must answer

1. **Does recursive Merkle hashing alone surface enough signal, or does LSH
   add real value?** Compute, per corpus: the count of real near-dup pairs
   that each LSH strategy finds but Merkle misses, after sampling and
   manual labeling.

2. **Which hashing strategy has the best precision × recall × speed
   trade-off?** Rank strategies on a 2D plot: (wall time per candidate)
   vs (labeled-real clusters per unit compute). Pick one "default" and one
   "high-recall" configuration.

3. **Do the triviality filters hide real signal?** From each filter's
   5-sample review, count false rejections, compute a rough precision of
   the filter itself, recommend adjustments.

4. **Are there real refactor targets in `emend`?** Hand-pick up to 10
   highest-value near-duplicates from the `emend` corpus report and list
   them with suggested consolidation (e.g. "extract helper in
   `transform.py`").

5. **Does sibling-sequence detection find things whole-subtree detection
   misses?** Count sibling-sequence clones that have no corresponding
   whole-subtree duplicate on the same byte range.

6. **How well does alpha-renaming generalize across scope kinds?** From
   Phase 2's open question: did `PyScopeResolver` produce stable qualified
   names for comprehension variables, walrus bindings, and nested
   functions? Include a table of test cases with results.

7. **What's the rough ceiling on duplication in each corpus?** For each
   corpus: `(duplicated_LOC) / (total_LOC)` under the Merkle-exact scheme.
   Compare to published clone-detection benchmarks.

## Structure

```markdown
# AST dedup experiment — evaluation (YYYY-MM-DD)

## TL;DR
<3-5 bullets>

## Corpus summary
<table: corpus, files, LOC, candidates, Merkle clusters, dup ratio>

## Strategy comparison
<table + commentary>

## Filter audit
<per-filter FP rate + recommendations>

## Real refactor targets in emend
<numbered list of up to 10 findings with file:line and rationale>

## Sibling-sequence value add
<count + 3 representative examples>

## Scope edge cases
<table of PyScopeResolver qn stability findings>

## Recommendation
<would we productize this? what would we need?>
```

## Follow-up ticket candidates

The evaluation should enumerate any follow-ups worth opening as emend
issues:

- Expose `PyNode` in `ast_utils.py` as the public face (if Phase 1 kept it
  in `emend_core` only)
- Port `collect_identifier_positions` / `get_statement_ranges` to use
  `PyNode` internally
- Add a new emend command (something like `grep --dupes`) only if the
  evaluation's answer to Q4 found real refactor targets with high
  confidence
- Tighten specific filters based on the audit

## Checklist

- [ ] `experiments/ast_dedup/EVALUATION.md` covering Q1-Q7
- [ ] Raw per-corpus reports referenced from the evaluation
- [ ] Follow-up tickets listed at the bottom
- [ ] Hand-labeled sample CSV attached in `experiments/ast_dedup/labels/`
      for reproducibility of the precision numbers
