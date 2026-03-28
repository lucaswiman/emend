Commands Reference
===================

search
------

Unified search command that auto-detects the mode from the query. If the query contains metavariables (``$X``, ``$...Y``), it uses **pattern matching** mode. When ``::`` is present, the right side is auto-detected: if it parses as a valid selector, it uses **symbol lookup** mode; otherwise it's treated as a code pattern (e.g. ``**::assert False``). A bare file/directory path with no filters shows a **symbol summary**.

Also available as: ``query``, ``show``, ``get``, ``lookup``, ``find`` for intuitive workflows.

.. code-block:: text

   emend search QUERY [PATH] [OPTIONS]

**Arguments:**

- ``QUERY`` -- A pattern with ``$X`` metavars (e.g. ``print($X)``), a selector (e.g. ``file.py::func``), a file-scoped pattern (e.g. ``**::assert False``), or a bare file/dir path (summary/lookup mode)
- ``PATH`` -- File, glob, or directory to search (pattern mode). Alternative to embedding the file scope via ``::`` in the query.

**Output format (--output / -o):**

Auto-detected when not specified. Use ``--output=FORMAT`` or ``--output=BASE::MODIFIER``:

+---------------------+----------------------------------------+
| Mode                | Default when...                        |
+=====================+========================================+
| ``code``            | Selector without component             |
+---------------------+----------------------------------------+
| ``location``        | Pattern mode                           |
+---------------------+----------------------------------------+
| ``selector``        | Bare file/dir path with filters        |
+---------------------+----------------------------------------+
| ``summary``         | Bare file/dir path, no filters         |
+---------------------+----------------------------------------+
| ``metadata``        | Explicit only                          |
+---------------------+----------------------------------------+
| ``json``            | JSON structured output                 |
+---------------------+----------------------------------------+
| ``count``           | Count of matches only                  |
+---------------------+----------------------------------------+

**Output modifiers (appended with ``::`` to base format):**

+---------------------+----------------------------------------+
| Modifier            | Description                            |
+=====================+========================================+
| ``flat``            | Flat list (summary mode)               |
+---------------------+----------------------------------------+
| ``dedent``          | Dedent source code (code mode)         |
+---------------------+----------------------------------------+

**Options (all modes):**

+-----------------------+-------------------------------------------+
| Option                | Description                               |
+=======================+===========================================+
| ``--output``, ``-o``  | Output format: code, location, selector,  |
|                       | summary, metadata, json, count, with      |
|                       | optional modifiers (::flat, ::dedent)     |
+-----------------------+-------------------------------------------+
| ``--imported-from MODULE`` | Only when imported from given module  |
+-----------------------+-------------------------------------------+
| ``--scope-local``   | Only match locally-defined names       |
+-----------------------+-------------------------------------------+
| ``--type-engine``     | Type inference engine for ``:type[X]``    |
|                       | and ``:returns[X]`` constraints:          |
|                       | ``auto`` (default), ``pyrefly``,          |
|                       | ``pyright``, ``ty``                       |
+-----------------------+-------------------------------------------+

**Options (lookup/filter mode):**

+---------------------+----------------------------------------+
| Option              | Description                            |
+=====================+========================================+
| ``--kind TEXT``     | Filter by symbol kind                  |
+---------------------+----------------------------------------+
| ``--name TEXT``     | Filter by name (glob or ``/regex/``)  |
+---------------------+----------------------------------------+
| ``--returns TEXT``  | Filter by return type annotation       |
+---------------------+----------------------------------------+
| ``--depth TEXT``    | Filter by nesting depth                |
+---------------------+----------------------------------------+
| ``--has-param TEXT`` | Filter by parameter name              |
+---------------------+----------------------------------------+
| ``-i``              | Case-insensitive name matching         |
+---------------------+----------------------------------------+
| ``--smart-case``    | Match naming convention variants       |
+---------------------+----------------------------------------+

**Options (pattern mode only):**

+---------------------+----------------------------------------+
| Option              | Description                            |
+=====================+========================================+
| ``--where EXPR``    | Filter/scope constraint (syntax:       |
|                     | 'def test_*', 'not class', 'MyClass',  |
|                     | '@decorator', 'print($X)')             |
+---------------------+----------------------------------------+

**Examples:**

.. code-block:: bash

   # Pattern mode (has $ or non-selector after ::):
   emend search 'print($X)' src/
   emend search '**::print($X)'
   emend search '**::assert False'
   emend search 'src/::import os'
   emend search 'assertEqual($A, $B)' tests/ --output count

   # Lookup mode (valid selector after ::):
   emend search file.py::func[params]
   emend search src/ --kind function

   # Summary mode (bare file/dir, no filters):
   emend search api.py
   emend search api.py --output summary::flat
   emend search api.py::MyClass --output summary
   emend search api.py --output code::dedent

   # Metadata mode:
   emend search api.py::my_func --output metadata

---

edit
----

Modify or remove existing symbol components. Shows a diff by default; use ``--apply`` to write changes.

.. code-block:: text

   emend edit SELECTOR [VALUE] [OPTIONS]

**Arguments:**

- ``SELECTOR`` -- Full selector like ``file.py::Symbol[component]`` or ``file.py::Symbol[component][accessor]``
- ``VALUE`` -- New value (omit with ``--rm`` to remove)

**Options:**

