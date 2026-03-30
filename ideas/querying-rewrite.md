# Query Language Unification: Brainstorming

## Status Quo

emend currently has **seven** overlapping query/config languages:

| Language | Syntax Style | Purpose |
|----------|-------------|---------|
| **Selector** | `file.py::Class.method[params][0]` | Navigate to code components |
| **Pattern** | `print($X)` with metavars | Match/capture code fragments |
| **Query (GritQL-like)** | `` `pat($x)` => `new($x)` where { ... } `` | Structural search + transform |
| **Trace YAML** | `sources:` / `sinks:` / `sanitizers:` | Taint analysis config |
| **Lint YAML** | `find:` / `flows-from:` / `flows-to:` | Pattern + flow lint rules |
| **Policy YAML** | `checks:` with `type: flow\|structural\|...` | Declarative policy checks |
| **Datalog (CozoScript)** | `?[x] := *symbol[x, kind], kind == "function"` | Relational fact queries |

The pain points:
- Trace, lint, and policy YAML all encode similar concepts (patterns, flow constraints) in slightly different schemas
- The query language and pattern language overlap but have different power
- Datalog is powerful but disconnected from the pattern/selector world
- Users must learn multiple syntaxes to use the full feature set

---

## Proposal 1: SQL-Flavored Query Language ("CodeQL Lite")

Model everything as tables. Patterns become `WHERE` predicates. Traces become joins.

### Core idea

Code facts are tables (`symbols`, `calls`, `references`, `def_use`, `cfg_edges`).
Pattern matching is a special predicate function `MATCHES(node, pattern)`.
Taint/flow is a built-in `FLOWS(source, sink)` relation.

### Examples

```sql
-- Find all print calls (replaces: find "print($X)")
SELECT match FROM code
WHERE MATCHES(match, 'print($X)')
  AND file GLOB 'src/**/*.py'

-- Lookup a symbol (replaces: file.py::MyClass.method[params])
SELECT params FROM symbols
WHERE qname = 'MyClass.method'

-- Taint analysis (replaces: trace YAML)
SELECT source, sink, path FROM FLOWS
WHERE MATCHES(source, 'request.args.get($X)')
  AND MATCHES(sink, 'cursor.execute($Q)')
  AND NOT EXISTS (
    SELECT 1 FROM path_nodes(path) p
    WHERE MATCHES(p, 'escape($X)')
  )
  AND label = 'user_input'

-- Lint rule (replaces: lint YAML)
CREATE RULE no_print AS
  SELECT match, 'Use logger instead' AS message
  FROM code WHERE MATCHES(match, 'print($X)')

-- Transform (replaces: replace command)
UPDATE code
SET match = REWRITE(match, 'print($X)', 'logger.info($X)')
WHERE MATCHES(match, 'print($X)')

-- Dead code
SELECT s.qname FROM symbols s
LEFT JOIN references r ON r.symbol_qn = s.qname
WHERE r.symbol_qn IS NULL
  AND s.kind = 'function'

-- Call graph traversal
WITH RECURSIVE callers AS (
  SELECT caller_qn FROM calls WHERE callee_qn = 'dangerous_func'
  UNION
  SELECT c.caller_qn FROM calls c
  JOIN callers ON c.callee_qn = callers.caller_qn
)
SELECT * FROM callers
```

### Pros
- Familiar to most developers
- Naturally expresses joins, aggregation, recursion
- Tables map cleanly to the existing fact graph
- `MATCHES()` keeps pattern syntax unchanged

### Cons
- Verbose for simple operations (`emend find "print($X)"` becomes a full SELECT)
- SQL recursion (WITH RECURSIVE) is clunkier than Datalog for transitive closures
- Rewrite/transform doesn't fit SQL semantics naturally
- Requires a SQL parser or embedding in an existing engine

---

## Proposal 2: CSS Selector Extension ("Code Selectors")

Extend the existing selector syntax to subsume patterns, flow, and filtering.
The selector already looks CSS-like; lean into it fully.

### Core idea

