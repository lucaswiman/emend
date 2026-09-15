"""Benchmark extraction and correlated sanitizer analysis on synthetic functions.

Run with the checkout's built environment:
    uv run --no-project --python .venv/bin/python benchmarks/bench_flow_paths.py
Use PYTHONPATH to select another built checkout for a baseline comparison.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from emend import emend_core
from emend.checks.flow import (
    CompiledFlowConfig, FlowSanitizer, FlowSink, FlowSource, evaluate_flow_config,
)


def cases():
    for size in (100, 500, 1000):
        yield f"straight-{size}", "x = source()\n" + "".join(
            f"v{i} = {i}\n" for i in range(size)) + "clean(x)\nsink(x)"
        yield f"protected-{size}", "x = source()\ntry:\n" + "".join(
            f"    v{i} = x + {i}\n" for i in range(size)
        ) + "    clean(x)\nexcept Exception:\n    x = 0\nsink(x)"
        yield f"chain-{size}", "clean(0)\nx = source()\n" + (
            "x = transform(x)\n" * size) + "sink(x)"
    for size in (8, 12, 16):
        yield f"branch-{size}", "x = source()\n" + "".join(
            f"y{i} = x\nif flag{i}:\n    y{i} = 0\n" for i in range(size)
        ) + "clean(x)\nsink(" + "+".join(f"y{i}" for i in range(size)) + ")"
    for size in (10, 30, 100):
        yield f"sources-{size}", "".join(
            f"x{i} = source()\nclean(x{i})\nsink(x{i})\n" for i in range(size))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    config = CompiledFlowConfig(
        labels=["value"], sources=[FlowSource("source()", "value")],
        sinks=[FlowSink("sink($X)", "value", "unsafe")],
        sanitizers=[FlowSanitizer("clean($X)", "value")],
    )
    for name, body in cases():
        source = "def f():\n" + "".join("    " + line + "\n" for line in body.splitlines())
        extraction, analysis = [], []
        for _ in range(args.repeat):
            start = perf_counter()
            facts = emend_core.build_flow_facts(source, "py")
            extraction.append(perf_counter() - start)
            with TemporaryDirectory(prefix="emend-flow-bench-") as folder:
                path = Path(folder) / "app.py"
                path.write_text(source)
                start = perf_counter()
                rows = evaluate_flow_config(config, [str(path)], project_path=folder)
                analysis.append(perf_counter() - start)
        print(json.dumps({
            "case": name, "events": len(facts["events"]),
            "extract_seconds": round(median(extraction), 6),
            "analysis_seconds": round(median(analysis), 6),
            "warnings": len(rows), "engines": sorted({row.engine for row in rows}),
        }), flush=True)


if __name__ == "__main__":
    main()
