# Feature Ideas for Agentic Coding Workflows

Emend already provides a powerful foundation for AI coding agents: structured edits that
don't require rewriting entire files, scope-aware cross-project refactoring, pattern
matching with metavariables, and Rust-accelerated search. These features let agents make
precise, surgical changes rather than blunt file rewrites.

The ideas below focus on what's *missing* — capabilities that would let agents plan
changes more confidently, understand code more deeply, and compose multi-step refactorings
safely.

---

## 1. Change Impact Analysis (`emend impact`)

**Problem:** Before an agent renames a symbol, changes a function signature, or moves a
module, it needs to know what will break. Today it can use `refs` to find references, but
that doesn't tell it *what kind* of breakage to expect — will call sites need new
arguments? Will type annotations become invalid? Will re-exports stop working?

**Proposed feature:**
```bash
emend impact models.py::User --rename Account
emend impact utils.py::parse_date[params] --remove timeout
emend impact utils.py --move helpers/utils.py
```

Output a structured report:
```json
{
  "direct_references": 42,
  "call_sites_needing_update": 15,
  "type_annotations_affected": 8,
  "re_exports_broken": ["api/__init__.py"],
  "subclasses_affected": ["AdminUser", "GuestUser"],
  "string_references": ["serializers.py:23"],
  "test_files_affected": ["tests/test_models.py"]
}
```

**Why agents need this:** Agents can use impact analysis to decide whether a refactoring
is safe to attempt autonomously, or whether it needs human approval. It lets the agent
estimate the blast radius before touching anything.


## 2. Dependency Graph / Reverse Dependencies (`emend deps`)

**Problem:** `graph` gives call graphs, but agents often need higher-level dependency
information: which modules import this one? Which symbols does this function transitively
depend on? What's the "dependency cone" of a given symbol?

**Proposed feature:**
```bash
# What does this symbol depend on?
emend deps utils.py::parse_date --direction down --depth 2

# What depends on this symbol?
emend deps utils.py::parse_date --direction up --json

# Module-level: which modules import this one (transitively)?
emend deps utils.py --direction up --transitive
```

**Why agents need this:** When an agent is asked to "refactor the date parsing logic," it
needs to understand the full dependency cone to plan the refactoring. Reverse dependencies
tell it what might break; forward dependencies tell it what it needs to understand.


## 3. Safe Delete (`emend delete`)

**Problem:** Agents frequently need to remove dead code, but removing a symbol might leave
behind orphaned imports, helper functions that only it called, or type definitions that
only it used. Today, `deadcode` finds unreferenced code but doesn't connect it to
"removing X would make Y dead too."

**Proposed feature:**
```bash
emend delete models.py::LegacyUser --apply
```

This would:
1. Remove the symbol itself
2. Remove now-unused imports in the same file
3. Report (or optionally remove) newly-dead symbols elsewhere
4. Clean up `__all__` entries
5. Remove the symbol from re-export `__init__.py` files

```bash
emend delete models.py::LegacyUser --cascade --dry-run
# Shows everything that would be removed transitively
```

**Why agents need this:** "Remove this dead code" is a common instruction, and doing it
correctly requires chasing transitive effects. An agent shouldn't leave behind a trail of
newly-orphaned imports and helpers.


## 4. Extract Function / Extract Variable (`emend extract`)

**Problem:** Agents often need to break apart large functions — extracting a block of code
into a helper, or pulling a repeated expression into a variable. This is one of the most
common refactorings but currently requires the agent to manually rewrite the code.

**Proposed feature:**
```bash
# Extract lines 42-58 of handler.py into a new function
emend extract handler.py:42-58 --name validate_input --apply

# Extract an expression into a variable
emend extract handler.py:35 --expr "config['timeout'] * 1000" --name timeout_ms --apply
```

The extract-function command would:
1. Identify variables read (parameters) and written (return values) in the range
2. Generate a function signature with the right parameters
3. Replace the original code with a call to the new function
4. Handle `return`, `yield`, `await` correctly

**Why agents need this:** Extract-function is the bread and butter of making code more
modular. Agents can identify the *intent* (this block should be its own function) but need
tool support for the *mechanics* (what are the free variables? what gets returned?).


## 5. Inline Function / Inline Variable (`emend inline`)

**Problem:** The inverse of extract — sometimes an agent needs to inline a trivial helper
to simplify code, or inline a variable that's used exactly once.