Everything is a selector chain with pseudo-classes, combinators, and attribute filters.
Patterns are `:matches(...)`. Flow is the `~>` combinator (taint-flows-to).
Transforms use `{ property: value }` blocks like CSS declarations.

### Examples

```css
/* Find print calls (replaces: find "print($X)") */
call:matches(print($X))

/* Scoped to files */
file[path~="src/**/*.py"] call:matches(print($X))

/* Symbol lookup (replaces: file.py::MyClass.method[params]) */
file[path="file.py"]::MyClass::method::params

/* With type filter */
function:returns(str):kind(public)

/* Nesting / containment (like CSS descendant combinator) */
class::TestSuite > method:matches(test_*)

/* NOT inside */
call:matches(eval($X)):not-inside(function:matches(test_*))

/* Taint / flow (new ~> combinator) */
call:matches(request.args.get($X))
  ~> call:matches(cursor.execute($Q))
  :not-through(call:matches(escape($X)))
  :label(user_input)

/* Lint rule */
@rule no-print {
  call:matches(print($X)) {
    message: "Use logger instead";
    replace: "logger.info($X)";
  }
}

/* Dead code */
symbol:kind(function):not(:referenced)

/* Transitive callers */
symbol[qname="dangerous"]::callers*

/* Policy */
@policy no-sqli (severity: error) {
  call:matches(request.args.get($X))
    ~> call:matches(cursor.execute($Q))
    :not-through(call:matches(sanitize($X))) {
    message: "SQL injection risk";
  }
}
```

### Pros
- Natural extension of existing selector syntax
- Pseudo-classes (`:not()`, `:matches()`, `:returns()`) already exist in emend
- Combinators (`>` child, ` ` descendant, `~>` flow) are intuitive
- Compact for common cases

### Cons
- CSS selector semantics don't map perfectly to code (no real "cascade")
- Complex flow constraints get unwieldy in selector syntax
- Hard to express Datalog-style joins and recursion
- Novel combinators like `~>` have no CSS precedent — might confuse

---

## Proposal 3: Pattern Calculus ("Regex for ASTs")

Extend the pattern language to be the single query language.
Regex has `/pattern/flags`; we do `{pattern}modifiers`.
Everything composes via regex-like operators.

### Core idea

The pattern `print($X)` is already the most natural part of emend.
Extend it with quantifiers, lookahead/lookbehind, alternation, and flow operators.
Selectors become pattern anchors. Config becomes inline annotations.

### Syntax

```
# Atoms (unchanged)
print($X)                          # literal code pattern
$X:identifier                      # typed metavar

# Regex-like operators
print($X) | log($X)               # alternation
request.$_($...ARGS)              # wildcard (already exists)

# Anchors (replace selectors)
@file.py :: @MyClass :: @method    # navigate to scope
@file.py:4-10                      # line range anchor
@src/**/*.py                       # file glob anchor

# Lookahead / lookbehind (replace --inside / --not-inside)
print($X) (?inside def test_$_)    # inside constraint
eval($X) (?!inside class Safe)     # NOT inside (negative lookahead)

# Flow operator (replaces trace YAML)
request.args.get($X) ~> cursor.execute($X)           # taint flows
request.args.get($X) ~> cursor.execute($X) !~ escape($X)  # not through

# Quantified flow
request.args.get($X) ~>all cursor.execute($X)        # all paths
request.args.get($X) ~>any cursor.execute($X)         # any path

# Rewrite (=> already exists in GritQL layer)
print($X) => logger.info($X)

# Named rules (replace lint/policy YAML)
rule no_print: print($X) => logger.info($X)
  @ severity=warning
  @ message="Use logger"

# Composition
rule sqli:
  request.args.get($X) ~> cursor.execute($Q) !~ escape($X)
  @ severity=error
  @ label=user_input
```

### Execution model

```bash
# CLI stays simple
emend find 'print($X)'
emend find 'print($X) (?inside def test_$_)'
emend find '@src/**/*.py :: request.args.get($X) ~> cursor.execute($Q)'
emend replace 'print($X)' 'logger.info($X)' --apply

# Config file uses the same syntax
# .emend/rules.em
rule no-print: print($X) => logger.info($X) @ severity=warning
rule sqli: request.args.get($X) ~> cursor.execute($Q) !~ escape($X) @ severity=error
```

