# Auto-Reindex on File Changes

## Problem

Currently, users must manually run `:EmendReindex` after editing files for
the search index to reflect changes. This is easy to forget, leading to
stale results.

## Proposed Approach

### 1. BufWritePost autocmd (simplest)

Watch for file writes in the vim plugin and trigger a targeted reindex:

```vim
augroup emend_auto_reindex
  autocmd!
  autocmd BufWritePost *.py call emend#reindex_file(expand('%:p'))
augroup END
```

This requires a new `reindex_file` RPC method that only re-indexes
a single file, rather than scanning the whole project. The current
`reindex` method already checks file mtimes and only refreshes stale
files, but it still walks the entire file list which adds latency.

### 2. Server-side single-file reindex

Add a `reindex_file` method to `EditorSearchEngine`:

```python
def reindex_file(self, file_path: str) -> SearchResult:
    """Re-index a single file after it has been saved."""
    from emend.transform import _index_single_file
    _index_single_file(file_path, self._get_conn())
    # Also update FTS
    self._refresh_fts_for_file(file_path)
    return SearchResult(items=[], elapsed_ms=0, mode="reindex_file")
```

This would be O(1) per save rather than O(files) for full reindex.

### 3. Debounced batch reindex

For rapid saves (e.g. `:wqa` on multiple files, or external formatter
rewriting many files), debounce reindex requests:

```vim
let s:reindex_pending = {}
let s:reindex_timer = -1

function! emend#reindex_file(path) abort
  let s:reindex_pending[a:path] = 1
  if s:reindex_timer >= 0
    call timer_stop(s:reindex_timer)
  endif
  let s:reindex_timer = timer_start(500, {_ -> s:flush_reindex()})
endfunction

function! s:flush_reindex() abort
  let l:files = keys(s:reindex_pending)
  let s:reindex_pending = {}
  let s:reindex_timer = -1
  call emend#send('reindex_files', {'files': l:files}, {_ -> 0})
endfunction
```

### 4. inotify / fswatch (advanced)

For changes outside the editor (e.g. `git checkout`, `git pull`,
external build tools), the editor-server could watch the project
directory using `inotify` (Linux) or `fsevents` (macOS):

- Use Python's `watchdog` library in a background thread
- Debounce filesystem events (100-500ms) to coalesce rapid changes
- Automatically refresh the index for modified files
- Send a `reindexed` notification to the vim plugin so the UI
  knows results may have changed

This is more complex but provides a fully transparent experience.

### 5. Configuration

Add to `.emend/config.toml`:

```toml
[vim]
auto_reindex = true          # Enable auto-reindex on save (default: true)
auto_reindex_debounce = 500  # Debounce interval in ms (default: 500)
```

Or via `pyproject.toml`:

```toml
[tool.emend.vim]
auto_reindex = true
auto_reindex_debounce = 500
```

## Recommended Implementation Order

1. **Phase 1**: BufWritePost autocmd + existing `reindex` (quick win,
   reuses current infra, <100 lines of code)
2. **Phase 2**: Single-file `reindex_file` RPC for O(1) reindex
3. **Phase 3**: Debounced batch for rapid multi-file saves
4. **Phase 4**: `watchdog`-based filesystem monitoring (optional,
   for completeness)

## Trade-offs

| Approach | Latency | Complexity | Coverage |
|----------|---------|------------|----------|
| BufWritePost + full reindex | ~200ms | Low | Editor saves only |
| Single-file reindex | ~5ms | Medium | Editor saves only |
| Debounced batch | ~5ms | Medium | Editor saves only |
| fswatch / inotify | ~50ms | High | All changes |

Phase 1 is good enough for most workflows. Phase 2 makes it
imperceptible. Phases 3-4 are refinements for edge cases.
