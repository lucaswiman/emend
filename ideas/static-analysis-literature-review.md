

# Static Analysis Techniques: Literature Review for Emend

## Tier 1: Natural Extensions of Existing Infrastructure

These techniques can be built directly on emend's existing fact graph, taint engine, pattern matching, call graph, and type oracle with relatively modest effort.

---

### 1. Program Slicing

**Seminal papers:**
- Mark Weiser, "Program Slicing," *IEEE TSE*, 1984 — introduced the concept of backward slicing: given a variable at a program point, compute the set of statements that could affect its value.
- Horwitz, Reps, Binkley, "Interprocedural Slicing Using Dependence Graphs," *ACM TOPLAS*, 1990 — extended slicing to interprocedural settings using System Dependence Graphs (SDGs).
- Sridharan, Fink, Bodik, "Thin Slicing," *PLDI 2007* — a practical refinement that computes much smaller slices by tracking only direct data dependencies (producer statements) rather than full transitive closure over control and data dependence. Dramatically improves precision for debugging and understanding tasks.

**How it works:** A backward slice from variable `v` at line `L` is the set of all statements that could influence the value of `v` at `L`. Forward slicing computes the dual: all statements affected by a definition. This requires computing data dependence (def-use chains) and control dependence (which branches govern which statements). Thin slicing restricts to direct value-producing statements, omitting heap base pointers and control dependence, yielding far smaller and more actionable slices.

**Mapping to emend's infrastructure:**
- The **fact graph** already has `ReferenceFact` (read/write classification) and `CallFact`, which gives basic def-use and call information.
- The **taint engine** already performs intraprocedural data flow tracking through assignments, which is essentially a forward slice restricted to taint labels. The propagation logic in `_analyze_function()` is close to what a forward slicer needs.
- The **call graph** provides the interprocedural backbone.
- The **scope resolver** in Rust provides the variable binding information needed for precise def-use computation.

**New capabilities enabled:**
- "Show me everything that could affect this variable's value" — invaluable for debugging.
- "Show me everything affected if I change this definition" — stronger than current impact analysis which only uses caller closure.
- Thin slicing for concise explanations of data flow (much smaller than full slices).
- Could power a `slice` command: `emend slice file.py::func.x --backward --line 42`.

**Implementation complexity:** Medium-low. The core data structures exist. The main work is building a proper Program Dependence Graph (PDG) from tree-sitter ASTs — computing control dependence from the CFG (which the taint engine partially constructs) and data dependence from def-use chains (which the scope resolver provides). Thin slicing is actually easier than full slicing since you skip control dependence.

**Verdict:** Natural extension. The taint engine is already a specialized forward slicer. Generalizing it is straightforward.

---

### 2. CFL-Reachability Based Analysis

**Seminal papers:**
- Reps, "Program Analysis via Graph Reachability," *Information and Software Technology*, 1998 — showed that many program analyses (points-to, slicing, taint) can be formulated as context-free language reachability problems on graphs.
- Reps, Horwitz, Sagiv, "Precise Interprocedural Dataflow Analysis via Graph Reachability," *POPL 1995* — the IFDS framework (see below) is fundamentally a CFL-reachability instance.
- Zheng and Rugina, "Demand-Driven Alias Analysis for C," *POPL 2008* — demand-driven CFL-reachability for alias queries.
- Zhang, Su, "Context-Sensitive Data-Dependence Analysis via Linear Conjunctive Language Reachability," *POPL 2017* — showed that some analyses need LCL-reachability (intersection of two CFLs), which is undecidable in general but can be approximated.

**How it works:** The program is represented as a labeled graph. Edges carry labels like `(i` (call into procedure i), `)i` (return from procedure i), `[f` (field access f), `]f` (field store f). An analysis question becomes: "Is node B reachable from node A via a path whose edge labels form a string in a given context-free language?" For interprocedural analysis, the CFL enforces matched call/return: a path through `call_foo` must return through `ret_foo`, not `ret_bar`. This automatically gives context sensitivity.

**Mapping to emend's infrastructure:**
- The **fact graph** is already a labeled graph with calls, references, taint flows, types, and imports. Adding edge labels for call-site identifiers is straightforward.
- The **call graph** provides call/return edges.
- The **taint engine's** interprocedural analysis with function summaries is already approximating CFL-reachability (matched calls) via fixed-point iteration.

**New capabilities enabled:**
- A unified framework where taint, slicing, alias analysis, and type-state checking are all instances of the same algorithm operating on the same graph with different grammars.
- Context-sensitive interprocedural analysis "for free" — current taint analysis could become more precise by using CFL-reachability to enforce matched call/return paths.
- Demand-driven queries: "Does taint from source A reach sink B through matched call paths?" without analyzing the whole program.

**Implementation complexity:** Medium. The graph is already there (fact graph). The main work is implementing a CFL-reachability solver, which is essentially a dynamic transitive closure algorithm with grammar-guided edge generation. For the matched-parentheses grammar (Dyck language) used in interprocedural analysis, efficient algorithms exist (cubic time in graph size, but practical optimizations bring it down).

**Verdict:** Natural extension of the fact graph. Provides a theoretical unification of several analyses emend already does or wants to do.

---

### 3. Specification Mining (Daikon-style)

**Seminal papers:**
- Ernst, Cockrell, Griswold, Notkin, "Dynamically Discovering Likely Program Invariants to Support Program Evolution," *IEEE TSE*, 2001 (Daikon) — infers likely invariants (preconditions, postconditions, loop invariants) from dynamic execution traces by checking a large set of candidate invariants against observed values.
- Ammons, Bodik, Larus, "Mining Specifications," *POPL 2002* — mines temporal API specifications (ordering constraints on method calls) from execution traces using probabilistic finite automata.
- Livshits and Zimmermann, "DynaMine: Finding Common Error Patterns by Mining Software Repositories," *ESEC/FSE 2005* — mines error patterns from version histories.
- Lemieux, Park, Beschastnikh, "General LTL Specification Mining," *ASE 2015* — mines temporal properties expressed in linear temporal logic from traces.
- Recent: Le and Lo, "Deep Specification Mining," *ICSE 2018* — uses RNNs to mine API specifications from code.

**How it works:** Static specification mining examines code patterns to infer likely invariants. For example: "Every call to `open()` is followed by `close()` on the same object" or "Parameter `x` is always non-negative when `foo(x)` is called." Dynamic approaches instrument code and observe runtime values, then propose invariants that hold across all observed executions. Static approaches instead look at patterns across the codebase — if 95% of callers of `connect()` check the return value, the 5% that don't are likely bugs.

**Mapping to emend's infrastructure:**
- The **pattern matching engine** can search for code patterns across the project.
- The **fact graph** can aggregate call patterns, reference patterns, and type patterns.
- The **call graph** can identify common call sequences.
- The **policy engine** is the natural place to express mined specifications as checks.

**New capabilities enabled:**
- Automatic discovery of project conventions: "You always pass `timeout=` to API calls except here."
- Anomaly detection: "99 out of 100 callers of `parse()` wrap it in try/except; this one doesn't."
- Mining typestate protocols from usage patterns (see typestate analysis below).
- Feeding mined specs into the policy engine for ongoing enforcement.
- A `mine` or `infer` command: `emend mine --kind protocols file.py` or `emend mine --kind invariants`.

