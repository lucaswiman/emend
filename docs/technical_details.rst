Technical Details
=================

This document describes emend's internal design, the rationale behind key
architectural choices, and the dependencies that underpin each subsystem.

Overview
--------

emend is structured around two complementary refactoring primitives that share
a common execution model:

- **Structured edits** — component-level surgery on symbol metadata (parameters,
  return types, decorators, bases, body) addressed via a selector grammar.
- **Pattern transforms** — code-pattern search and replace using metavariable
  capture syntax (``$X``, ``$...ARGS``) compiled to LibCST matchers.

Both systems parse source files into a concrete syntax tree (CST) that preserves
all formatting whitespace and comments.  Transformations produce a new CST from
which the modified source is rendered, ensuring that untouched code is reproduced
character-for-character.

Two-layer architecture
-----------------------

Rust layer: ``emend_core``
~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``emend_core`` extension is a `PyO3 <https://pyo3.rs>`_ / `maturin
<https://www.maturin.rs>`_ Rust crate compiled directly into the emend wheel.
It handles everything that benefits from raw throughput and parallelism:

- **File discovery** — parallel directory walk via ``rayon`` (skips
  ``.git``, ``__pycache__``, ``.venv``, etc.).
- **Content pre-filtering** — reads all candidate files in parallel with
  ``rayon``, discarding any file that does not contain the literal tokens
  that must be present for a pattern to match.  This eliminates most files
  before the CST layer is invoked.
- **Pattern fast-path** — a subset of emend's pattern language compiles to a
  tree-sitter IR.  When a query uses only features the Rust engine supports and
  no scope constraints are needed, ``find_pattern_in_files`` / ``find_multi_patterns_in_files``
  perform the full match in Rust (parallel, GIL-free), returning ``(file, line,
  col, text)`` tuples directly to Python.
- **Symbol batch collection** — ``collect_symbols_batch`` extracts the symbol
  tree for a list of files in a single parallel pass, feeding the ``search``
  command's summary mode.
- **Callee extraction** — ``collect_callees`` performs a single tree-sitter
  traversal to enumerate function call edges for the ``graph`` command.
- **Import analysis** — ``extract_imports`` and ``files_importing_module``
  support the ``--imported-from`` filter and module-rename import rewriting.

The Rust module is registered with ``#[pymodule(gil_used = false)]``, which
tells PyO3 that none of its functions acquire the GIL.  On **free-threaded
Python** (``3.13t``, ``3.14t``) this means multiple emend operations can run
truly concurrently across OS threads with no lock contention.

Rust dependencies:

- `pyo3 <https://pyo3.rs>`_ 0.25 — Python/Rust FFI
- `tree-sitter <https://tree-sitter.github.io>`_ 0.24 + ``tree-sitter-python`` 0.23 — incremental parsing for the Rust pattern fast-path
- `rayon <https://github.com/rayon-rs/rayon>`_ 1.10 — data-parallel iterators (file I/O, pattern scanning)
- `memchr <https://github.com/BurntSushi/memchr>`_ 2.7 — fast literal-string pre-filter

Python layer: LibCST
~~~~~~~~~~~~~~~~~~~~

Complex or scope-aware operations that require full semantic analysis are
handled in Python using `LibCST <https://github.com/Instagram/LibCST>`_:

- **Scope-aware rename / find-references** — uses LibCST's ``QualifiedNameProvider``
  and ``PositionProvider`` to resolve qualified names before renaming, ensuring
  that shadowed locals and identically named symbols in other modules are not
  accidentally rewritten.
- **Pattern matching with scope constraints** (``--where``, ``--inside``,
  ``--not-inside``, ``--imported-from``, ``--scope-local``) — tree-sitter
  cannot reason about Python scoping rules; LibCST matchers + metadata
  providers are used instead.
- **Component-level edits** (``edit``, ``add``) — parameter lists, return
  annotations, decorator lists, and base classes are round-tripped through
  LibCST to guarantee syntactically valid output.
- **Cross-file operations** (``rename``, ``move``, ``refs``) — ``visit_project()``
  iterates all project files through a ``MetadataWrapper`` that provides
  qualified-name resolution.

LibCST is also used for all *mutations* — the Rust layer is read-only.

Caching and indexing
--------------------