**Proposed feature:**
```bash
emend inline utils.py::is_valid --apply    # Inline function body at all call sites
emend inline utils.py:42 --var result --apply  # Inline variable at usage sites
```

**Why agents need this:** Agents doing cleanup or simplification work need both directions
— extract to add structure, inline to remove unnecessary indirection.


## 6. Structured JSON Output for All Commands (`--json` everywhere)

**Problem:** Some commands output structured JSON (`deadcode --json`, `refs --json`), but
many don't. Agents parsing text output is fragile and wastes tokens. Every command should
have a machine-readable output mode.

**Proposed feature:**
```bash
emend search 'print($X)' src/ --json
# [{"file": "app.py", "line": 23, "match": "print(result)", "captures": {"X": "result"}}]

emend edit file.py::func[params] "x: int" --json --apply
# {"file": "file.py", "applied": true, "diff": "...", "symbols_modified": ["func"]}

emend rename models.py::User --to Account --json --apply
# {"files_modified": ["models.py", "views.py"], "references_updated": 15, "diffs": {...}}
```

**Why agents need this:** Agents shouldn't parse diffs or text tables. Structured output
lets agents feed results directly into their next planning step. This is probably the
single highest-leverage change for agentic adoption.


## 7. Convention Detection (`emend conventions`)

**Problem:** When an agent adds new code to a codebase, it should follow existing patterns
— naming conventions, decorator patterns, import styles, error handling patterns. Today
agents infer this from examples, which is token-expensive and unreliable.

**Proposed feature:**
```bash
emend conventions src/ --kind naming
# {"functions": "snake_case", "classes": "PascalCase", "constants": "UPPER_SNAKE"}

emend conventions src/ --kind patterns
# {"error_handling": "try/except with logging", "decorators_common": ["@router.get", "@inject"]}

emend conventions src/ --kind imports
# {"style": "absolute", "grouping": "stdlib/third-party/local", "tool": "isort"}
```

**Why agents need this:** Agents produce better code when they understand the codebase's
conventions. Instead of the agent reading 10 files to figure out naming conventions, emend
can analyze the entire codebase in seconds and report patterns.


## 8. Scope Context at a Point (`emend context`)

**Problem:** When an agent is about to insert code at a specific location, it needs to
know what names are in scope — local variables, function parameters, imported names,
enclosing class attributes. This prevents the agent from introducing name collisions or
using undefined variables.

**Proposed feature:**
```bash
emend context handler.py:45
# {
#   "locals": ["request", "user_id", "db"],
#   "params": ["self", "request"],
#   "enclosing_class": "UserHandler",
#   "class_attrs": ["model", "serializer_class"],
#   "imports": {"json": "json", "User": "models.User", ...},
#   "enclosing_scopes": ["UserHandler", "get"]
# }
```

**Why agents need this:** This prevents a whole class of agent errors: using a variable
that isn't in scope, shadowing an important name, or importing something that's already
available. It's a guardrail that makes agent-generated code more correct.


## 9. Semantic Diff (`emend diff`)

