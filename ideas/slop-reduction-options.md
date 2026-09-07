# Simplification options: current-stack audit

Reconciled on 2026-09-07 against `2149de6` (#233), using six Luna audit
lanes and parent review. Footprints are physical lines at that baseline,
not assumed deletions. Estimated net savings include replacement code and
tests; ranges overlap and must not be added. Larger changes remain proposals.

## Completed work, not new proposals

| Earlier option | Current status |
| --- | --- |
| Python tree-sitter migration | #230 removed the separate Python AST compiler, retaining partial patterns. |
| One analysis snapshot/cache owner | #231 owns freshness and shared content artifacts, including worktree/edit/revert reuse. Its correctness machinery increased LOC; this is not a future 2–4K deletion claim. |
| One compiled rule model/flow evaluator | #232 removed competing rule/flow paths; subsequent cleanup must build on that evaluator. |
| Behavioral-contract suites | #233 removed 922 total lines including its guide, with structural oracles and mutation checks. The original 5–8K hypothesis was not achieved. |

The misc follow-up keeps small behavior-preserving changes: common native
parser dispatch and symbol formatting, ordered config-layer merging, removal
of unused allocations/imports, and the frozen dataclass's generated hash.
Compatibility exports and public command aliases remain intact.

## Prioritized additional refactors

| Proposal | Inspected footprint | Hypothesized net reduction |
| --- | --- | --- |
| One CLI/MCP command service boundary | 4,058 CLI + 1,733 MCP source lines | 400–1,000 |
| Config-driven embedded-language extraction | 1,588 DSL source + 2,050 dedicated test lines | 700–1,300 |
| One symbol projection for lookup/index/summary | 264 `ast_utils` + 302 `ast_commands` + 542 `query` + 1,498 `transform/index` lines | 300–750 |
| One benchmark harness | 1,666 lines in five benchmark/support modules | 300–600 |
| Canonical/generated user reference | 7,088 lines under `docs/` and `ideas/` | 800–1,500, documentation only |

### 1. One command service boundary

`cli_find.py` and `mcp/find.py`, and the analysis/edit/check counterparts,
repeat scope normalization, dispatch, result conversion, and error handling.
Move shared domain operations into small typed services; keep Typer/MCP
wrappers responsible for their different input, error and serialization
contracts. Delete duplicate orchestration, not features. Do not introduce a
generic command-description interpreter merely to shorten wrappers.

Prototype one search or analysis operation first. Preserve default scope,
single/multiple files, hidden aliases, dry-run/apply, JSON fields, CLI exit
codes and MCP schemas. Retain real end-to-end tests for both transports.
Measure total code/tests and cold/warm invocation cost before broadening it.

### 2. Embedded-language extraction

`dsl.py` has separate SQL/Jinja/GraphQL region, symbol and link paths plus
repeated dispatch in `analyze_file`. Standardize region/source-offset mapping
and symbol records around configured tree-sitter queries. Keep ORM, template,
GraphQL and regex-group linking rules explicit where their semantics differ;
moving branches into separate plugin classes alone saves nothing.

First prototype two existing extraction paths. Compare exact regions, byte
offsets, symbols and links on the existing 2,050-line test corpus, including
Unicode, multiline/malformed literals, magic comments and standalone files.
Benchmark project extraction. No supported language is removed, and no
structural parser is replaced with raw-source regexes.

### 3. One symbol projection

`ast_utils.py`, `ast_commands.py`, `query.py` and `transform/index.py` adapt
tree-sitter symbol data for different consumers. Inventory those conversions,
then provide one immutable projection with consumer-specific views for paths,
signatures and hierarchy. Delete only genuinely repeated conversion/remapping
code. This is not another cache owner: reuse `AnalysisStore` and its snapshots.

Gate on exact CLI/editor output, nested/anonymous symbols, module-qualified
names, unsaved overlays, identical content at distinct paths, worktrees and
incremental/full-index parity. No extra parse/type work on warm queries.

### 4. Benchmark and documentation consolidation

`bench_django.py` (467), `bench_cozodb.py` (449), `goto_def_audit.py` (507),
`django_checkout.py` (196), and `bench_utils.py` (47) are candidates for shared
process/timing/output/checkout handling. Keep workload and correctness-audit
logic independent. Preserve JSON, accepted exit codes, cold/warm labels and
cache isolation; run each benchmark family before accepting the replacement.

For docs, choose one reference per concept and generate repeated CLI/MCP
tables from existing registration metadata. Validate with strict Sphinx and
link checks, accounting for every visible command/config key. Do not silently
delete unresolved idea documents or call shorter documentation runtime-code
simplification. Generation must save more maintenance than its generator adds.

## High-ceiling experiment: replace the custom matcher

This remains an unimplemented earlier proposal, not a newly discovered cut.
The current footprint is 5,892 source lines: `pattern.py` (369), Rust
`pattern.rs` (746), and `matcher.rs` (4,777). Five selected pattern/find/sequence
suites alone contain 2,797 lines; these cases are not presumed redundant.

Prototype an adapter to ast-grep, retaining emend's scope/type filters and
byte-edit layer. Its [rule model](https://ast-grep.github.io/reference/rule.html)
supports structural/relational composition and contextual patterns, but its
[FAQ](https://ast-grep.github.io/advanced/faq.html) describes syntax/matching
constraints. Neither source proves compatibility with emend's partial patterns.

Require differential spans/captures, variadics, repeated metavariables, partial
headers, comprehensions, comments, malformed input and replacements across
languages. Preserve source overrides and occurrence-flow endpoints. Benchmark
project search. A 3–5K net cut is plausible only if most custom machinery can
actually be deleted; count zero savings until the prototype establishes that.

## Do not implement as incidental cleanup

- Retiring experimental `saturate` could remove roughly 1.3–1.5K lines including
  tests/docs, but is a product decision. Its shared `UnionFind` is also used by
  duplicate detection. Keeping it calls for a parser/e-graph compatibility
  experiment, not silent feature deletion.
- `transform/cache.py` is now 283 lines, largely schema and delegates. Audit
  individual legacy table readers before retiring anything; do not repeat the
  completed owner migration or touch user-authored mappings or deferred GC.
- More project fixtures may help isolated suites, but raw `write_text` counts
  do not justify another multi-thousand-line estimate after #233. Require an
  actual case mapping and mutation-preserving prototype first.
- Language/CLI alias removal and optionalizing default type engines change
  user contracts. They are not included in the design savings above.
