Commands Reference
==================

emend's public CLI is organized around a small set of top-level commands:

- ``find`` -- unified search, lookup, and summary output
- ``edit`` -- mutating refactors and code transforms
- ``analyze`` -- read-only code analysis
- ``tool`` -- operational and debugging commands
- ``check`` -- unified project rules from ``.emend/rules.yaml``
- ``lint`` / ``policy`` -- focused rule runners kept for compatibility
- ``map`` -- identifier and module mappings
- ``mcp`` -- MCP server for LLM clients

Hidden compatibility aliases still exist.  For example, ``emend rm`` maps to
``emend edit rm``, and legacy read commands such as ``search``, ``grep``,
``show``, ``get``, and ``lookup`` route to ``emend find``.


find
----

Unified search: auto-detects pattern matching vs symbol lookup.

.. code-block:: text

   emend find [OPTIONS] QUERY [FILES]...

Canonical syntax is ``emend find [FLAGS] QUERY [FILES...]``.

Mode detection:

- Query contains ``$`` metavariables -> pattern mode
- Query parses as a selector -> selector lookup mode
- Bare file/directory path -> summary mode
- ``PATH::QUERY`` keeps file scope explicit while still auto-detecting the
  right-hand side as selector vs pattern

Useful options:

+-------------------------+------------------------------------------------+
| Option                  | Description                                    |
+=========================+================================================+
| ``--kind``              | Filter symbol kind in lookup/summary mode      |
+-------------------------+------------------------------------------------+
| ``--name``              | Filter symbol names by glob or ``/regex/``     |
+-------------------------+------------------------------------------------+
| ``--returns``           | Filter by return annotation / inferred return  |
+-------------------------+------------------------------------------------+
| ``--has-param``         | Filter symbols by parameter name               |
+-------------------------+------------------------------------------------+
| ``--output`` / ``-o``   | ``code``, ``location``, ``selector``,          |
|                         | ``summary``, ``metadata``, ``json``,           |
|                         | ``count``, ``summary::flat``, ``code::dedent`` |
+-------------------------+------------------------------------------------+
| ``--within``            | Structural containment for pattern mode        |
+-------------------------+------------------------------------------------+
| ``--not-within``        | Exclude pattern matches inside a structure     |
+-------------------------+------------------------------------------------+
| ``--matching``          | Lookup-mode body/decorator filter              |
+-------------------------+------------------------------------------------+
| ``--where``             | Legacy compatibility shorthand                 |
+-------------------------+------------------------------------------------+
| ``--imported-from``     | Import-aware filter for pattern mode           |
+-------------------------+------------------------------------------------+
| ``--scope-local``       | Exclude imported symbols in pattern mode       |
+-------------------------+------------------------------------------------+
| ``--include-map``       | Resolve dotted selectors through mappings      |
+-------------------------+------------------------------------------------+
| ``--dsl``               | Search embedded SQL/CSS/HTML regions           |
+-------------------------+------------------------------------------------+

Examples:

.. code-block:: bash

   emend find 'print($X)' src/
   emend find 'src/::assert False'
   emend find file.py::handler[params]
   emend find file.py --output summary::flat
   emend find src/ --kind function --matching '@app.command'
   emend find 'json.loads($X)' src/ --imported-from json


edit
----

Grouped code changes and refactors.

.. code-block:: text

   emend edit COMMAND [ARGS]...

Subcommands:

+----------------+---------------------------------------------------+
| Subcommand     | Description                                       |
+================+===================================================+
| ``set``        | Edit or replace existing symbol components        |
+----------------+---------------------------------------------------+
| ``rm``         | Remove a symbol or component                      |
+----------------+---------------------------------------------------+
| ``delete``     | Safe delete with optional cascading removal       |
+----------------+---------------------------------------------------+
| ``add``        | Insert new items into list components             |
+----------------+---------------------------------------------------+
| ``replace``    | Pattern-based replacement                         |
+----------------+---------------------------------------------------+
| ``cp``         | Copy a symbol to another file                     |
+----------------+---------------------------------------------------+
| ``rename``     | Rename a symbol or module                         |
+----------------+---------------------------------------------------+
| ``mv``         | Move a symbol or module with import updates       |
+----------------+---------------------------------------------------+
| ``batch``      | Apply YAML/JSON refactoring batches               |
+----------------+---------------------------------------------------+
| ``saturate``   | Experimental equality-saturation rewrites         |
+----------------+---------------------------------------------------+

Examples:

.. code-block:: bash

   emend edit set api.py::get_user[returns] "User | None" --apply
   emend edit add api.py::get_user[params] "timeout: int = 30" --apply
   emend edit rm api.py::deprecated_func --apply
   emend edit replace 'print($X)' 'logger.info($X)' src/ --apply
   emend edit rename models.py::User --to Account --apply
   emend edit mv utils.py::helper helpers/core.py --apply
   emend edit cp workflow.py::Builder._build.helper tasks.py --dedent --apply
   emend edit batch refactor.yaml --apply

Notes:

- All write operations default to dry-run; add ``--apply`` to write changes.
- Hidden aliases such as ``emend rm``, ``emend replace``, ``emend add``,
  ``emend rename``, and ``emend mv`` still work.


