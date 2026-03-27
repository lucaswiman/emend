# Next Static Analyses: Implementation Priorities

Based on a literature review of 21 techniques across program analysis,
ranked by feasibility and value given emend's existing infrastructure.

See [static-analysis-literature-review.md](static-analysis-literature-review.md)
for full details, papers, and implementation strategies.

## Key Insight: Most Analyses Are Datalog Queries

Many of the techniques in the literature review reduce to "transitive closure
over program-graph edges with different filters."  Once a Datalog engine exists
on the fact graph, these become rule sets rather than separate implementations:

- **Program slicing** — `InSlice(def) :- InSlice(use), UsesVar(use, V), DefinesVar(def, V).`
- **CFL-reachability** — matched-parenthesis reachability is expressible as recursive Datalog rules
- **IFDS/IDE** — IFDS problems *are* Datalog programs (this is how Doop and CodeQL work)
- **Points-to analysis** — Andersen's analysis is the canonical Datalog textbook example
- **Effect inference** — `HasEffect(f, e) :- Calls(f, g), HasEffect(g, e).`

These are marked **won't do separately** below — they'll ship as example
rule sets for the Datalog engine rather than as bespoke implementations.

## Priorities

### 1. Datalog Engine on the Fact Graph ✅

CozoDB v0.7.6 integrated as a Rust dependency in `emend_core`.
`FactGraph` is now CozoDB-backed with Datalog recursive rules for
transitive closures and dead code.  `emend query` command and
`DatalogCheck` policy type are live.  All `parse.db` fact queries
(`query_symbol_index`, `query_reference_index`, `query_import_graph`,
`_find_dead_code_cached`) now use CozoDB with SQLite fallback.

**Key papers:** Whaley & Lam (PLDI 2004), Scholz et al. (CC 2016) (Soufflé),
Smaragdakis & Bravenboer (FTPL 2011).

**Reuses:** fact_graph.py, policy engine YAML parsing.
**Enables:** User-defined analyses, a query language for the MCP server, and a
unification layer for taint, dead code, and type checks.  Also subsumes
program slicing, CFL-reachability, IFDS/IDE, points-to, and effect inference
as rule sets rather than separate implementations.
**Effort:** Medium.

### 2. Per-Function Control Flow Graphs

Build CFGs in Rust as part of the existing tree-sitter parse pass, expose
via PyO3.  This is the intraprocedural counterpart of the Datalog engine:
the Datalog engine unlocks interprocedural query-side analyses, while CFGs
unlock intraprocedural path-sensitive analyses.

**Design:** See [control-flow-graphs.md](control-flow-graphs.md).

**Reuses:** emend_core tree-sitter infrastructure, scope resolver's AST walk.
**Enables:** Path-sensitive taint, must-close-on-all-paths typestate,
`if x is not None` guard narrowing, unreachable code detection within
functions, `ControlDependsOn` / `DefUse` facts for the Datalog engine.
**Effort:** Medium (~1000 lines of Rust for core, ~200-300 lines Python per consumer).

### 3. Typestate Analysis

Track object protocol states (e.g., file: unopened -> opened -> closed) and
detect violations (reading from a closed file, forgetting to close).

**Key papers:** Strom & Yemini (IEEE TSE 1986), Fink et al. (ISSTA 2006),
Bierhoff & Aldrich (OOPSLA 2007).

**Reuses:** Taint engine (taint labels as states, sanitizers as transitions),
policy engine for protocol definitions.
**Enables:** Resource leak detection, protocol checking for Python files,
connections, locks, iterators.
**Effort:** Medium.
**Note:** The dataflow part is Datalog-expressible, but the protocol
definitions, aliasing handling, and must-close-on-all-paths logic need
dedicated machinery beyond just rules.

### 4. API Migration

Automated library upgrade patterns: given migration rules (old API -> new API),
rewrite callsites across a project with import updates.

**Key papers:** Padioleau et al. (Coccinelle, EuroSys 2008), Henkel & Diwan
(ICSE 2005), Dig & Johnson (ECOOP 2006).

**Reuses:** Mapping store (already has identifier/module mappings), batch
command, replace engine, rename/move infrastructure.
**Enables:** `emend migrate --from requests==1 --to requests==2` or rule-based
migration YAML files.
**Effort:** Medium (infrastructure is mostly there).

### 5. Specification Mining

Infer likely invariants and coding conventions from patterns in the codebase.
Flag anomalies where the convention is violated.

**Key papers:** Ernst et al. (Daikon, IEEE TSE 2001), Ammons/Bodik/Larus
(POPL 2002), Livshits/Zimmermann (FSE 2005).

