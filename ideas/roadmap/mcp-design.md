# MCP Design

## Recommendation

Keep high-level tools first. Treat raw query execution as a later expert mode.

## Good MCP Primitives

- `search`
- `replace`
- `rename`
- `impact`
- `taint`
- `refs`
- `graph`

These are easy for agents to learn and produce bounded, structured outputs.

## Expert Mode

After the relation schema stabilizes, add an expert query tool:

- `emend_query`

It can expose Datalog or egglog syntax, but this should be additive, not the
primary interface.

## Why Not Lead With Datalog

- most agent work benefits more from structured domain tools than open-ended query languages
- provenance and bounded outputs matter more than expressiveness alone
- a raw query interface is harder to validate, secure, and document well

## When It Becomes Worthwhile

An expert query interface becomes compelling once:

- impact and taint facts already exist
- the relation schema is stable
- provenance/witness output is part of the protocol
- there are real examples that require composable ad hoc querying

## Imperative Layer

Even with a query layer, side effects should remain explicit:

- apply changes
- write files
- run tests

The query layer should compute candidate actions; imperative tools should
perform them.
