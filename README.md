# emend

A Python refactoring CLI tool built on [LibCST](https://github.com/Instagram/LibCST), with AST-based commands for handling nested functions and closures.

Built on two complementary systems:
- **Structured Edits** - Precise changes to symbol metadata using selectors like `file.py::func[params][0]`
- **Pattern Transforms** - Code-pattern search and replace with capture variables like `print($X)` → `logger.info($X)`

## Installation

### Using uv (recommended)

```bash
uv tool install emend
```

### Using pip

```bash
pip install emend
```

Verify the installation:

```bash
emend --help
```

## Usage

```bash
emend <command> [options]
```

### Workflow

1. **Preview changes**: Run with `--dry-run` (default) to see what will change
2. **Review the diff output**
3. **Apply changes**: Re-run with `--apply`
4. **Format code**: Run formatters (black/ruff/isort) - emend may not preserve exact formatting
5. **Verify**: Run tests/type checks

## Selector Syntax

Three types of selectors:

### Symbol Selectors
```bash
file.py::Class.method.nested   # Nested symbol path
file.py::func                  # Module-level symbol
```

### Extended Selectors (with components)
```bash
file.py::func[params]           # Function parameters
file.py::func[params][ctx]      # Specific parameter (by name)
file.py::func[params][0]        # Specific parameter (by index)
file.py::func[returns]          # Return annotation
file.py::func[decorators]       # Decorator list
file.py::MyClass[bases]         # Base classes
file.py::func[body]             # Function body
```

### Pseudo-class Selectors
```bash
file.py::func[params]:KEYWORD_ONLY       # Keyword-only parameter slot
file.py::func[params]:POSITIONAL_ONLY    # Positional-only parameter slot
```

### Line Selectors
```bash
file.py:42                      # Single line
file.py:42-100                  # Line range
```

### Wildcard Selectors
```bash
file.py::*[params]              # All function parameters
file.py::Test*[decorators]      # Test class parameters
file.py::*.*[returns]           # All method return types
file.py::Class.*[body]          # All method bodies in Class
```

Wildcards support glob patterns:
- `*` - Match any symbol at this level
- `Test*` - Match symbols starting with Test
- `*.*` - Match any method in any class
- `Class.*` - Match any method in Class

### Selector Grammar (Lark)

```lark
start: selector

selector: file_path DOUBLE_COLON symbol_path? component*

file_path: PATH
symbol_path: IDENTIFIER ("." IDENTIFIER)*
component: "[" COMPONENT_NAME "]" accessor? pseudo_class?
accessor: "[" (IDENTIFIER | INT) "]"
pseudo_class: PSEUDO_CLASS

COMPONENT_NAME: "params" | "returns" | "decorators" | "bases" | "body" | "imports"
DOUBLE_COLON: "::"
PATH: /[^:]+/
IDENTIFIER: /[a-zA-Z_][a-zA-Z0-9_]*/
INT: /\d+/
PSEUDO_CLASS: /:KEYWORD_ONLY|:POSITIONAL_ONLY|:POSITIONAL_OR_KEYWORD/
```

## Commands

### Read Commands

#### `lookup` - Unified read command
Combines `get`, `query`, and `show` with smart mode detection:
- **Component extraction** (like `get`): `emend lookup file.py::func[params]`
- **Filtering** (like `query`): `emend lookup file.py --kind function --name test_*`
- **Display source** (like `show`): `emend lookup file.py::func`
- **Wildcard support**: `emend lookup 'file.py::*[params]'` for bulk extraction
- **Output modes**: `--json`, `--metadata`, `--paths-only`, `--count`

#### `edit` - Unified write command
Combines `set`, `add`, and `rm` with location control:
- **Replace** (like `set`): `emend edit file.py::func[returns] "int" --apply`
- **Insert** (like `add`): `emend edit file.py::func[params] "new_param" --before ctx --apply`
- **Remove** (like `rm`): `emend edit file.py::func[params][old_param] --rm --apply`
- **Position flags**: `--before`, `--after`, `--at` for precise insertion
- **Pseudo-class support**: `:KEYWORD_ONLY`, `:POSITIONAL_ONLY` for parameters

### Alternate Structured Edit Commands
- `get` - Read component value (alias for `lookup`)
- `set` - Replace component value (alias for `edit`)
- `add` - Insert into list component (alias for `edit`)
- `rm` - Remove symbol or component (alias for `edit`)

### Pattern Transform Commands
- `find` - Find pattern matches
- `replace` - Simple pattern substitution
- `batch` - Run multiple operations from JSON

### Symbol Management
- `query` - Find symbols with filters → use `lookup` instead
- `show` - Display symbol source code → use `lookup` instead
- `list-symbols` - List symbols in a module
- `find-references` - Find all references to a symbol
- `rename` - Rename a symbol across the project
- `move` - Move a symbol to another file with import updates
- `copy-to` - Copy a symbol to another file

### Module Operations
- `move-module` - Move a module to another package
- `rename-module` - Rename a module file


### Utility
- `batch` - Run multiple operations from JSON

## Examples

### Read and Edit Examples

#### Using `lookup`
```bash
# Extract function parameters
emend lookup api.py::handler[params]

# Extract all function parameters from a file (wildcard)
emend lookup 'api.py::*[params]'

# Query functions by name pattern
emend lookup src/ --kind function --name test_*

# Show function source code
emend lookup api.py::handler

# Get return types as JSON
emend lookup 'src/**/*.py::*[returns]' --json
```

#### Using `edit`
```bash
# Update return type
emend edit api.py::handler[returns] "Response" --apply

# Add parameter with default value
emend edit api.py::handler[params] "timeout: int = 30" --apply

# Add keyword-only parameter using pseudo-class
emend edit "api.py::handler[params]:KEYWORD_ONLY" "debug: bool" --apply

# Insert parameter before specific param
emend edit api.py::handler[params] "ctx: Context" --before user_id --apply

# Remove a specific parameter
emend edit api.py::handler[params][deprecated_arg] --rm --apply
```

### Alternate Structured Edits

```bash
# These alternate commands also work:
emend add api.py::handler[params] "timeout: int = 30" --apply
emend set api.py::handler[returns] "Response" --apply
emend rm api.py::handler[params][deprecated_arg] --apply
```

### Pattern Transforms

```bash
# Simple find and replace (dry-run by default)
emend replace 'print($X)' 'logger.info($X)' file.py

# Replace within a specific scope
emend replace 'old_var' 'new_var' api.py --in process --apply

# Replace with pattern capture
emend replace 'get_field($N)' 'field$N' api.py --in process --apply

# Find pattern matches
emend find 'print($X)' src/

# Multi-rule batch operations
emend batch rules.json --apply

# Or use shell loop for multiple replacements
for pattern in "pattern1:replacement1" "pattern2:replacement2"; do
  IFS=: read -r from to <<< "$pattern"
  emend replace "$from" "$to" file.py --apply
done
```

### Advanced Operations

```bash
# List symbols with full nesting
emend list-symbols workflow.py --tree-depth 3

# Extract nested function to another file
emend copy-to workflow.py::Builder._build.helper tasks.py --dedent --apply

# Rename a symbol project-wide
emend rename models.py::User --to Account --apply

# Move a symbol to another file (updates imports)
emend move utils.py::parse_date helpers/dates.py --apply

# Find all references to a symbol
emend find-references models.py::User --json

# Copy imports from one file to another (using primitives)
emend get source.py::[imports] | while read import_line; do
  emend add dest.py::[imports] "$import_line" --apply
done
```

### Pattern Syntax

Patterns support metavariables for capturing:

```bash
# Single expression
emend find 'print($MSG)' src/

# Multiple arguments with capture
emend find 'func($A, $B)' src/

# Variable arguments
emend find 'func($...ARGS)' src/

# Type constraints
emend find 'range($N:int)' src/

# Anonymous metavariables
emend find 'func($_, $ARG)' src/

# Structural constraints
emend find 'print($X)' src/ --inside 'async def'
emend find 'await $X' src/ --not-inside 'if __debug__'

# Supported pattern types:
#   Literals: $X, $MSG:str, $N:int, 3.14
#   Calls: func($X), obj.method($A, $B)
#   Operations: $A + $B, $A and $B, not $X, $X[$Y]
#   Collections: ($A, $B), [$X, $Y], {$K: $V}
#   Control: return $X, assert $A == $B, raise $EXC
```

### Pattern Grammar (Lark)

```lark
start: pattern

pattern: (code_chunk | metavar)+

metavar: DOLLAR (ELLIPSIS)? METAVAR_NAME TYPE_CONSTRAINT?
       | DOLLAR UNDERSCORE

DOLLAR: "$"
ELLIPSIS: "..."
UNDERSCORE: "_"
METAVAR_NAME: /[A-Z][A-Z0-9_]*/
TYPE_CONSTRAINT: /:(?:expr|stmt|identifier|int|str|float|call|attr|any)/
code_chunk: /[^$:]+/ | ":"
```

The `code_chunk` rule excludes colons (`/[^$:]+/`) to prevent consuming colons that are part of type constraints (e.g., `$MSG:str`). A standalone colon is matched by the alternative `| ":"` for patterns containing colons outside of type constraints.

### Diff Patch Format

```
- pattern_to_find
+ replacement_pattern

- another_pattern
+ another_replacement
```

Lines prefixed with `-` are matched; corresponding `+` lines are the replacement. Blank lines separate rules.

## Development

### Installing from Source

Clone the repository and install for development:

```bash
git clone https://github.com/anthropics/emend
cd emend

# Using make (creates virtual environment)
make venv

# Or manually:
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
make test

# Run specific test file
make test TESTS=tests/test_emend/test_add_parameter.py

# Run specific test
make test TESTS="tests/test_emend/test_add_parameter.py::test_add_parameter_with_default"
```

### Project Structure

```
emend/
├── src/emend/
│   ├── cli.py                # CLI entry point, argument parsing
│   ├── transform.py          # Transform primitives (get/set/add/remove/find/replace)
│   ├── pattern.py            # Pattern parsing and compilation
│   ├── query.py              # Symbol querying with filters
│   ├── ast_commands.py       # AST-based command implementations
│   ├── ast_utils.py          # AST traversal utilities
│   ├── component_selector.py # Extended selector parsing
│   └── grammars/
│       ├── selector.lark     # Extended selector grammar
│       └── pattern.lark      # Pattern grammar
├── tests/test_emend/         # Test suite
├── Makefile
└── pyproject.toml
```

## License

MPL 2.0
