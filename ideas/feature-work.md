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
