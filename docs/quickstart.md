# Quick Start

## Workflow

1. **Preview changes**: Run any command without `--apply` (dry-run is the default) to see a diff of proposed changes.
2. **Review the diff output**.
3. **Apply changes**: Re-run with `--apply`.
4. **Format code**: Run your formatter (black, ruff, isort) -- emend may not preserve exact formatting.
5. **Verify**: Run tests and type checks.

## Example: Add a parameter

```bash
# Preview the change (dry-run)
emend edit api.py::get_user[params] "user_id: int, force: bool = False"

# Apply it
emend edit api.py::get_user[params] "user_id: int, force: bool = False" --apply
```

## Example: Find and replace a pattern

```bash
# Find all print() calls in src/
emend find 'print($X)' src/

# Replace them with logger.info()
emend replace 'print($X)' 'logger.info($X)' src/ --apply
```

## Example: Look up symbol information

```bash
# Show a function's source code
emend lookup api.py::get_user

# Get return type
emend lookup api.py::get_user[returns]

# List all functions in a file
emend lookup api.py --kind function
```
