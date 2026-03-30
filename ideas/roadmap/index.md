# Roadmap

**When you complete a task, check off its checkbox.**

---

## Taint-CFG Precision: Effects, Path Quantifiers, and Temporal Sequences

Spec: [taint-cfg-precision.md](taint-cfg-precision.md)

Five general mechanisms that increase the expressive power of the
taint/CFG/Datalog stack.  Each is independently useful; together they
replace `attribute_mutation_sinks` and other bespoke features.

Phases 1–4 (effect predicates, path-sensitive sanitization, scope
boundaries, type-conditioned filtering) are complete.

### Phase 5: Temporal Sequence Patterns

- [ ] Update TOCTOU example in `commands.rst` with sequence rule form

### Phase 6: Naming Unification and Cleanup — `taint` → `trace`

Rename the "taint" abstraction to **"trace"** across the codebase.  The
engine is a general labeled data-flow tracer — sources emit labeled
values, sinks consume them, sanitizers block propagation — and "taint"
is just one framing (security).  The same engine powers lint flow rules,
policy flow checks, sequence patterns, and effect predicates.  "Trace"
better describes the core action: *follow labeled values through code*.

#### CLI and command renaming

- [x] Rename `emend taint` → `emend trace` (keep `taint` as hidden alias for backwards compat)
- [x] Rename `--interprocedural` flag (already applies to `trace`)
- [x] Rename `emend facts --type taint_flows` → `emend facts --type trace_flows` (alias old name)
- [x] Update `--preset` help text: "framework-specific trace rules" instead of "taint rules"
- [ ] Update TOCTOU example in `commands.rst` with sequence rule form (existing Phase 5 TODO)

#### Config and YAML renaming

- [x] Rename `taint:` section in `.emend/patterns.yaml` → `trace:` (accept both, prefer new)
- [x] Rename `TaintSource` → `TraceSource`, `TaintSink` → `TraceSink`, `TaintSanitizer` → `TraceSanitizer`, `TaintScopeSanitizer` → `TraceScopeSanitizer` in config model
- [x] Rename `TaintConfig` → `TraceConfig`, `TaintViolation` → `TraceViolation`
- [x] Keep YAML key aliases: `sources`/`sinks`/`sanitizers` stay the same (only the section name changes)

#### Source file renaming

- [x] Rename `taint.py` → `trace.py` (update all imports)
- [x] Rename `taint_presets.py` → `trace_presets.py`
- [x] Rename `TaintFlowFact` → `TraceFlowFact` in `fact_graph.py`
- [x] Rename Datalog relations: `taint_flow` → `trace_flow` in CozoDB schema
- [x] Rename test files: `test_taint.py` → `test_trace.py`, `test_interprocedural_taint.py` → `test_interprocedural_trace.py`, etc.

#### Object-sensitive dispatch

Resolve `obj.method()` by receiver type using the `type_binding` relation
(Phase 4) and `type_constraint` field already on sources/sinks/sanitizers.

- [x] Join `method_call` with `type_binding` in Datalog to resolve receiver types
- [x] Filter source/sink/sanitizer pattern matches by receiver `type_constraint`
- [x] Add `receiver_type` parameter to `MethodCallFact` (or resolve via join at query time)

#### Simplification from code review

- [x] Extract `_inline_relation(name, cols, rows)` helper for CozoScript query building (used in `trace_propagation_datalog`, `flow_rule_check_datalog`, `_compile_sequence_query`)
- [x] Introduce `TraceDatalogConfig` dataclass to reduce `trace_propagation_datalog()` from 9 params to 3 (`sources`, `sinks`, `config`)
- [x] Deduplicate blocker resolution loops in `compile_sequence_rule()` (`not_through` / `not_through_scope` share 55 identical lines)
- [x] Cache `evaluate_type_constraint()` parsed constraint expressions (currently re-parses on every call)

#### Documentation

- [x] Update CLAUDE.md file/command tables
- [x] Update `commands.rst` references
- [x] Add migration note in CHANGELOG explaining `taint` → `trace` rename

---

## Reference Documents

- [taint-cfg-precision.md](taint-cfg-precision.md) — taint-CFG precision: mutation tracking, path sensitivity, type filtering
- [query-language-for-code-invariants.md](query-language-for-code-invariants.md) — ongoing witness-quality requirement
- [relation-to-existing-tools.md](relation-to-existing-tools.md) — positioning vs Semgrep, CodeQL, Pysa
- [open-questions.md](open-questions.md) — ongoing design trade-offs
