# Phase 3 — Single-source CLI registration

## Why

Today every command is registered twice. Example for `replace`:

1. `src/emend/cli_edit.py:270` — `@app.command("replace", hidden=True)`
   on the function definition.
2. `src/emend/cli.py:36–80` — imports the function and re-registers it
   under the `edit` subapp via `edit_app.command("replace")(replace_cmd)`,
   plus several hidden top-level aliases.

Consequences:

- Renaming `replace_cmd` requires touching both files.
- Adding a new alias requires touching both files.
- The "where is this command registered?" question has two answers.
- `cli.py` re-exports a grab-bag of helpers (`parse_where_clause`,
  `resolve_files`, `QueryShape`, `_reject_file_glob`, `detect_query_shape`,
  `resolve_file_scopes`, `resolve_many_files`, `search`) for "backward
  compatibility" with no clear consumer.

## Target layout

`cli.py` becomes the **only** file that calls `app.command(...)`,
`edit_app.command(...)`, `analyze_app.command(...)`, `tool_app.command(...)`,
or `map_app.command(...)`.

`cli_*.py` modules export plain functions only:

```python
# cli_edit.py — before
@app.command("replace", hidden=True)
def replace_cmd(...): ...

# cli_edit.py — after
def replace_cmd(...): ...
```

`cli.py` holds a single registration table:

```python
COMMANDS = [
    # (subapp, name, function, hidden, aliases)
    (edit_app,    "set",       edit_set_cmd, False, []),
    (edit_app,    "replace",   replace_cmd,  False, []),
    (edit_app,    "rm",        remove_cmd,   False, ["delete"]),
    (analyze_app, "refs",      refs_cmd,     False, ["references"]),
    (analyze_app, "graph",     graph_cmd,    False, []),
    (analyze_app, "deadcode",  dead_code_cmd,False, ["dead-code", "dead_code"]),
    # ... etc
    (app,         "find",      search,       False, ["grep", "search", "show", "get", "lookup", "ls"]),
    (app,         "lint",      lint_cmd,     False, []),
    # hidden top-level aliases for the edit/analyze namespaces
    (app,         "replace",   replace_cmd,  True,  []),
    (app,         "rm",        remove_cmd,   True,  ["remove"]),
    # ...
]

for subapp, name, fn, hidden, aliases in COMMANDS:
    subapp.command(name, hidden=hidden)(fn)
    for alias in aliases:
        subapp.command(alias, hidden=True)(fn)
```

This makes the full command surface visible in one place, in one
data structure, sortable / greppable.

## Drop the cruft re-exports

`cli.py`'s `__all__` currently contains:

```python
__all__ = [
    "QueryShape", "_reject_file_glob", "app", "detect_query_shape",
    "main", "parse_where_clause", "resolve_file_scopes", "resolve_files",
    "resolve_many_files", "search",
]
```

Audit each. As of this writing:

| Symbol | Used outside `emend.cli` package? | Action |
|---|---|---|
| `app` | Yes (test entry) | Keep |
| `main` | Yes (entry point) | Keep |
| `search` | Tests import from `emend.cli` | Keep, or update test imports |
| `QueryShape`, `detect_query_shape` | Internal | Drop from `cli.py`; importers go to `cli_base` |
| `parse_where_clause` | Internal | Drop |
| `resolve_files`, `resolve_many_files`, `resolve_file_scopes` | Mixed | Move to `cli_base` and update importers |
| `_reject_file_glob` | Internal (leading underscore!) | Drop unconditionally |

Run `grep -rn "from emend.cli import" src/ tests/` and `grep -rn
"emend.cli\." src/ tests/` first to make decisions data-driven.

## Execution plan

1. **Move all `@app.command` decorators out of `cli_*.py`** in one commit.
   Functions become plain. Tests will fail until step 2.
2. **Build the registration table in `cli.py`**. Tests pass again.
3. **Verify command count and help text.** Run `emend --help`,
   `emend edit --help`, `emend analyze --help`, `emend tool --help`,
   `emend map --help`. The visible commands and their descriptions must
   be identical to before. Use the `test_cli_surface_consolidation.py`
   suite as ground truth.
4. **Drop unused re-exports** from `cli.py` `__all__`. One commit per
   removed symbol so each can be reverted if it turns out to have an
   external consumer.

## Acceptance criteria

- [ ] Every `@app.command` / `@edit_app.command` / `@analyze_app.command`
      / `@tool_app.command` / `@map_app.command` decorator lives in
      `cli.py` (verified via `grep -rn "@\(app\|edit_app\|analyze_app\|tool_app\|map_app\)\.command" src/emend/`).
- [ ] `emend --help`, `emend edit --help`, `emend analyze --help`,
      `emend tool --help`, `emend map --help` are byte-identical to
      pre-refactor output.
- [ ] `test_cli_surface_consolidation.py` passes.
- [ ] `cli.py` `__all__` contains only verified-external symbols.

## Caveats

- Some commands have non-default Typer kwargs at the decorator (e.g.
  `app.command("rm", hidden=True, no_args_is_help=True)`). The
  registration table needs to support per-command kwargs — extend the
  tuple to a dataclass, or use `functools.partial`.
- `@app.callback` (`_app_callback` in `cli_base.py:259`) is a different
  beast — it's the top-level callback, not a command. Leave it alone.

## Estimated diff size

- ~150 lines deleted from `cli_*.py` (decorator removals).
- ~120 lines added to `cli.py` (registration table).
- ~30 lines deleted from `cli.py` `__all__` and the import block.
- Net: ~−60 lines and one source of truth.
