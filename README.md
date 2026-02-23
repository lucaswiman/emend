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

### Search & Read

**`search`** - Unified search with auto-detection
- Pattern mode: `emend search 'print($X)' file.py`
- Lookup mode: `emend search file.py::func`
- Summary mode: `emend search file.py` (list symbols)
- Filters: `--kind`, `--name`, `--returns`, `--depth`, `--has-param`, `--output`, `--where`, `--imported-from`, `--scope-local`
- Output formats: `code`, `location`, `selector`, `summary`, `metadata`, `json`, `count`, `summary::flat`, `code::dedent`
- Also available as: `query`, `show`, `get`, `lookup`, `find` for intuitive workflows

### Edit & Transform

**`edit`** - Modify or remove existing symbol components
- Replace: `emend edit file.py::func[returns] "int" --apply`
- Insert: `emend edit file.py::func[params] "new_param" --before ctx --apply`
- Remove: `emend edit file.py::func[params][old_param] --rm --apply`
- Wildcards: `emend edit 'file.py::*[decorators]' "@dataclass" --apply`

**`add`** - Insert new items into list components (alternative to `edit`)
- `emend add file.py::func[params] "timeout: int = 30" --apply`
- `emend add "file.py::func[params]:KEYWORD_ONLY" "debug: bool" --apply`

**`replace`** - Replace pattern matches with pattern-based substitution
- `emend replace 'print($X)' 'logger.info($X)' file.py --apply`
- Scope constraints: `--in`, `--inside`, `--not-inside`, `--where`

### Symbol Management

**`refs`** - Find all references to a symbol across the project
- `emend refs models.py::User`
- Filters: `--writes-only`, `--reads-only`, `--calls-only`
- Output: `--output json`, `--output location`

**`rename`** - Rename a symbol or module across the project
- Symbol: `emend rename models.py::User --to Account --apply`
- Module: `emend rename models.py --to accounts.py --apply`
- Filters: `--docs`, `--no-hierarchy`, `--unsure`

**`move`** - Move a symbol or module with automatic import updates
- Symbol: `emend move utils.py::parse_date helpers/dates.py --apply`
- Module: `emend move utils.py helpers/utils.py --apply`

**`copy-to`** - Copy a symbol to another file
- `emend copy-to workflow.py::Builder._build.helper tasks.py --dedent --apply`

### Utilities

**`batch`** - Apply multiple refactoring operations from YAML/JSON file
- `emend batch rules.json --apply`

**`lint`** - Lint files using pattern rules from `.emend/patterns.yaml`
- `emend lint src/`
- `emend lint src/ --fix --apply` to auto-fix issues

**`graph`** - Generate a call graph for functions in a file
- `emend graph src/module.py --format plain`
- Formats: `plain`, `json`, `dot`

## Examples

### Search & Read Examples

```bash
# Search by pattern (pattern mode)
emend search 'print($X)' src/
emend search 'assertEqual($A, $B)' tests/ --output count

# Search by symbol (lookup mode)
emend search file.py::func
emend search src/ --kind function --name test_*
emend search file.py --output json

# Extract function parameters
emend search api.py::handler[params]
emend search 'api.py::*[params]'  # Wildcard: all function params in file

# Get return types
emend search 'src/**/*.py::*[returns]' --output metadata

# List symbols in a module
emend search file.py                          # Tree view
emend search file.py --output summary::flat   # Flat list
emend search file.py --depth 2                # Limit nesting depth
```

### Edit Examples

```bash
# Update return type
emend edit api.py::handler[returns] "Response" --apply

# Add parameter with default value
emend edit api.py::handler[params] "timeout: int = 30" --apply

# Add keyword-only parameter
emend edit "api.py::handler[params]:KEYWORD_ONLY" "debug: bool" --apply

# Insert parameter before specific param
emend edit api.py::handler[params] "ctx: Context" --before user_id --apply

# Remove a specific parameter
emend edit api.py::handler[params][deprecated_arg] --rm --apply

# Edit multiple symbols at once (wildcards)
emend edit 'file.py::*[decorators]' "@dataclass" --apply
```

### Pattern Transform Examples

```bash
# Simple find and replace (dry-run by default)
emend replace 'print($X)' 'logger.info($X)' file.py

# Replace within a specific scope
emend replace 'old_var' 'new_var' api.py --in process --apply

# Replace with pattern capture
emend replace 'get_field($N)' 'field$N' api.py --in process --apply

# Find all pattern matches
emend search 'print($X)' src/ --output location

# Multi-rule batch operations
emend batch rules.json --apply
```

### Symbol Management Examples

```bash
# Find all references to a symbol
emend refs models.py::User --output json
emend refs models.py::User --writes-only    # Only write references
emend refs models.py::User --calls-only     # Only function calls

# Rename a symbol project-wide
emend rename models.py::User --to Account --apply

# Move a symbol to another file (updates imports)
emend move utils.py::parse_date helpers/dates.py --apply

# Copy a symbol to another file
emend copy-to workflow.py::Builder._build.helper tasks.py --dedent --apply

# List symbols using search
emend search workflow.py --depth 3
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
