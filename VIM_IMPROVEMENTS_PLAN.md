# Vim Plugin Improvements Plan

## Issue 1: File Name Search in `:Emend`

### Problem
The `:Emend` command (interactive search) only searches symbol names, qualified names, and signatures via FTS5. Typing a file path like `foo/bar/baz.py` finds nothing useful because `file_path` is not in the search index.

### Solution

**Server side (`editor_search.py`):**

1. **Add a `file_fts` FTS5 table** alongside the existing `symbol_fts`:
   ```sql
   CREATE VIRTUAL TABLE file_fts USING fts5(
     file_path,
     tokenize='trigram'
   );
   ```
   Populated from `SELECT DISTINCT file_path FROM symbol_index`. Rebuilt alongside `symbol_fts` in `rebuild_fts()`.

2. **Add a `_search_files()` method** to `EditorSearchEngine`:
   - Query `file_fts` with the trigram `MATCH` for candidate paths (requires query length >= 3)
   - For shorter queries, fall back to `LIKE '%query%'` on `symbol_index` `DISTINCT file_path`
   - Post-filter candidates with the fuzzy subsequence algorithm (Issue 2)
   - Return results as items with `kind: "file"`, `name: basename`, `file_path: full_path`, `line: 1`
   - Score: exact basename match = 1100, path substring = 1050, fuzzy subsequence = 950

3. **Integrate into `_search_symbols()`** (the default search path):
   - After the 7-strategy symbol cascade, also call `_search_files()`
   - **File matches appear first** in the combined result list (higher base scores)
   - Deduplicate: if a file match and symbol match point to the same file, keep both (file match shows the file, symbol match shows the specific symbol)

4. **Auto-detect file-like queries**: If the query contains `/` or ends with a known extension (`.py`, `.ts`, `.js`, `.rs`, etc.), prioritize file search by running it first and giving it higher scores.

**Vim side (`autoload/emend/ui.vim`):**

5. **Render file results distinctly**: Add a file icon (e.g., `F`) to `s:KIND_ICONS` and `s:KIND_HIGHLIGHTS` for `kind == "file"`. Show the relative path as the name.

### Files to modify
- `src/emend/editor_search.py` — `rebuild_fts()`, new `_search_files()`, modify `_search_symbols()` and `search()`
- `vim/autoload/emend/ui.vim` — add file kind icon/highlight

---

## Issue 2: Fuzzy Subsequence File Matching

### Problem
The user wants to find files where the search string appears as a **subsequence** of the file path, with tolerance for up to 1 character substitution (typo). FTS5 trigram search finds substring matches but not subsequence matches.

### Background: How FTS5 Trigram Works
FTS5 with `tokenize='trigram'` splits indexed text into overlapping 3-character windows. For example, `"hello"` is indexed as `{"hel", "ell", "llo"}`. A `MATCH` query for `"ell"` finds this entry. This means:
- Queries must be >= 3 characters
- It finds **substring** matches (contiguous), not subsequence matches
- It's fast (inverted index lookup) but has false positives for short queries

### Strategy
Use FTS5 as a **coarse pre-filter** to get candidate file paths, then apply the fuzzy subsequence algorithm as a **post-filter**. This avoids scanning all files.

**Pre-filter**: Extract overlapping 3-char substrings from the search query and query FTS5. Any file path that shares enough trigrams is a candidate.

**Post-filter**: Apply the fuzzy subsequence algorithm below.

### Fuzzy Subsequence Algorithm

**Definition**: `search` is a fuzzy subsequence of `path` if the characters of `search` appear in order in `path`, allowing at most 1 position where `search[i]` is matched to a different character in `path` (substitution).

**Single-pass O(n+m) algorithm** using NFA-style parallel state tracking:

