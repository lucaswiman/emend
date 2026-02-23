Installation
=============

Using uv (recommended)
----------------------

.. code-block:: bash

   uv tool install emend

Using pip
---------

.. code-block:: bash

   pip install emend

Verifying installation
----------------------

.. code-block:: bash

   emend --help

Dependencies
------------

emend uses LibCST as its sole AST backend -- all parsing, traversal, and transformation (including ``search``, ``rename``, ``refs``, and scope analysis) go through LibCST.

Runtime dependencies:

- `LibCST <https://github.com/Instagram/LibCST>`_ -- Concrete syntax tree parsing and transformation
- `Typer <https://typer.tiangolo.com/>`_ -- CLI framework
- `Lark <https://github.com/lark-parser/lark>`_ -- Grammar-based parsing for selectors and patterns
- `PyYAML <https://pyyaml.org/>`_ -- YAML parsing for batch operations and lint rules

Installing from source
----------------------

To install emend for development, clone the repository and install with dev dependencies:

.. code-block:: bash

   git clone <repo-url>
   cd emend

   # Create virtual environment and install with dev dependencies
   make venv

Or manually:

.. code-block:: bash

   python3 -m venv .venv
   .venv/bin/pip install -e ".[dev]"
