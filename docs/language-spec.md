# emend Query & Transform Language Specification

## Design Goals

1. **Code patterns are code** -- a valid Python expression is a valid pattern
2. **Metavariables for capture** -- `$x` captures, `$_` wildcards, `$...xs` spreads
3. **Declarative transforms** -- `pattern => replacement` with conditions
4. **Cross-file awareness** -- scoped to project, with import tracking
5. **Composable constraints** -- `where`, `within`, `not within`, `contains`
6. **Language-agnostic core** -- tree-sitter backend, language via config

## Quick Examples

```
# Find all print calls
`print($msg)`

# Replace old API with new
`requests.get($url)` => `httpx.get($url)`

# With conditions
`$fn($...args)` => `$fn($...args, timeout=30)` where {
    $fn <: imported_from("requests"),
    not $...args <: contains `timeout=$_`
}

# Structural navigation
`class $name($...bases): $...body` where {
    $bases <: contains `BaseModel`
}

# Multi-statement sequential transforms
sequential {
    `from old_module import $name` => `from new_module import $name`,
    `old_module.$name` => `new_module.$name`
}
```

## 1. Patterns

### 1.1 Code Snippets

Any target-language code snippet enclosed in backticks is a pattern.
The snippet is parsed by tree-sitter and matched structurally (not textually).

```
`print("hello")`           # matches exactly print("hello")
`x = 1`                    # matches x = 1
`def foo(): pass`          # matches def foo(): pass
```

When a pattern contains no metavariables, it is a **literal pattern** --
it matches code that is structurally identical (ignoring whitespace and
formatting).

### 1.2 Metavariables

Metavariables capture subtrees of the target AST.

| Syntax | Meaning |
|--------|---------|
| `$name` | Captures any single AST node, binds to `$name` |
| `$_` | Captures any single AST node, does not bind (anonymous) |
| `$...name` | Captures zero or more nodes (spread), binds to `$...name` |
| `$...` | Captures zero or more nodes, does not bind |

Metavariable names follow the convention `$lowercase_snake_case`.
Metavariables are **file-scoped** by default: the same `$x` used twice
in a pattern must bind to structurally identical subtrees.

```
# $x must be the same on both sides of ==
`$x == $x`                 # matches a == a but NOT a == b

# $...args matches zero or more arguments
`print($...args)`          # matches print(), print(1), print(1, 2)

# $_ matches anything without capturing
`isinstance($_, str)`      # matches isinstance(x, str), isinstance(foo(), str)
```

### 1.3 Type Constraints

Constrain what a metavariable can match:

| Constraint | Matches |
|------------|---------|
| `$x <: int_literal` | Integer literal |
| `$x <: string_literal` | String literal |
| `$x <: float_literal` | Float literal |
| `$x <: identifier` | Bare name |
| `$x <: call` | Function call |
| `$x <: attribute` | Attribute access (`a.b`) |
| `$x <: not int_literal` | Anything except integer |

These constraints use tree-sitter node types.  The `<:` operator
means "matches" (borrowed from GritQL).

**Oracle type constraints** query an external type inference engine:

| Constraint | Requires |
|------------|----------|
| `$x <: type("Connection")` | Type engine (pyrefly/pyright/ty) |
| `$fn <: returns("str")` | Type engine |

### 1.4 Glob Identifiers

Names with wildcards match multiple identifiers:

```
`test_*`                   # matches test_foo, test_bar, test_123
`*Error`                   # matches ValueError, TypeError
`My*Class`                 # matches MyBaseClass, MySubClass
```

These compile to regex patterns on the identifier text.

## 2. Rewrites

### 2.1 The `=>` Operator

The rewrite operator transforms matched code:

```
`old_func($x)` => `new_func($x)`
```

The left side is the **match pattern**; the right side is the **replacement
template**.  Metavariables captured on the left are substituted on the right.

```
# Rename function
`calculate($a, $b)` => `compute($a, $b)`

# Wrap in function call
`$expr` => `str($expr)` where { $expr <: int_literal }

# Delete matched code
`print($...args)` => .

# Swap arguments
`assertEqual($a, $b)` => `assertEqual($b, $a)`
```

The special pattern `.` (dot) on the right side means "delete the match".

### 2.2 String Interpolation

Access the text content of a captured string literal:

```
# Strip quotes from captured string
`Union["$x"]` => `${x.text} | None`
```

