# Behavioral contracts

Engine tests describe source scenarios and expected behavior. Share file creation,
configuration and execution setup with fixtures; use named parameter cases when
the assertions are the same. Keep language-specific syntax and exceptions visible
in the scenario data. Multiline source remains multiline, especially when line
numbers, scopes or statement ordering matter.

Adapter tests pin argument forwarding, defaults, error handling and output shape.
Retain real end-to-end examples for each public route; mocks alone cannot verify
that an adapter and engine still work together. Lifecycle, concurrency and cache
invalidation tests should keep their action sequences explicit.

When consolidating tests:

- Map every old semantic case and assertion to a named scenario or retained test.
- Compare exact values or structures where practical. Do not compute expected
  values with the same implementation under test, or use round trips as the sole
  oracle for a parser.
- Keep smoke tests visibly distinct from assertions about supported behavior.
  An empty result must not make a positive behavior test pass vacuously.
- Run representative deliberate mutations and check that the corresponding
  contracts fail at the intended assertions, then restore normal behavior.
- Measure all replacement code and fixtures, not just deleted test functions.
  Fewer collected tests is not evidence of simplification or preserved coverage.

Run tests through `make test`, which builds the required native extension. Include
the MCP extra when changing its adapters; a module-level skip is not validation.

## Consolidated suites

| Contract | Scenario inventory |
| --- | --- |
| `test_trace_{typescript,rust}.py` | Direct and alternate sources, assignments, branches, loops, sanitization, TS arrows/subscripts/try-catch, Rust match arms; container propagation remains explicitly best-effort. |
| `test_interprocedural_trace_{typescript,rust}.py` | Arguments, returned taint, transitive summaries, late sanitizers, TS delegation/async, Rust method/closure analogues. |
| `test_transform.py` | Seven literal/node-kind constraints and six quote-stripping interpolation forms; statement captures, invalid interpolation and dry runs stay separate. |
| `test_type_oracle.py`, `test_typeoracle_integration.py` | Language type structure, balanced delimiters, oracle constraints, engine selection and precedence. |
| `test_fact_graph.py` | Symbol filters (including combined filters), trace-flow filters and seven single-import Rust forms. Nested imports and multiple source lines remain separate. |
| `test_multi_language.py` | Extension detection, config sections, TypeScript/Rust symbol discovery and pattern matching; Rust integer matching retains a separate best-effort smoke test. |
| `test_dead_code.py`, `test_cfg_{typescript,rust}.py` | Shared warm-index setup and def-use collection; scenario assertions and source fixtures remain explicit. |
| CLI/MCP/Vim adapters | Named help groups and both trace routes; exact trace output, duplicate defaults and asynchronous indexing responses. |

Cache identity, edits/reverts, worktree reuse, overlays, concurrent publication,
and incremental/full-rebuild parity retain their dedicated stateful tests.
