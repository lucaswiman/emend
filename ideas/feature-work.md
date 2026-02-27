# Feature Ideas for Agentic Coding Workflows

Emend already provides a powerful foundation for AI coding agents: structured edits that
don't require rewriting entire files, scope-aware cross-project refactoring, pattern
matching with metavariables, and Rust-accelerated search. These features let agents make
precise, surgical changes rather than blunt file rewrites.

The ideas below focus on what's *missing* — specifically, capabilities that agents
**cannot easily replicate** by reading code or using existing tools. Each idea was
filtered against the question: "could a capable coding agent already do this well enough
by reading files, running existing commands, or using git?" If yes, it was cut.

### What was cut and why

Many ideas from earlier brainstorming were removed because they fall into agent strengths
or are already solved:

- **Extract/Inline function** — Agents are good at reading code, identifying free
  variables, and rewriting functions. This is core LLM capability.
- **Convention detection, scope context, insertion point intelligence** — Agents infer
  these from reading a few files. LLMs are excellent pattern recognizers.
- **Scaffold/stub generation** — Writing boilerplate from a protocol is trivial for agents.
- **Change description generation** — Agents write better commit messages than a tool
  would because they understand narrative context, not just structure.
- **NL search compilation** — The agent IS the natural language layer. Adding a second
  model to translate intent into query syntax adds latency and failure modes.
- **Symbol docs & usage extraction** — Combining `search --output code` + `refs` is two
  commands. Not worth a dedicated feature.
- **Structural invariant checking** — Already exists as `lint` with `.emend/patterns.yaml`.
- **Multi-step plans** — `batch` already exists. The atomicity part is covered by
  transactions (below).
- **Dependency graph** — Between `graph`, `refs`, and `files_importing_module` in
  emend_core, this is largely covered.
- **Workspace snapshots, code provenance** — `git stash` and `git log -S` already exist.
- **Semantic diff, API surface, codebase health** — Agents can read diffs, `__init__.py`
  files, and assess quality from code. Not hard enough to warrant tools.

---

## 1. MCP Server Mode (`emend serve`)

**Problem:** AI agents invoke emend as a subprocess, paying Python startup cost on every
call (~200-500ms for interpreter + imports + file discovery). In a multi-step refactoring
involving 15-20 emend calls, that's 3-10 seconds of pure overhead. More importantly,
emend's parse cache and file list cache are thrown away between calls, so every invocation
re-discovers and re-parses the same files.

**Proposed feature:**
```bash
emend serve --mcp --stdio     # For IDE/agent integration
emend serve --mcp --port 8080 # For networked agents
```

Exposes every emend command as an MCP tool with typed JSON Schema parameters. The server
process stays warm between calls, keeping the parse cache (LRU 256 entries), file list
cache, and loaded modules in memory.

**What this changes concretely:**
- First call: ~500ms (cold start, file discovery, parsing). Subsequent calls: ~10-50ms
  (cached parses, no reimport overhead)
- Agent frameworks call emend through native tool-use protocol instead of constructing
  shell command strings
- Any MCP-compatible agent (Claude Code, Cursor, Windsurf, Cline, etc.) gets emend
  integration for free

**Why an agent can't do this itself:** This is infrastructure. No amount of clever
prompting makes subprocess startup faster or preserves state between invocations.


## 2. Change Impact Analysis (`emend refs --categorize`)

**Problem:** Before an agent renames a symbol, changes a function signature, or moves a
module, it needs to know what will break. `refs` finds all references, but treats them as
a flat list. An agent looking at 42 references can't efficiently tell which are call sites
(need arg updates), which are type annotations (need type updates), which are re-exports
(will break downstream packages), and which are subclasses (need method signature changes).

Classifying references by kind requires understanding the AST context of each reference
site — is this `Name` node inside a `Call`, an `Annotation`, a `ClassDef.bases`, an
`ImportFrom`? Emend already visits each reference site with a CST visitor; it just doesn't
report the classification.

