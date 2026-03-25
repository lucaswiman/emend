# Semantic Program Flow: Manipulating the Platonic Forms of Code

## The Problem Statement, Felt

You're sitting there staring at code and what you SEE is not what you MEAN. You
see `def process_order(order: Order) -> Receipt:` but what you MEAN is "when a
customer wants something, figure out if we can give it to them, take their money,
and tell them it worked." The code is a corpse of the idea. A fossil. The actual
living thing is the *flow of intent through a system*.

What Engelbart was really after: the computer should be a bicycle for the mind,
not a typewriter for the mind. We're still typing. We're typing MORE. We have
better autocomplete on our typewriters. That's not the revolution.

## What emend Already Knows

emend already has bones of something deeper:

- **Fact Graph**: symbols, calls, references, taint flows, types, imports as
  *relations*. This is already a step toward the platonic — it's code-as-database,
  code-as-knowledge-graph.
- **Taint Analysis**: "where does this value GO?" — that's a *question about meaning*,
  not about syntax. You're asking about the *life of a datum*.
- **Impact Analysis**: "if I change this, what moves?" — that's a question about
  *causal structure*.
- **Equality Saturation**: "these expressions are the same" — literally finding
  platonic equivalence classes.
- **Patterns with Metavariables**: `$EXPR.close()` — you're already reaching for
  the universal, the archetype. "ANY expression, being closed."

The gap: these are all still *read-only queries* or *local rewrites*. You ask a
question, you get an answer. But you can't GRAB the flow and BEND it.

## The Vision: Flow as First-Class Object

### What if you could do this:

```
# "Show me the life of a request"
emend flow trace --from "request = Request(...)" --to "return Response(...)"

# And get back not code, but a DIAGRAM OF MEANING:
#
#   [Request Created]
#       → [Validated]
#       → [Authorized]
#       → {Branching: [Found in Cache] | [Fetched from DB]}
#       → [Transformed]
#       → [Response Created]
#
# Each node is a SEMANTIC PHASE, not a line of code.
# The system INFERRED these phases from the actual flow.
```

But that's still read-only. Here's where it gets psychedelic:

```
# "I want authorization to happen BEFORE validation"
emend flow swap "Authorized" "Validated" --in "request_handler"

# The tool UNDERSTANDS WHAT THIS MEANS and rewrites the code.
# Not by moving lines around — by understanding that "Authorized"
# corresponds to a check that currently happens after parsing,
# and it needs to restructure so the auth check can work with
# the raw/unparsed input, which might mean changing signatures...
```

### The Core Abstraction: Semantic Phases

Every program has phases. Not "lines" or "functions" or "classes" — PHASES.
A phase is:

