Commands Reference
===================

search
------

Unified search command that auto-detects the mode from the query. If the query contains metavariables (``$X``, ``$...Y``), it uses **pattern matching** mode. If the query is a bare file/directory path with no filters, it shows a **symbol summary** (like the old ``list-symbols``). Otherwise, it uses **symbol lookup** mode.

Also available as: ``query``, ``show``, ``get``, ``lookup``, ``find`` for intuitive workflows.

.. code-block:: text

   emend search QUERY [PATH] [OPTIONS]

**Arguments:**

- ``QUERY`` -- A pattern with ``$X`` metavars (pattern mode), a selector like ``file.py::sym`` (lookup mode), or a bare file/dir path (summary/lookup mode)
- ``PATH`` -- File, glob, or directory to search (pattern mode only)

**Output format (--output / -o):**

Auto-detected when not specified:

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

**Options (shared):**

+-----------------------+-------------------------------------------+
| Option                | Description                               |
+=======================+===========================================+
| ``--output``, ``-o``  | Output format: code, location, selector,  |
|                       | summary, metadata                         |
+-----------------------+-------------------------------------------+
| ``--json``            | Output as JSON (orthogonal modifier)      |
+-----------------------+-------------------------------------------+
| ``--count``           | Output only count of matches              |
+-----------------------+-------------------------------------------+
| ``--dedent``          | Dedent source code in output              |
+-----------------------+-------------------------------------------+

**Options (lookup/summary mode):**

+---------------------+----------------------------------------+
| Option              | Description                            |
+=====================+========================================+
| ``--kind TEXT``     | Filter by symbol kind                  |
+---------------------+----------------------------------------+
| ``--name TEXT``     | Filter by name (glob or ``/regex/``)  |
+---------------------+----------------------------------------+
| ``--has-decorator TEXT`` | Filter to symbols with decorator      |
+---------------------+----------------------------------------+
| ``--returns TEXT``  | Filter by return type annotation       |
+---------------------+----------------------------------------+
| ``--in-class TEXT`` | Restrict to methods of this class      |
+---------------------+----------------------------------------+
| ``--depth TEXT``    | Filter by nesting depth                |
+---------------------+----------------------------------------+
| ``--has-param TEXT`` | Filter by parameter name              |
+---------------------+----------------------------------------+
| ``-i``              | Case-insensitive name matching         |
+---------------------+----------------------------------------+
| ``--smart-case``    | Match naming convention variants       |
+---------------------+----------------------------------------+
| ``--matching TEXT`` | Filter by body pattern match           |
+---------------------+----------------------------------------+
| ``--flat``          | Flat output (summary mode)             |
+---------------------+----------------------------------------+
| ``--tree-depth N``  | Nesting depth limit (summary mode)     |
+---------------------+----------------------------------------+

**Options (pattern mode):**

+---------------------+----------------------------------------+
| Option              | Description                            |
+=====================+========================================+
| ``--in SCOPE``      | Limit to within a named symbol         |
+---------------------+----------------------------------------+
| ``--inside STRUCTURE`` | Only match inside this structure type |
+---------------------+----------------------------------------+
| ``--not-inside STRUCTURE`` | Only match outside this structure |
+---------------------+----------------------------------------+
| ``--where PATTERN`` | Only match inside structures matching this pattern |
+---------------------+----------------------------------------+
| ``--imported-from MODULE`` | Only when imported from given module |
+---------------------+----------------------------------------+
| ``--scope-local``   | Only match locally-defined names       |
+---------------------+----------------------------------------+

**Examples:**

.. code-block:: bash

   # Pattern mode (has $):
   emend search 'print($X)' src/
   emend search 'assertEqual($A, $B)' tests/ --count
   emend search '$X = $Y' src/ --output selector

   # Lookup mode (has :: or file path):
   emend search file.py::func[params]
   emend search src/ --kind function --has-decorator pytest

   # Summary mode (bare file/dir, no filters):
   emend search api.py
   emend search api.py --flat
   emend search api.py::MyClass --output summary
   emend search api.py --output summary --tree-depth 2

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
| ``--apply``         | Write changes to disk                         |
+---------------------+-----------------------------------------------+
| ``--in SCOPE``      | Limit to within a named symbol                |
+---------------------+-----------------------------------------------+
| ``--inside STRUCTURE`` | Only replace inside this structure type    |
+---------------------+-----------------------------------------------+
| ``--not-inside STRUCTURE`` | Only replace outside this structure type |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # Replace print with logger.info (dry-run)
   emend replace 'print($X)' 'logger.info($X)' src/

   # Apply the replacement
   emend replace 'print($X)' 'logger.info($X)' src/ --apply

   # Replace only inside functions
   emend replace 'old_api($X)' 'new_api($X)' src/ --inside def --apply

   # Replace unittest assertions
   emend replace 'assertEqual($A, $B)' 'assert $A == $B' tests/ --apply

   # Replace only inside a specific method
   emend replace 'log($X)' 'logger.info($X)' app.py --in MyClass.process --apply

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
With ``--calls-only``, only returns actual call sites (replaces the former ``callers`` command).

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

   # Only call sites (replaces: emend callers src/module.py::process)
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

.. note::

   **Command consolidations:**

   - ``list-symbols`` → Use ``emend search FILE`` (auto-detects summary mode) or ``emend search FILE --output summary``
   - ``find-references``, ``callers``, ``callees`` → Use ``emend refs SELECTOR`` with ``--writes-only``, ``--reads-only``, ``--calls-only`` filters
   - ``rename-symbol``, ``rename-module`` → Use ``emend rename SELECTOR --to NEW_NAME`` (auto-detects mode)
   - ``move-module`` → Use ``emend move MODULE DESTINATION`` (auto-detects mode)
   - ``lookup``, ``query``, ``show``, ``find`` → Use ``emend search`` (also available as aliases)

