# Next Static Analyses: Implementation Priorities

Based on a literature review of 21 techniques across program analysis,
ranked by feasibility and value given emend's existing infrastructure.

See [static-analysis-literature-review.md](static-analysis-literature-review.md)
for full details, papers, and implementation strategies.

## Top 10 Priorities

### 1. Datalog Engine on the Fact Graph

Expose the fact graph via a Datalog-style query language so users can write
custom analyses without Python code.  The fact graph already has the right
relational structure (symbols, calls, references, taint flows, types, imports);
the main work is parsing Datalog rules and implementing a semi-naive evaluator.

**Key papers:** Whaley & Lam (PLDI 2004), Scholz et al. (CC 2016) (Soufflé),
Smaragdakis & Bravenboer (FTPL 2011).

**Reuses:** fact_graph.py, policy engine YAML parsing.
**Enables:** User-defined analyses, a query language for the MCP server, and a
unification layer for taint, dead code, and type checks.
**Effort:** Medium.

### 2. Program Slicing

Given a variable at a program point, compute the set of statements that affect
its value (backward slice) or are affected by its definition (forward slice).
Thin slicing (Sridharan et al., PLDI 2007) produces much smaller, more
actionable slices by skipping control dependence.

**Key papers:** Weiser (IEEE TSE 1984), Horwitz/Reps/Binkley (TOPLAS 1990),
Sridharan/Fink/Bodik (PLDI 2007).

**Reuses:** Taint engine (already a specialized forward slicer), scope resolver,
fact graph, call graph.
**Enables:** `emend slice file.py::func.x --backward`, stronger impact analysis.
**Effort:** Medium-low.

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

### 7. Incremental / Demand-Driven Analysis

Only re-analyze what changed rather than re-processing the whole project.
Critical for editor integration and large codebases.

**Key papers:** Acar et al. (Adapton, PLDI 2014), Szabó et al. (IncA, OOPSLA
2016), Arzt & Bodden (FlowDroid revisions).

**Reuses:** parse.db cache (content-hash keyed), editor-server.
**Enables:** Sub-second analysis updates after file saves.
**Effort:** Medium.

### 8. Effect Inference

Track side effects (I/O, mutations, exceptions) through the call graph. A
function is "pure" if it and all callees produce no effects.

**Key papers:** Lucassen & Gifford (POPL 1988), Benton et al. (JFP 2009),
Gordon et al. (OOPSLA 2013).

**Reuses:** semantic_context() (already detects side effects), call graph.
**Enables:** Purity checking, safe-to-parallelize analysis, effect-based
policy rules.
**Effort:** Medium.

### 9. None/Optional Abstract Domain

A focused abstract interpretation that tracks whether variables may be None.
Python's most common runtime error is AttributeError on None.

**Key papers:** Cousot & Cousot (POPL 1977), Livshits et al. (OOPSLA 2015),
Logozzo & Fähndrich (SAS 2008).

**Reuses:** Taint engine (lattice structure maps directly), type oracle.
**Enables:** None-safety checking without full type annotations.
**Effort:** Medium.

### 10. LLM-Guided False Positive Reduction

Use the MCP server to let an LLM review analysis results and filter false
positives using semantic understanding.

**Reuses:** MCP server, all analysis outputs.
**Enables:** Higher precision without sacrificing recall.
**Effort:** Low (infrastructure is already there).

## Summary Matrix

| # | Technique | Tier | Key Reuse | Effort |
|---|-----------|------|-----------|--------|
| 1 | Datalog engine | 2 | fact_graph | Medium |
| 2 | Program slicing | 1 | taint engine | Medium-low |
| 3 | Typestate analysis | 1 | taint engine | Medium |
| 4 | API migration | 2 | mapping store, batch | Medium |
| 5 | Spec mining | 1 | pattern matching | Low-medium |
| 6 | Adv. impact analysis | 1 | impact, git | Low-medium |
| 7 | Incremental analysis | 2 | parse.db, editor | Medium |
| 8 | Effect inference | 2 | semantic_context() | Medium |
| 9 | None/Optional domain | 2 | taint engine | Medium |
| 10 | LLM false positive filtering | 1 | MCP server | Low |

## Longer-Term (Tier 3-4)

These require significant new infrastructure but have high potential:

- **IFDS/IDE framework** — context-sensitive interprocedural analysis
- **Points-to analysis** — precise call graphs and alias analysis
- **Bounded model checking** — property verification for Python
- **Shape analysis** — data structure invariant checking
- **Semantic code search** — embedding-based code search
- **CFL-reachability** — unified framework for interprocedural analyses

See the full literature review for details on these.