+-----------+-----------------------------------------------+
| Option    | Description                                   |
+===========+===============================================+
| ``--rm``  | Remove the component or symbol                |
+-----------+-----------------------------------------------+
| ``--apply`` | Write changes to disk (default is dry-run) |
+-----------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # Change return type (dry-run)
   emend edit api.py::get_user[returns] "User | None"

   # Apply the change
   emend edit api.py::get_user[returns] "User | None" --apply

   # Add return annotation to unannotated function
   emend edit api.py::process[returns] "None" --apply

   # Replace entire parameter list
   emend edit api.py::get_user[params] "user_id: int, db: Session" --apply

   # Modify a single parameter by name
   emend edit api.py::get_user[params][user_id] "user_id: UUID" --apply

   # Modify a parameter by index
   emend edit api.py::get_user[params][0] "user_id: UUID" --apply

   # Replace all decorators
   emend edit api.py::MyClass[decorators] "@dataclass\n@frozen" --apply

   # Replace base classes
   emend edit models.py::User[bases] "Base, TimestampMixin" --apply

   # Remove a parameter
   emend edit api.py::get_user[params][debug] --rm --apply

   # Remove all decorators
   emend edit api.py::get_user[decorators] --rm --apply

   # Remove a function entirely
   emend edit api.py::deprecated_func --rm --apply

---

add
---

Insert new items into list components (params, decorators, bases). Shows a diff by default; use ``--apply`` to write.

.. code-block:: text

   emend add SELECTOR VALUE [OPTIONS]

**Arguments:**

- ``SELECTOR`` -- Selector ending in a list component, optionally with a pseudo-class for param kind:
  ``file.py::func[params]:POSITIONAL_OR_KEYWORD``
- ``VALUE`` -- The item to insert

**Pseudo-classes for ``[params]``:**

+---------------------------+------------------------------------------+
| Pseudo-class              | Description                              |
+===========================+==========================================+
| ``:POSITIONAL_OR_KEYWORD`` | Regular parameter (default)              |
+---------------------------+------------------------------------------+
| ``:KEYWORD_ONLY``         | Keyword-only parameter (after ``*``)    |
+---------------------------+------------------------------------------+
| ``:POSITIONAL_ONLY``      | Positional-only parameter (before ``/``) |
+---------------------------+------------------------------------------+

**Position options:**

+-----------+----------------------------------------+
| Option    | Description                            |
+===========+========================================+
| ``--at N``  | Insert at position N (0-indexed)       |
+-----------+----------------------------------------+
| ``--before NAME`` | Insert before the named item    |
+-----------+----------------------------------------+
| ``--after NAME`` | Insert after the named item      |
+-----------+----------------------------------------+
| ``--apply`` | Write changes to disk                  |
+-----------+----------------------------------------+

**Examples:**

.. code-block:: bash

   # Append a parameter
   emend add api.py::get_user[params] "timeout: int = 30" --apply

   # Prepend a parameter
   emend add api.py::get_user[params] "self" --at 0 --apply

   # Insert between existing parameters
   emend add api.py::get_user[params] "ctx: Context" --before user_id --apply

   # Add keyword-only parameter
   emend add api.py::get_user[params]:KEYWORD_ONLY "force: bool = False" --apply

   # Add a decorator at top
   emend add api.py::get_user[decorators] "@cache" --at 0 --apply

   # Add a base class
   emend add models.py::User[bases] "TimestampMixin" --apply

---

replace
-------

Replace code patterns. Dry-run by default; use ``--apply`` to write.

.. code-block:: text

   emend replace PATTERN REPLACEMENT PATH [OPTIONS]

**Arguments:**

- ``PATTERN`` -- Pattern to search for (with metavariables).
- ``REPLACEMENT`` -- Replacement (can reference captured metavariables).
- ``PATH`` -- File, glob, or directory.

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--apply``         | Write changes to disk (default is dry-run)   |
+---------------------+-----------------------------------------------+
| ``--where EXPR``    | Filter by scope/structure (syntax:            |
|                     | 'def', 'class', 'MyClass.method', 'not ...')  |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # Replace print with logger.info (dry-run)
   emend replace 'print($X)' 'logger.info($X)' src/

   # Apply the replacement
   emend replace 'print($X)' 'logger.info($X)' src/ --apply

   # Replace only inside functions (structural constraint)
   emend replace 'old_api($X)' 'new_api($X)' src/ --where 'def' --apply

   # Replace unittest assertions
   emend replace 'assertEqual($A, $B)' 'assert $A == $B' tests/ --apply

   # Replace only inside a specific method (scope constraint)
   emend replace 'log($X)' 'logger.info($X)' app.py --where 'MyClass.process' --apply

   # Replace outside a structure
   emend replace '$X = $Y' '$X: int = $Y' src/ --where 'not class' --apply

---

lint
----

Lint files using pattern rules from a YAML config file.

.. code-block:: text

   emend lint PATH [OPTIONS]

Reads rules from ``.emend/patterns.yaml`` by default. Rules define patterns to find and optional replacements.

**Arguments:**

- ``PATH`` -- File or directory to lint

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--config FILE``   | Path to patterns.yaml config                  |
+---------------------+-----------------------------------------------+
| ``--fix``           | Auto-apply associated ``replace`` patterns    |
+---------------------+-----------------------------------------------+
| ``--rule NAME``     | Run only a specific rule by name              |
+---------------------+-----------------------------------------------+

**Config file format (**.emend/patterns.yaml**):**

