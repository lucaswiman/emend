# Taint-CFG Precision: Effects, Path Quantifiers, and Temporal Sequences

## Motivation

Trying to describe a TOCTOU rule in English reveals five concepts the
pattern language cannot express:

> Flag when a value is **loaded** from a shared store, then **mutated**,
> without an **exclusive lock** on **every execution path** between load
> and mutation, and before the transaction **ends**.

| English concept | Current status |
|---|---|
| "loaded from a shared store" | Source patterns (works) |
| "mutated" | Bespoke `attribute_mutation_sinks` — not a pattern |
| "exclusive lock held" | Sanitizer pattern (works) |
| "on every execution path" | No path quantification |
| "the same object" | Implicit via variable name |
| "before the transaction ends" | No scope/lifetime concept |
| "then" (temporal ordering) | Implicit via taint propagation |

Five of seven concepts are missing or hacked around.  Each gap requires
a general mechanism, not a TOCTOU-specific fix.

## Design Principles

1. **General mechanisms over bespoke features.**  Each change increases
   the expressive power of the taint/CFG/Datalog stack.
2. **Facts are the interface.**  New capabilities = new fact types +
   Datalog queries, not Python special cases.
3. **No backwards compatibility burden.**  Schemas and APIs can change.

## Overview of Phases

| Phase | Concept | Replaces |
|---|---|---|
| 1 | Effect predicates (`writes`, `reads`, `calls`) | `attribute_mutation_sinks`, augmented-assignment blindness |
| 2 | Path quantifiers (`all_paths` / `some_path`) | Eager taint deletion in sanitizers |
| 3 | Scope boundaries (`scope_sanitizers`) | No transaction-lifetime tracking |
| 4 | Type-conditioned filtering (`type_constraint`) | False positives on scalar queries |
| 5 | Temporal sequence patterns | Source/sink decomposition for multi-step rules |

Each phase is detailed below with CozoDB schema changes and
parameterized Datalog queries.

---

## Phase 1: Effect Predicates

### Problem

"Mutates `$OBJ`" is an equivalence class over many syntactic forms:
`obj.f = v`, `obj.f += v`, `setattr(obj, 'f', v)`, `del obj.f`,
`obj.items.append(v)`.  Today each requires its own pattern or bespoke
mechanism.  `attribute_mutation_sinks` handles plain assignment;
augmented assignment is invisible; method-call mutation is untracked.

### Concept

An **effect predicate** classifies what a statement *does* to a
variable, resolved from the fact graph rather than from pattern
enumeration.  Available predicates:

| Predicate | Meaning |
|---|---|
| `writes($X)` | Any mutation of `$X` or its attributes (assign, aug-assign, del, setattr) |
| `reads($X)` | Any observation of `$X`'s value |
| `calls($X, $M)` | Method call `$X.$M(...)` |
| `defines($X)` | Any binding of name `$X` |

### Fact graph changes

**1. Add `kind` to `def_use` relation.**

Current schema:

```
{:create def_use {
    file_path: String, func_qn: String, var_name: String,
    def_block: Int, use_block: Int
    => def_line: Int, def_col: Int, use_line: Int, use_col: Int
}}
```

New schema:

```
{:create def_use {
    file_path: String, func_qn: String, var_name: String,
    kind: String,
    def_block: Int, use_block: Int
    => def_line: Int, def_col: Int, use_line: Int, use_col: Int
}}
```

`kind` values: `"read"`, `"write"`, `"aug_write"`, `"del"`.

**Rust `emend_core` change:** The CFG builder already distinguishes
tree-sitter node types (`assignment`, `augmented_assignment`,
`delete_statement`).  Tag each def/use entry with the corresponding
kind.  Augmented assignments emit *both* a `use` (kind=`"read"`) and
a `def` (kind=`"aug_write"`) for the target variable in the same
block.

**2. Add `method_call` relation.**

For tracking `obj.method()` calls where `obj` is the receiver:

```
{:create method_call {
    file_path: String, func_qn: String,
    receiver: String, method: String,
    block_id: Int, line: Int
}}
```

