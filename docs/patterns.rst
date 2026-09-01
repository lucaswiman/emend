Pattern Syntax
==============

emend's ``find`` and ``edit replace`` commands use a pattern language based on Python syntax extended with **metavariables**.

Metavariables
-------------

A metavariable is a placeholder that matches any single expression or statement. They are written as ``$NAME`` where ``NAME`` is an uppercase identifier.

.. code-block:: bash

   # $X matches any single expression
   emend find 'print($X)' src/

   # Multiple metavariables
   emend find 'assertEqual($A, $B)' tests/

Metavariable rules
~~~~~~~~~~~~~~~~~~~

- ``$X`` -- matches any single expression node
- ``$_`` -- wildcard that matches any expression and is not captured
- ``$...ARGS`` -- matches zero or more arguments in a call (variadic)

Type constraints
----------------

Constrain what a metavariable can match by adding a type suffix:

.. code-block:: bash

   # Match only when argument is a string literal
   emend find 'print($X:str)' src/

   # Match only when argument is an integer literal
   emend find 'range($X:int)' src/

Negated type constraints
~~~~~~~~~~~~~~~~~~~~~~~~

Prefix the type with ``!`` to match anything *except* that type:

.. code-block:: bash

   # Match range() calls with non-integer arguments
   emend find 'range($X:!int)' src/

   # Match addition where left side is NOT a function call
   emend find '$A:!call + $B' src/

TypeOracle constraints
~~~~~~~~~~~~~~~~~~~~~~

For inferred (not just syntactic) type checking, use ``:type[X]`` and ``:returns[X]`` constraints. These require a type inference engine (see ``--type-engine``).

- ``:type[X]`` — the inferred type of the captured expression must match ``X``
- ``:returns[X]`` — the captured node must be a function whose inferred return type matches ``X``

.. code-block:: bash

   # Find calls where the argument has inferred type 'Connection'
   emend find 'use($X:type[Connection])' src/ --type-engine pyrefly

   # Find functions whose inferred return type is 'str | None'
   emend find '$F:returns[str | None]' src/ --type-engine auto

   # Find functions returning Optional[str] (parameterized form)
   emend find '$F:returns[Optional[str]]' src/ --type-engine pyright

TypeOracle constraints are cached per-file for efficiency. The engine is started once and results are cached. Use ``--type-engine auto`` (default) to let emend detect the engine from project config files; or specify ``pyrefly``, ``pyright``, or ``ty`` explicitly.

Basic patterns
--------------

Function calls
~~~~~~~~~~~~~~

.. code-block:: bash

   # Match any call to print() with one argument
   emend find 'print($X)' src/

   # Match any call to print() with two arguments
   emend find 'print($A, $B)' src/

   # Match any call to open() regardless of arguments
   emend find 'open($...)' src/

Assignments
~~~~~~~~~~~

.. code-block:: bash

   # Match any simple assignment
   emend find '$NAME = $VALUE' src/

   # Match augmented assignment
   emend find '$NAME += $VALUE' src/

Attribute access
~~~~~~~~~~~~~~~~

.. code-block:: bash

   emend find '$OBJ.method($X)' src/
   emend find 'self.$ATTR' src/

Return statements
~~~~~~~~~~~~~~~~~

.. code-block:: bash

   emend find 'return $X' src/
   emend find 'return None' src/

Raise statements
~~~~~~~~~~~~~~~~

.. code-block:: bash

   emend find 'raise $EXC($MSG)' src/
   emend find 'raise ValueError($X)' src/

Comparisons
~~~~~~~~~~~

.. code-block:: bash

   emend find '$A == $B' src/
   emend find '$X is None' src/
   emend find '$X is not None' src/

Compound statement patterns
---------------------------

Match compound statements by their header. The body is unconstrained unless explicitly specified.

If statements
~~~~~~~~~~~~~

.. code-block:: bash

   # Match any if statement with a specific condition pattern
   emend find 'if $COND:' src/

   # Match if-checks for None
   emend find 'if $X is None:' src/

For loops
~~~~~~~~~

.. code-block:: bash

   # Match any for loop
   emend find 'for $VAR in $ITER:' src/

   # Match enumerate loops
   emend find 'for $I, $V in enumerate($X):' src/

While loops
~~~~~~~~~~~

.. code-block:: bash

   emend find 'while $COND:' src/
   emend find 'while True:' src/

With statements
~~~~~~~~~~~~~~~

.. code-block:: bash

   # With context and alias
   emend find 'with $CTX as $VAR:' src/

   # With just context (no alias)
   emend find 'with $CTX:' src/

Try/except statements
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Match any try block
   emend find 'try:' src/

   # Match except clauses
   emend find 'except $EXC:' src/
   emend find 'except $EXC as $VAR:' src/

