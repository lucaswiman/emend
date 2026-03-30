# Querying / Rules / MCP Rewrite

## Goal

Make emend easier to use by reducing the number of concepts the user has to
learn.

The practical goals are:

1. Search should feel like `grep`/`rg`: pattern first, files after, flags for
   refinement.
2. Patterns and selectors should remain the two main user-facing mini-languages.
3. We should document one canonical syntax, but be lenient whenever the user's
   intent is unambiguous.
4. The lint / trace / policy / deadcode YAML formats should become one unified
   rule format.
5. The MCP surface should be smaller, more regular, and more model-friendly.


## Diagnosis

emend currently has good primitives, but too many overlapping surfaces:

- **Patterns** are good.
- **Selectors** are good.
- **Search UX** is clever, but too inference-heavy.
- **Lint / trace / policy / deadcode config** are overlapping schemas.
- **MCP** exposes too many multi-mode tools with large schemas.

The problem is not that emend lacks expressive power. The problem is that the
same ideas are expressed in too many ways.


## Design Principles

### 1. Preserve the good parts

Keep these:

- **Pattern syntax** for code-shaped matching:
  - `print($X)`
  - `$OBJ.method($...ARGS)`
  - `def $F($...ARGS):`
- **Selector syntax** for location-shaped addressing:
  - `file.py::func`
  - `file.py::Class.method`
  - `file.py::func[params][0]`

These are the two user-facing syntaxes worth preserving.

### 2. Keep patterns opaque

Patterns should stay code-like strings. We should not keep growing a textual
meta-language around them. This avoids keyword collisions like `where`,
`inside`, `type`, etc. appearing both as DSL keywords and as valid code
identifiers.

This means:

- predicates should live outside the pattern string
- file scope should live outside the pattern string in the canonical syntax
- rule metadata should live outside the pattern string

### 3. One canonical syntax, forgiving parser

The user-facing guidance should be:

> Document one syntax. Be lenient when human intent is unambiguous.

That means:

- the docs should teach one primary way to do things
- the implementation may accept shorthands and old forms
- shorthands should desugar to the canonical representation internally

Examples:

- Canonical:
  - `emend find 'print($X)' src/**/*.py`
- Accepted sugar:
  - `emend find 'src/**/*.py::print($X)'`
  - `emend search 'print($X)' 'src/**/*.py'`

We should optimize the docs for clarity, not for enumerating every accepted
shorthand.


## Search UX

### Canonical CLI Shape

The primary search form should be:

```bash
emend find [FLAGS] QUERY [FILES...]
```

Where:

- `QUERY` is a pattern or selector
- `FILES...` are file paths, directories, or globs
- flags refine the search

Examples:

```bash
emend find 'print($X)' src/**/*.py
emend find 'print($X)' 'src/**/*.py'
emend find --within 'def $F' 'print($X)' src/**/*.py
emend find path/to/file.py::Class.method
emend find --kind function --has-param session src/**/*.py
```

This should be the documented mental model:

- `find X Y` means "find X in Y"
- selectors are already location-bearing and may not need extra files
- omitted files means project/default scope

### Why this is better

This avoids the awkward `in` / `inside` pairing:

- file scope should be positional on the CLI: `QUERY [FILES...]`
- AST containment should use explicit structural flags like `--within`

So instead of:

```bash
emend find "print($X)" --in "src/**/*.py" --inside "def $F"
```

prefer:

```bash
emend find --within "def $F" "print($X)" "src/**/*.py"
```

### Suggested Search Flags

The canonical flags should be explicit and shallow:

- `--within PATTERN`
- `--not-within PATTERN`
- `--kind KIND[,KIND...]`
- `--has-param NAME`
- `--has-arg NAME`
- `--has-kwarg NAME`
- `--imported-from MODULE`
- `--scope-local`
- `--output ...`

These are intentionally not a separate predicate language. They are just named
operators around an opaque pattern or selector.

### Selector Guidance

Selector syntax should stay as-is:

