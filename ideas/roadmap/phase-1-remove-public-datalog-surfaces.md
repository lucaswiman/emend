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

- [ ] Remove `emend tool query`.
- [ ] Remove any hidden top-level `query` / `datalog` CLI aliases.
- [ ] Remove the MCP `datalog` tool.
- [ ] Remove raw CozoScript examples from docs.
- [ ] Decide whether `datalog` rule kind in `.emend/rules.yaml` should be:
  - removed entirely, or
  - retained temporarily as undocumented internal compatibility.
- [ ] If retained temporarily, clearly mark it internal-only in code comments.
- [ ] Update MCP profiles so there is no public "expert because raw datalog" story.
- [ ] Update `grammar_and_cookbook` and README to stop teaching CozoScript.

## Exit Criteria

- No public CLI command for raw Datalog exists.
- No public MCP tool for raw Datalog exists.
- Docs no longer present Datalog as part of normal emend usage.