**Implementation complexity:** Low to medium. Basic pattern-frequency mining can be built on existing pattern matching + fact graph queries. Counting how often a pattern appears, finding deviations, and ranking by confidence is straightforward. More sophisticated temporal mining needs ordered call sequences, which can be extracted from the CFG.

**Verdict:** Natural extension. The infrastructure is there; the main work is defining candidate invariant templates and implementing frequency-based anomaly ranking.

---

### 4. Provenance Tracking / Data Lineage

**Seminal papers:**
- Cheney, Ahmed, Acar, "Provenance as Dependency Analysis," *MSCS*, 2011 — formalizes provenance (tracking where data came from and how it was transformed) as a form of dependency analysis, connecting it to program slicing.
- Buneman, Khanna, Tan, "Why and Where: A Characterization of Data Provenance," *ICDT 2001* — distinguishes *why-provenance* (which inputs contributed to an output) from *where-provenance* (which input locations were copied to an output location).
- Green, Karvounarakis, Tannen, "Provenance Semirings," *PODS 2007* — a unifying algebraic framework for provenance using semirings; different semirings give different provenance granularities.
- Murta et al., "noWorkflow: Capturing and Analyzing Provenance of Scripts," *IPAW 2014* — provenance tracking for Python scripts specifically.

**How it works:** Provenance tracking annotates data values with metadata about their origin and the transformations applied to them. In a static analysis context, this means tracking: for each value at a program point, which source-code definitions contributed to it (why-provenance), which specific sub-expressions were copied vs. computed (where-provenance), and what operations were applied along the way (how-provenance). This is closely related to taint analysis but richer — taint is essentially boolean provenance (tainted or not), while provenance preserves the full derivation history.

**Mapping to emend's infrastructure:**
- The **taint engine** already tracks source-to-sink flows; provenance is a direct generalization that preserves the full path and transformation history rather than just a taint label.
- The `TaintFlowFact` in the **fact graph** already has `label` and could be extended with provenance metadata.
- The `--trace` flag on taint analysis already shows propagation paths, which is primitive provenance.

**New capabilities enabled:**
- "Where did this value come from, through what transformations?" — richer than taint traces.
- Compliance checking: "Show that all PII data is derived only from consented sources and passes through anonymization."
- Data flow documentation: automatic generation of data lineage diagrams.
- A `provenance` command: `emend provenance file.py::func.result --format graph`.

**Implementation complexity:** Low-medium. Extending the taint engine to carry richer annotations (sets of contributing sources, transformation chains) rather than just labels. The propagation machinery already exists.

**Verdict:** Natural extension of taint analysis. Moderate value-add over existing traces.

---

### 5. Code Clone Detection (Beyond Syntactic Matching)

**Seminal papers:**
- Baxter, Yahin, Moura, Sant'Anna, Bier, "Clone Detection Using Abstract Syntax Trees," *ICSM 1998* — tree-based clone detection using AST hashing and comparison.
- Jiang, Misherghi, Su, Glondu, "DECKARD: Scalable and Accurate Tree-Based Detection of Code Clones," *ICSE 2007* — uses characteristic vectors computed from AST subtrees; clones are trees with similar vectors. Scales to millions of LOC.
- Kamiya, Kusumoto, Inoue, "CCFinder: A Multilinguistic Token-Based Code Clone Detection System," *IEEE TSE*, 2002 — token-based approach with parameterized matching (renaming identifiers).
- Li et al., "CCLearner: A Deep Learning-Based Clone Detection Approach," *ICSME 2017* — deep learning for semantic clone detection.
- Saini et al., "Oreo: Detection of Clones in the Twilight Zone," *FSE 2018* — combines metrics, machine learning, and deep learning for Type-3/4 clones.
- Recent: Mehrotra et al., "Modeling Functional Similarity in Source Code with Graph-Based Siamese Networks," *ASE 2023* — GNN-based functional clone detection.

**Clone type taxonomy:**
- **Type 1**: Exact copies (modulo whitespace/comments).
- **Type 2**: Syntactically identical modulo identifier renaming and literal changes.
- **Type 3**: Near-miss clones with added/removed/modified statements.
- **Type 4**: Semantic clones — different syntax, same behavior.

**Mapping to emend's infrastructure:**
- The **tree-sitter AST** in Rust gives direct access to AST structure for tree-based clone detection.
- The **pattern matching engine** with metavariables already supports Type-2 detection (a pattern like `$F($X, $Y)` matches all binary calls regardless of names).
- The **Rust backend** can efficiently compute AST hashes and characteristic vectors.
- The **symbol collection** provides function boundaries for clone-unit delineation.

**New capabilities enabled:**
- Detecting duplicated logic across a codebase for refactoring opportunities.
- "Find code similar to this function" — useful for consistency enforcement.
- Detecting diverged copies (clones that were modified independently and may have inconsistent bug fixes).
- A `clones` command: `emend clones --min-size 5 --type 2 src/`.

**Implementation complexity:** Low for Type 1-2 (AST hashing with metavar normalization is a minor extension of existing pattern compilation). Medium for Type 3 (needs tree edit distance or characteristic vectors). High for Type 4 (needs semantic comparison — embeddings or symbolic).

**Verdict:** Type 1-2 detection is a very natural extension of the pattern engine. Type 3 via DECKARD-style vectors is moderate effort.

---

### 6. Advanced Change Impact Analysis

**Seminal papers:**
- Lehnert, "A Taxonomy for Software Change Impact Analysis," *IWPSE-Evol 2011* — comprehensive taxonomy of impact analysis techniques.
- Ren, Shah, Tip, Ryder, Chesley, "Chianti: A Tool for Change Impact Analysis of Java Programs," *OOPSLA 2004* — uses atomic changes (added/deleted/changed methods) and affected tests are those whose call graph intersects with changed methods. More precise than simple caller closure because it considers the nature of the change.
- Zhang et al., "FaultTracer: A Spectrum-based Approach to Change Impact Analysis," *ICSM 2012* — combines static analysis with test coverage data.
- Gall, Hajek, Jazayeri, "Detection of Logical Coupling Based on Product Release History," *ICSM 1998* — co-change analysis from version history.
- Musco et al., "A Large-Scale Study of Call Graph-based Impact Analysis Using Mutation Testing," *IST 2017* — empirical evaluation of call-graph-based impact analysis precision.

**How it works beyond caller closure:** Emend's current impact analysis uses reverse-caller BFS from changed symbols. More advanced techniques include:
1. **Change classification**: Different kinds of changes have different impact. Adding a parameter has different impact than changing a function body.
2. **Field sensitivity**: If you only change how field `.x` is computed, only callers that use `.x` are impacted, not all callers.
3. **Co-change mining**: From git history, identify files/symbols that historically change together (logical coupling), even without direct call relationships.
4. **Test-change mapping**: Maintain a mapping from symbols to the tests that exercise them (via static analysis of test call graphs), enabling precise "which tests to re-run" answers.

**Mapping to emend's infrastructure:**
- The **impact analysis** already does reverse-caller BFS with witness edges.
- The **fact graph** can store historical co-change data.
- The **dead code detection** already has heuristics for identifying test files/symbols.
- The **git integration** (git log -S) already exists for last-reference tracking.
- The **type oracle** can provide field-level information for field-sensitive impact.

