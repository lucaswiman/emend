# DSL Support for Embedded Languages

**Status: Phase 1 partial, Phase 2 partial — regex-based SQL infrastructure implemented**

Initial implementation in `dsl.py`: SQL region detection (keyword heuristics,
magic comments), table/column extraction, ORM link resolution
(`__tablename__` + singularize/PascalCase), `emend dsl` CLI command.
Tree-sitter DSL grammars and CSS/JSX/Jinja extractors are deferred.

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

**Design choice**: We adopt a *virtual code* model inspired by Volar.js
rather than relying solely on tree-sitter's built-in injection queries.
Tree-sitter injections are syntax-only (no cross-boundary semantic info),
and their `injections.scm` files don't encode the call-site heuristics we
need (e.g., "first arg of `cursor.execute()` is SQL").  Instead, emend
detects injection sites using its own configurable rules (below), extracts
the string content, and parses it as a standalone DSL document — similar to
Volar.js's `VirtualCode` objects but at the index/batch level rather than
in an LSP hot path.

This also follows the *Language Services* pattern recommended by VS Code:
embed DSL parsers internally rather than forwarding requests to external
language servers, keeping the tool self-contained and CLI-friendly.

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐
│ Host parse   │ ──▶ │ Injection detect │ ──▶ │ DSL sub-parse     │
│ (Python CST) │     │ (rule-based:     │     │ (standalone SQL/  │
│              │     │  call patterns,  │     │  HTML/Jinja CST   │
│              │     │  tags, comments) │     │  per region)      │
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
by consuming `LinkHint`s and querying the existing symbol index.

**Relation to scope graphs**: The academic scope graph model (Visser et al.)
provides a clean formal framework for cross-language name resolution —
declarations and references in different languages become nodes in a shared
graph, with resolution edges crossing language boundaries.  GitHub's Stack
Graphs operationalize this for Python and TypeScript.  Our approach is more
pragmatic: rather than building a unified scope graph across languages, we
use strategy-based link hints that encode domain-specific conventions (ORM
naming, component exports, CSS selectors).  This trades theoretical
generality for practical accuracy — an ORM-aware strategy can resolve
`users` → `class User` with high confidence, while a generic scope graph
would need the same domain knowledge encoded as custom scope rules anyway.

A future evolution could adopt scope graph edges for the cross-language
links, enabling transitive resolution (e.g., SQL column → ORM attribute →
API serializer field) and integration with GitHub's Stack Graphs or
Sourcegraph SCIP indexers.

Resolution architecture:

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

8. **SCIP interoperability**: Should DSL symbols be exportable in
   Sourcegraph's SCIP format?  This would enable cross-language navigation
   on Sourcegraph instances and provide a standard interchange format.
   SCIP's symbol scheme would need extending for DSL-qualified names.

9. **Scope graph integration**: Should we adopt stack graph edges for
   cross-language links long-term?  This would enable transitive resolution
   chains (SQL column → ORM attr → API field) and potential integration
   with GitHub's precise code navigation, but adds complexity.

10. **Concatenated/interpolated strings**: JetBrains IntelliLang handles
    SQL queries built from concatenated string fragments as a single logical
    document.  Should we support this?  Python f-strings and TS template
    literals with interpolation are common for dynamic SQL.  Proposal:
    phase 1 handles only complete string literals; phase 2 adds support for
    concatenation with `$HOLE` placeholders in the DSL parse.

