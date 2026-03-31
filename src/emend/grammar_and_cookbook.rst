emend Grammar & Cookbook
========================

This reference covers the full syntax for selectors, patterns, and commands,
followed by cookbook recipes for common refactoring tasks.  Call this tool
whenever you need to construct an emend command and aren't sure of the exact
syntax.

.. contents:: Sections
   :local:

Selector syntax
---------------

Selectors address Python symbols and their components.  The general form is::

    [path]::[symbol.path][component][accessor]

If the path is omitted (``::func``), it defaults to ``**`` (all Python files).
A bare name that doesn't match a file (``func``) is also treated as
``**::func``.

Symbol selectors
~~~~~~~~~~~~~~~~

::

    file.py::func                  # module-level function
    file.py::Class                 # class
    file.py::Class.method          # method
    file.py::Class.method.nested   # nested function inside a method
    ::func                         # search all files for func
    func                           # same (bare name fallback)
    Class.method                   # search all files for Class.method

Extended selectors (components)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    file.py::func[params]          # all parameters
    file.py::func[params][ctx]     # parameter by name
    file.py::func[params][0]       # parameter by index
    file.py::func[returns]         # return annotation
    file.py::func[decorators]      # decorator list
    file.py::Class[bases]          # base classes
    file.py::func[body]            # function body
    file.py::[imports]             # module-level imports

Components: ``params``, ``returns``, ``decorators``, ``bases``, ``body``,
``imports``.

Accessors: ``[name]`` by name, ``[0]`` by index, ``[-1]`` last.

Pseudo-class selectors
~~~~~~~~~~~~~~~~~~~~~~

For the ``add`` command, specify the parameter kind slot::

    file.py::func[params]:KEYWORD_ONLY          # after *
    file.py::func[params]:POSITIONAL_ONLY       # before /
    file.py::func[params]:POSITIONAL_OR_KEYWORD  # regular

Wildcard selectors
~~~~~~~~~~~~~~~~~~

::

    file.py::*[params]             # all functions' params
    file.py::Test*[decorators]     # decorators on Test* symbols
    file.py::*.*[returns]          # all method return types
    file.py::Class.*[body]         # all method bodies in Class

File globs
~~~~~~~~~~

::

    'src/**/*.py::func'            # match across multiple files
    '**::func'                     # all Python files recursively

File scope with patterns
~~~~~~~~~~~~~~~~~~~~~~~~

The ``::`` separator also works with code patterns, not just selectors.
When the right side of ``::`` doesn't parse as a valid selector, it is
treated as a code pattern::

    '**::print($X)'               # search all files for pattern
    'src/::assert False'           # search src/ for literal pattern
    'file.py::print()'             # search one file for pattern

Detection order:

1. Right side contains ``$`` → pattern mode (metavar search)
2. Right side parses as valid selector → selector mode (symbol lookup)
3. Right side fails selector parse → pattern mode (literal code search)

Line selectors
~~~~~~~~~~~~~~

::

    file.py:42                     # single line
    file.py:42-100                 # line range

Formal selector grammar (Lark)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: ../src/emend/grammars/selector.lark


Pattern syntax
--------------

Patterns match code structures using metavariables (``$``-prefixed).

Metavariables
~~~~~~~~~~~~~

::

    $X                 # any single expression
    $_                 # wildcard (match but don't capture)
    $...ARGS           # zero or more arguments (variadic/ellipsis)
    $X:int             # type-constrained metavar
    $X:!int            # negated type constraint

Syntactic type constraints: ``int``, ``str``, ``float``, ``expr``, ``stmt``,
``identifier``, ``call``, ``attr``.

Oracle type constraints (require ``--type-engine``)::

    $X:type[Connection]       # inferred type matches Connection
    $F:returns[str | None]    # inferred return type matches

Expression patterns
~~~~~~~~~~~~~~~~~~~

::

    print($X)                     # call with one arg
    func($A, $B)                  # call with two args
    func($...ARGS)                # call with any number of args
    $A + $B                       # binary operation
    $X[$Y]                        # subscript
    $OBJ.method($X)               # method call
    self.$ATTR                    # attribute access
    $X is None                    # comparison
    $A == $B                      # equality
    $A < $B < $C                  # chained comparison
    lambda $X: $EXPR              # lambda
    *$X                           # star unpack
    **$X                          # double-star unpack
    {'key': $V, ...}              # partial dict match
    [$X, $Y]                      # list with two elements

Statement patterns
~~~~~~~~~~~~~~~~~~

::

    return $X                     # return
    raise $EXC($MSG)              # raise
    assert $A == $B               # assert
    $NAME = $VALUE                # assignment
    $NAME += $VALUE               # augmented assignment
    if $COND:                     # if statement
    for $VAR in $ITER:            # for loop
    while $COND:                  # while loop
    with $CTX as $VAR:            # with statement
    try:                          # try block
    except $EXC as $VAR:          # except clause
    async for $VAR in $ITER:      # async for
    async with $CTX as $VAR:      # async with

Decorator patterns::

    @$DEC\ndef $FUNC($...ARGS):   # decorated function
    @property\ndef $F($...A):     # specific decorator

