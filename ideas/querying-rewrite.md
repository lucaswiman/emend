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
- Trace, lint, and policy YAML all encode the same concepts (patterns, flow, constraints) in three different schemas
- The query language and pattern language overlap but have different power
- Datalog is powerful but disconnected from the pattern/selector world
- Users must learn multiple syntaxes to use the full system
- The YAML configs each invented ad-hoc ways to say "match this, not inside that, flowing from here to there"

### What should be preserved

The **pattern syntax** (`print($X)`, `$...ARGS`, `$OBJ:type[Conn]`) is emend's
best feature. It's code-shaped, instantly readable, and compresses a tree query
into something that looks like the code you're searching for. Any unification
should keep patterns as the primary way to describe code.

The **selector syntax** (`file.py::Class.method[params]`) is also good — it
names a location in the code the way a filesystem path names a file. It's
orthogonal to patterns (selectors navigate, patterns match).

The mess is in everything above those two: how patterns are composed, filtered,
connected by flow, grouped into rules, and configured.

---

## Proposal 1: Pipe Composition ("Patterns + jq + CSS pseudo-classes")

**Inspiration**: jq pipes for composition, CSS pseudo-classes for filtering,
Unix philosophy for "do one thing per stage."

### Core insight

Every emend operation is really a pipeline: *select files* → *match patterns*
→ *filter by structure* → *do something*. Make the pipe explicit. Each stage
is a small language that already exists (glob, pattern, selector). The pipe
is the glue that replaces all the YAML config keys.

### Syntax

```bash
# The pipe operator connects stages
in "src/**/*.py" | match "print($X)" | inside "def $FUNC" | replace "logger.info($X)"

# Short form (implicit first stage from context)
match "print($X)" | not-inside "class Test*" | count

# Flow is a pipe stage that takes two patterns
in "src/**" | flow "request.args.get($X)" -> "cursor.execute($Q)" \
             | not-through "escape($X)" \
             | quantifier all-paths

# Selectors are a stage too
select "file.py::MyClass.method" | component params | index 0

# Graph traversal — recurse is a pipe stage (like jq's recurse)
select "dangerous_func" | callers | recurse callers --depth 3

# Dead code is a built-in pipeline
in "src/**" | symbols --kind function | unreferenced

# Output shaping (steal from jq)
in "src/**" | match "TODO" | format "{file}:{line} {matched}"
```

### Rule files (.emend/rules.pipe)

```bash
# Each rule is a named pipeline
rule no-print (severity=warning, message="Use logger"):
  match "print($X)" | replace "logger.info($X)"

rule sqli (severity=error, message="SQL injection"):
  in "src/**" | flow "request.args.get($X)" -> "cursor.execute($Q)" \
               | not-through "escape($X)"

# Macros are just named pattern fragments
let user_input = "request.args.get($X)" | "request.form[$X]"

rule sqli2 (severity=error):
  flow {user_input} -> "cursor.execute($Q)"
```

### Why this works

- Patterns stay exactly as-is — they're string arguments to `match`
- Selectors stay as-is — they're arguments to `select`
- The pipe replaces: `--inside`, `--not-inside`, `--where`, `--scope-local`,
  `flows-from`/`flows-to`/`not-through`, and the `where {}` clause from GritQL
- `flow A -> B` subsumes all of: trace YAML sources/sinks, lint flow rules,
  policy flow checks
- Rules are just named pipelines with metadata — no separate YAML schemas
- Each stage has one job, so the mental model is simple

### Concerns

- Shell-like syntax means quoting hell when used on an actual shell command line
- Multi-line pipelines need continuation (backslash or wrapping)
- No natural way to express Datalog-style joins (two unrelated facts about
  the same symbol). Would still need a Datalog escape hatch for power queries.

---

## Proposal 2: List Comprehensions ("Python for code queries")

**Inspiration**: Python/Haskell comprehensions, GraphQL's "request what you
want" philosophy, Rego's rule-as-comprehension style.

### Core insight

emend users are Python developers. They already know comprehension syntax.
A code query is just `[what for thing in where if conditions]`. Flow is a
generator you iterate over. Rules are assignments.

### Syntax

```python
# Find (comprehension over matches)
[m for m in match("print($X)") if m.file ~ "src/**"]

# With scope constraint
[m for m in match("print($X)")
   if m.inside ~ "def $FUNC"
   if not m.inside ~ "class Test*"]

# Replace (assignment to .code)
[m.code = "logger.info($X)"
 for m in match("print($X)")]

# Flow (iterate over flow paths)
[v for v in flow(
    source = "request.args.get($X)",
    sink   = "cursor.execute($Q)",
 )
 if not v.through ~ "escape($X)"]

# Symbol lookup
[s for s in symbols("src/**")
   if s.kind == "function"
   if s.name ~ "test_*"]

# Dead code
[s for s in symbols("src/**")
   if s.kind == "function"
   if len(s.refs) == 0]

# Call graph
[c for c in callers("dangerous_func", transitive=True, depth=3)]

# Selector access
[s.params[0] for s in select("file.py::MyClass.method")]

# Output shaping
[{"file": m.file, "line": m.line, "text": m.text}
 for m in match("print($X)")]
```