**New capabilities enabled:**
- More precise impact sets (fewer false positives than pure caller closure).
- "Which tests should I re-run?" with better precision.
- Historical co-change warnings: "When you change `A`, you usually also need to change `B`."
- Field-sensitive impact: changing a method that only affects `.name` doesn't flag callers that only use `.id`.

**Implementation complexity:** Low-medium. Co-change mining from git log is straightforward. Field-sensitive impact needs attribute tracking in the fact graph. Test-change mapping needs test call graph construction (already possible from call graph).

**Verdict:** Natural extension of existing impact analysis. High practical value.

---

### 7. Typestate Analysis

**Seminal papers:**
- Strom and Yemini, "Typestate: A Programming Language Concept for Enhancing Software Reliability," *IEEE TSE*, 1986 — the original typestate concept: objects have states, and different operations are valid in different states.
- DeLine and Fähndrich, "Typestates for Objects," *ECOOP 2004* — typestate for object-oriented languages, tracking object state through aliases.
- Bierhoff and Aldrich, "Modular Typestate Checking of Aliased Objects," *OOPSLA 2007* — modular typestate analysis with access permissions to handle aliasing.
- Garcia et al., "Foundations of Typestate-Oriented Programming," *ACM Computing Surveys*, 2014 — comprehensive survey.
- Recent: Das et al., "Oxide: The Essence of Rust," *arXiv 2019* — Rust's ownership/borrowing as a form of typestate.

**How it works:** Typestate analysis tracks the abstract state of an object through a program. A file object might be in states {unopened, open, closed}. The operation `read()` is only valid in state `open`. The analysis checks that all paths through the code respect the typestate protocol. At its core, this is a data-flow analysis where the lattice is the power set of possible states, and transfer functions are defined by the protocol (e.g., `open()` transitions from `unopened` to `open`).

**Mapping to emend's infrastructure:**
- The **taint engine** already performs intraprocedural data-flow analysis tracking abstract properties (taint labels) through assignments. Typestate analysis is structurally identical — instead of tracking taint labels, you track states.
- The **policy engine** with YAML configuration is the natural place to define typestate protocols.
- The **pattern matching** can identify state-transitioning operations.
- **Spec mining** (above) can automatically infer typestate protocols from usage patterns.

**Example policy YAML:**
```yaml
typestate:
  - name: file-protocol
    type: "*.open(*)"  # objects returned by open()
    states: [open, closed]
    initial: open
    transitions:
      - method: read
        from: [open]
        to: open
      - method: close
        from: [open]
        to: closed
    error_states:
      - state: open
        at: end_of_scope
        message: "File may not be closed"
```

**New capabilities enabled:**
- Resource leak detection: "This file/connection/lock might not be closed on all paths."
- Protocol violation detection: "You're calling `send()` on a socket that hasn't called `connect()` yet."
- Iterator protocol checking: "You're calling `next()` after `StopIteration`."
- Context manager verification: objects used with `with` statements follow protocol.

**Implementation complexity:** Medium. The data-flow machinery exists in the taint engine. The main work is: (1) defining the typestate lattice and transfer functions, (2) handling aliasing (the hard part — when two variables point to the same object, a state change through one must be reflected in the other), (3) handling branches (must-close on all paths). For Python, aliasing is particularly tricky. A pragmatic approach ignores aliasing initially (track state per variable name, not per abstract object) and adds alias tracking later.

**Verdict:** Natural extension of the taint engine. The taint propagation machinery is essentially a typestate engine parameterized by a trivial protocol (tainted/untainted). High practical value for resource leak detection.

---

## Tier 2: Moderate Effort, Building on Existing Infrastructure

These techniques require some new infrastructure but can leverage substantial existing components.

---

### 8. Datalog-Based Program Analysis

**Seminal papers:**
- Whaley and Lam, "Cloning-Based Context-Sensitive Pointer Alias Analysis Using Binary Decision Diagrams," *PLDI 2004* — Datalog + BDDs for context-sensitive points-to analysis.
- Bravenboer and Smaragdakis, "Strictly Declarative Specification of Sophisticated Points-to Analyses," *OOPSLA 2009* (Doop) — the canonical Datalog-based framework for Java pointer analysis. Key insight: the analysis is specified purely as Datalog rules; the engine handles evaluation strategy.
- Smaragdakis and Bravenboer, "Using Datalog for Fast and Easy Program Analysis," *Springer LNCS*, 2010 — tutorial/survey on Datalog for program analysis.
- Jordan et al., "Soufflé: On Synthesis of Program Analyzers," *CAV 2016* — high-performance Datalog engine that compiles rules to parallel C++. Used in production at Oracle, Amazon, and by the US DoD.
- Avgustinov et al., "QL: Object-Oriented Queries on Relational Data," *ECOOP 2016* (CodeQL/Semmle) — QL extends Datalog with OO features, aggregation, and recursion.
- Recent: Scholz et al., "On Fast Large-Scale Program Analysis in Datalog," *CC 2016* — compilation strategies for Soufflé.

**How it works:** Program facts are expressed as relations (tables): `CallsFunction(caller, callee)`, `PointsTo(var, heap_obj)`, `Taint(var, label)`. Analysis rules are Datalog clauses:
```
Reachable(x, y) :- CallsFunction(x, y).
Reachable(x, z) :- Reachable(x, y), CallsFunction(y, z).
TaintedVar(v, l) :- TaintSource(v, l).
TaintedVar(v2, l) :- TaintedVar(v1, l), AssignedFrom(v2, v1).
```
The engine evaluates all rules to a fixed point using semi-naive evaluation.

**Mapping to emend's infrastructure:**
- The **fact graph** already contains exactly the kinds of relations Datalog operates on: `SymbolFact`, `CallFact`, `ReferenceFact`, `TaintFlowFact`, `TypeFact`, `ImportFact`.
- The existing `build_from_project()` populates these facts — this is the EDB (extensional database).
- The **transitive closures** in the fact graph are already hand-coded Datalog rules (transitive reachability).
- The **taint analysis** fixed-point iteration is a hand-coded Datalog evaluation for taint propagation rules.
- The **policy engine** checks are essentially single Datalog queries.

**New capabilities enabled:**
- A query language for the fact graph: `emend query "TaintReaches(src, sink) :- TaintSource(src, 'sql'), Calls(src, sink), HasAnnotation(sink, 'route')"`.
- User-defined analyses without code changes — just add Datalog rules.
- Compose analyses: use the output of one analysis as input to another (e.g., points-to feeds taint analysis for precision).
- Would replace several hand-coded analyses with declarative specifications.

**Implementation approaches:**
1. **Embed Soufflé**: Compile to Soufflé rules, call Soufflé as a subprocess. Pro: industrial-strength performance. Con: native dependency, compilation step.
2. **Use a Python Datalog library** (e.g., `pyDatalog`): Pure Python, easy integration. Con: slower.
3. **Implement semi-naive evaluation on the fact graph**: The fact graph already stores facts in indexed maps. Implementing bottom-up evaluation with semi-naive optimization is ~500-1000 lines. This is the most natural approach — the fact graph becomes both the storage and the evaluation substrate.
4. **Compile to SQL on parse.db**: SQLite already stores facts; recursive CTEs can express Datalog. Limited but requires no new code.

