# Shared Cache for Git Worktrees

## Problem

emend stores its cache DB at `<project_root>/.emend/cache/parse.db`. The project root is found by `_find_project_root()`, which walks up from `.` looking for `.git`, `pyproject.toml`, etc.

In a git worktree, `.git` is a *file* (not a directory) containing a pointer like `gitdir: /main/repo/.git/worktrees/my-worktree`. Each worktree has its own working tree at a separate filesystem path. This means:

1. Each worktree gets its own `.emend/cache/parse.db` — no sharing.
2. Cache entries built in the main repo are invisible to worktrees and vice versa.
3. Content-hashed data (parse results, QN indexes, type inference) is duplicated across worktrees even though the vast majority of files are identical across checkouts.

Since worktrees of the same repo share nearly all the same file content hashes (they typically differ in only a few files), the cache should be shared.

## Design

### Core Idea: Resolve to the Main Repo's Cache

Add a helper function `_resolve_cache_root(project_root)` that returns the **main repo's** project root for cache purposes. All cache DB path construction flows through this function instead of directly using `project_root / ".emend" / "cache"`.

```python
def _resolve_cache_root(project_root: str | Path) -> Path:
    """Return the main repo root for cache storage.

    In a git worktree, the cache lives in the main repo so all
    worktrees share a single parse.db.  For non-worktree repos
    (or non-git projects), returns project_root unchanged.
    """
    root = Path(project_root).resolve()
    git_path = root / ".git"

    if git_path.is_file():
        # Worktree: .git is a file like "gitdir: /main/.git/worktrees/foo"
        text = git_path.read_text().strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (root / gitdir).resolve()
            # gitdir is e.g. /main/repo/.git/worktrees/my-wt
            # The commondir file points to the main .git
            commondir_file = gitdir / "commondir"
            if commondir_file.is_file():
                commondir = commondir_file.read_text().strip()
                main_git_dir = (gitdir / commondir).resolve()
                # main_git_dir is /main/repo/.git → parent is /main/repo
                return main_git_dir.parent

    # Not a worktree (or not git at all) — use project_root as-is
    return root
```

Then define:

```python
def _cache_db_dir(project_root: str | Path) -> Path:
    """Return the directory for the shared cache DB."""
    main_root = _resolve_cache_root(project_root)
    return main_root / ".emend" / "cache"
```

### What Changes

Replace all ~11 occurrences of:
```python
cache_dir = Path(project_root) / ".emend" / "cache"
```
with:
```python
cache_dir = _cache_db_dir(project_root)
```

Same for `type_oracle.py::_type_cache_db_path`.

This is a mechanical find-and-replace. The function result can be `@lru_cache`d since the mapping from project root to cache root is stable for the lifetime of a process.

### Why Content-Hashing Makes This Work

The existing cache design is already almost worktree-safe because the content-addressable tables (`parse_cache`, `qn_index`, `type_cache`, `symbol_index`, `import_graph`, `reference_index`) are all keyed by **content hash** (MD5 of file bytes), not by file path. Two worktrees checking out the same file content produce the same hash and hit the same cache rows.

The only table that uses **absolute file paths** is `file_manifest`:

```
file_manifest(path TEXT PRIMARY KEY, mtime_ns, size, content_hash, indexed_at)
```

This table serves as a "which files have I already indexed?" fast-check per working directory. It needs special handling.

### file_manifest: Per-Worktree View

The `file_manifest` table maps absolute paths to content hashes. Worktrees have different absolute paths for the same logical file. Two approaches:

#### Option A: Relative Paths in file_manifest (Recommended)

Store paths relative to the worktree root instead of absolute. When querying, resolve relative to the current worktree root.

**Pros:** Simple, one row per logical file regardless of how many worktrees exist.
**Cons:** A single worktree root is embedded in mtime/size; when switching worktrees, the stat info won't match and triggers Tier 3 (content hash) verification. But since the content hash will match, this is a single `read + md5` per file — roughly the same cost as a clean `stat` cache hit except you read the file too. For a 10k-file project this adds ~0.5s on the first run from a different worktree, then the mtime gets updated.

**Problem**: This doesn't work cleanly because two worktrees can have different content for the same relative path. The mtime/size for `src/foo.py` in worktree A is meaningless for worktree B.

#### Option B: Worktree-Scoped Manifest (Recommended)

Add a `worktree_id` column (or prefix) to `file_manifest` so each worktree has its own manifest rows, while all content-hashed tables remain shared.

```sql
CREATE TABLE IF NOT EXISTS file_manifest (
    worktree_id TEXT NOT NULL,  -- NEW: identifies which worktree
    path TEXT NOT NULL,          -- absolute path within that worktree
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    content_hash BLOB NOT NULL,
    indexed_at REAL NOT NULL,
    PRIMARY KEY (worktree_id, path)
);
```

The `worktree_id` is derived from the resolved worktree root path (e.g. its MD5 hash, or just the path string itself). Each worktree maintains its own manifest independently.

**Pros:**
- Each worktree's stat cache (mtime, size) is accurate for its own files — no false invalidation.
- Content-hashed tables (parse_cache, symbol_index, etc.) are fully shared — a file indexed in worktree A is immediately available to worktree B by content hash.
- The freshness check (`_scan_manifest`) works unchanged per-worktree; it just filters by `worktree_id`.
- Zero performance penalty for interleaved queries: each worktree reads only its own manifest rows, and all hash lookups hit the shared tables.

