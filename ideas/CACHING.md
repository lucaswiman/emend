# Fast Directory Indexing for Low-Latency Emend Operations

## Motivation

Every emend CLI invocation currently starts from scratch: it walks the entire
directory tree, reads every `.py` file, parses each one into a LibCST tree, and
optionally wraps it with expensive metadata providers (QualifiedNameProvider
costs 3-5x base parsing alone). For a 1,000-file project, even a simple
`search` takes 1-3 seconds and a `find-references` takes 5-10 seconds.

This is fine for one-off CLI use but makes emend unusable as a backend for an
editor plugin (e.g. vim/neovim) where operations need to complete in <100ms to
feel instantaneous.

## Current Performance Bottlenecks

### Per-Invocation Work

| Step | Cost | Called By |
|------|------|-----------|
| `_collect_python_files()` via `Path.rglob("*.py")` | ~50-200ms | Every cross-project operation |
| `Path.read_text()` per file | ~0.5ms/file | Every file in project |
| `name_hint not in content` pre-filter | ~0.1ms/file | Every file in project |
| `cst.parse_module(content)` | ~5-15ms/file | Every file matching name hint |
| `MetadataWrapper` + PositionProvider | ~1.2x parse | Most visitors |
| `MetadataWrapper` + QualifiedNameProvider | ~3-5x parse | Reference/rename/callers |
| `MetadataWrapper` + ParentNodeProvider | ~1.3x parse | Write/read context detection |

### Repeated Work Across Commands

1. **File list**: Rebuilt every invocation (same `rglob` walk)
2. **File contents**: Re-read from disk every invocation
3. **Parsed modules**: Re-parsed even if file unchanged
4. **Metadata**: Re-computed even when no structural changes occurred
5. **Symbol index**: No persistent index; full scan required for lookups
6. **Import graph**: Not maintained; scope operations scan every file

## Proposal: Persistent Project Index

### Architecture Overview

```
                    ┌──────────────────────┐
                    │    emend daemon       │
                    │   (long-running)      │
                    │                       │
                    │  ┌─────────────────┐  │
  fs events ──────────>│  File Watcher   │  │
  (inotify/         │  └────────┬────────┘  │
   FSEvents)        │           │           │
                    │  ┌────────▼────────┐  │
                    │  │  Invalidation   │  │
                    │  │     Engine      │  │
                    │  └────────┬────────┘  │
                    │           │           │
                    │  ┌────────▼────────┐  │
                    │  │  Project Index  │  │
                    │  │                 │  │
                    │  │  - file list    │  │
                    │  │  - content hash │  │
                    │  │  - parsed CSTs  │  │
                    │  │  - symbols      │  │
                    │  │  - imports      │  │
                    │  │  - metadata     │  │
                    │  └────────┬────────┘  │
                    │           │           │
  CLI / editor ───────> query/transform ◄──┘
  (unix socket       │  (uses cached data)
   or stdin/stdout)  └──────────────────────┘
```

### Index Layers

The index is built in layers, where each layer depends on the one below it.
Higher layers are more expensive to compute but also more stable (change less
often).

#### Layer 0: File Manifest

```
{
  file_path: str  →  FileEntry {
    mtime: float,
    size: int,
    content_hash: bytes,  # blake3, fast
  }
}
```

- **Built by**: Single `os.scandir()` walk (faster than `Path.rglob`)
- **Invalidated by**: Any filesystem event in watched directories
- **Cost to rebuild**: ~10-50ms for 1,000 files
- **Storage**: In-memory dict, optionally persisted to `.emend/cache/manifest.json`

#### Layer 1: Content Cache

```
{
  content_hash: bytes  →  source_text: str
}
```

- **Built by**: `Path.read_text()` on first access
- **Invalidated by**: Content hash change (detected via Layer 0)
- **Cost per file**: ~0.5ms read
- **Eviction**: LRU with configurable max size (default 500 files)

#### Layer 2: Parsed Module Cache

```
{
  content_hash: bytes  →  cst.Module
}
```

- **Built by**: `cst.parse_module(content)` on first access
- **Invalidated by**: Content hash change
- **Cost per file**: ~5-15ms parse
- **Key insight**: Keyed by content hash, not file path. If two files have
  identical content, they share one parsed module. More importantly, if a file
  is reverted to a previous version, the cached parse is still valid.

#### Layer 3: Symbol Index