.. code-block:: yaml

   macros:
     test_assert: "self.assert$METHOD($...ARGS)"
     api_call: "requests.$METHOD($URL, $...KWARGS)"

   rules:
     no-bare-assert:
       find: "assert $X"
       message: "Use pytest assertions instead"
     no-print:
       find: "print($...ARGS)"
       not-inside: "def test_*"
       message: "Use logger instead of print"
       replace: "logger.info($...ARGS)"

Macros can be referenced in rules using ``{macro_name}`` syntax.

**Examples:**

.. code-block:: bash

   # Lint all source files
   emend lint src/

   # Use a custom config
   emend lint src/ --config .emend/patterns.yaml

   # Auto-fix violations that have a replace rule
   emend lint src/ --fix

   # Run only a specific rule
   emend lint src/ --rule no-print

---

batch
-----

Apply batch refactoring operations from a YAML or JSON file. Dry-run by default; use ``--apply`` to write.

.. code-block:: text

   emend batch OPS_FILE [OPTIONS]

**Arguments:**

- ``OPS_FILE`` -- YAML or JSON file containing operations

**Options:**

+-----------+-----------------------------------------------+
| Option    | Description                                   |
+===========+===============================================+
| ``--apply`` | Apply changes (default is dry-run)          |
+-----------+-----------------------------------------------+

**Operation types:**

+-------+-----------------------------------+-----------------------------------+
| Type  | Required fields                   | Description                       |
+=======+===================================+===================================+
| rename| ``selector``, ``to``              | Rename a symbol                   |
+-------+-----------------------------------+-----------------------------------+
| replace | ``pattern``, ``replacement``, ``path`` | Pattern replace                |
+-------+-----------------------------------+-----------------------------------+
| add   | ``selector``, ``value``           | Add to a component                |
+-------+-----------------------------------+-----------------------------------+
| edit  | ``selector``, ``value``           | Edit a component                  |
+-------+-----------------------------------+-----------------------------------+
| remove| ``selector``                      | Remove a component or symbol      |
+-------+-----------------------------------+-----------------------------------+

**Example YAML:**

.. code-block:: yaml

   operations:
     - rename: {selector: "api.py::get_user", to: "fetch_user"}
     - replace:
         pattern: "get_user($ID)"
         replacement: "fetch_user(user_id=$ID)"
         path: "src/"
     - add:
         selector: "api.py::fetch_user[params]:KEYWORD_ONLY"
         value: "timeout: float = 30.0"
     - edit:
         selector: "api.py::fetch_user[returns]"
         value: "User | None"
     - remove:
         selector: "api.py::deprecated_func"

**Examples:**

.. code-block:: bash

   # Preview all changes (dry-run)
   emend batch refactor.yaml

   # Apply changes
   emend batch refactor.json --apply

---

graph
-----

Generate a call graph for all functions in a file.

.. code-block:: text

   emend graph FILE [OPTIONS]

**Options:**

+-----------+-----------------------------------------------+
| Option    | Description                                   |
+===========+===============================================+
| ``--format``, ``-f`` | Output format: ``plain`` (default), ``json``, ``dot`` |
+-----------+-----------------------------------------------+
| ``--project``, ``-p`` | Project root directory              |
+-----------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # Human-readable text output
   emend graph src/module.py

   # Graphviz DOT format (pipe to dot for visualization)
   emend graph src/module.py --format dot | dot -Tsvg > deps.svg

   # JSON adjacency list
   emend graph src/module.py --format json

---

deadcode
--------

Find potentially dead (unreferenced) code in a project. Scans all Python files and reports top-level symbols that have no references outside their own definition, using scope-aware analysis.

Also available as: ``dead-code``, ``dead_code``.

.. code-block:: text

   emend deadcode [PATH] [OPTIONS]

**Arguments:**

- ``PATH`` -- Project directory to scan (default: ``.``)

**Options:**

+-------------------------------+-----------------------------------------------+
| Option                        | Description                                   |
+===============================+===============================================+
| ``--kind``, ``-k``           | Filter by symbol kind: ``function``, ``class``|
+-------------------------------+-----------------------------------------------+
| ``--include-private``         | Include ``_private`` symbols (excluded by      |
|                               | default)                                      |
+-------------------------------+-----------------------------------------------+
| ``--json``                    | Output as JSON                                |
+-------------------------------+-----------------------------------------------+
| ``--exclude-references-from`` | Directories to ignore when scanning for       |
|                               | references (e.g. ``tests/``). Supports glob   |
|                               | patterns (``*``, ``**``, ``?``). Repeatable.  |
+-------------------------------+-----------------------------------------------+
| ``--no-strings``              | Don't count string literals as references     |
+-------------------------------+-----------------------------------------------+
| ``--no-last-reference``       | Don't show git last-reference info            |
+-------------------------------+-----------------------------------------------+
| ``--entry-point-decorator``   | Additional decorator names to treat as entry  |
|                               | points. Symbols with these decorators are     |
|                               | never flagged. Can be repeated.               |
+-------------------------------+-----------------------------------------------+
| ``--entry-point-name``        | Additional function/class names to treat as   |
|                               | entry points. Can be repeated.                |
+-------------------------------+-----------------------------------------------+
| ``--exclude-path``            | Directories to exclude entirely from          |
|                               | analysis. Supports glob patterns (``*``,      |
|                               | ``**``, ``?``). Repeatable.                   |
+-------------------------------+-----------------------------------------------+

**Automatic exclusions:**

The following are automatically skipped (not reported as dead):