### Rule files (.emend/rules.py — it's just Python-shaped)

```python
# Rules are named comprehensions
rule("no-print", severity="warning", message="Use logger")
violations = [m for m in match("print($X)")]
fix = "logger.info($X)"

rule("sqli", severity="error", message="SQL injection")
violations = [
    v for v in flow(
        source = "request.args.get($X)",
        sink   = "cursor.execute($Q)",
    )
    if not v.through ~ "escape($X)"
]

# Macros are just variables
user_input = "request.args.get($X)" | "request.form[$X]"

# Compound rules
rule("toctou", severity="error")
violations = [
    v for v in flow(source="$Q.first()", sink=writes("$OBJ"))
    if not v.scope_boundary ~ "session.commit()"
]
```

### Why this works

- Zero learning curve for Python developers
- Comprehension syntax naturally encodes filter chains, which is what all
  the YAML configs were trying to express
- `if` clauses replace `--where`, `--inside`, `not-through`, etc.
- `for v in flow(...)` unifies trace analysis and flow rules into one concept
- Pattern strings stay as opaque arguments — no grammar collision
- The `~` operator (match) is the only new thing to learn

### Concerns

- It *looks* like Python but isn't — subtly different semantics would confuse
- Actually parsing Python comprehension syntax is nontrivial
- The `~` operator for pattern matching is ad-hoc
- No natural way to express "all paths" vs "some path" quantifiers without
  keyword args buried in function calls
- Might lure people into expecting arbitrary Python (lambdas, imports, etc.)

---

## Proposal 3: S-Expression Core with Surface Sugar ("Lisp Machine for Code")

**Inspiration**: Emacs/Elisp (queries as data), Clojure spec (composable
predicates), Datalog (logic), tree-sitter's own S-expression query syntax.

### Core insight

Tree-sitter already has an S-expression query language for ASTs. emend's
patterns compile to tree-sitter queries internally. What if the composition
layer was also S-expressions? S-expressions are trivially parseable,
homoiconic (queries are data structures), and compose without ambiguity.
A thin surface syntax makes them human-friendly.

### The S-expression core (what gets executed)

```scheme
;; Find
(find (pattern "print($X)")
      (in "src/**/*.py")
      (inside (pattern "def $FUNC")))

;; Replace
(rewrite (pattern "print($X)")
         (replacement "logger.info($X)")
         (in "src/**/*.py"))

;; Flow
(flow (source (pattern "request.args.get($X)"))
      (sink (pattern "cursor.execute($Q)"))
      (not-through (pattern "escape($X)"))
      (quantifier all-paths))

;; Selector lookup
(lookup (selector "file.py::MyClass.method")
        (component params)
        (index 0))

;; Graph
(callers "dangerous_func" (transitive #t) (depth 3))

;; Dead code
(dead-code (kind function) (in "src/**"))

;; Composition via AND/OR
(and (find (pattern "eval($X)"))
     (not (inside (pattern "def test_$_"))))

;; Rule definition
(rule "sqli"
  (severity error)
  (message "SQL injection risk")
  (flow (source (pattern "request.args.get($X)"))
        (sink (pattern "cursor.execute($Q)"))
        (not-through (pattern "escape($X)"))))
```

### Surface syntax (what humans type)

```bash
# CLI: the S-expr is implicit, positional args fill common slots
emend find "print($X)" --in "src/**" --inside "def $FUNC"
emend flow "request.args.get($X)" -> "cursor.execute($Q)" --not-through "escape($X)"

# Config file: indented keyword syntax (like Hy, or a Lisp without parens)
rule sqli
  severity error
  message "SQL injection risk"
  flow
    source "request.args.get($X)"
    sink "cursor.execute($Q)"
    not-through "escape($X)"

rule no-print
  severity warning
  message "Use logger"
  find "print($X)"
  fix "logger.info($X)"
```

### Why this works

- S-expressions give you free composability — rules, queries, and configs
  are all the same data structure
- The surface syntax is just YAML-shaped indentation that desugars to S-exprs
- Macros fall out naturally: `(define user-input (or (pattern "request.args.get($X)") (pattern "request.form[$X]")))`
- Tree-sitter's S-expression queries are well-known in the ecosystem
- The MCP/API layer can accept either S-expressions or the JSON equivalent
  (S-exprs and JSON are near-isomorphic)
- Pattern strings stay opaque — no escaping issues

### Concerns

