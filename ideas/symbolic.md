# Symbolic Evaluation in emend

A discussion of what "symbolic evaluation" means in the context of a code
analysis tool, how it maps onto the existing CozoDB/Datalog infrastructure,
and where it would provide the most value for code navigation, correctness
checking, and automated editing.

## What Is Symbolic Evaluation?

Symbolic evaluation executes code with *symbols* instead of concrete values.
Where a normal interpreter would compute `f(3) → 7`, a symbolic evaluator
computes `f(x) → 2*x + 1`, preserving the relationship between input and
output as an expression.  The three main variants are:

1. **Symbolic execution** (King 1976, KLEE): Fork at branches, accumulate
   path conditions, query an SMT solver for feasibility.  Produces concrete
   test inputs.  Exponential path explosion in general.

2. **Abstract interpretation** (Cousot & Cousot 1977): Replace concrete
   domains with abstract lattices (signs, intervals, nullability, taint
   labels).  Compute a fixed point over the CFG.  Always terminates via
   widening.  Trades precision for coverage.

3. **Partial evaluation** (Jones, Gomard & Sestoft 1993): Specialize a
   program with respect to known (static) inputs, producing a residual
   program that only depends on unknown (dynamic) inputs.  The Futamura
   projections connect this to compilation.

All three share the core operation: "propagate what we know symbolically and
see what falls out."  The question for emend is: which fragments of symbolic
evaluation are cheap enough to run on every save, and useful enough to drive
real actions?

## The Existing Infrastructure as a Starting Point

emend already does symbolic evaluation — it just doesn't call it that:

| Existing system | What it computes symbolically | Lattice / domain |
|----------------|-------------------------------|-------------------|
| Taint analysis | "Which variables carry taint label L?" | `{tainted, untainted}` per label |
| Def-use chains | "Which definitions reach this use?" | Set of (var, def-site) |
| Effect sinks | "Does a write/read to X happen on a tainted var?" | `{writes, reads}` predicates |
| Type oracle | "What type flows to this position?" | Type descriptors |
| Dead code | "Is this symbol reachable from entry points?" | `{reachable, unreachable}` |
| Rewrite engine | "Are these expressions equivalent under rules?" | E-graph equivalence classes |

The taint engine is literally an abstract interpreter: it computes a fixed
point over CFG edges using Datalog's semi-naive evaluation as the worklist
algorithm.  The `unsanitized[fp, fq, var, lbl, block]` relation is the
abstract state, and each Datalog rule is a transfer function.

This means the path to symbolic evaluation is **incremental, not
revolutionary** — it's about enriching the abstract domains while reusing the
same Datalog propagation infrastructure.

## Concrete Proposals

### 1. Nullability Domain (Value: High, Effort: Medium)

**The pitch:** Python's #1 runtime error is `AttributeError: 'NoneType' ...`.
A focused abstract domain that tracks `{definitely-None, maybe-None,
definitely-not-None, unknown}` per variable catches these without full type
annotations.

**Why Datalog fits:** This is structurally identical to taint analysis with
a richer lattice.  The Datalog rules look like:

```datalog
% Transfer function: assignment from None literal
null_state[fp, fq, var, block, "definitely_none"] :=
    *def_use[fp, fq, var, "write", block, _, line, _, _, _],
    none_literal_at[fp, line]

% Transfer function: None guard (if x is not None:)
null_state[fp, fq, var, true_block, "definitely_not_none"] :=
    null_state[fp, fq, var, guard_block, _],
    *cfg_edge[fp, fq, guard_block, true_block, "true_branch", _, _],
    none_guard[fp, fq, var, guard_block]

% Violation: attribute access on maybe-None
null_violation[fp, fq, var, block, line] :=
    null_state[fp, fq, var, block, state],
    state == "maybe_none",
    attr_access[fp, fq, var, block, line]
```

The pattern-matching engine already extracts `if x is not None` guards.
The type oracle provides annotations that seed the initial state.  The
CFG provides the branch structure.  The only new work is: (a) defining the
lattice join (`definitely_none ⊔ definitely_not_none = maybe_none`), (b)
extracting None-producing patterns (function returns, dict.get, etc.), and
(c) the violation query.

