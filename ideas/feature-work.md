# Feature Ideas for Agentic Coding Workflows

Emend already provides a powerful foundation for AI coding agents: structured edits that
don't require rewriting entire files, scope-aware cross-project refactoring, pattern
matching with metavariables, and Rust-accelerated search.

The ideas below were filtered through two rounds of "could a capable coding agent already
do this?" Most ideas that sound useful for agents turn out to be things agents are already
good at (reading code, classifying things, generating boilerplate, managing git state) or
things already solved by existing tools. What remains are capabilities that require
whole-project graph traversal or persistent infrastructure — things no amount of LLM
reasoning can substitute for.

### What was cut

**Agents are already good at these:** extract/inline function, convention detection, scope
context, insertion point intelligence, scaffold generation, change descriptions, NL search
compilation, impact analysis (agents classify refs from code snippets), diff minimization
(use surgical edits instead), clone detection (name search + jscpd), test-symbol mapping
(2-3 existing commands approximate it).

**Already solved:** structural invariants (= `lint`), batch rollback (`--dry-run` + `git
stash`), dependency graph (`graph` + `refs`), bulk rename mapping (overlap conflicts are
rare), workspace snapshots (`git stash`), semantic diff / API surface / codebase health /
code provenance (agents read diffs and `git log -S` directly), MCP server (modest savings;
disk-persisted parse cache gets most of the benefit without protocol complexity).

---

## 1. Safe Delete (`emend delete`)

**Problem:** `deadcode` finds unreferenced symbols. But removing a symbol creates
cascading effects: its imports become unused, helper functions that only it called become
dead, type aliases only it referenced become orphaned. An agent can do this manually:
delete the symbol, run ruff to clean up imports, run `deadcode` again, delete newly-dead
symbols, repeat. But tracking the transitive closure correctly across 3-4 rounds of
project-wide analysis is exactly the kind of mechanical graph traversal that tools should
handle and agents should not.

**Proposed feature:**
```bash
emend delete models.py::LegacyUser --cascade --dry-run
```
```
Would remove:
  models.py::LegacyUser (the target)
  models.py: import legacy_validator  (now unused)
  validators.py::legacy_validator     (was only called by LegacyUser.__init__)
  validators.py: import legacy_schema (now unused)
  schemas.py::legacy_schema           (was only called by legacy_validator)

5 symbols/imports across 3 files
```

```bash
emend delete models.py::LegacyUser --cascade --apply
emend delete models.py::LegacyUser --apply   # Non-cascading: just the symbol + its unused imports
```

