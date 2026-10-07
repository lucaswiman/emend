"""Bounded release latency probes; use PYTHONPATH to select a built checkout.

Run arms in alternating order on an idle machine. Setup, imports and fact
materialization are excluded except in the cold index and native extraction probes.
This measures synthetic workloads, not a production SLA.
"""
import argparse
import gc
import json
import shutil
import subprocess
import tracemalloc
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

import emend
from emend import emend_core
from emend.analysis_store import AnalysisStore
from emend.checks.flow import CompiledFlowConfig, FlowSanitizer, FlowSink, FlowSource, evaluate_flow_config
from emend.component_selector import NestedSymbol
from emend.editor_search import EditorSearchEngine
from emend.git_diff import LineIntervals, read_diff
from emend.transform.impact import _symbols_in_intervals
from emend.transform.index import warm_caches


def git(root, *args):
    subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


def prepare(case, root):
    if case in {'straight', 'finalizers', 'empty-finalizers'}:
        if case == 'straight':
            body = 'x = source()\n' + 'x = transform(x)\n' * 1000 + 'sink(x)'
        elif case == 'finalizers':
            body = 'x = source()\nwhile flag:\n' + ''.join(
                f'    try:\n        if flag{i}: continue\n        x = transform(x)\n'
                '    finally: clean(x)\n' for i in range(200)) + 'sink(x)'
        else:
            body = 'source()\n' + 'if True:\n    try: pass\n    finally: pass\n' * 12 + 'sink()'
        source = 'def f():\n' + ''.join('    ' + line + '\n' for line in body.splitlines())
        return lambda: emend_core.build_flow_facts(source, 'py')
    if case in {'effect-reads', 'some-path', 'many-sanitizers', 'unrelated-functions'}:
        if case == 'effect-reads':
            source = 'def f():\n    x = source()\n' + ''.join(f'    v{i} = x + {i}\n' for i in range(200))
            sink = FlowSink('', 'value', 'unsafe', effect='reads($X)')
            sanitizers = []
        else:
            existential = case in {'some-path', 'many-sanitizers'}
            source = ('def f():\n    x = source()\n'
                      + '    clean(x)\n' * (50 if case == 'many-sanitizers' else 1)
                      + '    sink(x)\n' * (100 if existential else 1))
            sink = FlowSink('sink($X)', 'value', 'unsafe')
            sanitizers = [FlowSanitizer('clean($X)', 'value', quantifier='some_path' if existential else 'all_paths')]
            if case == 'unrelated-functions':
                source += ''.join(f'def other{i}():\n' + ''.join(f'    v{j} = {j}\n' for j in range(10)) for i in range(300))
        path = root / 'app.py'
        path.write_text(source)
        config = CompiledFlowConfig(sources=[FlowSource('source()', 'value')], sinks=[sink], sanitizers=sanitizers)
        graph = AnalysisStore.open(root).query_facts()
        def analyze():
            rows = evaluate_flow_config(config, [str(path)], project_path=str(root), graph=graph)
            assert bool(rows) is (case == 'effect-reads')
            return rows
        return analyze
    if case in {'picker', 'cold-picker'}:
        git(root, 'init', '-q')
        for i in range(10000):
            (root / f'file{i:05}.txt').touch()
        git(root, 'add', '.')
        if case == 'cold-picker':
            def first_query():
                engine = EditorSearchEngine(str(root))
                try:
                    return engine.search('file')
                finally:
                    engine.close()
            return first_query
        engine = EditorSearchEngine(str(root))
        engine.search('file')
        return lambda: engine.search('file')
    if case in {'warm-index', 'cold-index'}:
        for i in range(150):
            (root / f'module{i}.py').write_text(f'def function{i}(x):\n    return x + {i}\n')
        if case == 'warm-index':
            warm_caches(str(root), jobs=4, type_engine=None)
        return lambda: warm_caches(str(root), jobs=4, type_engine=None)
    if case == 'symbol-selection':
        symbols = [NestedSymbol(f'f{i}', 'function', i*3+1, i*3+2, 0, [f'f{i}']) for i in range(30000)]
        lines = LineIntervals([(45001, 45002)])
        def select():
            selected = list(_symbols_in_intervals(symbols, lines))
            assert [symbol.name for symbol in selected] == ['f15000']
            return selected
        return select
    if case == 'patch-payload':
        git(root, 'init', '-q')
        git(root, 'config', 'user.name', 'benchmark')
        git(root, 'config', 'user.email', 'benchmark@example.invalid')
        path = root / 'app.py'
        path.write_text('original = 0\n')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'baseline')
        path.write_text('original = 0\n' + 'assignment = 1234567890\n' * 100000)
        def patch():
            selected = read_diff(root, 'HEAD')
            assert selected[0].lines[1].intervals == [(2, 100002)]
            return selected
        return patch
    raise ValueError(case)


CASES = ('straight', 'finalizers', 'empty-finalizers', 'effect-reads', 'some-path', 'many-sanitizers',
         'unrelated-functions', 'picker', 'cold-picker', 'warm-index', 'cold-index',
         'symbol-selection', 'patch-payload')


def close_stores():
    for store in tuple(AnalysisStore._instances.values()):
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, action='append')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--memory', action='store_true')
    args = parser.parse_args()
    for case in args.case or CASES:
        with TemporaryDirectory(prefix='emend-latency-') as folder:
            root = Path(folder)
            operation = prepare(case, root)
            # Warm lazy imports/grammar setup; keep cold project caches empty.
            operation()
            def reset():
                if case == 'cold-index':
                    close_stores()
                    shutil.rmtree(root / '.emend')
                gc.collect()
            samples = []
            for _ in range(args.repeat):
                reset()
                start = perf_counter()
                result = operation()
                samples.append(perf_counter() - start)
                del result
            peak = None
            if args.memory:
                reset()
                tracemalloc.start()
                operation()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            print(json.dumps({'case': case, 'seconds': median(samples), 'samples': samples,
                              'python_peak_bytes': peak, 'module': emend.__file__,
                              'native': emend_core.__file__}), flush=True)
            close_stores()


if __name__ == '__main__':
    main()