Async compound statements
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Match async for loops
   emend find 'async for $VAR in $ITER:' src/

   # Match async with statements
   emend find 'async with $CTX as $VAR:' src/
   emend find 'async with $CTX:' src/

Decorator patterns
------------------

Match decorated function definitions using multi-line patterns:

.. code-block:: bash

   # Find functions with any decorator
   emend find '@$DEC\ndef $FUNC($...ARGS):' src/

   # Find functions with a specific decorator
   emend find '@property\ndef $FUNC($...ARGS):' src/

   # Multiple decorators
   emend find '@$DEC1\n@$DEC2\ndef $FUNC($...ARGS):' src/

   # Async decorated functions
   emend find '@$DEC\nasync def $FUNC($...ARGS):' src/

Lambda patterns
---------------

Match lambda expressions:

.. code-block:: bash

   # Match single-argument lambdas
   emend find 'lambda $X: $EXPR' src/

   # Match multi-argument lambdas
   emend find 'lambda $X, $Y: $EXPR' src/

   # Match lambdas with star args
   emend find 'lambda *$ARGS: $EXPR' src/

   # Match lambdas that return a specific pattern
   emend find 'lambda $X: $X + 1' src/

Star expression patterns
------------------------

Match star and double-star unpacking expressions:

.. code-block:: bash

   # Match star unpacking
   emend find '*$X' src/

   # Match double-star dict unpacking
   emend find '**$X' src/

   # Match function calls with star/double-star args
   emend find 'func(*$ARGS, **$KWARGS)' src/

Dict patterns
-------------

Match dictionary literals with specific keys:

.. code-block:: bash

   # Exact dict match (all keys must be present, no extras)
   emend find "{'name': \$NAME, 'age': \$AGE}" src/

   # Partial dict match (extra keys allowed)
   emend find "{'type': 'user', ...}" src/

Chained comparison patterns
---------------------------

Match chained comparisons:

.. code-block:: bash

   # Two-operator chain
   emend find '$A < $B < $C' src/

   # Mixed operators
   emend find '$A <= $B < $C' src/

Walrus operator patterns
------------------------

Match walrus operator (``:=``) in various contexts:

.. code-block:: bash

   # Walrus in if conditions
   emend find 'if ($VAR := $EXPR):' src/

   # Walrus in comprehension filters
   emend find '[$X for $VAR in $ITER if ($TARGET := $EXPR)]' src/

Replacements
------------

In ``edit replace``, the replacement string can reference captured metavariables:

.. code-block:: bash

   # Replace print with logger.info
   emend edit replace 'print($X)' 'logger.info($X)' src/ --apply

   # Swap arguments
   emend edit replace 'assertEqual($A, $B)' 'assertEqual($B, $A)' tests/ --apply

   # Convert assert style
   emend edit replace 'assertEqual($A, $B)' 'assert $A == $B' tests/ --apply

   # Add a wrapper
   emend edit replace 'open($PATH)' 'open($PATH, encoding="utf-8")' src/ --apply

String content interpolation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Append ``.content`` to a metavariable in a replacement to extract the inner
value of a captured string literal (i.e. the string without surrounding quotes).
Use ``${NAME.content}`` syntax:

.. code-block:: bash

   # Union["Foo", Bar] → Foo | Bar  (strips quotes from the string literal)
   emend edit replace 'Union["$X", $Y]' '${X.content} | $Y' src/ --apply

If the captured node is not a string literal, the replacement is skipped for
that match. This is particularly useful for migrating ``Union["X", Y]``
(deferred-annotation style) to PEP 604 ``X | Y`` union syntax.

Scope constraints
-----------------

All scope constraints are passed via the ``--where`` flag. The syntax is auto-detected:

- A dotted name like ``MyClass.method`` restricts to that scope
- A structural keyword like ``def`` or ``class`` restricts to inside that block type
- A pattern like ``def test_*`` or ``async def fetch_*`` restricts to matching blocks
- A ``not`` prefix (e.g., ``not class``) excludes matches inside that structure
- A ``@decorator`` restricts to symbols with that decorator (lookup mode)

.. code-block:: bash

   # Only match inside the process_request function
   emend find 'print($X)' app.py --where process_request

   # Only match inside MyClass.method
   emend find 'self.$ATTR' app.py --where MyClass.method

   # Only match inside async functions named fetch_*
   emend find 'await $X' src/ --within 'async def fetch_*'

``--scope-local``
~~~~~~~~~~~~~~~~~

Only match names that are locally defined (excludes imports):

.. code-block:: bash

   # Find local variables named 'config', not imported ones
   emend find 'config' src/ --scope-local