Populated from the Rust scope resolver: when a `call` reference has
a dotted callee like `obj.append`, emit a `method_call` fact with
`receiver="obj"`, `method="append"`.

### Datalog: resolving effect predicates

Effect predicates are resolved into CozoScript subqueries.  The
Python layer translates `writes($OBJ)` in a sink config into the
appropriate Datalog join.

**Helper: dotted-name prefix matching.**

CozoDB's `starts_with` is byte-level, so `starts_with("objection", "obj")`
is true.  All dotted-prefix checks must enforce a dot boundary:

```
% var_name is var itself, or var.something
is_var_or_attr[var_name, var] :=
    var_name == var
is_var_or_attr[var_name, var] :=
    starts_with(var_name, concat(var, "."))
```

This helper is inlined (or emitted as a rule) wherever the proposal
says "match `var` or `var.*`".

**`writes(var)` in block `B`** — any of:

```
% Plain or augmented write to var or var.* in block B
write_kind[k] <- [["write"], ["aug_write"], ["del"]]
...
*def_use[fp, fq, var_name, kind, _, B, _, _, _, _],
write_kind[kind],
is_var_or_attr[var_name, var]

% OR: method call on var (e.g. var.append(...))
*method_call[fp, fq, var, _, B, _]
```

**`reads(var)` in block `B`**:

```
*def_use[fp, fq, var_name, "read", _, B, _, _, _, _],
is_var_or_attr[var_name, var]
```

**CozoScript note:** CozoDB has no `x in [...]` syntax.  Set
membership is expressed via inline relations joined as subqueries
(e.g. `write_kind[kind]` above).  All examples in this document
use that convention.

### Taint config changes

Replace `attribute_mutation_sinks` with effect-based sinks:

```yaml
# Before (bespoke):
attribute_mutation_sinks:
  - label: unlocked_read
    message: "TOCTOU: mutation on unlocked ORM object"

# After (general):
sinks:
  - effect: "writes($OBJ)"
    label: unlocked_read
    message: "TOCTOU: mutation on unlocked ORM object"
```

An `effect` key on a sink/source/sanitizer means "resolve via fact
graph" instead of "match via pattern."  Both `pattern` and `effect`
can coexist in the same config; they are different ways to identify
the same thing (a source/sink/sanitizer location).

### Taint.py changes

**`_find_assignments_in_source()`:** Add augmented assignment regex:

```python
_AUG_ASSIGN_RE = re.compile(
    r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*"
    r"(\+|-|\*|/|//|%|\*\*|&|\||\^|>>|<<)=\s*(.+)",
    re.DOTALL,
)
```

Emit as `("mutate", (target, rhs))` ops.  In Step 2 propagation,
treat `"mutate"` like `"assign"` but also preserve existing taint on
the target.  In the sink check, `"mutate"` ops fire effect-based sinks
the same way `"assign"` ops do.

**Step 3.5 removal:** Once effect-based sinks are implemented,
`attribute_mutation_sinks` and the Python Step 3.5 loop are deleted.
The Datalog query handles all mutation forms uniformly.

### Parameterized Datalog query

The `taint_propagation_datalog()` method gains an `effect_sinks`
parameter.  When present, the violation query joins against `def_use`
kind and `method_call` instead of requiring pre-computed sink
locations:

```python
def taint_propagation_datalog(
    self,
    sources: list[tuple[str, str, str, int, str]],
    sinks: list[tuple[str, str, str, int, str]] | None = None,
    effect_sinks: list[tuple[str, str]] | None = None,
    # effect_sinks: [(label, effect_kind)] e.g. [("unlocked_read", "writes")]
    sanitizers: ...,
) -> list[TaintFlowFact]:
```

When `effect_sinks` is provided, the violation rule becomes:

```
% Allowed write kinds
write_kind[k] <- [["write"], ["aug_write"], ["del"]]

% Effect-based sink: tainted var is written/mutated in a reachable block
?[fp, fq, src_var, sink_var, lbl, src_block, sink_block] :=
    tainted[fp, fq, sink_var, sink_block, lbl],
    effect_sink_label[lbl],
    *def_use[fp, fq, dv, kind, _, sink_block, _, _, sink_line, _],
    write_kind[kind],
    is_var_or_attr[dv, sink_var],
    taint_source[fp, fq, src_var, src_block, lbl]
```

The `effect_sink_label` is an inline relation built from the
`effect_sinks` parameter:

```python
esl_rows = ", ".join(f'["{lbl}"]' for lbl, _ in effect_sinks)
effect_sink_rule = f'effect_sink_label[lbl] <- [{esl_rows}]\n'
```

---

## Phase 2: Path-Sensitive Sanitization

### Problem

Sanitizers today delete taint eagerly from a flat dict.  If
`with_for_update()` appears on one branch of an if/else, the taint
is deleted globally — the unsanitized branch is silently safe.
Conversely, if the sanitizer appears *after* the sink in source
order but on a different branch, it may not fire at all.

### Concept: path quantifiers

Three quantifiers over CFG paths between two matched points:

| Quantifier | English | Fires when |
|---|---|---|
| `all_paths` | "on every path between A and B" | *No* unsanitized path exists from source to sink |
| `some_path` (default) | "on some path" | *Any* unsanitized path exists |
| `no_path` | "on no path" | Complement of `some_path` |

The default for sanitizers becomes `all_paths`: a sanitizer must
appear on **every** path from source to sink to suppress the
violation.  This matches the English "without a lock on every path."

### Fact graph changes

No new relations.  The existing `cfg_edge` and `cfg_block` relations
provide all the structure needed.  Sanitizer locations are passed as
inline relations (same as sources/sinks today).

### Datalog: `some_path` unsanitized reachability

Replace the current `taint_propagation_datalog()` propagation rule.

Current (def-use chain, flat sanitizer check):

```
tainted[fp, fq, target, use_block, lbl] :=
    tainted[fp, fq, source, def_block, lbl],
    *def_use[fp, fq, source, def_block, use_block, _, _, _, _],
    target = source,
    not sanitizer[fp, fq, source, def_block, lbl]
```

New (CFG-edge reachability with per-edge sanitizer blocking):

```
% Base case: source block is unsanitized-reachable
unsanitized[fp, fq, lbl, block] :=
    taint_source[fp, fq, _, block, lbl]

% Recursive: propagate along CFG edges, blocked by sanitizer blocks
unsanitized[fp, fq, lbl, to_block] :=
    unsanitized[fp, fq, lbl, from_block],
    *cfg_edge[fp, fq, from_block, to_block, _, _, _],
    not sanitizer_block[fp, fq, lbl, from_block]

% A variable is tainted in a block if:
%   (a) it's a source in that block, OR
%   (b) taint propagates to it via def-use AND the block is
%       unsanitized-reachable from the source
tainted[fp, fq, var, block, lbl] :=
    taint_source[fp, fq, var, block, lbl]

tainted[fp, fq, var, use_block, lbl] :=
    tainted[fp, fq, var, def_block, lbl],
    *def_use[fp, fq, var, kind, def_block, use_block, _, _, _, _],
    unsanitized[fp, fq, lbl, use_block]
```

Key difference: taint only propagates to blocks that are
**unsanitized-reachable** from the source.  If a sanitizer dominates
the sink (all paths pass through it), no `unsanitized` fact is
derived for the sink block, so the violation is suppressed.

**Intra-block ordering caveat.** If a sanitizer and a sink are in
the *same* basic block, the sanitizer blocks propagation *out of*
the block but does not prevent violations *within* it.  Example:
`obj = q.with_for_update().first(); obj.x = 1` where both are in
block B — block B is unsanitized-reachable from the source, so the
sink fires even though the sanitizer precedes it.

Fix: when a sanitizer and sink co-occur in the same block, the
violation query should include a line-ordering guard:
`sink_line > sanitizer_line` suppresses the violation.  The Python
layer adds per-block `(sanitizer_line, label)` facts to an inline
relation, and the violation rule checks:

