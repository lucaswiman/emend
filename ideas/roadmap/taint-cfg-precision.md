# Taint-CFG Precision: Mutation Tracking and Path-Sensitive Sanitization

## Problem

The TOCTOU example in `commands.rst` demonstrates taint analysis detecting
unlocked ORM reads followed by attribute mutation.  It works for the simple
case but has structural blind spots:

**False negatives:**

- Augmented assignments (`obj.balance += amount`) are invisible.  The
  assignment extractor in `taint.py` explicitly skips `+=`, `-=`, etc.
- Method-call mutations (`obj.items.append(x)`, `obj.save()`) aren't
  tracked as mutations on `obj`.
- Subscript mutations on dotted targets (`orm_obj["field"] = val`) are
  missed because the container-mutation regex only matches bare names.

**False positives:**

- Scalar-returning queries (`session.query(func.count(Model.id))`) are
  tainted identically to ORM-instance-returning queries.
- Any attribute write on a tainted object fires the sink, even if the
  field is a local-only computed property unrelated to the database row.
- Objects accessed after `session.close()` / `session.commit()` are
  still tainted despite being expired/detached.

**Missing path sensitivity:**

- Sanitizers (`with_for_update()`) operate by eagerly deleting taint
  state.  If a sanitizer appears on one branch of an if/else, the
  current engine either sanitizes globally (if the sanitizer line
  precedes the sink in source order) or misses it entirely.
- There is no "must-pass-through" semantics: no way to say "flag only
  if there exists *some* path from source to sink that avoids the
  sanitizer."

These aren't specific to TOCTOU.  They apply to any taint analysis that
needs to reason about mutations, types, or branching.

## Design Principles

1. **General mechanisms over bespoke features.**  Each change should
   increase the expressive power of the taint/CFG/Datalog stack, not
   add a one-off handler for a single use case.
2. **Facts are the interface.**  New analysis capabilities should be
   expressible as new fact types + Datalog queries, not as Python
   special cases.
3. **No backwards compatibility burden.**  Existing fact schemas,
   taint config keys, and Python APIs can change freely.

## Phase 1: Augmented and Compound Assignment Tracking

**Goal:** Make `obj.x += 1`, `d[k] += 1`, and similar augmented
assignments visible to taint analysis as both a read and a write.

### Changes

**`taint.py` — `_find_assignments_in_source()`:**

Add a third regex path for augmented assignments on any target shape
(simple, dotted, subscript):

```
target (+=|-=|*=|/=|//=|%=|**=|&=|\|=|\^=|>>=|<<=) rhs
```

Emit these into the ops list as a new `"mutate"` kind (distinct from
`"assign"`).  A `"mutate"` op means "target is both read and written;
RHS contributes to the new value."

**`taint.py` — Step 2 (propagation) and Step 3.5 (attribute mutation sinks):**

- In propagation: a `"mutate"` op on `target` propagates taint from
  *both* the existing taint on `target` *and* any tainted identifiers
  in `rhs`.
- In attribute mutation sinks: check `"mutate"` ops the same way as
  `"assign"` ops — if `base` is tainted, fire the sink.

**Rust `emend_core` — CFG def/use extraction:**

Augmented assignments should emit both a `use` and a `def` for the
target variable in the same block.  Currently the Rust CFG builder
may only emit a `def`.  Verify and fix if needed so that `DefUseFact`
is correct for augmented assignments.

### New fact type: none

No schema change.  `DefUseFact` already covers this once the Rust
extractor emits both def and use.

## Phase 2: Mutation Kind on Def-Use Facts

**Goal:** Let Datalog queries distinguish reads, writes, and
augmented writes without re-parsing source.

### Changes

**`fact_graph.py` — `DefUseFact`:**

Add a `kind` field:

```
kind: "read" | "write" | "aug_write" | "del"
```

- `write`: plain assignment LHS (`x = ...`, `obj.field = ...`)
- `aug_write`: augmented assignment LHS (`x += ...`)
- `read`: any use that is not a write target
- `del`: `del x`

**Rust `emend_core` — block def/use emission:**

Tag each def/use entry with its kind.  The tree-sitter node type
(`assignment`, `augmented_assignment`, `delete_statement`) determines
the kind.

