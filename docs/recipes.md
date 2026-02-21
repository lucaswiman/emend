# Recipes

Common refactoring tasks with emend.

## Migrate from unittest to pytest

Replace assertion methods with native pytest assertions:

```bash
emend replace 'self.assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
emend replace 'self.assertNotEqual($A, $B)' 'assert $A != $B' tests/ --apply
emend replace 'self.assertTrue($X)' 'assert $X' tests/ --apply
emend replace 'self.assertFalse($X)' 'assert not $X' tests/ --apply
emend replace 'self.assertIsNone($X)' 'assert $X is None' tests/ --apply
emend replace 'self.assertIsNotNone($X)' 'assert $X is not None' tests/ --apply
emend replace 'self.assertIn($A, $B)' 'assert $A in $B' tests/ --apply
emend replace 'self.assertNotIn($A, $B)' 'assert $A not in $B' tests/ --apply
```

Or use a batch file for the same thing:

```yaml
# migrate-pytest.yaml
operations:
  - replace: {pattern: "self.assertEqual($A, $B)", replacement: "assert $A == $B", path: "tests/"}
  - replace: {pattern: "self.assertTrue($X)", replacement: "assert $X", path: "tests/"}
  - replace: {pattern: "self.assertFalse($X)", replacement: "assert not $X", path: "tests/"}
  - replace: {pattern: "self.assertIsNone($X)", replacement: "assert $X is None", path: "tests/"}
```

```bash
emend batch migrate-pytest.yaml --apply
```

## Replace print with logging

```bash
# Preview first
emend replace 'print($X)' 'logger.info($X)' src/

# Apply everywhere
emend replace 'print($X)' 'logger.info($X)' src/ --apply
```

## Set up a lint rule to catch prints

```yaml
# .emend/patterns.yaml
rules:
  no-print:
    find: "print($...ARGS)"
    not-inside: "def test_*"
    message: "Use logger instead of print in production code"
    replace: "logger.info($...ARGS)"
```

```bash
# Check for violations
emend lint src/

# Auto-fix violations
emend lint src/ --fix
```

## Add type annotations to function returns

First find all functions missing return types:

```bash
emend lookup src/ --kind function --json | grep -v returns
```

Then add them one at a time:

```bash
emend edit api.py::get_user[returns] "User | None" --apply
emend edit api.py::create_user[returns] "User" --apply
```

## Add a parameter to every method in a class

```bash
# Preview
emend lookup api.py::MyClass --kind method --paths-only | while read sel; do
    emend add "$sel[params]" "ctx: Context" --after self
done

# Apply
emend lookup api.py::MyClass --kind method --paths-only | while read sel; do
    emend add "$sel[params]" "ctx: Context" --after self --apply
done
```

## Replace deprecated API calls

```bash
# Old: requests.get(url, **kwargs)
# New: httpx.get(url, **kwargs)
emend replace 'requests.get($URL)' 'httpx.get($URL)' src/ --apply
emend replace 'requests.post($URL, $DATA)' 'httpx.post($URL, $DATA)' src/ --apply
```

## Rename a class everywhere

```bash
# Preview
emend rename api.py::OldName --to NewName

# Apply (including docstrings)
emend rename api.py::OldName --to NewName --docs --apply
```

## Move a helper function to another module

```bash
# Preview (shows diff for source, destination, and all files that import it)
emend move utils.py::helper_func helpers/core.py

# Apply
emend move utils.py::helper_func helpers/core.py --apply
```

## Find all functions that raise a specific exception

```bash
emend find 'raise ValueError($MSG)' src/ --json
```

## Audit all open() calls (check for missing encoding)

```bash
# Find open() calls without encoding kwarg
emend find 'open($PATH)' src/
emend find 'open($PATH, $MODE)' src/

# Add encoding where missing
emend replace 'open($PATH)' 'open($PATH, encoding="utf-8")' src/ --apply
```

## Find all places a function is called

```bash
# Pattern-based search (text matching)
emend find 'process_request($X)' src/ --json

# Scope-aware callers analysis (uses LibCST scope analysis)
emend callers src/api.py::process_request
```

## Understand what a function depends on

```bash
# What functions does main() call?
emend callees src/app.py::main

# Who calls process()?
emend callers src/app.py::process

# Visualize the call graph for the whole file
emend graph src/app.py --format dot | dot -Tsvg > deps.svg
```

## Find where a variable is mutated

```bash
# Only write references (assignments)
emend find-references config.py::settings --writes-only

# Only read references (loads)
emend find-references config.py::settings --reads-only
```

## Extract and move a nested function

```bash
# Extract (with dedent to fix indentation)
emend copy-to module.py::OuterClass.inner_func helpers.py --dedent --apply

# Then remove from original
emend edit module.py::OuterClass.inner_func --rm --apply
```

## Rename a module

```bash
# Rename the file and update all imports
emend rename-module old_utils.py new_utils --apply
```

## Batch-add a decorator to all async functions

```bash
emend lookup src/ --kind async_function --paths-only | while read sel; do
    emend add "$sel[decorators]" "@trace" --at 0 --apply
done
```

## Multi-step refactoring with batch

```yaml
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
```

```bash
# Preview all changes
emend batch refactor.yaml

# Apply all changes
emend batch refactor.yaml --apply
```

## Use the unified search command

```bash
# Pattern mode (automatically detected because of $)
emend search 'print($X)' src/

# Lookup mode (automatically detected because of ::)
emend search api.py::get_user[params]

# Lookup with filters
emend search src/ --kind function --has-decorator cache
```

## Find imports from a specific module

```bash
# Only match when json is actually imported from the json module
emend find 'json.loads($X)' src/ --imported-from json
```

## Scope-aware pattern searching

```bash
# Find prints only inside test functions
emend find 'print($X)' tests/ --inside 'def test_*'

# Find awaits only inside async fetch functions
emend find 'await $X' src/ --where 'async def fetch_*'

# Find locally-defined config variables (not imported ones)
emend find 'config' src/ --scope-local

# Find prints NOT inside try blocks
emend find 'print($X)' src/ --not-inside 'try:'
```

## Find dict literals with specific keys

```bash
# Find all dicts with a 'type' key set to 'user'
emend find "{'type': 'user', ...}" src/

# Find exact dict structures
emend find "{'name': \$NAME, 'age': \$AGE}" src/
```

## Find walrus operator usage

```bash
# Find walrus in if conditions
emend find 'if ($VAR := $EXPR):' src/
```

## Find chained comparisons

```bash
# Find range checks
emend find '$A < $B < $C' src/
```