```text
path/to/file.py::Symbol.method
```

This is easy to remember, similar to `pytest`, and works well because it joins
two naturally related things:

- file scope
- symbol path

The only caution is that `::` should not be the primary documented syntax for
pattern searches. It is acceptable sugar, not the main teaching surface.

### Pattern Guidance

Patterns should stay code-shaped and example-driven. They should not become the
place where we encode:

- file scope
- metadata
- rule severity
- generic predicates

If we improve patterns, it should be by making pattern-local constructs clearer,
not by turning them into a general query language.


## Canonical vs Accepted Search Forms

### Canonical

```bash
emend find 'print($X)' src/**/*.py
emend find --within 'def $F' 'print($X)' src/**/*.py
emend find path/to/file.py::Class.method
```

### Accepted but not primary

```bash
emend find 'src/**/*.py::print($X)'
emend grep 'print($X)' src/**/*.py
emend search 'print($X)' src/**/*.py
```

### Guidance

Only document the canonical forms prominently. Everything else is compatibility
or convenience syntax.


## Unified Rules Format

The existing YAML/config story should be collapsed into one format.

Today we have separate encodings for:

- lint pattern rules
- flow rules
- trace config
- deadcode config
- policy checks

These are all really rules with metadata plus one kind of matcher.

### Proposed File

```text
.emend/rules.yaml
```

### Top-Level Shape

```yaml
macros:
  user_input: "request.args.get($X) | request.form[$X]"

presets:
  - flask
  - sqlalchemy

rules:
  no-print:
    match: "print($X)"
    not-within: "def test_$_"
    files: ["src/**/*.py"]
    fix: "logger.info($X)"
    severity: warning
    message: "Use logger instead of print"

  sql-injection:
    flow:
      from: "{user_input}"
      to: "cursor.execute($Q)"
      not-through: "escape($X)"
      quantifier: all_paths
    files: ["src/**/*.py"]
    interprocedural: true
    severity: error
    message: "Unsanitized input reaches SQL execution"

  unused-functions:
    deadcode:
      kind: function
      entry-points:
        decorators: ["app.route", "celery.task"]
        names: ["main", "cli"]
      exclude-paths: ["tests/**", "migrations/**"]
    severity: warning
    message: "Function appears unused"

  return-types:
    type-check:
      selector: "src/**/*.py::*"
      kind: returns
      expected: "str | int | None"
    severity: info
    message: "Unexpected return type"

  recursive-calls:
    datalog: |
      ?[name, file, line] :=
        *call[caller, callee, file, line, _, _, _],
        caller == callee,
        *symbol[callee, _, name, _, _, _, _]
    severity: warning
    message: "Recursive function detected"
```

### Rule Kinds

Each rule has the same envelope:

- `severity`
- `message`
- optional `files`

And exactly one payload:

- `match:`
- `flow:`
- `deadcode:`
- `type-check:`
- `datalog:`

This replaces the separate schemas in `patterns.yaml` and `policies.yaml`.

### Structural Rule

```yaml
rules:
  no-print:
    match: "print($X)"
    within: "def $F"
    not-within: "class Test*"
    files: ["src/**/*.py"]
    fix: "logger.info($X)"
    severity: warning
    message: "Use logger"
```

### Flow Rule

```yaml
rules:
  sql-injection:
    flow:
      from: "request.args.get($X)"
      to: "cursor.execute($Q)"
      not-through: "escape($X)"
      quantifier: all_paths
    files: ["src/**/*.py"]
    severity: error
    message: "Unsanitized input reaches SQL execution"
```

Support for:

- `effect` sinks
- scope boundaries / scope sanitizers
- presets
- interprocedural analysis

should remain, but under this one rule kind.

### Dead Code Rule

```yaml
rules:
  unused-functions:
    deadcode:
      kind: function
      include-private: false
      entry-points:
        decorators: ["app.route"]
        names: ["main"]
      exclude-paths: ["tests/**"]
    severity: warning
    message: "Function appears unused"
```

