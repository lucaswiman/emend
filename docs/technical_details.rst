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
  capture syntax (``$X``, ``$...ARGS``) compiled to a unified structural matcher.

Both systems parse source files into a concrete syntax tree (CST) using
`Tree-sitter <https://tree-sitter.github.io>`_ via a Rust backend.
Transformations produce a sequence of byte-range edits that are applied to the
original source, ensuring that untouched code is reproduced character-for-character
while maintaining high performance.

Two-layer architecture
-----------------------

Rust layer: ``emend_core``
~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``emend_core`` extension is a `PyO3 <https://pyo3.rs>`_ / `maturin
<https://www.maturin.rs>`_ Rust crate compiled directly into the emend wheel.
It handles all performance-critical AST analysis and manipulation:

- **File discovery** — parallel directory walk via ``rayon``.
- **Unified Scope Resolver** — a multi-language-capable engine that builds
  scope trees and resolves qualified names.  Scoping and binding rules are
  driven by a language configuration file (e.g., ``languages/python.toml``).
- **Structural Matcher** — a generic matcher that executes structural queries
  and captures metavariables directly on the Tree-sitter AST.
- **Mutation Engine** — ``PyFileTransform`` manages a set of non-overlapping
  byte-range replacements, providing the foundation for all code edits.
- **Symbol collection** — extracts definition trees with rich metadata
  (signatures, return types, visibility) for the ``search --output summary``
  command.
- **Reference analysis** — resolves all identifiers and attributes to
  qualified names, supporting ``refs``, ``rename``, and ``deadcode``.

Python layer: CLI and Orchestration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Python layer provides the user-facing CLI and orchestrates complex
refactoring workflows:

- **CLI Entry Point** — command definitions and argument parsing using `Typer`.
- **Query Parsing** — Lark grammars for the selector and pattern languages.
- **Transform Orchestration** — higher-level refactoring logic (e.g., ``move``,
  ``copy-to``) that combines primitives like symbol extraction and byte-range
  edits.
- **Type Oracle integration** — adapters for external type inference engines
  (Pyright, Pyrefly) that provide optional semantic metadata.

Language Configuration
----------------------

emend's scoping and binding logic is language-agnostic and data-driven. Each
supported language is defined by a TOML configuration file that specifies:

- **Scope Creators** — AST node types that create new lexical scopes (functions,
  classes, comprehensions).
- **Binding Rules** — rules for how names are introduced (assignments, loop
  variables, parameters).
- **Import Resolution** — strategies for resolving cross-file qualified names.
- **Visibility Rules** — language-specific conventions for public vs. private
  API detection.

Adding a new language
---------------------

To add support for a new language, create a directory under ``languages/`` with
two files:

.. code-block:: text

   languages/<lang_name>/
   ├── config.toml       # scope resolver configuration
   └── symbols.scm       # tree-sitter query for symbol extraction

config.toml
~~~~~~~~~~~

The configuration file drives the scope resolver and qualified-name builder.
Use ``languages/python/config.toml`` as a reference.  The file contains the
following sections:

``[language]``
   Top-level metadata: language ``name``, the ``tree_sitter_grammar`` crate to
   use for parsing, ``file_extensions`` (e.g., ``[".py"]``), and ``keywords``
   that the pattern compiler should treat as reserved.

``[scoping]``
   Declares which AST node types create new scopes (``scope_creators``) and per-
   kind rules.  Each scope kind has an ``is_closure_boundary`` flag that controls
   whether name lookups propagate outward (e.g., Python class scopes are closure
   boundaries, so inner functions cannot see class-level names directly).

``[bindings]``
   Rules for how names are introduced into a scope: assignments, loop targets,
   function/class parameters, ``with`` targets, exception handler names, and
   definition nodes (functions, classes).  Each rule maps an AST node type to
   the child field that holds the bound name.

``[imports]``
   Describes the structure of import statements so the resolver can extract
   module paths and bound names.  ``resolution`` selects one of the built-in
   resolvers (see below).

``[qualified_names]``
   Controls how qualified names are assembled: ``module_separator`` (``"."`` for
   Python), ``class_member_prefix`` and ``nested_function_prefix`` flags, and
   the ``locals_marker`` string for nested scopes.