**Implementation complexity:** Medium. The fact graph is 90% of the way there. The missing piece is a rule parser (could reuse the pattern grammar or add a simple Datalog syntax to the Lark grammars) and a semi-naive evaluation loop. Option 3 is probably 2-3 weeks of work.

**Verdict:** This is arguably the single highest-value addition. It would unify and generalize the fact graph, taint analysis, transitive closures, and policy checks under one framework, while giving users a query language.

---

### 9. IFDS/IDE Framework

**Seminal papers:**
- Reps, Horwitz, Sagiv, "Precise Interprocedural Dataflow Analysis via Graph Reachability," *POPL 1995* — the IFDS framework. Key insight: interprocedural dataflow problems where the domain is a finite set and the transfer functions distribute over meet can be solved precisely (context-sensitively) in polynomial time by reducing to CFL-reachability on an "exploded supergraph."
- Sagiv, Reps, Horwitz, "Precise Interprocedural Dataflow Analysis with Applications to Constant Propagation," *TCS*, 1996 — the IDE extension: handles "environments" (maps from finite domain to a potentially infinite range), enabling analysis like linear constant propagation while retaining precision.
- Naeem, Lhoták, Rodriguez, "Practical Extensions to the IFDS Algorithm," *CC 2010* — engineering improvements for large-scale IFDS.
- Bodden, "Inter-procedural Data-Flow Analysis with IFDS/IDE and Soot," *ACM SIGPLAN tutorial*, 2012 — practical tutorial.
- Späth, Ali, Bodden, "Context-, Flow-, and Field-Sensitive Data-Flow Analysis using Synchronized Pushdown Systems," *POPL 2019* — extends IFDS with field sensitivity.

**How it works:** IFDS constructs an "exploded supergraph" — the program's interprocedural control flow graph crossed with the data-flow domain. Each node is (program point, data-flow fact). Edges represent flow, call, and return transitions. The analysis then finds all nodes reachable from entry nodes in this graph, respecting matched call/return (CFL-reachability with the Dyck language). This automatically gives context sensitivity for free.

For taint analysis: the data-flow domain is the set of variables/access paths. A taint fact `(stmt, x)` means "variable x is tainted at stmt." Transfer functions map taints across assignments, calls, and returns. IFDS finds all reachable taint facts.

**Mapping to emend's infrastructure:**
- The **interprocedural taint analysis** with function summaries is already an approximation of IFDS. Function summaries (param-to-return, param-to-sink) are exactly the summary edges in IFDS.
- The **call graph** provides the interprocedural control flow.
- The **scope resolver** provides variable binding information for constructing the flow graph.
- This is a more principled replacement for the current fixed-point iteration in `run_interprocedural_taint_analysis()`.

**New capabilities enabled:**
- Context-sensitive taint analysis: distinguishing `f(tainted)` from `f(clean)` at different call sites, without cloning.
- Precise handling of parameter passing and returns (the current function summary approach loses some precision).
- Framework for other IFDS problems: uninitialized variables, possibly-null analysis, typestate (if the state set is finite).
- IDE extension enables constant propagation and more.

**Implementation complexity:** Medium-high. The exploded supergraph construction requires an interprocedural CFG (which needs to be built from per-function CFGs + call graph). The tabulation algorithm is well-documented but intricate. However, the payoff is a general framework that replaces several ad-hoc analyses.

**Verdict:** Moderate effort, high value. Would make the taint analysis significantly more precise and provide a reusable framework. A natural evolution from the existing interprocedural taint engine.

---

### 10. Abstract Interpretation

**Seminal papers:**
- Cousot and Cousot, "Abstract Interpretation: A Unified Lattice Model for Static Analysis of Programs by Construction or Approximation of Fixpoints," *POPL 1977* — the foundational paper. Programs are analyzed by computing fixpoints over abstract domains that approximate concrete program semantics.
- Cousot and Cousot, "Systematic Design of Program Analysis Frameworks," *POPL 1979* — the practical framework.
- Miné, "The Octagon Abstract Domain," *HOSC*, 2006 — the octagon domain, tracking constraints of the form ±x ± y ≤ c. Sweet spot between precision and performance.
- Blanchet et al., "A Static Analyzer for Large Safety-Critical Software," *PLDI 2003* (Astrée) — industrial-scale abstract interpretation for C, proving absence of runtime errors in Airbus flight control software.
- Urban and Müller, "An Abstract Interpretation Framework for Input Data Usage," *ESOP 2018* — abstract interpretation for data science/ML pipelines.

**Core abstract domains:**
- **Sign domain**: {negative, zero, positive, top, bottom}. Very cheap, detects sign errors.
- **Interval domain**: [lo, hi] for each variable. Detects out-of-bounds, division by zero. O(n) per abstract step.
- **Octagon domain**: Relations ±x ± y ≤ c. More precise than intervals (captures relationships between variables). O(n³) per step.
- **Polyhedra domain**: Arbitrary linear constraints. Most precise numerical domain. Exponential worst case.
- **String domains**: Prefix/suffix, regex approximation, character set tracking.

**Mapping to emend's infrastructure:**
- The **taint engine** is already an abstract interpreter with a trivial abstract domain (set of taint labels per variable). The iteration strategy, widening (implicit in the fixed-point iteration), and transfer function structure are all present.
- The **type oracle** provides type information that can seed abstract domains (e.g., knowing `x: int` means the interval domain applies).
- The **Rust backend** could implement numerical domains efficiently.

**New capabilities enabled:**
- Detecting runtime errors statically: division by zero, index out of bounds, integer overflow.
- Range analysis: "This variable is always between 0 and 100 at this point."
- String analysis: "This SQL query string always starts with SELECT" (useful for SQL injection beyond taint).
- Array bounds checking for NumPy/pandas operations.
- A `check` command: `emend check --domain intervals file.py` reporting potential runtime errors.

**Implementation complexity:** Medium-high. The abstract interpretation loop structure exists in the taint engine. The new work is: (1) implementing abstract domains (sign and interval are simple; octagon is moderate), (2) defining transfer functions for Python operations (arithmetic, string operations, container operations) — this is the bulk of the work, (3) implementing widening operators for loop convergence. For Python, the lack of static types makes this harder than for C/Java, but the type oracle helps.

**Verdict:** Moderate effort. The sign domain and interval domain are straightforward; octagon and beyond require significant investment. The practical value for Python is moderate (Python developers are less likely to have integer overflow bugs than C developers), but range analysis for data science code and string analysis for security are compelling.

---

### 11. Incremental / Demand-Driven Analysis

