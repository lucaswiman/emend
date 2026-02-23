Recipes
=======

Common refactoring tasks with emend.

Migrate from unittest to pytest
-------------------------------

Replace assertion methods with native pytest assertions:

.. code-block:: bash

   emend replace 'self.assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
   emend replace 'self.assertNotEqual($A, $B)' 'assert $A != $B' tests/ --apply
   emend replace 'self.assertTrue($X)' 'assert $X' tests/ --apply
   emend replace 'self.assertFalse($X)' 'assert not $X' tests/ --apply
   emend replace 'self.assertIsNone($X)' 'assert $X is None' tests/ --apply
   emend replace 'self.assertIsNotNone($X)' 'assert $X is not None' tests/ --apply
   emend replace 'self.assertIn($A, $B)' 'assert $A in $B' tests/ --apply
   emend replace 'self.assertNotIn($A, $B)' 'assert $A not in $B' tests/ --apply

Or use a batch file for the same thing:

.. code-block:: yaml

   # migrate-pytest.yaml
   operations:
     - replace: {pattern: "self.assertEqual($A, $B)", replacement: "assert $A == $B", path: "tests/"}
     - replace: {pattern: "self.assertTrue($X)", replacement: "assert $X", path: "tests/"}
     - replace: {pattern: "self.assertFalse($X)", replacement: "assert not $X", path: "tests/"}
     - replace: {pattern: "self.assertIsNone($X)", replacement: "assert $X is None", path: "tests/"}

.. code-block:: bash

   emend batch migrate-pytest.yaml --apply

Replace print with logging
---------------------------

.. code-block:: bash

   # Preview first
   emend replace 'print($X)' 'logger.info($X)' src/

   # Apply everywhere
   emend replace 'print($X)' 'logger.info($X)' src/ --apply

Set up a lint rule to catch prints
-----------------------------------

.. code-block:: yaml

   # .emend/patterns.yaml
   rules:
     no-print:
       find: "print($...ARGS)"
       not-inside: "def test_*"
       message: "Use logger instead of print in production code"
       replace: "logger.info($...ARGS)"

.. code-block:: bash

   # Check for violations
   emend lint src/

   # Auto-fix violations
   emend lint src/ --fix

Add type annotations to function returns
----------------------------------------

First find all functions missing return types:

.. code-block:: bash

   emend search src/ --kind function --json | grep -v returns

Then add them one at a time:

.. code-block:: bash

   emend edit api.py::get_user[returns] "User | None" --apply
   emend edit api.py::create_user[returns] "User" --apply

Add a parameter to every method in a class
-------------------------------------------

.. code-block:: bash

   # Preview
   emend search api.py::MyClass --kind method --output selector | while read sel; do
       emend add "$sel[params]" "ctx: Context" --after self
   done

   # Apply
   emend search api.py::MyClass --kind method --output selector | while read sel; do
       emend add "$sel[params]" "ctx: Context" --after self --apply
   done

Replace deprecated API calls
-----------------------------

.. code-block:: bash

   # Old: requests.get(url, **kwargs)
   # New: httpx.get(url, **kwargs)
   emend replace 'requests.get($URL)' 'httpx.get($URL)' src/ --apply
   emend replace 'requests.post($URL, $DATA)' 'httpx.post($URL, $DATA)' src/ --apply

Rename a class everywhere
--------------------------

.. code-block:: bash

   # Preview
   emend rename api.py::OldName --to NewName

   # Apply (including docstrings)
   emend rename api.py::OldName --to NewName --docs --apply

Move a helper function to another module
----------------------------------------

.. code-block:: bash

   # Preview (shows diff for source, destination, and all files that import it)
   emend move utils.py::helper_func helpers/core.py

   # Apply
   emend move utils.py::helper_func helpers/core.py --apply

Find all functions that raise a specific exception
--------------------------------------------------

.. code-block:: bash

   emend search 'raise ValueError($MSG)' src/ --json

