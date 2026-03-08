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


Identifier and Module Mappings (``emend map``)
---------------------------------------------

emend provides a unified ``map`` command for managing both cross-service
identifier mappings and module-to-repo mappings.

Identifier mappings record relationships between symbols in different projects,
while module mappings provide coarse information about where code for a
specific module lives.

Identifier Mappings
~~~~~~~~~~~~~~~~~~~

Record and query cross-service identifier relationships.

.. code-block:: bash

   # Add a mapping
   emend map add \
       backend "users.UserService.create" \
       gateway "POST /api/v1/users" \
       --rel calls --src-kind function --tgt-kind endpoint

   # Search mappings (FTS5 trigram)
   emend map search "UserService"

   # Find all mappings for an identifier
   emend map lookup "users.UserService.create"

   # Delete a mapping
   emend map rm 1

**Relationship types:** ``equivalent``, ``calls``, ``implements``, ``produces``, ``consumes``


Module Mappings
~~~~~~~~~~~~~~~

Map Python module prefixes to external repositories or local directories. When
a mapping points to a GitHub repo, emend clones it automatically using ``gh``
and manages worktrees for different branches.

.. code-block:: bash

   # Map a module prefix to a GitHub repo
   emend map add-module payments --repo org/payments-service

   # Map to a specific branch and subdirectory
   emend map add-module gateway --repo org/gateway --branch v2 --subpath src/gateway

   # Map to a local directory
   emend map add-module shared.utils --path /home/user/shared-utils

   # List all module mappings
   emend map list-modules

   # Delete a mapping
   emend map rm-module payments

How to specify mappings
^^^^^^^^^^^^^^^^^^^^^^^

The path (or repo + subpath) should point to the directory or file that
**corresponds to the module prefix**. The prefix itself is stripped from the
module name during resolution, and the remainder is appended to the mapped path.

**Example 1: Package in a subdirectory**
  If your repo ``shared-libs`` has a package ``utils`` at ``src/utils/``, map it as::

      emend map add-module utils --repo org/shared-libs --subpath src/utils

  Resolution: ``utils.networking.fetch`` → ``.../shared-libs/src/utils/networking.py::fetch``

**Example 2: Top-level package**
  If your repo ``payments-service`` has the ``payments`` package in the root, map it as::

      emend map add-module payments --repo org/payments-service/payments

  Or use the repo root and the prefix will be handled correctly if it matches the directory name::

      emend map add-module payments --repo org/payments-service --subpath payments


Unified Resolution
~~~~~~~~~~~~~~~~~~

The ``resolve`` and ``resolve-file`` commands provide a unified way to find
the local location of a symbol or module, taking all mappings into account.

.. code-block:: bash

   # Resolve to an explicit selector (file.py::Symbol)
   emend map resolve payments.models.Order
   # Output: /path/to/payments/models.py::Order

   # Resolve to file and line (for editor integration)
   emend map resolve-file payments.models.Order
   # Output:
   # File: /path/to/payments/models.py
   # Line: 42
   # Kind: class

**Dotted Selectors:** You can use dotted paths like ``path.to.module.Symbol``
anywhere a selector is expected if you use the ``--include-map`` flag with
``emend search`` (aliased as ``grep``)::

    emend search --include-map payments.models.Order


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