```
% Same-block suppression
not same_block_sanitized[fp, fq, lbl, sink_block, sink_line]

same_block_sanitized[fp, fq, lbl, block, sink_line] :=
    sanitizer_in_block[fp, fq, lbl, block, san_line],
    sink_line > san_line
```

This requires the sanitizer inline relation to carry line numbers
(already available from pattern matching).

### Sanitizer block resolution

The Python layer resolves sanitizer patterns to `(file_path, func_qn,
label, block_id)` tuples and passes them as an inline relation:

```python
san_block_rows = ", ".join(
    f'["{fp}", "{fq}", "{lbl}", {bid}]'
    for fp, fq, lbl, bid in sanitizer_blocks
)
sanitizer_block_rule = f"sanitizer_block[fp, fq, lbl, bid] <- [{san_block_rows}]\n"
```

### `flow_rule_check_datalog()` — implement `through`

The currently-stubbed `through` parameter uses the same mechanism.
`through` means "flag only if a path exists that does *not* pass
through the required point" — i.e., the complement of `all_paths`:

```
% Required pass-through points
required[fp, fq, bid] <- [...]

% Reachability avoiding required points
avoids_required[fp, fq, block] :=
    flow_source[fp, fq, _, block]

avoids_required[fp, fq, to_block] :=
    avoids_required[fp, fq, from_block],
    *cfg_edge[fp, fq, from_block, to_block, _, _, _],
    not required[fp, fq, from_block]

% Violation: sink reachable while avoiding required point
?[fp, fq, src_var, sink_var] :=
    avoids_required[fp, fq, sink_block],
    flow_sink[fp, fq, sink_var, sink_block],
    flow_source[fp, fq, src_var, _]
```

### Config syntax

```yaml
sanitizers:
  - pattern: "$Q.with_for_update()"
    label: unlocked_read
    # quantifier: all_paths  ← this is the default
  - pattern: "validate($X)"
    label: user_input
    quantifier: some_path   # any path through validate() suffices
```

---

## Phase 3: Scope Boundaries

### Problem

Taint lives until function end or sanitizer match.  But many taint
labels have natural lifetimes: a database row is tainted only within
its transaction; a file handle is relevant only while open.  After
`session.commit()`, *all* `unlocked_read` taint is meaningless —
not just the variable in the pattern.

### Concept: scope sanitizers

A **scope sanitizer** is a pattern that, when matched, kills all
taint for a given label — not just variables captured by the pattern.
It models "the scope/context has ended."

### Fact graph changes

No new stored relations.  Scope sanitizers are resolved to block IDs
and passed as an inline relation to the Datalog query, reusing the
`unsanitized` propagation from Phase 2.

### Datalog integration

Scope sanitizers produce `scope_kill` facts that block *all* taint
for a label, regardless of variable:

```
scope_kill[fp, fq, lbl, block] <- [...]  % inline from Python

% Phase 2's unsanitized rule gains an extra negation:
unsanitized[fp, fq, lbl, to_block] :=
    unsanitized[fp, fq, lbl, from_block],
    *cfg_edge[fp, fq, from_block, to_block, _, _, _],
    not sanitizer_block[fp, fq, lbl, from_block],
    not scope_kill[fp, fq, lbl, from_block]
```

This is the only change — scope kills plug directly into the
existing CFG-edge propagation.

### Config syntax

```yaml
scope_sanitizers:
  - pattern: "session.commit()"
    label: unlocked_read
  - pattern: "session.close()"
    label: unlocked_read
```

### Known limitation: nested scopes

Scope sanitizers kill *all* taint for a label, not just taint
associated with a particular session/handle.  With nested sessions:

```python
with Session() as outer:
    obj = outer.query(Model).first()
    with Session() as inner:
        inner_obj = inner.query(Model2).first()
        inner.commit()  # kills ALL unlocked_read taint
    obj.name = "x"      # false negative: obj's taint was killed
```