```
{
  file_path: str  →  [SymbolEntry {
    name: str,
    qualified_name: str,
    kind: "function" | "class" | "method" | ...,
    line_start: int,
    line_end: int,
    decorators: list[str],
    parameters: str | None,
    return_type: str | None,
    parent: str | None,
  }]
}

# Plus reverse index:
{
  symbol_name: str  →  [(file_path, SymbolEntry)]
}
```

- **Built by**: Running `_SymbolCollector` (with PositionProvider) on parsed module
- **Invalidated by**: Content hash change for that file
- **Cost per file**: ~2-5ms (parse + visitor)
- **Enables**: O(1) symbol lookup by name, instant `search` commands

#### Layer 4: Import Graph

```
{
  file_path: str  →  ImportInfo {
    imports_from: dict[str, list[str]],  # module → [names]
    import_modules: list[str],           # bare module imports
  }
}

# Plus reverse index:
{
  module_path: str  →  [file_path]  # files that import from this module
}
```

- **Built by**: Simple visitor over `Import` and `ImportFrom` nodes (no metadata
  providers needed -- just walk the CST)
- **Invalidated by**: Content hash change for that file
- **Cost per file**: ~1-2ms
- **Enables**: Fast `find-references` pre-filtering (only check files that
  actually import the target module), fast `rename-module` dependency detection

#### Layer 5: Name Occurrence Index (Trigram)

```
{
  trigram: bytes  →  [file_path]
}
```

- **Built by**: Extract all trigrams from source text
- **Invalidated by**: Content hash change
- **Cost per file**: ~0.5ms
- **Enables**: Sub-millisecond pre-filtering for pattern matching. Instead of
  `name_hint in content` (which reads the full file), check the trigram index
  to find candidate files. This is the same technique `ripgrep` uses internally.

#### Layer 6: Qualified Name / Scope Cache (Optional, Expensive)

```
{
  content_hash: bytes  →  QualifiedNameMetadata
}
```

- **Built by**: `MetadataWrapper` with `QualifiedNameProvider`
- **Invalidated by**: Content hash change for that file, OR any change to files
  it imports from (requires Layer 4 import graph for transitive invalidation)
- **Cost per file**: ~15-75ms (3-5x base parse)
- **Build strategy**: Lazy -- only computed for files that actually need
  scope-aware operations. Warmed in background after initial index build.

### Invalidation Strategy

#### File-Level Invalidation

When a file changes:

```python
def invalidate_file(path: str):
    old_entry = manifest.get(path)
    new_entry = stat_file(path)

    if old_entry and old_entry.content_hash == new_entry.content_hash:
        return  # Content unchanged (e.g. only mtime changed by touch)

    # Invalidate layers bottom-up
    manifest[path] = new_entry                     # Layer 0: update
    content_cache.pop(old_entry.content_hash)       # Layer 1: evict old
    parsed_cache.pop(old_entry.content_hash)        # Layer 2: evict old
    symbol_index.pop(path)                          # Layer 3: evict
    import_graph.invalidate(path)                   # Layer 4: evict + cascade
    trigram_index.invalidate(path)                   # Layer 5: rebuild
    qualified_names.pop(old_entry.content_hash)      # Layer 6: evict

    # Cascade: invalidate Layer 6 for files that import from this one
    for dependent in import_graph.reverse[module_for(path)]:
        qualified_names.pop(manifest[dependent].content_hash)
```

#### Transitive Invalidation

The import graph (Layer 4) enables cascading invalidation:

- File A exports `class Foo`
- Files B and C import `Foo` from A
- When A changes, invalidate scope caches for B and C too

This is critical for correctness of `find-references` and `rename` operations.
Without it, a cached qualified name resolution in file B might return stale
results if A's exports changed.

#### Cheap Invalidation Check

Before using any cached data, check:

```python
def is_valid(path: str) -> bool:
    cached = manifest.get(path)
    if cached is None:
        return False
    try:
        stat = os.stat(path)
    except OSError:
        return False
    return (stat.st_mtime_ns == cached.mtime_ns
            and stat.st_size == cached.size)
```

If mtime+size match, skip the expensive content hash. This makes validation
essentially free (~1 syscall per file). Only recompute content hash when
mtime or size differ.

### Daemon Architecture

#### Option A: Long-Running Daemon Process

```
emend daemon start    # start background daemon
emend daemon stop     # stop daemon
emend daemon status   # check if running

# All normal commands auto-detect running daemon:
emend search "foo"    # → connects to daemon via unix socket
                      # → daemon uses cached index
                      # → returns results in <50ms
```