Audit all open() calls (check for missing encoding)
---------------------------------------------------

.. code-block:: bash

   # Find open() calls without encoding kwarg
   emend search 'open($PATH)' src/
   emend search 'open($PATH, $MODE)' src/

   # Add encoding where missing
   emend replace 'open($PATH)' 'open($PATH, encoding="utf-8")' src/ --apply

Find all places a function is called
------------------------------------

.. code-block:: bash

   # Pattern-based search (text matching)
   emend search 'process_request($X)' src/ --json

   # Scope-aware callers analysis (uses LibCST scope analysis)
   emend refs src/api.py::process_request --calls-only

Understand what a function depends on
-------------------------------------

.. code-block:: bash

   # What functions does main() call?
   emend graph src/app.py::main

   # Who calls process()?
   emend refs src/app.py::process --calls-only

   # Visualize the call graph for the whole file
   emend graph src/app.py --format dot | dot -Tsvg > deps.svg

Find where a variable is mutated
---------------------------------

.. code-block:: bash

   # Only write references (assignments)
   emend refs config.py::settings --writes-only

   # Only read references (loads)
   emend refs config.py::settings --reads-only

Extract and move a nested function
----------------------------------

.. code-block:: bash

   # Extract (with dedent to fix indentation)
   emend copy-to module.py::OuterClass.inner_func helpers.py --dedent --apply

   # Then remove from original
   emend edit module.py::OuterClass.inner_func --rm --apply

Rename a module
---------------

.. code-block:: bash

   # Rename the file and update all imports
   emend rename old_utils.py --to new_utils --apply

Batch-add a decorator to all async functions
---------------------------------------------

.. code-block:: bash

   emend search src/ --kind async_function --output selector | while read sel; do
       emend add "$sel[decorators]" "@trace" --at 0 --apply
   done

Multi-step refactoring with batch
---------------------------------

.. code-block:: yaml

   # refactor.yaml
   operations:
     - rename: {selector: "api.py::get_user", to: "fetch_user"}
     - replace:
         pattern: "get_user($ID)"
         replacement: "fetch_user(user_id=$ID)"
         path: "src/"
     - add:
         selector: "api.py::fetch_user[params]:KEYWORD_ONLY"
         value: "timeout: float = 30.0"

.. code-block:: bash

   # Preview all changes
   emend batch refactor.yaml

   # Apply all changes
   emend batch refactor.yaml --apply

Use the unified search command
------------------------------

.. code-block:: bash

   # Pattern mode (automatically detected because of $)
   emend search 'print($X)' src/

   # Lookup mode (automatically detected because of ::)
   emend search api.py::get_user[params]

   # Lookup with filters
   emend search src/ --kind function --has-decorator cache

Find imports from a specific module
-----------------------------------

.. code-block:: bash

   # Only match when json is actually imported from the json module
   emend search 'json.loads($X)' src/ --imported-from json

Scope-aware pattern searching
-----------------------------

.. code-block:: bash

   # Find prints only inside test functions
   emend search 'print($X)' tests/ --inside 'def test_*'

   # Find awaits only inside async fetch functions
   emend search 'await $X' src/ --where 'async def fetch_*'

   # Find locally-defined config variables (not imported ones)
   emend search 'config' src/ --scope-local

   # Find prints NOT inside try blocks
   emend search 'print($X)' src/ --not-inside 'try:'

Find dict literals with specific keys
--------------------------------------

.. code-block:: bash

   # Find all dicts with a 'type' key set to 'user'
   emend search "{'type': 'user', ...}" src/

   # Find exact dict structures
   emend search "{'name': \$NAME, 'age': \$AGE}" src/

Find walrus operator usage
--------------------------

.. code-block:: bash

   # Find walrus in if conditions
   emend search 'if ($VAR := $EXPR):' src/

Find chained comparisons
------------------------

.. code-block:: bash

   # Find range checks
   emend search '$A < $B < $C' src/