A precise fix requires **scoped labels** — associating taint with
the session variable that produced it, so `inner.commit()` only
kills taint from `inner`.  This could be modeled by extending scope
sanitizers to capture a metavar (`$S.commit()`) and only killing
taint whose origin was `$S.query(...)`.  The `scope_kill` relation
would gain an `origin_var` column.  This is deferred: nested
sessions are uncommon enough that the simpler "kill all" semantics
is a reasonable first version.  The limitation should be documented.

### Taint.py changes

In the Python fallback path, scope sanitizers clear all entries for
the given label from `taint_state` (not just captured variables).
This is a ~5-line change in the sanitizer application step.

---

## Phase 4: Type-Conditioned Filtering

### Problem

`session.query(func.count(Model.id))` matches the `session.query($MODEL)`
source pattern, but the result is an `int`, not a mutable ORM object.
The taint is a false positive.  No amount of pattern refinement can
distinguish these — the syntactic form is identical.

### Concept: type constraints on taint rules

Allow an optional `type_constraint` on any source, sink, or sanitizer.
When present, the rule only fires if the assignment target's inferred
type satisfies the constraint.  This reuses the existing `TypeOracle`.

### Fact graph changes

**Populate `type_binding` during `build_from_project()`.**

The `type_binding` relation already exists in the schema but is not
populated.  Wire the type oracle into the build step:

```python
# In build_from_project(), after symbol extraction:
oracle = create_type_oracle(engine="auto")
if oracle:
    file_types = oracle.query_file(abs_file_path)
    for binding in file_types.bindings:
        graph.add_type(TypeFact(
            symbol_qn=binding.qualified_name,
            type_str=binding.type_str,
            file_path=rel_path,
            line=binding.line,
            binding_kind=binding.kind,
        ))
```

### Datalog integration

Type constraints are evaluated as an extra predicate on taint sources.
The Python layer resolves `type_constraint: "!int & !float"` into a
CozoScript filter:

```
% Scalar type names (inline relation)
scalar_type[t] <- [["int"], ["float"], ["bool"], ["str"]]

% Type-filtered source: only taint if type is not scalar
effective_source[fp, fq, var, block, lbl] :=
    taint_source[fp, fq, var, block, lbl],
    not scalar_typed[fp, fq, var, block]

scalar_typed[fp, fq, var, block] :=
    taint_source[fp, fq, var, block, _],
    *type_binding[_, fp, line, _, type_str],
    *def_use[fp, fq, var, _, _, block, line, _, _, _],
    scalar_type[type_str]
```

Note: `scalar_type` uses exact equality, not substring matching.
A type like `Optional[int]` would *not* match `"int"`.  For
compound types the Python layer should normalize to the top-level
constructor before inserting into `type_binding` (e.g.,
`Optional[int]` → `Optional`), or the inline relation should list
the full forms.  In practice, the Python fallback handles compound
types more naturally: after pattern matching identifies a source,
query the type oracle and evaluate the constraint in Python where
`parse_type_string()` provides structured access.

### Config syntax

```yaml
sources:
  - pattern: "session.query($MODEL)"
    label: unlocked_read
    type_constraint: "!int & !float & !bool & !str"
```

The constraint is a boolean expression over type names: `!` (not),
`&` (and), `|` (or).  Bare names are matched against the
**top-level** type constructor returned by `parse_type_string()`,
not as substrings — `int` matches `int` but not `Optional[int]` or
`MyInteger`.

---

## Phase 5: Temporal Sequence Patterns

### Problem

The source/sink model encodes a two-point temporal constraint: "taint
enters here, reaches there."  But many real rules are multi-step:

- TOCTOU: load → mutate (two steps, no lock between)
- Double-free: free → free (same pointer, no realloc between)
- Use-after-close: close → use (same handle)
- Missing cleanup: acquire → end-of-scope (no release between)

Each of these is awkward to express as source+sink+sanitizer because
the "steps" aren't data-flow connected — they're temporally ordered
operations on the same object.

### Concept: sequence rules

