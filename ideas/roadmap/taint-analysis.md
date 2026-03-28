# Taint Analysis

The intraprocedural and interprocedural taint engine is implemented.  See
`taint.py`, `test_taint.py`, `test_interprocedural_taint.py`, and the
`taint` command in `commands.rst`.

## Deferred

The following precision improvements are out of scope for the current engine
and require dedicated work:

- **Field sensitivity** — currently treats `obj.field` as the whole object
- **Object-sensitive dispatch** — `obj.method()` is resolved by name, not by
  the receiver's concrete type
- **High-precision container modeling** — list/dict element taint is coarse
- **Aggressive framework-specific modeling** — Django, Flask, SQLAlchemy etc.
  need hand-written source/sink/sanitizer rules for good recall
