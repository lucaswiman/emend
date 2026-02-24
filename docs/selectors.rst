Selector Syntax
===============

Selectors identify Python symbols and their components. emend supports three selector forms.

Symbol selectors
----------------

Address a symbol by its dotted path within a file:

.. code-block:: bash

   file.py::func                    # Module-level function
   file.py::Class.method            # Method in a class
   file.py::Class.method.nested     # Nested function inside a method

Extended selectors (components)
-------------------------------

Append a bracketed component name to drill into a symbol's structure:

.. code-block:: bash

   file.py::func[params]            # Function parameters
   file.py::func[params][ctx]       # Specific parameter by name
   file.py::func[params][0]         # Specific parameter by index
   file.py::func[returns]           # Return annotation
   file.py::func[decorators]        # Decorator list
   file.py::MyClass[bases]          # Base classes
   file.py::func[body]              # Function body

Component reference
~~~~~~~~~~~~~~~~~~~

+-----------------+--------------------+--------+-----------------------------+
| Component       | Target             | Type   | Valid on                    |
+=================+====================+========+=============================+
| ``[params]``    | Parameter list     | list   | functions, methods          |
+-----------------+--------------------+--------+-----------------------------+
| ``[returns]``   | Return annotation  | scalar | functions, methods          |
+-----------------+--------------------+--------+-----------------------------+
| ``[decorators]``| Decorator list     | list   | functions, methods, classes |
+-----------------+--------------------+--------+-----------------------------+
| ``[bases]``     | Base class list    | list   | classes                     |
+-----------------+--------------------+--------+-----------------------------+
| ``[body]``      | Body statements    | list   | functions, methods, classes |
+-----------------+--------------------+--------+-----------------------------+

Accessor types
~~~~~~~~~~~~~~

**By name** (any component):

.. code-block:: bash

   file.py::func[params][ctx]            # parameter named "ctx"
   file.py::func[decorators][property]   # decorator named "property"
   file.py::Class[bases][Protocol]       # base class named "Protocol"

**By index** (any component):

.. code-block:: bash

   file.py::func[params][0]             # first parameter
   file.py::func[decorators][-1]        # last decorator
   file.py::Class[bases][0]             # first base class

Pseudo-class selectors
----------------------

For parameters, you can specify the parameter kind:

.. code-block:: bash

   file.py::func[params]:KEYWORD_ONLY          # keyword-only parameters (after *)
   file.py::func[params]:POSITIONAL_ONLY       # positional-only parameters (before /)
   file.py::func[params]:POSITIONAL_OR_KEYWORD # regular parameters

These are used with the ``add`` command to insert parameters into the correct slot:

.. code-block:: bash

   emend add "api.py::handler[params]:KEYWORD_ONLY" "debug: bool" --apply

Line selectors
--------------

Address code by line number:

.. code-block:: bash

   file.py:42          # Single line
   file.py:42-100      # Line range

Wildcard selectors
------------------

Use glob patterns to match multiple symbols:

.. code-block:: bash

   file.py::*[params]              # All function parameters in file
   file.py::Test*[decorators]      # Decorators on Test* classes/functions
   file.py::*.*[returns]           # All method return types
   file.py::Class.*[body]          # All method bodies in Class

Wildcard patterns:

- ``*`` -- Match any symbol at one level
- ``Test*`` -- Match symbols starting with "Test"
- ``*.*`` -- Match any method in any class
- ``Class.*`` -- Match any method in a specific class

Selector grammar (Lark)
-----------------------

.. code-block:: lark

   start: selector

   selector: file_path DOUBLE_COLON symbol_path? component*

   file_path: PATH
   symbol_path: symbol_segment ("." symbol_segment)*
   symbol_segment: WILDCARD | IDENTIFIER
   component: "[" COMPONENT_NAME "]" accessor? pseudo_class?
   accessor: "[" (IDENTIFIER | INT) "]"
   pseudo_class: PSEUDO_CLASS

   COMPONENT_NAME: "params" | "returns" | "decorators" | "bases" | "body" | "imports"
   DOUBLE_COLON: "::"
   PATH: /[^:]+/
   WILDCARD: "*" | /[a-zA-Z_*][a-zA-Z0-9_*]*/
   IDENTIFIER: /[a-zA-Z_][a-zA-Z0-9_]*/
   INT: /-?\d+/
   PSEUDO_CLASS: /:KEYWORD_ONLY|:POSITIONAL_ONLY|:POSITIONAL_OR_KEYWORD/