**Seminal papers:**
- Horwitz, Reps, Sagiv, "Demand Interprocedural Dataflow Analysis," *FSE 1995* — instead of analyzing the whole program, start from the query and work backward, analyzing only what's needed.
- Acar, "Self-Adjusting Computation," *PhD thesis, CMU*, 2005 (Adapton follows from this work).
- Hammer, Acar, et al., "Adapton: Composable, Demand-Driven Incremental Computation," *PLDI 2014* — a framework where computations are automatically re-executed when inputs change, but only the parts affected by the change.
- Szabó, Erdweg, Voelter, "IncA: A DSL for Incremental Program Analysis," *ASE 2016* — a Datalog-like DSL specifically designed for incremental analysis; analyses are re-evaluated incrementally when code changes.
- Pacak, Erdweg, Szabó, "A Systematic Approach to Deriving Incremental Type Checkers," *OOPSLA 2020* — systematic derivation of incremental type checkers.
- Recent: Brandl et al., "Modular, Demand-Driven, Incremental Static Analysis in Rust," *PLDI 2023* — describes the query-based architecture used by the Rust compiler (salsa/chalk).

**How it works:** Instead of re-analyzing the entire project after every edit, incremental analysis tracks dependencies between analysis results and input facts. When a file changes, only the analyses that depend on facts from that file are re-executed. Demand-driven analysis goes further: don't compute anything until a query is issued, then compute only what's needed to answer that query.

**Mapping to emend's infrastructure:**
- The **parse.db** cache already provides file-level incrementality (files are re-parsed only when content changes, keyed by hash).
- The **type_cache** table provides incrementality for type queries.
- The **editor-server** (JSON-RPC) is the primary consumer of incremental analysis — it needs fast responses after edits.
- The **fact graph** with `build_from_project()` currently rebuilds from scratch; adding incrementality here would be the main win.

**New capabilities enabled:**
- Near-instant analysis updates after edits in the editor server.
- Demand-driven queries: "Is this variable tainted?" without analyzing the whole program.
- Practical for large codebases where full re-analysis is too slow.
- Foundation for a language-server-protocol (LSP) implementation.

**Implementation complexity:** Medium. File-level incrementality (re-analyze only changed files, update the fact graph incrementally) is straightforward — track which facts came from which file, delete old facts, insert new ones. True incremental analysis at the statement level (Adapton-style) is much harder. The pragmatic approach: file-level incrementality for the fact graph, demand-driven evaluation for transitive queries.

**Verdict:** High practical value, especially for the editor server. File-level incrementality is moderate effort; statement-level is research-level.

---

### 12. Points-to Analysis

**Seminal papers:**
- Andersen, "Program Analysis and Specialization for the C Programming Language," *PhD thesis, DIKU*, 1994 — inclusion-based (subset) points-to analysis. For each pointer p, computes pts(p) = {objects p may point to}. Constraint: `p = q` implies pts(q) ⊆ pts(p). Cubic complexity.
- Steensgaard, "Points-to Analysis in Almost Linear Time," *POPL 1996* — unification-based: `p = q` implies pts(p) = pts(q). Almost linear but less precise.
- Lhoták and Hendren, "Scaling Java Points-to Analysis using Spark," *CC 2003* — engineering tricks for practical points-to analysis.
- Smaragdakis, Bravenboer, Lhoták, "Pick Your Contexts Well: Understanding Object-Sensitivity," *POPL 2011* — object-sensitive variants for OO languages, showing that call-site sensitivity is often inferior to object sensitivity for Java-like languages.
- Kastrinis and Smaragdakis, "Hybrid Context-Sensitivity for Points-To Analysis," *PLDI 2013* — combining different context-sensitivity strategies.
- Li et al., "Precision-Guided Context Sensitivity for Pointer Analysis," *OOPSLA 2018* — selectively applying context sensitivity only where it improves precision.
- Wei and Ryder, "Practical Blended Taint Analysis for JavaScript," *ISSTA 2013* — relevant for dynamic languages.

**How it works:** Points-to analysis determines which heap objects a variable (or expression) may reference at runtime. For Python, this means: given `x = foo()`, what object does `x` point to? Given `x.bar()`, which `bar` method is called? This is fundamental for resolving dynamic dispatch, which is pervasive in Python.

**For Python specifically:** Python's extreme dynamism makes precise points-to analysis very hard. However, practical approaches exist:
- Type-based approximation: use type information (from type oracle) to determine the set of possible objects.
- Allocation-site abstraction: each `ClassName()` call creates a distinct abstract object.
- Flow-sensitive: track assignments sequentially (Python has no pointers, just name bindings, which simplifies things).

**Mapping to emend's infrastructure:**
- The **type oracle** (Pyrefly, Pyright, ty) already computes type information that is essentially a points-to approximation (knowing `x: List[int]` means x points to a list object).
- The **scope resolver** provides variable binding information.
- The **call graph** would benefit directly from points-to information (resolving `x.method()` calls).
- The **taint engine** would benefit from knowing that `x = y` means x and y alias the same object.

**New capabilities enabled:**
- More precise call graphs (resolving `obj.method()` calls based on what obj points to).
- Alias analysis: "Do x and y refer to the same object?" — needed for precise typestate analysis.
- More precise taint analysis: `x = y; sanitize(y)` should untaint x if they alias.
- Virtual call resolution for the fact graph.

**Implementation complexity:** High for a standalone analysis. Medium if leveraging the type oracle — the type oracle already provides much of what a points-to analysis would compute for Python, since Python type inference must reason about object types (which is what points-to analysis determines). The pragmatic approach: use type oracle results as points-to approximations, and only build a dedicated points-to analysis if the type oracle is insufficient.

**Verdict:** The type oracle already provides a practical approximation. A dedicated points-to analysis adds precision but at significant implementation cost. Best deferred until the type oracle's limitations become a bottleneck for call graph precision or taint analysis.

---

### 13. Effect Systems

**Seminal papers:**
- Gifford and Lucassen, "Integrating Functional and Imperative Programming," *LFP 1986* — early effect systems tracking read/write side effects.
- Lucassen and Gifford, "Polymorphic Effect Systems," *POPL 1988* — polymorphic effect inference.
- Plotkin and Power, "Algebraic Operations and Generic Effects," *Applied Categorical Structures*, 2003 — algebraic effects foundation.
- Bauer and Pretnar, "Programming with Algebraic Effects and Handlers," *JLAMP*, 2015 — practical algebraic effects.
- Rytz, Odersky, Haller, "Lightweight Polymorphic Effects," *ECOOP 2012* — practical effect inference for Scala.
- Recent: Brachthäuser, Schuster, Ostermann, "Effects as Abilities," *OOPSLA 2020*; Lindley et al., "Effect Handlers via Generalised Continuations," *JFP*, 2020.

**How it works:** An effect system tracks what side effects a computation may perform: reading/writing mutable state, I/O, exceptions, non-termination, network access, file system access, database queries, etc. Effect inference automatically determines the effects of each function from its body.

