# AST dedup experiment — evaluation (2026-04-15)

**Scope of this writeup.** Phases 1–6 of `ideas/roadmap/` are implemented. The
Phase 6 runner was executed against the `emend` corpus with all five hashing
strategies enabled; the raw report is pinned at
`experiments/ast_dedup/reports/emend-20260415T053153Z.{json,md}`. A later
cross-repo sweep populated exact canonical hashes for `django`, `fastapi`,
`flask`, `lark`, `sqlalchemy`, and `sympy` into a persistent SQLite corpus and
is summarized in `experiments/ast_dedup/CROSS_REPO_REPORT.md`. `cpython` and
`pandas` remain unrun. All numbers and code references here come from the
pinned reports unless stated otherwise.

## TL;DR

- **Merkle exact hashing alone already surfaces the signal we care about.**
  Every other hashing strategy either produced the same clusters as Merkle
  (`bag_of_subtrees`) or drowned real duplicates in false positives
  (`simhash`).
- **There are real refactor targets in `emend` itself.** The top exact cluster
  is a 19x repetition inside `fact_graph.py`. The top sibling-sequence clone
  is a 9-statement prefix shared by `set_component` (`transform.py:3808`) and
  `remove_component` (`transform.py:4082`) — unambiguously a helper
  waiting to be extracted.
- **Sibling-sequence detection earns its keep:** the transform.py run is not
  picked up by any whole-subtree strategy because the two functions are not
  themselves near-duplicates, only their opening blocks are.
- **Triviality filters barely fire (1.8 %)** because Phase 2's
  `iter_candidates` already yields coarse units (function defs + multi-stmt
  bodies + multi-stmt control-flow blocks). That's the intended behaviour,
  not a bug — but the filter pipeline is overbuilt for the current
  candidate stream.
- **`simhash(0.9)` is unusable at this threshold.** It produced a single
  cluster of 3,510 members (≈ half the accepted corpus) and 3.1 M
  "LSH-only" pairs. Raise the threshold or drop the strategy.
- **Exact cross-repo duplicates across mature libraries are mostly
  boilerplate.** Across `emend`, `django`, `fastapi`, `flask`, `lark`,
  `sqlalchemy`, and `sympy`, the 116 exact shared canonical hashes collapsed
  to zero interesting findings after filtering constant tables, tiny guards,
  and abstract stubs.

## Corpus summary

| corpus  | files | LOC    | candidates | after filters | Merkle clusters | dup ratio¹ |
|---------|------:|-------:|-----------:|--------------:|----------------:|-----------:|
| emend   |    36 | 37,798 |      3,696 |         3,631 |              85 |     ~4.8 % |

¹ *dup ratio* = fraction of accepted subtrees that belong to some Merkle
cluster of size ≥ 2. For `emend` that's about 173 / 3,631 ≈ 4.8 %.

Cross-repo exact-match sweep status:

| corpus     | rows written | note |
|------------|-------------:|------|
| emend      |        3,697 | local corpus |
| django     |       23,871 | populated |
| fastapi    |          755 | populated |
| flask      |          948 | populated |
| lark       |        1,654 | populated |
| sqlalchemy |       26,299 | populated |
| sympy      |       82,870 | populated |

Merged accepted subtrees in `cross-repo-all.sqlite`: `128,210`. Exact
cross-repo canonical hashes shared by at least two repos: `116`.

The remaining unrun corpora in `CORPORA` are `cpython` and `pandas`.

## Strategy comparison

Wall times below are the sum of `index_insert_secs + query_secs` from
`StrategyResult`, measured after `datasketch` was installed (the fallback
MinHash is ~1000× slower but gives the same clusters).

| strategy                    | clusters | wall   | largest | LSH-only pairs | notes                              |
|-----------------------------|---------:|-------:|--------:|---------------:|------------------------------------|
| merkle_exact                |       85 |   0.0s |      19 |              — | baseline                           |
| bag_of_subtrees             |       85 |   0.1s |      19 |              0 | identical cluster set to merkle    |
| kind_token_shingles_minhash |      143 |   0.1s |      19 |            179 | merkle + 58 extra clusters         |
| kind_shingles_minhash       |      726 |   0.1s |      19 |          1,713 | lots of shape-only false positives |
| simhash                     |       15 |  15.5s |   3,510 |      3,106,305 | threshold way too loose            |

