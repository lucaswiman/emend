# `semantic_context` MCP Tool — Specification

## The Core Problem

A coding agent exploring a codebase is in a dark room with a flashlight.
Each file read is one cone of light. Each grep is another. The agent
assembles understanding from pinhole glimpses and DOES NOT KNOW WHAT IT
HASN'T SEEN.

The most dangerous failures aren't wrong edits — they're edits that are
locally correct but miss non-obvious context. The function is an RPC
endpoint. The class is serialized to disk somewhere. The parameter name
appears in log parsing regexes. The test coverage has a gap exactly here.

**The tool's job is not to answer questions. It's to surface what the
agent doesn't know it needs to know.**

## Interface

### MCP Tool: `semantic_context`

```
emend semantic-context <selector> [--depth N] [--focus ASPECT] [--json]
```

**Input**: A symbol selector (same syntax as all emend commands).

**Output**: A structured semantic dossier on that symbol, organized by
what's most likely to cause an agent to make a mistake if it doesn't
know about it.

### Output Structure

```json
{
  "symbol": "app.orders.process_order",
  "kind": "function",
  "file": "app/orders.py",
  "line": 47,

  "signature": {
    "params": [
      {"name": "order", "type": "Order", "default": null}
    ],
    "returns": "Receipt",
    "decorators": ["@rpc_endpoint('orders.process')","@require_auth"],
    "is_async": false
  },

  "dangers": [
    {
      "level": "high",
      "category": "external_interface",
      "message": "Decorated with @rpc_endpoint — signature is part of wire protocol",
      "evidence": "app/orders.py:46"
    },
    {
      "level": "medium",
      "category": "dynamic_reference",
      "message": "Name 'process_order' appears as string literal in task_registry.py:23",
      "evidence": "task_registry.py:23: tasks = {'process_order': ...}"
    },
    {
      "level": "medium",
      "category": "serialization",
      "message": "Return type Receipt has __getstate__/__setstate__ — may be pickled",
      "evidence": "app/models.py:112"
    }
  ],

  "flow": {
    "data_in": [
      {
        "name": "order",
        "source_taint": ["user_input"],
        "validated_by": ["OrderSchema.validate (app/schemas.py:30)"],
        "note": "validated before reaching this function"
      }
    ],
    "data_out": [
      {
        "name": "return (Receipt)",
        "flows_to": [
          "send_confirmation_email (app/notifications.py:15)",
          "update_dashboard (app/analytics.py:88)",
          "serialize to JSON response (via @rpc_endpoint)"
        ]
      }
    ],
    "side_effects": [
      {"kind": "db_write", "target": "orders table", "evidence": "db.session.add(order_record)"},
      {"kind": "external_call", "target": "payment_gateway.charge()", "evidence": "line 62"}
    ]
  },

  "callers": [
    {"symbol": "handle_order_request", "file": "app/api.py:34", "kind": "direct"},
    {"symbol": "retry_failed_order", "file": "app/tasks.py:78", "kind": "direct"},
    {"symbol": "OrderRPCService", "file": "generated/rpc_client.py:45", "kind": "rpc"}
  ],

  "tests": {
    "direct": ["tests/test_orders.py::test_process_order_success",
               "tests/test_orders.py::test_process_order_invalid"],
    "indirect": ["tests/integration/test_order_flow.py::test_full_order_lifecycle"],
    "coverage_gaps": ["no test for payment gateway timeout",
                      "no test for concurrent duplicate orders"]
  },

  "related_symbols": [
    {"symbol": "cancel_order", "relationship": "sibling (same module, similar signature)"},
    {"symbol": "OrderStatus", "relationship": "enum consumed by process_order"},
    {"symbol": "ORDER_PROCESSING_TIMEOUT", "relationship": "constant used at line 58"}
  ],

  "history": {
    "last_modified": "2025-11-03",
    "change_frequency": "modified 4 times in last 3 months",
    "recent_changes": ["added timeout handling (abc123)", "fixed race condition (def456)"]
  }
}
```

### The `dangers` Section Is the Whole Point

Everything else is nice-to-have context. The `dangers` section is the
tool earning its keep. It surfaces things that would cause the agent to
make a mistake:

**Danger categories:**