**What it enables:**
- "This variable might be None here" warnings (correctness)
- "Safe to add `.x` here" confidence for automated edits
- None-guard insertion as an automated fix

### 2. Constant Propagation / Folding (Value: Medium, Effort: Medium)

**The pitch:** Track which variables hold known constant values through the
CFG.  This is the simplest "real" symbolic evaluation — the abstract domain is
`Constant(v) | Top` (unknown).

**Why it's useful for emend's goals:**

- **Dead branch detection:** If `DEBUG = False` and later `if DEBUG:`, the
  true branch is dead.  This goes beyond reference-counting dead code — the
  symbol is referenced, but the code is unreachable.
- **String value tracking:** If `table_name = "users"` and later
  `cursor.execute(f"SELECT * FROM {table_name}")`, constant propagation
  recovers the concrete SQL, enabling cross-language DSL analysis.
- **Config resolution:** Many Python codebases use `MODE = os.environ.get("MODE", "dev")`.
  Constant propagation with a "known environment" seed could specialize
  analysis to production vs. development paths.

**Datalog sketch:**

```datalog
% Seed: literal assignments
const_val[fp, fq, var, block, val] :=
    literal_assign[fp, fq, var, block, val]

% Propagate through CFG (only if not redefined)
const_val[fp, fq, var, to_block, val] :=
    const_val[fp, fq, var, from_block, val],
    *cfg_edge[fp, fq, from_block, to_block, _, _, _],
    not redefines[fp, fq, var, to_block]

% Kill on conflicting values at join points
% (This requires stratified negation or aggregation)
const_conflict[fp, fq, var, block] :=
    const_val[fp, fq, var, block, v1],
    const_val[fp, fq, var, block, v2],
    v1 != v2
```

The join-point problem (two branches assign different constants) requires
either stratified negation or an aggregation step.  CozoDB supports both.

### 3. String Abstract Domain (Value: High for DSL analysis, Effort: High)

**The pitch:** Track symbolic string values through concatenation, f-string
formatting, and `.format()` calls.  The abstract domain is a *string
template*: `"SELECT * FROM " ++ $table ++ " WHERE id = " ++ $id`.

**Why it matters for emend specifically:** The DSL engine (`dsl.py`) already
detects embedded SQL, but it works on literal strings.  String symbolic
evaluation would let it analyze *constructed* queries:

```python
query = f"SELECT * FROM {table}"  # table is a parameter
query += f" WHERE {col} = %s"     # col is a parameter
cursor.execute(query)
```

Today the DSL engine can't see the SQL structure because it's spread across
statements.  With string symbolic evaluation, we reconstruct the template
at the `execute()` call site and hand it to the SQL parser.

This also directly improves taint analysis — instead of just "tainted: yes/no",
we know *which parts* of the string are tainted (the `$id` fragment but not
the `SELECT * FROM` prefix).

**Datalog fit:** String templates are trees (concatenation nodes with
constant-string and symbolic leaves).  Representing them as Datalog facts
is possible but awkward — this might be better as a Python post-processing
step over Datalog-computed reaching definitions.

### 4. Interval / Range Domain (Value: Medium, Effort: Medium)

**The pitch:** Track numeric ranges: after `if 0 <= i < len(arr):`, we know
`i ∈ [0, len(arr)-1]`.

**What it enables:**
- Index-out-of-bounds detection
- Loop bound estimation (useful for complexity analysis)
- Branch feasibility (is this `else` branch reachable?)

**Datalog fit:** Moderate.  Interval arithmetic (add, subtract, intersect,
widen) maps to built-in arithmetic in CozoDB, but the widening operator
(needed to ensure termination on loops) requires careful encoding.  The
standard approach is to widen after N iterations, which maps to a bounded
recursion depth in Datalog.

### 5. Typestate via Symbolic States (Value: High, Effort: Medium)

Already discussed in `next-analyses.md`, but worth reframing: typestate
analysis *is* symbolic evaluation where the abstract domain is a finite
automaton of object states.  The taint engine already does this — "tainted"
and "untainted" are two states, and sanitizers are transitions.

Generalizing to arbitrary state machines:

