Recipes
=======

Common refactoring tasks with emend.

Migrate from unittest to pytest
-------------------------------

Replace assertion methods with native pytest assertions:

.. code-block:: bash

   emend edit replace 'self.assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
   emend edit replace 'self.assertNotEqual($A, $B)' 'assert $A != $B' tests/ --apply
   emend edit replace 'self.assertTrue($X)' 'assert $X' tests/ --apply
   emend edit replace 'self.assertFalse($X)' 'assert not $X' tests/ --apply
   emend edit replace 'self.assertIsNone($X)' 'assert $X is None' tests/ --apply
   emend edit replace 'self.assertIsNotNone($X)' 'assert $X is not None' tests/ --apply
   emend edit replace 'self.assertIn($A, $B)' 'assert $A in $B' tests/ --apply
   emend edit replace 'self.assertNotIn($A, $B)' 'assert $A not in $B' tests/ --apply

Or use a batch file for the same thing:

.. code-block:: yaml

   # migrate-pytest.yaml
   operations:
     - replace: {pattern: "self.assertEqual($A, $B)", replacement: "assert $A == $B", path: "tests/"}
     - replace: {pattern: "self.assertTrue($X)", replacement: "assert $X", path: "tests/"}
     - replace: {pattern: "self.assertFalse($X)", replacement: "assert not $X", path: "tests/"}
     - replace: {pattern: "self.assertIsNone($X)", replacement: "assert $X is None", path: "tests/"}

.. code-block:: bash

   emend edit batch migrate-pytest.yaml --apply

Replace print with logging
---------------------------

.. code-block:: bash

   # Preview first
   emend edit replace 'print($X)' 'logger.info($X)' src/

   # Apply everywhere
   emend edit replace 'print($X)' 'logger.info($X)' src/ --apply

Set up a lint rule to catch prints
-----------------------------------

.. code-block:: yaml

   # .emend/rules.yaml
   rules:
     no-print:
       match: "print($...ARGS)"
       not-within: "def test_*"
       message: "Use logger instead of print in production code"
       fix: "logger.info($...ARGS)"

.. code-block:: bash

   # Check for violations
   emend lint src/

   # Auto-fix violations
   emend lint src/ --fix

Add type annotations to function returns
----------------------------------------

First find all functions missing return types:

.. code-block:: bash

   emend find src/ --kind function --json | grep -v returns

Then add them one at a time:

.. code-block:: bash

   emend edit set api.py::get_user[returns] "User | None" --apply
   emend edit set api.py::create_user[returns] "User" --apply

Add a parameter to every method in a class
-------------------------------------------

.. code-block:: bash

   # Preview
   emend find api.py::MyClass --kind method --output selector | while read sel; do
       emend edit add "$sel[params]" "ctx: Context" --after self
   done

   # Apply
   emend find api.py::MyClass --kind method --output selector | while read sel; do
       emend edit add "$sel[params]" "ctx: Context" --after self --apply
   done

Replace deprecated API calls
-----------------------------

.. code-block:: bash

   # Old: requests.get(url, **kwargs)
   # New: httpx.get(url, **kwargs)
   emend edit replace 'requests.get($URL)' 'httpx.get($URL)' src/ --apply
   emend edit replace 'requests.post($URL, $DATA)' 'httpx.post($URL, $DATA)' src/ --apply

Rename a class everywhere
--------------------------

.. code-block:: bash

   # Preview
   emend edit rename api.py::OldName --to NewName

   # Apply (including docstrings)
   emend edit rename api.py::OldName --to NewName --docs --apply

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

   emend find 'raise ValueError($MSG)' src/ --json

Audit all open() calls (check for missing encoding)
---------------------------------------------------

.. code-block:: bash

   # Find open() calls without encoding kwarg
   emend find 'open($PATH)' src/
   emend find 'open($PATH, $MODE)' src/

   # Add encoding where missing
   emend edit replace 'open($PATH)' 'open($PATH, encoding="utf-8")' src/ --apply

Find all places a function is called
------------------------------------