**Cons:**
- Slightly more storage for manifest rows (one set per worktree). For a 5k-file project with 3 worktrees, that's ~15k manifest rows — trivial.
- Schema migration needed (add column, change primary key).

**This is the recommended approach.** It gives each worktree an independent, accurate view of "which files are fresh" while sharing all the expensive computed data (parsed ASTs, QN indexes, type info, symbol/reference indexes).

### git_head Tracking

The `index_meta` table stores `git_head` for staleness detection. With shared DB, this also needs worktree scoping:

```sql
-- Instead of:
--   key='git_head', value='abc123'
-- Use:
--   key='git_head:<worktree_id>', value='abc123'
```

Or add a separate small table:

```sql
CREATE TABLE IF NOT EXISTS worktree_meta (
    worktree_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (worktree_id, key)
);
```

### SQLite Concurrency

Multiple worktrees may access the cache DB concurrently (e.g., running `emend` in two terminals). The existing WAL mode (`PRAGMA journal_mode=WAL`) already supports this:

- WAL allows concurrent readers with one writer.
- Multiple processes can read simultaneously without blocking.
- Writes are serialized but short (individual INSERT/UPDATE + COMMIT).
- The existing `timeout=10` on connections handles brief write contention.

No additional locking is needed. SQLite WAL mode is designed for exactly this use case.

One thing to watch: the current code uses a **module-global** `_disk_cache_conn` singleton. This is fine for single-process use but means the connection is opened once at import time and reused. Since each process (each worktree's emend invocation) opens its own connection, and WAL supports multiple connections, this works.

### Schema Migration

The schema change to `file_manifest` requires migration. Options:

1. **Bump schema_version** (currently `"3"` → `"4"`). When `_ensure_index_fresh` sees version < 4, it returns `False`, triggering a full re-index via `emend index`. This is the simplest approach and consistent with how schema changes are already handled.

2. During `_get_disk_cache()` init, detect the old schema and `ALTER TABLE` to add the column + rebuild the primary key. This is more complex and not necessary — a one-time re-index is cheap.

**Recommendation:** Bump to schema version 4. On first access from any worktree with the new code, the old index is treated as stale and rebuilt. The rebuild now populates worktree-scoped manifest rows.

### Implementation Summary

1. **Add `_resolve_cache_root(project_root)` and `_cache_db_dir(project_root)`** in `transform.py`. Cache with `@lru_cache`.

2. **Replace all `Path(project_root) / ".emend" / "cache"`** with `_cache_db_dir(project_root)` (~11 sites in `transform.py`, 1 in `type_oracle.py`).

3. **Add `_get_worktree_id(project_root)` helper** — returns a stable identifier for the current working tree (e.g., the resolved project root path string).

4. **Update `file_manifest` schema** to include `worktree_id` in the primary key.

5. **Add `worktree_meta` table** for per-worktree metadata (git_head, etc.).

6. **Update `_scan_manifest()`** to filter by current `worktree_id`.

7. **Update index population** (`warm_caches`, `_ensure_index_fresh` inline re-index) to write `worktree_id`.

8. **Update `_ensure_cache_ignore_files()`** to use `_cache_db_dir()`.

9. **Bump schema version** to `"4"`.

10. **Update `_find_project_root()`** — no change needed. It already finds the worktree root (which has `.git` as a file). The cache just gets redirected to the main repo via `_resolve_cache_root`.

### Performance Analysis

**Interleaved queries from different worktrees (the key scenario):**

| Operation | Before (separate DBs) | After (shared DB) |
|-----------|----------------------|-------------------|
| Parse cache lookup | Miss (separate DB) | Hit (same content hash) |
| QN index lookup | Miss | Hit |
| Type cache lookup | Miss | Hit |
| Symbol index query | Hit (own DB) | Hit (shared, filtered by worktree manifest) |
| Manifest stat check | Hit (own DB) | Hit (own worktree_id rows) |
| `emend index` | Full rebuild per worktree | First worktree: full. Others: only differing files need indexing; content-hashed data reused |

**Net effect:** Near-zero overhead for the common case (identical files across worktrees get instant cache hits). The only added cost is the `_resolve_cache_root()` call (one `Path.read_text()` on `.git` file + path arithmetic, ~microseconds, cached by `@lru_cache`).

### Edge Cases

- **Non-git projects:** `_resolve_cache_root` returns the project root unchanged. No behavior change.
- **Bare repos / submodules:** `.git` is a directory (bare) or file (submodule). The submodule case looks like a worktree but `commondir` may not exist — fall back to local cache.
- **Worktree deleted while DB open:** The manifest rows for that worktree become stale but harmless. Could add periodic cleanup of worktree_ids that no longer exist.
- **Symlinked worktrees:** `Path.resolve()` handles symlinks, so the worktree_id is stable.
- **`.emend/` in worktree vs main repo:** Only the main repo gets `.emend/cache/`. Worktrees don't need their own `.emend/` directory at all (for caching purposes). Other `.emend/` contents like `patterns.yaml` are tracked in git and thus present in all worktrees already.
