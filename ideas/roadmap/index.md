# Roadmap

**When you complete a task, check off its checkbox.**

---

## Taint Precision Improvements

Spec: [taint-analysis.md](taint-analysis.md)

- [ ] Object-sensitive dispatch — resolve `obj.method()` by receiver type, not just name

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

- [ ] Rename `emend taint` → `emend trace` (keep `taint` as hidden alias for backwards compat)
- [ ] Rename `--interprocedural` flag (already applies to `trace`)
- [ ] Rename `emend facts --type taint_flows` → `emend facts --type trace_flows` (alias old name)
- [ ] Update `--preset` help text: "framework-specific trace rules" instead of "taint rules"
- [ ] Update TOCTOU example in `commands.rst` with sequence rule form (existing Phase 5 TODO)

#### Config and YAML renaming

- [ ] Rename `taint:` section in `.emend/patterns.yaml` → `trace:` (accept both, prefer new)
- [ ] Rename `TaintSource` → `TraceSource`, `TaintSink` → `TraceSink`, `TaintSanitizer` → `TraceSanitizer`, `TaintScopeSanitizer` → `TraceScopeSanitizer` in config model
- [ ] Rename `TaintConfig` → `TraceConfig`, `TaintViolation` → `TraceViolation`
- [ ] Keep YAML key aliases: `sources`/`sinks`/`sanitizers` stay the same (only the section name changes)

#### Source file renaming

- [ ] Rename `taint.py` → `trace.py` (update all imports)
- [ ] Rename `taint_presets.py` → `trace_presets.py`
- [ ] Rename `TaintFlowFact` → `TraceFlowFact` in `fact_graph.py`
- [ ] Rename Datalog relations: `taint_flow` → `trace_flow` in CozoDB schema
- [ ] Rename test files: `test_taint.py` → `test_trace.py`, `test_interprocedural_taint.py` → `test_interprocedural_trace.py`, etc.

#### Simplification from code review

- [ ] Extract `_inline_relation(name, cols, rows)` helper for CozoScript query building (used in `taint_propagation_datalog`, `flow_rule_check_datalog`, `_compile_sequence_query`)
- [ ] Introduce `TraceDatalogConfig` dataclass to reduce `taint_propagation_datalog()` from 9 params to 3 (`sources`, `sinks`, `config`)
- [ ] Deduplicate blocker resolution loops in `compile_sequence_rule()` (`not_through` / `not_through_scope` share 55 identical lines)
- [ ] Cache `evaluate_type_constraint()` parsed constraint expressions (currently re-parses on every call)

#### Documentation

- [ ] Update CLAUDE.md file/command tables
- [ ] Update `commands.rst` references
- [ ] Add migration note in CHANGELOG explaining `taint` → `trace` rename with alias support

---

## Reference Documents

- [taint-cfg-precision.md](taint-cfg-precision.md) — taint-CFG precision: mutation tracking, path sensitivity, type filtering
- [taint-analysis.md](taint-analysis.md) — deferred taint precision work
- [query-language-for-code-invariants.md](query-language-for-code-invariants.md) — ongoing witness-quality requirement
- [relation-to-existing-tools.md](relation-to-existing-tools.md) — positioning vs Semgrep, CodeQL, Pysa
- [open-questions.md](open-questions.md) — ongoing design trade-offs
