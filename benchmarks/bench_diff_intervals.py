"""Synthetic diff parse/translation/filter benchmark, including pre-change modules.

Usage: uv run --no-project python benchmarks/bench_diff_intervals.py PATH_TO_git_diff.py
Input headers omit patch bodies: this isolates selection storage, not Git I/O.
"""

import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

spec = importlib.util.spec_from_file_location("bench_diff", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

for name, headers in [
    ("million_lines", "@@ -0,0 +1,1000000 @@\n"),
    ("10000_small_hunks", "".join(f"@@ -{i * 4 + 1},1 +{i * 4 + 1},1 @@\n" for i in range(10000))),
    ("small_diff", "@@ -10,3 +10,3 @@\n"),
]:
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n" + headers

    def run():
        parsed = module._parse_diff(patch)
        mapped = module._map_lines(parsed[0].lines[1], [(1, 0, 2, 1)])
        selection = module.DiffSelection(Path("/tmp"), {"/tmp/a.py": mapped})
        for i in range(1000):
            selection.matches("/tmp/a.py", i * 37 + 1, i * 37 + 9)
        return len(getattr(mapped, "intervals", mapped))

    times, peaks = [], []
    for _ in range(5):
        tracemalloc.start()
        stored = run()
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    print(json.dumps({"case": name, "median_seconds_untraced": statistics.median(times),
                      "peak_bytes": max(peaks), "stored_entries": stored}))