**Proposed feature:**
```bash
emend refs models.py::User --categorize --json
```
```json
{
  "call_sites": [{"file": "views.py", "line": 23, "code": "User(name=name)"}],
  "type_annotations": [{"file": "views.py", "line": 10, "code": "user: User"}],
  "subclasses": [{"file": "admin.py", "line": 5, "code": "class AdminUser(User)"}],
  "re_exports": [{"file": "api/__init__.py", "line": 3}],
  "string_references": [{"file": "serializers.py", "line": 23, "code": "'User'"}],
  "imports": [{"file": "views.py", "line": 1, "code": "from models import User"}]
}
```

**Implementation:** Extend `_ReferenceFinder` to inspect the parent node of each matched
`Name` — `Call` parent means call site, `Annotation` parent means type annotation,
`ClassDef.bases` means subclass, `ImportFrom` means import. The visitor already walks the
CST; this is classifying what it already finds.

**Why an agent can't do this itself:** An agent could read each reference site and classify
it, but that means reading 42 files to classify 42 references. That's 42 tool calls of
file-reading. Emend is already visiting those sites in a single pass.


## 3. Safe Delete (`emend delete`)

**Problem:** Agents frequently need to remove dead code, but removing a symbol creates
cascading effects: orphaned imports in the same file, helper functions that only the
deleted symbol called, type aliases only it referenced. `deadcode` finds unreferenced
symbols but doesn't answer "what *becomes* unreferenced if I remove X?"

**Proposed feature:**
```bash
emend delete models.py::LegacyUser --cascade --dry-run
```
```
Would remove:
  models.py::LegacyUser (the target)
  models.py: import legacy_validator  (now unused import)
  validators.py::legacy_validator     (only caller was LegacyUser.__init__)
  validators.py: import legacy_schema (now unused import)
  schemas.py::legacy_schema           (only caller was legacy_validator)
```

```bash
emend delete models.py::LegacyUser --cascade --apply  # Do it
emend delete models.py::LegacyUser --apply             # Just the symbol + its unused imports
```

**Implementation:** Remove the symbol, run unused-import detection on the modified file,
then run `deadcode` scoped to symbols that were previously referenced only by the deleted
code. Recurse until stable.

**Why an agent can't do this itself:** An agent could do this manually: delete the symbol,
grep for orphaned imports, run `deadcode`, delete newly-dead symbols, repeat. But it's
3-4 rounds of tool calls per deletion, and the agent has to correctly track the transitive
closure. This is exactly the kind of mechanical graph traversal a tool should handle.


## 4. Code Clone Detection (`emend clones`)

**Problem:** Before writing a new helper function, an agent should check: does something
similar already exist? After a refactoring, it should check: did I introduce duplication?
Detecting structural similarity across a codebase requires comparing AST shapes after
normalizing variable names — something an agent fundamentally cannot do without reading
every function in the project.

**Proposed feature:**
```bash
# "Does something like this already exist?"
emend clones src/ --like handlers/user.py::validate_user --threshold 0.8 --json

# "Where's the duplication in this codebase?"
emend clones src/ --min-lines 10 --json
```
```json
[{
  "similarity": 0.92,
  "instances": [
    {"file": "handlers/user.py", "symbol": "validate_user", "lines": "23-45"},
    {"file": "handlers/admin.py", "symbol": "validate_admin", "lines": "12-34"}
  ],
  "differences": "variable names only; admin has one extra condition"
}]
```

**Implementation:** Structural hashing — parse each function's AST, replace all `Name`
nodes with a placeholder, hash the resulting tree. Functions with similar hashes are
candidates. Then compute tree edit distance for the similarity score. The Rust backend
could do the hashing in parallel across all files.

**Why an agent can't do this itself:** An agent would have to read every function in the
project and mentally compare them. For a project with 500 functions, that's infeasible.
This requires O(n) AST processing that only a tool can do efficiently.


