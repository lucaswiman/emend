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
(2-3 existing commands approximate it), refactoring by example (agent reads before/after,
writes `emend replace` command — that's just using emend).

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


---

## Crazy Ideas

Ideas that are probably too hard or too speculative, but would fundamentally change what's
possible if they worked.


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

Safe delete is the clear #1 because it turns `deadcode` from a read-only report into an
action, and the transitive cascade is something agents genuinely struggle with.

The honest takeaway: emend's existing feature set is already strong for agents. JSON
output has been implemented across all major commands. The remaining gap is acting on
analysis results (safe delete with cascade). The truly transformative ideas (behavioral
diff, daemon/LSP, cross-language refactoring) are in the Crazy Ideas section because
they're genuinely hard problems.