### 2.3 Dry-Run by Default

All rewrites are dry-run by default.  Use `--apply` to write changes.

## 3. Conditions

### 3.1 `where` Clauses

Conditions refine when a pattern matches:

```
`$fn($...args)` where {
    $fn <: `requests.get`
}
```

Multiple conditions are AND-ed:

```
`$fn($...args)` where {
    $fn <: imported_from("requests"),
    $...args <: not contains `timeout=$_`,
    $fn <: not `requests.head`
}
```

### 3.2 Match Operator (`<:`)

The `<:` operator tests whether a value matches a pattern or predicate:

```
$x <: `foo`                # $x is literally "foo"
$x <: identifier           # $x is an identifier node
$x <: r"test_.*"           # $x matches regex
$x <: int_literal          # $x is an integer literal
$x <: not `None`           # $x is not None
```

### 3.3 Structural Predicates

| Predicate | Meaning | Example |
|-----------|---------|---------|
| `contains PATTERN` | Has a descendant matching PATTERN | `$body <: contains \`print($...)\`` |
| `within PATTERN` | Is nested inside a match of PATTERN | `$x <: within \`def test_$_($...): $...\`` |
| `not within PATTERN` | Is NOT inside PATTERN | `$x <: not within \`class $_: $...\`` |
| `imported_from(MOD)` | Name is imported from module | `$fn <: imported_from("json")` |
| `precedes PATTERN` | Immediately before | `$stmt <: precedes \`return $_\`` |
| `follows PATTERN` | Immediately after | `$stmt <: follows \`if $_:\`` |

### 3.4 `or` and `and`

```
# Match either old or new API
`$fn($x)` where {
    $fn <: or { `old_api`, `legacy_api` }
}

# Both conditions must hold
`$fn($x)` where {
    $fn <: `requests.get` and $x <: string_literal
}
```

### 3.5 `maybe`

Optionally apply a rewrite -- if the pattern matches, transform; if not, succeed anyway:

```
maybe `from old import $name` => `from new import $name`
```

### 3.6 `if` / `else`

```
if ($x <: int_literal) {
    `$x` => `str($x)`
} else {
    `$x` => `repr($x)`
}
```

## 4. Scope & Navigation

### 4.1 Selectors

Selectors address specific code locations using a path syntax:

```
file.py::ClassName.method_name[params]
```

Structure: `[file_path]::[symbol.path][component][accessor]`

| Part | Examples |
|------|----------|
| File path | `file.py`, `src/**/*.py`, `**` (all files) |
| Symbol path | `func`, `Class.method`, `Class.method.nested` |
| Component | `[params]`, `[returns]`, `[decorators]`, `[bases]`, `[body]` |
| Accessor | `[0]` (index), `[name]` (by name), `[-1]` (last) |

```
# All parameters of a function
file.py::process[params]

# Return type annotation
file.py::process[returns]

# First decorator
file.py::handler[decorators][0]

# All methods in a class
file.py::MyClass.*[body]

# Wildcard: all functions matching a pattern
file.py::test_*[params]
```

### 4.2 `within` (Scope Constraint)

Restrict matches to those inside a certain structure:

```
# Only match print() inside test functions
`print($...args)` where {
    within `def test_$_($...): $...`
}

# Short form for keyword scopes
`print($...args)` where { within def }
`$x` where { within class }
```

Keyword scope shortcuts: `def`, `class`, `for`, `while`, `try`, `with`, `if`.

### 4.3 `not within`

```
# Match outside of class definitions
`global $x` where { not within class }
```

### 4.4 `scope_local`

Restrict to locally-defined names (exclude imports):

```
`config` where { scope_local }
```

### 4.5 `imported_from`

Verify that a name is imported from a specific module:

```
`loads($data)` where { $_ <: imported_from("json") }
```

## 5. Cross-File Operations

### 5.1 `sequential`

Apply multiple transforms in order, each seeing the result of the previous:

```
sequential {
    `from old_module import $name` => `from new_module import $name`,
    `old_module.$attr` => `new_module.$attr`
}
```

### 5.2 `multifile`

Gather information from one file, apply across all:

```
multifile {
    # Step 1: find the name being moved
    file($body) where {
        $body <: contains `class $target($...bases): $...`
    },
    # Step 2: update imports in all other files
    file($body) where {
        $body <: contains `from old import $target` => `from new import $target`
    }
}
```

