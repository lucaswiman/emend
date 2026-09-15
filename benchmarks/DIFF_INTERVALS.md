# Synthetic diff interval benchmark (#287)

Baseline: `71411b2fda7777cda6c244afba6b9da006f86d37`.
Measured on Linux, CPython 3.14.3 free-threaded, using
`benchmarks/bench_diff_intervals.py`. Each case parses synthetic hunk headers,
translates through one insertion, and performs 1,000 finding-span lookups.
Elapsed times are medians of five untraced runs; peak allocated bytes are the
maximum of five separate `tracemalloc` runs. Input construction and module
import are outside the measurements.

| Synthetic case | Before time | After time | Before peak bytes | After peak bytes | Before/after stored entries |
| --- | ---: | ---: | ---: | ---: | ---: |
| One million-line insertion | 117.280 ms | 0.355 ms | 133,112,450 | 6,701 | 1,000,000 / 2 |
| 10,000 separated one-line hunks | 20.843 ms | 21.697 ms | 3,762,826 | 5,805,664 | 10,000 / 10,000 |
| One three-line hunk | 0.209 ms | 0.249 ms | 2,492 | 2,868 | 3 / 1 |

The insertion used for translation splits the million-line selection into two
intervals: its inserted line remains unselected. Storage now grows with hunk
count rather than selected line count. Singleton hunks cost more memory because
each interval stores two endpoints; this benchmark intentionally shows that
tradeoff. The small case adds about 40 microseconds for the entire parse,
translation, and 1,000-lookups workload. These are synthetic measurements,
not end-to-end CLI or repository performance claims.

Reproduce from the repository root (use an available Python interpreter with
`uv run --no-project --python ...` if needed):

```sh
git show 71411b2fda7777cda6c244afba6b9da006f86d37:src/emend/git_diff.py > /tmp/emend-git-diff-before.py
uv run --no-project python benchmarks/bench_diff_intervals.py /tmp/emend-git-diff-before.py
uv run --no-project python benchmarks/bench_diff_intervals.py src/emend/git_diff.py
```

## Bounds and remaining costs

Parsing and translation retain sorted, disjoint half-open intervals. Translation
sweeps selected intervals and edit hunks once. Finding intersection uses binary
search after rejecting spans outside the overall selection. Impact selection
sweeps symbol boundaries with depth-first ownership priority, preserving nested
and overlapping symbol semantics without iterating selected lines.

Git subprocess stdout and `_parse_diff`'s `splitlines()` still retain patch text.
Avoiding that separate source of memory use would require streaming the patch
boundary; this change bounds selection storage only. Synthetic header-only
inputs deliberately exclude patch-body buffering, source parsing, and Git I/O.