**Q1: does Merkle alone suffice, or does LSH add value?** Merkle suffices for
the emend corpus at the current candidate granularity. `bag_of_subtrees`
recovered exactly the Merkle cluster set with zero additional LSH-only pairs
— it is strictly redundant here, probably because the canonicalizer already
normalizes the exact bag of child Merkle hashes that Merkle itself hashes
into its root digest. `kind_token_shingles_minhash` is the one LSH strategy
that might earn its place: it finds 58 extra clusters and only 179 LSH-only
pairs, a manageable number to sample. `kind_shingles_minhash` (no tokens)
generates 1,713 LSH-only pairs, most of which are "two different functions
that share a pattern of for / if / return" — low precision. `simhash(0.9)`
collapses half the corpus into one cluster; at this threshold it is pure
noise. None of these conclusions are ground truth; they're a single-corpus
observation and should be re-run on `cpython` / `pandas` before
productizing.

**Q2: best precision × recall × speed trade-off?**

- *Default*: `merkle_exact` — O(n), sub-millisecond, zero false positives by
  construction.
- *High recall (candidate)*: `kind_token_shingles_minhash` + Merkle. Reruns
  the canonicalizer once and gives a second, wider net to sample from.

Do not default-enable `simhash` without first recalibrating its threshold
and/or adding banded gating. It is currently worse than useless on this
corpus — it would hide the real clusters inside its mega-cluster of 3,510.

## Cross-repo exact-match sweep

The persistent subtree-corpus pass added a second question: if we ignore
near-duplicates and only look at **exact** alpha-canonicalized Merkle hashes,
do large mature Python libraries actually share substantial chunks of code?

Short answer: **not really**.

The merged exact-match corpus across `emend`, `django`, `fastapi`, `flask`,
`lark`, `sqlalchemy`, and `sympy` produced 116 canonical hashes that appeared
in at least two repos. After reviewing the top matches and tightening two
post-filters (`constant_class_body` and `constant_assignment_block`), the
interesting exact cross-repo findings fell to zero.

Representative raw exact matches:

- Enum-like classes whose bodies are only constant assignments
- Constant tables (`FOO = "..."`) with the same number of entries
- Tiny `if not isinstance(x, T): raise ...; return x` validators
- 4-line loop fragments like `if node in todo: stack.append(node); ...`
- Abstract stubs of the form `def f(*args, **kwargs): raise NotImplementedError`

These are real matches, but they are not compelling duplicated logic. The
curated walkthrough is in `experiments/ast_dedup/CROSS_REPO_REPORT.md`.

## Filter audit

Out of 3,696 candidate subtrees, only 65 (1.8 %) were dropped:

| filter             | dropped |
|--------------------|--------:|
| stereotyped_dunder |      27 |
| token_diversity    |      20 |
| size_floor         |      17 |
| halstead_lite      |       1 |
| depth_floor        |       0 |
| identity_pattern   |       0 |
| root_kind_blocklist|       0 |

Interpretation: the filter pipeline is doing *almost no work* because Phase 2's
`iter_candidates` already yields coarse units — function definitions, bodies
with ≥ 2 statements, and control-flow blocks with ≥ 2 statements. Tiny
`self.x = x` snippets and bare `return None`s are never candidates in the
first place. A 5-sample audit of the rejected subtrees in each filter is
embedded in the pinned JSON under `filter_stats.rejected_samples` and shows:

- `stereotyped_dunder`: rejects legitimate dunder methods (e.g. a few
  short `__repr__` / `__eq__` bodies). No false rejections spotted in the
  sample.
- `size_floor`: 17 rejections are all tiny control-flow blocks; looks correct.
- `token_diversity`: 20 rejections are mostly assignment-heavy blocks with
  ≤ 3 distinct tokens; looks correct.
- `depth_floor` / `identity_pattern` / `root_kind_blocklist`: no rejections at
  all — these filters are subsumed by the others on this corpus.

