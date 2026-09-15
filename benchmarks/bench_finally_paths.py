"""Native extraction cost for repeated finalizers with conditional loop exits.

Run with each checkout's built extension and PYTHONPATH=src:
    uv run --no-project --python .venv/bin/python benchmarks/bench_finally_paths.py
"""

import json
from statistics import median
from time import perf_counter

from emend import emend_core


for count in (50, 100, 200):
    source = "def f():\n    x = source()\n    while flag:\n" + "".join(
        f"        try:\n            if flag{i}:\n                continue\n"
        "            x = transform(x)\n        finally:\n            clean(x)\n"
        for i in range(count)
    ) + "    sink(x)\n"
    times = []
    for _ in range(3):
        start = perf_counter()
        facts = emend_core.build_flow_facts(source, "py")
        times.append(perf_counter() - start)
    print(json.dumps({"finalizers": count, "seconds": median(times),
                      "events": len(facts["events"]), "edges": len(facts["edges"])}), flush=True)
