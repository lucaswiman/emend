# Taint Analysis

The intraprocedural and interprocedural taint engine is implemented.  See
`taint.py`, `test_taint.py`, `test_interprocedural_taint.py`, and the
`taint` command in `commands.rst`.

## Completed

- **Field sensitivity** — `obj.field` is now tracked as distinct from `obj`.
  `_extract_qualified_identifiers()` extracts dotted attribute paths;
  `_find_assignments_in_source()` recognises dotted targets (`obj.dirty = x`).
  Parent-to-child taint flows (`obj` tainted → `obj.field` tainted) but sibling
  fields stay independent.

- **High-precision container modeling** — taint propagates through
  `list.append()`, `list.extend()`, `dict[k] = v`, `dict.update()`,
  subscript reads (`x = d[k]`), and `for x in items:` iteration.
  Helpers: `_find_container_mutations()`, `_find_for_loops()`.

- **Framework-specific source/sink/sanitizer rules** — predefined presets
  for Flask, Django, SQLAlchemy, and FastAPI in `taint_presets.py`.  Loaded
  via `emend taint --preset flask` or `presets: [flask]` in YAML config.
  `merge_configs()` composes multiple configs.

## Deferred

The following precision improvement is out of scope for the current engine
and requires dedicated work:

- **Object-sensitive dispatch** — `obj.method()` is resolved by name, not by
  the receiver's concrete type.  Proper resolution would need integration with
  the type oracle to determine the receiver type and dispatch accordingly.