Formal pattern grammar (Lark)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: ../src/emend/grammars/pattern.lark

Replacement syntax
~~~~~~~~~~~~~~~~~~

In ``edit replace``, reference captured metavariables::

    emend edit replace 'print($X)' 'logger.info($X)' src/ --apply

String content interpolation -- ``${X.content}`` strips quotes from a
captured string literal::

    emend edit replace 'Union["$X", $Y]' '${X.content} | $Y' src/ --apply


Commands
--------

All write operations default to **dry-run** (show diff).  Pass ``--apply`` to
write changes.

find
~~~~

Auto-detects mode from the query:

- **Pattern mode**: query contains ``$`` metavariables, or right side of
  ``::`` doesn't parse as a valid selector (e.g. ``assert False``, ``print()``)
- **Lookup mode**: query contains ``::`` with a valid selector
- **Summary mode**: bare file/dir path without filters
- **Bare name**: name that doesn't match a file searches as ``**::name``

Canonical command name is ``find``. Hidden compatibility aliases include ``search``,
``grep``, ``show``, ``get``, ``lookup``, and ``ls``.

::

    emend find 'print($X)' src/                           # pattern (has $)
    emend find '**::print($X)'                            # pattern with file scope
    emend find '**::assert False'                         # literal pattern via ::
    emend find 'src/::import os'                          # pattern scoped to src/
    emend find file.py::func[params]                      # lookup (valid selector)
    emend find file.py                                    # summary
    emend find process_encounter                          # bare name
    emend find ::MyClass                                  # ::name shorthand
    emend find hello src/                                 # bare name scoped to src/

Output formats (``--output``/``-o``):
  ``code``, ``location``, ``selector``, ``summary``, ``metadata``,
  ``json``, ``count``.  Modifiers: ``summary::flat``, ``code::dedent``.

Filters (lookup/summary mode):
  ``--kind function|method|class|async_function|async_method``,
  ``--name 'test_*'``, ``--returns str``, ``--depth 1``,
  ``--has-param ctx``, ``-i`` (case-insensitive), ``--smart-case``.

Scope constraints (``--where``):
  ``'def'``, ``'class'``, ``'def test_*'``, ``'async def fetch_*'``,
  ``'not class'``, ``'MyClass.method'``, ``'@decorator'``.

Other:
  ``--imported-from MODULE``, ``--scope-local``,
  ``--type-engine auto|pyrefly|pyright|ty``.

replace
~~~~~~~

::

    emend edit replace PATTERN REPLACEMENT PATH [--apply] [--where EXPR]

::

    emend edit replace 'print($X)' 'logger.info($X)' src/ --apply
    emend edit replace 'assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
    emend edit replace 'old_api($X)' 'new_api($X)' src/ --where 'def' --apply

edit
~~~~

Edit or replace existing symbol components. Canonical grouped form is
``emend edit set``. Hidden alias: ``set``.

::

    emend edit set SELECTOR VALUE [--apply]

::

    emend edit set api.py::get_user[returns] "User | None" --apply
    emend edit rm api.py::get_user[params][debug] --apply
    emend edit rm api.py::deprecated_func --apply

add
~~~

Insert new items into list components. Hidden alias: ``insert``.

::

    emend edit add SELECTOR VALUE [--at N] [--before NAME] [--after NAME] [--apply]

::

    emend edit add api.py::get_user[params] "timeout: int = 30" --apply
    emend edit add api.py::get_user[params] "ctx: Context" --after self --apply
    emend edit add api.py::func[params]:KEYWORD_ONLY "force: bool = False" --apply
    emend edit add api.py::func[decorators] "@cache" --at 0 --apply
    emend edit add models.py::User[bases] "TimestampMixin" --apply

rm
~~

Remove a symbol or component. Hidden aliases: ``remove`` and ``delete``.
Canonical grouped form is ``emend edit rm``.

::

    emend edit rm SELECTOR [--apply]

::

    emend edit rm api.py::get_user[params][debug] --apply
    emend edit rm api.py::deprecated_func --apply

refs
~~~~

Find all references (scope-aware). Hidden aliases: ``references``, ``find-references``.

::

    emend analyze refs SELECTOR [--exclude-definition] [--exclude-imports]
              [--writes-only] [--reads-only] [--calls-only] [--project DIR]

::

    emend analyze refs src/api.py::process_request --calls-only
    emend analyze refs config.py::settings --writes-only

rename
~~~~~~

Rename a symbol or module across the project::

    emend edit rename SELECTOR --to NEW_NAME [--apply] [--docs] [--unsure]

::

    emend edit rename api.py::OldClass --to NewClass --apply
    emend edit rename old_utils.py --to new_utils --apply   # module rename

mv
~~

Move a symbol or module, updating imports. Canonical grouped form is
``emend edit mv``. Hidden alias: ``move``.

::

    emend edit mv SELECTOR DESTINATION [--dedent] [--apply]

::

    emend edit mv utils.py::helper other.py --apply
    emend edit mv utils.py pkg/ --project . --apply   # module move

cp
~~