### 5.3 `bubble`

Isolate metavariable scope to prevent cross-file leaking:

```
sequential {
    bubble file($body) where {
        $body <: contains `console.log($msg)` => `console.warn($msg)`
    },
    bubble file($body) where {
        $body <: contains `console.warn($msg)` => `console.info($msg)`
    }
}
```

The `bubble($var1, $var2)` form exports specific variables:

```
multifile {
    bubble($target) file($body) where {
        $body <: contains `class $target: $...`
    },
    bubble($target) file($body) where {
        $body <: contains `$target()` => `new_$target()`
    }
}
```

## 6. Symbol Operations

These operations work on the selector system (symbol-level, not pattern-level).

### 6.1 `edit`

Modify a symbol component:

```
# Change return type
edit file.py::func[returns] "int"

# Remove a parameter
edit file.py::func[params][old_param] --rm

# Change a decorator
edit file.py::handler[decorators][0] "@app.post('/new')"
```

### 6.2 `add`

Insert into a list component:

```
# Add a parameter
add file.py::func[params] "timeout: int = 30"

# Add a decorator
add file.py::func[decorators] "@cache"

# Add a base class
add file.py::MyClass[bases] "Serializable"

# Positional control
add file.py::func[params] "x: int" --after self
add file.py::func[params] "x: int" --before kwargs
add file.py::func[params] "x: int" --at 0
```

### 6.3 `rename`

Scope-aware rename across the project:

```
rename file.py::old_name --to new_name
rename file.py::Class.old_method --to new_method --docs
```

### 6.4 `move`

Move a symbol to another file, updating imports:

```
move file.py::MyClass dest.py
```

### 6.5 `copy`

Copy a symbol to another file:

```
copy file.py::helper_func utils.py
```

### 6.6 `refs`

Find all references to a symbol:

```
refs file.py::my_function
refs file.py::MyClass --writes-only
refs file.py::process --calls-only
```

## 7. Search & Query

### 7.1 Unified `search` Command

Auto-detects mode from the query:

```
# Pattern mode (has $metavar or contains pattern syntax)
search `print($x)`

# Symbol lookup mode (bare name or selector)
search file.py::MyClass

# Summary mode (bare file/directory)
search src/
```

### 7.2 Filters

```
search `$fn($...args)` --kind function
search file.py --name "test_*"
search src/ --returns str
search src/ --has-param ctx
search `$fn($x)` --imported-from json
search config --scope-local
```

### 7.3 Output Formats

| Format | Description |
|--------|-------------|
| `code` | Source code with file:line header (default) |
| `location` | `file.py:line` only |
| `selector` | `file.py::Symbol.path` |
| `summary` | Symbol tree with signatures |
| `json` | Structured JSON with captures |
| `count` | Number of matches |

## 8. Lint Rules

Define pattern-based lint rules in `.emend/patterns.yaml`:

```yaml
macros:
  DEBUG: "`print($...args)`"

rules:
  no-debug-print:
    find: "`print($...args)`"
    not-within: "`def test_$_($...): $...`"
    message: "Remove debug print statement"
    replace: "."

  no-bare-except:
    find: "`except:`"
    message: "Use explicit exception type"
    replace: "`except Exception:`"

  use-pathlib:
    find: "`os.path.join($...parts)`"
    where: "`$...parts` <: imported_from('os.path')"
    message: "Use pathlib instead of os.path.join"
    replace: "`Path($...parts)`"

deadcode:
  entry-point-decorators:
    - "@app.route"
    - "@click.command"
  entry-point-names:
    - "main"
  exclude-paths:
    - "tests/**/*.py"
```

## 9. Batch Operations

Apply multiple operations from a YAML file:

```yaml
operations:
  - rename:
      selector: "src/api.py::get_user"
      to: "fetch_user"

  - replace:
      find: "`requests.get($url)`"
      replacement: "`httpx.get($url)`"
      path: "src/"

  - add:
      selector: "src/models.py::User[bases]"
      value: "Auditable"

  - edit:
      selector: "src/views.py::index[returns]"
      value: "HttpResponse"
```

## 10. Dead Code Detection

```
deadcode src/
deadcode src/ --kind function --include-private
deadcode src/ --exclude-path "tests/**"
deadcode src/ --entry-point-decorator "@app.route"
deadcode src/ --no-strings --no-last-reference
```

