# Vim Plugin Improvement Proposal

## Goals

Improve the Vim/Neovim plugin from a "do common things as quickly as
possible" perspective, without splitting search into a separate file mode.

The intended behavior is:

- one unified search surface
- file-path matches remain part of normal search results
- search degrades gracefully when the symbol index is unavailable
- current-buffer operations should reflect unsaved edits when practical
- advanced analysis should help ranking and quick actions, not stall typing

## Current Strengths

The current plugin already has several useful pieces in place:

- a long-lived `editor-server` process
- an open SQLite connection with FTS support when available
- interactive search with debounce
- background reindex notifications
- result actions for refs, callers, callees, move, rename, and type hover
- a completion path that already uses scope and import context

This is a good foundation. The main issues are not missing infrastructure so
much as interaction design, lack of query-history support, and lack of a
hot-buffer path.

## Problems To Fix

### 1. There is no good way to revisit previous picker queries

Interactive search is optimized for the current query only. If you want to
re-run a recent search, compare adjacent queries, or bounce between a few
common lookups, the picker does not help much.

For repeated navigation, query history is more useful than arrow-key style
input history. The picker should make prior searches recallable with a single
action and let the user select from them quickly.

### 2. The picker is optimized for browsing, not repeated action

The search UI supports basic navigation and one-shot open, but it is missing
several high-value actions that make a picker fast in practice:

- open in horizontal split
- open in vertical split
- open in tab
- open but keep picker alive
- better handling for repeated open/jump workflows

Quickfix export exists and is useful, but it is not a substitute for these.

### 3. Outline filtering is not actually local

`EmendOutlineFilter` fetches file symbols once, stores them, and then does not
use that cached list for further filtering. That means a common current-file
workflow still pays RPC and indexing costs that should be avoidable.

### 4. Current-buffer operations are disk-backed, not editor-backed

Completion and `goto_definition` read the current file from disk and parse it
on demand. This means unsaved edits are invisible to the server. That limits
trust in completions and symbol navigation exactly where the editor should feel
most responsive.

### 5. No-index degradation is only partially intentional

The current unified search already includes file-path matching, which is the
right instinct. But the graceful-degradation story should be explicit:

- if the index is unavailable, the plugin should still return useful file hits
- if the symbol index is stale, the UI should surface that clearly but remain
  usable
- if the current buffer is dirty, current-buffer operations should prefer the
  in-memory snapshot over the on-disk file

## Proposal

## 1. Keep unified search, but make degradation a first-class design goal

Do not add a separate file mode.

Instead, keep returning file hits from normal search and lean into this as the
fast fallback path when richer indexing is unavailable.

Desired behavior by availability level:

### A. Hot path: current buffer available

For current-buffer operations, use the in-memory snapshot first:

- completion
- goto definition
- file symbols / outline
- local references in current file

### B. Warm path: index available

For project search, use the current indexed behavior:

- symbol search
- file-path search
- selector resolution
- refs/callers/callees/graph-backed navigation

### C. Cold path: index unavailable

Still provide useful results quickly:

- file-path matches from a lightweight source
- optionally grep-style literal fallback for obvious literal queries
- clear UI indication that index-backed symbol results are unavailable

The important point is that the user should not get "nothing" just because the
index is missing or stale.

## 2. Add query history recall to the picker

The picker should remember recent queries and make them easy to revisit
without turning the prompt into shell-style input history.

Recommended interface:

- `<C-r>` opens a compact "recent queries" overlay from inside the picker
- entries show the query text plus lightweight context, such as result source
  or top hit
- selecting an entry re-runs that query immediately
- `/` from the history overlay filters just the history list

This is better than binding up/down to prompt history because it scales beyond
the immediately previous query and supports recognition rather than recall.

Suggested behavior:

- keep the last 20-50 queries in memory
- optionally persist a shorter recent-query list across Vim sessions
- de-duplicate consecutive identical queries
- prefer recency, but boost queries that produced navigated-to results

## 3. Make the picker faster for common open workflows

Keep the unified search UI, but improve the actions.

Recommended bindings:

- `<CR>`: open in current window and close
- `<C-s>`: open in horizontal split and close
- `<C-v>`: open in vertical split and close
- `<C-t>`: open in tab and close
- `o`: open and keep picker active
- `p`: pin/unpin preview updates

The key point is not more features; it is reducing the number of keystrokes for
the common case of opening several related results in succession.

## 4. Make current-file navigation fully local when possible

`EmendOutlineFilter` should become a true local filter:

1. fetch file symbols once
2. store them in memory
3. filter locally on each keystroke
4. only re-fetch when the buffer changes materially or on explicit refresh

This should feel instantaneous even with no project index.

The same principle should apply to other current-file commands where feasible.

## 5. Add a hot-buffer protocol for local operations

This is the most important architectural change.

The editor server should support in-memory snapshots of open buffers. These
snapshots do not replace the project index; they supplement it for local,
editor-facing operations.

### RPC additions

Add:

- `buffer_open`
- `buffer_update`
- `buffer_close`

Payload should include:

- absolute file path
- buffer identifier
- changedtick or monotonically increasing version
- full current text

