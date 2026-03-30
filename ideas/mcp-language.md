# emend MCP Server — Query Language Reference

## Overview

The emend MCP server exposes code analysis and transformation as MCP tools.
Each tool is named `emend_<verb>` (e.g. `emend_find`, `emend_replace`).
Tool parameters are flat JSON objects.

**How tool calls work:** Each tool is invoked by its MCP tool name
(e.g. `emend_find`). The JSON examples in this document show the
**parameter objects** passed to each tool. Inside `emend_batch`, child
operations use a `"type"` field to select which operation to run
(e.g. `"type": "find"`).

### Conventions

- **`files`** parameters accept Unix glob syntax. `**` matches zero or more
  directory levels (e.g. `"src/**/*.py"`).
- **`symbol`** parameters accept dotted qualified names (e.g. `"MyClass.method"`).
  If ambiguous, results for all matches are returned.
- **`inside`** and **`not_inside`** parameters accept the same pattern syntax
  as `pattern` (with `$METAVAR` captures), not glob wildcards.
- All responses are JSON objects. On error: `{"error": "message"}`.
- `emend_replace` is the only tool that modifies code. It defaults to dry-run
  mode (`"apply": false`). All other tools are read-only.

## Pattern Syntax (Quick Reference)

Patterns are code-shaped templates with metavariable captures:

| Syntax | Meaning | Example |
|--------|---------|---------|
| `$X` | Capture any single AST node | `print($X)` |
| `$_` | Wildcard (match but don't capture) | `$_.method()` |
| `$...ARGS` | Capture zero or more nodes | `func($...ARGS)` |
| `$X:identifier` | AST node kind constraint | `$X:identifier` (name nodes only) |
| `$X:!int` | Negated AST kind constraint | `$X:!int` (anything except int literals) |
| `$X:type[T]` | Inferred type constraint (oracle) | `$X:type[Connection]` |
| `$F:returns[T]` | Return type constraint (oracle) | `$F:returns[str]` |

AST node kinds for simple constraints: `expr`, `stmt`, `identifier`, `int`,
`str`, `float`, `call`, `attr`, `any`.

**Note:** `$X:identifier` constrains by AST node kind (syntax).
`$X:type[Connection]` constrains by inferred type (semantics, requires a type
engine). The bracket `[T]` is the disambiguator.

## Tools

### `emend_find` — Structural Pattern Search

Find code matching a pattern. Returns matches with file, line, and captured
metavariable bindings.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `pattern` | string | yes | Code pattern with `$METAVAR` captures |
| `files` | string | no | File glob scope (e.g. `"src/**/*.py"`) |
| `inside` | string | no | Only match inside code matching this pattern (uses pattern syntax, not globs) |
| `not_inside` | string | no | Exclude matches inside code matching this pattern |
| `kind` | string | no | Filter by tree-sitter AST node type. Examples: `call`, `assignment`, `function_definition`, `class_definition`. Accepts any valid tree-sitter node type name. |
| `output` | string | no | One of: `code`, `location`, `selector`, `json`, `count`. Default: `json` |
| `limit` | integer | no | Maximum results. Default: 100 |

**Note on `kind`:** This filters by raw tree-sitter node type (e.g.
`function_definition`), not semantic symbol kind. For symbol-kind filtering,
use `emend_lookup` with its `kind` parameter (`function`, `class`, etc.).

**Examples:**

```json
{
  "type": "find",
  "pattern": "print($X)",
  "files": "src/**/*.py",
  "not_inside": "def test_$_($...ARGS): $...BODY",
  "output": "json"
}
```

```json
{
  "type": "find",
  "pattern": "if $COND:\n    $...BODY",
  "files": "src/**/*.py",
  "inside": "def $FUNC($...ARGS): $...BODY",
  "output": "location"
}
```

---

### `emend_lookup` — Symbol Lookup

Look up a symbol by name and optionally drill into its components.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | yes | Symbol name, optionally dotted (e.g. `"MyClass.method"`) |
| `files` | string | no | File glob or specific file path to restrict search |
| `kind` | string | no | Semantic symbol kind. One of: `function`, `class`, `method`, `variable`, `module` |
| `component` | string | no | Component to extract. One of: `params`, `returns`, `decorators`, `bases`, `body` |
| `index` | integer | no | 0-based index into the component list. Only valid when `component` is set. Negative indices count from end (`-1` = last). |
| `output` | string | no | One of: `code`, `summary`, `metadata`, `json`. Default: `json` |

**Note on `kind`:** This uses semantic symbol kinds (`function`, `class`),
not tree-sitter node types. Compare with `emend_find`'s `kind` which uses
raw AST node types (`function_definition`, `class_definition`).

**Examples:**

```json
{
  "type": "lookup",
  "symbol": "MyClass.method",
  "files": "src/app.py",
  "component": "params"
}
```

```json
{
  "type": "lookup",
  "symbol": "MyClass.method",
  "files": "src/app.py",
  "component": "params",
  "index": 0,
  "output": "json"
}
```

**Returns:** Symbol source code, signature, or component values.

---

### `emend_replace` — Pattern-Based Code Replacement

Replace code matching a pattern with a replacement template. **Dry run by
default** — you must pass `"apply": true` to write changes.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `pattern` | string | yes | Code pattern to match |
| `replacement` | string | yes | Replacement template. `$METAVAR` references are substituted with their captured text. No template logic or conditionals — just literal substitution. |
| `files` | string | no | File glob scope |
| `inside` | string | no | Only replace inside code matching this pattern |
| `not_inside` | string | no | Exclude replacements inside this pattern |
| `apply` | boolean | no | If `true`, write changes to disk. Default: `false` (dry run) |
| `limit` | integer | no | Maximum replacements. Default: unlimited. Recommend using dry run first. |

**Example:**

```json
{
  "type": "replace",
  "pattern": "Union[$X, $Y]",
  "replacement": "$X | $Y",
  "files": "src/**/*.py",
  "apply": true
}
```

**Returns:** List of replacements with file, line, before/after text.
When `apply` is `false`, returns a preview without modifying files.

---

### `emend_refs` — Find References

Find all references to a symbol across the project.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | yes | Qualified symbol name (e.g. `"MyClass.method"`) |
| `ref_kind` | string | no | Filter references by kind. One of: `writes_only`, `reads_only`, `calls_only` |
| `files` | string | no | File glob scope |
| `output` | string | no | One of: `code`, `location`, `json`. Default: `json` |

**Example:**

```json
{
  "type": "refs",
  "symbol": "db.execute",
  "ref_kind": "calls_only"
}
```

**Returns:** List of reference locations with file, line, column, reference kind.

---

### `emend_flow` — Taint/Data Flow Analysis

Trace data flow from source patterns to sink patterns, optionally checking
for sanitizer patterns along the path.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `from_pattern` | string | no | Source pattern — where tainted data originates. Required unless `preset` is provided. |
| `to_pattern` | string | no | Sink pattern — where tainted data must not reach. At least one of `to_pattern` or `effect` must be provided (unless `preset` supplies sinks). They are mutually exclusive. |
| `not_through` | string | no | Sanitizer pattern (path-sensitive). Data flowing through code matching this pattern is considered safe. |
| `quantifier` | string | no | How sanitizers are evaluated. `"all_paths"` (default): **every** CFG path from source to sink must pass through the sanitizer to suppress the violation — use this for security checks. `"some_path"`: a sanitizer on **any** path suppresses — only for exploratory queries. |
| `scope_boundary` | string | no | Scope-level sanitizer (path-insensitive). Kills **all** taint within its enclosing scope. Use for framework boundaries like `"session.commit()"` or `"db.flush()"`. Can be combined with `not_through`. |
| `effect` | string | no | Effect-based sink — alternative to `to_pattern` (mutually exclusive). Detects when a tainted variable is mutated or read. Syntax: `"writes($VAR)"` or `"reads($VAR)"`. `$VAR` is not a reference to a captured metavar — it matches the tainted value propagated from the source, including attribute access (e.g. `obj.field = ...` matches `writes($VAR)` if `obj` is tainted). Only one effect type per query. |
| `files` | string | no | File glob scope |
| `interprocedural` | boolean | no | Enable cross-function analysis with fixed-point iteration. Default: `false` |
| `label` | string | no | Taint label name. Tags output for grouping. Optional for single-query use; required when composing multiple flow rules in batch. |
| `preset` | string | no | Load framework-specific source/sink/sanitizer definitions. One of: `flask`, `django`, `sqlalchemy`, `fastapi`. Preset rules are **merged** with any explicitly provided patterns — you can use both. |

**Metavariable scoping:** Metavar names (e.g. `$X`) in `from_pattern`,
`to_pattern`, `not_through`, and `effect` are **independent** — `$X` in
`not_through` does not need to match `$X` captured by `from_pattern`.
Each pattern is matched separately against code.

**Examples:**

Basic flow (SQL injection):
```json
{
  "type": "flow",
  "from_pattern": "request.args.get($X)",
  "to_pattern": "cursor.execute($Q)",
  "not_through": "escape($X)",
  "quantifier": "all_paths",
  "files": "src/**/*.py"
}
```

Effect-based sink (TOCTOU — detect mutation of tainted object):
```json
{
  "type": "flow",
  "from_pattern": "$Q.first()",
  "effect": "writes($OBJ)",
  "scope_boundary": "session.commit()",
  "files": "src/**/*.py"
}
```

Using a preset:
```json
{
  "type": "flow",
  "preset": "flask",
  "not_through": "sanitize($X)",
  "files": "src/**/*.py"
}
```

**Returns:** List of violations, each with source location, sink location,
and the propagation trace (chain of variable assignments connecting source
to sink).

---

### `emend_graph` — Call Graph

Compute callers, callees, or full call graph for a symbol.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | yes | Symbol to analyze (e.g. `"MyClass.method"`) |
| `direction` | string | no | `"callers"`, `"callees"`, or `"both"`. Default: `"both"` |
| `transitive` | boolean | no | When `true`, follow call chains recursively to find all reachable callers/callees. When `false` (default), return only direct callers/callees. |
| `depth` | integer | no | Maximum traversal depth. Only applies when `transitive` is `true`; ignored otherwise. Default: unlimited. **Caution:** `direction: "both"` with `transitive: true` and no depth limit can return the entire call graph. |
| `output` | string | no | One of: `text`, `json`, `dot`. Default: `json` |

**Example:**

```json
{
  "type": "graph",
  "symbol": "handle_request",
  "direction": "callers",
  "transitive": true,
  "depth": 3,
  "output": "json"
}
```

**Returns:** Call graph edges as `(caller, callee)` pairs.

---

### `emend_deadcode` — Dead Code Detection

Find symbols that appear to be unreferenced.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `kind` | string | no | Filter by symbol kind. One of: `function`, `class`, `method`, `variable` |
| `files` | string | no | File glob scope |
| `entry_point_decorators` | string[] | no | Decorators that mark entry points (not dead even if unreferenced) |
| `entry_point_names` | string[] | no | Function names that are entry points |
| `exclude_paths` | string[] | no | Glob patterns for paths to exclude from analysis |
| `include_private` | boolean | no | Include `_private` symbols. Default: `false` |
| `output` | string | no | One of: `text`, `json`. Default: `json` |

**Example:**

```json
{
  "type": "deadcode",
  "kind": "function",
  "files": "src/**/*.py",
  "entry_point_decorators": ["app.route", "celery.task"],
  "entry_point_names": ["main", "cli"],
  "exclude_paths": ["tests/**", "migrations/**"]
}
```

**Returns:** List of unreferenced symbols with name, file, line.

---

### `emend_impact` — Impact Analysis

Given a changed symbol or diff, compute the transitive set of affected symbols.
**At least one of `symbol` or `diff_ref` must be provided.** If both are given,
the impacted sets are unioned.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | no | Symbol that changed |
| `diff_ref` | string | no | Git ref to diff against (e.g. `"HEAD~1"`, `"main"`). emend runs `git diff` internally to identify changed symbols. |
| `output` | string | no | `"symbols"` (default): all impacted symbols. `"tests"`: only impacted tests. `"graph"`: full impact graph with witness edges. All outputs are JSON-encoded. |
| `max_depth` | integer | no | Maximum reverse-caller traversal depth. Default: unlimited |

**Example:**

```json
{
  "type": "impact",
  "diff_ref": "HEAD~1",
  "output": "tests"
}
```

**Returns:** List of impacted symbols/tests with witness edges showing
why each is impacted.

---

### `emend_datalog` — Raw Datalog Query

Execute a raw CozoScript query against the fact graph. Escape hatch for
queries not expressible through the other tools.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | yes | CozoScript query string |
| `params` | object | no | Named parameters to bind in the query |

**Example:**

```json
{
  "type": "datalog",
  "query": "?[name, file, line] := *symbol[name, file, _, kind, line, _, _], kind == $k",
  "params": {"k": "function"}
}
```

**Returns:** `{"rows": [[...], ...], "headers": ["col1", ...]}`.

### Relation Schemas

Core relations (column order matters for positional queries):

```
symbol[qualified_name, file_path, name, kind, line, end_line, parent]
  kind: "function" | "class" | "method" | "variable"

call[caller_qn, callee_qn, file_path, line, col, func_qn, block_id]

reference[symbol_qn, file_path, line, col, ref_kind, func_qn, block_id]
  ref_kind: "read" | "write" | "call" | "import" | "definition"

def_use[file_path, func_qn, var_name, kind, def_block, use_block,
        def_line, def_col, use_line, use_col]
  kind: "read" | "write" | "aug_write" | "del"

cfg_edge[file_path, func_qn, from_block, to_block, edge_kind,
         from_line, to_line]

import[importing_file, imported_module, imported_name, line, alias]

type_binding[symbol_qn, file_path, line, binding_kind, type_str]
  binding_kind: "annotation" | "inferred" | "return"
```

Additional: `cfg_block`, `method_call`, `decorator_on`, `source_loc`,
`func_summary`, `entry_point_decorator`, `entry_point_name`.

### More Examples

```
-- All functions in a file
?[name, line] := *symbol[_, fp, name, "function", line, _, _], fp == $f

-- Join: calls from one function to another
?[callee, line] := *call[caller, callee, _, line, _, _, _], caller == $fn

-- Dead code: unreferenced functions
?[name, fp, line] := *symbol[qn, fp, name, "function", line, _, _],
                     not *reference[qn, _, _, _, _, _, _]

-- Negation: functions without type annotations
?[name, fp] := *symbol[qn, fp, name, "function", _, _, _],
               not *type_binding[qn, _, _, "return", _]
```

---

### `emend_batch` — Batch Operations

Execute multiple operations in sequence.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `operations` | object[] | yes | Array of operation objects (any of the types above) |

**Example:**

```json
{
  "type": "batch",
  "operations": [
    {
      "type": "find",
      "pattern": "print($X)",
      "files": "src/**/*.py",
      "output": "count"
    },
    {
      "type": "replace",
      "pattern": "print($X)",
      "replacement": "logger.info($X)",
      "files": "src/**/*.py",
      "apply": true
    }
  ]
}
```

**Returns:** Array of results, one per operation. If an operation fails, its
entry contains `{"error": "message"}` and subsequent operations **continue**
(fail-safe, not fail-fast). Each `replace` in a batch operates on the
original file content, not the output of previous replacements.

---

## Common Patterns (Cookbook)

### "Find all functions that call X"

```json
{"type": "graph", "symbol": "dangerous_func", "direction": "callers"}
```

### "What does this function do?"

```json
{"type": "lookup", "symbol": "process_request", "files": "src/handlers.py", "output": "code"}
```

### "Replace all calls to old_name with new_name"

```json
{"type": "replace", "pattern": "old_name($...ARGS)", "replacement": "new_name($...ARGS)", "apply": true}
```

**Caveat:** This only replaces call-site syntax. It does not rename the
function definition, update imports, or fix string references. For full
project-wide rename, use the `emend rename` CLI command (not yet exposed
as an MCP tool — see "Not Yet Exposed" below).

### "Is there dead code in this module?"

```json
{"type": "deadcode", "files": "src/legacy/**/*.py", "include_private": true}
```

### "Check for SQL injection"

```json
{
  "type": "flow",
  "from_pattern": "request.args.get($X)",
  "to_pattern": "cursor.execute($Q)",
  "not_through": "sanitize($X)",
  "files": "src/**/*.py"
}
```

### "What tests are affected by my change?"

```json
{"type": "impact", "diff_ref": "HEAD~1", "output": "tests"}
```

### "Find all assignments to a variable"

```json
{"type": "refs", "symbol": "config.DEBUG", "ref_kind": "writes_only"}
```

## Output Format Reference

Not all tools support all formats. Default is `json` for all tools.

**Serialization formats** (control how data is encoded):

| Format | Meaning | Supported by |
|--------|---------|-------------|
| `json` | Structured JSON | all tools |
| `code` | Source text with file:line header | find, lookup, refs |
| `location` | `file.py:line:col` only | find, refs |
| `selector` | emend selector (e.g. `src/app.py::MyClass.method`) | find |
| `summary` | Symbol tree with signatures | lookup |
| `metadata` | Per-symbol details (lines, kind, decorators) | lookup |
| `count` | Integer count of matches | find |
| `text` | Human-readable plain text | graph, deadcode |
| `dot` | Graphviz DOT format | graph |

**Scope selectors** (control what `emend_impact` returns — always JSON-encoded):

| Value | Meaning |
|-------|---------|
| `symbols` | All transitively impacted symbols (default) |
| `tests` | Only impacted test symbols |
| `graph` | Full impact graph with witness edges |

## Not Yet Exposed

These emend CLI capabilities are not yet available as MCP tools:

- **`rename`** — full project-wide rename (definition + imports + references + docs)
- **`edit`** / **`add`** — modify symbol components (parameters, decorators, bases)
- **`move`** / **`copy-to`** — move symbols between files with import updates
- **`delete`** — safe delete with cascading removal of dead dependents
- **`lint`** / **`check`** — run pattern-based lint rules from config
- **`cfg`** — per-function control flow graphs
- **`types`** — query inferred types for symbols
- **`dsl`** — embedded DSL analysis (SQL, regex, etc.)

## Design Principles

1. **One tool per concept.** `find` searches, `replace` transforms, `flow`
   traces, `refs` finds references. No "unified search" that changes behavior
   based on input shape.

2. **Patterns are always strings.** A pattern value is never parsed by the
   MCP transport — it's an opaque string passed to the emend engine. This
   means `$`, `(`, `)`, `[`, `]` never need escaping.

3. **Dry run by default.** Anything that modifies code (`replace`) defaults
   to `"apply": false`. The caller must explicitly opt in.

4. **Flat parameters.** No deeply nested objects. The deepest nesting is one
   level (e.g. `entry_point_decorators` as a string array).

5. **Consistent naming.** The same concept uses the same parameter name across
   tools: `symbol` for symbol identifiers, `files` for file globs, `output`
   for format control.