``--where`` structural keywords
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Only match inside a particular kind of block. Accepts both keywords and patterns:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - String
     - Matches
   * - ``def``
     - Any function definition.
   * - ``class``
     - Any class definition.
   * - ``for``
     - Any ``for`` loop.
   * - ``while``
     - Any ``while`` loop.
   * - ``if``
     - Any ``if``/``elif``/``else`` block.
   * - ``try``
     - Any ``try``/``except`` block.
   * - ``with``
     - Any ``with`` statement.

.. code-block:: bash

   # Only inside functions (keyword)
   emend find 'print($X)' src/ --where def

   # Only inside try blocks (keyword)
   emend find 'raise $E' src/ --where try

   # Only inside functions matching a name pattern
   emend find 'print($X)' src/ --where 'def test_*'

   # Only inside async functions
   emend find 'await $X' src/ --where 'async def fetch_*'

Negation (``not``) syntax
~~~~~~~~~~~~~~~~~~~~~~~~~

Prefix with ``not`` to exclude matches inside a block type:

.. code-block:: bash

   # Not inside class bodies
   emend edit replace '$X = $Y' '$X: int = $Y' src/ --where 'not class' --apply

   # Not inside try blocks
   emend find 'open($PATH)' src/ --where 'not try'

   # Not inside test functions
   emend find 'print($X)' src/ --where 'not def test_*'

``--imported-from MODULE``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Only match when the root name in the pattern is imported from a specific module:

.. code-block:: bash

   # Match json.loads() only when json is actually imported from the json module
   emend find 'json.loads($X)' src/ --imported-from json

   # Match datetime usage only when from the datetime module
   emend find 'datetime.now()' src/ --imported-from datetime

Literal patterns (no metavariables)
------------------------------------

Patterns don't require ``$`` metavariables — you can search for literal code.
Use ``::`` to separate the file scope from the pattern when there are no
metavariables, so emend knows the right side is a pattern (not a symbol
selector):

.. code-block:: bash

   # Literal patterns via :: file scope
   emend find '**::assert False'
   emend find 'src/::import os'
   emend find 'file.py::print()'

   # With metavariables, :: is optional ($ triggers pattern mode):
   emend find 'print($X)' src/
   emend find '**::print($X)'    # equivalent

When ``::`` is present, the right side is auto-detected:

1. Contains ``$`` → pattern mode (metavar search)
2. Parses as valid selector → selector mode (symbol lookup)
3. Doesn't parse as selector → pattern mode (literal code search)

Multi-file search
-----------------

The ``PATH`` argument (or the left side of ``::``) accepts:

- A single file: ``src/api.py``
- A glob: ``src/**/*.py``
- A directory (searches all ``.py`` files recursively): ``src/``
- ``**`` (all Python files recursively, the default)

.. code-block:: bash

   # Search all test files
   emend find 'assertEqual($A, $B)' tests/

   # Search with glob
   emend find 'print($X)' 'src/**/*.py'

   # Equivalent using :: file scope
   emend find 'src/**/*.py::print($X)'

JSON output
-----------

Use ``--json`` to get structured output including captured metavariables:

.. code-block:: bash

   emend find 'raise $EXC($MSG)' src/ --json

Output:

.. code-block:: json

   {
     "count": 3,
     "matches": [
       {
         "file": "src/api.py",
         "line": 42,
         "code": "raise ValueError(\"bad input\")",
         "captures": {
           "EXC": "ValueError",
           "MSG": "\"bad input\""
         }
       }
     ]
   }

DSL patterns (``--dsl``)
------------------------

The ``--dsl`` flag enables pattern matching inside embedded DSL regions
(SQL strings, CSS, HTML) rather than host-language code. Metavariables work
inside DSL patterns:

.. code-block:: bash

   # Find all SELECT..FROM patterns in SQL strings
   emend find 'SELECT $COLS FROM $TABLE' src/ --dsl sql

   # Find all tables referenced in SQL
   emend find 'FROM $TABLE' src/ --dsl sql

   # List all SQL regions in a directory
   emend find src/ --dsl sql

Whitespace in DSL patterns is flexible -- a single space matches any whitespace
including newlines, so patterns work with multi-line SQL strings.

The ``$METAVAR`` captures are reported in JSON output:

.. code-block:: bash

   emend find 'SELECT $COLS FROM $TABLE' src/ --dsl sql --output json


Limitations
-----------

- Patterns match at the expression or statement level; they cannot span multiple statements
- ``$X:stmt`` type constraint is not yet fully implemented
- ``:type[X]`` / ``:returns[X]`` oracle constraints require a supported type checker to be installed and only support one level of bracket nesting in the type argument (e.g. ``:type[Optional[str]]`` works; ``:type[Optional[List[str]]]`` does not)
- DSL patterns use regex-based matching (not tree-sitter) and are case-insensitive