11. **PolyglotPiranha-style rule graphs**: Uber's Piranha uses a graph of
    match-replace rules with capture propagation between nodes.  Should
    DSL-aware lint/replace rules support chaining (e.g., "match SQL
    pattern, then check the enclosing Python call")?  This is more
    expressive than flat rules but harder to configure.

## Prior Art

### Foundational Theory

**Scope Graphs (TU Delft, Eelco Visser et al.)** — The most rigorous
academic framework for language-independent name resolution.  Scope graphs
separate name resolution into two stages: (1) construct a language-specific
scope graph from an AST, and (2) resolve references using a
language-independent resolution algorithm.  The resolution calculus is
declarative and supports lexical scoping, modules, imports, and complex
binding rules.  This work directly influenced GitHub's Stack Graphs.

Key papers:
- "A Theory of Name Resolution" (ESOP 2015) — Neron, Tolmach, Visser,
  Wachsmuth
- "Scopes as types" (OOPSLA 2018)
- "Scope Graphs: The Story so Far" (EVCS 2023)

**Cross-Language Support Mechanisms (Mayer et al.)** — A controlled
experiment with 22 participants demonstrated that cross-language support
mechanisms (visualization, static checking, navigation, refactoring)
significantly aid software development.  Over 90% of surveyed professional
developers reported problems related to cross-language linking.

**Towards Analyzing N-language Polyglot Programs (2026)** — Recent research
identifying key open problems: incremental dataflow updates at language
boundaries, function summary exchange across languages, precise
inter-procedural call graph construction in polyglot systems.

### IDE and Editor Approaches

**JetBrains IntelliLang** — The most mature embedded language injection
system.  String literals are treated as fragments of another language with
full syntax highlighting, completion, navigation, and validation.  Injection
is driven by `@Language("SQL")` annotations (Java), `# language=SQL` magic
comments (Python), or configurable place patterns (e.g., "first argument of
`connection.prepareStatement()`").  The low-level `MultiHostInjector` API
handles concatenated strings — multiple fragments treated as one logical
document.  This is the closest prior art to what we are proposing, but it is
proprietary and tightly coupled to the JetBrains PSI infrastructure.

**Angular Language Service** — Deep cross-language navigation between
TypeScript and HTML templates: go-to-definition from template expressions
to TypeScript component properties/methods, rename-symbol that works across
templates and TypeScript, hover on template bindings.  Requires
`strictTemplates` in `tsconfig.json`.

**VS Code extensions** for specific DSL pairs:
- *React CSS modules* (`viijay-kr.react-ts-css`): Ctrl+Click on CSS class
  names in JSX/TSX to navigate to `.module.css` definitions.
- *vscode-styled-components*: Syntax highlighting and IntelliSense inside
  tagged template literals for CSS-in-JS.
- *vscode-graphql* (GraphQL Foundation): Go-to-definition, hover, and
  outline for GraphQL across files.
- *Relay GraphQL* (Meta/Coinbase): Go-to-definition for fragments, fields,
  and types with Relay compiler integration.

### Tree-sitter Injection Mechanism

Tree-sitter supports language injections via `queries/injections.scm` files
shipped with each grammar.  Two special captures drive the system:
`@injection.content` marks the text region to reparse, and
`@injection.language` captures a node whose text names the target language.
The `#set! injection.language "sql"` directive hardcodes the language for
known patterns.  Parsing produces a hierarchy of `LanguageLayer` objects;
injected languages can themselves have injections (arbitrary nesting).

Used by Neovim, Helix, Zed, and Pulsar for syntax highlighting.

**Current limitations** (relevant to this proposal): injected regions are
parsed as top-level nodes in the target grammar — there is no way to
specify that an injection should be a specific AST node type.
Cross-region semantic information (definitions visible across injection
boundaries) is not shared.  See
[tree-sitter/tree-sitter#3625](https://github.com/tree-sitter/tree-sitter/issues/3625)
for the active proposal to improve this.

### Embedded Language LSP Frameworks

**Volar.js** — The dominant embedded-language LSP framework, used by Vue,
Astro, and Svelte.  Core concept: a file is parsed into regions, each
mapped to a `VirtualCode` object for its language.  Service plugins
provide language features per embedded language.  This avoids duplicating
expensive TypeScript Language Service instances.  ByteDance's Lynx team
shipped a complete language toolset using Volar.js in two weeks with a
single developer.

**VS Code's two approaches** for embedded languages:
1. *Language Services* (recommended): the language server embeds libraries
   for each sub-language internally (e.g., the HTML LS uses CSS and JS
   language services for `<style>` and `<script>`).
2. *Request Forwarding*: the language server creates virtual text documents
   for embedded regions and forwards LSP requests back to VS Code.

**LSP Virtual Documents Proposal** — An emerging proposal to make embedded
languages first-class in LSP: servers create virtual text documents, and
clients delegate LSP requests to the appropriate language server.  Not yet
part of the LSP specification.

### Code Intelligence Platforms

**GitHub Stack Graphs** — GitHub's precise code navigation, based on scope
graphs research.  Properties: zero-configuration (no CI job needed),
declarative DSL for name binding rules, incremental (processes most commits
in seconds), built on tree-sitter.  Currently supports Python and TypeScript
with precise navigation.

**Sourcegraph SCIP** — The SCIP Code Intelligence Protocol (replacing LSIF)
is a Protobuf-based format for language-agnostic code indexing.  Supports
cross-repository navigation (following symbols across repos) and
cross-language navigation (e.g., Protobuf definitions to generated Java/Go
bindings).  10x CI speedup over LSIF for TypeScript indexing.

### Polyglot Transformation Tools

**PolyglotPiranha (Uber, PLDI 2024)** — A lightweight polyglot code
transformation DSL built on tree-sitter, supporting 10 languages.  Uses a
graph of match-replace rules where edges specify application order and
captures propagate between nodes.  Deployed at Uber scale (7.5M LoC),
deleting 210K LoC and migrating 20K LoC across 1,611 PRs.  Limitation:
purely syntactic — cannot express transformations requiring type resolution
or control-flow analysis.

**Semgrep** — Cross-language pattern matching via `metavariable-pattern`
for polyglot files (e.g., matching JavaScript `eval` inside HTML).
Commercial tier adds cross-file and cross-function dataflow analysis.  Built
on tree-sitter, supports 30+ languages.  Does not provide navigation or
reference-finding.

### DSL-Specific Tools

**Jinja2/Django templates:**
- *jinja-lsp* (uros-5): A dedicated LSP for Jinja templates supporting
  go-to-definition for both template identifiers and Python backend
  identifiers.  Works with Helix and Neovim.  Nascent but promising.
- *live-jinja-renderer* (VS Code): Ctrl+Click navigation to macros,
  variables, blocks, and template paths.

**SQL:** No existing tool provides true go-to-definition from embedded SQL
strings to table/column definitions in Python or Java.  The
sql-language-server (joe-re), sqls, and Postgres Language Server all work on
standalone `.sql` files.  JetBrains is the only environment that comes
close, via IntelliLang + Database Tools.

**Python-specific:** Jedi, Rope, and pylsp are purely Python-focused — none
handle embedded SQL, templates, or cross-language analysis.

### Gaps This Proposal Fills

The biggest gaps in the current landscape:

1. **Embedded SQL ↔ ORM navigation** — No tool connects SQLAlchemy/Django
   model definitions to their usage in raw SQL queries or vice versa.
2. **Template ↔ view navigation** — jinja-lsp is nascent; no tool provides
   bidirectional navigation between Flask/Django views and Jinja2 templates
   with full semantic understanding.
3. **General embedded DSL framework for static analysis** — Tree-sitter
   injections handle syntax only.  There is no general framework for
   semantic-level cross-language analysis in static analysis tools (as
   opposed to IDEs).
4. **LSP-level standard** — The virtual documents proposal exists but is not
   yet part of the LSP spec; each framework reinvents embedding.
5. **CLI-accessible cross-language refs** — All existing cross-language
   navigation is IDE-specific.  A CLI tool providing `emend refs
   models.py::User --include-dsl` has no equivalent today.
