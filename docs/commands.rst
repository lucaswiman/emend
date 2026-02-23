Commands Reference
===================

search
------

Unified search command that auto-detects the mode from the query. If the query contains metavariables (``$X``, ``$...Y``), it uses **pattern matching** mode. Otherwise, it uses **symbol lookup** mode.

.. code-block:: text

   emend search QUERY [PATH] [OPTIONS]

**Arguments:**

- ``QUERY`` -- A pattern with ``$X`` metavars (pattern mode) or a selector path like ``file.py::sym`` (lookup mode)
- ``PATH`` -- File, glob, or directory to search (required for pattern mode, optional for lookup mode)

**Options (shared):**

+-----------+-----------------------------------+
| Option    | Description                       |
+===========+===================================+
| ``--json``| Output as JSON                    |
+-----------+-----------------------------------+
| ``--count`` | Output only count of matches      |
+-----------+-----------------------------------+

**Options (lookup mode):**

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
| ``--metadata``      | Include location metadata              |
+---------------------+----------------------------------------+
| ``--paths-only``    | Output only selector paths             |
+---------------------+----------------------------------------+
| ``--dedent``        | Dedent source code in output           |
+---------------------+----------------------------------------+

**Examples:**

.. code-block:: bash

   # Pattern mode (has $):
   emend search 'print($X)' src/
   emend search 'assertEqual($A, $B)' tests/ --count

   # Lookup mode (has :: or file path):
   emend search file.py::func[params]
   emend search src/ --kind function --has-decorator pytest

---

lookup
------

Read symbol information -- never modifies files.

.. code-block:: text

   emend lookup SELECTOR [OPTIONS]

**Arguments:**

- ``SELECTOR`` -- A file path, glob, or full selector like ``file.py::Symbol[component]``

**Options:**

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
| ``--json``          | Output as JSON                         |
+---------------------+----------------------------------------+
| ``--metadata``      | Include location metadata              |
+---------------------+----------------------------------------+
| ``--paths-only``    | Output only selector paths             |
+---------------------+----------------------------------------+
| ``--count``         | Output only the count of matches       |
+---------------------+----------------------------------------+
| ``--dedent``        | Dedent source code in output           |
+---------------------+----------------------------------------+

**Examples:**

.. code-block:: bash

   # Show function source
   emend lookup api.py::get_user

   # Get return annotation
   emend lookup api.py::get_user[returns]

   # Get all parameters
   emend lookup api.py::get_user[params]

   # Get a specific parameter
   emend lookup api.py::get_user[params][user_id]

   # Get all decorators
   emend lookup api.py::MyClass.method[decorators]

   # List all functions in a file
   emend lookup api.py --kind function

   # List all async methods across a directory
   emend lookup src/ --kind async_method --paths-only

   # Find all functions with a specific decorator
   emend lookup src/ --has-decorator pytest.mark.parametrize

   # JSON output with metadata
   emend lookup api.py::get_user[params] --json --metadata

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

find
----

Search for code patterns using metavariables.

.. code-block:: text

   emend find PATTERN PATH [OPTIONS]

**Arguments:**

- ``PATTERN`` -- A Python expression/statement pattern. Use ``$NAME`` for metavariables.
- ``PATH`` -- A file, glob (``*.py``), or directory (searches recursively).

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--in SCOPE``      | Limit to within a named symbol                |
+---------------------+-----------------------------------------------+
| ``--inside STRUCTURE`` | Only match inside this structure type       |
+---------------------+-----------------------------------------------+
| ``--not-inside STRUCTURE`` | Only match outside this structure type |
+---------------------+-----------------------------------------------+
| ``--where PATTERN`` | Alias for ``--inside`` with pattern support   |
+---------------------+-----------------------------------------------+
| ``--scope-local``   | Only match locally-defined names              |
+---------------------+-----------------------------------------------+
| ``--imported-from MODULE`` | Only when imported from given module    |
+---------------------+-----------------------------------------------+
| ``--json``          | Output matches as JSON with captures          |
+---------------------+-----------------------------------------------+
| ``--count``         | Output only the count of matches              |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   # Find all print() calls
   emend find 'print($X)' src/

   # Find assertEqual calls with count
   emend find 'assertEqual($A, $B)' tests/ --count

   # Find print calls only inside functions
   emend find 'print($X)' src/ --inside def

   # Find print calls outside try blocks
   emend find 'print($X)' src/ --not-inside try

   # Use pattern-based inside constraint
   emend find 'print($X)' src/ --inside 'def test_*'

   # Use --where (alias for --inside with patterns)
   emend find 'await $X' src/ --where 'async def fetch_*'

   # Only match locally-defined names
   emend find 'config' src/ --scope-local

   # Limit search to a specific function
   emend find 'log($MSG)' app.py --in process_request

   # JSON output with captures
   emend find 'raise $EXC($MSG)' src/ --json

   # Find calls only when imported from a specific module
   emend find 'json.loads($X)' src/ --imported-from json

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