**Recommendation:** keep the filter definitions but mark
`depth_floor`/`root_kind_blocklist`/`identity_pattern` as redundant for the
current candidate stream. Re-evaluate once a finer candidate source (e.g.
every statement list of length ≥ 2, not just the ones inside control flow)
gets plugged in.

## Real refactor targets in `emend`

Hand-labeled from the top Merkle clusters and sibling-sequence runs:

1. **`transform.py` — extract `_resolve_component_range(selector)`.**
   Sibling-sequence detector flags `set_component` (`transform.py:3808-3829`)
   and `remove_component` (`transform.py:4086-4106`) sharing a 9-statement
   opening: read file → existence check → read text → derive ext → call
   `_rust.get_symbol_component_range` → null-check. A sibling run at
   `transform.py:L4059-4073 ↔ transform.py:L4184-4198` (rank 9) appears to be
   a near-miss of the same pattern in a third function. One extracted helper
   collapses all three.

2. **`fact_graph.py` — `_all_calls` / `_all_references` / `_all_types` /
   `_all_imports`** (cluster 5–8 in the Merkle top-10) are all shaped like:

   ```python
   result = self._client.run("?[...] := *TABLE[...]")
   return [SomeFact(**unpack(r)) for r in result["rows"]]
   ```

   The Merkle hashes confirm three distinct copies with `node_count=67` and
   three more near-miss pairs. A small `_select_all(query, factory) -> list`
   helper removes every one of them.

3. **`fact_graph.py` — `cfg_edges` / similar query-builder family**
   (`L1128-1142`, `L1185-1195`, `L1234-1244`, `L1260-1270`) all build a list
   of `clauses` and a `params` dict with the same `if X is not None:
   clauses.append(...); params[...] = X` pattern. Sibling-sequence detector
   highlights six pairs of 7-stmt runs within this family — a strong
   `_build_where(filters: dict) -> tuple[str, dict]` candidate.

4. **`fact_graph.py:1000-1009` — 19x cluster.** The top Merkle cluster (19
   matches) is the innermost `if X is not None: clauses.append(...)` guard
   itself, not a big block. Per-occurrence this is ~18 nodes, so it's
   borderline trivial; but the *count* of 19 confirms (3) above — each is a
   repeated clause guard inside the query-builder functions. Extracting
   `_build_where` eliminates both the cluster and the siblings simultaneously.

5. **`type_oracle.py:1355-1369` — PyrightAdapter `__init__`** (cluster 4,
   node_count=68, 3x) shows structural parity with the analogous
   `TyAdapter` / `TypeScriptAdapter` constructors. The shared shape is
   "resolve binary via `shutil.which`, fall back to a default, forward to
   super().__init__". A small
   `_resolve_adapter_binary(...)` utility would eliminate it without hurting
   the individual adapters.

6. **`type_oracle.py:L623-643 ↔ L666-686` — 7-statement run** (sibling-seq
   rank 8). Inside one module, two functions share a seven-line opening; they
   almost certainly belong to the same per-backend caching path and can share
   a helper.

## Sibling-sequence value add

On `emend`, the top sibling-sequence clone (9 statements, `transform.py`
`set_component` / `remove_component`) has **no corresponding whole-subtree
duplicate**: neither `set_component` nor `remove_component` is itself a near
duplicate of the other. The shared prefix is the duplicate, not the
enclosing function.

Counting: of the top-10 sibling runs, 7 sit in `fact_graph.py:1128-1270` and
are subsumed once the query-builder family is refactored; the remaining 3
(`transform.py:3809-3829`, `transform.py:4059-4073`, `type_oracle.py:623-643`)
are all *distinct* findings from the whole-subtree strategy and thus justify
keeping Phase 5 in the pipeline.

## Scope edge cases

Phase 2's open question about `PyScopeResolver` qualified-name stability
across comprehension variables, walrus bindings, and nested functions is
only *indirectly* exercised by this report. The corpus contains all three
constructs, and the Merkle exact clusters did not show obvious "should have
matched but didn't" cases during inspection. A dedicated micro-corpus with
side-by-side alpha-equivalent snippets is the right tool for that question;
see follow-up ticket below.

## Recommendation