``[exports]``
   Public API conventions such as ``__all__`` membership or naming patterns
   that mark a symbol as an entry point for dead-code analysis.

``[builtins]``
   A list of names that are always in scope and do not need resolution (e.g.,
   ``print``, ``len``, ``True`` in Python).

symbols.scm
~~~~~~~~~~~~

A `tree-sitter query <https://tree-sitter.github.io/tree-sitter/using-parsers/queries/>`_
file that defines which AST nodes constitute symbols for the ``search --output
summary`` and ``deadcode`` commands.  The Python query captures function
definitions, class definitions, decorated definitions, and top-level variable
assignments.  Each captured node should use a ``@name`` capture for the
symbol's identifier and a top-level capture (e.g., ``@function``, ``@class``,
``@variable``) for the full node.

Import resolution strategies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Rust backend provides built-in import resolution strategies selected by
the ``resolution`` field in ``[imports]``:

- ``"python"`` — follows importlib semantics: ``sys.path``, ``src/`` layout
  detection, relative imports, and namespace packages.
- ``"node"`` — implements Node.js module resolution for JavaScript and
  TypeScript: ``node_modules`` lookup, ``index.js`` defaults, and
  ``package.json`` ``main``/``exports`` fields.

Adding a new resolution strategy requires changes to the Rust ``emend_core``
crate.

Testing
~~~~~~~

After adding a new language configuration, run the full test suite to verify
that the scope resolver, symbol collection, and reference analysis all behave
correctly:

.. code-block:: bash

   make test

Pay particular attention to qualified-name construction and import resolution,
as errors in those areas propagate to ``refs``, ``rename``, and ``deadcode``.

Caching and indexing
--------------------

emend maintains a cache at ``.emend/cache/parse.db`` (SQLite, WAL mode).
The cache is content-addressed — almost every key is the MD5 of the file's
source text — so reverting edits within a checkout naturally reuses earlier
entries.  A ``.gitignore`` and ``.dockerignore`` are auto-generated inside
``.emend/cache/`` to prevent the database from being checked in.  Each git
worktree owns its cache because the manifest, editor projections, and fact
graph describe one mutable source snapshot.

Overview of cache tables
~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Table
     - Key
     - Contents
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
       via stat-only scans. The worktree identifier also keeps metadata valid
       if a checkout is moved or reused.
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

How caches are populated
~~~~~~~~~~~~~~~~~~~~~~~~

There are two population paths:

**Lazy (on first use).**  ``visit_project()`` populates ``qn_index`` as a
side-effect of running the Rust ``PyScopeResolver``: after each file is
resolved, the qualified-name set is stored in the cache.

**Eager (``emend tool index``).**  ``warm_caches()`` scans the project in parallel
using a ``ProcessPoolExecutor``.  Each worker subprocess (``_index_batch()``)
receives a batch of ``(file_path, source_text)`` tuples and performs:

1. **QN resolution** — ``PyScopeResolver`` → compressed pickle → ``qn_index``.
2. **Symbol collection** — ``emend_core.collect_symbols_from_str()`` →
   ``symbol_index`` rows (name, kind, line, signature, etc.).
3. **Import extraction** — regex scan of ``import`` / ``from … import``
   statements → ``import_graph`` rows.
4. **Reference collection** — ``PyScopeResolver`` reference output →
   ``reference_index`` rows (target QN, line, column, ref_kind).

All analysis is handled by the Rust tree-sitter backend.

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
relevant tables are skipped.

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
falls back to the cold path or advises running ``emend tool index``.

How caches are cleaned
~~~~~~~~~~~~~~~~~~~~~~

emend does **not** aggressively prune old entries.  Content-hash keyed tables
(``qn_index``, ``type_cache``) accumulate entries across branch switches, which
is intentional: switching back to an earlier branch reuses those entries.

Path-indexed rows are kept consistent by the re-index cycle described above:
when a file's content changes, its old rows (keyed on the previous content
hash) are deleted before new rows are inserted.  Deleted files are removed from
``file_manifest`` and their derived rows are cleaned up during
``_ensure_index_fresh()``.

To reclaim disk space or force a full rebuild:

.. code-block:: bash

   # Delete the entire cache and rebuild from scratch:
   rm -rf .emend/cache/
   emend tool index

   # Or just rebuild (existing entries are overwritten):
   emend tool index

