# Phase 6 — Corpus runner + statistics

## Purpose

Tie Phases 2-5 together into a single script that runs over several
codebases and produces a JSON + markdown report an agent can evaluate.

## Corpora

Cached under `experiments/ast_dedup/.corpora/` (gitignored):

| Name      | Source                                | Purpose                                 |
|-----------|---------------------------------------|-----------------------------------------|
| `emend`   | `src/emend/`                          | sanity check on our own codebase        |
| `django`  | `benchmarks/.django-checkout/` (5.2)  | large mature framework                  |
| `cpython` | `cpython` git clone (pinned tag)      | stdlib — expect many stereotyped dupes  |
| `flask`   | flask + werkzeug clones (pinned)      | medium-size web framework               |
| `pandas`  | `pandas/core/` (pinned)               | heavy internal templating, high dup rate|

`corpora.py` fetches each into `.corpora/` using the same pattern as
`benchmarks/django_checkout.py` (clone, checkout by tag, cache). Tag pins
live in a small `CORPORA: dict[str, CorpusSpec]` registry so results are
reproducible.

## Runner

```bash
python -m experiments.ast_dedup.run --corpus emend
python -m experiments.ast_dedup.run --corpus django --max-files 500
python -m experiments.ast_dedup.run --all
python -m experiments.ast_dedup.run --corpus emend --strategies merkle_exact,kind_shingles_minhash
python -m experiments.ast_dedup.run --corpus emend --no-filter
python -m experiments.ast_dedup.run --corpus emend --ablate rename_attrs
```

Pipeline per corpus:

1. `corpora.ensure(name)` → directory path
2. Walk `.py` files (respect `.gitignore`)
3. For each file: `parse_file` (Phase 1), `PyScopeResolver.index_file`,
   `canonicalize.iter_candidates` (Phase 2)
4. Run every registered hashing strategy (Phase 3)
5. Run sibling-sequence detection (Phase 5)
6. Apply triviality filters (Phase 4) and record removal counts
7. Aggregate into a `CorpusReport`
8. Write `reports/{corpus}-{timestamp}.json` and `.md`

## Report schema (JSON)

```json
{
  "corpus": "emend",
  "timestamp": "2026-04-14T10:00:00Z",
  "config": {
    "canonicalizer": { ... },
    "filter": { ... },
    "strategies": ["merkle_exact", "kind_shingles_minhash", "simhash",
                   "bag_of_subtrees"],
    "sequence": { "winnowing_w": 4, "winnowing_t": 5, "suffix_array": true }
  },
  "corpus_stats": {
    "files": 187,
    "total_loc": 51234,
    "candidate_subtrees": 8721,
    "after_filters": 2934
  },
  "filter_stats": {
    "size_floor": 3421,
    "token_diversity": 1288,
    "halstead_lite": 802,
    "stereotyped_dunder": 144,
    "identity_pattern": 132,
    "rejected_samples": [ ... 5 samples per filter ... ]
  },
  "strategy_stats": [
    {
      "name": "merkle_exact",
      "wall_time_sec": 2.14,
      "peak_rss_mb": 812,
      "cluster_count": 47,
      "largest_cluster_size": 8,
      "cluster_size_histogram": { "2": 30, "3": 10, "4": 4, "5-8": 3 },
      "top_clusters": [
        { "size": 8, "node_count": 22,
          "locations": [ [ "file.py", 120, 180 ], ... ] }
      ]
    },
    ...
  ],
  "agreement": {
    "merkle_exact_vs_kind_shingles_minhash": {
      "merkle_cluster_coverage": 0.96,
      "lsh_only_pairs": 18,
      "lsh_only_samples": [ ... 10 samples ... ]
    },
    ...
  },
  "sibling_sequence_clones": {
    "count": 63,
    "length_histogram": { "4": 30, "5": 15, "6-10": 14, "11+": 4 },
    "top_runs": [
      { "length": 14, "locations": [ ... ] }
    ]
  },
  "cross_corpus_overlaps": {
    "note": "computed only when --all is passed; pairs (corpus_a, corpus_b, shared_canonical_hash, location_a, location_b)",
    "entries": []
  }
}
```

## Report schema (markdown)

Short (under 3 KB), optimized for an agent's eyes:

```markdown
# emend @ 2026-04-14

Files: 187 · LOC: 51,234 · candidates: 8,721 · after filters: 2,934

## Top exact-duplicate clusters

1. cluster 8x — src/emend/query.py:412-440 and 7 more (node_count=22)
2. cluster 4x — src/emend/transform.py:1203-1260 and 3 more (node_count=51)
...

## Top sibling-sequence clones

1. 14-statement run: src/emend/transform.py:910-924 ↔ src/emend/rename.py:210-224
...

## Strategies

| strategy             | clusters | wall | peak RSS | LSH-only pairs |
|----------------------|---------:|-----:|---------:|---------------:|
| merkle_exact         |       47 | 2.1s |   812 MB |              — |
| kind_shingles_minhash|       81 | 3.4s |   830 MB |             18 |
| simhash              |       63 | 2.8s |   820 MB |              9 |

## Filters

| filter              | dropped |
|---------------------|--------:|
| size_floor          |   3,421 |
| token_diversity     |   1,288 |
| halstead_lite       |     802 |
...

## Open flags
- `kind_shingles_minhash` reports 18 pairs Merkle missed. Sampled 10 →
  6 real near-dupes, 4 boilerplate. Recommend tightening diversity filter.
```

## Runner implementation notes

- Use `multiprocessing.Pool` keyed by file for the parse + canonicalize
  stage; hashing and indexing run in the main process.
- Memory: for a 50k-file corpus, keep only `CanonicalSubtree`s that pass
  filters in RAM; write rejected subtrees' stats to the report's filter
  counter without retaining the full object.
- Wall-clock timing uses `time.perf_counter`; peak RSS via
  `resource.getrusage(RUSAGE_SELF)` (Linux).
- Deterministic output: sort clusters by `(size, node_count, first_location)`
  so diffs between runs are meaningful.

## Tests

`experiments/ast_dedup/tests/test_runner.py`:

1. Small synthetic corpus (3 files, 2 duplicated functions) produces a
   report with `merkle_exact.cluster_count == 1, largest_cluster_size == 2`.
2. `--strategies merkle_exact` honors the selection filter.
3. `--max-files` honors the limit.
4. Report JSON round-trips through `json.loads`.
5. Report markdown is ≤ 4 KB for the synthetic corpus.

## Checklist

- [x] `experiments/ast_dedup/corpora.py` with `CORPORA` registry + `ensure()`
- [x] `experiments/ast_dedup/stats.py` with `CorpusReport` dataclass +
      JSON/markdown emitters
- [x] `experiments/ast_dedup/run.py` CLI with flags above
- [x] `tests/test_runner.py` with cases 1-5
- [x] Runs successfully on `emend` corpus end-to-end
- [x] Runs successfully on `django` corpus end-to-end (network clone succeeded on 2026-04-15; follow-on sweep also populated `fastapi`, `flask`, `lark`, `sqlalchemy`, and `sympy` into the cross-repo SQLite corpus)
- [x] Reports pinned in `experiments/ast_dedup/reports/` for future
      regression comparison