- Lisp syntax is polarizing — many developers find it unreadable
- The surface sugar adds a second syntax, partially defeating the point
- S-expressions are verbose for simple cases: `(find (pattern "print($X)"))` vs `find "print($X)"`
- Debugging nested S-expressions is harder than debugging flat YAML
- The homoiconicity benefit only pays off if you build a macro system,
  which is a big investment

---

## Proposal 4: Prolog-Style Unification ("Everything is a Query")

**Inspiration**: Prolog unification, CodeQL (logic over code), Rego/OPA
(policy as logic), but with emend patterns as the term language instead
of abstract tuples.

### Core insight

The deepest unification is to notice that patterns, flow, selectors, and
rules are all forms of *logical constraint*. A pattern is a constraint on
code shape. A flow rule is a constraint on data paths. A selector is a
constraint on location. A lint rule is a constraint that, when satisfied,
indicates a problem.

What if the query language was just: state your constraints, get back
everything that satisfies them?

### Syntax

```prolog
% Simple find — "give me matches"
?- code(X), X ~ "print($ARG)".

% Scoped
?- code(X), X ~ "print($ARG)", file(X, F), F ~ "src/**".

% Inside constraint
?- code(X), X ~ "print($ARG)", inside(X, Y), Y ~ "def $FUNC".

% Replace — unification on the output
?- code(X), X ~ "print($ARG)" => "logger.info($ARG)".

% Flow — source and sink are just constrained code nodes
?- code(Src), Src ~ "request.args.get($KEY)",
   code(Sink), Sink ~ "cursor.execute($Q)",
   flows(Src, Sink),
   \+ sanitized(Src, Sink, "escape($X)").

% Selector — location is a constraint
?- symbol(S), name(S, "MyClass.method"), params(S, P), nth(P, 0, First).

% Dead code
?- symbol(S), kind(S, function), \+ referenced(S).

% Transitive callers — Prolog does this natively
caller(X, Y) :- calls(X, Y).
caller(X, Z) :- calls(X, Y), caller(Y, Z).
?- caller(Who, "dangerous_func").

% Type constraint
?- code(X), X ~ "$F($ARG)", type(ARG, "str"), returns(F, "int").
```

### Rule files (.emend/rules.pl)

```prolog
% Rule = a named query whose results are violations
:- rule(no_print, severity(warning), message("Use logger")).
no_print(File, Line) :-
    code_at(File, Line, X), X ~ "print($ARG)".

:- rule(sqli, severity(error), message("SQL injection")).
sqli(File, Line) :-
    code_at(File, SrcLine, Src), Src ~ "request.args.get($KEY)",
    code_at(File, Line, Sink), Sink ~ "cursor.execute($Q)",
    flows(Src, Sink),
    \+ sanitized(Src, Sink, "escape($X)").

% Macro = a reusable predicate
user_input(X) :- X ~ "request.args.get($KEY)".
user_input(X) :- X ~ "request.form[$KEY]".
```

### Why this works

- Logic programming is *the* natural fit for code analysis (this is why
  CodeQL, Doop, Soufflé, and CozoScript all exist)
- Patterns become the `~` operator on terms — code unification
- Flow, containment, type constraints, scope — all just predicates
- Rules are named queries — no separate YAML schema
- Transitive closure is native (recursive rules)
- The `~` operator on patterns is the bridge: it keeps pattern syntax
  unchanged while embedding it in a logic context
- Existing CozoScript/Datalog backend maps directly

### Concerns

- Prolog syntax is niche — most developers haven't seen it since university
- The `~` operator conflates tree-sitter matching with Prolog unification,
  which could confuse people who know Prolog
- Negation-as-failure (`\+`) is subtle and has known pitfalls
- Performance: naive Prolog evaluation without tabling would be disastrous
  on large codebases. Needs Datalog-style bottom-up evaluation.
- The gap between "this looks like Prolog" and "this isn't actually Prolog"
  would frustrate experienced logic programmers

---

## Proposal 5: GraphQL-Shaped Queries Over the Code Graph

**Inspiration**: GraphQL (ask for the shape you want back), Cypher/Neo4j
(graph pattern matching with ASCII art), the fact that code *is* a graph
(symbols reference each other, data flows between them).

### Core insight

The fact graph is already a property graph: nodes are symbols, files, and
code locations; edges are calls, references, data flow, containment. What
if you queried it the way you query a GraphQL API — by describing the shape
of the subgraph you want back?

### Syntax

