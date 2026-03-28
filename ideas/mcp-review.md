# MCP Tool Surface Review

_Claude Code, March 2026_

An honest assessment of the emend MCP schema from the perspective of an LLM
agent that would consume these tools during coding sessions.

## Numbers

| Metric | Value |
|--------|-------|
| Tools | 18 |
| Raw JSON schema | 42,680 bytes / 1,365 lines |
| Estimated tokens (schema) | ~12,000 |
| Server instructions block | ~270 tokens |
| **Total context cost per conversation** | **~12,200 tokens** |

For comparison, the GitHub MCP server typically ships ~40 tools.  12k tokens is
meaningful but not outrageous — roughly equivalent to a 400-line source file
sitting in context for the entire conversation.

### Per-tool breakdown

| Tool | Params (required) | Schema chars | % of total |
|------|-------------------|-------------|------------|
| search | 13 (1) | 4,401 | 10.3% |
| replace | 5 (3) | 1,343 | 3.1% |
| modify | 7 (1) | 2,322 | 5.4% |
| refs | 7 (1) | 1,584 | 3.7% |
| rename | 7 (2) | 1,602 | 3.8% |
| move | 7 (2) | 1,793 | 4.2% |
| graph | 3 (1) | 819 | 1.9% |
| deadcode | 10 (0) | 3,143 | 7.4% |
| lint | 4 (1) | 1,102 | 2.6% |
| impact | 5 (0) | 1,502 | 3.5% |
| semantic_context | 3 (1) | 1,916 | 4.5% |
| taint | 5 (1) | 1,417 | 3.3% |
| query_facts | 10 (0) | 2,544 | 6.0% |
| datalog_query | 3 (1) | 2,124 | 5.0% |
| check_policies | 3 (1) | 1,084 | 2.5% |
| map_read | 10 (0) | 2,707 | 6.3% |
| map_write | 18 (2) | 5,312 | 12.5% |
| grammar_and_cookbook | 0 (0) | 465 | 1.1% |

Two tools (`map_write` and `search`) account for nearly a quarter of the total
schema budget.

---

## Tiered assessment

### Tier 1 — core tools I'd reach for constantly

**search** — The unified search is genuinely the best tool here.  Pattern
matching with `$`-metavars, symbol lookup, and file summaries in one call means
I almost never need to fall back to `cat` or `grep`.  Thirteen params is a lot,
but only `query` is required and the rest are filters that an agent can ignore
until needed.  The auto-detection of mode (pattern vs lookup vs summary) is
exactly the right design — it means I can throw a query at it without thinking
about which sub-command to invoke.

**replace** — Clean, focused.  Pattern-based find-and-replace with dry-run
default is exactly what agents need for bulk changes.  Five params, three
required, no confusion.

**modify** — The unified set/add/remove for symbol components (params, returns,
decorators, bases) is a genuinely unique capability.  This is the kind of
structured edit that's painful and error-prone with raw text manipulation.
Changing a return type annotation or adding a parameter in the right position is
a single call rather than a fragile regex dance.

**rename** — Cross-project rename with import updates is high-value.  The dual
symbol/module mode (detected by `::` in the selector) is a nice design that
avoids needing two tools.

