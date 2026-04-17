# Cross-Repo AST Dedup Report (2026-04-15)

## Scope

This pass populated subtree-hash corpora for:

- `emend`
- `django`
- `fastapi`
- `flask`
- `lark`
- `sqlalchemy`
- `sympy`

The merged SQLite corpus is `experiments/ast_dedup/cross-repo-all.sqlite`.
Accepted subtrees after the Phase 4 filters: `128,210`.

Exact cross-repo canonical-hash overlaps (same normalized subtree appearing in
at least 2 repos): `116`.

## Result

After a manual pass over the top exact cross-repo matches and one heuristic
iteration to suppress constant-only blocks / constant-only class bodies, there
were **no clearly interesting substantial exact cross-repo matches left**.

That is itself a useful result:

- exact alpha-canonicalized Merkle hashes are strong enough that they rarely
  collide across unrelated mature libraries except on boilerplate
- the few cross-repo collisions that do exist are mostly stereotyped defensive
  code, constant tables, tiny signatures, or abstract-method stubs
- for cross-repo work, the next likely source of signal is **near-duplicate**
  matching rather than exact canonical hashes

## Representative Raw Matches

These are real exact normalized matches across repos, but they are not strong
refactor targets.

### 1. Enum / constant tables

`django/db/migrations/autodetector.py`:

```python
class Type(Enum):
    CREATE = 0
    REMOVE = 1
    ALTER = 2
    REMOVE_ORDER_WRT = 3
    ALTER_FOO_TOGETHER = 4
```

`sqlalchemy/engine/interfaces.py`:

```python
class CacheStats(Enum):
    CACHE_HIT = 0
    CACHE_MISS = 1
    CACHING_DISABLED = 2
    NO_CACHE_KEY = 3
    NO_DIALECT_SUPPORT = 4
```

These matched because the normalized shape is "enum-like class with 5 constant
assignments". This is boilerplate, not shared logic. The post-filter now marks
these as `constant_class_body`.

### 2. Constant assignment blocks

`django/db/migrations/operations/base.py`:

```python
ADDITION = "+"
REMOVAL = "-"
ALTERATION = "~"
PYTHON = "p"
SQL = "s"
MIXED = "?"
```

`sqlalchemy/sql/selectable.py`:

```python
UNION = "UNION"
UNION_ALL = "UNION ALL"
EXCEPT = "EXCEPT"
EXCEPT_ALL = "EXCEPT ALL"
INTERSECT = "INTERSECT"
INTERSECT_ALL = "INTERSECT ALL"
```

Again, this is a constant table. The post-filter now marks these as
`constant_assignment_block`.

### 3. Defensive type-check + raise + return

`django/contrib/gis/db/models/sql/conversion.py`:

```python
def get_prep_value(self, value):
    if not isinstance(value, Area):
        raise ValueError("AreaField only accepts Area measurement objects.")
    return value
```

`sympy/codegen/ast.py`:

```python
def _construct_text(cls, text):
    if not isinstance(text, str):
        raise TypeError("Argument text is not a string type.")
    return text
```

This is a real shared control-flow skeleton, and it showed up across
`django`, `fastapi`, `sqlalchemy`, and `sympy` in block form. But it is only a
3-line validation idiom, not a significant shared chunk of domain logic.

### 4. Tiny graph/topological loop fragment

`django/db/migrations/graph.py` and `sqlalchemy/util/topological.py` share:

```python
if node in todo:
    stack.append(node)
    todo.remove(node)
    break
```

This is one of the most semantically specific exact matches in the corpus, but
it is still only a 4-line local loop fragment.

### 5. Abstract stub methods

`django/core/management/base.py` and `sympy/matrices/common.py` both contain
the exact normalized shape:

```python
def name(self_or_cls, *args, **kwargs):
    """..."""
    raise NotImplementedError("...")
```

This appears in several places across `django` and `sympy`. It is a useful
signal that the canonicalizer is doing the right thing, but it is not an
interesting duplication target.

## Heuristic Iteration

Initial exact-cross-repo analysis surfaced 3 "interesting" clusters, all of
which were constant tables or enum-style class bodies. To remove those false
positives I added two post-filters in `cross_repo.py`:

- `constant_class_body`
- `constant_assignment_block`

After that pass:

- cross-repo candidate clusters remained `116`
- interesting clusters fell from `3` to `0`

The generated machine-readable reports are:

- `experiments/ast_dedup/reports/cross-repo-analysis-v1.{json,md}`
- `experiments/ast_dedup/reports/cross-repo-analysis-v2.{json,md}`

`v2` is the current post-filtered result.

## Interpretation

For **exact** normalized subtree hashes, the outcome is:

- good for finding intra-repo refactor targets inside one codebase
- too strict to recover substantial shared logic across distinct mature repos
- still useful as a baseline because it quantifies how little exact structural
  duplication actually survives across repos

## Intra-repo highlights

The strongest **within-codebase** findings from this experiment are still in
`emend`, because that is the corpus I ran through the full strategy +
sibling-sequence review and then hand-labeled in `EVALUATION.md`.

### `emend`

These were the most interesting duplicates inside one repo:

1. `transform.py` has a shared 9-statement opening between
   `set_component()` and `remove_component()`:
   read file, validate existence, read text, derive extension, call
   `_rust.get_symbol_component_range()`, then null-check. This is the clearest
   helper extraction candidate in the whole run.
2. `fact_graph.py` repeats the pattern
   `result = self._client.run(...); return [Fact(**unpack(r)) for r in result["rows"]]`
   across `_all_calls`, `_all_references`, `_all_types`, and `_all_imports`.
   That wants a `_select_all(query, factory)` helper.
