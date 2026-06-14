---
name: python-refactor
description: Python code refactoring using the emend tool. Use this skill for mechanical Python refactoring operations like renaming symbols (classes, functions, variables), moving functions/classes between modules, and organizing imports. emend automatically updates all references across the codebase using tree-sitter (via the Rust emend_core engine). Trigger on requests to rename, move, or reorganize Python code elements.
allowed-tools: Bash(emend *)
---

# Python Refactoring with emend

## Refactoring strategy

When performing large refactors in Python projects (especially involving large files), manual updates are error-prone and eat up context window size. To start off with, run an Explore agent to find relevant parts of the code, then construct a plan using commands below. For tasks involving moving large chunks of code, you should execute the following steps:
1. Use `emend` commands to move lines of code verbatim between files.
2. Have a task examine the diff and commit the change with `--no-verify` to skip pre-commit hooks. (It is not expected that the code will work at this point.) Make commits atomic and include the verbatim emend/sed/whatever command used in the commit message.
3. Repeat (1) and (2) until all large structural moves have been made.
4. Add an _empty_ commit with a message like "Code moves complete, now fixing references".
5. Use `emend` commands to fix up references, rename symbols, and perform other cleanups. After each command execution, have a task review and commit the changes with `--no-verify`.
6. Then (and only then) have a task directly make additional changes required to make the code actually working. You should start off with a task to get `pre-commit run --all-files` passing. Then commit (without `--no-verify` this time).
7. Finally, iterate on the semantics of the code (running tests, sandbox deploys, user input, etc.) until everything is working.

Make liberal use of Task, Explore and Agents to keep your context window from filling up.

## Two Complementary Systems

emend is built on two complementary systems:

1. **Structured Edits** — Precise changes to symbol metadata using extended selectors like `file.py::func[params][ctx]`
2. **Pattern Transforms** — Code-pattern search and replace with capture variables like `print($X)` → `logger.info($X)`

## Workflow

1. **Preview changes**: Commands default to dry-run (show diff without applying)
2. **Review the diff output**
3. **Apply changes**: Re-run with `--apply`
4. **Format code**: Run formatters (black/ruff/isort) or pre-commit hooks - emend may not preserve exact formatting
5. **Verify**: Run tests/type checks, review `git diff`

---

## Selector Syntax

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

### Pseudo-class Selectors (for parameter kind)
```bash
file.py::func[params]:KEYWORD_ONLY       # Keyword-only parameter slot
file.py::func[params]:POSITIONAL_ONLY    # Positional-only parameter slot
```

### Line Selectors
```bash
file.py:42                      # Single line
file.py:42-100                  # Line range
```

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

---

## Structured Edit Commands

### lookup — Read component value, query symbols, or display source
```bash
# Extract component value
emend lookup SELECTOR [--json]

# Query symbols with filters
emend lookup FILEPATH [--kind KIND] [--name PATTERN] [--has-decorator DEC] [--json] [--count]

# Display source code
emend lookup SELECTOR [--dedent] [--metadata]
```

Examples:
```bash
# Extract component values
emend lookup file.py::func[returns]                    # "str"
emend lookup file.py::func[params] --json              # ["self", "x: int"]
emend lookup file.py::MyClass[decorators]              # "@dataclass"
emend lookup file.py::MyClass[bases]                   # "BaseClass, Protocol"

# Query symbols
emend lookup file.py --kind function
emend lookup file.py --kind method --has-decorator "@pytest.fixture"
emend lookup file.py --name "test_*" --count

# Display source
emend lookup file.py::Builder._build.helper --dedent
emend lookup file.py:42-100
emend lookup file.py::func --metadata   # Shows line numbers, offsets, decorators, parameters
```

### edit — Replace or remove component value
```bash
emend edit SELECTOR [VALUE] [--rm] [--apply]
```

Examples:
```bash
# Replace component values
emend edit file.py::func[returns] "str | None" --apply
emend edit file.py::func[params][0] "self" --apply
emend edit file.py::func[decorators][0] "@lru_cache(maxsize=128)" --apply
emend edit file.py::MyClass[bases] "BaseClass, Protocol" --apply

# Remove components or symbols
emend edit file.py::func --rm --apply                      # Remove entire symbol
emend edit file.py::func[params][ctx] --rm --apply         # Remove parameter
emend edit file.py::func[decorators][deprecated] --rm --apply  # Remove decorator
emend edit file.py::func[returns] --rm --apply             # Remove return annotation
```

