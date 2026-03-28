# Query Language For Code Invariants

Both query surfaces are implemented:

- **Option A (YAML rule extensions):** `flows-from`, `flows-to`, `not-through`
  in `.emend/patterns.yaml` lint rules.  See `linting.rst`.
- **Option C (Expert Datalog):** `emend query` runs CozoScript against the
  CozoDB-backed fact graph.  See `commands.rst`.

## Ongoing: Witness Quality

Every query result should return:

- a witness path or structural explanation
- source locations
- the rule/predicate that matched

This is satisfied for taint and policy checks today.  Future analyses should
follow the same contract.
