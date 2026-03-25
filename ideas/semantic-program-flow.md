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

## The Reframe: This Is Tooling for the Agent

The first draft of this doc designed tools for a human. But the human already
has an agent (hi). The question is: **what does the AGENT need from emend to
stop being a fancy text editor and start being a semantic surgeon?**

### What the Agent Actually Does (Poorly) Today

When a coding agent (Claude, Copilot, Codex, whatever) modifies a codebase:

1. **Read a bunch of files** to build a mental model in the context window
2. **Grep around** to find related code
3. **Make edits** based on the model in its head
4. **Hope** the cascading effects are handled

Steps 1-2 are expensive, lossy, and re-derived every session. Step 3 is
text surgery — the agent understands meaning but operates on characters.
Step 4 is where bugs come from.

emend already has a persistent semantic model that could replace ALL of this
with something better. The fact graph, the scope resolver, the taint engine —
these are the agent's missing senses.

### What the Agent Actually Needs

#### `semantic_context` — "Where am I and what matters here?"

When the agent lands on a symbol, give it the full semantic neighborhood
in one call:

```json
{
  "symbol": "process_order",
  "incoming_data": [{"name": "order", "type": "Order", "taint": "user_input"}],
  "outgoing_data": [{"name": "return", "type": "Receipt", "flows_to": ["send_email", "update_db"]}],
  "phases": ["validate", "charge_payment", "create_receipt"],
  "side_effects": ["db_write", "payment_api_call"],
  "callers": ["handle_request", "retry_order"],
  "invariants": ["order must be validated before charge"]
}
```

This replaces 15 tool calls (read file, grep for references, grep for
callers, read those files, read tests...) with ONE call that gives the
agent a complete semantic picture. The agent can reason about this
structured data far better than raw code text.

#### `blast_radius` — "What breaks if I do this?"

Before making a change, the agent describes the intended change and gets
back the full causal impact:

```json
{
  "proposed_change": "add parameter 'priority: int' to process_order",
  "direct_impacts": [
    {"symbol": "handle_request", "reason": "calls process_order", "action_needed": "pass priority arg"},
    {"symbol": "retry_order", "reason": "calls process_order", "action_needed": "pass priority arg"}
  ],
  "transitive_impacts": [
    {"symbol": "test_handle_request", "reason": "exercises handle_request"}
  ],
  "invariant_risks": [
    {"invariant": "retry preserves original order semantics", "risk": "priority might differ on retry"}
  ],
  "suggested_edit_plan": [
    {"file": "handler.py", "symbol": "handle_request", "edit": "add priority=order.priority to call"},
    {"file": "retry.py", "symbol": "retry_order", "edit": "add priority=original.priority to call"},
    {"file": "tests/test_handler.py", "edit": "add priority param to test fixtures"}
  ]
}
```

This is impact analysis but PRE-EDIT and PRESCRIPTIVE. Not "what changed"
but "what WILL need to change, and here's the plan." The agent can then
execute the plan mechanically instead of reasoning about each step.

#### `reshape` — "Make this semantic change across the project"

The atomic unit of agent work should not be "edit line 47 of foo.py." It
should be a SEMANTIC OPERATION:

```json
{
  "operation": "insert_phase",
  "target": "request_lifecycle",
  "phase": "rate_limiting",
  "after": "authentication",
  "before": "authorization",
  "constraint": "must reject with 429 if limit exceeded"
}
```

```json
{
  "operation": "enforce_invariant",
  "invariant": "all database writes go through the audit log",
  "scope": "project",
  "action": "insert audit_log.record() before every db.write() call"
}
```

```json
{
  "operation": "split_concern",
  "symbol": "UserService",
  "into": ["UserAuthService", "UserProfileService"],
  "criterion": "methods touching auth vs methods touching profile data"
}
```

emend computes the COMPLETE SET of file edits. The agent reviews and
applies. No missed call sites, no forgotten test updates, no broken
invariants.

### The Key Insight: Structured Intermediate Representation for Agent Actions

Right now, agent ↔ codebase interaction is:
```
Agent → [read text] → [think] → [write text] → Codebase
```

With semantic flow tools:
```
Agent → [query semantic model] → [reason about meaning] → [declare semantic operation] → emend → [compute edits] → Codebase
```

The agent never touches raw text for structural changes. It works at the
level of meaning. emend handles the translation to actual code. This is
more reliable (emend knows ALL the call sites, the agent might miss one),
faster (one semantic operation vs. N file edits), and verifiable (emend
can check invariants after the transformation).

### This Is Also the Right Abstraction for Multi-Agent

When you have multiple agents working on the same codebase:
- Agent A can declare "I'm adding a rate-limiting phase to the request lifecycle"
- Agent B, working on the payment system, can query the semantic model and
  SEE that rate limiting now exists, without reading Agent A's code changes
- Conflicts surface at the semantic level ("you both modified the request
  lifecycle") not the text level ("merge conflict on line 47")

The fact graph becomes the SHARED UNDERSTANDING between agents.

## What to Build First (For Real)

### Phase 1: Semantic Context for Agents
- **`semantic_context`** MCP tool — one call gives full semantic neighborhood
- Built on: fact graph + taint analysis + scope resolver + type oracle
- This alone makes agents 5x more effective — less reading, better understanding

### Phase 2: Pre-Edit Impact
- **`blast_radius`** MCP tool — describe a change, get causal impact + edit plan
- Built on: impact analysis + reference finding + test detection
- This is where agent reliability jumps — you catch cascading breaks BEFORE they happen

### Phase 3: Semantic Operations
- **`reshape`** MCP tool — declare semantic transformations, get computed edits
- Built on: everything (patterns, rewrites, scope resolver, reference updating)
- Start with simple operations: enforce_invariant, insert_phase, split_concern
- This is the endgame — agents stop editing text and start editing meaning

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