The ``emend tool index --status`` command reports the number of indexed files,
symbols, import edges, and references, plus how many files are stale.

Warm-path query acceleration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When the index is fresh, several commands bypass full-project scans:

- ``find --complete <prefix>`` queries ``symbol_index`` with a ``LIKE``
  prefix match — typically < 5 ms.
- ``analyze refs`` queries ``reference_index`` by qualified name — typically < 10 ms.
- ``_files_importing_module()`` checks ``import_graph`` before falling back to
  the Rust ``files_importing_module`` scan.

All warm paths fall back transparently to their original (cold) implementations
when the index is unavailable or stale.

Git worktree support
~~~~~~~~~~~~~~~~~~~~

When emend runs inside a `git worktree
<https://git-scm.com/docs/git-worktree>`_, ``parse.db`` and ``facts.db`` stay
under that worktree's own ``.emend/cache/``.  Content hashes are safe to share,
but the databases also contain mutable manifests, full-text projections, and a
project-wide fact snapshot; sharing those structures lets one branch overwrite
another branch's analysis.

User-managed mappings are different: they are durable input rather than a
recomputable snapshot.  For those files, emend follows the worktree's
``commondir`` reference and stores the mapping data in the main checkout's
``.emend/`` directory.

SQLite WAL mode still protects concurrent readers and writers operating on the
same worktree cache.

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
``Pattern`` dataclasses containing ``MetaVar`` objects.
``compile_pattern_to_rust_ir()`` translates a ``Pattern`` into a JSON IR
consumed by the Rust structural matcher engine, which performs matching directly
on the Tree-sitter AST.

Metavariable types:

- ``$X`` — captures any expression node
- ``$_`` — anonymous, matches any expression and discards the capture
- ``$...ARGS`` — variadic capture (sequence of arguments)
- ``$X:str`` / ``$X:int`` / ``$X:call`` / ``$X:attr`` — syntactic type-constrained capture
- ``$X:type[T]`` / ``$X:returns[T]`` — inferred type constraint (requires TypeOracle; see below)

TypeOracle layer
----------------

``type_oracle.py`` provides a pluggable type inference adapter used by ``find``
(for ``:type[X]`` / ``:returns[X]`` pattern constraints and ``--returns`` lookup
filtering) and the ``analyze types`` command.

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
fast-path bypass simple: any oracle constraint is post-filtered after the Rust
structural matcher returns positional match data.

Engine autodetection
~~~~~~~~~~~~~~~~~~~~~

``detect_type_engine(project_root)`` checks for config files in order
(``pyrightconfig.json`` → ``ty.toml`` → ``pyrefly.toml`` → ``pyproject.toml``
sections), then falls back to tool availability on PATH
(pyrefly → ty → pyright).  ``create_type_oracle(engine="auto")`` combines
detection and instantiation in one call.

Lint engine
-----------

``lint.py`` loads rules from ``.emend/rules.yaml`` (with legacy fallback to
``.emend/patterns.yaml``).  Each rule specifies a ``match`` pattern, a
``message``, an optional ``not-within`` constraint, and an optional ``fix``
pattern for ``--fix`` mode.

The lint engine applies a two-tier scan:

1. **Batch Rust path** — rules whose ``match`` pattern compiles to Rust IR are
   batched into a single ``find_multi_patterns_in_files`` call.  This handles
   the common case of simple pattern rules (function calls, attribute accesses)
   with no structural scope constraint.
2. **Single-file Rust path** — rules with complex patterns or ``not-within``
   constraints are evaluated per-file using ``find_pattern()`` with the Rust
   structural matcher.

``# noqa`` suppression is implemented by tokenizing the source for ``# noqa``
comments, then mapping each comment to its enclosing statement range.  A
suppressed statement suppresses all matches inside it.

Free-threaded Python
---------------------

Python 3.13 introduced an experimental **free-threaded** build (``--disable-gil``)
and 3.14 continues this as a supported configuration.  emend is designed to
take full advantage:

- ``emend_core`` is registered ``gil_used = false``, so all Rust functions
  release the GIL immediately and can run on multiple OS threads simultaneously.
- Cross-file operations (``rename``, ``refs``) use a ``ThreadPoolExecutor`` on
  free-threaded Python, with the Rust extension performing GIL-free analysis
  across threads.

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