- Dunder methods (``__init__``, ``__str__``, etc.)
- Test functions/classes (``test_*``, ``Test*``)
- Decorated entry points (``@app.command``, ``@pytest.fixture``, ``@click.command``, ``@celery.task``, etc.)
- Symbols listed in ``__all__``
- Conventional entry points (``main``, ``setup``, ``teardown``)
- Private symbols (``_name``) unless ``--include-private`` is set
- Symbols with ``# noqa: emend:deadcode`` on the definition line
- Custom decorators specified via ``--entry-point-decorator``
- Custom names specified via ``--entry-point-name``

**String-as-reference detection:**

By default, string literals containing the symbol name are treated as references.
This reduces false positives from dynamic dispatch (``getattr(obj, "method")``),
serialization frameworks, and similar patterns. Disable with ``--no-strings``.

**Last reference tracking:**

By default, each dead symbol is annotated with the last git commit that mentioned
its name (via ``git log -S``), helping you decide whether to remove it. Disable
with ``--no-last-reference``.

**Examples:**

.. code-block:: bash

   # Scan a project for dead code
   emend deadcode src/

   # Only functions
   emend deadcode . --kind function

   # Include private symbols, output as JSON
   emend deadcode . --include-private --json

   # Ignore references from tests
   emend deadcode src/ --exclude-references-from tests/

   # Disable string matching and git annotation
   emend deadcode . --no-strings --no-last-reference

   # Custom entry point exclusions
   emend deadcode . --entry-point-decorator my_framework.handler
   emend deadcode . --entry-point-name plugin_init

   # Exclude entire directories from analysis
   emend deadcode . --exclude-path frontends/devtools/ --exclude-path scripts/

**Inline suppression:**

Suppress false positives with ``# noqa: emend:deadcode``:

.. code-block:: python

   def my_entry_point():  # noqa: emend:deadcode
       ...

A bare ``# noqa`` also suppresses the deadcode rule.

---

copy-to
-------

Copy a symbol to another file.

.. code-block:: text

   emend copy-to SELECTOR DESTINATION [OPTIONS]

**Options:**

+-----------+-----------------------------------------------+
| Option    | Description                                   |
+===========+===============================================+
| ``--append`` | Append to the destination file              |
+-----------+-----------------------------------------------+
| ``--dedent`` | Remove common indentation (for nested fn) |
+-----------+-----------------------------------------------+
| ``--apply``, ``-a`` | Write changes to disk               |
+-----------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend copy-to utils.py::helper other.py --apply
   emend copy-to utils.py::MyClass other.py --append --apply
   emend copy-to module.py::Outer.inner_func other.py --dedent --apply

---

refs
----

Find all references to a symbol across the project using tree-sitter-based scope analysis.
With ``--calls-only``, only returns actual call sites.

.. code-block:: text

   emend refs SELECTOR [OPTIONS]

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--exclude-definition`` | Exclude the definition site itself         |
+---------------------+-----------------------------------------------+
| ``--exclude-imports`` | Exclude import statements                   |
+---------------------+-----------------------------------------------+
| ``--writes-only``   | Only show write (assignment) references       |
+---------------------+-----------------------------------------------+
| ``--reads-only``    | Only show read (load) references              |
+---------------------+-----------------------------------------------+
| ``--calls-only``    | Only show call sites (not mere references)    |
+---------------------+-----------------------------------------------+
| ``--project``, ``-p`` | Project root directory (used with ``--calls-only``) |
+---------------------+-----------------------------------------------+
| ``--json``          | Output as JSON                                |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend refs src/emend/transform.py::get_component
   emend refs api.py::MyClass --exclude-imports --json

   # Filter by read/write context
   emend refs file.py::config --writes-only
   emend refs file.py::config --reads-only

   # Only call sites
   emend refs src/module.py::process --calls-only
   emend refs src/module.py::process --calls-only --json

---

rename
------

Rename a symbol across the entire project.

.. code-block:: text

   emend rename SELECTOR --to NEW_NAME [OPTIONS]

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--to TEXT``       | New name (required)                           |
+---------------------+-----------------------------------------------+
| ``--apply``         | Write changes to disk                         |
+---------------------+-----------------------------------------------+
| ``--docs``          | Also rename in docstrings                     |
+---------------------+-----------------------------------------------+
| ``--no-hierarchy``  | Don't rename in class hierarchy               |
+---------------------+-----------------------------------------------+
| ``--unsure``        | Rename uncertain occurrences                  |
+---------------------+-----------------------------------------------+
| ``--project``, ``-p`` | Project root directory                      |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend rename api.py::get_user --to fetch_user
   emend rename api.py::OldClass --to NewClass --apply
   emend rename utils.py::helper --to _helper --docs --apply

   # Module rename mode (no :: in selector):
   emend rename old_utils.py --to new_utils --apply

---

move
----

Move a symbol to another file, updating all imports.

.. code-block:: text

   emend move SELECTOR DESTINATION [OPTIONS]

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--dedent``        | Remove indentation for nested symbols         |
+---------------------+-----------------------------------------------+
| ``--no-update-imports`` | Skip import updates                        |
+---------------------+-----------------------------------------------+
| ``--apply``         | Write changes to disk                         |
+---------------------+-----------------------------------------------+
| ``--project``, ``-p`` | Project root directory                      |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend move utils.py::helper other.py
   emend move utils.py::MyClass models.py --apply

   # Module move mode (no :: in selector):
   emend move utils.py pkg --project . --apply

---

types
-----

Show inferred types for symbols in a file. Uses a type inference engine (Pyrefly, Pyright, or ty) to analyze source files and display inferred types for all symbols and expressions.