### Pros
- Single language for everything — patterns, selectors, flow, rules
- Regex analogy is widely understood
- Very compact for common cases
- CLI one-liners become extremely powerful

### Cons
- Novel syntax — nobody knows it yet
- Regex-like operators on ASTs could be confusing (lookahead on trees?)
- Hard to express relational queries (joins, aggregation, transitive closure)
- The `~>` flow operator hides significant complexity (interprocedural analysis)
- Rule files lose the readability of YAML

---

## Proposal 4: JSONPath/jq-Style Path Language ("Code Paths")

Treat the codebase as a tree (files > modules > classes > methods > statements).
Query it with a jq/JSONPath-style path language.

### Core idea

Code is a tree. Paths navigate it. Filters select nodes. Pipes compose operations.
Pattern matching is a filter. Flow analysis is a built-in pipe stage.

### Examples

```bash
# Navigate (replaces selectors)
.src."file.py".MyClass.method.params[0]
.src."file.py".MyClass.method | .params[0]

# Glob paths
.src.**.*.py                          # all Python files
..MyClass..test_*                     # recursive descent to test methods

# Filter by kind
.src..[] | select(.kind == "function")

# Pattern match filter
.src..[] | match("print($X)")

# Pipe to transform
.src..[] | match("print($X)") | rewrite("logger.info($X)")

# Flow analysis
.src..[] | flow(
  from: "request.args.get($X)",
  to: "cursor.execute($Q)",
  not_through: "escape($X)"
)

# Dead code
.src..[] | select(.kind == "function") | select(.references | length == 0)

# Call graph
.src."file.py".my_func | .callers | recurse(.callers)

# Combine with output format
.src..[] | match("TODO") | {file: .path, line: .line, text: .matched}

# Lint rules (.emend/rules.jsonl — one rule per line)
{"name": "no-print", "query": '..[] | match("print($X)")', "message": "Use logger", "fix": 'rewrite("logger.info($X)")'}

# Trace config
{"name": "sqli", "query": '..[] | flow(from: "request.args.get($X)", to: "cursor.execute($Q)", not_through: "escape($X)")', "severity": "error"}
```

### Pros
- jq is well-known and loved by CLI users
- Pipes compose naturally — each stage narrows or transforms
- Path navigation replaces selectors cleanly
- Output shaping (`| {file, line}`) replaces `--output` flags
- Easy to add new filter/pipe stages without grammar changes

### Cons
- jq's tree model doesn't capture cross-references (calls, imports)
- Flow analysis awkwardly shoehorned into a pipe
- Recursive descent (`..`) on code trees could be very slow without indexing
- Pattern matching inside jq syntax creates an ugly nesting
- Config-as-JSON is less readable than YAML for complex rules

---

## Proposal 5: Datalog-First with Pattern Syntax Sugar ("Soufflé Meets Semgrep")

Make Datalog the single underlying language but with syntactic sugar that
compiles down to it. Patterns, selectors, and flow all desugar to Datalog rules.

### Core idea

The fact graph already uses CozoScript internally. Expose it as the primary
query language, but provide a "surface syntax" that compiles common operations
to Datalog. This is the CodeQL approach: a logic language with domain-specific
libraries.

### Surface syntax (compiles to Datalog)

