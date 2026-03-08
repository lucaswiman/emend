Knowledge Base
==============

emend includes a built-in knowledge base for recording cross-service identifier
mappings, module-to-repo mappings, and free-form architectural notes. Everything
is stored in a per-project SQLite database at ``.emend/knowledge.db`` with
FTS5 trigram indexing for instant substring search.

Three subsystems:

1. **Knowledge notes** — a searchable scratchpad for architectural decisions,
   conventions, patterns, or any other information relevant to the codebase.
2. **Identifier mappings** — records that an identifier in one project maps to
   an identifier in another (e.g. ``users.UserService.create`` →
   ``POST /api/v1/users`` in the gateway repo).
3. **Module mappings** — coarse mappings from a Python module prefix to an
   external repo or local directory, with automatic ``gh`` cloning and
   git worktree management.


Environment Variables
---------------------

.. envvar:: EMEND_CACHE_DIR

   Override the global emend cache directory. Defaults to ``~/.cache/emend``.

   This controls where external repo checkouts are stored::

       $EMEND_CACHE_DIR/repo-checkouts/{repo-id}/contents       # bare clone
       $EMEND_CACHE_DIR/repo-checkouts/{repo-id}/checkouts/{ref} # worktrees

   Example::

       export EMEND_CACHE_DIR=/tmp/emend-cache
       emend modmap resolve payments.models


Knowledge Notes (``emend kb``)
------------------------------

Add, search, and manage free-form notes.

.. code-block:: bash

   # Add a note
   emend kb add "Auth flow" "Uses OAuth2 with PKCE. Tokens stored in Redis." \
       --category architecture --tags "auth,oauth"

   # Search notes (FTS5 trigram — any substring works)
   emend kb search "oauth"
   emend kb search "auth" --category architecture

   # Show a specific note
   emend kb show 1

   # Delete a note
   emend kb rm 1

**Fields:**

+-----------------+----------------------------------------------------------------------+
| Field           | Description                                                          |
+=================+======================================================================+
| ``title``       | Short title (required)                                               |
+-----------------+----------------------------------------------------------------------+
| ``content``     | Free-form body text (required)                                       |
+-----------------+----------------------------------------------------------------------+
| ``--category``  | ``note``, ``architecture``, ``convention``, ``decision``, ``pattern``|
+-----------------+----------------------------------------------------------------------+
| ``--tags``      | Comma-separated tags                                                 |
+-----------------+----------------------------------------------------------------------+
| ``--source``    | ``user``, ``llm``, ``heuristic``                                     |
+-----------------+----------------------------------------------------------------------+
| ``--project``   | Scope to a specific project/repo                                     |
+-----------------+----------------------------------------------------------------------+
| ``--file-path`` | Related file                                                         |
+-----------------+----------------------------------------------------------------------+
| ``--symbol``    | Related symbol                                                       |
+-----------------+----------------------------------------------------------------------+


Identifier Mappings (``emend mapping``)
---------------------------------------

Record and query cross-service identifier relationships.

.. code-block:: bash

   # Add a mapping
   emend mapping add \
       --source-project backend --source-id "users.UserService.create" --source-kind function \
       --target-project gateway --target-id "POST /api/v1/users" --target-kind endpoint \
       --relationship calls

   # Search mappings
   emend mapping search "UserService"

   # Find all mappings for an identifier
   emend mapping lookup "users.UserService.create"

   # Delete a mapping
   emend mapping rm 1

**Relationship types:** ``equivalent``, ``calls``, ``implements``, ``produces``, ``consumes``

**Provenance:** ``manual``, ``heuristic``, ``llm``


Module Mappings (``emend modmap``)
----------------------------------

Map Python module prefixes to external repositories or local directories. When
a mapping points to a GitHub repo, emend clones it automatically using ``gh``
and manages worktrees for different branches.

.. code-block:: bash

   # Map a module prefix to a GitHub repo
   emend modmap add payments --repo org/payments-service

   # Map to a specific branch and subdirectory
   emend modmap add gateway --repo org/gateway --branch v2 --subpath src/gateway

   # Map to a local directory
   emend modmap add shared.utils --path /home/user/shared-utils

   # List all module mappings
   emend modmap list

   # Resolve a module name to a local path
   emend modmap resolve payments.models.Order

   # Delete a mapping
   emend modmap rm 1

Repo Checkout Layout
~~~~~~~~~~~~~~~~~~~~

When a module mapping references a GitHub repo, emend uses ``gh repo clone``
to create a **bare clone**, then creates **git worktrees** for each
branch/tag/commit that is referenced:

.. code-block:: text

   ~/.cache/emend/repo-checkouts/
   └── org--payments-service/
       ├── contents/                  # bare clone
       └── checkouts/
           ├── main/                  # worktree for main branch
           └── v2/                    # worktree for v2 branch

This layout:

- Stores repos globally (not per-project), avoiding redundant clones
- Allows multiple branches of the same repo to coexist
- Uses the ``EMEND_CACHE_DIR`` env var to relocate the cache (see above)

**Prerequisites:** The `GitHub CLI <https://cli.github.com/>`_ (``gh``) must be
installed and authenticated for repo cloning to work.


MCP / Editor Integration
-------------------------

The knowledge base is also available through the MCP server and the Vim/Neovim
editor server (JSON-RPC). The MCP server exposes ``kb_search``,
``mapping_lookup``, and ``module_resolve`` tools. The editor server provides
``kb_search``, ``mapping_lookup``, ``mapping_goto``, and ``module_resolve`` RPC
methods.