The engine is auto-detected from project configuration files (pyrightconfig.json, ty.toml, pyrefly.toml, or pyproject.toml sections) and installed tools. Use --engine to override.

.. code-block:: text

   emend types PATH [OPTIONS]

**Arguments:**

- PATH -- File, glob, or directory to analyze

**Options:**

+---------------------------+-----------------------------------------------------+
| Option                    | Description                                         |
+===========================+=====================================================+
| ``--name``, ``-n`` TEXT   | Filter by symbol name                               |
+---------------------------+-----------------------------------------------------+
| ``--kind``, ``-k`` TEXT   | Filter by binding kind: ``definition``,             |
|                           | ``reference``, ``import``, ``diagnostic``           |
+---------------------------+-----------------------------------------------------+
| ``--definitions-only``,   | Show only definitions                               |
| ``-d``                    |                                                     |
+---------------------------+-----------------------------------------------------+
| ``--json``                | Output as JSON                                      |
+---------------------------+-----------------------------------------------------+
| ``--engine`` TEXT         | Type inference engine: ``auto`` (default),          |
|                           | ``pyrefly``, ``pyright``, ``ty``                    |
+---------------------------+-----------------------------------------------------+

**Examples:**

.. code-block:: bash

   # Show all inferred types for a file
   emend types src/models/user.py

   # Filter by symbol name
   emend types src/models/user.py --name User

   # Only show definitions, output as JSON
   emend types src/models/ --definitions-only --json

   # Use a specific engine
   emend types app.py --engine pyright
   emend types app.py --engine ty

---

index
-----

Pre-build caches for faster cross-project operations.  Parses every Python
file in the project and builds a qualified-name index.  Run this once after
cloning a repo or starting work on a new codebase — subsequent ``refs``,
``rename``, ``callers``, and ``deadcode`` commands will be significantly
faster.

.. code-block:: text

   emend index [PATH] [OPTIONS]

**Arguments:**

- ``PATH`` -- Project root directory (default: ``.``)

**Options:**

+------------------------+--------------------------------------------------+
| Option                 | Description                                      |
+========================+==================================================+
| ``--jobs``, ``-j``     | Max parallel workers (default: CPU count)        |
+------------------------+--------------------------------------------------+

**What gets cached:**

+-------------------------+---------------------------------------------------+
| Cache                   | Effect                                            |
+=========================+===================================================+
| Tree-sitter parse cache | Avoids re-parsing unchanged files (~5x faster)    |
+-------------------------+---------------------------------------------------+
| Qualified-name index    | Skips MetadataWrapper for non-matching files       |
+-------------------------+---------------------------------------------------+

The cache is stored in ``.emend/cache/parse.db`` and keyed by file content
hash, so it self-invalidates when files change.  The directory is
auto-gitignored and dockerignored.  Git worktrees automatically share a
single cache with the main repo — running ``emend index`` in any worktree
populates the shared cache, and all worktrees benefit.

.. code-block:: bash

   # Index the current project
   emend index

   # Index a specific directory with limited parallelism
   emend index src/ --jobs 4

When using the MCP server (``emend mcp``), indexing happens automatically in
the background at startup.

mcp
---

Start an MCP (`Model Context Protocol <https://modelcontextprotocol.io/>`_) server
that exposes emend commands as tools for LLM-based clients.

Requires the ``mcp`` optional dependency: ``pip install emend[mcp]``.

.. code-block:: text

   emend mcp [OPTIONS]

**Options:**

+---------------------------+-----------------------------------------------+
| Option                    | Description                                   |
+===========================+===============================================+
| ``--transport``, ``-t``   | Transport protocol: ``stdio`` (default) or    |
|                           | ``sse``                                       |
+---------------------------+-----------------------------------------------+
| ``--port``, ``-p``        | Port for SSE transport (default: 8000)        |
+---------------------------+-----------------------------------------------+

**Exposed tools:**

+---------------+-------------------------------------------------------------------+
| Tool          | Description                                                       |
+===============+===================================================================+
| search        | Unified search: pattern matching, symbol lookup, summary          |
+---------------+-------------------------------------------------------------------+
| replace       | Pattern replacement (dry-run by default)                          |
+---------------+-------------------------------------------------------------------+
| edit          | Modify or remove symbol components                                |
+---------------+-------------------------------------------------------------------+
| add           | Add items to symbol components                                    |
+---------------+-------------------------------------------------------------------+
| refs          | Find references to a symbol (returns JSON)                        |
+---------------+-------------------------------------------------------------------+
| rename        | Rename symbols or modules across the project                      |
+---------------+-------------------------------------------------------------------+
| move          | Move symbols or modules with import updates                       |
+---------------+-------------------------------------------------------------------+
| graph         | Generate call graphs                                              |
+---------------+-------------------------------------------------------------------+
| deadcode      | Find unreferenced code                                            |
+---------------+-------------------------------------------------------------------+
| lint          | Pattern-based linting                                             |
+---------------+-------------------------------------------------------------------+
| copy_to       | Copy a symbol to another file                                     |
+---------------+-------------------------------------------------------------------+
| impact        | Compute transitively impacted symbols from a change               |
+---------------+-------------------------------------------------------------------+
| taint         | Taint analysis: detect unsafe source-to-sink flows                |
+---------------+-------------------------------------------------------------------+
| query_facts   | Query the fact graph (symbols, calls, references, taint flows)    |
+---------------+-------------------------------------------------------------------+
| datalog_query | Execute a CozoScript query against the fact graph                 |
+---------------+-------------------------------------------------------------------+
| check_policies| Run declarative policy checks                                     |
+---------------+-------------------------------------------------------------------+
| map_read      | Look up identifier/module mappings                                |
+---------------+-------------------------------------------------------------------+
| map_write     | Add or update identifier/module mappings                          |
+---------------+-------------------------------------------------------------------+

