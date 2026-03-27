# DSL Support for Embedded Languages

**Status: Provisional**

## Motivation

Real-world web applications embed multiple DSLs inside host-language code:
Python files contain SQL strings, Jinja templates, and HTML fragments;
TypeScript files mix JSX/TSX markup with CSS class references and GraphQL
queries.  Today emend treats these embedded strings as opaque — you cannot
navigate from a SQL column name to the SQLAlchemy model attribute that
defines it, or from a CSS class selector to the React component that applies
it.

Adding cross-language navigation between host code and embedded DSLs would
bring the same "jump to definition" and "find references" power that emend
already provides within a single language to the multi-language reality of
web development.

## Goals

1. **Parse embedded DSLs** inside host-language string literals and template
   expressions using tree-sitter injection grammars.
2. **Extract cross-language symbols** — table/column names from SQL,
   template variable references from Jinja, CSS class names from stylesheets,
   component names from JSX/TSX — and store them in the existing symbol index.
3. **Resolve cross-language links** between embedded DSL symbols and
   host-language definitions (ORM models, component exports, context
   variables, etc.) so that `search`, `refs`, and editor navigation work
   across the boundary.
4. **Support pattern matching** inside embedded DSL regions so that `find`,
   `replace`, and `lint` rules can target DSL-specific constructs.

## Non-Goals (for initial version)

- Full semantic analysis of embedded languages (type inference inside SQL
  subqueries, Jinja macro scoping, CSS specificity).
- Support for every possible DSL — focus on the high-value cases first.
- Modifying DSL content via `edit`/`add` commands (read-only navigation
  first).

## Target DSLs

### Tier 1 — High value, well-defined grammars

| DSL | Host Languages | Key Symbols | Navigation Targets |
|-----|---------------|-------------|-------------------|
| **SQL** | Python, TypeScript | table names, column names, function calls | SQLAlchemy/Django/Prisma model classes & fields |
| **JSX/TSX** | TypeScript, JavaScript | component tags, prop names | Component function/class definitions, prop type interfaces |
| **CSS/SCSS** | Standalone, embedded `<style>` | class selectors, id selectors, custom properties | JSX `className` usage, HTML `class` attributes |
| **HTML** | Python (Jinja/Django), standalone | element ids, `class` attrs, `data-*` attrs, template vars | CSS rules, JS `getElementById`/`querySelector`, Python context dicts |

### Tier 2 — Valuable but more complex

| DSL | Host Languages | Key Symbols | Navigation Targets |
|-----|---------------|-------------|-------------------|
| **Jinja2/Django templates** | HTML-like `.html`, `.jinja2` | `{{ var }}`, `{% block %}`, `{% macro %}`, filters | Python view context, template inheritance tree |
| **GraphQL** | TypeScript, Python | type names, field names, query/mutation names | Schema definitions, resolver functions |
| **Regular expressions** | Python, TypeScript | named groups | `match.group('name')` call sites |

## Architecture

### 1. Tree-sitter Injection Parsing

Tree-sitter already supports [language injections](https://tree-sitter.github.io/tree-sitter/syntax-highlighting#language-injection)
where a parent grammar can delegate parsing of certain nodes to a child
grammar.  For example, `tree-sitter-python` can mark string literals as
potential injection sites, and a `tree-sitter-sql` grammar can parse the
contents.

**Approach**: Add an injection layer between the existing file parser and the
scope resolver:

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐
│ Host parse   │ ──▶ │ Injection detect │ ──▶ │ DSL sub-parse     │
│ (Python CST) │     │ (heuristics +    │     │ (SQL/HTML/Jinja   │
│              │     │  annotations)    │     │  CST per region)  │
└─────────────┘     └──────────────────┘     └───────────────────┘
```

**Injection detection strategies** (configured per language pair):

- **Type-based**: Variable annotated as or assigned from a known ORM/query
  builder type (e.g., `text("SELECT ...")` from sqlalchemy, `gql(...)` from
  graphql-tag).
- **Call-based**: String argument to known functions (`cursor.execute(...)`,
  `render_template_string(...)`, `styled.div\`...\``).
- **File-extension**: `.sql`, `.html`, `.jinja2`, `.graphql` files parsed
  directly as their DSL.
- **Magic comment**: `# language=sql` (PyCharm convention), `/* language=css */`,
  enabling opt-in injection for ambiguous cases.
- **Tag-based**: Tagged template literals in JS/TS (`sql\`...\``, `css\`...\``,
  `html\`...\``).

### 2. DSL Symbol Extraction

Each DSL gets a `DslSymbolExtractor` implementation that walks the DSL's CST
and emits symbols with cross-language link hints:

```python
@dataclass
class DslSymbol:
    """A symbol extracted from an embedded DSL region."""
    name: str                       # e.g. "users", "email", "UserCard"
    kind: DslSymbolKind             # table, column, component, css_class, ...
    dsl: str                        # "sql", "jsx", "css", "jinja", ...
    host_file: Path                 # file containing the embedding
    host_range: tuple[int, int]     # byte range within host file
    dsl_range: tuple[int, int]      # byte range within DSL region
    link_hints: list[LinkHint]      # how to resolve to host-language defs
```

```python
@dataclass
class LinkHint:
    """A hint for resolving a DSL symbol to a host-language definition."""
    strategy: str          # "orm_model", "component_export", "css_class_usage", ...
    target_pattern: str    # e.g., class name "User", component "UserCard"
    target_kind: str       # "class", "function", "variable", ...
    module_hint: str       # optional: expected module path pattern
```

### 3. Cross-Language Link Resolution

A new `DslLinkResolver` connects DSL symbols to host-language definitions
by consuming `LinkHint`s and querying the existing symbol index:

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐
│ DslSymbol +  │ ──▶ │ DslLinkResolver  │ ──▶ │ Host symbol match │
│ LinkHints    │     │ (per-strategy    │     │ (from existing    │
│              │     │  resolution)     │     │  scope resolver)  │
└─────────────┘     └──────────────────┘     └───────────────────┘
```

**Resolution strategies**:

| Strategy | DSL → Host Example | Resolution Logic |
|----------|-------------------|-----------------|
| `orm_model` | SQL `users` → Python `class User` | Table name → class with `__tablename__`, or singularize + PascalCase convention |
| `orm_column` | SQL `email` → Python `User.email` | Column name → attribute on resolved model class |
| `django_model` | SQL `auth_user` → Python `class User` (in `auth` app) | `{app}_{model}` naming convention |
| `component_export` | JSX `<UserCard>` → TS `function UserCard` | Component tag → exported function/class of same name |
| `css_class_usage` | CSS `.btn-primary` → JSX `className="btn-primary"` | Class selector → string literal in `className`/`class` attributes |
| `css_class_def` | JSX `className="btn-primary"` → CSS `.btn-primary` | Reverse of above |
| `template_var` | Jinja `{{ user.name }}` → Python `context["user"]` | Template variable → render call context dict |
| `template_block` | Jinja `{% block content %}` → parent template block | Block name → same block in `{% extends %}` target |
| `graphql_type` | GQL `type User` → TS `class UserResolver` | Type name → resolver class by naming convention |
| `graphql_field` | GQL `field email` → resolver method `email()` | Field name → method on resolved resolver class |
| `named_group` | Regex `(?P<slug>...)` → Python `match.group("slug")` | Group name → string argument to `.group()` calls |

### 4. Configuration

DSL support is configured per-project in `.emend/config.toml` (or
`pyproject.toml` under `[tool.emend]`):

```toml
[dsl]
# Enable/disable DSL detection globally
enabled = true

[dsl.sql]
enabled = true
# ORM framework for table-to-model resolution
orm = "sqlalchemy"  # or "django", "prisma", "drizzle", "none"
# Additional injection triggers beyond defaults
inject_calls = ["db.execute", "session.execute", "conn.execute"]

[dsl.jsx]
enabled = true
# Component discovery paths (for cross-file component resolution)
component_dirs = ["src/components", "src/ui"]

[dsl.css]
enabled = true
# CSS module convention: "modules" | "bem" | "tailwind" | "plain"
convention = "modules"
# For CSS modules, the import variable name maps to the module object
# e.g., `import styles from './Foo.module.css'` → `styles.foo`

[dsl.jinja]
enabled = true
# Template search paths (mirrors Flask/Django TEMPLATES config)
template_dirs = ["templates", "src/templates"]
# Framework for context resolution
framework = "flask"  # or "django", "fastapi"

[dsl.graphql]
enabled = true
schema_paths = ["schema.graphql", "src/**/*.graphql"]
```

### 5. Integration with Existing Commands

#### `search` / `find`

Search across DSL boundaries with a `--dsl` flag or automatic detection:

```bash
# Find SQL references to the "users" table
emend search "users" --dsl sql

# Find all CSS classes used but never defined
emend search --dsl css --orphan

# Pattern match inside SQL regions
emend find 'SELECT $COLS FROM $TABLE WHERE $COND' --dsl sql
```

#### `refs` (find-references)

Cross-language reference finding:

```bash
# Find all references to User model — including SQL table "users"
emend refs app/models.py::User --include-dsl

