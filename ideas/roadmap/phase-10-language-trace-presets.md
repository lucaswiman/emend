# Phase 10: Language-Specific Trace Presets

## Goal

Add framework-specific trace rule presets for TypeScript and Rust ecosystems,
mirroring the existing Python presets (Flask, Django, SQLAlchemy, FastAPI).

## Why

The existing `--preset` flag (`emend trace --preset flask`) loads pre-built
source/sink/sanitizer configurations for Python frameworks.  TypeScript and
Rust have their own dominant frameworks with well-known security patterns.
Presets make trace analysis immediately useful without manual config authoring.

## Prerequisites

- Phase 4 (intraprocedural trace for TS/Rust)
- Phase 9 (interprocedural trace, recommended but not strictly required)

## Scope

- `src/emend/trace_presets.py` — `get_preset()`, `merge_configs()`
- `src/emend/cli.py` — `trace --preset` flag
- New preset definitions

## Existing Python Presets

| Preset | Sources | Sinks | Sanitizers |
|--------|---------|-------|------------|
| `flask` | `request.args`, `request.form`, `request.json` | `render_template_string()`, `Markup()`, `execute()` | `escape()`, `bleach.clean()` |
| `django` | `request.GET`, `request.POST`, `request.body` | `HttpResponse()`, `mark_safe()`, `execute()` | `escape()`, `format_html()` |
| `sqlalchemy` | `request.*` | `session.execute()`, `text()`, `engine.execute()` | parameterized queries |
| `fastapi` | Path/query/body params | `HTMLResponse()`, `execute()` | Pydantic validation |

## Planned TypeScript Presets

### `express`
- **Sources**: `req.params`, `req.query`, `req.body`, `req.headers`,
  `req.cookies`
- **Sinks**: `res.send()`, `res.write()`, `eval()`, `innerHTML`,
  `document.write()`, `$.html()`, `child_process.exec()`
- **Sanitizers**: `escape()`, `sanitize()`, `DOMPurify.sanitize()`,
  `validator.escape()`, `encodeURIComponent()`
- **Scope sanitizers**: response-level (`res.set('Content-Type', 'application/json')`)

### `react`
- **Sources**: `useSearchParams()`, `window.location`, `document.cookie`,
  `localStorage.getItem()`
- **Sinks**: `dangerouslySetInnerHTML`, `eval()`, `document.write()`,
  `innerHTML`
- **Sanitizers**: `DOMPurify.sanitize()`, JSX auto-escaping (React escapes
  by default in JSX expressions)

### `nextjs`
- **Sources**: `params`, `searchParams`, `cookies()`, `headers()`,
  `req.query`, `req.body`
- **Sinks**: `dangerouslySetInnerHTML`, `redirect()`, `sql` template tag
  (SQL injection)
- **Sanitizers**: parameterised queries, `encodeURIComponent()`

### `node-sql`
- **Sources**: any (label-based, e.g., `user-input`)
- **Sinks**: `pool.query()`, `connection.query()`, `knex.raw()`,
  `sequelize.query()`, `prisma.$queryRaw()`
- **Sanitizers**: parameterised queries (`pool.query("...", [params])`)

## Planned Rust Presets

### `actix-web`
- **Sources**: `web::Query`, `web::Json`, `web::Path`, `web::Form`,
  `HttpRequest::headers()`, `HttpRequest::cookie()`
- **Sinks**: `HttpResponse::body()`, `format!()` in SQL context,
  `std::process::Command::arg()`, `std::fs::write()`
- **Sanitizers**: type parsing (`parse::<i32>()`), parameterised queries
  (sqlx bind)

### `axum`
- **Sources**: `Query()`, `Json()`, `Path()`, `Extension()`, `HeaderMap`
- **Sinks**: `Html()`, `Response::builder().body()`, `format!()` in SQL,
  `Command::arg()`
- **Sanitizers**: type extraction (Axum extractors enforce types),
  parameterised queries

### `sqlx`
- **Sources**: any (label-based)
- **Sinks**: `sqlx::query()` with string interpolation, `query_as!()`
- **Sanitizers**: `sqlx::query()` with bind parameters (`$1`, `?`)

### `diesel`
- **Sources**: any (label-based)
- **Sinks**: `diesel::sql_query()` with string interpolation
- **Sanitizers**: query builder DSL (`.filter()`, `.select()`)

## Todo

- [ ] Implement `express` preset.
- [ ] Implement `react` preset.
- [ ] Implement `nextjs` preset.
- [ ] Implement `node-sql` preset.
- [ ] Implement `actix-web` preset.
- [ ] Implement `axum` preset.
- [ ] Implement `sqlx` preset.
- [ ] Implement `diesel` preset.
- [ ] Update `get_preset()` to return presets for all languages.
- [ ] Update `merge_configs()` to handle cross-language preset merging.
- [ ] Add tests for each preset: verify sources/sinks/sanitizers are correct,
  verify end-to-end trace detection on a small example.
- [ ] Document presets in CLI help and examples.

## Exit Criteria

- `emend trace --preset express` detects XSS and injection in Express apps.
- `emend trace --preset actix-web` detects injection in Actix-web apps.
- All presets have at least one end-to-end test showing a detected violation
  and a sanitized non-violation.
- `emend trace --preset flask` (existing) still works unchanged.