**Communication**: Unix domain socket at `.emend/cache/daemon.sock`

**Protocol**: Simple JSON-RPC over the socket. Each request is a full emend
command serialized as JSON; response is the output.

**Lifecycle**:
- Daemon starts, builds full index (Layers 0-5), ~2-5 seconds for 1,000 files
- Daemon watches filesystem for changes (inotify on Linux, FSEvents on macOS)
- On file change, incrementally updates affected index layers
- Daemon auto-exits after 30 minutes of inactivity (configurable)
- CLI auto-starts daemon on first invocation if not running

#### Option B: Persistent On-Disk Index (No Daemon)

```
emend index build     # build/rebuild index
emend index status    # show index stats

# Normal commands check on-disk index:
emend search "foo"    # → loads index from .emend/cache/
                      # → validates staleness via mtime check
                      # → rebuilds stale entries
                      # → returns results
```

**Storage**: SQLite database at `.emend/cache/index.db`

**Trade-offs vs daemon**:
- Simpler (no background process)
- Slower (must load index from disk on each invocation, ~50-200ms)
- No real-time invalidation (must check mtimes at query time)
- Still much faster than current approach (skip re-parsing unchanged files)

#### Recommendation: Hybrid

Start with Option B (on-disk index) for simplicity, then add Option A (daemon)
as an optimization for editor integration. The on-disk index provides the
foundation; the daemon adds real-time invalidation and keeps the index hot in
memory.

### Editor Integration Protocol

For vim/neovim integration, the daemon exposes a simple protocol:

```json
// Request
{"method": "search", "params": {"query": "MyClass", "output": "selector"}}

// Response
{"results": [
  {"selector": "src/models.py::MyClass", "line": 42, "kind": "class"},
  {"selector": "src/views.py::MyClass", "line": 17, "kind": "class"}
]}
```

```json
// Request
{"method": "refs", "params": {"selector": "src/models.py::MyClass"}}

// Response
{"references": [
  {"file": "src/views.py", "line": 10, "col": 5, "kind": "import"},
  {"file": "src/views.py", "line": 25, "col": 12, "kind": "usage"},
  {"file": "tests/test_models.py", "line": 3, "col": 5, "kind": "import"}
]}
```

```json
// Request
{"method": "rename", "params": {
  "selector": "src/models.py::MyClass",
  "to": "MyModel",
  "apply": true
}}

// Response
{"changed_files": ["src/models.py", "src/views.py", "tests/test_models.py"],
 "diffs": [...]}
```

This maps directly to vim's quickfix list and LSP-like workflows.

### Vim Plugin Sketch

```vim
" emend.vim - thin wrapper around emend daemon

function! emend#search(query) abort
  let result = emend#rpc('search', {'query': a:query, 'output': 'location'})
  call setqflist(map(result.results, {_, r -> {
    \ 'filename': r.file,
    \ 'lnum': r.line,
    \ 'text': r.selector
    \ }}))
  copen
endfunction

function! emend#refs() abort
  let selector = emend#selector_at_cursor()
  let result = emend#rpc('refs', {'selector': selector})
  call setqflist(map(result.references, {_, r -> {
    \ 'filename': r.file,
    \ 'lnum': r.line,
    \ 'col': r.col,
    \ 'text': r.kind
    \ }}))
  copen
endfunction

function! emend#rename(new_name) abort
  let selector = emend#selector_at_cursor()
  let result = emend#rpc('rename', {
    \ 'selector': selector,
    \ 'to': a:new_name,
    \ 'apply': v:true
    \ })
  " Reload changed buffers
  for f in result.changed_files
    execute 'checktime ' . f
  endfor
endfunction

nnoremap <leader>es :call emend#search(input('Search: '))<CR>
nnoremap <leader>er :call emend#refs()<CR>
nnoremap <leader>eR :call emend#rename(input('New name: '))<CR>
```

### Performance Targets

| Operation | Current | With On-Disk Index | With Daemon |
|-----------|---------|-------------------|-------------|
| `search "MyClass"` | 1-3s | 200-500ms | <50ms |
| `search --output summary src/` | 2-5s | 500ms-1s | <100ms |
| `find "$X.save()"` | 2-5s | 500ms-1s | <100ms |
| `refs src/models.py::MyClass` | 5-10s | 1-2s | <200ms |
| `rename ... --to NewName` | 10-15s | 2-3s | <500ms |
| `lint` (10 rules) | 20-30s | 5-8s | <2s |