# Find where CSS class "btn-primary" is used (in JSX, HTML, and CSS)
emend refs --dsl-symbol .btn-primary
```

#### `lint`

DSL-aware lint rules in `.emend/patterns.yaml`:

```yaml
rules:
  - name: no-select-star
    dsl: sql
    find: "SELECT * FROM $TABLE"
    message: "Avoid SELECT *; enumerate columns explicitly"

  - name: no-inline-styles
    dsl: jsx
    find: 'style={$EXPR}'
    message: "Use CSS classes instead of inline styles"

  - name: unused-css-class
    dsl: css
    check: orphan-classes
    message: "CSS class '{{name}}' is defined but never referenced"
```

#### `editor-server`

New JSON-RPC methods for editor integration:

```
dsl_goto_definition   — from cursor in DSL region → host-language definition
dsl_find_references   — from host-language symbol → all DSL usages
dsl_hover             — show DSL context (table schema, component props, etc.)
```

### 6. Index Storage

DSL symbols and links are stored in the existing `parse.db` SQLite database
with two new tables:

```sql
CREATE TABLE dsl_symbols (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,          -- table, column, component, css_class, ...
    dsl TEXT NOT NULL,           -- sql, jsx, css, jinja, graphql
    host_file TEXT NOT NULL,
    host_start INTEGER NOT NULL,
    host_end INTEGER NOT NULL,
    dsl_start INTEGER NOT NULL,
    dsl_end INTEGER NOT NULL,
    content_hash TEXT NOT NULL   -- for cache invalidation
);

CREATE TABLE dsl_links (
    id INTEGER PRIMARY KEY,
    dsl_symbol_id INTEGER REFERENCES dsl_symbols(id),
    target_qn TEXT NOT NULL,     -- qualified name of host-language symbol
    target_file TEXT,
    strategy TEXT NOT NULL,      -- resolution strategy used
    confidence REAL NOT NULL     -- 0.0–1.0, for ranked results
);

CREATE INDEX idx_dsl_name ON dsl_symbols(name);
CREATE INDEX idx_dsl_host ON dsl_symbols(host_file);
CREATE INDEX idx_dsl_link_target ON dsl_links(target_qn);
```

### 7. Language Config Extension

Each language config gets an optional `[dsl_injections]` section:

```toml
# In languages/python/config.toml
[dsl_injections]

[[dsl_injections.rules]]
dsl = "sql"
# Detect SQL in string args to these call patterns
trigger = "call"
call_patterns = [
    "text($SQL)",
    "$_.execute($SQL)",
    "$_.executemany($SQL, $_)",
    "cursor.execute($SQL)",
]
# Also detect via magic comment
magic_comment = "language=sql"

[[dsl_injections.rules]]
dsl = "jinja"
trigger = "call"
call_patterns = [
    "render_template_string($TPL)",
    "Template($TPL)",
    "Environment.from_string($TPL)",
]
# .html files in template_dirs are parsed as Jinja
file_extensions = ["html", "jinja2", "j2"]

[[dsl_injections.rules]]
dsl = "html"
trigger = "call"
call_patterns = ["Markup($HTML)"]
file_extensions = ["html"]

# In languages/typescript/config.toml
[dsl_injections]

[[dsl_injections.rules]]
dsl = "sql"
trigger = "tagged_template"
tag_names = ["sql", "Prisma.sql"]

[[dsl_injections.rules]]
dsl = "css"
trigger = "tagged_template"
tag_names = ["css", "styled", "styled.*"]
file_extensions = ["css", "scss", "less"]

[[dsl_injections.rules]]
dsl = "graphql"
trigger = "tagged_template"
tag_names = ["gql", "graphql"]
file_extensions = ["graphql", "gql"]
```

## Implementation Plan

### Phase 1: Infrastructure (injection parsing + symbol extraction)

1. **Add tree-sitter grammars** for SQL, HTML, CSS, and Jinja to the Rust
   `emend_core` crate. These are mature grammars available via
   `tree-sitter-sql`, `tree-sitter-html`, `tree-sitter-css`,
   `tree-sitter-jinja2`.
2. **Implement injection detection** in `emend_core`: given a host CST node
   + injection rules, identify DSL regions and parse them.
3. **Implement `DslSymbolExtractor`** for SQL (tables, columns), CSS (selectors),
   and JSX (component tags) — the three most immediately useful.
4. **Add `dsl_symbols` table** to `parse.db` and wire into the `index`
   command.

### Phase 2: Link resolution + navigation

5. **Implement `DslLinkResolver`** with initial strategies: `orm_model`,
   `orm_column`, `component_export`, `css_class_usage`.
6. **Add `dsl_links` table** and populate during indexing.
7. **Wire into `search` and `refs`** — `--include-dsl` flag to include
   cross-language matches.
8. **Add `dsl_goto_definition` to editor-server** for cursor-aware
   navigation.

### Phase 3: Pattern matching in DSL regions

9. **Extend pattern grammar** (`grammars/pattern.lark`) with a `--dsl`
   mode that switches to DSL-specific node types.
10. **Add DSL-aware lint rules** to the lint engine.
11. **Implement `find`/`replace` inside DSL regions** (initially read-only
    for find, then write support).

### Phase 4: Tier 2 DSLs + deeper integration

12. **Jinja template support**: variable resolution from Python view
    functions, block inheritance navigation.
13. **GraphQL support**: schema-to-resolver linking, query-to-type
    navigation.
14. **Regex named group support**: group name to `.group()` call
    navigation.
15. **`impact` command integration**: changes to an ORM model surface SQL
    queries that may break, component prop changes surface JSX call sites.

## Concrete Navigation Scenarios

### SQL ↔ SQLAlchemy

```python
# models.py
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True)