### add — Insert into list component
```bash
emend add SELECTOR VALUE [--at POS] [--before NAME] [--after NAME] [--apply]
```

Position options:
- `--at 0` — Insert at beginning
- No position flag — Append to end (default)
- `--before NAME` — Insert before named item
- `--after NAME` — Insert after named item

Examples:
```bash
# Add parameter
emend add file.py::func[params] "ctx: Context" --apply
emend add file.py::func[params] "ctx: Context" --after self --apply

# Add keyword-only parameter using pseudo-class syntax
emend add "file.py::func[params]:KEYWORD_ONLY" "debug: bool = False" --apply

# Add decorator
emend add file.py::func[decorators] "@cache" --at 0 --apply

# Add base class
emend add file.py::MyClass[bases] "Protocol" --apply
```

---

## Pattern Transform Commands

### Pattern Syntax

Patterns support metavariables for capturing:

| Syntax | Matches | Example |
|--------|---------|---------|
| `$X` | Single expression/identifier | `print($MSG)` |
| `$_` | Anonymous (don't capture) | `func($_, $ARG)` |
| `$...ARGS` | Zero or more arguments | `func($...ARGS)` |
| `$X:type` | Typed metavariable | `$N:int`, `$F:identifier` |

Supported pattern node types: literals, function calls, attribute access, comparisons, binary/boolean/unary operations, subscripts, ternary, await, collections (tuple/list/dict), lambda, return, assert, raise.

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

### find — Find pattern matches
```bash
emend find PATTERN PATH [--json] [--count] [--in SCOPE] [--inside STRUCT] [--not-inside STRUCT]
```

Examples:
```bash
emend find 'print($X)' src/
emend find 'assertEqual($A, $B)' tests/ --count
emend find 'await $X' src/ --inside 'async def'
emend find 'print($X)' src/ --not-inside 'if'
emend find 'old_name' file.py --in my_func
```

### replace — Pattern-based find and replace
```bash
emend replace PATTERN REPLACEMENT PATH [--apply] [--in SCOPE] [--inside STRUCT] [--not-inside STRUCT]
```

Examples:
```bash
emend replace 'print($X)' 'logger.info($X)' file.py --apply
emend replace 'assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
emend replace 'old_name' 'new_name' file.py --in my_func --apply
emend replace 'self\.' 'ctx.' file.py --in process --apply
```

---

## Symbol Management Commands

### list-symbols — List symbols in a module
```bash
emend list-symbols FILE [--tree-depth N] [--flat] [--selector PATH] [--project DIR]
```

Examples:
```bash
emend list-symbols models.py
emend list-symbols models.py --tree-depth 3
emend list-symbols models.py --flat
emend list-symbols models.py --selector Calculator
```

### find-references — Semantic reference search
```bash
emend find-references SELECTOR [--exclude-definition] [--exclude-imports] [--json]
```

Uses LibCST semantic analysis (not text matching).

```bash
emend find-references file.py::MyClass
emend find-references file.py::func --exclude-imports --json
```

### rename — Rename symbol project-wide
```bash
emend rename SELECTOR --to NEW_NAME [--apply] [--docs] [--no-hierarchy] [--unsure]
```

Examples:
```bash
emend rename models.py::User --to Account --apply
emend rename file.py::func --to new_func --docs --apply
```

### move — Move symbol to another file
```bash
emend move SELECTOR DESTINATION [--dedent] [--no-update-imports] [--apply]
```

Copies symbol, removes from source, updates imports across the project.

```bash
emend move utils.py::parse_date helpers/dates.py --apply
emend move workflow.py::Builder._build.helper utils.py --dedent --apply
emend move utils.py::func dest.py --no-update-imports --apply
```

---

## Other Commands

### copy-to — Copy symbol to another file (without removing from source)
```bash
emend copy-to SELECTOR DESTINATION [--dedent] [--append] [--apply]
```

```bash
emend copy-to workflow.py::Builder._build.helper tasks.py --dedent --apply
emend copy-to workflow.py::Builder._build.validate tasks.py --dedent --append --apply
```

### move-module / rename-module
```bash
emend move-module SOURCE DESTINATION [--apply]
emend rename-module FILE NEW_NAME [--apply]
```

### batch — Run multiple operations from JSON
```bash
emend batch CONFIG [--continue-on-error] [--apply]
```

---

## Common Refactoring Recipes

### Extract nested function and fix references

```bash
# Copy symbol to new file
emend copy-to workflow.py::Builder._build.process tasks/process.py --dedent --apply

# Fix self references using pattern replace
emend replace 'self\.' 'ctx.' tasks/process.py --in process --apply

# Add context parameter
emend add tasks/process.py::process[params] "ctx: Context" --at 0 --apply

# Transform decorators using edit
emend edit tasks/process.py::process[decorators] "@task" --apply

# Remove from source
emend edit workflow.py::Builder._build.process --rm --apply
```

### Migrate API calls across project

```bash
emend replace 'old_api($...ARGS)' 'new_api($...ARGS)' src/ --apply
emend replace 'from legacy import old_api' 'from modern import new_api' src/ --apply
```

### Convert unittest to pytest assertions

```bash
emend replace 'self.assertEqual($A, $B)' 'assert $A == $B' tests/ --apply
emend replace 'self.assertTrue($X)' 'assert $X' tests/ --apply
emend replace 'self.assertIsNone($X)' 'assert $X is None' tests/ --apply
```

### Batch query + transform

```bash
# Add decorator to all methods in a class
emend lookup file.py --kind method --in-class MyClass --paths-only | \
  xargs -I{} emend add {}[decorators] "@override" --at 0 --apply

# Add parameter to all test functions
emend lookup tests/ --kind function --name "test_*" --paths-only | \
  xargs -I{} emend add {}[params] "mock_db: MockDB" --apply
```

### Copy imports between files

```bash
emend lookup source.py::[imports] | while read import_line; do
  emend add dest.py::[imports] "$import_line" --apply
done
```

### Automated multi-symbol move with import setup and commit

This bash function automates moving multiple symbols from one file to another, setting up imports, running formatters, and creating a detailed commit:

```bash
do-moves() (
  set -e
  SOURCE_FILE="$1"
  DEST_FILE="$2"
  MOVES="$3"
  COMMIT_MSG="$4"
  PROMPT="${5:-}"

  emend lookup "$SOURCE_FILE"::[imports] > "$DEST_FILE"
  for ITEM in $MOVES; do
    emend move "$SOURCE_FILE"::"$ITEM" "$DEST_FILE" --apply
  done
  emend add "$SOURCE_FILE"::[imports] "from $(echo "$DEST_FILE" | sed 's|/|.|g; s|\.py$||') import ($(echo $MOVES | tr ' ' ','))" --apply
  git add "$SOURCE_FILE" "$DEST_FILE"

  if [ -n "$PROMPT" ]; then
    claude --model opus -p "$PROMPT" --allowedTools edit
  fi

  pre-commit run --all-files || pre-commit run --all-files  # should succeed on second try
  git add "$SOURCE_FILE" "$DEST_FILE"

  git commit -m \
"$COMMIT_MSG

Performed by:
$(declare -f do-moves)

Invocation:
do-moves $(printf '%q ' "$SOURCE_FILE" "$DEST_FILE" "$MOVES" "$COMMIT_MSG" "$PROMPT")

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
)
```

Usage examples:

```bash
# Basic usage: move multiple symbols to a new file
do-moves \
  routes/coding.py \
  routes/coding_types.py \
  "User Assignment Product OriginalCoderData EncounterQueryOpportunityImpact" \
  "refactor: extract coding types to coding_types.py"

# With Claude assistance for cleanup after move
do-moves \
  routes/coding.py \
  routes/encounter_router.py \
  "get_encounter_data statuses mark_complete reopen" \
  "refactor: move /encounter routes into their own router" \
  "Please edit @routes/encounter_router.py and @routes/coding.py so coding imports a fastapi router and includes it with no prefix"
```

=======
## Troubleshooting

### "Symbol not found"
- Check symbol is at module level (not nested in function/class) for cross-project commands
- Verify exact spelling including case
- Use `emend list-symbols <file>` to see available symbols
- For nested symbols, use `emend list-symbols <file> --tree-depth 3` and the selector syntax

### Import issues after refactoring
- Use a formatter like `black` or `ruff format`
- Check for circular import issues introduced by move

---

## Limitations

- Cannot update dynamic references (e.g., `importlib.import_module("mod.name")`)
- Cannot update string-based references in configs
- May miss references if project root is too narrow
- May not preserve exact formatting; run formatter (black/ruff) or pre-commit hooks after refactoring
- Cross-project commands (rename, move) only work on module-level symbols; use `copy-to`, `edit --rm`, and pattern transforms for nested functions