**CozoDB schema:**

```
def_use { file_path, func_qn, var_name, kind, def_block, use_block
          => def_line, def_col, use_line, use_col }
```

### Datalog impact

The attribute mutation sink query becomes expressible in pure Datalog:

```datalog
% Violation: tainted variable has a write/aug_write use in a reachable block
?[fp, fq, var, lbl, sink_block, sink_line] :=
    tainted[fp, fq, var, sink_block, lbl],
    *def_use[fp, fq, var, kind, _, sink_block, _, _, sink_line, _],
    kind in ["write", "aug_write"],
    contains(var, ".")    % dotted target = attribute mutation
```

This replaces the Python Step 3.5 loop entirely.

## Phase 3: Path-Sensitive Sanitization via CFG Reachability

**Goal:** A violation fires only if there exists a CFG path from
source block to sink block that does *not* pass through a sanitizer
block.

### Changes

**`fact_graph.py` — `taint_propagation_datalog()`:**

Replace the current propagation rule (which propagates through
def-use chains with a simple `not sanitizer` check at each hop)
with CFG-edge-based reachability:

```datalog
% Unsanitized reachability from source
unsanitized[fp, fq, var, block, lbl] :=
    taint_source[fp, fq, var, block, lbl]

unsanitized[fp, fq, var, to_block, lbl] :=
    unsanitized[fp, fq, var, from_block, lbl],
    *cfg_edge[fp, fq, from_block, to_block, _, _, _],
    not sanitizer_block[fp, fq, var, from_block, lbl]

% Violation: unsanitized path reaches a sink
?[fp, fq, src_var, sink_var, lbl, src_block, sink_block] :=
    unsanitized[fp, fq, sink_var, sink_block, lbl],
    taint_sink[fp, fq, sink_var, sink_block, lbl],
    taint_source[fp, fq, src_var, src_block, lbl]
```

This is strictly more precise than the current approach:

- If `with_for_update()` appears on only one branch, the other
  branch still has an unsanitized path to the sink.
- If the sanitizer dominates the sink (i.e. all paths pass through
  it), `unsanitized` never reaches the sink block.

**`fact_graph.py` — `flow_rule_check_datalog()`:**

Implement the currently-stubbed `through` parameter using the same
pattern: `must_through` means "flag if any path exists that skips
the required point."

### Backwards-incompatible change

Sanitizers in `taint_propagation_datalog()` move from "delete taint
at first match" semantics to "block propagation along specific CFG
edges" semantics.  This is a precision improvement, not a regression,
but results will differ.  The Python fallback path in `taint.py`
should adopt the same semantics by simulating per-block taint state
rather than a single flat dict.

## Phase 4: Type-Conditioned Taint Filtering

**Goal:** Allow taint rules to condition on the inferred type of a
variable, eliminating false positives from scalar-returning queries.

### Changes

**Taint config schema — new optional field on sources/sinks/sanitizers:**

```yaml
sources:
  - pattern: "session.query($MODEL)"
    label: unlocked_read
    type_constraint: "!int & !float & !bool & !str"
    # Only taint if result type is NOT a scalar primitive
```

A `type_constraint` is a boolean expression over type names using
`&` (and), `|` (or), `!` (not), with bare names matched as substrings
of the inferred type string.  This reuses the `TypeOracle`
infrastructure already available via `create_type_oracle()`.

**`taint.py` — source/sink identification:**

After pattern matching identifies a source/sink, and if a
`type_constraint` is present, query the type oracle for the
assignment target's type.  Skip the source/sink if the constraint
is not satisfied.

**`fact_graph.py` — `TypeFact` integration:**

When building taint input relations for Datalog, join against the
`type` relation to filter sources/sinks.  This keeps the filtering
in Datalog rather than requiring a Python round-trip:

```datalog
% Only taint sources whose type is not a scalar
effective_source[fp, fq, var, block, lbl] :=
    taint_source[fp, fq, var, block, lbl],
    *type[var, fp, _, type_str, _],
    not is_scalar(type_str)
```

(Where `is_scalar` is a helper predicate or inline check.)

### Prerequisite

`TypeFact` must be populated during `build_from_project()`.  This is
already partially implemented but needs to be wired to run the type
oracle and store results.

