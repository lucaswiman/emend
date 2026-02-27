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

Parse caching
~~~~~~~~~~~~~

``transform._cached_parse()`` maintains an LRU cache (256 entries) keyed on
the MD5 of the source text.  Repeated lookups for the same unchanged file are
free after the first parse.  The cache is thread-safe via a ``threading.Lock``
and evicts the oldest 25 % of entries when it reaches capacity.

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
- ``$X:str`` / ``$X:int`` / ``$X:call`` / ``$X:attr`` — type-constrained capture

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