| Category | What it catches |
|----------|----------------|
| `external_interface` | Symbol is exposed via RPC, REST, CLI, or other external API — signature changes have protocol implications |
| `dynamic_reference` | Symbol name appears as a string literal somewhere — renaming won't be caught by static analysis |
| `serialization` | Symbol or its types are serialized (pickle, JSON schema, protobuf) — structural changes may break stored data |
| `concurrency` | Symbol is called from multiple threads/processes, or uses shared mutable state |
| `global_state` | Symbol reads or writes module-level / global state |
| `monkey_patched` | Symbol is monkey-patched or mocked somewhere outside tests |
| `metaprogramming` | Symbol is created/modified by metaclass, decorator, or __init_subclass__ |
| `implicit_dependency` | Symbol depends on import-time side effects or module initialization order |
| `coverage_gap` | Critical paths through this symbol have no test coverage |
| `high_fan_out` | Symbol is called from many places — changes have wide blast radius |
| `recent_instability` | Symbol has been modified frequently recently — may be in flux |

### How It's Built (On Existing emend Primitives)

Every piece of this is already in emend, just not composed:

| Output field | Built from |
|-------------|-----------|
| `signature` | `query_symbols()` + type oracle |
| `dangers.external_interface` | Decorator pattern matching (configurable patterns in `.emend/config.toml`) |
| `dangers.dynamic_reference` | String literal scanning (already in dead-code detection) |
| `dangers.serialization` | Pattern match for `__getstate__`, `__reduce__`, schema definitions |
| `flow.data_in/out` | Taint analysis (intraprocedural, already exists) |
| `flow.side_effects` | Callees analysis + pattern matching for known effect patterns |
| `callers` | `find_callers()` (already exists) |
| `tests` | Impact analysis test detection (already exists) |
| `related_symbols` | Fact graph queries (same-module, type references) |
| `history` | `git log` integration (already in dead-code) |
| `coverage_gaps` | Heuristic: analyze branch structure vs test call patterns |

### Configuration: Teaching emend Your Codebase's Dangers

```toml
# .emend/config.toml

[semantic_context]

# Decorators that indicate external interfaces
external_interface_decorators = [
  "rpc_endpoint",
  "app.route",
  "celery.task",
  "click.command",
  "strawberry.mutation",
]

# Patterns that indicate serialization concerns
serialization_patterns = [
  "$CLS(Base)",           # SQLAlchemy models
  "@dataclass_json",
  "Schema($ARGS)",        # marshmallow
]

# Patterns that indicate shared mutable state
global_state_patterns = [
  "$MODULE.$VAR = $VAL",  # module-level assignment of mutable
]

# Custom danger rules (same pattern syntax as lint rules)
[[semantic_context.dangers]]
category = "billing"
level = "high"
pattern = "$FUNC(amount=$AMT)"
message = "Touches billing amounts — requires finance team review"

[[semantic_context.dangers]]
category = "pii"
level = "high"
pattern = "$VAR.email"
message = "Accesses PII (email) — check GDPR compliance"
```

## Usage by Agent

### Before editing: "What am I dealing with?"

```
Agent: I need to add a `priority` parameter to `process_order`.
       Let me check the semantic context first.

[calls semantic_context("app/orders.py::process_order")]

Tool returns dangers:
  - HIGH: @rpc_endpoint — this is a wire protocol change
  - MEDIUM: string reference in task_registry.py

Agent: Ah, I can't just add a parameter. I need to:
  1. Make it optional with a default (backward-compatible wire change)
  2. Update the RPC schema definition
  3. Update task_registry.py
  4. THEN update callers
```

Without semantic_context, the agent adds the parameter, updates Python
callers, and ships a breaking RPC change. With it, the agent sees the
full picture in one call and plans accordingly.

### During planning: "What's the blast radius?"

The `callers`, `flow.data_out`, and `tests` sections give the agent
a complete map of downstream effects. Combined with `dangers`, this
IS the blast_radius tool — just oriented around a symbol rather than
a proposed change.

### After editing: "Did I miss anything?"

Run semantic_context on each modified symbol. Check that all `dangers`
have been addressed. This is the verification step.

## Relationship to `blast_radius` and `reshape`

`semantic_context` is Phase 1 because it's **read-only** and builds on
existing primitives with minimal new logic. It's the foundation:

- `blast_radius` = `semantic_context` + "given this proposed change,
  which dangers and callers are affected?" (Phase 2 — adds reasoning
  about a hypothetical change)

- `reshape` = `blast_radius` + "compute the actual edits" (Phase 3 —
  adds code generation from semantic operations)

Each phase adds a layer of intelligence on top of the previous one.
You can ship Phase 1 and immediately make agents more effective, while
building toward the deeper semantic manipulation tools.

## What This Is NOT

- Not a language server. LSP gives you types and references. This gives
  you MEANING and DANGER.
- Not a linter. Linters find bugs in code. This finds gaps in the
  agent's understanding.
- Not documentation. Docs describe intended behavior. This describes
  ACTUAL behavior and its implications.

It's a **situational awareness engine for code agents**. The thing that
turns a flashlight into floodlights.