**For Python / emend:** Rather than a type-level effect system (which Python doesn't have), this would be a static analysis that infers the side effects of each function: "This function reads from the filesystem," "This function makes network calls," "This function mutates its argument."

**Mapping to emend's infrastructure:**
- The **semantic_context()** function in `transform.py` already identifies "side effects" and "dangers" as part of its analysis — this is primitive effect inference.
- The **call graph** provides the interprocedural structure for effect propagation.
- The **fact graph** can store effect facts alongside symbol facts.
- The **policy engine** can check effect constraints ("functions decorated with @pure should have no effects").

**New capabilities enabled:**
- Automatic `@pure` verification: "Does this function truly have no side effects?"
- Effect-based test isolation: "These test functions have network effects and should be mocked."
- Refactoring safety: "Is it safe to reorder these function calls? Only if their effects don't interfere."
- API documentation: automatic side-effect annotations.
- Policy checks: "No database access from the presentation layer."

**Implementation complexity:** Medium. Basic effect inference (identifying I/O operations, mutations, exceptions from known APIs) can be done with pattern matching on standard library calls + interprocedural propagation. This extends naturally from the semantic_context() analysis.

**Verdict:** Moderate effort, good value. The semantic_context() function is already partway there.

---

### 14. Symbolic Execution (Lightweight / Bounded)

**Seminal papers:**
- King, "Symbolic Execution and Program Testing," *CACM*, 1976 — the original symbolic execution paper.
- Cadar, Dunbar, Engler, "KLEE: Unassisted and Automatic Generation of High-Coverage Tests for Complex Systems Programs," *OSDI 2008* — the most influential symbolic execution tool. Executes programs with symbolic inputs, using an SMT solver (STP/Z3) to determine path feasibility and generate concrete test inputs.
- Godefroid, Klarlund, Sen, "DART: Directed Automated Random Testing," *PLDI 2005* — concolic (concrete + symbolic) testing: execute concretely with random inputs, collect path constraints symbolically, negate constraints to explore new paths.
- Sen, Marinov, Agha, "CUTE: A Concolic Unit Testing Engine for C," *ESEC/FSE 2005* — concolic testing with pointer constraints.
- Ball et al., "Automatic Predicate Abstraction of C Programs," *PLDI 2001* (SLAM) — predicate abstraction + model checking for C.
- Recent: Cadar and Sen, "Symbolic Execution for Software Testing: Three Decades Later," *CACM*, 2013 — comprehensive survey.

**For Python specifically:**
- CrossHair (https://github.com/pschanely/CrossHair) — a Python symbolic execution / contract checking tool using Z3. Demonstrates that symbolic execution for Python is practical for bounded depths.

**How it works:** Instead of executing with concrete values, execute with symbolic values (mathematical variables). At each branch, fork execution: one path assumes the branch is true, one assumes false. An SMT solver checks if each path is feasible. At the end of each path, you have: (1) the path condition (conjunction of branch conditions), and (2) a symbolic expression for the return value. This enables: checking assertions, generating test inputs that reach specific code, finding paths to error conditions.

**Mapping to emend's infrastructure:**
- The **tree-sitter AST** provides the program structure to interpret symbolically.
- The **type oracle** provides type information that constrains symbolic values.
- The **taint engine's** path-following logic is a simplified version of symbolic execution (tracking abstract values along execution paths).

**New capabilities enabled:**
- "Is this assertion ever violated?" — finding concrete inputs that trigger bugs.
- "Can this branch ever be taken?" — dead code detection beyond reference counting.
- Automatic test case generation: "Generate inputs that cover all branches of this function."
- Path-sensitive taint analysis: "Does tainted data reach this sink on any feasible path?"

**Implementation complexity:** High. Building a symbolic interpreter for even a subset of Python is substantial work. The pragmatic approach: use CrossHair as an optional backend (similar to how the type oracle uses Pyright/Pyrefly), or implement bounded symbolic execution for a restricted subset (arithmetic operations, string operations, comparisons).

**Verdict:** Full symbolic execution is Tier 3. Bounded symbolic execution for simple functions (arithmetic, string manipulation) is Tier 2, especially if leveraging Z3 via CrossHair.

---

### 15. Property-Based Testing Integration

**Seminal papers:**
- Claessen and Hughes, "QuickCheck: A Lightweight Tool for Random Testing of Haskell Programs," *ICFP 2000* — the seminal property-based testing paper.
- Padhye et al., "Semantic Fuzzing with Zest," *ISSTA 2019* — coverage-guided property-based testing using parametric generators.
- Lampropoulos et al., "Coverage Guided, Property Based Testing," *OOPSLA 2019* — using code coverage to guide property-based testing toward unexplored code.
- Löscher and Svensson, "Targeted Property-Based Testing," *ISSTA 2017* — using static analysis to guide PBT toward specific targets.
- Reddy et al., "Quickly Generating Diverse Valid Test Inputs with Reinforcement Learning," *ICSE 2020* — RL-guided test generation.
- Recent: Goldstein et al., "Synthesizing Input Grammars from Dynamic Control Flow," *PLDI 2024* — synthesizing input grammars for fuzzing from observed execution traces.

**How it works:** Static analysis identifies: (1) complex functions that would benefit from PBT, (2) likely properties to test (type-based, assertion-based, or specification-mined), (3) interesting input distributions (boundary values from interval analysis), (4) which code paths are hard to reach (guiding coverage). The output is automatically generated Hypothesis test stubs.

**Mapping to emend's infrastructure:**
- The **type oracle** provides argument types for generating Hypothesis strategies.
- The **pattern matching** can identify assertion patterns and derive properties.
- The **specification mining** (above) can infer likely invariants to use as properties.
- The **dead code / impact analysis** can identify undertested code.
- The **add command** can insert generated test stubs.

**New capabilities enabled:**
- `emend generate-tests file.py::complex_function` — auto-generates Hypothesis test stubs with appropriate strategies based on type annotations and mined properties.
- "This function has 5 branches but your tests only cover 2" + auto-generated tests for the other 3.
- Mutation-testing integration: use emend's replace command to inject mutations, run PBT to check if tests catch them.

**Implementation complexity:** Medium. The core is: parse function signatures (type oracle), generate Hypothesis strategies from types (straightforward mapping), identify properties (assertions in code, pre/post conditions from spec mining), emit test code (add command). The static analysis to guide testing is optional sophistication.

**Verdict:** High practical value, moderate effort. The type oracle + pattern matching make this very feasible.

---

## Tier 3: Significant New Infrastructure, High Value

These require substantial new components but would be transformative.

---

### 16. Shape Analysis / Heap Abstraction

**Seminal papers:**
- Sagiv, Reps, Wilhelm, "Parametric Shape Analysis via 3-Valued Logic," *ACM TOPLAS*, 2002 (TVLA) — the most general shape analysis framework. Represents heap structures using 3-valued logical structures (true/false/maybe). Can verify properties like "list is acyclic," "tree is balanced," "no dangling pointers."
- Calcagno, Distefano, O'Hearn, Yang, "Compositional Shape Analysis by means of Bi-Abduction," *POPL 2009* (Infer) — the foundation of Facebook's Infer tool. Uses separation logic and bi-abduction (simultaneously inferring preconditions and frame conditions) for scalable compositional analysis.
- Distefano, O'Hearn, "Jester: A Tool for Open-Source Bug Finding," *OOPSLA 2019 (experience report)* — practical lessons from deploying Infer at Facebook/Meta scale.
- Le et al., "Detecting Memory Leaks Using Separation Logic Abstraction," *ISSTA 2020*.

**How it works:** Shape analysis abstracts the heap (the set of dynamically allocated objects and references between them) into a finite representation. For Python, this is relevant for: mutable data structures (lists, dicts, trees), object graphs, reference cycles, and resource management.

**For Python specifically:** Python doesn't have manual memory management, so the classical use case (preventing null dereferences, use-after-free) is less relevant. However, shape analysis is still useful for:
- Verifying data structure invariants (sorted list stays sorted after insertion).
- Detecting modification of collections during iteration.
- Resource leak detection (generalizes typestate for objects with complex ownership).
- Detecting reference cycles that prevent garbage collection.

**Mapping to emend's infrastructure:** Limited direct overlap. Would need a new abstract domain for heap shapes. However, the taint engine's propagation framework and the type oracle's type information provide some foundation.

**Implementation complexity:** Very high for general shape analysis. Medium for targeted analyses (collection-during-iteration detection can be done with pattern matching + simple state tracking).

**Verdict:** General shape analysis is overkill for Python. Targeted heap analyses (iterator invalidation, reference cycles) are more feasible.

---

### 17. Bounded Model Checking

**Seminal papers:**
- Clarke, Kroening, Lerda, "A Tool for Checking ANSI-C Programs," *TACAS 2004* (CBMC) — bounded model checking for C. Unrolls loops up to a bound, translates the program to a SAT/SMT formula, checks if an assertion violation or property violation is satisfiable.
- Cordeiro, Fischer, Marques-Silva, "SMT-Based Bounded Model Checking for Embedded ANSI-C Software," *IEEE TSE*, 2012 (ESBMC) — uses SMT instead of SAT for richer theories.
- Recent: For Python specifically, there's limited direct BMC work, but CrossHair (mentioned above) and PyExZ3 (Li and Tan, "PyExZ3: Symbolic Execution of Python Programs," *SIGSOFT SEN*, 2014) are related.

**How it works:** Given a program and a property (assertion, postcondition), BMC: (1) unrolls all loops up to a bound k, (2) translates the unrolled program into an SMT formula where each variable at each program point becomes a fresh SMT variable, (3) asserts the negation of the property, (4) checks satisfiability. If SAT, produces a counterexample (concrete input violating the property). If UNSAT, the property holds up to bound k.

**For emend:** This would enable checking user-specified assertions or policy-generated properties against bounded execution. The main challenge is encoding Python semantics (dynamic types, complex built-in operations) in SMT.

**Implementation complexity:** Very high for general Python. Medium for a restricted numeric/string subset.

**Verdict:** High infrastructure cost for Python. The pragmatic alternative is the symbolic execution / CrossHair integration described above, which achieves similar goals with less infrastructure.

---

### 18. Semantic Code Search (Embedding-Based)

**Seminal papers:**
- Alon et al., "code2vec: Learning Distributed Representations of Code," *POPL 2019* — learns embeddings of code snippets from paths in the AST. Used for method naming, but the embeddings enable similarity search.
- Feng et al., "CodeBERT: A Pre-Trained Model for Programming and Natural Languages," *EMNLP 2020* — BERT pre-trained on code; embeddings capture semantic similarity.
- Guo et al., "GraphCodeBERT: Pre-Training Code Representations with Data Flow," *ICLR 2021* — adds data flow edges to the pre-training signal, improving code understanding.
- Husain et al., "CodeSearchNet: An Open-Source Benchmark for Code Search," *NeurIPS 2019 Workshop* — benchmark and baselines for natural-language-to-code search.
- Recent: Wang et al., "CodeT5+: Open Code Large Language Models for Code Understanding and Generation," *EMNLP 2023*; Li et al., "StarCoder: May the Source Be with You," *TMLR 2023*.

**How it works:** Code snippets (functions, classes, blocks) are converted to dense vector embeddings using a neural model (code2vec, CodeBERT, or similar). Similar code produces similar vectors. Queries can be in natural language ("function that sorts a list by the second element") or code ("find functions similar to this one"). Search is approximate nearest-neighbor on the embedding space.

**Mapping to emend's infrastructure:**
- The **editor_search.py** already has a search engine with FTS5 trigram indexing. Embedding-based search would be an additional ranking signal.
- The **symbol collection** provides function/class boundaries for embedding units.
- The **tree-sitter AST** can provide AST paths for code2vec-style embeddings.

**New capabilities enabled:**
- Natural language code search: "Find all functions that validate email addresses."
- Semantic clone detection (Type-4 clones).
- "Find functions similar to this one" for learning codebases.

**Implementation complexity:** High. Requires a neural model (either running locally or calling an API). The embedding index (FAISS or similar) is an additional dependency. However, simpler approaches exist: code2vec-style AST path features can be computed from tree-sitter without a neural model, and cosine similarity on TF-IDF vectors of AST node types is a surprisingly effective baseline.

**Verdict:** Full neural semantic search is Tier 3. AST-path-based similarity is Tier 2.

---

### 19. API Migration

**Seminal papers:**
- Henkel and Diwan, "CatchUp! Capturing and Replaying Refactorings to Support API Evolution," *ICSE 2005* — recording refactoring operations to replay them on client code.
- Dagenais and Robillard, "Recommending Adaptive Changes for Framework Evolution," *ACM TOSEM*, 2011 (SemDiff) — mines API change rules from framework version histories.
- Nguyen et al., "Graph-Based Mining of In-the-Wild, Fine-Grained, Semantic Code Change Patterns," *ICSE 2019* — mines code change patterns from GitHub commits.
- Lamothe et al., "A3: Assisting Android API Migrations Using Code Examples," *IEEE TSE*, 2021.
- Recent: Xu et al., "API-Specific Code Generation with Large Language Models," *ICSE 2024*; He et al., "Automating Code Updates with Pre-trained Language Models," *ASE 2023*.

**How it works:** API migration involves transforming code that uses API version N to use API version N+1. This requires: (1) a mapping of old API symbols to new ones (rename, moved, split, merged, parameter changed), (2) transformation rules that handle complex changes (argument reordering, wrapping in new constructors, splitting into multiple calls), (3) applying transformations across the codebase.

**Mapping to emend's infrastructure:**
- The **knowledge.py** mapping store already handles identifier and module mappings (YAML-backed).
- The **rename** command already renames symbols across projects.
- The **move** command already moves symbols and rewrites imports.
- The **pattern matching + replace** handles complex transformations.
- The **batch command** applies multiple operations from YAML.
- The **map command** already manages identifier and module mappings.

**New capabilities enabled:**
- Automated library upgrade: `emend migrate --from django==3.2 --to django==4.0` applies all known API changes.
- Community-maintained migration rules as YAML packages.
- Mining migration rules from library changelogs or version diffs.
- The existing `map resolve` command is a foundation for this — extending it with richer transformation patterns would cover API migration.

**Implementation complexity:** Medium. The infrastructure is largely there. The main work is: (1) extending the mapping store to handle richer transformations (not just rename but also argument changes, wrapping, etc.), (2) building or curating migration rule sets for popular libraries, (3) handling ambiguous cases. The pattern matcher + replace engine can handle complex rewrites; the challenge is defining the rules.

**Verdict:** This is arguably the most commercially valuable addition, and emend already has most of the infrastructure. The mapping store + batch + pattern replace is 80% of what's needed.

---

## Tier 4: Research-Level, Interesting but Speculative

---

### 20. Galois Connections and Practical Abstract Domains

**Seminal papers (beyond Cousot & Cousot 1977):**
- Giacobazzi, Ranzato, Scozzari, "Making Abstract Interpretations Complete," *JACM*, 2000 — when and how to construct abstract domains that are "complete" (lose no precision for a given set of operations).
- Logozzo and Fähndrich, "Pentagons: A Weakly Relational Abstract Domain for the Efficient Validation of Array Accesses," *SAC 2008* — cheap relational domain: intervals + upper bounds. Good for array bounds checks.
- Singh, Püschel, Vechev, "Fast Numerical Program Analysis with Reinforcement Learning," *CAV 2018* — using RL to choose which abstract domain operations to apply for optimal precision/performance tradeoff.
- Recent: Monat, Ouadjaout, Miné, "Mopsa: A Modular Open-Source Platform for Static Analysis," *SAS 2024* — modular abstract interpretation framework for Python and C, combining value, type, string, and container abstract domains. Particularly relevant for emend as it targets Python specifically.

**Why this matters for emend:** The question isn't whether to do abstract interpretation (covered above), but which abstract domains are most valuable for Python. Python's dynamic typing means type inference is itself an abstract interpretation (the domain is the set of possible types). Practical domains for Python:
- **Type domain**: {int, str, list, None, ...} with subtyping. The type oracle already computes this.
- **None/Optional domain**: {definitely-None, definitely-not-None, maybe-None}. Very high value — catches None-related crashes.
- **String domain**: prefix/suffix/regex tracking for format strings, SQL queries, URLs.
- **Container domain**: {empty, singleton, nonempty, unknown} for lists/dicts. Catches "accessing element of empty list."

**Verdict:** The None/Optional domain alone justifies investment. It's a relatively simple abstract domain with huge practical impact for Python.

---

### 21. Recent Cutting-Edge Techniques (PLDI/POPL/OOPSLA/ICSE 2023-2025)

**Modular and compositional analysis:**
- Sung et al., "Modular Component-Based Interprocedural Program Analysis," *POPL 2024* — defines analyses as composable modules. Each module handles one aspect (aliasing, taint, types) and they compose automatically. Relevant to emend's modular architecture.

**LLM-guided static analysis:**
- Li et al., "LLM-Assisted Static Analysis for Detecting Security Vulnerabilities," *arXiv 2024 / Oakland S&P 2025 submission* — uses LLMs to reduce false positives from static analysis by having the LLM reason about path feasibility. Directly applicable to emend: use the MCP server to have an LLM agent review flagged taint violations for false positives.
- Fang et al., "Large Language Models for Code Analysis: Do LLMs Really Do Their Job?" *arXiv 2024* — comprehensive evaluation of LLMs for static analysis tasks.

**Incremental analysis for real-time feedback:**
- Brandl et al., already mentioned under Incremental Analysis. The Rust compiler's query-based architecture (salsa framework) is the state of the art for incremental type checking and analysis. This architecture has been adopted by rust-analyzer and is being adopted by the new Rust type checker (chalk/ty).

**Tensor-based program analysis:**
- Katz et al., "Lifting Datalog-Based Analyses to Software Product Lines," *FSE 2024* — lifted analysis that analyzes all configurations simultaneously. Relevant if emend ever targets multi-configuration Python projects (e.g., code with `if sys.platform == ...`).

**Abstract interpretation for ML:**
- Urban et al., "Abstract Interpretation for Tensor Operations," *SAS 2024* — abstract domains for numpy/tensor operations. Directly relevant to emend's Python focus, as many Python codebases are ML/data-science heavy.

**Synthesis-based repair:**
- Bader et al., "Getafix: Learning to Fix Bugs Automatically," *OOPSLA 2019* (Facebook/Meta) — mines fix patterns from past bug fixes and applies them to new instances.
- Drain et al., "Generating Bug-Fixes Using Pretrained Transformers," *MAPS 2021* — LLM-based automated repair.
- Relevance to emend: the lint engine already has `--fix` with replacement patterns. Extending this with learned fix patterns from git history would be powerful.

---

## Summary Matrix

| Technique | Tier | Reuses Existing | New Capability | Effort |
|-----------|------|----------------|----------------|--------|
| Program Slicing | 1 | Taint engine, fact graph, call graph | "What affects this value?" / "What does this change affect?" | Medium-low |
| CFL-Reachability | 1 | Fact graph, call graph | Unified framework for interprocedural analysis | Medium |
| Specification Mining | 1 | Pattern matching, fact graph | Anomaly detection, convention enforcement | Low-medium |
| Provenance Tracking | 1 | Taint engine | Richer data lineage than taint traces | Low-medium |
| Code Clone Detection | 1 | Pattern engine, tree-sitter | Refactoring opportunities, diverged copies | Low (Type 1-2) |
| Advanced Impact Analysis | 1 | Impact analysis, git, fact graph | Co-change, field-sensitive impact, test mapping | Low-medium |
| Typestate Analysis | 1 | Taint engine, policy engine | Resource leak detection, protocol checking | Medium |
| Datalog Engine | 2 | Fact graph, taint engine | User-defined analyses, query language | Medium |
| IFDS/IDE | 2 | Taint engine, call graph | Context-sensitive interprocedural analysis | Medium-high |
| Abstract Interpretation | 2 | Taint engine, type oracle | Runtime error detection, range analysis | Medium-high |
| Incremental Analysis | 2 | parse.db cache, editor server | Real-time analysis after edits | Medium |
| Points-to Analysis | 2 | Type oracle, scope resolver | Precise call graphs, alias analysis | High |
| Effect Systems | 2 | semantic_context(), call graph | Purity checking, effect-based policies | Medium |
| Symbolic Execution | 2 | Type oracle, tree-sitter | Assertion checking, test generation | High |
| PBT Integration | 2 | Type oracle, pattern matching | Auto-generated property-based tests | Medium |
| Shape Analysis | 3 | Limited | Data structure invariants | Very high |
| Bounded Model Checking | 3 | Limited | Property verification | Very high |
| Semantic Code Search | 3 | Editor search, symbol collection | Natural language code search | High |
| API Migration | 2* | Mapping store, rename, move, batch, replace | Automated library upgrades | Medium |
| Galois Connections / Domains | 3 | Taint engine (structure) | None-safety, string analysis | Medium-high |

*API Migration is labeled Tier 2 because emend already has most of the infrastructure.

## Recommended Priority Ordering

Based on feasibility, value, and synergy with existing infrastructure:

1. **Datalog engine on the fact graph** — unifies and generalizes multiple existing analyses; enables a user-facing query language; moderate effort.
2. **Program slicing** — direct generalization of the taint engine; immediately useful for debugging and impact analysis.
3. **Typestate analysis** — direct reuse of taint engine for resource leak detection; high practical value for Python (files, connections, locks).
4. **API migration** — the mapping store, batch, and replace infrastructure is already there; high commercial value.
5. **Specification mining** — low effort, feeds into the policy engine and lint rules.
6. **Advanced impact analysis** (co-change, field-sensitive) — builds directly on existing impact analysis.
7. **Incremental analysis** for the editor server — necessary for scaling.
8. **Effect inference** — extends semantic_context() into a proper analysis.
9. **None/Optional abstract domain** — a focused abstract interpretation that catches Python's most common runtime error.
10. **LLM-guided false positive reduction** — the MCP server enables this immediately.