analyze
-------

Read-only code analysis commands.

.. code-block:: text

   emend analyze COMMAND [ARGS]...

Subcommands:

+----------------+---------------------------------------------------+
| Subcommand     | Description                                       |
+================+===================================================+
| ``refs``       | Find references to a symbol                       |
+----------------+---------------------------------------------------+
| ``graph``      | Generate call graphs                              |
+----------------+---------------------------------------------------+
| ``deadcode``   | Find potentially unused symbols and modules       |
+----------------+---------------------------------------------------+
| ``impact``     | Compute transitive impact from a change           |
+----------------+---------------------------------------------------+
| ``types``      | Show inferred types                               |
+----------------+---------------------------------------------------+
| ``trace``      | Trace unsafe data flows                           |
+----------------+---------------------------------------------------+
| ``facts``      | Query the relational fact graph                   |
+----------------+---------------------------------------------------+
| ``cfg``        | Show control-flow graphs                          |
+----------------+---------------------------------------------------+
| ``dsl``        | Debug embedded DSL detection                      |
+----------------+---------------------------------------------------+
| ``dupes``      | Find duplicate code via AST canonicalization      |
+----------------+---------------------------------------------------+

Examples:

.. code-block:: bash

   emend analyze refs src/api.py::process_request --calls-only
   emend analyze graph src/app.py --format dot
   emend analyze deadcode src/ --exclude-references-from tests/
   emend analyze deadcode src/ --unused-modules
   emend analyze impact models.py::User --json
   emend analyze trace src/ --preset flask --interprocedural
   emend analyze facts --type references --symbol package.module.func --json
   emend analyze cfg src/module.py --format json
   emend analyze dupes src/                                   # scan whole tree
   emend analyze dupes --check-file src/emend/foo.py --json   # post-write hook

``analyze dupes`` detects exact structural duplicates (alpha-renamed AST
subtrees) and sibling-sequence duplicates (shared statement runs across
functions). Useful flags:

- ``--mode exact|sequence|all`` -- which detector(s) to run.
- ``--file PATH`` -- restrict the scan to one file or directory (intra-scope only).
- ``--check-file PATH`` -- scan the whole project and report only clusters with at
  least one member in ``PATH``. Designed for post-write hooks; returns exit
  code 0 and empty JSON (``[]``) when nothing is found.
- ``--min-lines N`` / ``--min-score S`` -- tighten the signal/noise floor.
- ``--json`` -- machine-readable output with line ranges, scores, and members.

Post-write hook example (Claude Code ``settings.json``): run the duplicate
check after every successful ``Edit``/``Write`` and surface non-trivial
findings back to the model.

.. code-block:: json

   {
     "hooks": {
       "PostToolUse": [
         {
           "matcher": "Edit|Write|MultiEdit",
           "hooks": [
             {
               "type": "command",
               "command": "emend analyze dupes --check-file \"$CLAUDE_TOOL_FILE_PATH\" --min-score 100 --limit 5"
             }
           ]
         }
       ]
     }
   }

The equivalent MCP tool ``check_duplicates(file_path=..., project=...)``
can be invoked by the model directly without a shell hook.


tool
----

Operational and debugging commands.

.. code-block:: text

   emend tool COMMAND [ARGS]...

Subcommands:

- ``index`` -- pre-build caches for faster cross-project operations
- ``editor-search`` -- one-shot JSON search for editors
- ``editor-server`` -- long-running stdio JSON-RPC server for the Vim plugin

Examples:

.. code-block:: bash

   emend tool index
   emend tool index src/ --jobs 8
   emend tool editor-server


check, lint, and policy
-----------------------

``check`` is the canonical rules entry point.  It reads
``.emend/rules.yaml`` by default and can run match, flow, deadcode, and type rules.

.. code-block:: text

   emend check [PATHS]... [OPTIONS]

Useful options:

- ``--config`` -- path to ``rules.yaml``
- ``--rule`` -- run one named rule
- ``--kind`` -- restrict to ``match``, ``flow``, ``deadcode``, or ``type``
- ``--fix`` -- apply auto-fixes for match rules
- ``--json`` -- structured output

Examples:

.. code-block:: bash

   emend check src/
   emend check src/ --kind flow
   emend check src/ --rule no-print --fix

``lint`` and ``policy`` remain available for focused workflows and compatibility
with older configs.  They fall back to ``.emend/rules.yaml`` when possible.


map
---

Cross-repo identifier and module mappings.

.. code-block:: bash

   emend map add backend "UserService.create" gateway "POST /api/v1/users" --rel calls
   emend map add-module payments --repo org/payments-service
   emend map resolve payments.models.Order


mcp
---

Start the MCP server.

.. code-block:: bash

   emend mcp
   emend mcp --transport sse --port 8080
   emend mcp --profile core

Profiles:

- ``core`` -- search, transform/references/analyze/check, grammar reference
- ``refactor`` -- alias for ``core``
- ``expert`` -- ``refactor`` plus mappings
- ``full`` -- canonical tools plus legacy compatibility tools
