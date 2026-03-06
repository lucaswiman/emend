Linting
=======

emend includes a pattern-based linter that uses the same pattern engine as ``search`` and ``replace``. Define rules in a YAML config file, and emend will find violations -- and optionally auto-fix them.

Overview
--------

The lint command reads rules from a YAML config file (default: ``.emend/patterns.yaml``) and checks source files for pattern matches. Each rule specifies:

- A **pattern** to search for (using emend's metavariable syntax)
- A **message** to display when the pattern is found
- An optional **scope constraint** to exclude certain contexts
- An optional **replacement** for auto-fixing

.. code-block:: bash

   # Check for violations
   emend lint src/

   # Auto-fix violations that have a replace rule
   emend lint src/ --fix

   # Run only a specific rule
   emend lint src/ --rule no-print


Configuration
-------------

Create a ``.emend/patterns.yaml`` file in your project root:

.. code-block:: yaml

   macros:
     print_call: "print($...ARGS)"

   rules:
     no-print:
       find: "{print_call}"
       not-inside: "def test_*"
       message: "Use logger instead of print"
       replace: "logger.info($...ARGS)"

     no-bare-assert:
       find: "assert $X"
       message: "Use pytest assertions instead of bare assert"

     no-type-ignore:
       find: "$X  # type: ignore"
       message: "Avoid blanket type: ignore comments"

The config file has two top-level sections:

macros
~~~~~~

Optional reusable pattern fragments. Reference them in rules using ``{macro_name}`` syntax:

.. code-block:: yaml

   macros:
     api_call: "requests.$METHOD($URL, $...KWARGS)"
     test_assert: "self.assert$METHOD($...ARGS)"

   rules:
     no-requests:
       find: "{api_call}"
       message: "Use httpx instead of requests"

rules
~~~~~

Each rule is a mapping with a unique key (the rule name) and the following fields:

+-----------------+----------+-----------------------------------------------+
| Field           | Required | Description                                   |
+=================+==========+===============================================+
| ``find``        | Yes      | Pattern to search for (same syntax as          |
|                 |          | ``emend search``)                             |
+-----------------+----------+-----------------------------------------------+
| ``message``     | Yes      | Human-readable violation message               |
+-----------------+----------+-----------------------------------------------+
| ``not-inside``  | No       | Scope constraint -- exclude matches inside     |
|                 |          | this context (e.g., ``def``, ``class``,       |
|                 |          | ``def test_*``)                               |
+-----------------+----------+-----------------------------------------------+
| ``replace``     | No       | Replacement pattern for auto-fix (same syntax  |
|                 |          | as ``emend replace``)                         |
+-----------------+----------+-----------------------------------------------+

Patterns support the full emend pattern syntax including metavariables (``$X``, ``$...ARGS``), type constraints (``$X:str``), and all expression/statement forms. See :doc:`patterns` for the complete reference.


Scope constraints (not-inside)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``not-inside`` field filters out matches that occur within certain code structures:

.. code-block:: yaml

   rules:
     no-print-in-production:
       find: "print($...ARGS)"
       not-inside: "def test_*"          # Skip matches inside test functions
       message: "Use logger instead of print"

     no-global-assigns:
       find: "$X = $Y"
       not-inside: "def"                  # Skip matches inside any function
       message: "Avoid module-level assignments"

Supported values:

- ``def`` -- any function definition
- ``class`` -- any class definition
- ``def test_*`` -- functions matching a name pattern
- ``async def`` -- async function definitions
- ``try:`` -- try/except blocks


Output format
-------------

Violations are printed one per line:

.. code-block:: text

   src/api.py:42:0: [no-print] Use logger instead of print
   src/utils.py:15:0: [no-bare-assert] Use pytest assertions instead of bare assert

The format is ``FILE:LINE:COL: [RULE_NAME] MESSAGE``, compatible with standard editor/IDE integrations.


Exit codes
----------

+------+----------------------------------------+
| Code | Meaning                                |
+======+========================================+
| 0    | No violations found                    |
+------+----------------------------------------+
| 1    | Violations found                       |
+------+----------------------------------------+
| 2    | Config file not found                  |
+------+----------------------------------------+


Auto-fixing
-----------

Rules that include a ``replace`` field can be auto-fixed:

.. code-block:: yaml

   rules:
     no-print:
       find: "print($...ARGS)"
       message: "Use logger instead of print"
       replace: "logger.info($...ARGS)"

.. code-block:: bash

   # Preview violations first
   emend lint src/

   # Apply fixes
   emend lint src/ --fix

When ``--fix`` is used, emend applies the replacement pattern and reports how many replacements were made per file. Rules without a ``replace`` field are skipped during fix mode.


Inline suppression (``# noqa``)
-------------------------------

You can suppress lint violations on individual statements with ``# noqa`` comments. emend uses the ``emend:`` prefix to avoid collisions with other linters (flake8, ruff, etc.).

Suppress all emend rules on a line:

.. code-block:: python

   print("debug info")  # noqa

Suppress a specific rule:

.. code-block:: python

   print("debug info")  # noqa: emend:no-print

Suppress multiple rules:

.. code-block:: python

   print("debug info")  # noqa: emend:no-print, emend:no-debug

Mix with other linters (only ``emend:``-prefixed entries affect emend):

.. code-block:: python

   x = some_long_expression  # noqa: E501, emend:no-print

A bare ``# noqa: E501`` with no ``emend:`` entries has no effect on emend.

Suppression is case-insensitive -- ``# NOQA`` and ``# Noqa`` work too.

Multi-line statements
~~~~~~~~~~~~~~~~~~~~~

A ``# noqa`` comment on the first line of a multi-line simple statement suppresses matches on all lines of that statement:

.. code-block:: python

   result = (  # noqa: emend:no-print
       print("hello")
   )

For compound statements (``if``, ``for``, ``def``, ``class``), a ``# noqa`` on the header line covers only that line, not the entire body.

How it works
~~~~~~~~~~~~

emend uses Python's ``tokenize`` module to find ``# noqa`` comments. This correctly ignores ``#`` characters inside string literals:

.. code-block:: python

   x = "# noqa"  # this does NOT suppress anything
   print("hello")  # this violation is still reported

Interaction with ``--fix``
~~~~~~~~~~~~~~~~~~~~~~~~~~

When ``--fix`` is used, lines suppressed by ``# noqa`` are left unchanged while other violations are still fixed:

.. code-block:: python

   # Before --fix
   print("keep this")  # noqa
   print("fix this")

   # After --fix
   print("keep this")  # noqa
   logger.info("fix this")

If all matches in a file are suppressed, the file is not modified at all.


CLI reference
-------------

.. code-block:: text

   emend lint PATH [OPTIONS]

**Arguments:**

- ``PATH`` -- File or directory to lint

**Options:**

+---------------------+-----------------------------------------------+
| Option              | Description                                   |
+=====================+===============================================+
| ``--config FILE``   | Path to config file                           |
|                     | (default: ``.emend/patterns.yaml``)           |
+---------------------+-----------------------------------------------+
| ``--fix``           | Auto-apply ``replace`` patterns               |
+---------------------+-----------------------------------------------+
| ``--rule NAME``     | Run only a specific rule by name              |
+---------------------+-----------------------------------------------+


Examples
--------

Catch common anti-patterns
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   # .emend/patterns.yaml
   macros:
     print_call: "print($...ARGS)"
     requests_call: "requests.$METHOD($...ARGS)"

   rules:
     no-print:
       find: "{print_call}"
       not-inside: "def test_*"
       message: "Use logger instead of print"
       replace: "logger.info($...ARGS)"

     no-requests:
       find: "{requests_call}"
       message: "Use httpx instead of requests"

     no-open-without-encoding:
       find: "open($PATH)"
       message: "Specify encoding when calling open()"
       replace: "open($PATH, encoding='utf-8')"

Enforce testing conventions
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   rules:
     no-unittest-assert:
       find: "self.assertEqual($A, $B)"
       message: "Use pytest assert instead of unittest assertEqual"
       replace: "assert $A == $B"

     no-unittest-assertTrue:
       find: "self.assertTrue($X)"
       message: "Use pytest assert instead of unittest assertTrue"
       replace: "assert $X"


Dead code detection
-------------------

emend can detect unreferenced (dead) code as part of linting. Add a ``deadcode`` section to your ``.emend/patterns.yaml`` config.

Quick enable
~~~~~~~~~~~~

The simplest form:

.. code-block:: yaml

   deadcode: true

This enables dead code detection with default settings.

Full configuration
~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   deadcode:
     enabled: true
     kind: function                           # "function", "class", or omit for all
     include-private: false                   # Include _private symbols
     exclude-references-from:                 # Ignore refs from these dirs
       - tests/
       - "**/generated/"                       # Glob patterns supported
     strings-count-as-references: true        # String literals count as refs
     message: "Symbol appears to be unused"   # Custom message prefix
     entry-point-decorators:                  # Extra decorators that mark entry points
       - my_framework.handler
       - celery_app.task
     entry-point-names:                       # Extra function/class names to skip
       - plugin_init
       - on_startup
     exclude-paths:                            # Directories to skip entirely
       - frontends/devtools/
       - "**/migrations/"                      # Glob patterns supported

+---------------------------------+----------+-----------------------------------------------+
| Field                           | Default  | Description                                   |
+=================================+==========+===============================================+
| ``enabled``                     | ``true`` | Enable/disable dead code detection            |
+---------------------------------+----------+-----------------------------------------------+
| ``kind``                        | (all)    | Filter: ``function`` or ``class``             |
+---------------------------------+----------+-----------------------------------------------+
| ``include-private``             | ``false``| Include ``_private`` symbols                  |
+---------------------------------+----------+-----------------------------------------------+
| ``exclude-references-from``     | (none)   | Directories to ignore when scanning for refs. |
|                                 |          | Supports glob patterns (``*``, ``**``, ``?``).|
+---------------------------------+----------+-----------------------------------------------+
| ``strings-count-as-references`` | ``true`` | Treat string literals containing the symbol   |
|                                 |          | name as references                            |
+---------------------------------+----------+-----------------------------------------------+
| ``message``                     | "Symbol  | Custom message prefix for violations          |
|                                 | appears  |                                               |
|                                 | to be    |                                               |
|                                 | unused"  |                                               |
+---------------------------------+----------+-----------------------------------------------+
| ``entry-point-decorators``      | (none)   | Additional decorator names (or basenames)     |
|                                 |          | that mark a symbol as an entry point.         |
|                                 |          | Symbols with these decorators are never       |
|                                 |          | flagged. Both full names (``pkg.deco``) and   |
|                                 |          | basenames (``deco``) are matched.             |
+---------------------------------+----------+-----------------------------------------------+
| ``entry-point-names``           | (none)   | Additional function/class names to treat as   |
|                                 |          | entry points. Symbols with these names are    |
|                                 |          | never flagged as dead code.                   |
+---------------------------------+----------+-----------------------------------------------+
| ``exclude-paths``               | (none)   | Directories to exclude entirely from dead     |
|                                 |          | code analysis. Symbols defined in these       |
|                                 |          | paths are never reported. Supports glob       |
|                                 |          | patterns (``*``, ``**``, ``?``).              |
+---------------------------------+----------+-----------------------------------------------+

How it works
~~~~~~~~~~~~

Dead code detection uses tree-sitter-based scope analysis for scope-aware analysis. It:

1. Collects all top-level function and class definitions across the project
2. Visits every file once (O(files) not O(symbols * files)) to find references
3. Reports symbols with zero references outside their own definition

It automatically detects ``src/`` layout projects (via ``pyproject.toml``) and computes correct qualified names.

Automatic exclusions are the same as the ``emend deadcode`` CLI command: dunders, test functions, decorated entry points, ``__all__`` members, and private symbols.  Use ``entry-point-decorators`` and ``entry-point-names`` to extend the built-in heuristics with project-specific exclusions.

Interaction with rules
~~~~~~~~~~~~~~~~~~~~~~

Dead code violations appear alongside pattern-rule violations with the rule name ``deadcode``:

.. code-block:: text

   src/utils.py:42:0: [deadcode] Symbol appears to be unused: old_helper

You can run only dead code detection with ``--rule deadcode``:

.. code-block:: bash

   emend lint src/ --rule deadcode

Inline suppression works the same way as for pattern rules:

.. code-block:: python

   def my_entry_point():  # noqa: emend:deadcode
       ...

Standalone command
~~~~~~~~~~~~~~~~~~

Dead code detection is also available as a standalone command with additional options (``--json``, ``--no-last-reference``, ``--entry-point-decorator``, ``--entry-point-name``, ``--exclude-path``):

.. code-block:: bash

   emend deadcode src/
   emend deadcode src/ --exclude-references-from tests/ --json
   emend deadcode . --entry-point-decorator my_framework.handler
   emend deadcode . --entry-point-name plugin_init
   emend deadcode . --exclude-path frontends/devtools/

See :doc:`commands` for the full ``deadcode`` command reference.


pre-commit integration
----------------------

emend can be used as a `pre-commit <https://pre-commit.com/>`_ hook to automatically lint files on each commit.

Add the following to your ``.pre-commit-config.yaml``:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.2.0  # replace with desired version tag
       hooks:
         - id: emend-lint

This runs ``emend lint`` on all staged Python files using your project's ``.emend/patterns.yaml`` config.

To use a custom config path:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.2.0
       hooks:
         - id: emend-lint
           args: ["--config", "custom/patterns.yaml"]

To also auto-fix violations:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.2.0
       hooks:
         - id: emend-lint
           args: ["--fix"]