**Reuses:** Pattern matching, fact graph, call graph.
**Enables:** "99/100 callers check the return value; this one doesn't."
Convention enforcement via the policy engine.
**Effort:** Low-medium.

### 6. Advanced Change Impact Analysis

Extend current caller-closure impact analysis with co-change detection (from
git history), field-sensitive impact, and test-coverage mapping.

**Key papers:** Ren/Shah/Tip/Ryder (Chianti, OOPSLA 2004), Gall/Hajek/Jazayeri
(ICSM 1998), Lehnert (IWPSE-Evol 2011).

**Reuses:** Impact analysis, git log integration, fact graph.
**Enables:** Files that historically co-change, field-level impact precision.
**Effort:** Low-medium.
**Note:** The transitive-closure part of impact analysis is a Datalog query,
but co-change mining from git history and field-sensitive tracking are not.

### 7. Incremental / Demand-Driven Analysis

Only re-analyze what changed rather than re-processing the whole project.
Critical for editor integration and large codebases.

**Key papers:** Acar et al. (Adapton, PLDI 2014), Szabó et al. (IncA, OOPSLA
2016), Arzt & Bodden (FlowDroid revisions).

**Reuses:** parse.db cache (content-hash keyed), editor-server.
**Enables:** Sub-second analysis updates after file saves.
**Effort:** Medium.

### 8. None/Optional Abstract Domain

A focused abstract interpretation that tracks whether variables may be None.
Python's most common runtime error is AttributeError on None.

**Key papers:** Cousot & Cousot (POPL 1977), Livshits et al. (OOPSLA 2015),
Logozzo & Fähndrich (SAS 2008).

**Reuses:** Taint engine (lattice structure maps directly), type oracle.
**Enables:** None-safety checking without full type annotations.
**Effort:** Medium.

### 9. LLM-Guided False Positive Reduction

Use the MCP server to let an LLM review analysis results and filter false
positives using semantic understanding.

**Reuses:** MCP server, all analysis outputs.
**Enables:** Higher precision without sacrificing recall.
**Effort:** Low (infrastructure is already there).

## Won't Do Separately (Subsumed by Datalog Engine)

These are all transitive-closure or reachability computations over program
graphs.  Once the Datalog engine ships, they become example rule sets:

| Technique | Why it's a Datalog query | Example rule |
|-----------|-------------------------|--------------|
| Program slicing | Transitive closure over def-use edges | `InSlice(d) :- InSlice(u), Uses(u,V), Defs(d,V).` |
| CFL-reachability | Matched-parenthesis reachability | `Reaches(a,b) :- Call(a,f,cs), Reaches(f,b), Ret(b,cs).` |
| IFDS/IDE | IFDS problems are Datalog programs | (Doop, CodeQL are existence proofs) |
| Points-to analysis | Andersen's = subset constraints | `PtsTo(p,o) :- Assign(p,q), PtsTo(q,o).` |
| Effect inference | Transitive effect propagation | `HasEffect(f,e) :- Calls(f,g), HasEffect(g,e).` |

**Key papers for these are preserved in the literature review** for reference
when writing the rule sets.

## Summary Matrix

| # | Technique | Status | Key Reuse | Effort |
|---|-----------|--------|-----------|--------|
| 1 | Datalog engine | **✅ Complete** | fact_graph | Medium |
| 2 | Per-function CFGs | **TODO** | emend_core, scope resolver | Medium |
| 3 | Typestate analysis | **TODO** | taint engine, CFGs | Medium |
| 4 | API migration | **TODO** | mapping store, batch | Medium |
| 5 | Spec mining | **TODO** | pattern matching | Low-medium |
| 6 | Adv. impact analysis | **TODO** | impact, git | Low-medium |
| 7 | Incremental analysis | **TODO** | parse.db, editor | Medium |
| 8 | None/Optional domain | **TODO** | taint engine, CFGs | Medium |
| 9 | LLM false positive filtering | **TODO** | MCP server | Low |
| — | Program slicing | **Datalog rules** | — | — |
| — | CFL-reachability | **Datalog rules** | — | — |
| — | IFDS/IDE | **Datalog rules** | — | — |
| — | Points-to analysis | **Datalog rules** | — | — |
| — | Effect inference | **Datalog rules** | — | — |

## Longer-Term (Tier 3-4)

These require significant new infrastructure but have high potential:

- **Bounded model checking** — property verification for Python
- **Shape analysis** — data structure invariant checking
- **Semantic code search** — embedding-based code search

See the full literature review for details on these.
