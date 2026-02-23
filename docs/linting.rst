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


pre-commit integration
----------------------

emend can be used as a `pre-commit <https://pre-commit.com/>`_ hook to automatically lint files on each commit.

Add the following to your ``.pre-commit-config.yaml``:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.1.0  # replace with desired version tag
       hooks:
         - id: emend-lint

This runs ``emend lint`` on all staged Python files using your project's ``.emend/patterns.yaml`` config.

To use a custom config path:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.1.0
       hooks:
         - id: emend-lint
           args: ["--config", "custom/patterns.yaml"]

To also auto-fix violations:

.. code-block:: yaml

   repos:
     - repo: https://github.com/lucaswiman/emend
       rev: v0.1.0
       hooks:
         - id: emend-lint
           args: ["--fix"]