```prolog
% Find print calls — syntactic sugar
find("print($X)") :- match(Node, "print($X)").

% Explicit Datalog — same thing
?[file, line, text] :-
  *reference[sqn, file, line, col, kind, fq, bid],
  match(text, "print($X)").

% Selector navigation — sugar
lookup("file.py::MyClass.method[params]") :-
  *symbol[qn, file, name, kind, line, end_line, parent],
  qn = "file.MyClass.method",
  component(qn, "params", Result).

% Flow analysis — sugar
flow_violation(Src, Sink) :-
  source(Src, "request.args.get($X)"),
  sink(Sink, "cursor.execute($Q)"),
  flows(Src, Sink),
  not sanitized(Src, Sink, "escape($X)").

% Dead code — direct Datalog
dead(QN) :-
  *symbol[QN, _, _, "function", _, _, _],
  not *reference[QN, _, _, _, _, _, _].

% Transitive callers — Datalog shines here
transitive_caller(X, Y) :- *call[X, Y, _, _, _, _, _].
transitive_caller(X, Z) :- transitive_caller(X, Y), *call[Y, Z, _, _, _, _, _].

% Rules/policies — Datalog rules with metadata annotations
@rule(name="no-print", severity="warning", message="Use logger")
violation(File, Line) :-
  *reference[_, File, Line, _, _, _, _],
  match_at(File, Line, "print($X)").

@rule(name="sqli", severity="error")
violation(File, Line) :-
  source_at(File, SrcLine, "request.args.get($X)"),
  sink_at(File, Line, "cursor.execute($Q)"),
  flows(SrcLine, Line),
  not sanitized_between(SrcLine, Line, "escape($X)").
```

### Config file (.emend/rules.dl)

```prolog
% Macros
macro(user_input, "request.args.get($X)").
macro(user_input, "request.form[$X]").

% Rules
@rule(name="sqli", severity="error", message="SQL injection risk")
violation(F, L) :-
  macro(user_input, SrcPat), source_at(F, SL, SrcPat),
  sink_at(F, L, "cursor.execute($Q)"),
  flows(SL, L),
  not sanitized_between(SL, L, "escape($X)").
```

### Pros
- Datalog is the right formalism for code analysis (CodeQL, Doop, Soufflé prove this)
- Transitive closures, joins, negation are natural
- Pattern matching becomes a predicate, not a separate language
- Everything compiles to one execution engine
- Sugar keeps simple cases simple: `find("print($X)")` is still one line

### Cons
- Datalog is unfamiliar to most developers
- The sugar layer needs careful design to not become its own language
- CozoScript syntax is not standard Datalog — users can't transfer knowledge from Soufflé/CodeQL directly
- Error messages from Datalog engines are notoriously bad
- Two-level system (sugar + raw) could be confusing about what's happening

---

## Proposal 6: Minimal Unification ("Keep Three, Kill Four")

Don't invent a new language. Instead, reduce to three complementary layers that
already exist, and make the YAML configs compile to them.

### The three layers

1. **Patterns** (unchanged): `print($X)`, `$OBJ:type[Conn]`, `$...ARGS`
2. **Selectors** (unchanged): `file.py::Class.method[params][0]`
3. **Datalog** (exposed): `?[x] := *symbol[x, kind], kind == "function"`

### What changes

- **Lint YAML** becomes a thin config that references patterns:
  ```yaml
  rules:
    no-print:
      match: "print($X)"       # pattern
      scope: "src/**/*.py"     # file glob (selector-like)
      inside: "def $F"         # pattern (context constraint)
      message: "Use logger"
      fix: "logger.info($X)"   # pattern (replacement)
  ```

- **Trace YAML** is abolished. Flow rules move into lint YAML with a `flow:` block:
  ```yaml
  rules:
    sqli:
      flow:
        from: "request.args.get($X)"
        to: "cursor.execute($Q)"
        not-through: "escape($X)"
        quantifier: all_paths
      scope: "src/**/*.py"
      message: "SQL injection"
  ```

- **Policy YAML** is abolished. Policies are just lint rules with severity:
  ```yaml
  rules:
    sqli:
      flow: { from: "...", to: "...", not-through: "..." }
      severity: error
      message: "SQL injection"
    no-eval:
      match: "eval($X)"
      severity: error
      message: "No eval"
  ```

- **Datalog** is available for advanced users via `emend query`:
  ```bash
  emend query '?[name] := *symbol[name, kind], kind == "function"'
  ```

- The GritQL-like query language is removed. Its features map to:
  - `where { $x <: contains ... }` → `--inside` flag + patterns
  - `sequential { ... }` → `emend batch` with YAML
  - `=> replacement` → `emend replace` with pattern pair

