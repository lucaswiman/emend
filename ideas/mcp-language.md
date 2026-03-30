# emend MCP Server — Query Language Reference

## Overview

The emend MCP server exposes code analysis and transformation as tool calls.
Every operation is a JSON object with a `"type"` field that selects the
operation, plus flat, named parameters. Code patterns (e.g. `print($X)`) are
always passed as plain string values — never parsed by the transport layer.

## Pattern Syntax (Quick Reference)

Patterns are code-shaped templates with metavariable captures:

| Syntax | Meaning | Example |
|--------|---------|---------|
| `$X` | Capture any single AST node | `print($X)` |
| `$_` | Wildcard (match but don't capture) | `$_.method()` |
| `$...ARGS` | Capture zero or more nodes | `func($...ARGS)` |
| `$X:type` | Type-constrained capture | `$X:identifier`, `$X:str` |
| `$X:!type` | Negated type constraint | `$X:!int` |
| `$X:type[T]` | Oracle type constraint | `$X:type[Connection]` |
| `$F:returns[T]` | Return type constraint | `$F:returns[str]` |

Available simple types: `expr`, `stmt`, `identifier`, `int`, `str`, `float`,
`call`, `attr`, `any`.

## Tools

### `emend_find` — Structural Pattern Search

Find code matching a pattern.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `pattern` | string | yes | Code pattern with `$METAVAR` captures |
| `files` | string | no | File glob to scope the search (e.g. `"src/**/*.py"`) |
| `inside` | string | no | Only match inside code matching this pattern |
| `not_inside` | string | no | Exclude matches inside code matching this pattern |
| `kind` | string | no | Filter by AST node kind. One of: `call`, `assignment`, `function_definition`, `class_definition` |
| `output` | string | no | Output format. One of: `code`, `location`, `selector`, `json`, `count`. Default: `code` |
| `limit` | integer | no | Maximum number of results. Default: 100 |

**Example:**

```json
{
  "type": "find",
  "pattern": "print($X)",
  "files": "src/**/*.py",
  "not_inside": "def test_*",
  "output": "location"
}
```

**Returns:** List of matches with file, line, captured metavariable bindings.

---

### `emend_lookup` — Symbol Lookup

Look up a symbol by name and optionally drill into its components.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | yes | Symbol name, optionally dotted (`MyClass.method`) |
| `file` | string | no | Restrict to a specific file |
| `kind` | string | no | Symbol kind filter. One of: `function`, `class`, `method`, `variable`, `module` |
| `component` | string | no | Component to extract. One of: `params`, `returns`, `decorators`, `bases`, `body`, `imports` |
| `index` | integer | no | Index into the component list (e.g. `0` for first parameter) |
| `output` | string | no | Output format. One of: `code`, `summary`, `metadata`, `json`. Default: `code` |

**Example:**

```json
{
  "type": "lookup",
  "name": "MyClass.method",
  "file": "src/app.py",
  "component": "params"
}
```

**Returns:** Symbol source code, signature, or component values.

---

### `emend_replace` — Pattern-Based Code Replacement

Replace code matching a pattern with a replacement template.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `pattern` | string | yes | Code pattern to match |
| `replacement` | string | yes | Replacement template (can reference `$METAVAR` captures) |
| `files` | string | no | File glob scope |
| `inside` | string | no | Only replace inside code matching this pattern |
| `not_inside` | string | no | Exclude replacements inside this pattern |
| `apply` | boolean | no | If `true`, write changes to disk. Default: `false` (dry run) |

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
| `symbol` | string | yes | Qualified symbol name (e.g. `MyClass.method`) |
| `filter` | string | no | Reference kind filter. One of: `writes_only`, `reads_only`, `calls_only` |
| `files` | string | no | File glob scope |
| `output` | string | no | Output format. One of: `code`, `location`, `json`. Default: `location` |

**Example:**

```json
{
  "type": "refs",
  "symbol": "db.execute",
  "filter": "calls_only"
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
| `from_pattern` | string | yes | Source pattern — where tainted data originates |
| `to_pattern` | string | yes | Sink pattern — where tainted data must not reach |
| `not_through` | string | no | Sanitizer pattern — if data flows through this, it's safe |
| `quantifier` | string | no | Sanitizer quantifier. `"all_paths"`: sanitizer must appear on every path (default). `"some_path"`: sanitizer on any path suffices. |
| `scope_boundary` | string | no | Scope sanitizer pattern — kills all taint for the label within its scope (e.g. `"session.commit()"`) |
| `effect` | string | no | Effect-based sink (e.g. `"writes($OBJ)"` to detect mutations on tainted objects) |
| `files` | string | no | File glob scope |
| `interprocedural` | boolean | no | Enable cross-function analysis. Default: `false` |
| `label` | string | no | Taint label name for grouping related sources/sinks |
| `preset` | string | no | Load framework-specific rules. One of: `flask`, `django`, `sqlalchemy`, `fastapi` |

**Example:**

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

**Returns:** List of violations with source location, sink location,
and optionally the propagation path (variable assignments connecting
source to sink).

---

### `emend_graph` — Call Graph

Compute callers, callees, or full call graph for a symbol.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | yes | Symbol to analyze |
| `direction` | string | no | `"callers"`, `"callees"`, or `"both"`. Default: `"both"` |
| `depth` | integer | no | Maximum traversal depth for transitive queries. Default: unlimited |
| `transitive` | boolean | no | Follow edges transitively. Default: `false` |
| `output` | string | no | Output format. One of: `text`, `json`, `dot`. Default: `text` |

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
| `output` | string | no | Output format. One of: `text`, `json`. Default: `text` |

**Example:**

```json
{
  "type": "deadcode",
  "kind": "function",
  "files": "src/**/*.py",
  "entry_point_decorators": ["app.route", "celery.task"],
  "entry_point_names": ["main", "cli"],
  "exclude_paths": ["tests/", "migrations/"]
}
```

**Returns:** List of unreferenced symbols with name, file, line.

---

### `emend_impact` — Impact Analysis

Given a changed symbol or diff, compute the transitive set of affected symbols.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | no | Symbol that changed |
| `diff` | string | no | Git diff text or ref (e.g. `"HEAD~1"`) to parse for changed symbols |
| `output` | string | no | What to return. `"symbols"`: all impacted symbols. `"tests"`: only impacted tests. `"graph"`: full impact graph with edges. Default: `"symbols"` |
| `max_depth` | integer | no | Maximum reverse-caller depth. Default: unlimited |

**Example:**

```json
{
  "type": "impact",
  "diff": "HEAD~1",
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

**Returns:** Query result rows.

**Available relations:** `symbol`, `call`, `reference`, `def_use`, `cfg_edge`,
`cfg_block`, `import`, `type_binding`, `method_call`, `decorator_on`,
`source_loc`, `func_summary`, `entry_point_decorator`, `entry_point_name`.

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

**Returns:** Array of results, one per operation.

---

## Common Patterns (Cookbook)

### "Find all functions that call X"

```json
{"type": "graph", "symbol": "dangerous_func", "direction": "callers"}
```

### "What does this function do?"

```json
{"type": "lookup", "name": "process_request", "file": "src/handlers.py", "output": "code"}
```

### "Rename a function across the project"

```json
{"type": "replace", "pattern": "old_name($...ARGS)", "replacement": "new_name($...ARGS)", "apply": true}
```

Note: For full rename (including imports, string references, docs), prefer
the `emend rename` CLI command. The MCP `replace` tool does syntactic replacement only.

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
{"type": "impact", "diff": "HEAD~1", "output": "tests"}
```

### "Find all assignments to a variable"

```json
{"type": "refs", "symbol": "config.DEBUG", "filter": "writes_only"}
```

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

5. **Consistent output control.** Every tool accepts `"output"` to control
   format. The default is always the most useful for an agent (structured,
   not pretty-printed).