callers
-------

Find all call sites of a function across the project. Unlike ``find-references``, this only returns places where the function is actually called (not mere references or imports).

.. code-block:: text

   emend callers SELECTOR [OPTIONS]

**Options:**

+-----------+-----------------------------------------------+
| Option    | Description                                   |
+===========+===============================================+
| ``--project``, ``-p`` | Project root directory              |
+-----------+-----------------------------------------------+
| ``--json``| Output as JSON                                |
+-----------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend callers src/module.py::process
   emend callers src/module.py::process --json

---

callees
-------

Find all functions/methods called inside a function. Analyzes the body of the target function and lists all calls made.

.. code-block:: text

   emend callees SELECTOR [OPTIONS]

**Options:**

+-----------+-----------------------------------------------+
| Option    | Description                                   |
+===========+===============================================+
| ``--project``, ``-p`` | Project root directory              |
+-----------+-----------------------------------------------+
| ``--json``| Output as JSON                                |
+-----------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend callees src/module.py::main
   emend callees src/module.py::main --json

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

find-references
---------------

Find all references to a symbol across the project using LibCST's scope analysis.

.. code-block:: text

   emend find-references SELECTOR [OPTIONS]

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
| ``--json``          | Output as JSON                                |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend find-references src/emend/transform.py::get_component
   emend find-references api.py::MyClass --exclude-imports --json

   # Filter by read/write context
   emend find-references file.py::config --writes-only
   emend find-references file.py::config --reads-only

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

**Examples:**

.. code-block:: bash

   emend rename api.py::get_user --to fetch_user
   emend rename api.py::OldClass --to NewClass --apply
   emend rename utils.py::helper --to _helper --docs --apply

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

**Examples:**

.. code-block:: bash

   emend move utils.py::helper other.py
   emend move utils.py::MyClass models.py --apply

---

list-symbols
------------

List all symbols in a module.

.. code-block:: text

   emend list-symbols FILE [OPTIONS]

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--tree-depth N``  | Maximum nesting depth                         |
+---------------------+-----------------------------------------------+
| ``--flat``          | Flat output with full dotted paths            |
+---------------------+-----------------------------------------------+
| ``--selector``, ``-s`` | Filter to a symbol subtree                 |
+---------------------+-----------------------------------------------+
| ``--project``, ``-p`` | Project root directory                      |
+---------------------+-----------------------------------------------+

**Examples:**

.. code-block:: bash

   emend list-symbols api.py
   emend list-symbols api.py --flat
   emend list-symbols api.py --selector MyClass

---

move-module
-----------

Move a module file to another package, updating imports.

.. code-block:: text

   emend move-module SOURCE DESTINATION [OPTIONS]

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--project``, ``-p`` | Project root                                |
+---------------------+-----------------------------------------------+
| ``--apply``         | Write changes to disk                         |
+---------------------+-----------------------------------------------+

**Example:**

.. code-block:: bash

   emend move-module utils/helpers.py pkg --apply

---

rename-module
-------------

Rename a module file, updating imports across the project.

.. code-block:: text

   emend rename-module FILE NEW_NAME [OPTIONS]

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--project``, ``-p`` | Project root                                |
+---------------------+-----------------------------------------------+
| ``--apply``         | Write changes to disk                         |
+---------------------+-----------------------------------------------+

**Example:**

.. code-block:: bash

   emend rename-module utils/old_helpers.py new_helpers --apply
