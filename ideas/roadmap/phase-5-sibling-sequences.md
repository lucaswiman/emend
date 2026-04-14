# Phase 5 — Sibling-sequence duplicate detection

## Motivation

Directly addresses the user's third question: "What about long sequences of
duplicate sibling nodes but their parent nodes aren't necessarily close
overall?" Two functions that share a 10-statement initialization prelude
but diverge afterwards will NOT be flagged by any whole-subtree strategy —
the parent hashes differ. But the shared run is often the real duplication.

## Approach

Treat each function/method body as a **sequence of canonical statement
hashes**. Finding duplicated runs across all such sequences is a classic
longest-common-substring problem with two good solutions we implement and
cross-check.

### Method A: Winnowing

Schleimer-Wilkerson-Aiken "Winnowing: Local Algorithms for Document
Fingerprinting" (SIGMOD 2003):

1. Build the statement-hash sequence `S = [h1, h2, ..., hn]` for each
   function, where each `hi` is the Merkle hash of statement `i` canonicalized
   via Phase 2 (using the *enclosing function* as the alpha-renaming scope
   so sibling statements share a renaming context).
2. Slide a window of size `w` (say `w=4`) over `S` and, in each window,
   select the rightmost minimum hash value as the window's fingerprint.
3. Deduplicate consecutive identical selections.
4. Index fingerprints → list of `(function, position)`. Any fingerprint hit
   by ≥ 2 functions indicates a candidate shared run.
5. Extend each hit leftward and rightward as far as the hashes still agree,
   to recover the full matched run length.

Winnowing guarantees that any match of length ≥ `w + t - 1` (where `t` is
the shingle size) will be detected at least once, with O(n) work.

### Method B: Generalized suffix array

On small corpora (say ≤ 50k statements) we can afford the exact method:

1. Concatenate every function's statement-hash sequence with a unique
   separator between each function.
2. Build a suffix array over the concatenated sequence (hashes are
   integers, so this is classic SA construction; use `pydivsufsort`).
3. Compute the LCP array.
4. Every LCP entry ≥ `L` corresponds to a shared run of ≥ `L` statements;
   recover the two function/position pairs from the suffix positions.
5. Filter out self-matches and matches whose run straddles a separator.

This gives *exact* longest-common-substring ground truth against which
winnowing can be calibrated.

## Data structures

```python
@dataclass(frozen=True)
class StatementSeq:
    file: str
    function_qn: str              # qualified name from PyScopeResolver
    start_line: int
    end_line: int
    hashes: tuple[bytes, ...]     # per-statement canonical Merkle hashes
    line_ranges: tuple[tuple[int, int], ...]  # per-statement (start,end)

@dataclass(frozen=True)
class SequenceClone:
    left: StatementSeq
    left_range: tuple[int, int]   # statement indices [start, end)
    right: StatementSeq
    right_range: tuple[int, int]
    length: int                   # statements
    method: str                   # "winnowing" or "suffix_array"
```

## Canonicalization context

Each statement hash comes from canonicalizing the statement subtree with
the **enclosing function** as the renaming scope. This ensures that:

- A loop counter `i` defined in the function has a stable `bound_k` token
  across all statements that reference it.
- Two functions that rename their loop counter to `j` vs `i` still hash
  their statements identically, because both become `bound_0`.
- Two functions that rename their counter and *also* change its semantics
  (say, iterating over a different collection) will hash differently at the
  `for` statement but may still hash identically at the arithmetic
  statements — which is fine; those substrings are real shared runs.

This is subtle but correct: the scope of alpha-renaming determines what
counts as "the same" across statements. Using the *enclosing function* is
the natural choice for intra-function repetition; using the *statement
itself* would count every `x = x + 1` as identical regardless of which
variable.

## Triviality filters (sibling-sequence edition)

Phase 4's filters don't all transfer. We add:

- **Minimum run length:** `length >= MIN_RUN` (default 4 statements).
- **Non-trivial statement mix:** the run must include at least 2 distinct
  statement kinds (not all assignments, not all method calls).
- **Cross-boundary bonus:** runs that span ≥ 2 functions in ≥ 2 files are
  reported with elevated priority (likely real refactor targets); runs
  within one file are reported with normal priority.

## Tests

`experiments/ast_dedup/tests/test_sequence.py`:

1. Two hand-built functions share a 6-statement prelude; both winnowing
   and suffix array find the same clone with `length == 6`.
2. A loop rename (`i` → `j`) does not break the match.
3. A literal change (`3` → `5`) does not break the match (literals are
   canonicalized).
4. An operator change (`+` → `-`) breaks the match at the changed
   statement, splitting the run into two shorter runs.
5. Agreement check: on a 10-function fixture, winnowing finds every run the
   suffix array finds (precision and recall both 1.0 up to the `w + t - 1`
   minimum length).
6. A run that is entirely `self.x = x` assignments is filtered by the
   non-trivial statement mix rule.

## Checklist

- [ ] `experiments/ast_dedup/sequence.py` with both methods + shared
      `StatementSeq`/`SequenceClone` types
- [ ] `tests/test_sequence.py` with cases 1-6
- [ ] Runner (Phase 6) feeds sibling-sequence results into the report
      alongside whole-subtree results
- [ ] Verified: uses `pydivsufsort` optionally; falls back to pure-Python
      LCP if unavailable
