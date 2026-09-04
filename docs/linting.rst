Linting
=======

emend includes rule-based checking that uses the same pattern engine as ``find`` and ``edit replace``. Define rules in a YAML config file, and emend will find violations -- and optionally auto-fix them.

Overview
--------

The canonical entry point is ``emend check`` reading ``.emend/rules.yaml``. ``emend lint`` remains available for focused match/flow/deadcode workflows and compatibility with older configs. Each rule specifies:

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

Create a ``.emend/rules.yaml`` file in your project root:

.. code-block:: yaml

   macros:
     print_call: "print($...ARGS)"

   rules:
     no-print:
       match: "{print_call}"
       not-within: "def test_*"
       message: "Use logger instead of print"
       fix: "logger.info($...ARGS)"

     no-bare-assert:
       match: "assert $X"
       message: "Use pytest assertions instead of bare assert"

     no-type-ignore:
       match: "$X  # type: ignore"
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
       match: "{api_call}"
       message: "Use httpx instead of requests"

rules
~~~~~

Each rule is a mapping with a unique key (the rule name) and the following fields:

.. list-table::
   :header-rows: 1
   :widths: 20 12 68

   * - Field
     - Required
     - Description
   * - ``match``
     - Yes, except flow rules
     - Pattern to search for (same syntax as ``emend find``). Not required for
       flow rules that use ``flows-from``/``flows-to``.
   * - ``message``
     - Yes
     - Human-readable violation message.
   * - ``not-within``
     - No
     - Scope constraint: exclude matches inside this context (for example,
       ``def``, ``class``, or ``def test_*``).
   * - ``fix``
     - No
     - Replacement pattern for auto-fix (same syntax as
       ``emend edit replace``).
   * - ``flows-from``
     - No
     - Source pattern for flow rules (see below).
   * - ``flows-to``
     - No
     - Sink pattern for flow rules (see below).
   * - ``not-through``
     - No
     - Sanitizer pattern or list of alternatives for flow rules (see below).
   * - ``dsl``
     - No
     - Embedded-language type: ``sql``, ``css``, or ``html``.

Patterns support the full emend pattern syntax including metavariables (``$X``, ``$...ARGS``), type constraints (``$X:str``), and all expression/statement forms. See :doc:`patterns` for the complete reference.


Scope constraints (not-within)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``not-within`` field filters out matches that occur within certain code structures:

.. code-block:: yaml

   rules:
     no-print-in-production:
       match: "print($...ARGS)"
       not-within: "def test_*"          # Skip matches inside test functions
       message: "Use logger instead of print"

     no-global-assigns:
       match: "$X = $Y"
       not-within: "def"                  # Skip matches inside any function
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

Rules that include a ``fix`` field can be auto-fixed:

.. code-block:: yaml

   rules:
     no-print:
       match: "print($...ARGS)"
       message: "Use logger instead of print"
       fix: "logger.info($...ARGS)"

.. code-block:: bash

   # Preview violations first
   emend lint src/

   # Apply fixes
   emend lint src/ --fix

When ``--fix`` is used, emend applies the replacement pattern and reports how many replacements were made per file. Rules without a ``fix`` field are skipped during fix mode.


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
|                     | (default: ``.emend/rules.yaml``)              |
+---------------------+-----------------------------------------------+
| ``--fix``           | Auto-apply ``fix`` patterns                   |
+---------------------+-----------------------------------------------+
| ``--rule NAME``     | Run only a specific rule by name              |
+---------------------+-----------------------------------------------+


Examples
--------

Catch common anti-patterns
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   # .emend/rules.yaml
   macros:
     print_call: "print($...ARGS)"
     requests_call: "requests.$METHOD($...ARGS)"

   rules:
     no-print:
       match: "{print_call}"
       not-within: "def test_*"
       message: "Use logger instead of print"
       fix: "logger.info($...ARGS)"

     no-requests:
       match: "{requests_call}"
       message: "Use httpx instead of requests"

     no-open-without-encoding:
       match: "open($PATH)"
       message: "Specify encoding when calling open()"
       fix: "open($PATH, encoding='utf-8')"