## 11. Call Graph

```
graph src/api.py
graph src/api.py --format dot | dot -Tpng -o graph.png
graph src/api.py --format json
```

## Appendix A: Grammar (Formal)

```
program     := statement+
statement   := rewrite | search | sequential | multifile | condition

rewrite     := pattern "=>" (pattern | ".")
             | rewrite "where" "{" conditions "}"

search      := pattern
             | pattern "where" "{" conditions "}"

sequential  := "sequential" "{" statement ("," statement)* "}"
multifile   := "multifile" "{" file_stmt ("," file_stmt)* "}"
file_stmt   := ["bubble" ["(" metavar_list ")"]] "file" "(" "$" name ")" "where" "{" conditions "}"

conditions  := condition ("," condition)*
condition   := match_cond | rewrite | negation | disjunction | if_else | maybe

match_cond  := expr "<:" matcher
negation    := "not" condition
disjunction := condition "or" condition
maybe       := "maybe" statement
if_else     := "if" "(" condition ")" "{" statement+ "}" ["else" "{" statement+ "}"]

matcher     := pattern
             | node_type
             | predicate
             | "not" matcher
             | "or" "{" matcher ("," matcher)* "}"
             | regex

predicate   := "contains" pattern
             | "within" (pattern | keyword)
             | "not" "within" (pattern | keyword)
             | "imported_from" "(" string ")"
             | "scope_local"
             | "precedes" pattern
             | "follows" pattern
             | "type" "(" string ")"
             | "returns" "(" string ")"

pattern     := "`" code "`"
             | metavar
             | node_type "(" (field "=" pattern)* ")"

metavar     := "$" name
             | "$_"
             | "$..." name
             | "$..."

node_type   := identifier           # tree-sitter node kind
keyword     := "def" | "class" | "for" | "while" | "try" | "with" | "if"

regex       := "r\"" regex_body "\""
code        := <target language source with optional metavariables>
string      := "\"" chars "\""
```

## Appendix B: Migration from Current emend Syntax

| Current emend | New syntax |
|---------------|------------|
| `emend grep 'print($X)' src/` | `search \`print($x)\` src/` |
| `emend replace 'print($X)' 'log($X)' src/` | `\`print($x)\` => \`log($x)\`` |
| `emend grep '$X' --inside def` | `search \`$x\` where { within def }` |
| `emend grep '$X' --not-inside class` | `search \`$x\` where { not within class }` |
| `emend grep '$X' --where 'def test_*'` | `search \`$x\` where { within \`def test_$_($...): $...\` }` |
| `emend grep '$X' --imported-from json` | `search \`$x\` where { $x <: imported_from("json") }` |
| `emend grep '$X' --scope-local` | `search \`$x\` where { scope_local }` |
| `$X:int` (old constraint) | `$x` where `{ $x <: int_literal }` |
| `$X:type[Conn]` (old oracle) | `$x` where `{ $x <: type("Conn") }` |
| `emend edit file.py::func[returns] str` | `edit file.py::func[returns] "str"` |
| `emend add file.py::func[params] 'x: int'` | `add file.py::func[params] "x: int"` |

## Appendix C: Comparison with GritQL

| This language | GritQL | Notes |
|---------------|--------|-------|
| `` `code` `` | `` `code` `` | Same |
| `$name` | `$name` | Same |
| `$...name` | `$[...$name]` | Simpler spread syntax |
| `$_` | `$_` | Same |
| `=>` | `=>` | Same |
| `.` (delete) | `.` (delete) | Same |
| `where { }` | `where { }` | Same |
| `<:` | `<:` | Same |
| `contains` | `contains` | Same |
| `within` | `within` | Same |
| `sequential { }` | `sequential { }` | Same |
| `multifile { }` | `multifile { }` | Same |
| `bubble` | `bubble` | Same |
| `maybe` | `maybe` | Same |
| `imported_from()` | Not built-in | Extended |
| `scope_local` | Not in GritQL | Extended |
| `type()` / `returns()` | Not in GritQL | Extended (oracle) |
| Selector syntax | Not in GritQL | Extended (symbol navigation) |
| `edit` / `add` | Not in GritQL | Extended (component operations) |
| `rename` / `move` | Not in GritQL | Extended (scope-aware refactoring) |
| `refs` / `deadcode` | Not in GritQL | Extended (scope analysis) |
