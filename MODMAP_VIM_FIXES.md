# Module Mapping & Vim Fixes Plan

Review of `modmap-fixes` branch (diff vs `origin/main`).

---

## 1. Command Surface Area Is Too Large

### Problem

The branch introduces **three** resolution commands under `map`:
- `map resolve` — unified dotted-selector resolution
- `map resolve-module` — resolve a module prefix only (renamed from `modmap resolve`)
- `map resolve-file` — resolve to file + line number

Plus the entire `modmap` app is kept as a hidden backward-compat shim with **5 alias commands** that just delegate to `map_*` functions (~50 lines of pure boilerplate).

### Recommendation

1. **Drop `map resolve-module`** — it's strictly a subset of `map resolve`. `resolve` already handles the "exact module match" case (lines 500-512 of the diff). There's no reason to keep both.

2. **Merge `map resolve-file` into `map resolve --file`** (or `--location`) — the only difference is output format. One command with a flag is simpler than two commands.

3. **Drop the `modmap` backward-compat shim entirely** — this is a new feature that hasn't shipped. There are no external users to break. The 50+ lines of alias code is dead weight. If backward compat is needed later, a one-line deprecation warning in the help text suffices.

### Result

`map` subcommands shrink from 10 to 7:
`add`, `add-module`, `lookup`, `search`, `resolve`, `rm`, `rm-module`, `list-modules`, `update-module`

---

## 2. Documentation: Which Path to Map

### Problem

The docs (knowledge.rst, Example 2) show:

```bash
emend map add-module payments --repo org/payments-service/payments
```

This is **wrong** — `--repo` takes a GitHub `org/name` identifier (passed to `gh repo clone`), not a filesystem path. `org/payments-service/payments` is not a valid GitHub repo slug.

The correct version is shown on the next line:

```bash
emend map add-module payments --repo org/payments-service --subpath payments
```

### Recommendation

1. **Remove the incorrect Example 2 first form** — it will produce a clone error at runtime.

2. **Add a clear "rule of thumb" box:**
   > The `--repo` + `--subpath` (or `--path`) should point to the **directory that _is_ the package** — i.e. the directory whose name matches the module prefix's last component, or whose contents are the package's `__init__.py` and submodules.
   >
   > - `payments` package lives at `repo-root/payments/` → `--repo org/repo --subpath payments`
   > - `payments` package lives at `repo-root/src/payments/` → `--repo org/repo --subpath src/payments`
   > - `payments` package IS the repo root → `--repo org/repo` (no subpath)

3. **Add an anti-pattern warning:**
   > Do NOT point to the directory _containing_ the package. If `payments/` is at `src/payments/`, mapping to `--subpath src` is wrong because resolution would look for `src/payments/models.py` by appending `payments.models` to `src/`, duplicating the `payments` part.

---

## 3. Git Repo Caching: Staleness Bug

### Problem

The tag fetch at line 1155-1163 of `knowledge.py` runs **every time** `_ensure_repo_cloned` is called, which is good. However, it **only fetches tags** — not branches.

For mappings that point to a **branch** (e.g. `--branch main` or no branch specified, which defaults to `main`):
- The bare clone is created once and never updated with new commits.
- The worktree is checked out once and never pulled.
- **Result: The cached code becomes permanently stale** for branch-based mappings.

For mappings that point to a **tag** (e.g. `--branch v1.0.5`):
- Tags are immutable, so staleness is not an issue once fetched.
- New tags are fetched on each call (good).

### Recommendation

Add a lightweight freshness check for branch-based checkouts:

```python
# After worktree exists check (line 1176):
if worktree_dir.is_dir():
    # For tags, always reuse (immutable).
    # For branches, check if stale (older than TTL).
    if not _is_tag(contents_dir, ref):
        _maybe_fetch_branch(contents_dir, worktree_dir, ref, ttl_hours=24)
    return str(worktree_dir)
```

