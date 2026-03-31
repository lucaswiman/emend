# Phase 1: Remove Public Datalog Surfaces

## Goal

Stop exposing CozoScript / Datalog as a user-facing concept.

The user model should be:

- `find`
- `edit`
- `analyze`
- `check`
- `map`
- `mcp`

Not:

- `query`
- `datalog`
- `cozoscript`

## Why First

This is low risk compared to storage/index changes. It reduces conceptual
surface area immediately and makes the later internal migration less visible to
users.

## Scope

- CLI
- MCP
- docs
- rule/config surface

## Todo

- [x] Remove `emend tool query`.
- [x] Remove any hidden top-level `query` / `datalog` CLI aliases.
- [x] Remove the MCP `datalog` tool.
- [x] Remove raw CozoScript examples from docs.
- [x] Decide whether `datalog` rule kind in `.emend/rules.yaml` should be:
  - ~~removed entirely, or~~
  - retained temporarily as undocumented internal compatibility.
- [x] If retained temporarily, clearly mark it internal-only in code comments.
- [x] Update MCP profiles so there is no public "expert because raw datalog" story.
- [x] Update `grammar_and_cookbook` and README to stop teaching CozoScript.

## Implementation Notes

- `emend tool query` and `emend tool datalog` removed from `cli.py` and `cli_tooling.py`
- MCP `datalog()` raw tool removed; `datalog_query()` renamed to `facts_query()` (guided-only)
- `_CORE_TOOLS` updated: `datalog` → `facts_query`
- `DatalogCheck` retained in `policy.py` as internal-only (not advertised in CLI help)
- `--kind datalog` removed from `check` CLI help text
- README, docs/commands.rst, grammar_and_cookbook.rst updated

## Exit Criteria

- No public CLI command for raw Datalog exists.
- No public MCP tool for raw Datalog exists.
- Docs no longer present Datalog as part of normal emend usage.