emend maintains a cache at ``.emend/cache/parse.db`` (SQLite, WAL mode).
The cache is content-addressed — almost every key is the MD5 of the file's
source text — so switching branches or reverting edits naturally reuses
earlier entries.  A ``.gitignore`` and ``.dockerignore`` are auto-generated
inside ``.emend/cache/`` to prevent the database from being checked in.
When running inside a git worktree the cache is stored in the **main repo's**
``.emend/cache/`` so all worktrees share a single database (see
`Git worktree support`_ below).

Overview of cache tables
~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Table
     - Key
     - Contents
   * - ``parse_cache``
     - content MD5 (BLOB)
     - Compressed-pickled ``libcst.Module``.  Avoids reparsing unchanged files.
   * - ``qn_index``
     - content MD5 (BLOB)
     - Compressed-pickled ``set[str]`` of every qualified name in the file.
       Used by ``visit_project()`` to skip files that cannot reference a target
       symbol.
   * - ``type_cache``
     - content MD5 (TEXT)
     - Compressed-pickled ``FileTypes`` from the type oracle.  Avoids
       re-running pyrefly / pyright / ty on unchanged files.
   * - ``file_manifest``
     - (worktree_id, absolute path)
     - ``(mtime_ns, size, content_hash, indexed_at)``.  Bridges path-based
       queries to the content-hash caches and enables incremental re-indexing
       via stat-only scans.  Scoped by ``worktree_id`` so each worktree
       maintains its own stat cache while sharing content-hashed data.
   * - ``symbol_index``
     - (content_hash, file_path, name, ...)
     - One row per symbol definition (function, class, method).  Stores name,
       qualified name, kind, line range, depth, parent, signature, return type,
       and decorators.  Indexed on ``name``, ``qualified_name``, ``file_path``,
       and ``kind`` for fast lookups.
   * - ``reference_index``
     - (content_hash, target_qn, file_path, line, col)
     - One row per reference to a qualified name.  Each row records the
       reference kind (``read``, ``write``, ``import``, ``call``).  Indexed on
       ``target_qn`` for fast find-references.
   * - ``import_graph``
     - (content_hash, imported_module)
     - One row per import statement, mapping the importing file to the dotted
       module name.  Indexed on ``imported_module`` for fast "files importing X"
       queries.
   * - ``index_meta``
     - key name (TEXT)
     - Key-value pairs: ``schema_version``, ``git_head:<worktree_id>``,
       ``indexed_at:<worktree_id>``.  Per-worktree keys are scoped by the
       worktree's resolved root path.

In-memory parse cache
~~~~~~~~~~~~~~~~~~~~~

``transform._cached_parse()`` maintains a two-tier cache:

1. **In-memory dict** (256 entries, keyed on source MD5).  Thread-safe via
   ``threading.Lock``.  When full, the oldest 25 % of entries are evicted.
2. **Disk** (``parse_cache`` table).  On a cache miss the source is parsed,
   the result is written to disk, then promoted to memory.

This means repeated lookups for the same unchanged file are free after the
first parse, even across process restarts.

How caches are populated
~~~~~~~~~~~~~~~~~~~~~~~~

There are two population paths:

**Lazy (on first use).**  ``_cached_parse()`` populates ``parse_cache``
transparently whenever a file is parsed.  ``visit_project()`` populates
``qn_index`` as a side-effect of running ``QualifiedNameProvider``: after each
file's ``MetadataWrapper.visit()`` completes, a lightweight ``_QNCollector``
re-walks the resolved tree and stores the qualified-name set.

**Eager (``emend index``).**  ``warm_caches()`` scans the project in parallel
using a ``ProcessPoolExecutor``.  Each worker subprocess (``_index_batch()``)
receives a batch of ``(file_path, source_text)`` tuples and performs:

1. **Parse** — ``cst.parse_module()`` → compressed pickle → ``parse_cache``.
2. **QN resolution** — ``MetadataWrapper`` + ``_QNCollector`` → compressed
   pickle → ``qn_index``.
3. **Symbol collection** — ``_SymbolCollector`` (from ``query.py``) →
   ``symbol_index`` rows (name, kind, line, signature, etc.).
4. **Import extraction** — regex scan of ``import`` / ``from … import``
   statements → ``import_graph`` rows.