1. A **transformation** of some data from one semantic state to another
2. With **preconditions** (what must be true for this phase to start)
3. And **postconditions** (what's true after it completes)
4. And **effects** (what it does to the world outside the data)

This is basically Hoare logic but made *tangible and manipulable*.

```yaml
# .emend/flow.yaml — you declare the MEANING, emend finds it in the code
phases:
  request_lifecycle:
    - name: parse
      transforms: "raw bytes → structured request"
      marker: "$VAR = parse($RAW)"  # pattern that identifies this phase
    - name: authenticate
      transforms: "request → authenticated request"
      marker: "$AUTH = authenticate($REQ)"
      effect: "may reject (401)"
    - name: authorize
      transforms: "authenticated request → authorized request"
      marker: "$AUTHZ = authorize($REQ, $RESOURCE)"
      effect: "may reject (403)"
    - name: execute
      transforms: "authorized request → result"
    - name: serialize
      transforms: "result → response bytes"
```

Now emend can:
- **Verify** the code actually follows this flow
- **Visualize** where reality diverges from intent
- **Refactor** by manipulating phases, not lines

### The MCP Interface: Semantic Operations

Here's what the MCP tools look like. These are operations on MEANING:

#### `flow/trace` — Follow the Life of a Value

Not grep. Not find-references. The actual semantic journey.

```json
{
  "tool": "flow/trace",
  "params": {
    "origin": "user_input",      // conceptual, not syntactic
    "destination": "database",    // where does it end up?
    "through": "request_handler", // scope
    "show": "transformations"     // what happens to it along the way
  }
}
```

Returns something like:
```
user_input (str, untrusted)
  → json.loads() → dict (parsed, still untrusted)
  → Schema.validate() → ValidatedInput (trusted structure)
  → .to_query() → SQLQuery (trusted, parameterized)
  → db.execute() → [reaches database]
```

This is taint analysis but INVERTED — instead of "find the bad paths," it's
"show me the actual path and let me reason about it." The system is your
PARTNER in understanding, not your cop.

#### `flow/assert` — Declare Invariants About Meaning

```json
{
  "tool": "flow/assert",
  "params": {
    "assertion": "no path from user_input to database without validation",
    "scope": "project"
  }
}
```

This is policy checks but expressed in NATURAL SEMANTIC LANGUAGE. The tool
figures out what "validation" means in your codebase (maybe it's a decorator,
maybe it's a function call, maybe it's a type narrowing).

#### `flow/reshape` — Change the Topology of Meaning

This is the big one. You're not editing code. You're editing the SHAPE of
what the code does.

```json
{
  "tool": "flow/reshape",
  "params": {
    "flow": "request_lifecycle",
    "operation": "insert_phase",
    "phase": {
      "name": "rate_limit",
      "after": "authenticate",
      "before": "authorize",
      "transforms": "authenticated request → rate-limited request",
      "effect": "may reject (429)"
    }
  }
}
```

emend figures out:
1. Where in the actual code "authenticate" ends and "authorize" begins
2. What the data looks like at that point (types, state)
3. Generates a skeleton for the new phase that fits the actual types
4. Inserts it, threading the data through correctly
5. Updates tests to account for the new phase

#### `concept/define` — Name a Pattern of Meaning

```json
{
  "tool": "concept/define",
  "params": {
    "name": "resource_cleanup",
    "pattern": "acquire → use → release",
    "markers": {
      "acquire": "$HANDLE = $RESOURCE.open($ARGS)",
      "release": "$HANDLE.close()"
    },
    "invariant": "release always follows acquire on all paths"
  }
}
```

Now you've defined a CONCEPT. The tool can:
- Find all instances of this concept in the codebase
- Verify the invariant holds everywhere
- When you refactor, ensure the concept is preserved
- Suggest when code SHOULD use this concept but doesn't

#### `concept/unify` — These Two Things Are the Same

```json
{
  "tool": "concept/unify",
  "params": {
    "a": "UserService.get_user(id)",
    "b": "fetch_user_from_db(user_id)",
    "assertion": "same semantic operation"
  }
}
```

The e-graph already does this for expressions. Extend it to OPERATIONS.
These two things do the same thing. The tool can now:
- Suggest consolidation
- Track them together for impact analysis
- When you change one, ask if the other should change too

#### `intent/declare` — Say What You Mean

The most Engelbart thing of all:

```json
{
  "tool": "intent/declare",
  "params": {
    "intent": "when a user uploads a file, scan it for malware before storing it",
    "scope": "upload_handler"
  }
}
```

The tool:
1. Finds the upload handler
2. Traces the flow of the uploaded file
3. Identifies where storage happens
4. Shows you the current path (file goes straight to storage? goes through
   some processing?)
5. Generates the insertion point and skeleton for a malware scan phase
6. You review and approve

You declared INTENT. The machine figured out the CODE.

## The Deeper Philosophy

### Programs Are Not Text

A program is a NETWORK OF CONSTRAINTS AND TRANSFORMATIONS. Text is just one
serialization format. The fact graph in emend is already a better
representation. But even the fact graph is too close to the code — it's
"function X calls function Y." What we want is "the authentication phase
establishes trust, which the authorization phase consumes."

### The Editor of the Future Looks Like a Map

Not lines of text scrolling. A MAP. You see:
- Regions of meaning (the auth system, the payment system, the notification system)
- Flows between them (data paths, control paths, dependency paths)
- Hotspots (high complexity, high change frequency, high coupling)
- And you can ZOOM: from the whole system down to a single expression

emend's `graph` command is the seed of this. But instead of call graphs, we
want CONCEPT graphs.

### Equality Saturation Is the Key Mechanism

The rewrite engine already knows that expressions can be equivalent. Scale this up:
- Two functions that do the same thing? Equivalent.
- Two modules that serve the same role? Equivalent.
- Two architectures that produce the same behavior? Equivalent.

The e-graph becomes the MEDIUM of thought. You explore the space of equivalent
programs, choosing the one that best expresses your intent.

## What to Build First

Concretely, for emend as an MCP server:

### Phase 1: Flow Materialization
- `flow/trace` — build on taint analysis but make it bidirectional and
  value-centric rather than taint-centric
- `flow/phases` — automatically segment a function into semantic phases
  using heuristics (variable state changes, function call boundaries,
  branch points)
- `flow/visualize` — render as structured data an AI can reason about

### Phase 2: Semantic Assertions
- `flow/assert` — declare properties about flows, checked against fact graph
- `concept/define` — register reusable semantic patterns
- `concept/find` — find all instances of a concept

### Phase 3: Semantic Manipulation
- `flow/reshape` — insert/remove/reorder phases
- `concept/unify` — declare equivalences, get consolidation suggestions
- `flow/diff` — semantic diff: not "what lines changed" but "what MEANING changed"

### Phase 4: Intent Bridge
- `intent/declare` — natural language intent → semantic operations → code changes
- This is where the LLM becomes part of the tool, not just the user of it
- The MCP server provides the SEMANTIC INFRASTRUCTURE that makes LLM code
  manipulation reliable and verifiable

## The Engelbart Connection

Engelbart's NLS wasn't just a text editor. It was a system for manipulating
STRUCTURED THOUGHT. Every piece of text was a node in a graph. You could
link anything to anything. You could create views that showed different
aspects of the same underlying structure.

What we're building: NLS for programs. The underlying structure is the
semantic flow graph. The views are: code (traditional), flow diagrams,
concept maps, impact graphs, type landscapes. The manipulation operations
work on the STRUCTURE, and all views update.

The LSD insight, if there is one: **the code and the meaning are not the
same thing, and we've been confusing them for 60 years.** The meaning is
the flow, the intent, the transformation of state through phases. The code
is just one shadow on the cave wall. emend already knows how to parse the
shadow. Now we build the tools to work with the light.

---

*"The digital computer is, above all, a tool for manipulating symbols.
We might as well manipulate the RIGHT symbols."*