```datalog
% Protocol: file must be opened before read, closed after use
obj_state[fp, fq, var, block, "open"] :=
    obj_state[fp, fq, var, prev_block, "closed"],
    *cfg_edge[fp, fq, prev_block, block, _, _, _],
    method_call_at[fp, fq, var, "open", block]

% Violation: read on a closed file
typestate_violation[fp, fq, var, block, line, "read_after_close"] :=
    obj_state[fp, fq, var, block, "closed"],
    method_call_at[fp, fq, var, "read", block]
```

The `method_call` relation already exists in the fact graph.  Protocol
definitions could live in `.emend/policies.yaml` alongside existing policy
checks.

### 6. Symbolic Preconditions for Automated Edits (Value: High, Effort: Medium-High)

**The pitch:** Before applying an automated refactoring, symbolically evaluate
whether the transformation preserves behavior.  This is where symbolic
evaluation most directly serves emend's editing mission.

**Examples:**

- **Safe extract-method:** "Can I extract lines 10-20 into a function?"
  Symbolically, this requires that (a) no variable defined in 10-20 is used
  after line 20 except via the return value, and (b) the extracted block has
  no side effects that depend on local state.  The def-use chains + effect
  analysis answer (a) and (b).

- **Safe reorder:** "Can I swap these two statements?"  Only if their
  def-use chains don't intersect and their effects don't interfere.  This is
  a symbolic independence check.

- **Safe inline:** "Can I inline this function call?"  Requires that the
  function is pure (no side effects) or that inlining preserves the
  evaluation order of effects.

These checks compose existing analyses (def-use, effects, call graph) into
*compound symbolic predicates*.  They don't require a full symbolic
interpreter — just the ability to query multiple facts and reason about their
conjunction.

**Datalog fit:** Excellent.  "Is it safe to extract lines 10-20?" becomes:

```datalog
% Variables defined in the region
region_def[fp, fq, var] :=
    *def_use[fp, fq, var, "write", block, _, line, _, _, _],
    line >= 10, line <= 20

% Variables used after the region
post_region_use[fp, fq, var] :=
    *def_use[fp, fq, var, "read", _, _, _, _, use_line, _],
    use_line > 20

% Escape analysis: defined in region, used after — must be returned
must_return[fp, fq, var] :=
    region_def[fp, fq, var],
    post_region_use[fp, fq, var]

% Blocker: too many escaping variables (can't return a tuple of 5 things)
extract_blocked[fp, fq] :=
    must_return[fp, fq, v1],
    must_return[fp, fq, v2],
    must_return[fp, fq, v3],
    must_return[fp, fq, v4],
    v1 != v2, v1 != v3, v1 != v4,
    v2 != v3, v2 != v4, v3 != v4
```

## Architectural Options

### Option A: Everything in Datalog

Encode each abstract domain as Datalog relations and let CozoDB's semi-naive
evaluation compute the fixed point.

**Pros:**
- Uniform infrastructure — no new execution engines
- Composable — different domains are just more relations in the same query
- Incremental — CozoDB's evaluation is inherently incremental
- Debuggable — intermediate states are queryable relations

**Cons:**
- Lattice joins with widening are awkward in pure Datalog (need aggregation
  or stratified negation)
- String domains don't fit well (tree-structured values)
- Performance for large abstract domains is untested

**Best for:** Nullability, taint extensions, typestate, refactoring
preconditions — anything with a small, finite abstract domain.

### Option B: Datalog for Propagation, Python for Transfer Functions

Use Datalog to compute which facts reach which program points (the "framework"
part of a dataflow analysis), but implement the transfer functions —
the domain-specific abstract operations — in Python.

**Pros:**
- Full expressiveness for complex domains (strings, intervals)
- Datalog handles the graph traversal it's good at
- Python handles the domain arithmetic it's good at

**Cons:**
- Two-phase: extract reaching facts via Datalog, apply transfer functions
  in Python, feed results back.  The fixed-point iteration lives in Python.
- Loses some composability (can't freely join across Datalog and Python
  domains in a single query)

**Best for:** String domain, interval domain — anything where the abstract
operations are complex.

### Option C: E-Graph Integration

