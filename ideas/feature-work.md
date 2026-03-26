# Feature Ideas for Agentic Coding Workflows

## 1. ✅ Safe Delete (`emend delete`)

**Problem:** `deadcode` finds unreferenced symbols. But removing a symbol creates
cascading effects: its imports become unused, helper functions that only it called become
dead, type aliases only it referenced become orphaned. An agent can do this manually:
delete the symbol, run ruff to clean up imports, run `deadcode` again, delete newly-dead
symbols, repeat. But tracking the transitive closure correctly across 3-4 rounds of
project-wide analysis is exactly the kind of mechanical graph traversal that tools should
handle and agents should not.

**Proposed feature:**
```bash
emend delete models.py::LegacyUser --cascade --dry-run
```
```
Would remove:
  models.py::LegacyUser (the target)
  models.py: import legacy_validator  (now unused)
  validators.py::legacy_validator     (was only called by LegacyUser.__init__)
  validators.py: import legacy_schema (now unused)
  schemas.py::legacy_schema           (was only called by legacy_validator)

5 symbols/imports across 3 files
```

```bash
emend delete models.py::LegacyUser --cascade --apply
emend delete models.py::LegacyUser --apply   # Non-cascading: just the symbol + its unused imports
```

**Implementation:** Remove the symbol. Run unused-import detection on the file. Identify
symbols that were previously referenced only by the deleted code (intersection of the
symbol's callees with `deadcode` results after deletion). Recurse until stable.

**Status:** Implemented as `safe_delete()` in `transform.py` + `emend delete --cascade`
CLI command. Uses BFS over callees, reference index queries to check for remaining
callers, and fixed-point iteration to transitively identify cascade targets.

---

## Crazy Ideas

### Cross-Language Refactoring

Renaming a Python symbol also updates references in Django/Jinja2 templates, YAML configs,
SQL migrations, and OpenAPI specs:

```bash
emend rename models.py::User --to Account --cross-language --apply
# Also updates:
#   templates/profile.html: {{ user.name }} → {{ account.name }}
#   alembic/versions/001.py: table_name = 'user' → 'account'
```

### Behavioral Diff

Given two versions of a function, determine whether they behave differently:

```bash
emend behavioral-diff utils.py::parse_date --against HEAD~1
# "Identical behavior for all tested inputs"
# OR: "Differs for input '2024-02-29' — old returns datetime(2024,2,29), new raises ValueError"
```

Uses property-based test generation (Hypothesis) and optionally symbolic execution
(CrossHair) to find inputs where behavior diverges.

### Daemon Mode / LSP

A persistent daemon that watches the filesystem and maintains an always-current symbol
index, potentially exposed as an LSP server. The "pragmatic middle ground" of a persistent
parse cache (SQLite keyed by file path + mtime) has already been implemented as `parse.db`.
The full daemon/LSP remains a much larger effort.
