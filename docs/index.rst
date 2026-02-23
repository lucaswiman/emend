emend documentation
===================

A Python refactoring CLI tool built on `LibCST <https://github.com/Instagram/LibCST>`_, with two complementary systems:

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
   recipes

Indices and tables
==================

* :ref:`genindex`
* :ref:`search`