5. **Reference collection** — ``_RefIndexCollector`` visitor with
   ``QualifiedNameProvider + PositionProvider + ParentNodeProvider`` →
   ``reference_index`` rows (target QN, line, column, ref_kind).

After all workers finish, the main process performs three additional steps:

- **File manifest** — ``stat()`` every indexed file and writes
  ``(worktree_id, path, mtime_ns, size, content_hash, timestamp)`` to
  ``file_manifest``.  Each worktree maintains its own set of manifest rows.
- **Git HEAD** — runs ``git rev-parse HEAD`` and stores the SHA in
  ``index_meta`` under the key ``git_head:<worktree_id>``.
- **Type cache** — runs the configured type engine (pyrefly / pyright / ty)
  and stores results in ``type_cache``.

Workers write directly to the SQLite database (WAL mode permits concurrent
writers across processes).  Files whose content hash already appears in all
relevant tables are skipped without parsing.

How caches are invalidated
~~~~~~~~~~~~~~~~~~~~~~~~~~

Because caches are keyed on file content (MD5 hash), not file path, they are
**automatically correct** — if a file's content hasn't changed, its cached data
is still valid regardless of when it was written.  There is no explicit
"invalidation" of stale entries; old entries simply become unreachable when no
file on disk has that content anymore.

For the path-indexed tables (``file_manifest``, ``symbol_index``,
``reference_index``, ``import_graph``), a three-tier freshness check determines
which files need re-indexing:

**Tier 1 — Git HEAD (~1 ms).**  ``git rev-parse HEAD`` is compared against the
stored ``git_head:<worktree_id>`` in ``index_meta``.  If they match, no files
have changed since the last index in this worktree.

**Tier 2 — File stat (~10–50 ms for 5 000 files).**  Each file is ``stat()``-ed
and its ``(mtime_ns, size)`` compared against ``file_manifest``.  Files whose
mtime and size match are unchanged — no I/O required.

**Tier 3 — Content hash (only for stat-mismatched files).**  Files whose mtime
or size differ are read and hashed.  If the hash matches the manifest (e.g.
``git stash pop`` touched the mtime but didn't change content), the manifest's
mtime is updated in-place.  If the hash differs, the file is re-indexed: old
rows keyed on the previous content hash are deleted from ``symbol_index``,
``reference_index``, and ``import_graph``, then fresh rows are inserted.

This check is implemented in ``_scan_manifest()`` and exposed through
``_ensure_index_fresh()``, which commands call before querying the index.  If
fewer than 50 files are stale, they are re-indexed inline; otherwise the caller
falls back to the cold path or advises running ``emend index``.

How caches are cleaned
~~~~~~~~~~~~~~~~~~~~~~

emend does **not** aggressively prune old entries.  Content-hash keyed tables
(``parse_cache``, ``qn_index``, ``type_cache``) accumulate entries across
branch switches, which is intentional: switching back to an earlier branch
reuses those entries.  The in-memory parse cache is bounded at 256 entries and
self-evicting.

Path-indexed rows are kept consistent by the re-index cycle described above:
when a file's content changes, its old rows (keyed on the previous content
hash) are deleted before new rows are inserted.  Deleted files are removed from
``file_manifest`` and their derived rows are cleaned up during
``_ensure_index_fresh()``.

To reclaim disk space or force a full rebuild:

.. code-block:: bash

   # Delete the entire cache and rebuild from scratch:
   rm -rf .emend/cache/
   emend index

   # Or just rebuild (existing entries are overwritten):
   emend index

The ``emend index --status`` command reports the number of indexed files,
symbols, import edges, and references, plus how many files are stale.

Warm-path query acceleration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When the index is fresh, several commands bypass full-project scans:

- ``search --complete <prefix>`` queries ``symbol_index`` with a ``LIKE``
  prefix match — typically < 5 ms.
- ``refs`` queries ``reference_index`` by qualified name — typically < 10 ms.
- ``_files_importing_module()`` checks ``import_graph`` before falling back to
  the Rust ``files_importing_module`` scan.

All warm paths fall back transparently to their original (cold) implementations
when the index is unavailable or stale.

Git worktree support
~~~~~~~~~~~~~~~~~~~~

When emend runs inside a `git worktree
<https://git-scm.com/docs/git-worktree>`_, the cache is automatically shared
with the main repository.  This means:

- Running ``emend index`` in **any** worktree populates the shared
  ``parse.db``.  Other worktrees immediately benefit from the cached parse
  trees, qualified-name indexes, type information, symbol definitions, and
  reference data — all of which are keyed by content hash.
- Each worktree maintains its own ``file_manifest`` rows (scoped by a
  ``worktree_id`` derived from the worktree's absolute path), so stat-based
  freshness checks are accurate per worktree.
- Git HEAD tracking is per-worktree (``git_head:<worktree_id>`` keys in
  ``index_meta``), so branch switches in one worktree don't invalidate
  another.

The mechanism works by reading the ``.git`` file in the worktree root (which
contains a ``gitdir:`` pointer) and following the ``commondir`` reference to
locate the main repository.  ``_resolve_cache_root()`` in ``transform.py``
performs this resolution and caches the result.  For non-worktree repos (and
non-git projects), the project root is used directly — no behavior change.

SQLite WAL mode ensures that concurrent access from multiple worktrees (or
multiple emend processes) is safe: readers never block, and writes are
serialized with a configurable timeout.

Selector grammar
----------------

Selectors are parsed by a `Lark <https://github.com/lark-parser/lark>`_ grammar
(``grammars/selector.lark``) into ``ExtendedSelector`` dataclasses.  The grammar
handles:

- dotted symbol paths with wildcard segments (``Class.*``, ``Test*``)
- component accessors (``[params]``, ``[returns]``, ``[decorators]``, ``[bases]``,
  ``[body]``, ``[imports]``)
- by-name and by-index sub-accessors (``[ctx]``, ``[0]``)
- pseudo-classes (``:KEYWORD_ONLY``, ``:POSITIONAL_ONLY``, ``:POSITIONAL_OR_KEYWORD``)
- line-range selectors (``file.py:42-100``)
- file glob expansion (``src/**/*.py::*[params]``)

Pattern grammar
---------------

Patterns are parsed by a second Lark grammar (``grammars/pattern.lark``) into
``Pattern`` dataclasses containing ``MetaVar`` objects.  ``compile_pattern_to_matcher()``
translates a ``Pattern`` into a LibCST matcher tree.  A separate
``compile_pattern_to_rust_ir()`` path translates simple patterns into a JSON IR
consumed by the Rust tree-sitter engine; patterns that use features the Rust
engine does not support (e.g. complex structural constraints) fall through to
the LibCST path automatically.

Metavariable types:

- ``$X`` — captures any expression node
- ``$_`` — anonymous, matches any expression and discards the capture
- ``$...ARGS`` — variadic capture (sequence of arguments)
- ``$X:str`` / ``$X:int`` / ``$X:call`` / ``$X:attr`` — syntactic type-constrained capture
- ``$X:type[T]`` / ``$X:returns[T]`` — inferred type constraint (requires TypeOracle; see below)

TypeOracle layer
----------------

``type_oracle.py`` provides a pluggable type inference adapter used by ``search``
(for ``:type[X]`` / ``:returns[X]`` pattern constraints and ``--returns`` lookup
filtering) and the ``types`` command.

Abstract interface: ``TypeOracle``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``TypeOracle`` is an abstract base class with four abstract methods:

- ``infer_file(path, project_root) → FileTypes`` — return all type bindings for a file
- ``type_at(path, line, col) → TypeBinding | None`` — return the binding at a position
- ``clear_cache()`` — evict cached results
- ``is_available() → bool`` — check if the backing tool is installed

Results are returned as ``FileTypes`` (a list of ``TypeBinding`` objects with
positional and name indexes built by ``FileTypes.build_index()``).  ``TypeBinding``
records the name, source location, ``raw_type`` string from the engine, and a
parsed ``TypeDescriptor`` tree.

Backends
~~~~~~~~

Three backends are provided:

- **PyreflyAdapter** — shells out to ``pyrefly check --debug-info`` and parses the
  JSON binding dump.  This is the most comprehensive source of type information
  (full binding dump, not just diagnostics) but requires pyrefly to be installed.
  Supports ``infer_batch()`` for multi-file queries in one subprocess call.

- **PyrightAdapter** — starts ``pyright-langserver`` via the LSP protocol and
  queries ``textDocument/hover`` for each identifier collected by ``_collect_symbols()``.
  Type strings are extracted from the hover markdown and parsed into ``TypeDescriptor``
  trees.  The LSP process is started once and reused across calls.

- **TyAdapter** — same approach as PyrightAdapter but using ``ty lsp``.

All three cache results keyed on the MD5 of the file's content (``_FileTypeCache``,
bounded at 256 entries with FIFO eviction) so unchanged files are not re-analyzed.