## 5. Diff Minimization (`emend minimize-diff`)

**Problem:** Agents produce correct code but noisy diffs. When an agent rewrites a
function, it often reformats adjacent lines, reorders imports it didn't need to touch, or
changes whitespace. This makes code review harder and increases merge conflict risk. The
agent doesn't *mean* to touch those lines — it just doesn't have fine-grained control over
what it changes vs. preserves when it writes a file.

**Proposed feature:**
```bash
# Agent writes the file it wants, then minimizes the diff
emend minimize-diff models.py --target /tmp/models_new.py --apply

# From stdin
cat /tmp/models_new.py | emend minimize-diff models.py --apply
```

This performs symbol-level diffing: parse both versions, match symbols by name, and only
emit changes for symbols whose AST actually differs. Whitespace changes outside modified
symbols are discarded. Import reorderings that don't change semantics are suppressed.

**Implementation:** Parse old and new files with LibCST. Walk both CSTs in parallel,
matching top-level statements by identity (class/function name, import target). For each
pair, compare the CST subtrees. Only emit changes for subtrees that actually differ.
Reconstruct the output file using unchanged regions from the original.

**Why an agent can't do this itself:** The agent writes the full file content — that's its
output format. It can't retroactively un-change lines it already changed. The minimization
has to happen *after* the agent produces its output, as a post-processing step. And it
requires AST comparison that text diffing can't do (e.g., recognizing that reordered
imports are semantically equivalent).


## 6. Cross-File Atomic Edits (`emend batch --rollback`)

**Problem:** Many refactorings touch multiple files: add a parameter to a function AND
update all call sites, or add a method to a class AND update the protocol it implements.
If the agent runs 5 `emend edit` commands and the 4th one fails (parse error, selector
doesn't match), the codebase is in an inconsistent state with 3 files changed and 2 not.

Today's `batch` command applies operations sequentially with no rollback. If operation 3
of 5 fails, operations 1-2 are already applied.

**Proposed feature:**
```bash
emend batch operations.yaml --rollback --apply
# On success: all operations applied
# On failure: all operations rolled back, error reported
```

```yaml
operations:
  - edit: {selector: "models.py::User.save[params]", value: "validate: bool = True"}
  - replace: {pattern: "user.save()", replacement: "user.save(validate=True)", path: "src/"}
  - edit: {selector: "protocols.py::Persistable.save[params]", value: "validate: bool = True"}
```

**Implementation:** Before applying any operation, snapshot each file that will be
modified (in memory or temp files). Apply all operations. If any fails, restore all
snapshots. This is simpler than a transaction protocol (no begin/commit state machine) and
fits the existing batch command.

**Why an agent can't do this itself:** The agent can save file contents and restore them
manually, but that's fragile and verbose. More importantly, the agent doesn't know which
files a `replace` command will touch until it runs, so it can't snapshot them in advance.
Emend knows the full operation scope internally.


## 7. Bulk Rename with Mapping (`emend rename --map`)

**Problem:** Some refactorings require renaming many symbols at once: renaming an enum's
values, aligning function names with a new convention, or renaming multiple related
classes. Doing these sequentially with `rename` is dangerous: if you rename A→B first,
then B→C, the first rename's output becomes the second rename's input, and you might
accidentally rename what used to be A all the way to C.

**Proposed feature:**
```bash
emend rename --map renames.yaml --apply
```

```yaml
renames:
  - from: models.py::UserStatus.ACTIVE
    to: ENABLED
  - from: models.py::UserStatus.INACTIVE
    to: DISABLED
  - from: models.py::UserStatus.PENDING
    to: AWAITING_APPROVAL
```

All renames are computed against the *original* code state, then applied simultaneously.
A→B and B→C produce the correct result (original A becomes B, original B becomes C)
without cross-contamination.

**Implementation:** Collect all rename mappings, build a single `_SymbolRenamer` that
maps multiple qualified names to their new names, and do one pass through the project.
This is a natural extension of the existing rename infrastructure.

**Why an agent can't do this itself:** An agent running `rename` 5 times sequentially gets
the wrong result when renames overlap. It could try to order them carefully, but computing
the safe ordering is itself a graph problem. The tool should handle the atomicity.


## 8. Test-Symbol Mapping (`emend test-map`)

**Problem:** After modifying a symbol, an agent runs the full test suite. On a large
project, that's minutes of waiting. The agent *could* grep for the symbol name in test
files, but that only finds direct references. If the agent changed `validate_email()` and
the test calls `register_user()` which internally calls `validate_email()`, the direct
grep misses it.

**Proposed feature:**
```bash
emend test-map utils.py::validate_email --json
```
```json
{
  "direct_tests": ["tests/test_utils.py::test_validate_email_valid",
                    "tests/test_utils.py::test_validate_email_invalid"],
  "transitive_tests": ["tests/test_auth.py::test_register_user"],
  "command": "pytest tests/test_utils.py::test_validate_email_valid tests/test_utils.py::test_validate_email_invalid tests/test_auth.py::test_register_user"
}
```

```bash
emend test-map --changed --json  # Map all git-modified symbols to tests
```

**Implementation:** Combine `refs` (filtered to test directories) for direct references,
then `callers` to find intermediate functions, then `refs` again filtered to test
directories for those intermediates. The call graph infrastructure already exists.

**Why an agent can't do this itself:** Direct test mapping is easy (grep test files for
the symbol name). The *transitive* part is what's hard — it requires call graph traversal
that emend's `callers` and `graph` infrastructure can do efficiently but an agent would
need many rounds of reference-finding to approximate.


