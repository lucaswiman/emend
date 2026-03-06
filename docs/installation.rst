Installation
=============

emend wheels include the compiled ``emend_core`` Rust extension — there is no
separate binary package to install.

Using uv with free-threaded Python (recommended)
-------------------------------------------------

.. code-block:: bash

   uv tool install --python 3.13t emend

Python 3.13+ ships a **free-threaded** variant (``3.13t``, ``3.14t``) that removes
the Global Interpreter Lock (GIL). emend's Rust core is already GIL-free, so on a
free-threaded interpreter it can parallelize file I/O and pattern-matching with no
lock contention — giving substantially faster results on multi-file operations like
``search``, ``lint``, ``refs``, and ``rename`` across large codebases.

We recommend **3.13t** for the free-threaded interpreter. The ``3.14t`` variant also
works for core emend commands, but the optional MCP server (``emend mcp``) depends on
Pydantic which does not yet support Python 3.14t (as of February 28, 2026):

.. code-block:: bash

   uv tool install --python 3.13t emend   # free-threaded 3.13 (recommended)
   uv tool install --python 3.14t emend   # free-threaded 3.14 (no MCP server support)

Using uv (standard Python)
--------------------------

.. code-block:: bash

   uv tool install emend

Using pip
---------

.. code-block:: bash

   pip install emend

MCP server
----------

To use emend as an `MCP <https://modelcontextprotocol.io/>`_ server for LLM-based
clients, install the optional ``mcp`` extra:

.. code-block:: bash

   pip install emend[mcp]

Then start the server:

.. code-block:: bash

   emend mcp                              # stdio transport (default)
   emend mcp --transport sse --port 8080  # SSE transport

.. note::

   The MCP server requires Pydantic, which does not support Python 3.14t as of
   February 28, 2026. Use Python 3.10–3.13 (including 3.13t) for MCP server mode.

Adding emend to Claude Code
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The quickest way to connect emend to
`Claude Code <https://code.claude.com/>`_ is the ``claude mcp add`` CLI:

.. code-block:: bash

   # Add for the current project (default scope: local)
   claude mcp add --transport stdio emend -- emend mcp

   # Or share with your team via .mcp.json (project scope)
   claude mcp add --transport stdio --scope project emend -- emend mcp

If you installed with ``uv tool install``, make sure the ``emend`` binary is on
your PATH, or use ``uvx``:

.. code-block:: bash

   claude mcp add --transport stdio emend -- uvx emend mcp

You can also configure it by editing JSON directly:

.. code-block:: bash

   claude mcp add-json emend '{"type":"stdio","command":"emend","args":["mcp"]}'

For a **team-shared** setup, add a ``.mcp.json`` file to your project root
(and commit it to version control):

.. code-block:: json

   {
     "mcpServers": {
       "emend": {
         "type": "stdio",
         "command": "emend",
         "args": ["mcp"]
       }
     }
   }

Verify the server is connected:

.. code-block:: bash

   claude mcp list          # from the terminal

Or inside Claude Code, type ``/mcp`` to see all connected servers and their
status.

**Scopes.** Claude Code has three MCP configuration scopes:

- ``local`` (default) -- private to you, current project only (stored in
  ``~/.claude.json``)
- ``project`` -- shared with the team via ``.mcp.json`` in the project root
  (committed to version control)
- ``user`` -- available to you across all projects (stored in
  ``~/.claude.json``)

Use ``--scope`` with ``claude mcp add`` to choose. See the
`Claude Code MCP documentation <https://code.claude.com/docs/en/mcp>`_ for
full details on scopes, environment variables, and managed configurations.

Vim / Neovim plugin
-------------------

emend ships with a Vim/Neovim plugin for interactive code search.  It
communicates with ``emend editor-server`` via JSON-RPC over stdio pipes,
keeping the server warm for sub-5ms symbol lookups.

Install with `vim-plug <https://github.com/junegunn/vim-plug>`_:

.. code-block:: vim

   Plug 'lucaswiman/emend', { 'rtp': 'vim' }

Or for local development, point to your checkout:

.. code-block:: vim

   Plug '~/src/emend', { 'rtp': 'vim' }
   let g:emend_command = '~/src/emend/.venv/bin/emend'

After installing, use ``:Emend`` to open the search prompt.  See
``vim/README.md`` and ``:help emend`` for full documentation.

Verifying installation
----------------------

.. code-block:: bash

   emend --help

Dependencies
------------

emend uses the bundled ``emend_core`` Rust extension (built on tree-sitter) as its AST backend — all parsing, traversal, scope analysis, pattern matching, and transformation go through tree-sitter and Rust. The extension is GIL-free and optimized for parallel file scanning on free-threaded Python.

Runtime dependencies (all installed automatically):

- `Typer <https://typer.tiangolo.com/>`_ -- CLI framework
- `Lark <https://github.com/lark-parser/lark>`_ -- Grammar-based parsing for selectors and patterns
- `PyYAML <https://pyyaml.org/>`_ -- YAML parsing for batch operations and lint rules

The ``emend_core`` extension (tree-sitter, rayon, PyO3) is compiled into the wheel
and requires no separate installation.

Installing from source
----------------------

Requires a Rust toolchain (``rustup``) and `maturin <https://github.com/PyO3/maturin>`_.

.. code-block:: bash

   git clone https://github.com/lucaswiman/emend
   cd emend

   # Create a free-threaded venv, compile the Rust extension, install dev deps
   make venv

Or manually:

.. code-block:: bash

   uv venv .venv --python 3.13t
   uv pip install maturin
   .venv/bin/maturin develop -E dev
