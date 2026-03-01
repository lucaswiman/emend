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

Verifying installation
----------------------

.. code-block:: bash

   emend --help

Dependencies
------------

emend's Python layer uses LibCST as its sole AST backend — all parsing, traversal,
and transformation (including ``search``, ``rename``, ``refs``, and scope analysis)
go through LibCST. The bundled ``emend_core`` Rust extension accelerates file
discovery and pattern matching via tree-sitter and rayon.

Runtime dependencies (all installed automatically):

- `LibCST <https://github.com/Instagram/LibCST>`_ -- Concrete syntax tree parsing and transformation
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