A **sequence rule** is an ordered list of **steps**, each matching
by pattern or effect, with **binding constraints** across steps and
**path constraints** between them.

```yaml
rules:
  - name: toctou-unlocked-mutation
    message: "Mutation on ORM object loaded without SELECT FOR UPDATE"
    sequence:
      - bind: load
        pattern: "$OBJ = session.query($MODEL)"
      - bind: mutate
        effect: "writes($OBJ)"
    path:
      load -> mutate:
        not_through:
          - pattern: "$Q.with_for_update()"
        not_through_scope:
          - pattern: "session.commit()"
```

### Semantics

1. Each step is resolved to a set of `(file_path, func_qn, block_id,
   line, bindings)` tuples — either via pattern matching or effect
   resolution.
2. **Binding propagation:** metavariables (like `$OBJ`) captured in
   one step are available in later steps.  `effect: "writes($OBJ)"`
   means "the variable bound to `$OBJ` in the `load` step is
   written."  Resolution: the Python layer substitutes the concrete
   variable name from the first match into the effect query.
3. **Ordering:** step `load` must appear in a CFG block that
   *precedes* step `mutate` (via CFG reachability), not just in an
   earlier source line.
4. **Def-use liveness:** when a binding is carried between steps,
   the variable must still be live (not redefined) between them.
   If `obj` is reassigned between the load and the mutation, the
   mutation operates on a different object.  The violation query
   joins against `def_use` to ensure the definition from step A
   reaches the use at step B without an intervening redefinition.
   This prevents false positives from reassignment.
5. **Path constraints:** `not_through` between two steps means "there
   exists a CFG path from step A's block to step B's block that does
   not pass through any block matching the given pattern."  This
   reuses Phase 2's `unsanitized` propagation.

### Datalog implementation

A two-step sequence with a `not_through` constraint compiles to:

```
% Step 1: resolve "load" locations
step_load[fp, fq, block, line, obj_var] <- [...]  % from Python pattern matching

% Step 2: resolve "mutate" locations (effect-based)
write_kind[k] <- [["write"], ["aug_write"]]
step_mutate[fp, fq, block, line, obj_var] :=
    step_load[fp, fq, _, _, obj_var],
    *def_use[fp, fq, dv, kind, _, block, _, _, line, _],
    write_kind[kind],
    is_var_or_attr[dv, obj_var]

% Blocker: not_through pattern locations
blocker[fp, fq, block] <- [...]  % from Python pattern matching

% Unsanitized reachability from load to mutate
reachable[fp, fq, block, obj_var] :=
    step_load[fp, fq, block, _, obj_var]

reachable[fp, fq, to_block, obj_var] :=
    reachable[fp, fq, from_block, obj_var],
    *cfg_edge[fp, fq, from_block, to_block, _, _, _],
    not blocker[fp, fq, from_block]

% Def-use liveness: obj_var's definition from the load step reaches the
% mutate block without an intervening redefinition
live_binding[fp, fq, obj_var, load_block, use_block] :=
    step_load[fp, fq, load_block, _, obj_var],
    *def_use[fp, fq, obj_var, _, load_block, use_block, _, _, _, _]

% Violation: mutate step reachable from load, without passing through
% blocker, AND the variable binding is still live (not redefined)
?[fp, fq, obj_var, load_line, mutate_line] :=
    reachable[fp, fq, mutate_block, obj_var],
    step_mutate[fp, fq, mutate_block, mutate_line, obj_var],
    step_load[fp, fq, load_block, load_line, obj_var],
    live_binding[fp, fq, obj_var, load_block, mutate_block]
```

For N-step sequences, the query chains N reachability relations.
Each pair of consecutive steps gets its own `reachable_i_to_j`
relation with its own blocker set.

### Relationship to existing source/sink model

A traditional taint rule is a degenerate two-step sequence:

```yaml
# This taint rule:
sources:
  - pattern: "request.args.get($X)"
    label: user_input
sinks:
  - pattern: "cursor.execute($Q)"
    label: user_input

# Is equivalent to this sequence rule:
rules:
  - name: sqli
    sequence:
      - bind: source
        pattern: "$V = request.args.get($X)"
      - bind: sink
        pattern: "cursor.execute($Q)"
    path:
      source -> sink:
        data_flow: "$V flows to $Q"
```

The difference is that sequence rules make the temporal structure
explicit and allow steps connected by effects or ordering rather
than only by data flow.  The existing source/sink model continues
to work unchanged — sequence rules are a superset.

### Implementation approach

Sequence rules are compiled to Datalog by a new
`compile_sequence_rule()` function in `fact_graph.py`.  It:

1. Resolves each step to locations (via pattern matching in Python
   or effect queries in Datalog).
2. Generates inline relations for each step's locations.
3. Generates `reachable` rules between consecutive steps with
   blocker negation.
4. Generates the final violation query joining all steps.

The function returns a CozoScript string that can be passed to
`self._client.run()`.

---

## Revised TOCTOU Config (After All Phases)

```yaml
taint:
  labels:
    - unlocked_read

  sources:
    - pattern: "session.query($MODEL)"
      label: unlocked_read
      type_constraint: "!int & !float & !bool & !str"
    - pattern: "session.get($MODEL, $ID)"
      label: unlocked_read

  sanitizers:
    - pattern: "$Q.with_for_update()"
      label: unlocked_read
      # quantifier: all_paths  (default)
    - pattern: "$Q.with_for_update($ARGS)"
      label: unlocked_read

  scope_sanitizers:
    - pattern: "session.commit()"
      label: unlocked_read
    - pattern: "session.close()"
      label: unlocked_read

  sinks:
    - effect: "writes($OBJ)"
      label: unlocked_read
      message: "TOCTOU: mutation on ORM object without SELECT FOR UPDATE"
```

Or equivalently as a sequence rule (no taint labels needed):

```yaml
rules:
  - name: toctou-unlocked-mutation
    severity: error
    message: "Mutation on ORM object loaded without SELECT FOR UPDATE"
    sequence:
      - bind: load
        pattern: "$OBJ = session.query($MODEL)"
        type_constraint: "!int & !float & !bool & !str"
      - bind: mutate
        effect: "writes($OBJ)"
    path:
      load -> mutate:
        not_through:
          - pattern: "$Q.with_for_update()"
        not_through_scope:
          - pattern: "session.commit()"
          - pattern: "session.close()"
```

Both forms produce identical Datalog queries.  The sequence form is
more readable; the taint form composes with interprocedural analysis.

---

## Summary of Changes by File

| File | Phase | Change |
|---|---|---|
| `emend_core` (Rust) | 1 | Def/use kind tagging; augmented assignment emits both def and use; method_call extraction |
| `fact_graph.py` | 1 | `kind` column on `def_use`; `method_call` relation and schema |
| `fact_graph.py` | 2 | CFG-edge `unsanitized` reachability; `through` implementation |
| `fact_graph.py` | 4 | `type_binding` population; type-filtered source relation |
| `fact_graph.py` | 5 | `compile_sequence_rule()` — sequence-to-Datalog compiler |
| `taint.py` | 1 | Augmented assignment regex; `"mutate"` op kind; effect-based sinks |
| `taint.py` | 1 | Remove `attribute_mutation_sinks` and Step 3.5 |
| `taint.py` | 2 | Sanitizer `quantifier` field; per-block taint in Python fallback |
| `taint.py` | 3 | `scope_sanitizers` config key |
| `taint.py` | 4 | `type_constraint` field; type oracle query in source identification |
| `policy.py` | 5 | `SequenceCheck` policy type (wraps sequence rules) |
| `lint.py` | 5 | `sequence` key on lint rules (compiles to sequence check) |
| Config schema | all | New YAML keys: `effect`, `quantifier`, `scope_sanitizers`, `type_constraint`, `sequence`, `path` |
| `commands.rst` | all | Updated TOCTOU example; new sequence rule docs |

---

## Migration: `def_use` Schema Change

