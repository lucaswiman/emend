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
| `test_interprocedural_trace_{typescript,rust}.py` | Arguments, returned taint, transitive summaries and three-hop chains, late sanitizers, TS async (best effort). Cases that only renamed identifiers were dropped. |
| `test_transform.py` | Seven literal/node-kind constraints and six quote-stripping interpolation forms; statement captures, invalid interpolation and dry runs stay separate. |
| `test_type_oracle.py`, `test_typeoracle_integration.py` | Language type structure, balanced delimiters, oracle constraints, engine selection and precedence. |
| `test_fact_graph.py` | Symbol filters (including combined filters), trace-flow filters and `build_from_files`. |
| `test_import_extraction.py` | Per-language import rows: seven single-import Rust forms (exact rows), nested use trees, multi-line Python imports, and TS forms that produce no facts. Multiple source lines remain separate. |
| `test_multi_language.py` | Extension detection, config sections, TypeScript/Rust symbol discovery and pattern matching; Rust integer matching retains a separate best-effort smoke test. |
| `test_language_registry.py` | Registry freshness, `resolve_files`, exported names, DSL extension detection, and one parse smoke case per DSL extension (`parse_source` returns `None` without a grammar). |
| `test_dead_code.py`, `test_cfg_{typescript,rust}.py` | Shared warm-index setup and def-use collection; scenario assertions and source fixtures remain explicit. |
| `test_trace.py`, `test_interprocedural_trace.py`, `test_policy.py` | Engine scenarios plus the CLI routes: `trace --json` API/CLI parity, `trace --interprocedural` JSON schema and text summary, `engine == "occurrence"`, summaries/iterations, flow-policy API/CLI parity. |
| `test_refs.py` | `refs` write/read filters, nested-symbol qualified names, callers and callees, with their CLI commands. |
| `test_graph.py` | Python call graph in plain/JSON/DOT and through the CLI (`test_graph_{rust,typescript}.py` are the language companions). |
| `test_show.py` | `search` code output, `--output metadata`, line selectors and lookup dispatch. |
| `test_ast_utils.py`, `test_project_iter.py` | Symbol-tree helpers (`find_nested_definitions`, path/line lookup, wildcards); `visit_project_ts` and `_file_to_module`. |
| CLI/MCP/Vim adapters | Named help groups, top-level aliases (`test_cli_surface_consolidation.py`) and both trace routes; exact trace output, duplicate defaults and asynchronous indexing responses. All MCP tool tests live in `test_mcp_server.py`, which skips as a module without the `mcp` SDK. |

Cache identity, edits/reverts, worktree reuse, overlays, concurrent publication,
and incremental/full-rebuild parity retain their dedicated stateful tests.

There are no `test_phaseN*.py` suites: their surviving cases live in the topical
files above (completion ranking in `test_completion_ranking.py`, the duplicate
cache in `test_duplicate.py`).