### Type Rule

```yaml
rules:
  db-connections-only:
    type-check:
      selector: "src/**/*.py::*"
      kind: has_type
      expected: "Connection"
    severity: error
    message: "Expected Connection-typed symbol"
```

### Datalog Escape Hatch

```yaml
rules:
  recursive-calls:
    datalog: |
      ?[file_path, line, message] :=
        *call[caller, caller, file_path, line, _, _, _],
        message = "Recursive function detected"
    severity: warning
    message: "Recursive function detected"
```

This is the expert escape hatch. It should exist, but it should not define the
common UX.


## Commands

### Primary Search Command

Document:

```bash
emend find [FLAGS] QUERY [FILES...]
```

Keep aliases like `grep` and `search` for compatibility.

### Replace

```bash
emend replace [FLAGS] PATTERN REPLACEMENT [FILES...]
```

Examples:

```bash
emend replace 'print($X)' 'logger.info($X)' src/**/*.py --apply
emend replace --within 'def $F' 'assertEqual($A, $B)' 'assert $A == $B' tests/**/*.py --apply
```

### Check

Introduce:

```bash
emend check [PATHS...]
emend check --rule sql-injection
emend check --kind flow
```

`check` becomes the main entry point for unified rules.

Keep `lint` as a compatibility alias for a while, but the conceptual model
should become:

- `find` for ad hoc querying
- `replace` / edit commands for changes
- `check` for reusable project rules

### Flow as an Ad Hoc Command

If kept, make it explicit and simple:

```bash
emend flow --from 'request.args.get($X)' --to 'cursor.execute($Q)' src/**/*.py
emend flow --from 'request.args.get($X)' --to 'cursor.execute($Q)' \
  --not-through 'escape($X)' \
  --quantifier all_paths \
  src/**/*.py
```

This is easier to explain than requiring the user to write trace config for
one-off analyses.


## MCP Rewrite

### MCP Goals

The MCP should optimize for:

- low schema entropy
- explicit discriminators
- minimal inference
- small number of tools
- regular parameter naming

The current issue is not just tool count. It is that some tools are large
multi-mode surfaces with implicit behavior.

For MCP, the cost model matters:

- every tool schema costs tokens
- every repeated description costs tokens
- every ambiguous tool increases reasoning burden

So we do **not** want many tiny tools, and we do **not** want giant
auto-detecting tools. We want a few tools with clear tagged-union schemas.

### Proposed MCP Surface

Keep a small set of tools:

1. `search`
2. `transform`
3. `references`
4. `analyze`
5. `check`
6. `datalog`
7. `grammar_and_cookbook`

Keep mapping as a product feature, but do not include mapping tools in the
smallest default MCP surface. They support user-configured cross-repo
navigation and editor/plugin workflows, but are still specialized compared to
core refactoring operations.

Also drop separate MCP entry points for policy-vs-lint-vs-trace distinctions.
Those should become modes inside `check` / `analyze`, or disappear behind the
unified rules model.

### 1. `search`

This should cover ad hoc querying with an explicit mode discriminator.

```json
{
  "mode": "code" | "symbol" | "summary",
  "query": "print($X)",
  "files": ["src/**/*.py"],
  "within": "def $F",
  "not_within": "class Test*",
  "kind": ["function"],
  "has_param": "session",
  "has_arg": "session",
  "imported_from": "requests",
  "output": "locations"
}
```

Notes:

- `mode` is required
- no auto-detecting selector-vs-pattern behavior in MCP
- `files` should be explicit and structured
- `where` should not exist in MCP

### 2. `transform`

One tool with a clear operation discriminator:

```json
{
  "operation": "replace" | "edit" | "add" | "remove" | "rename" | "move",
  "selector": "file.py::func[returns]",
  "pattern": "print($X)",
  "replacement": "logger.info($X)",
  "value": "User | None",
  "destination": "other.py",
  "apply": false
}
```

This is a good union because:

- operations are explicit
- write operations share `apply`
- transform operations are conceptually grouped

We should keep this narrower than the current CLI surface and remove odd
combinations.

### 3. `references`

```json
{
  "selector": "file.py::func",
  "kind": "all" | "reads" | "writes" | "calls",
  "exclude_definition": false,
  "exclude_imports": false,
  "project": "."
}
```

This is simple and focused.

### 4. `analyze`

One analysis tool with explicit mode:

```json
{
  "mode": "graph" | "impact" | "semantic_context" | "deadcode" | "flow",
  "selector": "file.py::func",
  "files": ["src/**/*.py"],
  "from_pattern": "request.args.get($X)",
  "to_pattern": "cursor.execute($Q)",
  "not_through": "escape($X)",
  "transitive": true,
  "depth": 3,
  "interprocedural": false
}
```

This replaces separate MCP tools for:

- graph
- impact
- semantic_context
- deadcode
- trace_analysis

This works because these are all read-only analysis surfaces. The discriminator
must be explicit.

### 5. `check`

Unified project rule runner:

```json
{
  "paths": ["src/**/*.py"],
  "config": ".emend/rules.yaml",
  "rule": "sql-injection",
  "kind": "match" | "flow" | "deadcode" | "type" | "datalog",
  "fix": false
}
```

This replaces separate MCP tools for lint and policy checks.

### 6. `datalog`

Raw expert-mode access:

```json
{
  "query": "?[name, file] := *symbol[qn, file, name, kind, _, _, _], kind == 'function'",
  "project": ".",
  "limit": 200
}
```

No guided mode. Guided mode bloats the schema while duplicating higher-level
tools.

### 7. `grammar_and_cookbook`

Keep this. It is useful as a retrieval/documentation tool.


## MCP Parameter Conventions

Use the same names everywhere:

- `files`
- `within`
- `not_within`
- `selector`
- `pattern`
- `replacement`
- `apply`
- `project`

Avoid:

- `where`
- ad hoc aliases
- positional-overloaded meanings


## Functionality to Remove from the Default MCP Profile

These are candidates to remove from the default MCP profile:

- separate policy tool
- separate lint tool
- guided fact-query mode
- multiple ways to express the same graph/query analysis

Rationale:

- they are niche
- they bloat schema
- they are less likely to be chosen correctly by an agent
- they can remain available in extended/expert MCP profiles or in the CLI

### Mapping

Mapping should remain part of emend. It is useful user configuration for
cross-repo navigation and editor integration, especially in the Vim plugin.

Recommendation:

- keep mapping commands and storage in the product
- keep mapping MCP tools available in an extended/expert profile
- exclude mapping from the smallest default MCP profile


## Migration Plan

### Phase 1: Canonical syntax and compatibility

- Document `emend find [flags] QUERY [FILES...]` as the primary search syntax.
- Keep accepting current shorthand forms where intent is unambiguous.
- Add `--within` / `--not-within` and prefer them in docs over `--where`.

### Phase 2: Unified rules

- Introduce `.emend/rules.yaml`.
- Implement a loader that dispatches to the existing engines.
- Support migration from:
  - `.emend/patterns.yaml`
  - `.emend/policies.yaml`
- Add `emend check`.

### Phase 3: MCP cleanup

- Collapse MCP into the smaller set of discriminated tools above.
- Remove inference-heavy fields from MCP.
- Drop low-value MCP surfaces.

### Phase 4: Deprecation

- Deprecate separate trace/policy config formats.
- Deprecate `where` as the primary documented search refinement.
- Keep compatibility aliases, but stop teaching them.


## Final Recommendation

Do not invent another general-purpose textual query language.

Instead:

- keep **patterns**
- keep **selectors**
- make the CLI **grep-like**
- document **one canonical syntax**
- accept lenient sugar when intent is clear
- unify all YAML/config surfaces into **one rules format**
- make MCP smaller, explicit, and schema-efficient

That gives emend a much smaller concept budget without giving up power.