**Implementation:** Remove the symbol. Run unused-import detection on the file. Identify
symbols that were previously referenced only by the deleted code (intersection of the
symbol's callees with `deadcode` results after deletion). Recurse until stable.

**Why an agent can't do this itself:** It *can*, but poorly. The transitive closure
requires multiple rounds of project-wide `deadcode` scanning, and the agent has to
correctly track which symbols to check at each round. Missing one creates orphaned code.
Getting the recursion wrong leaves the project in a half-cleaned state. This is the kind
of thing where "tool does it atomically" is meaningfully more reliable than "agent does it
in 4 rounds of tool calls."


## 2. Structured JSON Output (`--json` for all commands)

**Problem:** `deadcode --json` and `refs --json` exist, but `search`, `edit`, `add`,
`rename`, `move`, and `replace` output human-formatted text. Agents parse this fine — it's
not blocking — but each command has a different text format, and the agent spends tokens on
format-handling that could go toward reasoning.

**Proposed feature:**

Add `--json` to remaining commands. Most useful for `search` (include captures):

```bash
emend search 'print($X)' src/ --json
```
```json
[{"file": "app.py", "line": 23, "match": "print(result)",
  "captures": {"X": {"code": "result", "type": "Name"}}}]
```

The capture metadata is the part agents can't get from text output today: knowing that
`$X` matched a `Name` node vs. a `Call` node vs. a `BinaryExpression` helps the agent
decide what replacement is appropriate.

**Being honest:** This is incremental. Agents handle text output. The value is
consistency, compactness, and capture metadata — not a fundamental capability gap.


---

## Crazy Ideas

Ideas that are probably too hard or too speculative, but would fundamentally change what's
possible if they worked.


### Refactoring by Example

Show emend a before/after pair of one file and have it infer the structural pattern:

```bash
emend by-example --before old_handler.py --after new_handler.py --apply-to src/handlers/
```

Internally: diff the before/after ASTs, extract the structural transformation as a
pattern/replacement pair, verify it's generalizable, apply it.

**Why it's crazy:** Inferring the generalization from one example is AI-complete. Did the
user change `requests.get(url)` to `httpx.get(url)` because they're migrating HTTP
libraries (generalizes to all `requests` calls) or because this specific endpoint needs
async (doesn't generalize)? The intent is ambiguous from one example.

**Why it'd be a game-changer:** This is the most natural way to express "do what I did
here, everywhere else." And for simple structural transforms (rename a call, swap
arguments, wrap in a context manager), the generalization IS unambiguous and could work
reliably.

**Constrained version that might actually work:** Limit to transforms expressible as
emend `replace` patterns. Diff the before/after, try to express the diff as `replace
'pattern' 'replacement'`, show the agent the inferred pattern for confirmation, then
apply. This sidesteps the generalization problem by only supporting transforms that map
cleanly to emend's existing pattern language.


### Cross-Language Refactoring

Renaming a Python symbol also updates references in Django/Jinja2 templates, YAML configs,
SQL migrations, and OpenAPI specs:

```bash
emend rename models.py::User --to Account --cross-language --apply
# Also updates:
#   templates/profile.html: {{ user.name }} → {{ account.name }}
#   alembic/versions/001.py: table_name = 'user' → 'account'
```

**Why it's crazy:** Every template language has different syntax. The mapping from Python
symbol to template variable isn't 1:1 (`UserProfile` → `user_profile` in templates).
Building parsers for every config format is unbounded scope.

**Why it'd be a game-changer:** In real projects, Python symbols leak into non-Python
files. Renaming just the Python side leaves the project broken in ways that only surface
at runtime. No existing tool handles this.


### Behavioral Diff

Given two versions of a function, determine whether they behave differently:

```bash
emend behavioral-diff utils.py::parse_date --against HEAD~1
# "Identical behavior for all tested inputs"
# OR: "Differs for input '2024-02-29' — old returns datetime(2024,2,29), new raises ValueError"
```

Uses a combination of: existing test suite execution, property-based test generation
(Hypothesis), and optionally symbolic execution (CrossHair) to find inputs where behavior
diverges.

**Why it's crazy:** Behavioral equivalence is undecidable. Side-effectful functions can't
be compared by input/output alone. Symbolic execution doesn't scale to complex functions.
Property-based testing can increase confidence but never proves equivalence.

**Why it'd be a game-changer:** The #1 fear in automated refactoring is "did this change
behavior?" An agent that can verify "my refactoring didn't change behavior for any input I
could find" would refactor with dramatically more confidence.


### Daemon Mode / LSP

A persistent daemon that watches the filesystem and maintains an always-current symbol
index, potentially exposed as an LSP server so IDEs benefit too:

```bash
emend daemon start                   # Background process, watches for file changes
emend search 'print($X)' src/       # Instant: reads from index, no parsing
emend refs models.py::User           # Instant: pre-computed reference graph
emend deadcode src/                  # Instant: pre-computed reference counts
```

As an LSP, IDEs could get emend-powered features natively: "find all callers" backed by
emend's scope-aware `QualifiedNameProvider` analysis, pattern-based lint diagnostics
inline, dead code dimming, and refactoring code actions (rename, move, extract) that use
emend's cross-project machinery rather than the IDE's own (often weaker) Python support.

**Why it's crazy:** Incremental re-indexing is a hard systems problem. File watching has
platform-specific gotchas. Memory management for large projects. Cache invalidation when
files change during a query. And LSP is a large protocol surface — implementing even the
common methods (textDocument/definition, textDocument/references, textDocument/rename,
textDocument/codeAction, textDocument/diagnostic) is substantial.

This is essentially building a language server, which is a multi-year project.

**Why it'd be a game-changer:** Every emend query currently scans the project from
scratch. For a 10,000-file project, `refs` takes seconds. With an index, it would take
milliseconds. This would make emend usable in real-time interactive loops — both for
agents calling it dozens of times per minute and for humans getting instant feedback in
their editor. Unlike MCP (which only helps agents), an LSP serves both audiences.

**Pragmatic middle ground:** Don't build the full daemon/LSP. Instead, persist the parse
cache to disk (SQLite keyed by file path + mtime). Each CLI call checks mtimes and only
re-parses changed files. Gets 80% of the speed benefit with 10% of the complexity, and
both agents and humans benefit.


---

## Priority

| # | Feature | Effort | Impact |
|---|---------|--------|--------|
| 1 | Safe Delete (cascade) | Medium | High |
| 2 | JSON output + captures | Low | Low-Medium |

Safe delete is the clear #1 because it turns `deadcode` from a read-only report into an
action, and the transitive cascade is something agents genuinely struggle with. JSON
output is worth doing because it's low-effort — but it's incremental, not a reason to
choose emend.

The honest takeaway: emend's existing feature set is already strong for agents. The gap
isn't missing features — it's acting on analysis results (safe delete) and output format
(JSON). The truly transformative ideas (behavioral diff, daemon/LSP, refactoring by
example) are in the Crazy Ideas section because they're genuinely hard problems.