```python
def is_fuzzy_subsequence(search: str, path: str, max_subs: int = 1) -> bool:
    """Check if search is a subsequence of path with at most max_subs substitutions.

    Single linear traversal of path. O(n + m) time, O(max_subs) space.
    """
    s = search.lower()
    p = path.lower()
    n, m = len(s), len(p)

    if n == 0:
        return True
    if n > m + max_subs:
        return False

    # si[k] = number of search chars matched so far using exactly k substitutions.
    # -1 means this state is not yet active.
    si = [0] + [-1] * max_subs

    for c in p:
        # Process states from most-subs to least-subs to avoid double-advancing.
        for k in range(max_subs, -1, -1):
            if si[k] < 0 or si[k] >= n:
                continue
            if s[si[k]] == c:
                # Exact match: advance this state.
                si[k] += 1
            elif k < max_subs:
                # Mismatch: fork a new state with one more substitution.
                new_si = si[k] + 1
                if si[k + 1] < new_si:
                    si[k + 1] = new_si
            # else: mismatch with no subs remaining — skip this path char.

        if any(x >= n for x in si):
            return True

    return any(x >= n for x in si)
```

**How it works**: We maintain `max_subs + 1` parallel "threads" (just integers). Thread `k` tracks how many search characters we've matched using exactly `k` substitutions. On each path character:
- If it matches the next needed search char, advance that thread
- If it doesn't match and we have substitution budget, fork a new thread that "pretends" it matched (substitution)
- Processing in reverse order (highest k first) prevents a single path character from advancing the same chain twice

**Examples**:
- `is_fuzzy_subsequence("foo/bar", "src/foo/bar/baz.py")` -> `True` (plain subsequence)
- `is_fuzzy_subsequence("fxo/bar", "src/foo/bar/baz.py")` -> `True` (1 substitution: x->o)
- `is_fuzzy_subsequence("xyz", "src/foo/bar/baz.py")` -> `False`

### Files to modify
- `src/emend/editor_search.py` — add `is_fuzzy_subsequence()`, use in `_search_files()` post-filter

---

## Issue 3: Selection Highlight Not Moving

### Problem
The `>` caret is rendered in `s:format_result_line()` at line 670 based on `a:index == s:selected`. But `s:render_list()` is only called when **new search results arrive**. When the user navigates with `j`/`k`/`C-n`/`C-p`, `emend#ui#move()` calls `s:highlight_selected()` which moves the cursor (CursorLine), but does **not** re-render the list text — so the `>` caret stays on the first result forever.

Additionally, CursorLine highlighting may not be visible when focus is on the input window (interactive mode), since `cursorline` only renders in the focused window by default.

### Solution

1. **Update caret text on move**: Add a helper `s:update_caret(old_idx, new_idx)` that modifies just the prefix of the old and new lines in the list buffer:
   ```vim
   function! s:update_caret(old_idx, new_idx) abort
     " Swap ' > ' / '   ' prefix on the two affected lines.
     let l:old_lnum = a:old_idx + 3  " 1-indexed, after header + separator
     let l:new_lnum = a:new_idx + 3
     " Read old line, replace prefix, write back (for both lines)
   endfunction
   ```
   Call this from `emend#ui#move()` before `s:highlight_selected()`.

2. **Use extmark highlight for selection instead of (or in addition to) CursorLine**: Create a dedicated extmark on the selected line with `hl_group: 'EmendSelected'` and `hl_eol: true` so it renders as a full-line background highlight regardless of which window has focus. Move this extmark in `s:highlight_selected()`.

3. **Make EmendSelected more visible**: Ensure the highlight stands out — add bold text plus a distinct background:
   ```vim
   highlight default EmendSelected guibg=#1d4e7a guifg=#ffffff gui=bold
   ```

### Files to modify
- `vim/autoload/emend/ui.vim` — `emend#ui#move()`, `s:highlight_selected()`, `s:update_caret()`, extmark-based selection

---

## Feature: Fix EmendGoto (Go-to-Definition)

### Problem
`:EmendGoto` is regressed. Two issues:

1. **No file context passed**: The vim side calls `mapping_goto` with just the bare identifier from `expand('<cword>')` but does NOT pass the current file path. The server's `mapping_goto` RPC accepts a `file` parameter for Tier 3 import-aware resolution (it parses imports in the current file to construct fully-qualified paths), but this is never triggered because no file is sent.

