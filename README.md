# emend

A Python refactoring CLI built on [tree-sitter](https://tree-sitter.github.io/tree-sitter/) with a Rust backend. The name means "to make corrections to a text" — which is what it does, but with AST-aware precision instead of find-and-replace.

Two complementary systems: **structured edits** use selectors like `file.py::func[params][0]` for precise changes to symbol metadata, and **pattern transforms** use capture variables like `print($X)` → `logger.info($X)` for code-pattern search and replace.

## Installation

### Using uv with free-threaded Python (recommended for best performance)

```bash
uv tool install --python 3.13t emend
```

Python 3.13+ ships a **free-threaded** variant (`3.13t`, `3.14t`) that removes the
GIL. emend's Rust core (`emend_core`) is already GIL-free (built with
`#[pymodule(gil_used = false)]`), so on free-threaded Python it can run parallel
file scans with no lock contention — meaning `find`, `lint`, `analyze refs`, and
`edit rename` across large codebases are significantly faster.

We recommend **3.13t** for the free-threaded interpreter. The 3.14t variant also
works for core emend commands, but the optional MCP server (`emend mcp`) depends on
Pydantic which does not yet support Python 3.14t (as of February 28, 2026):

```bash
uv tool install --python 3.13t emend   # free-threaded 3.13 (recommended)
uv tool install --python 3.14t emend   # free-threaded 3.14 (no MCP server support)
```

### Using uv (standard Python)

```bash
uv tool install emend
```

### Using pip

```bash
pip install emend
```

### MCP server

