Mappings
========

emend includes a built-in mapping store for cross-repo identifier mappings and
module-to-repo mappings.  The public CLI surface for this is ``emend map``.


Overview
--------

Two mapping kinds are supported:

1. **Identifier mappings** -- relationships between identifiers in different
   systems, such as ``users.UserService.create`` -> ``POST /api/v1/users``.
2. **Module mappings** -- a module prefix that resolves into another repo or a
   local directory so selectors can jump across repositories.

Mappings are stored in emend's project metadata and are also exposed through the
MCP server's ``mappings`` tool.


Identifier mappings
-------------------

Use identifier mappings to record cross-service relationships.

.. code-block:: bash

   emend map add \
       backend "users.UserService.create" \
       gateway "POST /api/v1/users" \
       --rel calls --src-kind function --tgt-kind endpoint

   emend map search "UserService"
   emend map lookup "users.UserService.create"
   emend map rm "users.UserService.create"

Relationship kinds include ``equivalent``, ``calls``, ``implements``,
``produces``, and ``consumes``.


Module mappings
---------------

Module mappings tell emend where another codebase lives.

.. code-block:: bash

   emend map add-module payments --repo org/payments-service
   emend map add-module gateway --repo org/gateway --branch v2 --subpath src/gateway
   emend map add-module shared.utils --path /home/user/shared-utils

   emend map list-modules
   emend map update-module payments --branch release
   emend map rm-module payments


Resolution
----------

``emend map resolve`` resolves a dotted symbol or module path into a local
selector, taking mappings into account.

.. code-block:: bash

   emend map resolve payments.models.Order
   # /path/to/payments/models.py::Order

You can also use mapped resolution directly from ``find``:

.. code-block:: bash

   emend find --include-map payments.models.Order


Choosing the mapped path
------------------------

The target path (or repo + subpath) should point to the directory or file that
corresponds to the mapped prefix itself.

Examples:

- If the ``payments`` package lives at ``repo-root/payments/``, map with
  ``--repo org/repo --subpath payments``.
- If the ``payments`` package lives at ``repo-root/src/payments/``, map with
  ``--repo org/repo --subpath src/payments``.
- If the repo root is itself the package, use ``--repo org/repo`` with no
  subpath.


Repo checkout layout
--------------------

When a module mapping points at GitHub, emend uses ``gh repo clone`` to create a
bare clone, then creates git worktrees for requested refs:

.. code-block:: text

   ~/.cache/emend/repo-checkouts/
   └── org--payments-service/
       ├── contents/
       └── checkouts/
           ├── main/
           └── v2/

Set ``EMEND_CACHE_DIR`` to relocate this cache root.