Where `_maybe_fetch_branch` does:
1. Check a `.last_fetched` timestamp file in the worktree.
2. If older than TTL (e.g. 24 hours), run `git fetch origin {ref}` on the bare clone and `git merge --ff-only origin/{ref}` on the worktree.
3. Update the timestamp.

Also add `emend map update-module --fetch` to force a re-fetch.

---

## 4. Git Repo Caching: Multiple Worktrees Work Correctly

### Verified

The worktree layout supports multiple checkouts of the same repo:

```
~/.cache/emend/repo-checkouts/org--payments/
├── contents/          # shared bare clone
└── checkouts/
    ├── v1.0.5/        # worktree for tag v1.0.5
    ├── v2.3.5/        # worktree for tag v2.3.5
    └── main/          # worktree for main branch
```

Two callers with different `--branch` values will get independent worktrees. This is correct.

**Minor issue:** If two concurrent processes try to create the same worktree simultaneously, `git worktree add` will race. This is unlikely in practice but could be guarded with a lockfile.

---

## 5. Duplicated Re-export Resolution Logic

### Problem

There are **three** independent implementations of "follow imports to find actual definition":

| Location | Function | Used By |
|----------|----------|---------|
| `ast_utils.py` | `_resolve_through_reexports()` + `get_imports()` | `map resolve-file` CLI, `_resolve_selector_to_goto_item()` |
| `knowledge.py` | `KnowledgeBase._follow_reexport()` | `resolve_selector()` |
| `editor_search.py` | `_extract_import_binding()` | `_mapping_goto()` Tier 3 |

All three parse Python imports via `ast.parse()`, follow `from X import Y` chains, and handle `__init__.py` re-exports. They differ in minor ways (some handle star imports, some don't; some handle relative imports, some don't).

### Recommendation

1. **Consolidate into `ast_utils.py`** — it already has `get_imports()` and `_resolve_through_reexports()`, which is the most complete implementation (handles star imports, explicit imports, relative imports, cycle detection, depth limits).

2. **Delete `KnowledgeBase._follow_reexport()`** — replace calls in `resolve_selector()` with `_resolve_through_reexports()`. The KB method only handles `ImportFrom` in `tree.body` (misses conditional imports, TYPE_CHECKING blocks) and doesn't handle star imports.

3. **Delete `_extract_import_binding()`** — replace with `get_imports()` from `ast_utils.py` filtered to the target identifier.

4. **Make `_resolve_through_reexports` public** — rename to `resolve_through_reexports()` since it's used across modules.

---

## 6. `resolve_selector` Does Too Much

### Problem

`KnowledgeBase.resolve_selector()` (lines 855-955 of knowledge.py) is ~100 lines of path-walking logic with snake_case heuristics, `__init__.py` scanning, and re-export following. This is interleaved with module mapping lookup, making it hard to test or reuse.

### Recommendation

Split into two functions:
1. `resolve_module_to_path()` — already exists, handles module prefix → local path (keep as-is)
2. `resolve_dotted_path_to_selector(base_dir, parts)` — new pure function that walks remaining parts against the filesystem, handling snake_case fallback and `__init__.py` re-exports. This can be tested independently without a KB.

---

## 7. Vim Plugin Issues

### 7a. `\d` mapping hijack

The diff adds:
```vim
autocmd FileType python nnoremap <buffer> <silent> <Leader>d <Cmd>EmendGoto<CR>
```

This **silently overrides `\d`** for all Python files when `g:emend_default_mappings` is set. The comment says "Bug 1: \d unmapped for Python files deletes lines" — but this is a Vim default (`d` deletes), not a bug. Remapping `\d` to EmendGoto is surprising.

**Recommendation:** Remove this mapping. If you want a goto-definition shortcut, use `\eg` (already mapped) or document `gd`/`gD` integration instead.

### 7b. `s:open_selected()` renamed but callers NOT updated (BUG)