To use emend as an [MCP](https://modelcontextprotocol.io/) server for LLM-based
clients, install the optional `mcp` extra:

```bash
pip install emend[mcp]
```

Then start the server:

```bash
emend mcp                              # stdio transport (default)
emend mcp --transport sse --port 8080  # SSE transport
```

**Note:** The MCP server requires Pydantic, which does not support Python 3.14t as
of February 28, 2026. Use Python 3.10–3.13 (including 3.13t) for MCP server mode.

#### Adding emend to Claude Code

The quickest way is the `claude mcp add` CLI:

```bash
# Add for the current project (default scope: local)
claude mcp add --transport stdio emend -- emend mcp

# Or share with your team via .mcp.json (project scope)
claude mcp add --transport stdio --scope project emend -- emend mcp
```

If you installed with `uv tool install`, make sure the `emend` binary is on your
PATH, or use the full path:

```bash
claude mcp add --transport stdio emend -- uvx emend mcp
```

You can also add it by editing configuration JSON directly. For a **personal**
setup, run:

```bash
claude mcp add-json emend '{"type":"stdio","command":"emend","args":["mcp"]}'
```

For a **team-shared** setup, add a `.mcp.json` file to your project root
(and commit it to version control):

```json
{
  "mcpServers": {
    "emend": {
      "type": "stdio",
      "command": "emend",
      "args": ["mcp"]
    }
  }
}
```

Verify it's connected:

```bash
claude mcp list          # from the terminal
```

Or inside Claude Code, type `/mcp` to see all connected servers and their status.

See the [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp)
for details on scopes, environment variables, and managed configurations.

Run `emend --help` to verify. Full documentation at [lucaswiman.github.io/emend](https://lucaswiman.github.io/emend/).

### Vim / Neovim plugin

emend ships with a Vim/Neovim plugin for interactive code search. Install with
[vim-plug](https://github.com/junegunn/vim-plug):

```vim
Plug 'lucaswiman/emend', { 'rtp': 'vim' }
```

Then use `:Emend` to open the search prompt, or `:Emend parse` to search
directly. The plugin communicates with `emend tool editor-server` via JSON-RPC over
stdio pipes — the server stays warm for sub-5ms lookups.

For local development, point to your checkout:

```vim
Plug '~/src/emend', { 'rtp': 'vim' }
let g:emend_command = '~/src/emend/.venv/bin/emend'
```

See [vim/README.md](vim/README.md) for full documentation.

### Indexing (recommended for large codebases)

After installing, run `emend tool index` in your project root to pre-build caches:

```bash
emend tool index                  # index current directory
emend tool index src/ --jobs 8    # index specific directory with 8 workers
```

This parses every Python file and builds a qualified-name index, so subsequent
cross-project operations (`analyze refs`, `edit rename`, `analyze graph`, `analyze deadcode`) are
significantly faster. The cache is stored in `.emend/cache/parse.db` (automatically
gitignored and dockerignored) and is keyed by file content hash, so it
self-invalidates when files change. Git worktrees automatically share a single
cache with the main repo. Re-run after large merges or branch switches.

When using the MCP server (`emend mcp`), indexing happens automatically in the
background on startup.

## MCP Server

The MCP server now uses a smaller discriminated tool surface:

1. **`search`** — Code search, symbol lookup, and summary listing
2. **`transform`** — Pattern replace plus selector-based edits, adds, removes, copies, moves, and renames
3. **`references`** — References, callers, and callees
4. **`analyze`** — Graph, deadcode, impact, semantic context, and flow analysis
5. **`check`** — Unified project rules from `.emend/rules.yaml`
6. **`mappings`** — Cross-repo identifier and module mappings
7. **`grammar_and_cookbook`** — Syntax reference and cookbook text

See the [grammar_and_cookbook.rst](src/emend/grammar_and_cookbook.rst) reference for full command documentation.

## Usage

```bash
emend <command> [options]
```

### Workflow

All mutating commands default to dry-run, showing a diff of proposed changes. Re-run with `--apply` to write them. You'll probably want to run a formatter (black/ruff/isort) afterward, since emend doesn't try to preserve exact formatting.

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
```

## Commands

The public CLI is organized around a small set of top-level commands:

- **`find`** — Search for patterns, selectors, symbols, or summaries.
- **`edit`** — Code changes and refactors. Subcommands: `set`, `rm`, `delete`, `add`, `replace`, `cp`, `rename`, `mv`, `batch`, `saturate`.
- **`analyze`** — Read-only analysis. Subcommands: `refs`, `graph`, `deadcode`, `impact`, `types`, `trace`, `facts`, `cfg`, `dsl`.
- **`tool`** — Operational and debugging commands. Subcommands: `index`, `editor-search`, `editor-server`, `query`.
- **`check`** — Unified project rules from `.emend/rules.yaml`.
- **`lint`** / **`policy`** — Focused rule runners kept for compatibility and targeted workflows.
- **`map`** — Identifier and module mappings.
- **`mcp`** — Start the MCP server.

Hidden compatibility aliases still work. For example, `emend rm ...` maps to `emend edit rm ...`, and old read commands like `search`, `grep`, `show`, `get`, and `lookup` route to `emend find`.

## Examples

### Search & Read Examples

```bash
# Search by pattern (pattern mode)
emend find 'print($X)' src/
emend find 'assertEqual($A, $B)' tests/ --output count

# Search by symbol (lookup mode)
emend find file.py::func
emend find src/ --kind function --name test_*
emend find file.py --output json

# Extract function parameters
emend find api.py::handler[params]
emend find 'api.py::*[params]'  # Wildcard: all function params in file

# Get return types
emend find 'src/**/*.py::*[returns]' --output metadata

# List symbols in a module
emend find file.py                          # Tree view
emend find file.py --output summary::flat   # Flat list
emend find file.py --depth 2                # Limit nesting depth
```

### Edit Examples

```bash
# Update return type
emend edit set api.py::handler[returns] "Response" --apply

# Add parameter with default value
emend edit add api.py::handler[params] "timeout: int = 30" --apply

# Add keyword-only parameter
emend edit add "api.py::handler[params]:KEYWORD_ONLY" "debug: bool" --apply

# Insert parameter before specific param
emend edit add api.py::handler[params] "ctx: Context" --before user_id --apply

# Remove a specific parameter
emend edit rm api.py::handler[params][deprecated_arg] --apply

# Edit multiple symbols at once (wildcards)
emend edit set 'file.py::*[decorators]' "@dataclass" --apply
```

### Pattern Transform Examples

```bash
# Simple find and replace (dry-run by default)
emend edit replace 'print($X)' 'logger.info($X)' file.py

# Replace within a specific scope
emend edit replace 'old_var' 'new_var' api.py --where process --apply

# Replace with pattern capture
emend edit replace 'get_field($N)' 'field$N' api.py --where process --apply

# String content interpolation: ${X.content} strips quotes from a captured string literal
emend edit replace 'Union["$X", $Y]' '$X | $Y' src/ --apply

# Find all pattern matches
emend find 'print($X)' src/ --output location

# Multi-rule batch operations
emend edit batch rules.json --apply
```

### Symbol Management Examples

```bash
# Find all references to a symbol
emend analyze refs models.py::User --json
emend analyze refs models.py::User --writes-only    # Only write references
emend analyze refs models.py::User --calls-only     # Only function calls

# Rename a symbol project-wide
emend edit rename models.py::User --to Account --apply

# Move a symbol to another file (updates imports)
emend edit mv utils.py::parse_date helpers/dates.py --apply

# Copy a symbol to another file
emend edit cp workflow.py::Builder._build.helper tasks.py --dedent --apply

# Remove a symbol or component
emend edit rm file.py::deprecated_func --apply

# List symbols using find
emend find workflow.py --depth 3
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

# Structural constraints (via --where)
emend find 'print($X)' src/ --within 'async def'
emend find 'await $X' src/ --where 'not if __debug__'

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
TYPE_CONSTRAINT: /:!?(?:expr|stmt|identifier|int|str|float|call|attr|any)/
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

## Linting

emend's canonical rules file is `.emend/rules.yaml`. `emend check` is the unified entry point, while `emend lint` and `emend policy` remain available for focused workflows and compatibility.

```yaml
# .emend/rules.yaml
macros:
  print_call: "print($...ARGS)"

rules:
  no-print:
    match: "{print_call}"
    not-within: "def test_*"
    message: "Use logger instead of print"
    fix: "logger.info($...ARGS)"

  no-open-without-encoding:
    match: "open($PATH)"
    message: "Specify encoding when calling open()"
    fix: "open($PATH, encoding='utf-8')"
```

```bash
# Run all configured checks
emend check src/

# Auto-fix match rules that have a fix
emend check src/ --fix

# Focus on match-style lint rules only
emend lint src/ --rule no-print
```

Suppress violations inline with `# noqa` comments:

```python
print("keep this")  # noqa
print("keep this")  # noqa: emend:no-print
print("keep this")  # noqa: E501, emend:no-print  # mixed with other linters
```

### Dead code detection

Configure dead-code rules for `emend check`/`emend lint` in `.emend/rules.yaml`. The standalone `emend analyze deadcode` command uses the equivalent CLI flags shown below:

```yaml
# .emend/rules.yaml
deadcode: true

# Or with options:
deadcode:
  enabled: true
  kind: function                          # Only functions (or "class")
  include-private: true                   # Default: include private symbols/methods
  exclude-test-references: true           # Default: ignore refs from tests
  unused-modules: true                    # Default: report unimported modules
  strings-count-as-references: true       # String literals count as refs
  message: "Symbol appears to be unused"
```

```bash
# Run as part of unified checks
emend check src/

# Or use the standalone command
emend analyze deadcode src/
emend analyze deadcode src/ --include-test-references --json
emend analyze deadcode src/ --exclude-private --no-unused-modules
```

Suppress false positives inline:

```python
def my_entry_point():  # noqa: emend:deadcode
    ...
```

### Mapping Store

emend includes a built-in mapping store for cross-service identifier mappings and module-to-repo mappings. Stored in `.emend/mappings.yaml`.

```bash
# Identifier mappings — cross-service relationships
emend map add backend "UserService.create" gateway "POST /api/v1/users" \
    --source-kind function --target-kind endpoint
emend map search "UserService"

# Module mappings — map module prefixes to repos or local dirs
emend map add-module payments --repo org/payments-service
emend map add-module shared.utils --path /home/user/shared-utils
emend map resolve payments.models.Order
```

Module mappings that reference GitHub repos are automatically cloned (via `gh`) and checked out as git worktrees under `~/.cache/emend/repo-checkouts/`. Set `EMEND_CACHE_DIR` to relocate this cache.

### pre-commit integration

emend can run as a [pre-commit](https://pre-commit.com/) hook. Add to your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/lucaswiman/emend
    rev: v0.2.0  # replace with desired version tag
    hooks:
      - id: emend-lint
```

This runs `emend lint` on staged Python files using your `.emend/rules.yaml` config.

To auto-fix violations, add `args: ["--fix"]` to the hook configuration.

## Development

### Installing from Source

Clone the repository and install for development:

```bash
git clone https://github.com/lucaswiman/emend
cd emend

# Using make (creates a free-threaded venv, compiles the Rust extension, installs dev deps)
make venv

# Or manually (requires maturin and a Rust toolchain):
uv venv .venv --python 3.13t
uv pip install maturin
.venv/bin/maturin develop -E dev
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
│   ├── cli.py                # CLI assembly and compatibility layer
│   ├── cli_find.py           # `find`
│   ├── cli_edit.py           # `edit` subcommands
│   ├── cli_analysis.py       # `analyze` subcommands
│   ├── cli_tooling.py        # `tool` and `mcp`
│   ├── cli_checks.py         # `check`, `lint`, `policy`
│   ├── cli_map.py            # `map`
│   ├── transform.py          # Core search/edit/analysis engine
│   ├── pattern.py            # Pattern parsing and compilation
│   ├── query.py              # Symbol querying with filters
│   ├── ast_commands.py       # AST-based command implementations
│   ├── ast_utils.py          # AST traversal utilities
│   ├── component_selector.py # Extended selector parsing
│   ├── lint.py               # Pattern-based linter engine
│   ├── mcp_server.py         # MCP server (optional, requires emend[mcp])
│   └── grammars/
│       ├── selector.lark     # Extended selector grammar
│       └── pattern.lark      # Pattern grammar
├── rust/                     # emend_core Rust extension (bundled in wheel)
│   ├── src/lib.rs            # PyO3 bindings, GIL-free module definition
│   └── Cargo.toml
├── vim/                      # Vim/Neovim plugin (JSON-RPC over stdio)
│   ├── plugin/emend.vim      # Commands (:Emend, :EmendSearch, etc.)
│   ├── autoload/emend.vim    # RPC client, server lifecycle
│   ├── autoload/emend/ui.vim # Split-pane search UI
│   └── doc/emend.txt         # Vim help (:help emend)
├── tests/test_emend/         # Test suite
├── Makefile
└── pyproject.toml            # maturin build (bundles Rust + Python in one wheel)
```

## License

MPL 2.0
