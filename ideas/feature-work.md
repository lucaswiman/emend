# Feature Ideas for Agentic Coding Workflows

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