`emend#ui#accept()` was renamed to `s:open_selected()` (script-local), but **4 keymaps still call `emend#ui#accept()`**:
- `ui.vim:463` — `<CR>` in normal mode (floating window)
- `ui.vim:484` — `<CR>` in normal mode (split)
- `ui.vim:490` — `<CR>` in insert mode (Neovim)
- `ui.vim:491` — `<CR>` in normal mode (Neovim)

Plus `test/emend.vader:59` asserts `emend#ui#accept` exists.

**This means pressing Enter in the search UI is broken** — it calls a function that no longer exists. Either:
- Rename back to `emend#ui#accept()` (public), or
- Update all 4 callers to use `s:open_selected()` (but script-local functions can't be called from mappings set up via `s:map_current_buf` unless the mapping is defined in the same script scope — which it is, so this would work).

### 7c. `emend#jump_to` is well-designed

The new `emend#jump_to(file, line)` function that searches tabs/windows before opening is a good improvement. Consolidating the 4 separate `execute 'edit'` calls into it was the right call.

---

## 8. Selector Grammar Change Risk

### Problem

The `PATH` regex in `selector.lark` was changed from `/[^:]+/` to `/[^:\[\s]+/`. This means paths containing spaces or `[` are no longer valid in selectors. While spaces in Python file paths are rare, this could break edge cases.

The `dotted_selector` rule is added as an alternative to `explicit_selector` (which requires `::`). The parser now tries `dotted_selector` first for inputs without `::`, falling back to `explicit_selector`.

### Recommendation

This is acceptable but should be documented: "Selectors with spaces in file paths must use the `file.py::Symbol` form."

---

## 9. `_resolve_cache_root` Walk-Upward Change

### Problem

The original `_resolve_cache_root` only checked the immediate project root for `.git`. The new version walks upward looking for `.git` or `.emend`. This is a behavior change that could cause the cache root to jump to an unexpected parent directory if, say, a monorepo has `.emend` at the root but you're working in a subdirectory.

### Recommendation

This change is reasonable for worktree support but should be bounded — don't walk above the original `project_root` argument. The current code walks all the way to `/`, which could match unrelated `.emend` directories.

Actually, looking more carefully: the function starts at `project_root` and walks up. For the normal case (project root IS the git root), it finds `.git` immediately and returns. For worktrees, it finds the `.git` file and follows the commondir. The upward walk is only hit if `project_root` is a subdirectory. This seems fine but should have a max-depth or stop at filesystem boundaries.

---

## 10. Vim Should Expose a Map-Aware Search Method

### Problem

`emend search` correctly defaults to local-only (mapped external deps would add noise). `EmendGoto` correctly uses mappings (goto-definition should follow imports cross-repo). But there's no vim command for **searching** with mappings included — e.g. when you want to browse symbols in an external dependency.

### Recommendation

Add an `EmendSearchMap` command (or `EmendSearch --include-map` variant) that passes `include_map=true` to the search RPC. This keeps the default `EmendSearch` fast and local, while giving users an explicit way to search across mapped repos when they need it.

---

## Summary: Priority Order

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 5 | Consolidate re-export resolution (3 copies → 1) | Medium | High (maintainability) |
| 2 | Fix docs: wrong `--repo` example, clarify path semantics | Small | High (usability) |
| 3 | Add branch freshness check for git caching | Medium | High (correctness) |
| 1 | Simplify command surface (drop resolve-module, modmap shim) | Small | Medium (simplicity) |
| 7a | Remove `\d` Python mapping | Tiny | Medium (surprise factor) |
| 6 | Extract `resolve_dotted_path_to_selector` from KB | Medium | Medium (testability) |
| 10 | Add vim command for map-aware search | Small | Medium (completeness) |
| 7b | Verify `s:open_selected` callers | Tiny | Low (correctness) |
| 8 | Document selector space restriction | Tiny | Low |
| 9 | Bound upward walk in `_resolve_cache_root` | Tiny | Low |