2. **No local variable navigation**: The current `mapping_goto` only searches the symbol index (module-level definitions). It cannot navigate to a local variable's first definition point within a function.

### Solution

**Fix 1: Pass file context from vim**

In `autoload/emend.vim`, the goto function should pass the current file:
```vim
call emend#send('mapping_goto', {
      \ 'identifier': expand('<cword>'),
      \ 'file': expand('%:p'),
      \ }, callback)
```

This enables the server's Tier 3 import resolution: it reads imports from the current file, finds `from module import symbol`, constructs the fully-qualified name, and resolves to the definition location.

**Fix 2: Local variable navigation via scope resolver**

Add a `goto_local` RPC method that uses the Rust scope resolver to find the definition of a local variable:

```python
def goto_local(self, file: str, line: int, col: int) -> SearchResult:
    """Find the definition of the symbol at the given position.

    Uses the scope resolver to trace the reference back to its binding site.
    Works for local variables, parameters, loop variables, etc.
    """
```

**Implementation**:
- Parse the file with the scope resolver (`PyScopeResolver`)
- Call `references_in_file()` to get all references with their resolved qualified names
- Find the reference at the given (line, col) position
- Find the definition site for that qualified name (kind == "definition" or "write" with lowest line number)
- If it's a module-level symbol, fall back to `mapping_goto` for cross-file resolution

**Vim side**: `:EmendGoto` first tries `goto_local` (fast, single-file). If the definition is in the current file, jump there. If the resolved QN points to another module, fall back to `mapping_goto` for cross-file resolution.

### Files to modify
- `vim/autoload/emend.vim` — fix `emend#goto()` to pass file path, add `goto_local` call
- `src/emend/editor_search.py` — add `goto_local()` method, wire into `_dispatch()`

---

## Feature: Rename Symbol

### Design
`:EmendRename` renames the symbol under the cursor across the entire project, with a preview of changes before applying. Uses `transform.rename_symbol()` which is **fully scope-aware** (the Rust scope resolver distinguishes local variables in different functions) and **cross-file** (scans all files importing the target module).

### Scope
- **Module-level symbols**: Renamed across all files that import/reference them
- **Local variables**: Renamed only within their scope (function body), without affecting same-named variables in other functions
- **Method/attribute renames**: Renamed across the class hierarchy (unless `--no-hierarchy`)

### UX Flow
1. User places cursor on a symbol and runs `:EmendRename` (or `<leader>rn`)
2. Plugin resolves the symbol under cursor to a qualified name (via `goto_local` RPC to get the QN at cursor position)
3. Prompts for the new name via `input()` (pre-filled with current name)
4. Sends a `rename_preview` RPC to get the list of changes (files + diffs)
5. Shows changes in a preview buffer (diff format with syntax highlighting)
6. User confirms with `<CR>` or cancels with `<Esc>`/`q`
7. On confirm, sends `rename_apply` RPC which writes the changes to disk
8. Reloads affected buffers

### Implementation

**Server side**: Add `rename_preview` and `rename_apply` RPC methods:
```python
def rename_preview(self, qualified_name: str, new_name: str, file: str = "", line: int = 0, col: int = 0) -> dict:
    """Dry-run rename, return list of {file, old_text, new_text, line} changes.

    If qualified_name is a local variable QN, rename is scoped to that function.
    """

def rename_apply(self, qualified_name: str, new_name: str, file: str = "", line: int = 0, col: int = 0) -> dict:
    """Apply rename. Returns {files_changed: int, edits: int}."""
```

These delegate to `transform.rename_symbol()` with `dry_run=True/False`.

**Vim side**:
- `:EmendRename [new_name]` command in `plugin/emend.vim`
- `emend#rename()` in `autoload/emend.vim` — resolves symbol via `goto_local`, prompts, sends RPCs
- Preview buffer with diff highlighting
- Confirm/cancel keybindings