All write tools (edit, add, replace, rename, move) default to dry-run mode and
return a unified diff. Set ``apply=True`` to write changes to disk.

.. note::

   The MCP server requires Pydantic, which does not support Python 3.14t as of
   February 28, 2026. Use Python 3.10–3.13 (including 3.13t) for MCP server mode.

**Examples:**

.. code-block:: bash

   # Start MCP server on stdio (default, for use with Claude Code, etc.)
   emend mcp

   # Start on SSE transport with custom port
   emend mcp --transport sse --port 8080

**Claude Code configuration:**

The quickest way to add emend to `Claude Code <https://code.claude.com/>`_:

.. code-block:: bash

   # Add for the current project
   claude mcp add --transport stdio emend -- emend mcp

   # Share with your team (writes .mcp.json)
   claude mcp add --transport stdio --scope project emend -- emend mcp

   # If installed via uv tool install
   claude mcp add --transport stdio emend -- uvx emend mcp

Or add directly via JSON:

.. code-block:: bash

   claude mcp add-json emend '{"type":"stdio","command":"emend","args":["mcp"]}'

For a team-shared ``.mcp.json`` file (committed to version control):

.. code-block:: json

   {
     "mcpServers": {
       "emend": {
         "type": "stdio",
         "command": "emend",
         "args": ["mcp"]
       }
     }
   }

Verify the connection with ``claude mcp list`` or type ``/mcp`` inside
Claude Code. See :doc:`installation` for details on scopes and configuration
options.

---

impact
------

Compute the transitive set of impacted symbols from a change. Given a changed
symbol (selector) or git diff, computes which symbols, files, and tests are
transitively affected via reverse-caller closure.

.. code-block:: text

   emend impact [SELECTOR] [OPTIONS]

**Arguments:**

- ``SELECTOR`` -- Selector of the changed symbol (e.g. ``mymodule.py::func``)

**Options:**

+-----------------------------+-----------------------------------------------+
| Option                      | Description                                   |
+=============================+===============================================+
| ``--diff TEXT``             | Git diff spec (e.g. ``HEAD``, ``abc..def``)   |
+-----------------------------+-----------------------------------------------+
| ``--output``, ``-o``       | Output mode: ``symbols`` (default), ``tests``,|
|                             | ``graph``                                     |
+-----------------------------+-----------------------------------------------+
| ``--json``                  | Output as JSON                                |
+-----------------------------+-----------------------------------------------+
| ``--max-depth INT``         | Maximum BFS depth (default: 10)               |
+-----------------------------+-----------------------------------------------+
| ``--project``, ``-p``      | Project root directory                        |
+-----------------------------+-----------------------------------------------+

**Output modes:**

+----------+------------------------------------------------------------------+
| Mode     | Description                                                      |
+==========+==================================================================+
| symbols  | List changed and impacted symbol selectors                       |
+----------+------------------------------------------------------------------+
| tests    | List impacted test files/symbols                                 |
+----------+------------------------------------------------------------------+
| graph    | Show witness edges explaining why each symbol is impacted        |
+----------+------------------------------------------------------------------+

**Examples:**

.. code-block:: bash

   # From a selector
   emend impact mymodule.py::MyClass.method

   # From a git diff
   emend impact --diff HEAD
   emend impact --diff abc123..def456

   # Show impacted tests
   emend impact mymodule.py::func --output tests

   # Witness graph as JSON
   emend impact mymodule.py::func --output graph --json

---

taint
-----

Run intraprocedural taint analysis to detect unsafe data flows. Tracks value
flow from sources (e.g. user input) to sinks (e.g. SQL queries, ``eval``)
within individual functions, reporting violations when tainted data reaches a
sink without sanitization.

Configuration is read from the ``taint`` section of ``.emend/patterns.yaml``.

.. code-block:: text

   emend taint PATH [OPTIONS]

**Arguments:**

- ``PATH`` -- File or directory to analyze

**Options:**

+------------------------------+-----------------------------------------------+
| Option                       | Description                                   |
+==============================+===============================================+
| ``--config FILE``            | Path to config file                           |
|                              | (default: ``.emend/patterns.yaml``)           |
+------------------------------+-----------------------------------------------+
| ``--label TEXT``             | Only check a specific taint label             |
+------------------------------+-----------------------------------------------+
| ``--trace``                  | Show full propagation traces                  |
+------------------------------+-----------------------------------------------+
| ``--json``                   | Output as JSON                                |
+------------------------------+-----------------------------------------------+
| ``--project``, ``-p``        | Project root directory                        |
+------------------------------+-----------------------------------------------+
| ``--interprocedural``        | Track taint across function boundaries using  |
|                              | function summaries and fixed-point iteration  |
+------------------------------+-----------------------------------------------+
| ``--max-iterations INT``     | Max fixed-point iterations (default: 10)      |
+------------------------------+-----------------------------------------------+

**Config file format:**