Copy a symbol to another file (exact AST copy, no hallucination risk). Canonical
grouped form is ``emend edit cp``. Hidden aliases: ``copy-to``, ``copy``.

::

    emend edit cp SELECTOR DESTINATION [--append] [--dedent] [--apply]

::

    emend edit cp module.py::OuterClass.inner_func helpers.py --dedent --apply

graph
~~~~~

Generate a call graph::

    emend analyze graph FILE [--format plain|json|dot] [--project DIR]

::

    emend analyze graph src/app.py --format dot | dot -Tsvg > deps.svg

deadcode
~~~~~~~~

Find potentially dead (unreferenced) code::

    emend analyze deadcode [PATH] [--kind function|class] [--include-private]
                           [--exclude-references-from DIR] [--no-strings]
                           [--no-last-reference] [--unused-modules] [--json]

Inline suppression: ``# noqa: emend:deadcode``.

lint
~~~~

Lint using rules from ``.emend/rules.yaml``::

    emend lint PATH [--config FILE] [--fix] [--rule NAME]

check
~~~~~

Run unified match, flow, deadcode, and type rules from
``.emend/rules.yaml``::

    emend check [PATHS]... [--rule NAME] [--kind KIND] [--fix] [--json]

batch
~~~~~

Apply batch operations from a YAML/JSON file::

    emend edit batch OPS_FILE [--apply]

Operation types: ``rename``, ``replace``, ``add``, ``edit``, ``remove``.

Example YAML::

    operations:
      - rename: {selector: "api.py::get_user", to: "fetch_user"}
      - replace: {pattern: "get_user($ID)", replacement: "fetch_user(user_id=$ID)", path: "src/"}
      - add: {selector: "api.py::fetch_user[params]:KEYWORD_ONLY", value: "timeout: float = 30.0"}


Fact graph (``facts_query``)
----------------------------

The ``facts_query`` tool provides structured access to the project fact graph.
Use the ``fact_type`` parameter to query: ``symbols``, ``calls``, ``references``,
``trace_flows``, ``types``, or ``imports``.


Cookbook recipes
---------------

Migrate unittest to pytest
~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend edit replace 'self.assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
    emend edit replace 'self.assertTrue($X)' 'assert $X' tests/ --apply
    emend edit replace 'self.assertFalse($X)' 'assert not $X' tests/ --apply
    emend edit replace 'self.assertIsNone($X)' 'assert $X is None' tests/ --apply
    emend edit replace 'self.assertIn($A, $B)' 'assert $A in $B' tests/ --apply

Replace print with logging
~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend edit replace 'print($X)' 'logger.info($X)' src/ --apply

Add a parameter to every method in a class
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend find api.py::MyClass --kind method --output selector | while read sel; do
        emend edit add "$sel[params]" "ctx: Context" --after self --apply
    done

Add return type annotations
~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend edit set api.py::get_user[returns] "User | None" --apply

Find and fix open() calls without encoding
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend find 'open($PATH)' src/
    emend edit replace 'open($PATH)' 'open($PATH, encoding="utf-8")' src/ --apply

Extract a nested function to a new module
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend edit cp module.py::Outer.inner_func helpers.py --dedent --apply
    emend edit rm module.py::Outer.inner_func --apply

Find all callers of a function
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend analyze refs src/api.py::process_request --calls-only

Find where a variable is mutated vs read
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend analyze refs config.py::settings --writes-only
    emend analyze refs config.py::settings --reads-only

Batch-add a decorator to all async functions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend find src/ --kind async_function --output selector | while read sel; do
        emend edit add "$sel[decorators]" "@trace" --at 0 --apply
    done

Rename a class everywhere (including docstrings)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend edit rename api.py::OldName --to NewName --docs --apply

Move a function and update all imports
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend edit mv utils.py::helper_func helpers/core.py --apply

Set up a lint rule to catch prints
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``.emend/rules.yaml``::

    rules:
      no-print:
        match: "print($...ARGS)"
        not-within: "def test_*"
        message: "Use logger instead of print"
        fix: "logger.info($...ARGS)"

Then::

    emend check src/
    emend check src/ --fix

Multi-step refactoring with batch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``refactor.yaml``::

    operations:
      - rename: {selector: "api.py::get_user", to: "fetch_user"}
      - replace:
          pattern: "get_user($ID)"
          replacement: "fetch_user(user_id=$ID)"
          path: "src/"
      - add:
          selector: "api.py::fetch_user[params]:KEYWORD_ONLY"
          value: "timeout: float = 30.0"

::

    emend edit batch refactor.yaml        # preview
    emend edit batch refactor.yaml --apply

Find functions that raise a specific exception
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    emend find 'raise ValueError($MSG)' src/ --output json

Scope-aware searching
~~~~~~~~~~~~~~~~~~~~~

::

    emend find 'print($X)' tests/ --within 'def test_*'      # inside test funcs
    emend find 'await $X' src/ --within 'async def fetch_*'  # inside async funcs
    emend find 'print($X)' src/ --not-within 'class'         # outside classes
    emend find 'config' src/ --scope-local                   # only local definitions
    emend find 'json.loads($X)' src/ --imported-from json    # import-aware
