Quick Start
===========

Indexing (recommended)
-----------------------

After installing emend, run ``emend index`` once in your project root to
pre-build caches:

.. code-block:: bash

   emend index                  # index current directory
   emend index src/ --jobs 8    # index with 8 parallel workers

This parses every Python file and builds a qualified-name index so that
cross-project operations (``refs``, ``rename``, ``callers``, ``deadcode``)
are significantly faster on subsequent runs.  The cache lives in
``.emend/cache/parse.db`` (automatically gitignored and dockerignored) and is
keyed by file content hash, so it self-invalidates when files change.  Git
worktrees automatically share a single cache with the main repo.  Re-run
after large merges or branch switches.

When using the MCP server (``emend mcp``), indexing happens automatically in
the background at startup.

Workflow
--------

1. **Preview changes**: Run any command without ``--apply`` (dry-run is the default) to see a diff of proposed changes.
2. **Review the diff output**.
3. **Apply changes**: Re-run with ``--apply``.
4. **Format code**: Run your formatter (black, ruff, isort) -- emend may not preserve exact formatting.
5. **Verify**: Run tests and type checks.

Example: Add a parameter
------------------------

.. code-block:: bash

   # Preview the change (dry-run)
   emend edit api.py::get_user[params] "user_id: int, force: bool = False"

   # Apply it
   emend edit api.py::get_user[params] "user_id: int, force: bool = False" --apply

Example: Find and replace a pattern
------------------------------------

.. code-block:: bash

   # Find all print() calls in src/
   emend search 'print($X)' src/

   # Replace them with logger.info()
   emend replace 'print($X)' 'logger.info($X)' src/ --apply

Example: Look up symbol information
------------------------------------

.. code-block:: bash

   # Show a function's source code
   emend search api.py::get_user

   # Get return type
   emend search api.py::get_user[returns]

   # List all functions in a file
   emend search api.py --kind function