Enforce testing conventions
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: yaml

   rules:
     no-unittest-assert:
       match: "self.assertEqual($A, $B)"
       message: "Use pytest assert instead of unittest assertEqual"
       fix: "assert $A == $B"

     no-unittest-assertTrue:
       match: "self.assertTrue($X)"
       message: "Use pytest assert instead of unittest assertTrue"
       fix: "assert $X"


Flow rules (data-flow linting)
------------------------------

In addition to pattern-matching rules, emend supports **flow rules** that detect
when a value matching a source pattern reaches a sink pattern without passing
through a sanitizer. This is useful for catching security issues like SQL
injection, XSS, or code injection.

Configuration
~~~~~~~~~~~~~

Flow rules use ``flows-from``, ``flows-to``, and optionally ``not-through``
instead of ``match``:

.. code-block:: yaml

   rules:
     sql-injection:
       flows-from: "request.args.get($X)"
       flows-to: "cursor.execute($QUERY)"
       not-through:
         - "sanitize($X)"
         - "escape($X)"
       message: "SQL injection: user input flows to cursor.execute()"

     no-eval-user-input:
       flows-from: "input($PROMPT)"
       flows-to: "eval($CODE)"
       message: "User input flows to eval()"

.. list-table::
   :header-rows: 1
   :widths: 20 12 68

   * - Field
     - Required
     - Description
   * - ``flows-from``
     - Yes
     - Source pattern (introduces taint).
   * - ``flows-to``
     - Yes
     - Sink pattern (should not receive tainted data).
   * - ``not-through``
     - No
     - Sanitizer pattern or list of alternatives.
   * - ``message``
     - Yes
     - Violation message.

How it works
~~~~~~~~~~~~

For each function in the file, emend:

1. Finds all matches of the source pattern within the function
2. Extracts variable names from the source match (assignment targets and captures)
3. Propagates taint through assignments: if a tainted variable appears on the RHS
   of an assignment, the LHS becomes tainted
4. Checks if any tainted variable appears in a sink pattern match
5. If any configured sanitizer (``not-through``) matches on the source-to-sink
   path, including at either endpoint, the violation is suppressed

Analysis is intraprocedural (within each function body) and field-insensitive.

Violations include a ``FlowWitness`` trace showing the source, propagation chain,
and sink.

Example output
~~~~~~~~~~~~~~

.. code-block:: text

   src/app.py:15:0: [sql-injection] SQL injection: user input flows to cursor.execute()

Flow rules integrate with ``# noqa`` suppression and ``--rule`` filtering just
like regular pattern rules.

For standalone trace analysis with more configuration options (labels, multiple
source/sink/sanitizer groups, full traces), see the ``trace`` command in
:doc:`commands`.


DSL-aware lint rules
--------------------

Rules can target embedded DSL regions (SQL, CSS, HTML) instead of host-language
code by setting the ``dsl`` field. The ``find`` pattern is matched against the
content of detected DSL regions using case-insensitive regex with ``$METAVAR``
support.

Configuration
~~~~~~~~~~~~~

.. code-block:: yaml

   rules:
     no-select-star:
       dsl: sql
       find: "SELECT * FROM $TABLE"
       message: "Avoid SELECT *; enumerate columns explicitly"

     no-delete-without-where:
       dsl: sql
       find: "DELETE FROM $TABLE"
       message: "DELETE without WHERE clause is dangerous"

How it works
~~~~~~~~~~~~

For each file, emend:

1. Detects embedded DSL regions (SQL keyword heuristics, magic comments)
2. Filters regions by the rule's ``dsl`` type
3. Matches the ``find`` pattern against each region's content
4. Reports violations with the host file location