3. `fact_graph.py` also has a family of near-identical query-builder blocks
   in `cfg_edges`-style methods:
   `if X is not None: clauses.append(...); params[...] = X`.
   The top exact cluster is that guard repeated 19 times, and sibling-sequence
   matching shows the larger repeated runs around it.
4. `type_oracle.py` has structurally duplicated adapter constructors for
   resolving a backend binary and forwarding to `super().__init__`, especially
   in the `PyrightAdapter` / `TyAdapter` / `TypeScriptAdapter` family.
5. `type_oracle.py` also has a 7-statement duplicated opening between two
   backend-caching paths, suggesting another local helper extraction.

Those findings are documented in more detail in
`experiments/ast_dedup/EVALUATION.md`.

### Other corpora

`django`, `fastapi`, `flask`, `lark`, `sqlalchemy`, and `sympy` were
populated into the subtree SQLite corpus using `--populate-only`, which means
I did **not** run the full per-corpus strategy comparison / sibling-sequence
review / manual audit pass for them. So I do not want to pretend there is a
comparable ranked list of intra-repo duplicates for those repos yet.

The pinned markdown reports for those corpora are present under
`experiments/ast_dedup/reports/`, but because they came from the populate-only
path they do not include audited top-cluster findings. If we want the same
level of intra-repo detail for `django` or `sqlalchemy`, the next step is to
run the full Phase 6 analysis on those repos individually rather than just
populate their subtree hashes.

### `sqlalchemy`

I later ran a full Phase 6 pass on `sqlalchemy` with `merkle_exact`, which
produced `1,270` exact clusters and `44` sibling-sequence clones. As with the
cross-repo exact matches, the biggest exact clusters were mostly stereotype
heavy:

- dozens of `@property` methods in `testing/requirements.py` that are just
  `return exclusions.open()` / `return exclusions.closed()`
- repeated short abstract stubs and interface methods
- constant / enum-like blocks

The more interesting **within-repo** findings came from sibling-sequence
matching:

1. `dialects/postgresql/base.py` and `dialects/sqlite/base.py` share a
   13-statement `visit_on_conflict_do_update()` run. The code that walks
   `update_values_to_set`, coerces literals, formats assignments, warns on
   extra keys, and builds the final `"DO UPDATE SET ..."` string is
   structurally the same across both dialect compilers.
2. `dialects/postgresql/array.py` and `sql/sqltypes.py` share the ARRAY type
   constructor setup:
   validate nested ARRAY types, instantiate type objects passed as classes,
   then assign `item_type`, `as_tuple`, `dimensions`, and `zero_indexes`.
   This looks like a real base-class/helper candidate rather than accidental
   duplication.
3. `ext/asyncio/session.py` and `orm/session.py` share the sessionmaker
   constructor path that copies `bind`, `autoflush`, `expire_on_commit`, and
   optional `info` into `kw` before storing `self.kw` / `self.class_`. The
   async version and sync version intentionally diverge later, but the shared
   prefix is substantial enough to be a real dedup target.
4. `orm/util.py` contains a same-file duplicated 6-statement run around
   `L804-813` and `L826-835`, which is usually the strongest signal that a
   small local helper was never extracted.

So the `sqlalchemy` result is similar to `emend`, but less clean: exact
clusters skew heavily toward framework boilerplate, while sibling-sequence
matching is what starts surfacing the more useful internal duplicates.

The next experiments worth doing are:

1. Cross-repo near-duplicate matching with `kind_token_shingles_minhash`
   rather than exact canonical hashes.
2. Cross-repo sibling-sequence matching, especially for repeated validation or
   setup runs inside larger functions.
3. Optional candidate narrowing to functions / blocks with a minimum number of
   non-placeholder operators and calls, so extremely generic signatures and
   stubs never enter the cross-repo queue.

## Production recommendation

If this graduates from experiment to product, the integration should be
deliberately narrow:

- **Storage:** reuse the existing cache split. Put per-file canonical subtree
  payloads in `parse.db` keyed by content hash/version, because that is already
  the content-addressed SQLite cache. Put queryable duplicate facts in
  `facts.db`, because structured analysis belongs there.
- **CLI:** add a read-only analysis command rather than a new editor-like
  workflow. The right shape is an `analyze dupes` / `duplicates` command that
  returns ranked exact/sibling-sequence findings for the current repo, with
  JSON output for automation.
- **Lint:** keep lint integration conservative. Do not emit a warning for every
  repeated tiny idiom. Only report duplicates above a minimum score/length and
  only when the match crosses function boundaries or files.
- **MCP:** expose one bounded analysis surface, not the whole experimental
  toolbox. The MCP tool should return a small ranked list of duplicate
  findings, representative snippets, and suggested extraction points, with the
  same scoring/filters as the CLI.

What should **not** be productized from this branch:

- the ad hoc cross-repo SQLite corpus database
- external corpus cloning / report generation flows
- the full strategy registry as user-facing options
- free-form markdown reporting machinery

The product version should ship only one or two proven strategies:

- exact canonical Merkle hashes for whole-subtree duplicates
- sibling-sequence detection for extract-method style repeated runs

Near-duplicate LSH matching should stay behind an internal flag until it has
its own precision audit on real repos.

## Bottom line

Across `emend`, `django`, `fastapi`, `flask`, `lark`, `sqlalchemy`, and
`sympy`, I did **not** find compelling large exact normalized code chunks that
were shared across repos. The exact matches that do exist are almost entirely
boilerplate. That does not invalidate the experiment; it sharpens the next
step: for cross-repo work, focus on near-duplicates rather than exact
canonical-hash equality.