.. code-block:: bash

   # Pattern-based search (text matching)
   emend find 'process_request($X)' src/ --json

   # Scope-aware callers analysis (uses tree-sitter scope analysis)
   emend analyze refs src/api.py::process_request --calls-only

Understand what a function depends on
-------------------------------------

.. code-block:: bash

   # What functions does main() call?
   emend analyze graph src/app.py::main

   # Who calls process()?
   emend analyze refs src/app.py::process --calls-only

   # Visualize the call graph for the whole file
   emend analyze graph src/app.py --format dot | dot -Tsvg > deps.svg

Find where a variable is mutated
---------------------------------

.. code-block:: bash

   # Only write references (assignments)
   emend analyze refs config.py::settings --writes-only

   # Only read references (loads)
   emend analyze refs config.py::settings --reads-only

Extract and move a nested function
----------------------------------

.. code-block:: bash

   # Extract (with dedent to fix indentation)
   emend edit cp module.py::OuterClass.inner_func helpers.py --dedent --apply

   # Then remove from original
   emend edit rm module.py::OuterClass.inner_func --apply

Rename a module
---------------

.. code-block:: bash

   # Rename the file and update all imports
   emend edit rename old_utils.py --to new_utils --apply

Batch-add a decorator to all async functions
---------------------------------------------

.. code-block:: bash

   emend find src/ --kind async_function --output selector | while read sel; do
       emend edit add "$sel[decorators]" "@trace" --at 0 --apply
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
   emend edit batch refactor.yaml

   # Apply all changes
   emend edit batch refactor.yaml --apply

Use the canonical find command
-----------------------------

.. code-block:: bash

   # Pattern mode (automatically detected because of $)
   emend find 'print($X)' src/

   # Lookup mode (automatically detected because of ::)
   emend find api.py::get_user[params]

   # Lookup with filters
   emend find src/ --kind function --matching '@cache'

Find imports from a specific module
-----------------------------------

.. code-block:: bash

   # Only match when json is actually imported from the json module
   emend find 'json.loads($X)' src/ --imported-from json

Scope-aware pattern searching
-----------------------------

.. code-block:: bash

   # Find prints only inside test functions
   emend find 'print($X)' tests/ --within 'def test_*'

   # Find awaits only inside async fetch functions
   emend find 'await $X' src/ --within 'async def fetch_*'

   # Find locally-defined config variables (not imported ones)
   emend find 'config' src/ --scope-local

   # Find prints NOT inside try blocks
   emend find 'print($X)' src/ --not-within 'try:'

Find dict literals with specific keys
--------------------------------------

.. code-block:: bash

   # Find all dicts with a 'type' key set to 'user'
   emend find "{'type': 'user', ...}" src/

   # Find exact dict structures
   emend find "{'name': \$NAME, 'age': \$AGE}" src/

Find walrus operator usage
--------------------------

.. code-block:: bash

   # Find walrus in if conditions
   emend find 'if ($VAR := $EXPR):' src/

Find chained comparisons
------------------------

.. code-block:: bash

   # Find range checks
   emend find '$A < $B < $C' src/


Embedded SQL analysis
---------------------

Search inside SQL strings embedded in Python code:

.. code-block:: bash

   # Find all SQL queries referencing a table
   emend find 'FROM users' src/ --dsl sql

   # Find SELECT * anti-patterns
   emend find 'SELECT * FROM $TABLE' src/ --dsl sql

   # Lint SQL for anti-patterns
   cat > .emend/rules.yaml << 'EOF'
   rules:
     no-select-star:
       dsl: sql
       match: "SELECT * FROM $TABLE"
       message: "Avoid SELECT *; enumerate columns explicitly"
   EOF
   emend lint src/

   # See which SQL queries are affected by an ORM model change
   emend analyze impact models.py::User --json

Navigate regex named groups
---------------------------

Find where regex named groups are defined and used:

.. code-block:: python

   # In your code:
   pattern = re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})")
   m = pattern.match(text)
   year = m.group("year")