``$METAVAR`` placeholders match identifiers (e.g., table/column names).
Whitespace in patterns matches any whitespace including newlines, so patterns
work across multi-line SQL strings.

DSL lint rules support ``# noqa`` suppression on the host-language line
containing the match, just like regular rules.

Example output
~~~~~~~~~~~~~~

.. code-block:: text

   src/queries.py:42:0: [no-select-star] Avoid SELECT *; enumerate columns explicitly


Dead code detection
-------------------

emend can detect unreferenced (dead) code as part of checking. Add a ``deadcode`` section to your ``.emend/rules.yaml`` config.

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
     include-private: true                    # Include private symbols/methods
     exclude-test-references: true            # Ignore test refs (default)
     unused-modules: true                     # Report unimported modules (default)
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

.. list-table::
   :header-rows: 1
   :widths: 28 12 60

   * - Field
     - Default
     - Description
   * - ``enabled``
     - ``true``
     - Enable or disable dead-code detection.
   * - ``kind``
     - (all)
     - Filter to ``function`` or ``class`` symbols.
   * - ``include-private``
     - ``true``
     - Include private symbols and methods.
   * - ``exclude-test-references``
     - ``true``
     - Ignore references originating in test files.
   * - ``unused-modules``
     - ``true``
     - Report Python modules with no incoming import.
   * - ``exclude-references-from``
     - (none)
     - Directories to ignore when scanning for references. Supports glob patterns (``*``, ``**``, ``?``).
   * - ``strings-count-as-references``
     - ``true``
     - Treat string literals containing the symbol name as references.
   * - ``message``
     - ``"Symbol appears to be unused"``
     - Custom message prefix for violations.
   * - ``entry-point-decorators``
     - (none)
     - Additional decorator names, or basenames, that mark a symbol as an entry point. Both full names such as ``pkg.deco`` and basenames such as ``deco`` are matched.
   * - ``entry-point-names``
     - (none)
     - Additional function or class names to treat as entry points.
   * - ``exclude-paths``
     - (none)
     - Paths to exclude entirely from dead-code analysis. Supports glob patterns (``*``, ``**``, ``?``).

How it works
~~~~~~~~~~~~

Dead code detection uses tree-sitter-based scope analysis for scope-aware analysis. It:

1. Collects all top-level function and class definitions across the project
2. Visits every file once (O(files) not O(symbols * files)) to find references
3. Reports symbols with zero references outside their own definition

It automatically detects ``src/`` layout projects (via ``pyproject.toml``) and computes correct qualified names.

Automatic exclusions are the same as the ``emend analyze deadcode`` CLI command: dunders, test functions, decorated or packaged entry points, ``__all__`` members, and executable Python modules. Private symbols/methods and unused modules are included by default, while references from tests are ignored. Use ``entry-point-decorators`` and ``entry-point-names`` to extend the built-in heuristics with project-specific exclusions.

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

   emend analyze deadcode src/
   emend analyze deadcode src/ --include-test-references --json
   emend analyze deadcode . --entry-point-decorator my_framework.handler
   emend analyze deadcode . --entry-point-name plugin_init
   emend analyze deadcode . --exclude-path frontends/devtools/
   emend analyze deadcode . --exclude-private --no-unused-modules

See :doc:`commands` for the full ``analyze deadcode`` command reference.


pre-commit integration
----------------------

emend can be used as a `pre-commit <https://pre-commit.com/>`_ hook to automatically lint files on each commit.

Add the following to your ``.pre-commit-config.yaml``:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.5.0  # replace with desired version tag
       hooks:
         - id: emend-lint

This runs ``emend lint`` on all staged Python files using your project's ``.emend/rules.yaml`` config.

To use a custom config path:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.5.0
       hooks:
         - id: emend-lint
           args: ["--config", "custom/patterns.yaml"]

To also auto-fix violations:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.5.0
       hooks:
         - id: emend-lint
           args: ["--fix"]
