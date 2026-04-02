# Phase 17a: Field-Level Taint and Subscript Tracking

## Goal

Enable the Datalog trace engine to track taint at the field/attribute level
and through dictionary subscript operations.

## Why

After removing the legacy Python intraprocedural trace engine in Phase 17, two
limitations remain that prevent full parity with hand-written analysis:

1. The Rust CFG builder splits dotted attributes (`obj.dirty`) into separate
   tokens (`obj`, `dirty`) in the `defs` and `uses` lists.  The Datalog engine
   cannot distinguish `request.safe_field` from `request.user_input`.

2. Dictionary subscript assignment (`data['key'] = value`) and retrieval
   (`q = data['key']`) are not modeled as def-use relationships, so taint
   does not propagate through dict subscript patterns.

Both require changes to the Rust `emend_core` extension, not just the Python
Datalog rules.

## Scope

### Field-level taint (Rust changes required)

- `rust/src/scope.rs` — emit qualified identifiers (e.g. `obj.field`) as
  single tokens in `defs`/`uses` lists, preserving the dotted form
- `src/emend/fact_graph.py` — verify `DefUseFact` correctly stores qualified
  names once the Rust backend emits them
- The Datalog `tainted` and `unsanitized` rules already propagate per-variable;
  no rule changes expected once the facts are correct

### Dict subscript tracking (new fact model)

- `rust/src/scope.rs` — detect subscript assignment/access patterns
  (`container[key] = value`, `x = container[key]`) and emit them as def-use
  or a new dedicated fact type
- `src/emend/fact_graph.py` — add `SubscriptFact` or extend `DefUseFact` with
  a `key` field to model container-key relationships
- `src/emend/fact_graph.py` — add Datalog propagation rules for subscript
  taint flow

## Todo

- [ ] Extend Rust scope resolver to emit qualified identifiers in def/use lists
- [ ] Verify field-level taint propagation works end-to-end
- [ ] Remove `xfail` from `test_trace_field_sensitivity_distinct_fields`
- [ ] Add subscript tracking to Rust scope resolver
- [ ] Add subscript fact model and Datalog rules
- [ ] Remove `xfail` from `test_trace_container_dict_subscript`
- [ ] Run full test suite

## Exit Criteria

- `obj.dirty` and `obj.clean` are tracked as distinct taint targets
- `data['key'] = tainted; q = data['key']` propagates taint to `q`
- Both xfail markers removed and tests pass