Type string parsing
~~~~~~~~~~~~~~~~~~~

``parse_type_string(raw)`` converts type strings from any backend into a
``TypeDescriptor`` tree, handling named types, parameterized generics
(``list[int]``, ``dict[str, int]``), union types (``str | None``), callable
signatures (``(x: int) -> str``), and ``Self@ClassName`` prefixes from Pyrefly.
``TypeDescriptor.matches(constraint)`` performs structural matching: an unknown
constraint acts as a wildcard, a named constraint matches parameterized types by
base name, and union types match if any member satisfies the constraint.

Integration with pattern matching
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``:type[X]`` and ``:returns[X]`` constraint tokens are matched syntactically by
``m.DoNotCare()`` (any node passes) and then *post-filtered* by
``_filter_matches_by_type_oracle()`` in ``transform.py``.  This keeps the Rust
fast-path bypass simple: any oracle constraint skips the Rust engine and runs
the full LibCST path (which provides ``PositionProvider`` metadata needed to
look up nodes by position).

Engine autodetection
~~~~~~~~~~~~~~~~~~~~~

``detect_type_engine(project_root)`` checks for config files in order
(``pyrightconfig.json`` → ``ty.toml`` → ``pyrefly.toml`` → ``pyproject.toml``
sections), then falls back to tool availability on PATH
(pyrefly → ty → pyright).  ``create_type_oracle(engine="auto")`` combines
detection and instantiation in one call.

Lint engine
-----------

``lint.py`` loads rules from ``.emend/patterns.yaml``.  Each rule specifies a
``find`` pattern, a ``message``, an optional ``not-inside`` constraint, and an
optional ``replace`` pattern for ``--fix`` mode.

The lint engine applies a two-tier scan:

1. **Rust fast-path** — rules whose ``find`` pattern compiles to Rust IR are
   batched into a single ``find_multi_patterns_in_files`` call.  This handles
   the common case of simple pattern rules (function calls, attribute accesses)
   with no structural scope constraint.
2. **LibCST path** — rules with complex patterns or ``not-inside`` constraints
   that don't compile to Rust IR are evaluated per-file using
   ``find_pattern()`` with full LibCST scope resolution.

``# noqa`` suppression is implemented by tokenizing the source for ``# noqa``
comments, then mapping each comment to its enclosing statement range via
``_StatementRangeMapper`` (a LibCST ``CSTVisitor``).  A suppressed statement
suppresses all matches inside it.

Free-threaded Python
---------------------

Python 3.13 introduced an experimental **free-threaded** build (``--disable-gil``)
and 3.14 continues this as a supported configuration.  emend is designed to
take full advantage:

- ``emend_core`` is registered ``gil_used = false``, so all Rust functions
  release the GIL immediately and can run on multiple OS threads simultaneously.
- LibCST's ``MetadataWrapper`` is run through a ``ThreadPoolExecutor`` on
  free-threaded Python for cross-file operations (``rename``, ``refs``).
- The ``_cached_parse()`` cache uses a fine-grained ``threading.Lock`` that is
  only held during dict read/write, not during parsing, so threads rarely
  contend on it.

To enable free-threaded speedups, install emend with a free-threaded Python:

.. code-block:: bash

   uv tool install --python 3.14t emend

Build system
------------

The project uses `maturin <https://www.maturin.rs>`_ as its build backend.
``pyproject.toml`` declares ``build-backend = "maturin"`` and a ``[tool.maturin]``
section that points at ``rust/Cargo.toml`` and sets ``module-name =
"emend.emend_core"``.  maturin compiles the Rust crate and packages the resulting
shared library alongside the Python source into a single platform wheel.  Users
receive one wheel with no additional binary dependencies.

Version numbers are stored in ``rust/Cargo.toml`` and propagated to Python
metadata automatically by maturin (``dynamic = ["version"]`` in ``pyproject.toml``).