.. code-block:: yaml

   taint:
     labels:
       - user_input
       - sensitive_data

     sources:
       - pattern: "request.args.get($X)"
         label: user_input
       - pattern: "input($PROMPT)"
         label: user_input

     sinks:
       - pattern: "cursor.execute($QUERY)"
         label: user_input
         message: "SQL injection: user input reaches cursor.execute()"
       - pattern: "eval($CODE)"
         label: user_input
         message: "Code injection: user input reaches eval()"

     sanitizers:
       - pattern: "sanitize($X)"
         label: user_input
       - pattern: "escape($X)"
         label: user_input

**Examples:**

.. code-block:: bash

   # Analyze a directory
   emend taint src/

   # Filter to a specific label
   emend taint app.py --label user_input

   # Show propagation traces
   emend taint src/ --trace

   # JSON output
   emend taint src/ --json

   # Cross-function taint tracking
   emend taint src/ --interprocedural
   emend taint src/ --interprocedural --max-iterations 20

---

delete
------

Delete a symbol, with optional cascading removal of code that becomes dead
after the deletion.  Dry-run by default; use ``--apply`` to write.

Without ``--cascade``, removes only the target symbol (like ``rm``).
With ``--cascade``, emend finds every symbol whose only callers were in the
deleted set, removes those too, and repeats until stable.

.. code-block:: text

   emend delete SELECTOR [OPTIONS]

**Arguments:**

- ``SELECTOR`` -- Symbol selector (e.g. ``models.py::LegacyUser``)

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--cascade``       | Transitively delete symbols that become       |
|                     | unreferenced after removal                    |
+---------------------+-----------------------------------------------+
| ``--apply``         | Write changes to disk (default: dry-run)      |
+---------------------+-----------------------------------------------+
| ``--json``          | Output the deletion plan as JSON              |
+---------------------+-----------------------------------------------+
| ``--project``, ``-p`` | Project root directory                      |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # Preview cascade deletion (dry-run)
   emend delete models.py::LegacyUser --cascade

   # Apply the deletion
   emend delete models.py::LegacyUser --cascade --apply

   # Simple single-symbol delete
   emend delete api.py::deprecated_function --apply

   # JSON output for tooling
   emend delete models.py::LegacyUser --cascade --json

---

cfg
---

Build and display per-function control flow graphs.  Constructs basic-block
CFGs for every function in the target file(s), with text, JSON, and Graphviz
DOT output.

.. code-block:: text

   emend cfg PATH [OPTIONS]

**Arguments:**

- ``PATH`` -- File or directory to analyze

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--function``, ``-f`` | Restrict to a specific function name       |
+---------------------+-----------------------------------------------+
| ``--format TEXT``   | Output format: ``text`` (default), ``json``,  |
|                     | ``dot``                                       |
+---------------------+-----------------------------------------------+
| ``--unreachable``   | Only show unreachable blocks                  |
+---------------------+-----------------------------------------------+

**Output formats:**

+--------+------------------------------------------------------------------+
| Format | Description                                                      |
+========+==================================================================+
| text   | Human-readable block/edge summary                                |
+--------+------------------------------------------------------------------+
| json   | Structured JSON with blocks, edges, dominators                   |
+--------+------------------------------------------------------------------+
| dot    | Graphviz DOT (pipe to ``dot -Tsvg`` for visualization)           |
+--------+------------------------------------------------------------------+

**Examples:**

.. code-block:: bash

   # All functions in a file
   emend cfg src/app.py

   # A specific function
   emend cfg src/app.py --function process

   # Graphviz DOT (render as SVG)
   emend cfg src/app.py --function process --format dot | dot -Tsvg > cfg.svg

   # Detect unreachable code
   emend cfg src/ --unreachable

   # JSON for programmatic use
   emend cfg src/ --format json

---

facts
-----

Query the relational fact graph for code invariants.  Builds a unified graph
from project source and supports structured queries over symbols, calls,
references, taint flows, type information, and imports.

.. code-block:: text

   emend facts [PROJECT] [OPTIONS]

**Arguments:**

- ``PROJECT`` -- Project root directory (default: ``.``)

**Options:**

+-------------------------------+-----------------------------------------------+
| Option                        | Description                                   |
+===============================+===============================================+
| ``--type``, ``-t``            | Fact type: ``symbols`` (default), ``calls``,  |
|                               | ``references``, ``taint_flows``, ``types``,   |
|                               | ``imports``                                   |
+-------------------------------+-----------------------------------------------+
| ``--name``, ``-n``            | Filter by name (symbols)                      |
+-------------------------------+-----------------------------------------------+
| ``--kind``, ``-k``            | Filter by kind (symbols)                      |
+-------------------------------+-----------------------------------------------+
| ``--file``, ``-f``            | Filter by file path                           |
+-------------------------------+-----------------------------------------------+
| ``--symbol``, ``-s``          | Qualified symbol name (calls/refs/types)      |
+-------------------------------+-----------------------------------------------+
| ``--label``                   | Taint label filter (taint_flows)              |
+-------------------------------+-----------------------------------------------+
| ``--transitive``              | Compute transitive closure (calls)            |
+-------------------------------+-----------------------------------------------+
| ``--max-depth INT``           | Max depth for transitive queries (default: 10)|
+-------------------------------+-----------------------------------------------+
| ``--json``                    | Output as JSON                                |
+-------------------------------+-----------------------------------------------+
| ``--limit INT``               | Max results (default: 100)                    |
+-------------------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # List all symbols
   emend facts .

   # Direct calls to a function
   emend facts . --type calls --symbol mymod.process

   # Transitive callers
   emend facts . --type calls --symbol mymod.process --transitive

   # All references to a class
   emend facts . --type references --symbol mymod.MyClass

   # Taint flows for a specific label
   emend facts . --type taint_flows --label user_input

   # Imports in a file
   emend facts . --type imports --file src/app.py