```graphql
# Find pattern matches — query returns the shape you ask for
query {
  match(pattern: "print($X)", in: "src/**/*.py") {
    file
    line
    captures { X }
  }
}

# Symbol lookup with component drilling
query {
  symbol(name: "MyClass.method", file: "file.py") {
    params(index: 0) { name, type, default }
    returns { type }
    decorators { name }
  }
}

# Flow analysis — edges in the graph
query {
  flow(
    source: "request.args.get($X)",
    sink: "cursor.execute($Q)",
    notThrough: "escape($X)",
    quantifier: ALL_PATHS
  ) {
    source { file, line, captures { X } }
    sink { file, line }
    path { steps { file, line, variable } }
  }
}

# Call graph — recursive graph traversal
query {
  symbol(name: "dangerous_func") {
    callers(depth: 3) {
      name
      file
      callers { name }  # nested = transitive
    }
  }
}

# Dead code
query {
  symbols(in: "src/**", kind: FUNCTION, unreferenced: true) {
    name
    file
    line
  }
}

# Rewrite via mutation
mutation {
  replace(
    pattern: "print($X)",
    replacement: "logger.info($X)",
    in: "src/**/*.py"
  ) {
    file
    line
    before
    after
  }
}
```

### Rule files (.emend/rules.graphql)

```graphql
# Rules are named queries with violation semantics
rule @name("no-print") @severity(WARNING) @message("Use logger") {
  match(pattern: "print($X)") {
    file, line
  }
  fix: replace(replacement: "logger.info($X)")
}

rule @name("sqli") @severity(ERROR) @message("SQL injection") {
  flow(
    source: "request.args.get($X)",
    sink: "cursor.execute($Q)",
    notThrough: "escape($X)"
  ) {
    source { file, line }
    sink { file, line }
  }
}

# Fragments for reuse (= macros)
fragment UserInput on Match {
  match(pattern: "request.args.get($X)") { ... }
  match(pattern: "request.form[$X]") { ... }
}
```

### Why this works

- GraphQL is widely known (unlike Datalog/Prolog)
- "Request the shape you want" naturally replaces `--output` flags
- Nesting expresses graph traversal (callers of callers) without explicit
  recursion syntax
- Mutations map to rewrites/replacements
- Directives (`@severity`, `@message`) handle rule metadata
- Fragments handle macros/reuse
- The schema *is* the documentation — tools like GraphiQL give
  auto-complete for free
- Pattern strings are just argument values — no grammar collision

### Concerns

- GraphQL is read-heavy by design; mutations are second-class. Transform
  operations would feel bolted on.
- The query language is optimized for requesting *known shapes* from an API,
  not for expressing *unknown patterns* in code. The mismatch would show up
  in complex structural queries.
- Adding flow semantics to GraphQL's type system is a stretch — `notThrough`
  and `quantifier` don't have GraphQL equivalents.
- Overkill for simple `emend find "print($X)"` — forces you to specify
  return fields.
- Implementing a GraphQL schema + resolver layer is significant engineering.

---

## Proposal 6: "Keep Two Languages, Unify the Rest"

**Inspiration**: The pragmatic observation that emend already has two good
languages (patterns and selectors) and one powerful backend (Datalog). The
problem isn't the primitives — it's the *four different YAML schemas* that
awkwardly re-encode the same concepts. Kill the configs, not the languages.

### Core insight

Trace YAML, lint YAML, and policy YAML are all expressing the same thing:
"when this pattern/flow/condition holds, report a violation with this message."
They should be one config format. Meanwhile the GritQL-like query language
adds a third way to say `--inside` and `--where`, which the CLI flags already
handle. Remove it. You're left with:

1. **Patterns** — match code shapes (`print($X)`)
2. **Selectors** — name code locations (`file.py::Class.method[params]`)
3. **One rule config** — YAML or TOML that references patterns and selectors
4. **Datalog escape hatch** — for power users, exposed via `emend query`

### The unified rule config (.emend/rules.yaml)

```yaml
macros:
  user_input: "request.args.get($X) | request.form[$X]"

rules:
  # Structural rule (= current lint rule)
  no-print:
    match: "print($X)"
    not-inside: "def test_*"
    in: "src/**/*.py"
    severity: warning
    message: "Use logger instead of print"
    fix: "logger.info($X)"

  # Flow rule (= current trace config + lint flow rule + policy flow check)
  sqli:
    flow:
      from: "{user_input}"
      to: "cursor.execute($Q)"
      not-through: "escape($X)"
      quantifier: all_paths        # all_paths | some_path
    in: "src/**/*.py"
    severity: error
    message: "SQL injection: user input flows to cursor.execute()"

  # Effect-based flow (= current trace effect sinks)
  toctou:
    flow:
      from: "$Q.first()"
      to:
        effect: "writes($OBJ)"
      scope-boundary: "session.commit()"
    severity: error
    message: "TOCTOU: mutation on unlocked ORM object"
    interprocedural: true

  # Dead code (= current deadcode config)
  unused-functions:
    deadcode:
      kind: function
      entry-points:
        decorators: ["app.route", "task"]
        names: ["main"]
      exclude-paths: ["tests/", "migrations/"]
    severity: warning
    message: "Function appears unused"

  # Type check (= current policy type check)
  return-types:
    type-check:
      selector: "src/**/*.py::*"
      kind: returns
      expected: "str | int | None"
    severity: info

  # Raw Datalog (= current policy custom/datalog check)
  custom-invariant:
    datalog: |
      ?[file, line] :=
        *symbol[qn, file, _, "function", line, _, _],
        *reference[qn, file, line, _, "call", _, _]
    severity: warning
    message: "Recursive function detected"
```

