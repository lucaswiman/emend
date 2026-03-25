# Query Language For Code Invariants

## Goal

Support structural and flow-oriented invariant checks without forcing users to
learn a heavy query language on day one.

## Recommendation

Start with the smallest useful surface:

- extend YAML lint rules with `flows-from`, `flows-to`, and `not-through`
- support structural predicates like `not-inside`
- keep the syntax close to existing emend patterns

## Why

This covers the majority of likely use cases:

- user input reaches SQL execution
- a labeled value reaches logging
- a call occurs outside a required context manager
- an endpoint with a certain return type lacks a decorator

## Surface Options

### Option A: YAML rule extensions

Best near-term choice. Minimal syntax and natural fit for current lint config.

### Option B: Expert-mode query DSL

Possible later if YAML becomes too awkward, but should compile to the same
underlying fact model.

### Option C: Raw Datalog / egglog

Best treated as an expert interface, not the primary user-facing surface.

## Non-Goals

- designing a second standalone language before the engine stabilizes
- making all users learn Datalog
- binding the entire roadmap to egglog syntax choices

## Key Requirement

Every query result should be able to return:

- a witness path or structural explanation
- source locations
- the rule/predicate that matched

Without that, the query layer will be much less useful to humans and agents.
