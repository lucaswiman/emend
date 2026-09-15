# Correlated sanitizer paths (#285)

`all_paths` sanitizer checks now carry live value identities along the occurrence
control graph. Copies retain identity; evaluations produce derived identities.
Distinct live states remain separate at joins, and loop iterations create fresh
identities before canonicalizing their alias classes. Resolved calls initialize
all arguments together and return to their own call site. Witnesses follow the
accepted state sequence and share predecessor records during reconstruction.

The native graph stores immutable protected-region spans. Inner handlers,
failed handler assignments, unmatched catches, else bodies, and mandatory
finalizers preserve the value generations that actually survive. Finalizer exits
can conservatively resume either normal control or an enclosing exception.
Handler dispatch is separate from body execution: a mismatching clause cannot
apply that handler's writes before the next clause. Normal handler completion
and return/break/continue exits execute mandatory finalizers. Known built-in
catch-type names and TypeScript catch-parameter bindings use language configuration;
custom and dynamic catch-type expressions still participate in exception flow.
Extraction artifact version 20 invalidates cached facts with the old routing.

## Bounds and limitations

- Graph preparation and backward liveness are shared across sources for a rule.
  Reaching-definition extraction still uses its existing shared dataflow; there
  is no new traversal per definition. Protected-region routing scans regions and
  events, as before, with an additional region scan for finalizer continuations.
- Exact state exploration can grow exponentially with independent branches.
  The evaluator allows 10,000 additional states beyond one per occurrence, so a
  long straight-line function does not exhaust the budget just for being long.
  Exhaustion or recursive call re-entry retains a conservative violation with
  `engine="occurrence-budget"`, an explicit correlation-limit message, and no
  claimed witness. The existing configurable call-depth bound still applies.
- Paths refer to the extracted CFG, not a proof of branch-predicate feasibility.
  Exception typing remains approximate: configured broad catch types describe
  ordinary exception dispatch, and complete subclass/shadowing inference is
  unavailable. Independent baseline/current probes confirmed the inherited
  limitation for explicitly raised `KeyboardInterrupt`, `SystemExit`, and
  `BaseException` escaping `except Exception` into an outer `except BaseException`.
  This change does not claim to repair that preexisting exception-class analysis.
  Catch aliases and tuples (for example `except Exception as err` and
  `except (Exception,)`) also retain conservative warnings in nested overwrite
  cases; these warnings were reproduced on both main and the reviewed change.
- `some_path` retains its existing existential sanitizer semantics.

## Reproduction

Build each checkout's extension with its own environment, then run:

```sh
uv run --no-project --python .venv/bin/python benchmarks/bench_flow_paths.py
```

The script reports medians of three fresh-project runs, separately timing native
occurrence extraction and the full public evaluator. `PYTHONPATH` can select a
second built checkout for comparison. Cases cover large ordinary/protected
functions, long tainted chains, independent branches, and many sources/sinks.

## Diagnostic run

Baseline: main `71411b2`; CPython 3.14t; release native extensions. These runs
shared the machine with the full test suite, so the table is a scalability
check, **not a speedup claim**. Times are seconds, medians of three runs.

| Case | Baseline extraction | Changed extraction | Baseline full analysis | Changed full analysis |
|---|---:|---:|---:|---:|
| 100 ordinary assignments | 0.00109 | 0.00136 | 0.194 | 0.495 |
| 1,000 ordinary assignments | 0.01146 | 0.01570 | 0.565 | 0.488 |
| 100 protected assignments | 0.00545 | 0.00585 | 0.196 | 0.356 |
| 500 protected assignments | 0.09518 | 0.11256 | 0.389 | 0.606 |
| 1,000 protected assignments | 0.40599 | 0.36586 | 1.046 | 1.026 |
| 500 tainted transformations | 0.02272 | 0.02831 | 2.912 | 3.348 |
| 1,000 tainted transformations | 0.05308 | 0.05651 | 11.585 | 12.522 |
| 100 independent source/sink pairs | 0.01231 | 0.01302 | 0.706 | 0.525 |

Protected cases now suppress the false warning because their exception handler
replaces the tainted binding. Long transformation chains still report the sink
with a witness. A separate profile of the 500-transformation case attributed
2.76 of 3.23 seconds to ensuring disk facts; the table does not attribute that
existing end-to-end cost to the new state exploration.

The 8-branch alias case completed exact analysis without a warning. The 12- and
16-branch cases reached the explicit budget and emitted a conservative warning
without a witness (0.438 and 0.434 seconds respectively). These safe synthetic
cases deliberately demonstrate the cost/precision boundary rather than hiding
exponential exploration behind an unsound merge.

Validation: final `make test-ci` passed the Rust suite and Python suite, including
existing failed-call, failed-assignment, non-call exception, sanitizer, and
interprocedural regressions. New compact fixtures cover Python/TypeScript
handlers and finalizers, Python/TypeScript/Rust identity distinctions, loops,
multiple call arguments, repeated void calls, and conservative budget behavior.