The experiment answered its core question: **recursive Merkle hashing on
alpha-canonicalized subtrees, combined with sibling-sequence detection via
winnowing, is sufficient to find real refactor targets in `emend`.** The
additional LSH strategies are either redundant (`bag_of_subtrees`), modestly
useful but noisy (`kind_token_shingles_minhash`), or broken at the default
threshold (`simhash`).

We should *not* productize this as an `emend` command yet. The current
results are motivating for **intra-repo** refactoring but the cross-repo
exact-match sweep shows that exact canonical hashes are too strict to surface
substantial shared logic across distinct projects. Before building a
user-facing `emend grep --dupes` (or similar), run the remaining corpora
(`cpython`, `pandas`) and evaluate cross-repo **near-duplicate** matching
(`kind_token_shingles_minhash` / sibling-sequence) rather than exact hashes
alone.

## Follow-up tickets

- [ ] **Run the remaining corpus sweep.** `python -m experiments.ast_dedup.run
      --corpus cpython --write-db ...` and `--corpus pandas --write-db ...`.
      `django`, `fastapi`, `flask`, `lark`, `sqlalchemy`, and `sympy` are now
      populated; `cpython` and `pandas` are the remaining corpora from the
      original registry.
- [x] **Peak-RSS measurement is per-process, not per-strategy.** ~~All five
      strategies currently report the same `peak_rss_mb` because
      `resource.getrusage` returns a monotonic process-wide high-water mark.
      Fix by sampling RSS inside `compare_strategies` per strategy.~~ Fixed:
      `run.py` now samples `/proc/self/status` VmRSS per strategy and reports
      `rss_delta_mb` on `StrategyStats` (renamed from `peak_rss_mb`).
- [x] **Recalibrate or drop `simhash`.** ~~At `threshold=0.9` it produces one
      mega-cluster and 3.1 M LSH-only pairs.~~ Dropped from default `REGISTRY`
      in `hashers.py` — 8-bit bands collide at corpus scale regardless of
      threshold. `SimHasher`/`SimHashIndex` remain importable for explicit
      opt-in experimentation; a detailed root-cause comment sits above the
      registry.
- [ ] **Port top refactors.** Open issues / PRs for the three concrete
      targets in `transform.py` and `fact_graph.py` called out above. Good
      validation that the tool finds actionable signal.
- [ ] **Add cross-repo near-duplicate analysis.** Exact canonical hashes across
      mature repos mostly surfaced boilerplate. The next experiment should run
      `kind_token_shingles_minhash` or sibling-sequence matching across repos
      and apply the same report/audit workflow now used for exact matches.
- [x] **Dedicated scope-edge-case micro-corpus.** ~~A tiny `tests/`-adjacent
      corpus with comprehension variables, walrus, and nested functions in
      known alpha-equivalent pairs would directly answer Phase 2's open
      question about `PyScopeResolver` qualified-name stability.~~ Added
      `experiments/ast_dedup/tests/test_scope_edge_cases.py` (9 tests). All
      pass: `PyScopeResolver` produces stable qns for comprehension vars,
      walrus bindings, and nested-function locals; the Phase 2 open question
      is answered affirmatively.
- [x] **Expose `PyNode` in `ast_utils.py`** ~~(Phase 1 kept it in `emend_core`
      only). Low priority but mentioned in the design doc.~~ `PyNode`,
      `PyTree`, `parse_source`, and `parse_file` are now re-exported from
      `emend.ast_utils`.
- [ ] **Port `collect_identifier_positions` / `get_statement_ranges`** to use
      `PyNode` once that exposure lands.
- [ ] **Consider a finer-grained candidate source.** Currently
      `iter_candidates` yields function defs and multi-stmt bodies. An
      optional "yield every statement run of length ≥ 2" mode would give the
      triviality filters more to do and may surface extract-method targets
      that don't align with block boundaries.

## Hand-labeled sample

For reproducibility of the precision claims above, a tiny labels CSV with
one row per hand-inspected cluster / run is pinned at
`experiments/ast_dedup/labels/emend-2026-04-15.csv`. Re-running this
evaluation on another corpus should produce a sibling CSV there.