## 9. Structured JSON Output (`--json` everywhere)

**Problem:** Some emend commands output JSON (`deadcode --json`, `refs --json`) but most
output human-readable text. Agents *can* parse text output — it's not impossible — but
each text format is different and parsing wastes context window tokens on format-handling
logic.

**Proposed feature:**

Add `--json` to `search`, `edit`, `add`, `rename`, `move`, `replace`:

```bash
emend search 'print($X)' src/ --json
```
```json
[{"file": "app.py", "line": 23, "column": 4, "match": "print(result)",
  "captures": {"X": "result"}}]
```

```bash
emend rename models.py::User --to Account --json --apply
```
```json
{"files_modified": ["models.py", "views.py", "tests/test_models.py"],
 "references_updated": 15}
```

**Why this matters even though agents can parse text:** The value isn't that agents *can't*
parse text — they can. The value is (a) consistency: one output format to handle instead
of 10, (b) compactness: JSON with just the fields the agent needs, no decorative
formatting, and (c) composability: agent can pipe JSON into its next reasoning step
without parsing. This is low-effort, high-incremental-value.


---

## Crazy Ideas

Ideas that are probably too hard to implement well, but would be game-changers if they
worked.


### Refactoring by Example

Show emend a before/after pair from one file, and it infers the structural pattern and
applies it across the codebase:

```bash
emend by-example --before old_handler.py --after new_handler.py --apply-to src/handlers/
```

If the before/after shows `requests.get(url)` changed to `async with httpx.AsyncClient()
as client: await client.get(url)`, emend would infer the generalized transform and apply
it to all handlers.

**Why it's crazy:** Inferring the generalization from one example is an AI-complete
problem. The transform might depend on context the example doesn't show. Two similar-
looking changes might have different intents.

**Why it'd be a game-changer:** This is the most natural way to express "do what I did
here, everywhere else." Currently agents have to write pattern/replacement syntax which is
powerful but requires learning emend's DSL.


### Cross-Language Refactoring

Renaming a Python symbol also updates references in Django/Jinja2 templates, YAML configs,
SQL migrations, OpenAPI specs, and docstrings:

```bash
emend rename models.py::User --to Account --cross-language --apply
# Also updates:
#   templates/profile.html: {{ user.name }} → {{ account.name }}
#   openapi.yaml: $ref: '#/components/schemas/User' → Account
#   alembic/versions/001.py: table_name = 'user' → 'account'
```

**Why it's crazy:** Every template language, config format, and migration framework has
different syntax for referencing Python symbols. Building parsers for all of them is
unbounded scope. The mapping from Python symbol → template variable isn't always 1:1
(e.g., `UserProfile` might appear as `user_profile` in templates).

**Why it'd be a game-changer:** In real projects, Python symbols leak into dozens of
non-Python files. Renaming just the Python side leaves the project broken in ways that
only show up at runtime.


### Runtime-Informed Type Inference

Instrument test execution to capture actual types flowing through functions, then use that
data for type-aware pattern matching:

```bash
pytest --emend-collect-types    # Run tests with instrumentation
emend search '$f:type[Connection]' src/  # Find functions that receive a Connection
emend search --returns-type 'Optional[User]' src/  # Find by return type
```

**Why it's crazy:** Runtime instrumentation is invasive, slows tests significantly, and
only captures types for code paths actually exercised by tests. Monkey-patching `__call__`
on every function has performance and compatibility implications.

**Why it'd be a game-changer:** Most Python codebases don't have complete type annotations.
Runtime types would let emend do type-aware refactoring on untyped code — "find every
function that actually receives a database connection" — which static analysis can't
answer without annotations.


### Codemod Synthesis from Migration Guides

Given a library migration (e.g., "upgrade from SQLAlchemy 1.x to 2.x"), automatically
generate a set of `replace` rules from the library's migration documentation:

```bash
emend synthesize-codemod --from "sqlalchemy 1.4" --to "sqlalchemy 2.0" --output codemod.yaml
emend batch codemod.yaml --apply
```

**Why it's crazy:** Migration guides are written in natural language with varying levels
of detail. The API surface changes are complex and context-dependent. A `Query.filter()`
→ `select().where()` transform depends on what the query is doing.

**Why it'd be a game-changer:** Library migrations are the #1 use case for codemods, and
every migration today requires someone to manually write the transform rules. Automating
even 70% of the common cases would save enormous effort.


### Provably Safe Refactoring

For refactorings with well-defined semantics (rename, move, change-signature), generate
property-based tests that verify the refactoring preserves behavior:

```bash
emend rename models.py::User --to Account --verify --apply
# Generates Hypothesis tests that:
# - Import both old and new versions
# - Verify identical behavior on random inputs
# - Check that all existing tests still pass with the new names
```

**Why it's crazy:** Behavioral equivalence is undecidable in general. Property-based tests
can increase confidence but can't prove correctness. The "import both versions" approach
breaks down for non-pure functions with side effects.

**Why it'd be a game-changer:** The biggest fear in automated refactoring is "did this
break something subtle?" Machine-verified safety would let agents refactor with the
confidence of a human who has manually verified every call site.


---

## Priority Ranking

| Priority | Feature | Effort | Impact |
|----------|---------|--------|--------|
| 1 | MCP Server (#1) | Medium | Very High |
| 2 | Impact Analysis (#2) | Low | Very High |
| 3 | Safe Delete (#3) | Medium | High |
| 4 | JSON output everywhere (#9) | Low | Medium |
| 5 | Clone Detection (#4) | Medium | High |
| 6 | Batch rollback (#6) | Low | High |
| 7 | Diff Minimization (#5) | Medium | High |
| 8 | Bulk Rename (#7) | Low | Medium |
| 9 | Test-Symbol Mapping (#8) | Medium | Medium |

MCP server is #1 because it's a force multiplier: every other emend feature becomes more
useful when invocation is fast and stateful. Impact analysis is #2 because it's
low-effort (extending an existing visitor) with very high value (lets agents act
confidently). Batch rollback is surprisingly high despite being low-effort because it
makes all multi-step refactorings safer with a simple implementation (snapshot files
before modifying).