### What gets removed

- **Trace YAML** (`trace:` section with `labels:`, `sources:`, `sinks:`,
  `sanitizers:`, `scope_sanitizers:`) → folded into `flow:` rules above
- **Policy YAML** (`.emend/policies.yaml` with `policies:` list) → folded
  into rules above
- **GritQL query language** (`where { $x <: contains ... }`, `sequential`,
  `multifile`) → removed entirely; its features map to CLI flags:
  - `where { $x <: contains P }` → `--inside P`
  - `where { $x <: imported_from("mod") }` → `--imported-from mod`
  - `sequential { ... }` → `emend batch`
  - `P => Q` → `emend replace P Q`

### What stays the same

- `emend find "print($X)"` — unchanged
- `emend replace "old($X)" "new($X)"` — unchanged
- `emend search file.py::Class.method` — unchanged
- `emend refs MyFunc --writes-only` — unchanged
- `emend lint` — reads from unified rules.yaml instead of patterns.yaml
- `emend trace` — reads `flow:` rules from rules.yaml, `--preset` still works

### CLI for trace/flow becomes:

```bash
# Before (separate command with separate config)
emend trace --config .emend/patterns.yaml --label user_input

# After (lint subsumes trace)
emend lint --rule sqli
# or
emend lint  # runs all rules including flow rules
```

### Why this works

- Minimal invention: no new language, just one fewer config format
- Patterns and selectors are untouched
- The YAML is simpler than any of the three it replaces because it's
  consistent: every rule has `severity`, `message`, and one of `match:`,
  `flow:`, `deadcode:`, `type-check:`, or `datalog:`
- Migration is straightforward: mechanical translation from the old configs
- The Datalog escape hatch means nothing is lost

### Concerns

- Still two-and-a-half languages (pattern, selector, YAML-with-embedded-patterns)
- YAML is arguably the wrong format for anything with nesting and quoting
  (patterns with `$` in YAML strings is already annoying)
- Users who want the GritQL `where` clause's expressiveness lose it
- "Lint" as the name for "all checks including taint analysis" is a stretch

---

## Proposal 7: Cypher-Inspired Graph Pattern Language

**Inspiration**: Neo4j Cypher (ASCII-art graph patterns), SPARQL (RDF graph
queries), the observation that code analysis is fundamentally graph pattern
matching — not tree matching (patterns) or table joining (SQL).

### Core insight

Code relationships form a graph: `func_a -[calls]-> func_b -[reads]-> var_x`.
Data flow is a path in that graph. Taint analysis is reachability with
constraints. What if you could draw the pattern you're looking for?

### Syntax

```cypher
// Find pattern matches
MATCH (n:Code)-[:matches]->("print($X)")
WHERE n.file =~ "src/**/*.py"
RETURN n.file, n.line, n.captures.X

// Flow analysis — it's just a path pattern!
MATCH path = (src:Code)-[:flows_to*]->(sink:Code)
WHERE src matches "request.args.get($X)"
  AND sink matches "cursor.execute($Q)"
  AND NONE(n IN nodes(path) WHERE n matches "escape($X)")
RETURN src, sink, path

// Quantifier: ALL paths must be sanitized
MATCH path = (src)-[:flows_to*]->(sink)
WHERE src matches "request.args.get($X)"
  AND sink matches "cursor.execute($Q)"
  AND NOT ALL(p IN paths(src, sink)
              WHERE ANY(n IN nodes(p) WHERE n matches "escape($X)"))
RETURN src, sink

// Call graph — natural graph traversal
MATCH (caller:Symbol)-[:calls*1..3]->(target:Symbol {name: "dangerous_func"})
RETURN caller.name, caller.file

// Dead code
MATCH (s:Symbol {kind: "function"})
WHERE NOT (s)<-[:references]-()
RETURN s.name, s.file, s.line

// Containment
MATCH (outer:Symbol)-[:contains]->(inner:Code)
WHERE outer matches "def test_$NAME"
  AND inner matches "assert $X"
RETURN outer.name, inner.line

// Rewrite
MATCH (n:Code)
WHERE n matches "print($X)"
SET n.code = "logger.info($X)"

// Scope sanitizer — a path constraint
MATCH path = (src)-[:flows_to*]->(sink)
WHERE src matches "$Q.first()"
  AND sink matches writes("$OBJ")
  AND NONE(n IN nodes(path) WHERE n matches "session.commit()")
RETURN src, sink AS toctou_violation
```

