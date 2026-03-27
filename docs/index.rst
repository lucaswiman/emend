emend documentation
===================

A multi-language refactoring CLI built on `tree-sitter <https://tree-sitter.github.io>`_. The name means "to make corrections to a text" -- which is what it does, but with AST-aware precision instead of find-and-replace. Two complementary systems:

- **Structured Edits** -- Precise changes to symbol metadata using selectors like ``file.py::func[params][0]``
- **Pattern Transforms** -- Code-pattern search and replace with capture variables like ``print($X)`` to ``logger.info($X)``

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   quickstart
   selectors
   commands
   patterns
   linting
   knowledge
   recipes
   languages
   grammar_and_cookbook
   technical_details

Indices and tables
==================

* :ref:`genindex`
* :ref:`search`