---

query
-----

Execute a CozoScript (Datalog) query directly against the project's fact
graph.  For structured queries over known fact types, prefer ``emend facts``.
Use ``emend query`` for ad-hoc or compositional analyses.

The fact graph is stored in CozoDB with the following relations:

.. code-block:: text

   symbol      {qn, file_path, name, kind, line, end_line, parent}
   call        {caller_qn, callee_qn, file_path, line, col}
   reference   {symbol_qn, file_path, line, col, ref_kind}
   taint_flow  {source_var, sink_var, label, file_path, func_qn, source_line, sink_line}
   type_binding {symbol_qn, file_path, line, binding_kind, type_str}
   import      {importing_file, imported_module, imported_name, line, alias}

.. code-block:: text

   emend query COZOSCRIPT [OPTIONS]

**Arguments:**

- ``COZOSCRIPT`` -- CozoScript query string

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--project``, ``-p`` | Project root directory (default: ``.``)     |
+---------------------+-----------------------------------------------+
| ``--json``          | Output as JSON                                |
+---------------------+-----------------------------------------------+
| ``--db``            | Path to persistent CozoDB database            |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # All function symbols
   emend query '?[name, file] := *symbol[qn, file, name, "function", l, e, p]'

   # Dead code (symbols with no references)
   emend query 'has_ref[qn] := *reference[qn, _, _, _, _]
     dead[name, file, line] := *symbol[qn, file, name, kind, line, _, _], not has_ref[qn]
     ?[name, file, line] := dead[name, file, line]'

   # Transitive callers of a function
   emend query 'reaches[a] := *call[a, "mymod.func", _, _, _]
     reaches[a] := *call[a, mid, _, _, _], reaches[mid]
     ?[a] := reaches[a]'

---

policy
------

Run declarative policy checks against source code.  Policies are named,
reusable compliance rules loaded from ``.emend/policies.yaml`` that combine
flow analysis, structural checks, type constraints, and dead code detection.

.. code-block:: text

   emend policy PATH [OPTIONS]

**Arguments:**

- ``PATH`` -- File or directory to check

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--config FILE``   | Path to policies.yaml                         |
|                     | (default: ``.emend/policies.yaml``)           |
+---------------------+-----------------------------------------------+
| ``--policy``, ``-p`` | Run only a specific policy by name           |
+---------------------+-----------------------------------------------+
| ``--json``          | Output as JSON                                |
+---------------------+-----------------------------------------------+

**Config file format (**.emend/policies.yaml**):**

.. code-block:: yaml

   policies:
     no-sql-injection:
       description: "Prevent SQL injection via user input"
       checks:
         - type: flow
           flows-from: "request.args.get($X)"
           flows-to: "cursor.execute($Q)"
           message: "User input reaches SQL cursor.execute()"

     no-dead-exports:
       description: "All public API symbols must be referenced"
       checks:
         - type: deadcode
           kind: function
           message: "Public function appears to be unreferenced"

**Policy check types:**

+-------------+------------------------------------------------------------+
| Type        | Description                                                |
+=============+============================================================+
| flow        | Source-to-sink taint flow check                            |
+-------------+------------------------------------------------------------+
| structural  | Pattern-based structural constraint                        |
+-------------+------------------------------------------------------------+
| type        | Type annotation constraint                                 |
+-------------+------------------------------------------------------------+
| deadcode    | Unreferenced symbol detection                              |
+-------------+------------------------------------------------------------+
| custom      | Custom check via callable                                  |
+-------------+------------------------------------------------------------+

**Examples:**

.. code-block:: bash

   # Run all policies
   emend policy src/

   # Custom config
   emend policy src/ --config .emend/policies.yaml

   # Run a specific policy
   emend policy src/ --policy no-sql-injection

   # JSON output
   emend policy src/ --json

---

saturate
--------

.. warning::

   This command is **experimental**. The rule format and output may change
   between versions.

Apply equality saturation rewrites to expressions in source files.  Uses an
e-graph representation to find optimal rewrites for expressions that match
rules defined in ``.emend/rewrites.yaml``.

.. code-block:: text

   emend saturate PATH [OPTIONS]

**Arguments:**

- ``PATH`` -- File or directory to rewrite

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--config FILE``   | Path to rewrites.yaml                         |
|                     | (default: ``.emend/rewrites.yaml``)           |
+---------------------+-----------------------------------------------+
| ``--apply``, ``-a`` | Apply rewrites (default: dry-run)             |
+---------------------+-----------------------------------------------+
| ``--max-iterations INT`` | Max saturation iterations (default: 30) |
+---------------------+-----------------------------------------------+
| ``--json``          | Output as JSON                                |
+---------------------+-----------------------------------------------+

**Config file format (**.emend/rewrites.yaml**):**

.. code-block:: yaml

   rules:
     - name: "double-negation"
       lhs: "not (not $X)"
       rhs: "$X"
     - name: "bool-simplify"
       lhs: "$X and True"
       rhs: "$X"

**Examples:**

.. code-block:: bash

   # Dry-run with default config
   emend saturate src/

   # Custom rules file
   emend saturate src/ --config .emend/rewrites.yaml

   # Apply rewrites
   emend saturate file.py --apply

   # JSON output
   emend saturate src/ --json