### Rule files

```cypher
// Rules are named MATCH queries
CREATE RULE sqli (severity: "error", message: "SQL injection") AS
MATCH path = (src)-[:flows_to*]->(sink)
WHERE src matches "request.args.get($X)"
  AND sink matches "cursor.execute($Q)"
  AND NONE(n IN nodes(path) WHERE n matches "escape($X)")
RETURN src.file, src.line, sink.line

CREATE RULE no_print (severity: "warning", message: "Use logger",
                      fix: "logger.info($X)") AS
MATCH (n:Code)
WHERE n matches "print($X)"
```

### Why this works

- Graph pattern matching is genuinely the right abstraction for code analysis
- `(a)-[:calls*]->(b)` is more intuitive for transitive closure than SQL CTEs
  or Datalog recursive rules
- `NONE(n IN path WHERE ...)` is a *beautiful* way to express "not through" —
  it reads like English
- Path variables make taint analysis first-class: the path IS the trace
- Cypher is gaining adoption (Neo4j, Memgraph, Apache AGE, the GQL ISO standard)
- Patterns stay as string predicates via `matches`

### Concerns

- Requires a graph database or graph query engine — CozoScript can do some
  of this but isn't Cypher
- Cypher's property graph model doesn't perfectly match ASTs (ASTs are trees,
  not arbitrary graphs; the graph structure is in the *cross-references*)
- Variable-length path patterns (`*1..3`) have exponential worst cases
- Mixing ASCII-art graph patterns with code patterns (`"print($X)"`) in the
  same query is visually noisy
- The GQL/Cypher ecosystem is fragmented (Neo4j Cypher vs ISO GQL vs openCypher)

---

## Cross-Cutting Observations

**The real tension is between two kinds of query:**

1. **Structural matching** — "find code that looks like X." This is inherently
   *syntactic* and local. Patterns handle it perfectly.

2. **Relational/graph queries** — "find code where A calls B which reads C and
   C flows to D." This is inherently *semantic* and global. Patterns can't
   express it; you need joins, transitive closure, path constraints.

Every proposal above is really a different answer to: *how do you bridge these
two?* The options are:

- **Patterns as predicates in a relational language** (Proposals 4, 5, 7) —
  the relational language is primary, patterns are leaf predicates
- **Relational concepts as operators on patterns** (Proposals 1, 2, 3) —
  patterns are primary, relations are combinators/stages/operators
- **Keep them separate, share config** (Proposal 6) — don't bridge, just
  reduce the config surface

**The YAML configs are the real problem, not the languages.** Trace, lint,
and policy YAML re-encode the same concepts differently. Merging them into
one schema (Proposal 6) would eliminate most of the day-to-day confusion
without any new language design. The other proposals are more ambitious but
riskier.

**Anything relational should probably just be Datalog.** CozoScript is already
in the codebase. CodeQL proved that Datalog + code = a winning combination.
The question is just how much sugar to put on top.

---

## Coding-Agent-Friendly Query Syntax (MCP Server)

> **Design constraint**: Human ergonomics are irrelevant here. What matters is
> unambiguous, composable, low-hallucination syntax that an LLM can reliably
> generate from natural language instructions.

### What makes a syntax LLM-friendly

1. **JSON-native**: LLMs produce JSON with extremely high reliability.
   Structured output with known keys beats generating any DSL syntax.
2. **Flat over nested**: Deep nesting increases hallucination rate.
3. **Enumerated values over open syntax**: `"kind": "function"` is more
   reliable than remembering `:kind(function)` or `kind="function"` or
   `kind: function`.
4. **Explicit keys over positional semantics**: `{"from": "...", "to": "..."}`
   beats relying on argument order.
5. **Pattern strings as opaque values**: Patterns should be string values
   for a named key. Never embedded inside a larger grammar.
6. **Consistent shape**: Every query should have the same top-level structure.
7. **No quoting/escaping puzzles**: Patterns contain `$`, `(`, `)`, `[`, `]`.
   These must never need escaping in the transport format.

### Proposed format

Every MCP tool call is a JSON object with a `type` discriminator and flat,
optional fields. Patterns are always plain string values.