### Pros
- Minimal disruption — patterns and selectors don't change
- No new language to learn
- YAML stays for config (readable, tooling-friendly)
- Datalog escape hatch for power users
- Clear separation: patterns=matching, selectors=navigation, datalog=relations

### Cons
- Still three languages (just fewer configs)
- YAML is still verbose for complex rules
- Loses the composability of a single unified language
- The GritQL `where` clause was genuinely useful for inline constraints

---

## Proposal 7: EBNF-Style Production Rules ("Grammar of Code Smells")

Treat code patterns as grammar productions. Rules are productions that define
what constitutes a violation. Rewriting is grammar-guided transformation.

### Core idea

EBNF defines structure. Code queries are "grammars" that match bad code.
Nonterminals are named patterns. Terminals are code tokens and metavars.
Flow is expressed as production sequencing with constraints.

### Examples

```ebnf
(* Pattern matching — nonterminal = pattern *)
user_input  = "request.args.get(" , $KEY , ")" ;
sql_exec    = "cursor.execute(" , $QUERY , ")" ;
sanitizer   = "escape(" , $X , ")" ;

(* Lint rule — a "violation" production *)
sqli_violation = user_input , { statement - sanitizer } , sql_exec ;
(* reads: user_input followed by any statements except sanitizer, then sql_exec *)

(* Simple find — just a single production *)
print_call = "print(" , $ARGS , ")" ;

(* Selector — hierarchical grammar *)
target = file("src/**/*.py") > class("MyClass") > method("test_*") ;

(* Transform — production with replacement *)
print_call -> "logger.info(" , $ARGS , ")" ;

(* Dead code *)
dead_symbol = symbol(kind="function") - referenced_symbol ;

(* Composition *)
any_input = user_input | "request.form[" , $X , "]" | "os.environ[" , $X , "]" ;
```

### Pros
- Formal and precise — amenable to analysis and optimization
- Nonterminal reuse is natural (macros for free)
- Sequence (`a, b`) and exclusion (`a - b`) map well to flow/sanitizer concepts
- Familiar to PL/compiler folks

### Cons
- EBNF is about string/token sequences, not tree structures — awkward for ASTs
- Very unfamiliar to most developers outside compiler courses
- Sequencing in EBNF is linear; code flow is a graph (branches, loops)
- No natural way to express transitive closure or relational joins
- Would feel alien as a CLI query language

---

## Comparative Summary

| Criterion | SQL | CSS | Regex-AST | jq/Path | Datalog | Minimal | EBNF |
|-----------|-----|-----|-----------|---------|---------|---------|------|
| Familiarity | High | High | Medium | High | Low | High | Low |
| Pattern matching | Predicate | Pseudo-class | Native | Filter | Predicate | Native | Production |
| Flow analysis | JOIN/CTE | Combinator | Operator | Pipe | Native | YAML | Sequence |
| Transitive closure | CTE | Poor | Poor | `recurse` | Native | Datalog | Poor |
| Transform/rewrite | UPDATE | Declaration | `=>` | Pipe | Annotation | Pattern | `->` |
| Config ergonomics | Verbose | Medium | Compact | JSON | Verbose | YAML | Verbose |
| Implementation cost | High | Medium | High | Medium | Low* | Low | High |

\* Low because CozoScript already exists in the codebase.

---

## Coding-Agent-Friendly Query Syntax (MCP Server)

> **Design constraint**: Human ergonomics are irrelevant. What matters is
> unambiguous, composable, low-hallucination syntax that an LLM can reliably
> generate from natural language instructions.

### What makes a syntax LLM-friendly

1. **JSON-native**: LLMs are heavily trained on JSON. Structured output with
   known keys is far more reliable than generating novel DSL syntax.
2. **Flat over nested**: Deeply nested structures increase hallucination.
   A flat list of clauses is better than recursive grammar.
3. **Enumerated values over open strings**: `"kind": "function"` is more
   reliable than remembering arbitrary syntax like `:kind(function)`.