The rewrite engine already has an e-graph implementation.  Symbolic
expressions (from constant propagation or partial evaluation) could be
represented as e-graph nodes, with rewrite rules expressing algebraic
simplifications.

**Pros:**
- Multi-step simplification without choosing an order (equality saturation)
- Natural representation for "these expressions are equivalent"
- Already implemented

**Cons:**
- E-graphs are great for term rewriting but don't naturally handle control
  flow (branches, loops, side effects)
- The current implementation is expression-level only

**Best for:** Expression simplification, algebraic optimization, detecting
equivalent but syntactically different code (clone detection).

### Recommendation: Option A as Default, Option B for Complex Domains

The existing taint analysis proves that Option A works at scale.  Start by
adding new abstract domains (nullability, typestate, constant propagation)
as Datalog rule sets.  Graduate to Option B only for domains that genuinely
need complex transfer functions (strings, intervals).

Use Option C orthogonally for expression-level reasoning (detecting
equivalent expressions, simplifying extracted code).

## What Would a `emend symbolic` Command Look Like?

Probably not a single command.  The symbolic evaluation capabilities would
surface through existing commands with richer results:

```bash
# Nullability warnings integrated into lint
emend lint --domain nullability

# Constant propagation for dead branch detection
emend deadcode --symbolic

# String reconstruction for DSL analysis
emend dsl --resolve-strings

# Refactoring precondition checks
emend move file.py::func --check-only  # "safe to move? yes/no + reasons"

# Typestate protocol checking via policy
emend policy --check file-protocol
```

And via the fact graph for ad-hoc queries:

```bash
# "What's the symbolic state of `conn` at line 42?"
emend facts --type symbolic --file db.py --name conn --line 42

# "Which variables might be None in this function?"
emend facts --type nullability --file handler.py --symbol process_request
```

## Priority Ranking for Implementation

| # | Domain | Value | Effort | Dependencies |
|---|--------|-------|--------|-------------|
| 1 | Nullability | High | Medium | Type oracle seeds, CFG, def-use |
| 2 | Typestate | High | Medium | method_call facts, policy YAML |
| 3 | Refactoring preconditions | High | Medium | def-use, effect inference |
| 4 | Constant propagation | Medium | Medium | Literal extraction, CFG |
| 5 | String domain | High | High | Constant prop, DSL engine |
| 6 | Interval/range | Medium | Medium-High | Widening in Datalog |

Items 1-3 are natural next steps that build directly on existing
infrastructure with no new execution engines.  Item 4 is a prerequisite for
item 5.  Item 6 is valuable but the widening problem makes it trickier than
it looks.

## Open Questions

1. **Widening in Datalog:** CozoDB's recursion terminates via stratification
   and semi-naive evaluation, but numeric domains need widening to converge
   on loops.  Options: (a) bounded iteration depth (lose some precision),
   (b) aggregate functions as widening operators, (c) Python-side widening
   between Datalog iterations.

2. **Aliasing:** All the proposals above are *variable-level* — they track
   `x` but not "the object that `x` and `y` both point to."  Aliasing
   is the classic hard problem.  For Python, a pragmatic heuristic: assume
   no aliasing unless we see `y = x` (in which case propagate to both).
   Points-to analysis (already noted as a Datalog-expressible query in
   `next-analyses.md`) would improve this.

3. **Interprocedural symbolic evaluation:** The current interprocedural
   trace analysis uses function summaries (`param → return`, `param → sink`).
   Richer symbolic evaluation would want summaries like "if param is None,
   return is None; otherwise return is not-None."  This is a conditional
   summary, which is significantly more complex.

4. **Integration with type checkers:** Pyright/mypy already do nullability
   and type narrowing.  Should emend duplicate this, or consume their results?
   The type oracle adapter pattern suggests consuming — but type checkers
   don't expose their internal abstract states, only their final type
   assignments.  emend's value-add would be analyses that type checkers
   *don't* do: typestate, taint, string reconstruction, refactoring safety.

5. **Performance budget:** Symbolic evaluation is more expensive than
   syntactic analysis.  What's the latency budget?  For lint (batch), seconds
   are fine.  For editor integration, sub-100ms is needed.  Incremental
   analysis (recompute only changed functions) is essential for the editor
   case.