```jsonc
// Structural search
{
  "type": "find",
  "pattern": "print($X)",
  "files": "src/**/*.py",
  "inside": "def $FUNC",
  "not_inside": "class Test*",
  "limit": 50
}

// Symbol lookup
{
  "type": "lookup",
  "name": "MyClass.method",
  "file": "src/app.py",
  "kind": "function",
  "component": "params",
  "index": 0
}

// Replace
{
  "type": "replace",
  "pattern": "print($X)",
  "replacement": "logger.info($X)",
  "files": "src/**/*.py",
  "apply": true
}

// Flow/taint
{
  "type": "flow",
  "from_pattern": "request.args.get($X)",
  "to_pattern": "cursor.execute($Q)",
  "not_through": "escape($X)",
  "quantifier": "all_paths",
  "interprocedural": false
}

// References
{
  "type": "refs",
  "symbol": "MyClass.method",
  "filter": "writes_only"
}

// Call graph
{
  "type": "graph",
  "symbol": "handle_request",
  "direction": "callers",
  "depth": 3,
  "transitive": true
}

// Dead code
{
  "type": "deadcode",
  "kind": "function",
  "files": "src/**/*.py",
  "entry_point_decorators": ["app.route"],
  "entry_point_names": ["main"]
}

// Raw Datalog escape hatch
{
  "type": "datalog",
  "query": "?[name, file, line] := *symbol[name, file, _, kind, line, _, _], kind == 'function'"
}

// Batch
{
  "type": "batch",
  "operations": [
    {"type": "find", "pattern": "print($X)", "files": "src/**/*.py"},
    {"type": "replace", "pattern": "print($X)", "replacement": "log($X)", "apply": true}
  ]
}
```

### Why I'd prefer this over any of the proposals above

- **Reliability**: I can produce valid JSON with known keys almost 100% of
  the time. I cannot reliably produce novel DSL syntax, Cypher, Prolog, or
  even Datalog — I'd make subtle errors with operator precedence, quoting,
  and relation arity.
- **No syntax to confuse**: I don't need to remember whether flow uses `~>`,
  `->`, `FLOWS()`, `flows_to*`, or `not-through:`. It's just `"from_pattern"`
  and `"to_pattern"` as JSON keys.
- **Pattern strings are opaque**: I put the pattern in a string value and
  don't think about how it interacts with the surrounding grammar. No
  escaping `$` inside Cypher strings inside JSON.
- **Schema-validatable**: A JSON Schema can catch my mistakes before execution.
  This is a tighter feedback loop than "parse error at line 3 column 12."
- **Discoverable**: An MCP `tools/list` response tells me every available
  field, its type, and its enum values. I don't need to have memorized a
  grammar.

### What I would NOT want

- Any syntax requiring me to remember **positional column ordering** (CozoScript
  relation arities, Prolog term structure).
- Any syntax where **patterns must be escaped** for embedding (Cypher string
  literals, SQL string literals, regex inside regex).