The daemon targets assume warm cache (index already built and up-to-date).
Cold start adds 2-5 seconds for initial index build.

### Implementation Plan

#### Phase 1: On-Disk Module Cache

Add a content-hash-keyed cache for parsed LibCST modules:

```python
# In transform.py, modify visit_project():
from emend.cache import ModuleCache

def visit_project(name_hint, visitor_factory, ...):
    cache = ModuleCache(".emend/cache")
    for py_file in _collect_python_files(project_root):
        content = Path(py_file).read_text()
        if name_hint and name_hint not in content:
            continue
        module = cache.get_or_parse(content)  # Cache hit = ~0.1ms vs ~10ms
        ...
```

Note: LibCST modules are not directly picklable, so the cache would store
serialized form or use a custom serialization. Alternatively, cache at the
content level and re-parse (still saves disk reads for unchanged files where
only mtime checks are needed).

**Impact**: 2-5x speedup for repeated operations on unchanged files.

#### Phase 2: Symbol Index + Import Graph

Build Layers 3 and 4 as part of a persistent index:

```python
class ProjectIndex:
    def __init__(self, project_root: str):
        self.root = project_root
        self.db = sqlite3.connect(f"{root}/.emend/cache/index.db")
        self._ensure_tables()

    def symbols_named(self, name: str) -> list[SymbolEntry]:
        """O(1) lookup by symbol name."""
        ...

    def files_importing(self, module: str) -> list[str]:
        """Which files import from this module?"""
        ...

    def update_file(self, path: str):
        """Re-index a single file (incremental)."""
        ...
```

**Impact**: `search` and `lookup` become near-instant. `find-references`
can pre-filter to only files that import the target module.

#### Phase 3: File Watcher + Daemon

Add the long-running daemon with filesystem watching:

```python
# emend/daemon.py
import asyncio
from watchfiles import awatch

class EmendDaemon:
    def __init__(self, project_root: str):
        self.index = ProjectIndex(project_root)
        self.index.build()  # Initial full build

    async def watch(self):
        async for changes in awatch(self.root):
            for change_type, path in changes:
                if path.endswith('.py'):
                    self.index.update_file(path)

    async def handle_request(self, request: dict) -> dict:
        method = request["method"]
        params = request["params"]
        # Dispatch to emend operations using self.index
        ...
```

**Impact**: Real-time index updates, <50ms for most operations.

#### Phase 4: Vim/Neovim Plugin

Ship a thin vim plugin that communicates with the daemon. Could also support
LSP protocol for broader editor compatibility.

### Storage Estimates

For a 1,000-file Python project (~200K LOC):

| Index Layer | Memory | Disk |
|-------------|--------|------|
| File manifest | ~200KB | ~100KB |
| Content cache (500 files LRU) | ~50MB | N/A (read from disk) |
| Parsed module cache | ~200MB | ~100MB (serialized) |
| Symbol index | ~5MB | ~2MB |
| Import graph | ~1MB | ~500KB |
| Trigram index | ~10MB | ~5MB |
| Qualified name cache | ~50MB | ~25MB |

Total daemon memory: ~300-500MB for a large project. This is comparable to
other language servers (pyright uses similar amounts).

### Open Questions

1. **Cache serialization format**: LibCST modules aren't trivially serializable.
   Options: pickle (fast but fragile across versions), custom serializer, or
   just cache the text and re-parse (simpler, still faster than disk I/O for
   unchanged files). Another option: only cache the derived data (symbols,
   imports, metadata) rather than the parsed CST itself.

2. **Multi-root projects**: Should the index support multiple project roots
   (e.g. monorepo with multiple packages)? The current `visit_project()` takes
   a single `project_path`.

3. **Virtual environments**: Should the index include `.venv` / `site-packages`
   for cross-package reference finding? Currently excluded by
   `_collect_python_files()`.

4. **Concurrency**: Should the daemon handle concurrent requests? If the editor
   sends a `rename` while a `search` is in progress, should it queue, cancel,
   or run in parallel? LibCST operations are CPU-bound so parallelism is
   limited by GIL (unless using multiprocessing or Rust -- see RUST_REWRITE.md).

5. **LSP vs custom protocol**: Should the daemon speak LSP directly? This would
   give compatibility with every editor for free, but LSP's type system
   (references, rename, etc.) doesn't map perfectly onto emend's richer
   operations (pattern find/replace, component editing, batch operations).
   A pragmatic approach: speak LSP for standard operations, add custom
   extensions for emend-specific features.
