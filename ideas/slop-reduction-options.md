# Large simplification options

Proposals only: none of these architectural replacements is implemented by
PR #226. Estimates are physical lines, include replacement code and tests,
and require a prototype to validate. Scopes overlap; do not add the estimates.

| Option | Inspected footprint | Estimated net reduction |
| --- | --- | --- |
| Behavioral-contract test suites | About 55K Python test lines | 5–8K |
| Replace custom pattern machinery with ast-grep | 5,883 compiler/matcher source lines, at least 4,744 relevant test lines | 3–5.5K |
| One compiled rule model and flow evaluator | About 10K source and 10K relevant test lines | 2–4K |
| One analysis snapshot/cache owner | About 8.6K index/editor/type source lines, plus FactGraph | 2–4K |

## Behavioral-contract test suites

The strongest candidate for deleting more than 5K lines is repeated test
harness code. The audit counted roughly 3,000 test definitions and 1,579
`write_text()` calls, not 3,000 redundant behaviors. For example,
`test_trace_typescript.py` and `test_trace_rust.py` repeatedly construct files,
configure the same source/sink contract, execute it, and assert violations.

Organize engine tests around explicit scenarios with language-specific source
data. Exercise CLI, MCP and editor adapters for argument/output contracts,
retaining a smaller real end-to-end set. Source fixtures still count toward
the line total; moving inline strings into files is not a reduction.

Before committing: inventory every semantic case and language exception;
prove representative broken implementations still fail the consolidated
tests. Preserve readable scenario names and precise assertions. The 5–8K
estimate is a hypothesis about repeated harnesses, not a measured deletion.

## Replace the custom structural matcher

`pattern.py` (1,201 lines), `rust/src/pattern.rs` (708), and
`rust/src/matcher.rs` (3,974) total 5,883 source lines. Python patterns currently
use a Python AST compiler, while other languages use tree-sitter before
feeding a custom IR and matcher.

Prototype a small emend syntax/constraint adapter over ast-grep's Rust library.
Its [rule language](https://ast-grep.github.io/reference/rule.html) supplies
structural and relational predicates, and its
[programmatic API](https://ast-grep.github.io/guide/api-usage) supplies tree
inspection and edit support. Retain type-oracle postfilters and byte-edit
operations where necessary.

This is not a drop-in replacement. Its
[FAQ](https://ast-grep.github.io/advanced/faq) documents fragment-context and
metavariable constraints. Differentially test spans, captures, variadics,
repeated metavariables, headers, comments, malformed input and replacements
across languages; benchmark project searches. Exceeding 5K net deletions
requires both a small adapter and test consolidation.

An internal alternative is finishing one tree-sitter compiler. Direct probes
showed the current Rust path does not preserve Python comprehension,
exception/header and wildcard-definition semantics, so deleting the Python
compiler today would remove supported behavior.

## One rule model and flow evaluator

`checks/flow.py` still selects different evaluators depending on whether a
FactGraph was supplied. `trace.py` has a separate CFG/Datalog path. The current
cleanup removes the lint-to-flow adapter roundtrip, but preserves these
existing semantics; it does not complete the migration.

Compile lint, policy and trace configuration into one representation, resolving
source/sink matches, effects, scopes and witnesses once. Keep existing commands
and configuration forms as thin compatibility boundaries.

Define contracts for assignment ordering, same-line statements, loop-carried
flow, nested/module scopes, overwritten values, all/some-path sanitizers and
interprocedural summaries. An attempted source-line filter suppressed valid
same-line and loop-carried flows during this audit and was discarded. Correct
ordering belongs in CFG/dataflow facts, not an output filter. Parity must
preserve intended semantics, not copy known deficiencies of the old tracker.

## One analysis snapshot and cache owner

FactGraph imports extraction from `transform.cache`, which imports FactGraph
definitions/helpers back. Symbol/import/reference indices, Cozo relations,
editor state and type results have overlapping refresh and persistence paths.

Use a dependency-neutral analysis snapshot produced by extraction and consumed
by query engines and derived indices. Give one owner responsibility for cache
identity, validity and rebuilds; SQLite FTS can remain a derived search index.

Type inference exposes a concrete unresolved distinction: adapter memoization
must include engine/configuration/project identity, while dead-code analysis
asks for the latest available types without starting an engine. The current
path/content key fix prevents cross-file collisions but does not resolve that
contract or invalidate results after imported dependencies change.

Gate the redesign on incremental/full rebuild parity, identical files in
different modules, engine/config/dependency changes, unsaved editor buffers,
concurrency and startup latency. A cycle-free ownership model is valuable even
if the net deletion falls below 5K.

Suggested order: consolidate behavioral tests, specify the single rule
evaluator, and run a bounded ast-grep compatibility experiment before choosing
a matcher replacement.