## Phase 5: Scope-Expiry Sanitization

**Goal:** Model taint expiry at transaction/scope boundaries without
bespoke session-tracking logic.

### Changes

**Taint config schema — new `scope_sanitizers` key:**

```yaml
scope_sanitizers:
  - pattern: "session.commit()"
    label: unlocked_read
    # Clears taint on ALL variables with this label, not just
    # variables appearing in the pattern
  - pattern: "session.close()"
    label: unlocked_read
```

Unlike regular sanitizers (which clear taint on variables captured
by the pattern), a scope sanitizer clears *all* variables carrying
the given label.  This models "the transaction ended; all locks are
released; all tainted objects are now detached."

**Datalog encoding:**

```datalog
% A scope sanitizer in block B kills all taint for that label
% in any block reachable only through B
scope_kill[fp, fq, lbl, block] :=
    scope_sanitizer[fp, fq, lbl, block]

% Redefine unsanitized to also respect scope kills
unsanitized[fp, fq, var, to_block, lbl] :=
    unsanitized[fp, fq, var, from_block, lbl],
    *cfg_edge[fp, fq, from_block, to_block, _, _, _],
    not sanitizer_block[fp, fq, var, from_block, lbl],
    not scope_kill[fp, fq, lbl, from_block]
```

This is a natural extension of Phase 3's CFG-based propagation.

## Summary of Changes by File

| File | Phase | Change |
|------|-------|--------|
| `taint.py` | 1 | Augmented assignment extraction + `"mutate"` op kind |
| `taint.py` | 3 | Per-block taint state in Python fallback |
| `taint.py` | 4 | Type oracle integration for source/sink filtering |
| `taint.py` | 5 | Scope sanitizer support |
| `fact_graph.py` | 2 | `kind` field on `DefUseFact` + schema migration |
| `fact_graph.py` | 3 | CFG-edge-based taint propagation + `through` impl |
| `fact_graph.py` | 4 | Type-conditioned taint source filtering |
| `fact_graph.py` | 5 | Scope kill relation |
| `emend_core` (Rust) | 1 | Augmented assignment def+use emission |
| `emend_core` (Rust) | 2 | Def/use kind tagging |
| `type_oracle.py` | 4 | No change (already sufficient) |
| Taint YAML schema | 4, 5 | `type_constraint`, `scope_sanitizers` keys |
| `commands.rst` | all | Updated TOCTOU example with precise config |

## Revised TOCTOU Config (After All Phases)

```yaml
taint:
  labels:
    - unlocked_read

  sources:
    - pattern: "session.query($MODEL)"
      label: unlocked_read
      type_constraint: "!int & !float & !bool & !str & !Sequence"
    - pattern: "session.get($MODEL, $ID)"
      label: unlocked_read
    - pattern: "db.session.query($MODEL)"
      label: unlocked_read
      type_constraint: "!int & !float & !bool & !str & !Sequence"
    - pattern: "db.session.get($MODEL, $ID)"
      label: unlocked_read

  sanitizers:
    - pattern: "$QUERY.with_for_update()"
      label: unlocked_read
    - pattern: "$QUERY.with_for_update($ARGS)"
      label: unlocked_read

  scope_sanitizers:
    - pattern: "session.commit()"
      label: unlocked_read
    - pattern: "session.close()"
      label: unlocked_read
    - pattern: "db.session.commit()"
      label: unlocked_read
    - pattern: "db.session.close()"
      label: unlocked_read

  sinks:
    - pattern: "setattr($OBJ, $ATTR, $VAL)"
      label: unlocked_read
      message: "TOCTOU: setattr() on ORM object loaded without SELECT FOR UPDATE"

  attribute_mutation_sinks:
    - label: unlocked_read
      message: >-
        TOCTOU: attribute mutation on ORM object loaded without
        SELECT FOR UPDATE; use .with_for_update() on the query
```

This config, on the improved engine, would:

- Catch `obj.balance += amount` (Phase 1)
- Ignore `session.query(func.count(...))` when type oracle reports `int` (Phase 4)
- Correctly flag one-branch sanitization in if/else (Phase 3)
- Stop flagging mutations after `session.commit()` (Phase 5)