**refs** — Finding references is fundamental for understanding blast radius
before making changes.  The filter flags (`writes_only`, `reads_only`,
`calls_only`) are well-chosen and map to real agent workflows ("who writes to
this?" / "who calls this?").

### Tier 2 — valuable for specific workflows

**move** — Moving symbols between files with automatic import updates is complex
enough to warrant a dedicated tool.  The `copy_only` mode is a useful addition.

**semantic_context** — This is the most "agent-native" tool in the set.  It
answers "what could go wrong if I change this?" with concrete danger categories:
external interfaces, async side effects, dynamic string references, high fan-out,
caching, and missing test coverage.  I would use this before making risky edits
to unfamiliar code.  The compact JSON output with caller/test counts is exactly
the right level of detail.

**impact** — Computing the transitive blast radius of a change via reverse-caller
closure is genuinely valuable for large changes.  The `diff` mode that works
with git specs is smart — an agent can ask "what did my last commit affect?"
directly.

**graph** — Call graphs help when understanding module structure.  Cheap to
include at 819 chars.

**lint** — Useful when the project has `.emend/patterns.yaml` configured.  Not
universally applicable but the schema cost is low.

### Tier 3 — specialized, questionable ROI for general agent use

**deadcode** — Well-designed but heavy.  The 10 params consume 3,143 chars of
schema for something an agent uses infrequently.  Most of the configuration
params (`entry_point_decorators`, `entry_point_names`, `exclude_paths`,
`exclude_references_from`) are project-level settings that should come from the
config file rather than being per-call arguments.  The tool already reads
`.emend/patterns.yaml` — the CLI params are overrides that add schema weight
without much agent value.

**taint** — Powerful but requires config setup.  An agent doing general coding
work would rarely reach for taint analysis spontaneously.  It's appropriate for
security-focused sessions.

**query_facts** — Overlaps significantly with other tools.  Finding symbols,
calls, and references is already covered by `search`, `refs`, and `graph`.  The
main value is structured JSON output and transitive closure, but `search` already
has `output=json` and `impact` already does transitive analysis.

**datalog_query** — The most powerful and most demanding tool.  An agent needs
to understand CozoScript to use it, and the description spends ~1,000 chars
teaching the query language.  This is a genuine escape hatch for queries that
nothing else can express, but the per-call context cost is high.

**check_policies** — Like lint, only useful if `.emend/policies.yaml` exists.
Cheap to include but will sit unused in most sessions.

**grammar_and_cookbook** — A meta-tool that dumps the entire RST reference.  In
practice the output is enormous and would flood context.  The key syntax
patterns in the server instructions block are more useful.

### Tier 4 — candidates for rethinking

**map_read** (2,707 chars) — Cross-service identifier mappings is a specialized
knowledge-base feature.  The 10 params and two modes (mapping vs module) make
the interface complex.  Would most coding agents ever use this?

**map_write** (5,312 chars) — **The single biggest concern.**  Eighteen params,
most nullable, consuming 12.5% of the total schema budget for a feature about
maintaining a YAML knowledge base.  The parameter space is confusing — you need
to know which subset of params applies based on the `kind` + `op` combination.
Four actual operations are packed into one tool through combinatorial dispatch.

---

## Structural issues

### 1. Nullable param verbosity

Pydantic serializes `Optional[str]` as:

```json
"anyOf": [{"type": "string"}, {"type": "null"}],
"default": null
```

That's 8 lines per nullable param.  There are roughly 50 nullable params across
all tools — about **400 lines** of schema that communicate no useful information
beyond "this is optional."  Many MCP clients treat missing params as null
already.  If the schema generation could emit `"type": "string"` with an
implicit null default, it would cut ~30% of the total schema size (~3,600
tokens).

### 2. `map_write` should be split or accept a blob

Four operations (mapping+add, mapping+delete, module+add, module+delete) stuffed
into one tool with 18 conditional params is the wrong shape for tool calling.
Options:

- **Split** into `map_add_mapping`, `map_delete_mapping`, `map_add_module`,
  `map_delete_module` — each with only its relevant params.
- **Accept a JSON blob** — one `entry` param containing the structured data,
  rather than 16 flat nullable fields.

Either approach would reduce per-tool confusion and likely reduce total schema
size despite having more tools.

### 3. `query_facts` vs `datalog_query` overlap

`query_facts` is a guided version of `datalog_query`.  Keep one:

- **Keep `datalog_query`, drop `query_facts`**: saves 2,544 chars.  Agents
  sophisticated enough to query facts can handle CozoScript.
- **Keep `query_facts`, drop `datalog_query`**: saves 2,124 chars.  Safer for
  agents that don't know Datalog.
- **Merge**: add a `mode` param — `"guided"` (default) uses the current
  `query_facts` dispatch, `"raw"` passes through to CozoScript.

### 4. Config-heavy tools carry dead params

`deadcode` (10 params), `taint` (5 params), `lint` (4 params), and
`check_policies` (3 params) all accept configuration that typically lives in
project files.  For agent use, a simpler surface — just `path` and maybe
`config` — with the rest read from the config file, would be lighter.  The
override params are useful for CLI power users but waste schema budget in MCP
context.

### 5. `grammar_and_cookbook` output size

When called, this tool returns the entire RST reference — likely 5,000+ tokens.
For an agent mid-task, that's a heavy context hit.  Consider:

- Splitting into focused sections (`grammar_selectors`, `grammar_patterns`,
  `cookbook_recipes`)
- Or returning a table-of-contents with section IDs, letting the agent request
  specific sections

---

## What I'd actually do differently

If I were designing this for agent consumption from scratch:

**Keep as-is (11 tools, ~22k chars):**
`search`, `replace`, `modify`, `refs`, `rename`, `move`, `graph`,
`semantic_context`, `impact`, `lint`, `grammar_and_cookbook`

**Simplify (save ~3k chars):**
- `deadcode`: drop override params, read everything from config
- `taint`: same treatment
- `check_policies`: same

**Restructure (save ~5k chars):**
- Split `map_write` into focused tools or accept a JSON blob
- Merge `query_facts` into `datalog_query` (or drop one)
- Trim `map_read` params

**Fix schema generation (save ~3.5k tokens):**
- Stop emitting `anyOf: [type, null]` for optional params

**Target:** ~8,000-9,000 tokens total, down from ~12,000.  That's a ~30%
reduction while keeping all the actual capabilities.

---

## The honest question: would I install this?

Yes — with caveats.

The core refactoring tools (`search`, `replace`, `modify`, `rename`, `move`,
`refs`) provide structured code manipulation that's genuinely better than raw
text editing.  I would make fewer mistakes adding a parameter, changing a return
type, or renaming a symbol across a project with these tools than with sed/awk
or manual edits.

`semantic_context` is the standout.  No other MCP server I've seen offers
pre-edit safety analysis.  The danger categories are well-chosen and the output
is compact.

The analysis tools (`impact`, `graph`, `deadcode`) are useful for careful work
on unfamiliar codebases.

What gives me pause is the 12k token overhead.  In a typical coding session, I
start with ~10-15k tokens of system prompt, add the codebase's CLAUDE.md, add
any file reads — and now another 12k for emend tools.  That's noticeable.  The
schema size optimizations above would help a lot.

The mapping tools (`map_read`, `map_write`) feel like they belong in a separate
MCP server or behind an opt-in flag.  They serve a different use case
(cross-service knowledge management) than the core refactoring tools, and their
8k chars of schema is a real cost for most sessions where they'll never be used.

**Bottom line:** the core 8-10 tools are a solid, well-designed surface that I
would reach for over raw file editing.  The remaining tools are individually
fine but collectively expensive in context budget.  A `--profile` flag (e.g.
`emend mcp --profile=core` vs `--profile=full`) could let users opt into the
lighter set.

---

## Refactoring plan

### Phase 1: Schema compression (no behavioral changes)

These changes reduce token cost without altering any tool's behavior or
parameters.  They can ship as a single PR and require only `test_mcp_server.py`
updates (schema shape assertions).

**Estimated savings: ~7,400 chars (~2,100 tokens)**

- [ ] **1a. Post-process `dump_schema()` to simplify nullable types.**
  There are exactly 60 `Optional[X]` params across all tools.  Pydantic
  serializes each as `{"anyOf": [{"type": "X"}, {"type": "null"}], "default":
  null}` (~140 chars).  Post-process the schema dict before JSON serialization:
  walk every property, and when the value has an `anyOf` list with exactly two
  entries where one is `{"type": "null"}`, collapse it to the non-null type and
  keep the `default: null`.  Strip the `title` keys too — they're just
  PascalCase-ified param names and carry zero information for an LLM.  This is
  purely a `dump_schema` / wire-format change; the Pydantic models and runtime
  validation stay the same.
  - File: `mcp_server.py` — `dump_schema()`
  - Test: assert no tool schema contains `"anyOf"` with a null branch; assert
    every previously-nullable param still has `"default": null`
  - Verify: `emend mcp --schema | python -c "import sys,json; ..."`; compare
    char count before/after

- [ ] **1b. Strip `title` keys from schema output.**
  Every property and every tool's `inputSchema` has a `title` field
  (`"title": "Source Project"`, `"title": "replaceArguments"`).  These are
  Pydantic boilerplate — the property name and tool name already convey the
  same information.  Strip them in the same post-processing pass.
  - File: `mcp_server.py` — same function as 1a
  - Estimated additional savings: ~1,500 chars

- [ ] **1c. Trim `datalog_query` description.**
  The current description is 1,375 chars — the longest of any tool — mostly
  example CozoScript queries.  Move the relation schema and examples into
  `grammar_and_cookbook.rst` (where they belong as reference material) and keep
  only a 2-sentence description + a one-liner example in the tool docstring.
  - File: `mcp_server.py` — `datalog_query` docstring
  - File: `src/emend/grammar_and_cookbook.rst` — add a "Fact graph relations"
    section
  - Estimated savings: ~900 chars

- [ ] **1d. Regenerate `docs/_static/mcp_schema.json` and update token counts
  in this document.**

### Phase 2: Merge `query_facts` into `datalog_query`

`query_facts` (10 params, 2,544 chars) is a guided wrapper around
`datalog_query`.  Every `query_facts` call can be expressed as a CozoScript
query.  Merging them removes a tool and its 10-param schema.

**Estimated savings: ~2,500 chars (~700 tokens)**

- [ ] **2a. Add a `mode` param to `datalog_query`.**
  `mode="raw"` (default) is the current CozoScript passthrough.
  `mode="guided"` accepts the current `query_facts` params (`fact_type`, `name`,
  `kind`, `file_path`, `symbol`, `label`, `transitive`, `max_depth`, `limit`)
  and internally constructs the CozoScript query.  Use Pydantic's `Field`
  discriminator or just a string enum.
  - File: `mcp_server.py` — merge guided logic into `datalog_query`, rename to
    `query` or keep `datalog_query`
  - Consideration: the guided params only apply in guided mode; in raw mode
    they're ignored.  Document this clearly in the description.

- [ ] **2b. Deprecate and remove `query_facts`.**
  Remove the `@mcp_app.tool()` registration.  Update
  `test_all_tools_registered` expected set.  Port any `query_facts`-specific
  tests to use the merged tool.
  - File: `mcp_server.py`, `test_mcp_server.py`

- [ ] **2c. Update `docs/commands.rst` MCP tool table.**

### Phase 3: Restructure `map_write`

`map_write` has 18 params (5,312 chars) encoding four operations via
combinatorial `kind` x `op` dispatch.  Replace with a single `entry` JSON
param.

**Estimated savings: ~3,000 chars (~850 tokens)**

- [ ] **3a. Define `MappingEntry` and `ModuleEntry` typed dicts (or just
  document the JSON shapes in the description).**
  The tool accepts `kind`, `op`, and `entry: dict` instead of 16 flat fields.
  Example:
  ```python
  def map_write(
      kind: Annotated[str, Field(description="Entry type: 'mapping' or 'module'.")],
      op: Annotated[str, Field(description="Operation: 'add' or 'delete'.")],
      entry: Annotated[dict, Field(description=(
          "Entry data. For mapping+add: {source_project, source_identifier, "
          "target_project, target_identifier, ...}. For module+add: "
          "{module_prefix, repo|local_path, ...}. For delete: just the key "
          "fields (source_identifier or module_prefix)."
      ))],
  ) -> str:
  ```
  - File: `mcp_server.py` — rewrite `map_write`
  - The tool body unpacks `entry` and delegates to the same `MappingStore`
    methods as today

- [ ] **3b. Slim `map_read` similarly.**
  Reduce to `kind`, `query` (free text or identifier), and `options: dict` for
  the rare filter params (`source_project`, `target_project`, `relationship`,
  `direction`, `limit`).  Or just keep it as-is if the savings aren't worth the
  churn — `map_read` at 2,707 chars is heavy but tolerable.
  - Decision: revisit after 3a lands and re-measure

- [ ] **3c. Update `test_mcp_server.py`** — update or add tests for the new
  `entry` dict interface.

- [ ] **3d. Update `docs/commands.rst`** and `knowledge.rst` MCP sections.

### Phase 4: Trim config-heavy tool params

Move per-call override params on `deadcode`, `taint`, and `check_policies` into
config-file-only settings, keeping only `path` and `config` as MCP params.

**Estimated savings: ~3,500 chars (~1,000 tokens)**

- [ ] **4a. `deadcode` — drop MCP-only override params.**
  Keep: `path`, `kind`, `include_private`, `no_last_reference`.
  Drop from MCP surface (still available on CLI): `exclude_references_from`,
  `no_strings`, `all_files`, `entry_point_decorators`, `entry_point_names`,
  `exclude_paths`.  These are all configurable via `.emend/patterns.yaml`'s
  `deadcode` section already.
  - File: `mcp_server.py` — `deadcode` tool function
  - Keep the underlying `find_dead_code()` signature unchanged; the MCP tool
    just stops exposing the overrides and reads them from config instead
  - Add a `config` param (path to patterns.yaml) so the agent can point at a
    non-default config if needed

- [ ] **4b. `taint` — already lean (5 params), leave as-is.**
  The only candidate for removal is `interprocedural` (a mode switch), but it's
  genuinely useful.  No change needed.

- [ ] **4c. `check_policies` — already lean (3 params), leave as-is.**

- [ ] **4d. Update tests and docs.**

### Phase 5: `grammar_and_cookbook` sectioning

The current tool dumps 15,479 chars (~5,160 tokens) in a single response.
Split into sections the agent can request individually.

**Estimated savings: context per-call, not schema size**

- [ ] **5a. Add a `section` param.**
  `section=None` (default) returns a compact table of contents with section
  names and one-line summaries.  `section="selectors"`, `section="patterns"`,
  `section="recipes"`, etc. return just that section.
  - File: `mcp_server.py` — `grammar_and_cookbook`
  - File: `src/emend/grammar_and_cookbook.rst` — add `.. _section-selectors:`
    labels (or use heading text as keys)
  - Parse the RST into sections by heading level; return the requested slice

- [ ] **5b. Update tests.**
  - `test_grammar_and_cookbook` — test default returns TOC, test each section
    returns content

- [ ] **5c. Fix the existing `literalinclude` regex bug.**
  The `_inline` function uses `m.group(1)` but the regex has no capture group.
  This is already causing a test failure on Python 3.13.  Change
  `r"\.\. literalinclude:: [^\n]+\n..."` to
  `r"\.\. literalinclude:: ([^\n]+)\n..."` (add parens).
  - File: `mcp_server.py` line ~1193

### Phase 6: `--profile` flag for tool subsets

Let users choose a lighter tool surface when they don't need the full suite.

- [ ] **6a. Add `--profile` option to `emend mcp`.**
  Profiles:
  - `full` (default): all tools, current behavior
  - `core`: search, replace, modify, refs, rename, move, semantic_context,
    impact, grammar_and_cookbook (9 tools)
  - `refactor`: core + graph, lint, deadcode (12 tools)
  - Custom: `--tools search,replace,modify` for explicit selection

  Implementation: after `mcp_app` is constructed with all tools, remove
  tools not in the selected profile before starting the server.  FastMCP's
  `_tool_manager` likely supports removal, or we conditionally register.
  - File: `cli.py` — `mcp_cmd()` adds `--profile` / `--tools` params
  - File: `mcp_server.py` — add `configure_profile(profile, tools)` that
    prunes the tool registry

- [ ] **6b. Wire `--profile` into `--schema`** so `emend mcp --schema
  --profile core` shows only the core tools.

- [ ] **6c. Update `docs/commands.rst`** and `docs/installation.rst` with
  profile documentation and recommendations.

- [ ] **6d. Update instructions block per profile.**
  The `core` profile doesn't need the "Mappings" section in the instructions.
  Conditionally trim the instructions string based on which tools are active.

### Phase 7: Measure and validate

- [ ] **7a. Regenerate schema for each profile and record token counts.**
  Target:
  - `full`: ~8,000 tokens (down from 12,000)
  - `core`: ~5,000 tokens
  - `refactor`: ~6,500 tokens

- [ ] **7b. Run `test-mcp` for all profiles.**

- [ ] **7c. Manual smoke test with Claude Code.**
  Configure `emend mcp --profile core` as an MCP server in Claude Code,
  run through a real refactoring session, and confirm the tools work as
  expected without the dropped params causing friction.

- [ ] **7d. Update this document with final numbers.**

### Ordering and dependencies

```
Phase 1 (compression) ─── no deps, ship first
Phase 2 (merge facts) ─── no deps, can parallel with 1
Phase 3 (map_write)   ─── no deps, can parallel with 1-2
Phase 4 (trim params) ─── no deps, can parallel with 1-3
Phase 5 (grammar)     ─── no deps, can parallel with 1-4
Phase 6 (profiles)    ─── depends on 1-5 (needs final tool list)
Phase 7 (validate)    ─── depends on all above
```

Phases 1-5 are independent and can be done in any order or in parallel as
separate PRs.  Phase 6 should come after the tool set stabilizes.  Phase 7 is
the final validation pass.