- **Indentation-sensitive** formats where a wrong indent changes semantics.
- **Multiple equivalent ways** to express the same query (the current problem
  — I'd pick the wrong one half the time).

---

## Synthesis: Two Layers, One Rule Format

*Generated by a fresh Opus agent reviewing all seven proposals.*

### The Diagnosis

The proposals correctly identify two distinct domains:

1. **Structural matching** — "find code that looks like X" (local, syntactic)
2. **Relational querying** — "find code where A flows to B, C calls D" (global, semantic)

emend already has excellent primitives for both: **patterns** for (1),
**CozoScript/Datalog** for (2). The pain is neither — it is the **four YAML
config schemas** (trace, lint, policy, dead code) that each reinvented ad-hoc
ways to express "match this, constrain by scope/flow, report a violation."

### The Design

**Layer 1: Patterns (unchanged).** `print($X)`, `$OBJ:type[Conn]`, `$...ARGS`
stay exactly as they are. They are emend's best feature.

**Layer 2: Datalog with pattern predicates (extended).** CozoScript stays as
the relational backend but gains a `*code_match` virtual relation that bridges
patterns into Datalog queries. Power-user escape hatch.

**The Glue: One unified rule format** replacing trace YAML, lint YAML, policy
YAML, and dead code config. Every rule has the same envelope (`severity`,
`message`, optional `in` file glob). The rule **kind** is determined by which
key is present:

| Key present | Rule kind | Replaces |
|-------------|-----------|----------|
| `match:` | Structural pattern match | Lint `find:` rules |
| `flow:` | Data-flow / taint analysis | Trace YAML + lint flow rules + policy flow checks |
| `deadcode:` | Unreferenced symbol detection | Dead code config section |
| `type-check:` | Type constraint checking | Policy type checks |
| `datalog:` | Raw CozoScript query | Policy custom checks |

### The Unified Rule File (.emend/rules.yaml)

```yaml
macros:
  user_input: "request.args.get($X) | request.form[$X]"

presets:
  - flask
  - sqlalchemy

rules:
  # Structural rule (was lint find: rule)
  no-print:
    match: "print($X)"
    not-inside: "def test_$_"
    in: "src/**/*.py"
    fix: "logger.info($X)"
    severity: warning
    message: "Use logger instead of print"

  # Flow rule (was trace YAML + lint flow rule + policy flow check)
  sql-injection:
    flow:
      from: "{user_input}"
      to: "cursor.execute($Q)"
      not-through: "escape($X) | parameterize($Q, $ARGS)"
      quantifier: all_paths
    interprocedural: true
    severity: error
    message: "User input flows unsanitized to SQL execution"

  # Effect-based flow (was trace effect sinks)
  toctou-orm:
    flow:
      from: "$Q.first()"
      to:
        effect: "writes($OBJ)"
      scope-boundary: "session.commit()"
    severity: error
    message: "Mutation on ORM object outside transaction boundary"

  # Dead code (was deadcode config section)
  unused-functions:
    deadcode:
      kind: function
      entry-points:
        decorators: ["app.route", "celery.task", "pytest.fixture"]
        names: ["main", "cli"]
      exclude-paths: ["tests/**", "migrations/**"]
    severity: warning
    message: "Function appears unused"

  # Datalog escape hatch (was policy custom check)
  recursive-calls:
    datalog: |
      ?[name, file, line] :=
        *call[caller, callee, file, line, _, _, _],
        caller == callee,
        *symbol[callee, _, name, _, _, _, _]
    severity: warning
    message: "Recursive function detected"
```

### What Gets Removed

- **Trace YAML** (`labels:`, `sources:`, `sinks:`, `sanitizers:`,
  `scope_sanitizers:`) → folded into `flow:` rules
- **Policy YAML** (`.emend/policies.yaml`) → folded into rules
- **Dead code config section** in `patterns.yaml` → `deadcode:` rules
- **GritQL query language** → removed; its features map to CLI flags
  (`--inside`, `--imported-from`) or `emend batch`

### Datalog + Pattern Predicates (the bridge)

The key idea from Proposals 4 and 7: patterns should be predicates in the
relational language. Extend CozoScript with a `*code_match` virtual relation:

```
# Pattern matching as a Datalog predicate
?[file, line, x_text] :=
  *code_match["print($X)", file, line, captures],
  x_text = get(captures, "X")

# Flow as reachability with pattern predicates
?[src_file, src_line, sink_line] :=
  *code_match["request.args.get($X)", src_file, src_line, _],
  *code_match["cursor.execute($Q)", src_file, sink_line, _],
  *flows_to[src_file, src_line, src_file, sink_line],
  not *sanitized_by[src_file, src_line, sink_line, "escape($X)"]
```

`*code_match` is lazily materialized: the Datalog evaluator calls emend's
pattern engine and injects results as tuples. This gives power users full
Datalog expressiveness with zero new language.

### The Concept Budget

| Concept | What it is | When you need it |
|---------|-----------|-----------------|
| **Patterns** | Code-shaped search (`print($X)`) | Always |
| **Selectors** | Location paths (`file.py::Class.method[params]`) | Navigating to specific symbols |
| **Rules** | YAML with `match:`, `flow:`, `deadcode:`, etc. | Defining checks |
| **Datalog** | CozoScript + `*code_match` | Power users only |

Four concepts, down from seven. Most users never touch Datalog or write rules
(they use presets). The common case: `emend find "print($X)"` and `emend check`.

### The CLI

```bash
# No change for simple operations
emend find "print($X)" --in "src/**" --inside "def $FUNC"
emend replace "old($X)" "new($X)" --apply

# New: emend check runs all rules
emend check                          # all rules
emend check --rule sql-injection     # specific rule
emend check --kind flow              # only flow rules

# emend lint kept as alias for backward compat
emend lint  # same as emend check

# Flow from CLI (was: emend trace --config ...)
emend flow "request.args.get($X)" --to "cursor.execute($Q)" \
  --not-through "escape($X)" --quantifier all_paths

# Datalog (was: emend facts with limited query support)
emend query '?[name, file] := *symbol[_, file, name, "function", _, _, _]'
```

### Ideas Not Covered by the Original Proposals

**1. Rule composition via `extends`.** Large codebases need layered rules:

```yaml
extends:
  - "@company/base-rules"           # installed via uv/pip
  - ./shared/strict-rules.yaml

rules:
  no-print:
    severity: error                 # override: was warning in base
```

**2. Pattern-as-selector bridging.** Pattern matches identify locations,
so `find --output selector` can pipe into selector-expecting commands:

```bash
emend find "cursor.execute($Q)" --output selector \
  | xargs -I{} emend graph {} --direction callers
```

**3. Incremental rule evaluation.** The fact graph is persistent (SQLite-backed
CozoDB). Rules could be evaluated incrementally on file save, turning emend
from a batch tool into a live analysis engine.

### Implementation Phases

1. **Unified rule schema** (low risk, high value): new YAML loader dispatching
   to existing engines. Migration tool for old configs. `emend check` command.
2. **`*code_match` virtual relation**: pattern predicates in Datalog.
3. **MCP tool schema**: generated from the unified rule/CLI schema.
4. **Deprecate old configs**: remove trace/policy YAML, GritQL language.