### Files to modify
- `src/emend/editor_search.py` — add `rename_preview()`, `rename_apply()`, wire into `_dispatch()`
- `vim/plugin/emend.vim` — `:EmendRename` command
- `vim/autoload/emend.vim` — `emend#rename()` function
- `vim/autoload/emend/ui.vim` — rename preview/confirm UI

---

## Feature: Autocompletion (`<C-Space>`)

### Design
In the `:Emend` interactive search input, pressing `<C-Space>` triggers completion of the current token. Completions come from the same search engine but are formatted as a completion menu.

### Behavior
- **Trigger**: `<C-Space>` in the input buffer (insert mode)
- **What completes**:
  - Symbol names (functions, classes, methods, variables)
  - File paths (relative to project root)
  - Qualified names (e.g., `module.Class.method`)
  - Selector components (e.g., after typing `file.py::`, complete symbol names within that file)
- **Display**: Use vim's `complete()` function to show a popup menu with completion items
- **Accept**: Standard vim completion keys (`<C-y>` to accept, `<C-e>` to dismiss)

### Implementation

**Server side**: Add a `complete` RPC method:
```python
def complete(self, prefix: str, *, limit: int = 20, context: str = "") -> SearchResult:
    """Return completion candidates for the given prefix."""
    # If context contains '::', complete symbols within that file
    # Otherwise, return symbol names + file paths matching prefix
```

**Vim side**:
- Map `<C-Space>` in the input buffer to call `emend#ui#complete()`
- `emend#ui#complete()` extracts the current word, sends `complete` RPC, populates `complete()` from the callback
- Completion items include `kind` (file/function/class) and `info` (file path, signature)

### Files to modify
- `src/emend/editor_search.py` — add `complete()` method, wire into `_dispatch()`
- `vim/autoload/emend/ui.vim` — `emend#ui#complete()`, `<C-Space>` mapping
- `vim/autoload/emend.vim` — `emend#complete()` RPC wrapper

---

## Feature: Quickfix Integration

### Design
`<C-q>` in the Emend results pane sends all current search results to vim's quickfix list, then closes the Emend UI. The user can then navigate results with `:cnext`/`:cprev`/`:copen` while editing.

### Implementation
~15 lines of vimscript:

```vim
function! emend#ui#send_to_quickfix() abort
  let l:items = []
  for l:r in s:results
    call add(l:items, {
          \ 'filename': get(l:r, 'file_path', ''),
          \ 'lnum': get(l:r, 'line', 1),
          \ 'text': get(l:r, 'name', '') . ' [' . get(l:r, 'kind', '') . ']',
          \ })
  endfor
  call setqflist(l:items, 'r')
  call s:close_ui()
  copen
endfunction
```

Map `<C-q>` in both the list and input buffers.

### Files to modify
- `vim/autoload/emend/ui.vim` — `emend#ui#send_to_quickfix()`, keybinding

---

## Feature: Search History

### Design
`<C-r>` in the search input cycles through previous search queries. History is stored in a script-local list (persists for the vim session).

### Implementation

```vim
let s:search_history = []
let s:history_idx = -1

" On search submit: prepend to history (dedup)
" On <C-r>: increment history_idx, replace input text
" On <C-f>: decrement history_idx (forward in history)
```

### Files to modify
- `vim/autoload/emend/ui.vim` — history state, `<C-r>`/`<C-f>` mappings, save on search

---

## Implementation Order

1. **Issue 3** — Selection highlight fix (smallest, most annoying bug)
2. **Issue 1** — File name search (moderate, requires FTS table + scoring)
3. **Issue 2** — Fuzzy subsequence filter (builds on Issue 1)
4. **EmendGoto fix** — Pass file context + local variable navigation (enables Rename)
5. **Rename symbol** — High-value feature, leverages existing `rename_symbol()` + goto_local
6. **Quickfix integration** — Small, high-value
7. **Search history** — Small, nice-to-have
8. **Autocompletion** — Nice-to-have, builds on existing search infrastructure