**Problem:** When reviewing a change (its own or a human's), an agent needs to understand
*what changed structurally*, not just which lines differ. A textual diff can't easily
answer "which function signatures changed?" or "were any new public symbols added?"

**Proposed feature:**
```bash
emend diff HEAD~1..HEAD
# {
#   "symbols_added": ["utils.py::validate_email"],
#   "symbols_removed": [],
#   "signatures_changed": [
#     {"symbol": "utils.py::parse_date", "change": "added param 'strict: bool = False'"}
#   ],
#   "imports_changed": [
#     {"file": "app.py", "added": ["from utils import validate_email"]}
#   ]
# }

emend diff --staged   # What's about to be committed
emend diff branch..main  # What changed between branches
```

**Why agents need this:** After making changes, agents need to verify they did what was
intended and *only* what was intended. Semantic diff answers "did I accidentally change any
other function signatures?" without parsing raw diffs.


## 10. Multi-Step Refactoring Plans (`emend plan`)

**Problem:** Complex refactorings involve multiple coordinated steps. An agent might need
to: (1) extract an interface, (2) rename the concrete class, (3) update imports, (4)
adjust type annotations. If step 3 fails, steps 1-2 need to be rolled back. There's no
transactional model for multi-step refactorings.

**Proposed feature:**
```yaml
# refactoring.yaml
name: extract-interface
steps:
  - copy-to: models.py::User -> interfaces.py --name IUser
  - edit: interfaces.py::IUser[body] --keep-signatures-only
  - add: models.py::User[bases] "IUser" --before 0
  - replace: "$X: User" "$X: IUser" src/ --where "param"
  - add: "{interfaces.py::IUser -> file}[imports]" "from interfaces import IUser"
```

```bash
emend plan refactoring.yaml --dry-run   # Preview all changes
emend plan refactoring.yaml --apply     # Apply atomically (rollback on failure)
emend plan refactoring.yaml --validate  # Check steps are consistent without applying
```

This extends `batch` with:
- Step dependencies and ordering
- Rollback on failure
- Cross-step variable passing (output of step 1 feeds step 2)
- Validation mode that checks consistency without applying

**Why agents need this:** Agents plan multi-step refactorings but execute them one at a
time, risking partial completion. A plan-based model lets the agent define the full
transformation upfront, validate it, and apply it atomically.


## 11. API Surface Analysis (`emend api`)

**Problem:** When an agent needs to understand a module's public interface — what it
exports, what the stable API is — it has to read files and infer intent from `__all__`,
naming conventions, and documentation. This is exactly the kind of structural analysis
emend should provide.

**Proposed feature:**
```bash
emend api src/emend/ --json
# {
#   "public_functions": [...],
#   "public_classes": [...],
#   "re_exports": {"__init__.py": [...]},
#   "__all__": [...],
#   "private_but_imported_externally": [...]
# }

emend api src/models.py --diff HEAD~5
# Shows API changes over last 5 commits: added/removed/modified public symbols
```

**Why agents need this:** When an agent is asked to "add a feature to the API" or "check
for breaking changes," it needs a clear picture of what the public API *is*. This also
feeds into impact analysis — changing a public symbol is higher-risk than a private one.


## 12. Type-Aware Pattern Matching (pyright/mypy integration)

**Problem:** Current pattern constraints are syntactic (`$X:int` matches integer
*literals*, not expressions that *evaluate to* int). Agents doing sophisticated
refactoring need type information: "find all functions that return Optional[str]" or
"replace all Dict[str, Any] with TypedDict."

**Proposed feature:**
```bash
# Find functions whose return type resolves to Optional
emend search --returns-type "Optional[str]" src/

# Find calls where the first argument is a string type (not just a string literal)
emend search '$func($arg:type[str], $...rest)' src/

# Replace dict access with TypedDict
emend replace '$d:type[dict][$key:str]' '$d.$key' src/ --apply
```

This would integrate with pyright's type stubs or mypy's type cache to resolve actual
types, not just syntactic patterns.

**Why agents need this:** Type information is crucial for confident refactoring. "Change
all functions that return User to return UserResponse" is a type-level query that syntax
alone can't answer.


## 13. Symbol Documentation & Usage Extraction (`emend docs`)

**Problem:** When an agent encounters an unfamiliar symbol, it needs to quickly understand
what it does: its docstring, signature, type annotations, and how it's actually used in
the codebase. Today this requires multiple commands.

**Proposed feature:**
```bash
emend docs utils.py::parse_date
# {
#   "signature": "def parse_date(s: str, strict: bool = False) -> datetime",
#   "docstring": "Parse a date string in ISO 8601 format...",
#   "overloads": [],
#   "usage_examples": [
#     {"file": "handler.py", "line": 45, "code": "dt = parse_date(request.date_str)"},
#     {"file": "importer.py", "line": 112, "code": "parse_date(row['date'], strict=True)"}
#   ],
#   "callers_count": 12,
#   "test_count": 3,
#   "first_defined_in_commit": "abc123"
# }
```

**Why agents need this:** This is the "explain this symbol" command that agents need
constantly. Instead of reading the file, finding references, checking tests — one command
gives a complete picture. It saves tokens and planning steps.


## 14. Insertion Point Intelligence (`emend where-to-add`)

**Problem:** When an agent needs to add a new function, class, or import, it needs to
decide *where* in the file to put it. Import grouping conventions, logical ordering of
functions, and class method organization all matter. Agents currently guess or append to
the end.

**Proposed feature:**
```bash
emend where-to-add utils.py --kind function --name validate_email
# {"suggested_line": 145, "reason": "after validate_phone, before format_email",
#  "section": "validation helpers"}

emend where-to-add utils.py --kind import --module "re"
# {"suggested_line": 3, "reason": "stdlib imports group, after 'os', before 'sys'"}

emend where-to-add models.py::User --kind method --name "validate"
# {"suggested_line": 67, "reason": "after other validation methods"}
```

**Why agents need this:** Bad placement makes code harder to read. Agents that drop new
functions at the end of files or put imports in the wrong group create technical debt. This
command encodes placement conventions so the agent doesn't have to infer them.


## 15. Test-Symbol Mapping (`emend test-map`)

**Problem:** After modifying a symbol, an agent needs to know which tests to run. Running
the full test suite is wasteful; the agent should run the targeted tests that exercise the
changed code. Currently there's no way to go from "I changed parse_date" to "run
test_parse_date_iso and test_parse_date_invalid."

**Proposed feature:**
```bash
emend test-map utils.py::parse_date
# {
#   "direct_tests": ["tests/test_utils.py::test_parse_date_iso", ...],
#   "indirect_tests": ["tests/test_handler.py::test_create_event"],
#   "suggested_command": "pytest tests/test_utils.py::test_parse_date_iso tests/test_utils.py::test_parse_date_invalid"
# }

emend test-map --changed   # Map all modified symbols to their tests
```

This works by combining `refs` (finding test files that reference the symbol) with naming
heuristics (`test_<symbol_name>`) and call graph analysis.

**Why agents need this:** Targeted test execution is essential for fast feedback loops.
An agent that changes 3 functions and runs only the relevant 8 tests instead of 500 gets
feedback in seconds instead of minutes.


## 16. Patch Application from Description (`emend apply-patch`)

**Problem:** Agents often express changes as diffs or patch descriptions. If an agent
has a unified diff (from a conversation, PR review, or its own planning), it should be
able to apply it through emend for validation and conflict detection.

**Proposed feature:**
```bash
# Apply a unified diff through emend (with structural validation)
emend apply-patch changes.diff --validate --apply

# Apply changes described as selector operations
echo 'edit file.py::func[returns] "int"' | emend apply-patch --from-commands --apply
```

The `--validate` flag would check that the patch makes sense structurally: does the symbol
still exist? Are the line numbers plausible? Is the result syntactically valid Python?

**Why agents need this:** Agents working in multi-turn conversations accumulate planned
changes. Being able to express and apply those changes through emend (with structural
validation) is safer than raw text patching.


## 17. Codebase Health Dashboard (`emend health`)

**Problem:** When an agent starts working on a codebase, it needs to quickly assess the
state of things: how much dead code exists, what's the complexity distribution, are there
circular imports, what's untested?

**Proposed feature:**
```bash
emend health src/ --json
# {
#   "total_symbols": 847,
#   "dead_code_count": 23,
#   "circular_imports": [["a.py", "b.py"]],
#   "avg_function_length": 18,
#   "long_functions": [{"symbol": "process_data", "lines": 245}],
#   "deeply_nested": [{"symbol": "validate", "max_depth": 7}],
#   "missing_type_hints": 134,
#   "missing_docstrings": 89,
#   "test_coverage_estimate": "67%"
# }
```

**Why agents need this:** This gives an agent a quick "lay of the land" assessment. When
asked to "improve code quality," the agent can use this to prioritize: attack the 245-line
function first, then the circular imports, then the dead code.


---

## Priority Ranking for Agentic Workflows

Ordered by impact-to-implementation-effort ratio:

| Priority | Feature | Effort | Impact |
|----------|---------|--------|--------|
| 1 | Structured JSON output everywhere (#6) | Low | Very High |
| 2 | Change impact analysis (#1) | Medium | Very High |
| 3 | Safe delete with cascade (#3) | Medium | High |
| 4 | Symbol docs & usage extraction (#13) | Low | High |
| 5 | Test-symbol mapping (#15) | Medium | High |
| 6 | Semantic diff (#9) | Medium | High |
| 7 | Dependency graph (#2) | Medium | High |
| 8 | Scope context (#8) | Medium | Medium |
| 9 | Extract function (#4) | High | High |
| 10 | Multi-step plans (#10) | High | High |
| 11 | Convention detection (#7) | Medium | Medium |
| 12 | API surface analysis (#11) | Low | Medium |
| 13 | Insertion point intelligence (#14) | Medium | Medium |
| 14 | Inline function (#5) | High | Medium |
| 15 | Codebase health dashboard (#17) | Medium | Medium |
| 16 | Type-aware matching (#12) | Very High | High |
| 17 | Patch application (#16) | Medium | Low |

The top 3 — structured JSON output, impact analysis, and safe delete — would
fundamentally change how useful emend is for AI agents. JSON output is table stakes for
any tool-using agent. Impact analysis is what lets agents act *confidently*. Safe delete
turns "find dead code" from a report into an action.


---

## Additional Ideas


## 18. MCP Server Mode (`emend serve`)

**Problem:** AI agents today invoke emend as a subprocess, paying startup cost on every
call (Python interpreter, import chain, file discovery). More importantly, the agent
framework has to map between its internal tool-call format and shell command strings, which
is lossy and error-prone. The Model Context Protocol (MCP) defines a standard way for
agents to discover and call tools with typed parameters.

**Proposed feature:**
```bash
emend serve --mcp --stdio
emend serve --mcp --port 8080
```

This exposes every emend command as an MCP tool with a JSON Schema for its parameters:
```json
{
  "name": "emend_search",
  "description": "Search for code patterns or symbols",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string"},
      "path": {"type": "string"},
      "output": {"enum": ["code", "location", "selector", "summary", "json"]},
      "kind": {"type": "string"},
      "name": {"type": "string"}
    },
    "required": ["query"]
  }
}
```

**Why agents need this:**
- Zero startup latency after initial connection (warm caches, loaded modules)
- Native typed parameters instead of string-based CLI invocation
- Persistent file cache and parse cache across calls (huge speedup for multi-step workflows)
- Automatic tool discovery — the agent doesn't need to know emend's CLI syntax
- Standard protocol means any MCP-compatible agent (Claude, Cursor, Windsurf, etc.) can
  use it without custom integration


## 19. Structural Validation / Invariant Checking (`emend check`)

**Problem:** After an agent modifies code, how does it know the result is structurally
valid? Python's syntax check only catches parse errors. Agents need to verify higher-level
invariants: "every route handler has an `@auth` decorator," "all dataclass fields have
type annotations," "no function exceeds 50 lines."

**Proposed feature:**
```yaml
# .emend/invariants.yaml
invariants:
  auth-required:
    find: "def $handler($...args)"
    inside: "@app.route"
    not-inside: "@auth_required"
    message: "Route handler missing @auth_required decorator"

  typed-dataclass-fields:
    find: "$field = field($...args)"
    inside: "@dataclass"
    not-inside: "$field: $type"
    message: "Dataclass field missing type annotation"

  max-function-length:
    selector: "**/*.py::*"
    kind: function
    max-lines: 50
    message: "Function exceeds 50 lines"
```

```bash
emend check src/                      # Check all invariants
emend check src/ --rule auth-required  # Check specific rule
emend check --changed                 # Check only modified files (for pre-commit)
```

**Why agents need this:** This is a "did I break any rules?" check that agents can run
after every edit. It's like lint, but for *structural* invariants that the project defines.
An agent that runs `emend check` after each change catches problems immediately rather
than at PR review time. It's also a way for humans to define guardrails: "the agent should
never remove an `@auth_required` decorator."


## 20. Code Clone Detection (`emend clones`)

**Problem:** Agents frequently introduce duplicate code, and existing codebases often have
near-duplicate functions that should be consolidated. Detecting structural similarity
(not just textual similarity) requires AST-level comparison.

**Proposed feature:**
```bash
emend clones src/ --min-lines 10 --json
# [
#   {
#     "group": 1,
#     "similarity": 0.92,
#     "instances": [
#       {"file": "handlers/user.py", "symbol": "validate_user", "lines": "23-45"},
#       {"file": "handlers/admin.py", "symbol": "validate_admin", "lines": "12-34"}
#     ],
#     "differences": ["variable names", "one extra condition in admin version"]
#   }
# ]

# Find code similar to a specific function
emend clones src/ --like handlers/user.py::validate_user --threshold 0.8
```

This uses structural hashing of the AST (normalizing variable names and literals) to find
functions that have the same control flow but different names.

**Why agents need this:** When asked to "reduce duplication" or "clean up the codebase,"
agents need to find the duplicates first. Clone detection also helps agents avoid
*creating* duplicates — before writing a new helper, check if a similar one already exists.


## 21. Scaffold / Stub Generation (`emend scaffold`)

**Problem:** When an agent needs to implement a protocol, abstract base class, or
interface, it should be able to generate a complete stub with all required methods, correct
signatures, and `raise NotImplementedError` bodies.

**Proposed feature:**
```bash
# Generate stub implementing a Protocol
emend scaffold protocols.py::Cacheable --output stubs.py --apply
# Creates class with all required methods stubbed out

# Generate test stubs for all public methods of a class
emend scaffold models.py::UserService --test --output tests/test_user_service.py --apply
# Creates test_<method> for each public method

# Generate __init__ from dataclass-like fields
emend scaffold models.py::Config --init --apply
```

**Why agents need this:** Agents spend many tokens figuring out what methods a protocol
requires and writing boilerplate stubs. Emend already has the AST machinery to extract
method signatures from a base class or protocol — it just needs to generate the stubs.


## 22. Diff Minimization (`emend minimize-diff`)

**Problem:** Agents often produce correct but unnecessarily large diffs — reformatting
lines they didn't need to touch, reordering imports unnecessarily, adding blank lines.
This makes code review harder and increases merge conflict risk. Given a desired end state,
emend could compute the *minimal* set of changes needed.

**Proposed feature:**
```bash
# Given a file the agent wants to write, compute minimal diff from current state
emend minimize-diff models.py --target models_new.py --apply

# Or from stdin
cat models_new.py | emend minimize-diff models.py --apply
```

This performs AST-aware diffing: if only a function body changed, the diff only shows that
function. Whitespace-only changes outside modified regions are discarded. Import reordering
that doesn't change semantics is suppressed.

**Why agents need this:** The #1 complaint about AI-generated code changes is unnecessary
churn. This tool lets an agent write the file it *wants* and then compute the minimal edit
to get there, keeping diffs clean and reviewable.


## 23. Code Provenance / Symbol History (`emend history`)

**Problem:** Agents often need to understand *why* code looks the way it does — when was
this function last changed? Who wrote it? What was the commit message? `git blame` gives
line-level attribution, but agents need *symbol-level* history.

**Proposed feature:**
```bash
emend history utils.py::parse_date --json
# {
#   "created": {"commit": "abc123", "date": "2024-01-15", "author": "alice", "message": "Add date parsing"},
#   "last_modified": {"commit": "def456", "date": "2024-11-03", "message": "Add strict mode"},
#   "modifications": [
#     {"commit": "def456", "date": "2024-11-03", "summary": "added param 'strict'"},
#     {"commit": "789ghi", "date": "2024-06-20", "summary": "fixed timezone handling"}
#   ],
#   "age_days": 410,
#   "churn_count": 3
# }
```

**Why agents need this:** When deciding whether to refactor something, context matters.
A function modified 12 times in the last month is probably under active development — the
agent should be careful. A function untouched for 3 years might be stable and well-tested,
or it might be abandoned. History gives agents judgment.


## 24. Workspace Snapshots for Exploratory Refactoring (`emend snapshot`)

**Problem:** Agents sometimes need to *try* a refactoring to see if it works — apply it,
run tests, and roll back if tests fail. Git stash/branch works but is heavyweight and
agents don't always manage git state cleanly.

**Proposed feature:**
```bash
emend snapshot save "before-refactoring"   # Save current state
emend rename models.py::User --to Account --apply
make test                                  # Did it break?
emend snapshot restore "before-refactoring"  # Roll back if needed
emend snapshot list                        # See saved snapshots
emend snapshot diff "before-refactoring"   # What changed since snapshot?
```

Implementation: thin wrapper around `git stash` or a shadow copy of modified files, but
with a cleaner API that agents can use without managing git state.

**Why agents need this:** Exploratory refactoring — "try this and see if it works" — is a
natural pattern for agents. Snapshots make it safe by guaranteeing easy rollback without
the agent needing to understand git branching.


## 25. Bulk Rename with Mapping (`emend rename-map`)

**Problem:** Sometimes a refactoring requires renaming many symbols at once in a
coordinated way — renaming an enum's values, renaming multiple related functions, or
applying a naming convention change. Doing them one by one risks inconsistency if later
renames conflict with earlier ones.

**Proposed feature:**
```bash
# From a mapping file
emend rename-map mappings.yaml --apply

# mappings.yaml:
# renames:
#   - from: models.py::UserStatus.ACTIVE
#     to: ENABLED
#   - from: models.py::UserStatus.INACTIVE
#     to: DISABLED
#   - from: models.py::UserStatus.PENDING
#     to: AWAITING_APPROVAL

# Or inline for simple cases
emend rename-map models.py::UserStatus --case snake_to_pascal --apply
```

The key difference from running `rename` in a loop: all renames are computed against the
*original* code, so renaming A→B and B→C doesn't accidentally turn A into C.

**Why agents need this:** Agents doing large-scale renaming (migrating naming conventions,
refactoring enums, aligning names with a new schema) need atomicity. Sequential renames
are fragile and can introduce conflicts.


## 26. Natural Language Search Compilation (`emend search --nl`)

**Problem:** Agents internally think in natural language ("find all functions that take a
database connection and return a list") but have to translate that into emend's pattern
syntax or flag combinations. This translation step is error-prone and wastes agent
reasoning tokens.

**Proposed feature:**
```bash
emend search --nl "functions that take a database connection parameter" src/
# Internally compiles to: emend search '**/*.py::*' --has-param db --kind function
# Or: emend search 'def $func($...args, db: $T, $...rest)' src/

emend search --nl "classes that inherit from BaseModel but don't have a Meta class" src/
# Compiles to a combination of selector queries with filtering
```

This could use a small local model or a rule-based compiler to translate natural language
descriptions into emend's query language, then execute the compiled query.

**Why agents need this:** This is a force multiplier — agents can express intent directly
and emend figures out the right query. It also makes emend accessible to agents that
haven't been fine-tuned on emend's specific syntax.


## 27. Cross-File Atomic Edits (`emend transaction`)

**Problem:** Many refactorings require editing multiple files atomically — adding a
parameter to a function AND updating all its call sites, or adding a method to a class AND
updating the protocol it implements. If the agent edits file A but crashes before editing
file B, the codebase is in an inconsistent state.

**Proposed feature:**
```bash
emend transaction begin
emend edit models.py::User.save[params] "validate: bool = True" --apply
emend replace 'user.save()' 'user.save(validate=True)' src/ --apply
emend edit protocols.py::Persistable.save[params] "validate: bool = True" --apply
emend transaction commit
# All changes applied atomically, or all rolled back on error

emend transaction rollback  # Undo everything since begin
```

**Why agents need this:** Atomicity is the difference between "refactoring tool" and
"reliable refactoring tool." Agents making multi-file changes need the guarantee that
either all changes succeed or none do. This is especially important when the agent is
running autonomously without human oversight.


## 28. Contextual Code Completion Hints (`emend complete`)

**Problem:** When an agent is writing new code at a specific location, it benefits from
knowing what's available: what methods does this object have? What are the common patterns
for calling this function? What arguments does this decorator expect?

**Proposed feature:**
```bash
emend complete models.py:45 --after "user."
# {
#   "methods": ["save", "delete", "refresh", "validate"],
#   "properties": ["id", "name", "email", "is_active"],
#   "common_patterns": [
#     "user.save(commit=True)",
#     "user.validate(raise_errors=True)"
#   ]
# }

emend complete models.py:10 --decorator "@"
# {
#   "available_decorators": ["staticmethod", "classmethod", "property", "cached_property"],
#   "project_decorators": ["@login_required", "@cache(ttl=300)", "@retry(max_attempts=3)"]
# }
```

**Why agents need this:** This is LSP-style completion but designed for batch/offline use
by agents rather than keystroke-by-keystroke IDE use. It gives agents the same contextual
awareness that a human developer gets from their IDE's autocomplete.


## 29. Change Description Generation (`emend describe`)

**Problem:** After an agent makes changes, it needs to generate commit messages, PR
descriptions, and changelog entries. These should be structural ("added parameter
`strict` to `parse_date`") not just textual ("modified line 23 of utils.py").

**Proposed feature:**
```bash
emend describe --staged
# "Added parameter `strict: bool = False` to `utils.parse_date()`.
#  Updated 8 call sites in handlers/ to pass `strict=True`.
#  Added test `test_parse_date_strict` in tests/test_utils.py."

emend describe HEAD~3..HEAD --format changelog
# - Added `validate_email()` function to utils module
# - Renamed `User` to `Account` across 12 files
# - Removed deprecated `LegacySerializer` class

emend describe HEAD~1..HEAD --format commit-message
# "feat(utils): add strict mode to parse_date
#
#  Add a `strict` parameter that raises ValueError on ambiguous dates.
#  Update all existing call sites to use non-strict mode (preserving behavior)."
```

**Why agents need this:** Agents write terrible commit messages when they describe raw line
changes. Structural change descriptions ("added parameter X to function Y") are more
useful for both humans and other agents reviewing the changes later.


## 30. Emend as a Linter for Agent-Generated Code (`emend review`)

**Problem:** Agent-generated code frequently has specific categories of problems: unused
imports, shadowed variables, inconsistent naming, missing error handling that the rest of
the codebase uses, parameters added without updating docstrings. A purpose-built review
command could catch these patterns.

**Proposed feature:**
```bash
emend review --staged --json
# {
#   "issues": [
#     {"severity": "warning", "file": "utils.py", "line": 5,
#      "message": "Import 'os' added but unused"},
#     {"severity": "style", "file": "utils.py", "line": 23,
#      "message": "New function 'validate' doesn't match project naming pattern 'validate_*'"},
#     {"severity": "warning", "file": "utils.py", "line": 30,
#      "message": "Added parameter 'strict' but docstring not updated"},
#     {"severity": "info", "file": "utils.py", "line": 23,
#      "message": "Similar function 'validate_input' exists in handlers/validation.py"}
#   ]
# }

emend review --diff HEAD~1..HEAD  # Review specific changes
```

This combines several analyses:
- Unused import detection (within the diff)
- Naming convention conformance
- Docstring/signature sync
- Duplicate/similar code detection
- Missing test coverage for new symbols

**Why agents need this:** Self-review is a critical agentic capability. If the agent can
run `emend review` on its own changes and fix the issues before presenting them to the
human, the quality bar goes up significantly. It's the equivalent of a human developer
doing a self-review before requesting PR review.


---

## Updated Priority Ranking

| Priority | Feature | Effort | Impact | Category |
|----------|---------|--------|--------|----------|
| 1 | Structured JSON output (#6) | Low | Very High | Foundation |
| 2 | MCP Server mode (#18) | Medium | Very High | Integration |
| 3 | Change impact analysis (#1) | Medium | Very High | Planning |
| 4 | Safe delete with cascade (#3) | Medium | High | Action |
| 5 | Agent code review (#30) | Medium | High | Validation |
| 6 | Symbol docs & usage (#13) | Low | High | Understanding |
| 7 | Structural invariants (#19) | Low | High | Validation |
| 8 | Test-symbol mapping (#15) | Medium | High | Validation |
| 9 | Semantic diff (#9) | Medium | High | Validation |
| 10 | Dependency graph (#2) | Medium | High | Understanding |
| 11 | Diff minimization (#22) | Medium | High | Quality |
| 12 | Change description (#29) | Low | Medium | Quality |
| 13 | Cross-file transactions (#27) | Medium | High | Reliability |
| 14 | Bulk rename mapping (#25) | Low | Medium | Action |
| 15 | Scope context (#8) | Medium | Medium | Understanding |
| 16 | Extract function (#4) | High | High | Action |
| 17 | Multi-step plans (#10) | High | High | Planning |
| 18 | Convention detection (#7) | Medium | Medium | Understanding |
| 19 | Clone detection (#20) | Medium | Medium | Understanding |
| 20 | API surface analysis (#11) | Low | Medium | Understanding |
| 21 | Code provenance (#23) | Medium | Medium | Understanding |
| 22 | Scaffold generation (#21) | Medium | Medium | Action |
| 23 | Insertion point (#14) | Medium | Medium | Quality |
| 24 | Workspace snapshots (#24) | Low | Medium | Reliability |
| 25 | Inline function (#5) | High | Medium | Action |
| 26 | Codebase health (#17) | Medium | Medium | Understanding |
| 27 | Completion hints (#28) | Medium | Medium | Understanding |
| 28 | NL search compilation (#26) | High | Medium | Integration |
| 29 | Type-aware matching (#12) | Very High | High | Foundation |
| 30 | Patch application (#16) | Medium | Low | Action |

The new entries — MCP server, agent code review, structural invariants, diff minimization,
and cross-file transactions — fill the gaps in the original list. MCP server mode (#18) is
arguably the single most impactful addition: it turns emend from a CLI tool that agents
shell out to into a first-class tool-use integration with warm caches, typed parameters,
and zero startup cost per call.