Adding `kind` to the `def_use` key changes the column count from 9
to 10.  **Every** existing Datalog query referencing `*def_use[...]`
must be updated in a single pass:

- `taint_propagation_datalog()` (line ~1507)
- `flow_rule_check_datalog()` (line ~1614)
- `dead_code_unified()` (uses def_use indirectly)
- `interprocedural_taint_datalog()` (line ~1550)

All references change from `*def_use[fp, fq, var, db, ub, ...]` to
`*def_use[fp, fq, var, _kind, db, ub, ...]` (binding or ignoring
the new column).  This is a mechanical find-and-replace.

The `DefUseFact` dataclass gains a `kind: str = "read"` field with
a default to avoid breaking callers that don't set it.

---

## Sequence Rule: Dual-Mode Compilation

The taint-as-sequence example (sqli) uses `data_flow: "$V flows to
$Q"`, while the TOCTOU example uses temporal ordering + effect.
These are different mechanisms:

- **Temporal steps** (ordering, effects): compiled to `reachable`
  rules over CFG edges.
- **Data-flow steps** (taint propagation): compiled to `tainted`
  rules over def-use chains.

When both appear in the same sequence, `compile_sequence_rule()`
emits both kinds of rules and conjoins them in the violation query.
Steps connected by `data_flow` use Phase 2's `tainted` propagation.
Steps connected by temporal ordering use `reachable` propagation.
Steps connected by both require both constraints.

---

## Known Limitations (Not Addressed)

These are inherent to the intraprocedural, variable-name-based model
and are **not** fixed by this proposal:

- **Tuple unpacking:** `a, b = tainted_func()` — the assignment
  regex does not match destructuring.  Neither `a` nor `b` is
  tainted.  Fix requires tree-sitter-level assignment parsing.

- **Walrus operator:** `if (x := source()):` — not matched by the
  assignment regex.  Same fix needed.

- **Implicit aliasing:** `alias = obj; alias.x = 1` — works (taint
  propagates through `alias = obj`).  But `items[0].x = 1` where
  `items` contains tainted objects does not fire `writes($OBJ)` for
  the original `$OBJ`.  Requires alias/points-to analysis.

- **Indirect mutation via function calls:** `modify(obj)` where
  `modify` internally writes `obj.x = 1` — the effect predicate
  only sees direct syntactic writes.  Fix requires interprocedural
  effect summaries (a `FuncEffectSummaryFact`).

- **Global/nonlocal variables:** Taint on globals written in one
  function and read in another is invisible to intraprocedural
  analysis.  The interprocedural system tracks param→return flows
  but not shared-state flows.

- **Dead code paths:** CFG-edge reachability considers all edges
  including statically infeasible ones (`if False: ...`).  The
  `unsanitized` relation may propagate through dead branches.

- **Loop-carried taint via back edges:** If a source is in block 3
  and a sink in block 2, CFG reachability via a back edge (3→4→5→2)
  marks the sink as reachable.  This is correct for loop-carried
  dependencies but can produce first-iteration false positives.  The
  def-use liveness check (Phase 5, semantic point 4) mitigates this
  for sequence rules.

---

## Deferred Work

- **Alias analysis / binding identity:** `$X is $Y` constraints
  across sequence steps.  Requires points-to or alias analysis.
  For now, binding identity is by variable name (via metavar
  substitution), which handles the common case.

- **Object-sensitive dispatch:** `obj.method()` resolved by receiver
  type.  Requires type oracle integration at the call-resolution
  level, not just at the taint-source level.

- **Interprocedural sequences:** Sequence steps spanning multiple
  functions.  Requires extending `FuncSummaryFact` to summarize
  effect predicates and sequence participation, not just taint flow.
  A `FuncEffectSummaryFact(func_qn, param, effect)` would model
  "this function writes to its first argument."

- **Scoped labels for nested contexts:** Scope sanitizers that only
  kill taint from a specific origin variable (see Phase 3 caveat).
  Requires extending the `scope_kill` relation with an `origin_var`
  column and tracing taint provenance through the origin.