4. **Explicit keys over positional semantics**: `{"from": "...", "to": "..."}`
   is better than relying on operand position.
5. **No escaping puzzles**: Patterns contain quotes, brackets, parens.
   Embedding them in strings-within-strings is an error magnet.
6. **Consistent structure**: Every query should have the same shape regardless
   of what it's doing.

### Proposed MCP query format

Every query is a JSON object with a `type` discriminator and flat fields.

```jsonc
// Find pattern matches
{
  "type": "find",
  "pattern": "print($X)",
  "files": "src/**/*.py",
  "inside": "def $FUNC",           // optional scope constraint
  "not_inside": "class Test*",     // optional exclusion
  "kind": "call",                  // optional node kind filter
  "limit": 50
}

// Symbol lookup
{
  "type": "lookup",
  "name": "MyClass.method",
  "file": "src/app.py",            // optional
  "kind": "function",              // optional: function, class, method, variable
  "component": "params",           // optional: params, returns, decorators, bases, body
  "index": 0                       // optional: component index
}

// Replace/transform
{
  "type": "replace",
  "pattern": "print($X)",
  "replacement": "logger.info($X)",
  "files": "src/**/*.py",
  "inside": "def $FUNC",
  "apply": true
}

// Flow/taint query
{
  "type": "flow",
  "from_pattern": "request.args.get($X)",
  "to_pattern": "cursor.execute($Q)",
  "not_through": "escape($X)",     // optional
  "quantifier": "all_paths",       // "all_paths" | "some_path"
  "label": "user_input",
  "files": "src/**/*.py",
  "interprocedural": false
}

// References
{
  "type": "refs",
  "symbol": "MyClass.method",
  "filter": "writes_only",         // "writes_only" | "reads_only" | "calls_only" | null
  "files": "src/**/*.py"
}

// Call graph
{
  "type": "graph",
  "symbol": "handle_request",
  "direction": "callers",          // "callers" | "callees" | "both"
  "depth": 3,                      // max traversal depth
  "transitive": true
}

// Dead code detection
{
  "type": "deadcode",
  "kind": "function",
  "files": "src/**/*.py",
  "entry_points": {
    "decorators": ["app.route", "task"],
    "names": ["main", "cli"]
  }
}

// Raw Datalog (escape hatch)
{
  "type": "datalog",
  "query": "?[name, file, line] := *symbol[name, file, _, kind, line, _, _], kind == 'function'"
}

// Batch (multiple operations)
{
  "type": "batch",
  "operations": [
    {"type": "find", "pattern": "print($X)", "files": "src/**/*.py"},
    {"type": "replace", "pattern": "print($X)", "replacement": "log($X)", "apply": true}
  ]
}
```

### Why this is what I'd prefer

- **I can generate this reliably.** JSON with known keys is in my training
  distribution billions of times. I almost never hallucinate valid JSON with
  enumerated fields.
- **Pattern strings stay opaque.** I don't need to embed patterns inside a
  larger grammar. The pattern is always a plain string value for a known key.
- **No syntax to forget.** I don't need to remember whether flow uses `~>`,
  `=>`, `FLOWS()`, or `:flows-to`. It's just `"from_pattern"` and
  `"to_pattern"`.
- **Flat and predictable.** Every query has `type` + a few known optional
  fields. I can construct it from a template.
- **Composable without parsing.** `batch` is just an array. No need to figure
  out sequencing operators.
- **Self-documenting.** The keys say what they mean. No positional arguments.
- **Easy to validate.** A JSON Schema can catch my mistakes before execution.

### What I would NOT want

- Having to generate Datalog/CozoScript except as a last resort — the syntax
  is undertrained in my corpus and I'd make subtle errors with column ordering
  and relation names.
- Selector syntax with pseudo-classes — I'd confuse `:returns[str]` with
  `:type[str]` with `[returns]` constantly.
- Any syntax where I need to escape patterns inside patterns (regex inside
  SQL strings inside JSON).
- Positional arguments where I need to remember argument order.