# queries.py
results = session.execute(text("SELECT email FROM users WHERE id = :id"))
```

- From `users` in SQL → jump to `class User` (via `__tablename__` match)
- From `email` in SQL → jump to `User.email` (via column on resolved table)
- From `User.email` → find refs includes the SQL `SELECT email FROM users`

### JSX ↔ CSS Modules

```tsx
// UserCard.tsx
import styles from './UserCard.module.css';
export function UserCard({ name }: Props) {
    return <div className={styles.card}><span className={styles.name}>{name}</span></div>;
}
```

```css
/* UserCard.module.css */
.card { border: 1px solid #ccc; padding: 16px; }
.name { font-weight: bold; }
```

- From `styles.card` in TSX → jump to `.card` in CSS
- From `.card` in CSS → find refs shows `styles.card` usage in TSX
- From `<UserCard>` in another file → jump to `UserCard` function definition

### Jinja ↔ Flask

```python
# views.py
@app.route("/profile")
def profile():
    user = get_current_user()
    return render_template("profile.html", user=user, posts=user.posts)
```

```html
<!-- templates/profile.html -->
{% extends "base.html" %}
{% block content %}
  <h1>{{ user.name }}</h1>
  {% for post in posts %}
    <p>{{ post.title }}</p>
  {% endfor %}
{% endblock %}
```

- From `{{ user.name }}` in template → jump to `profile()` view, then to
  `User.name` attribute
- From `{% block content %}` → jump to same block in `base.html`
- From `render_template("profile.html", ...)` → jump to the template file
- From `profile()` → find refs includes template variable usages

### GraphQL ↔ Resolvers

```graphql
type Query {
  user(id: ID!): User
}

type User {
  email: String!
  posts: [Post!]!
}
```

```typescript
@Resolver(() => User)
class UserResolver {
  @Query(() => User)
  async user(@Arg("id") id: string): Promise<User> { ... }

  @FieldResolver()
  async posts(@Root() user: User): Promise<Post[]> { ... }
}
```

- From `user(id: ID!)` in GQL schema → jump to `UserResolver.user()` method
- From `posts: [Post!]!` → jump to `UserResolver.posts()` field resolver
- From `type User` → jump to `class User` entity/model definition

## Open Questions

1. **Confidence thresholds**: When a DSL symbol name is ambiguous (e.g., a
   column called `name` could match many classes), should we require a
   minimum confidence score, or show all candidates ranked?

2. **Framework auto-detection**: Should emend auto-detect the ORM/framework
   from installed packages (inspect requirements.txt / package.json), or
   always require explicit configuration?

3. **Injection parse caching**: DSL sub-parses are cheap individually but
   could add up across a large project. Should we cache DSL CSTs separately
   or re-derive them from the host CST on demand?

4. **Selector syntax for DSL symbols**: How should DSL symbols be addressed
   in selectors? Some options:
   - `queries.py::sql:users` (DSL-qualified)
   - `queries.py::[dsl=sql]users` (attribute syntax)
   - `queries.py::users:dsl[sql]` (component syntax)

5. **Write support**: When `replace` modifies a DSL region inside a host
   string, how should escaping/quoting be handled? The replacement text
   needs to be valid within the host string's quoting context.

6. **CSS-in-JS variants**: styled-components, Emotion, Tailwind, CSS
   Modules, and vanilla CSS all have different conventions for connecting
   class names to usage sites. Should each be a separate strategy, or can
   we unify them?

7. **Performance budget**: What's the acceptable overhead for DSL parsing
   during `emend index`? Proposal: DSL indexing should add no more than
   30% to total index time, gated behind `[dsl] enabled = true`.

## Related Work

- **tree-sitter injection queries**: Standard mechanism for embedded
  language parsing; used by Neovim, Helix, and Zed for syntax highlighting.
- **IntelliJ language injection**: JetBrains IDEs support language injection
  via annotations (`@Language("SQL")`) and heuristics — closest prior art.
- **LSP embedded languages**: The LSP spec has limited support for embedded
  languages; most implementations handle it ad-hoc.
- **Semgrep**: Supports cross-file taint tracking but not cross-language DSL
  navigation.