Use full-text updates first. Incremental diffs can come later if needed.

### Server behavior

Maintain an in-memory table:

```python
hot_buffers[(path, bufnr)] = {
    "version": changedtick,
    "text": current_buffer_text,
}
```

Then local RPC methods should prefer snapshot text when the request file matches
an open hot buffer:

- `goto_definition`
- `complete`
- `types_at_cursor`
- `file_symbols`

This avoids re-reading from disk and makes unsaved edits visible immediately.

### Scope boundary

Do not try to keep the entire Cozo fact graph hot on every keystroke.

That is the wrong granularity. The project index should remain the backing store
for whole-project queries. Hot buffers should only cover operations where the
editor user expects unsaved edits to matter right now.

## 6. Use analysis to improve ranking and quick actions, not the main loop

Cozo, CFG, and trace analysis can afford useful editor features, but they
should be carefully scoped so they do not harm typing latency.

### Good uses

#### Completion ranking by scope and reachability

The current completion already ranks local names highly. This can be extended
using CFG facts to avoid suggesting names that are out of scope or obviously
uninitialized along the current path.

This is most valuable for current-buffer completion and should run against the
hot buffer snapshot, not the stale file on disk.

#### Context-aware member completion

For dotted completions, use imported-name resolution, known bases, and cached
symbol/type facts to rank likely members more accurately.

This should stay lightweight and bounded.

#### Quick actions from graph analysis

The search UI could expose graph-aware next hops:

- callers
- callees
- sinks
- impact
- semantic context

These are already partly present and should be framed as fast follow-on actions,
not part of the core typing loop.

#### Hotkey-driven navigation interface

The picker should also support a compact "next hop" interface for the selected
symbol or result, driven by one keystroke per action.

Recommended shape:

- `g s`: goto symbol in current file or project
- `g c`: show callers
- `g e`: show callees
- `g k`: show sinks or trace-relevant destinations when available
- `g i`: show impact / reverse-dependency navigation

This should feel like a navigation launcher, not a modal menu maze. The value
is that the user can stay on one symbol and rapidly fan out through the most
common semantic traversals.

### Bad uses

Do not run any of the following on every keystroke:

- full project graph traversals
- interprocedural trace analysis
- dead-code checks
- impact closure

These belong in explicit commands or side actions.

## 7. Make no-index behavior explicit in the UI

When the index is unavailable, the plugin should avoid the current "cache
warming or bust" feel.

Recommended behavior:

- continue showing file-path matches immediately
- optionally allow regex/literal grep fallback for some queries
- mark the result header with the source of results, for example:
  - `[hot-buffer]`
  - `[index]`
  - `[files-only]`
  - `[grep fallback]`
- keep the async indexing flow, but do not block basic use on it

The user should see that richer results are still coming, but should also be
able to act immediately on the cheap results.

## Recommended Implementation Order

### Phase 1: Fast workflow wins

- add recent-query recall in the picker
- add split/tab/open-without-closing picker actions
- expose result provenance in the picker header

These are high-value and low-risk.

### Phase 2: Local-first current-file workflows

- make `EmendOutlineFilter` truly local
- add hotkey-driven navigation for symbol/callers/callees/sinks/impact

This improves current-buffer workflows even before hot-buffer RPC exists.

### Phase 3: Hot-buffer protocol

- add `buffer_open` / `buffer_update` / `buffer_close`
- teach `complete`, `goto_definition`, `types_at_cursor`, and `file_symbols`
  to prefer hot-buffer text

This is the step that makes the plugin feel much more editor-native.

### Phase 4: Bounded analysis-aware intelligence

- CFG-informed local completion ranking
- better dotted-member ranking
- graph-aware quick actions from the picker

These should only be added after Phases 1-3 make the baseline interaction
reliable and fast.

## Deferred / Future

- sink-aware helper ranking near known sink contexts
- trace-informed completion nudges for escaping, parameterization, or safe
  wrappers
- persisted cross-session query history with ranking heuristics
- richer graph-action launchers once the basic hotkey navigation proves useful

## Trade-offs

| Change | Value | Complexity | Risk |
|--------|-------|------------|------|
| Query history recall | High | Low | Low |
| Better open actions | High | Low | Low |
| Local outline filtering | High | Low | Low |
| Hot-buffer protocol | Very high | Medium | Medium |
| Hotkey navigation actions | High | Medium | Low |
| CFG-informed ranking | Medium | Medium | Medium |
| Trace-aware ranking hints | Deferred | Medium | Medium |

The hot-buffer work is the main structural change. Everything else should be
evaluated in terms of whether it improves the common edit-search-jump-complete
loop without adding latency or state complexity that users can feel.

## Recommendation

Keep the unified search model.

Do not introduce a separate file mode. Instead:

- make unified search explicitly degrade to file hits and cheap fallbacks
- add recent-query recall so frequent searches are easy to revisit
- make current-buffer operations hot-buffer aware
- make the picker optimized for repeated open/jump workflows
- add one-keystroke navigation actions for callers/callees/sinks/impact
- use Cozo/CFG/trace analysis sparingly to improve ranking and quick actions

That keeps the product shape simple while making the editor experience much
faster and more trustworthy.
