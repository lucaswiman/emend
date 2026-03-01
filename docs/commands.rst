Commands Reference
===================

search
------

Unified search command that auto-detects the mode from the query. If the query contains metavariables (``$X``, ``$...Y``), it uses **pattern matching** mode. If the query is a bare file/directory path with no filters, it shows a **symbol summary**. Otherwise, it uses **symbol lookup** mode.

Also available as: ``query``, ``show``, ``get``, ``lookup``, ``find`` for intuitive workflows.

.. code-block:: text

   emend search QUERY [PATH] [OPTIONS]

**Arguments:**

- ``QUERY`` -- A pattern with ``$X`` metavars (pattern mode), a selector like ``file.py::sym`` (lookup mode), or a bare file/dir path (summary/lookup mode)
- ``PATH`` -- File, glob, or directory to search (pattern mode only)

**Output format (--output / -o):**

Auto-detected when not specified. Use ``--output=FORMAT`` or ``--output=BASE::MODIFIER``:

+---------------------+----------------------------------------+
| Mode                | Default when...                        |
+=====================+========================================+
| ``code``            | Selector without component             |
+---------------------+----------------------------------------+
| ``location``        | Pattern mode (``$`` in query)          |
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

   # Pattern mode (has $):
   emend search 'print($X)' src/
   emend search 'assertEqual($A, $B)' tests/ --output count
   emend search '$X = $Y' src/ --output selector

   # Lookup mode (has :: or file path):
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

+------------------------------+-----------------------------------------------+
| Option                       | Description                                   |
+==============================+===============================================+
| ``--kind``, ``-k``          | Filter by symbol kind: ``function``, ``class``|
+------------------------------+-----------------------------------------------+
| ``--include-private``        | Include ``_private`` symbols (excluded by      |
|                              | default)                                      |
+------------------------------+-----------------------------------------------+
| ``--json``                   | Output as JSON                                |
+------------------------------+-----------------------------------------------+
| ``--exclude-references-from``| Directories to ignore when scanning for       |
|                              | references (e.g. ``tests/``). Can be repeated.|
+------------------------------+-----------------------------------------------+
| ``--no-strings``             | Don't count string literals as references     |
+------------------------------+-----------------------------------------------+
| ``--no-last-reference``      | Don't show git last-reference info            |
+------------------------------+-----------------------------------------------+

**Automatic exclusions:**

The following are automatically skipped (not reported as dead):

- Dunder methods (``__init__``, ``__str__``, etc.)
- Test functions/classes (``test_*``, ``Test*``)
- Decorated entry points (``@app.command``, ``@pytest.fixture``, ``@click.command``, ``@celery.task``, etc.)
- Symbols listed in ``__all__``
- Conventional entry points (``main``, ``setup``, ``teardown``)
- Private symbols (``_name``) unless ``--include-private`` is set
- Symbols with ``# noqa: emend:deadcode`` on the definition line

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

Find all references to a symbol across the project using LibCST's scope analysis.
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

+-----------+-------------------------------------------------------------------+
| Tool      | Description                                                       |
+===========+===================================================================+
| search    | Unified search: pattern matching, symbol lookup, summary          |
+-----------+-------------------------------------------------------------------+
| replace   | Pattern replacement (dry-run by default)                          |
+-----------+-------------------------------------------------------------------+
| edit      | Modify or remove symbol components                                |
+-----------+-------------------------------------------------------------------+
| add       | Add items to symbol components                                    |
+-----------+-------------------------------------------------------------------+
| refs      | Find references to a symbol (returns JSON)                        |
+-----------+-------------------------------------------------------------------+
| rename    | Rename symbols or modules across the project                      |
+-----------+-------------------------------------------------------------------+
| move      | Move symbols or modules with import updates                       |
+-----------+-------------------------------------------------------------------+
| graph     | Generate call graphs                                              |
+-----------+-------------------------------------------------------------------+
| deadcode  | Find unreferenced code                                            |
+-----------+-------------------------------------------------------------------+
| lint      | Pattern-based linting                                             |
+-----------+-------------------------------------------------------------------+
| copy_to   | Copy a symbol to another file                                     |
+-----------+-------------------------------------------------------------------+

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

Add to your Claude Code MCP settings:

.. code-block:: json

   {
     "mcpServers": {
       "emend": {
         "command": "emend",
         "args": ["mcp"]
       }
     }
   }

---
