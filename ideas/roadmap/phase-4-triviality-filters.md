# Phase 4 — Triviality filters

## Purpose

Address the user's explicit concern: "How to avoid uninteresting matches like
`$bound1=$free1` or whatever?" Most exact/near-exact duplicates in a real
codebase are trivial — short assignments, single-line returns, stereotyped
dunders. We need filters that prune these *before* they dominate the report,
while also reporting their removal counts so the filters themselves can be
audited.

## Design

Every filter is a `Callable[[CanonicalSubtree], FilterVerdict]` registered
by name. Filters short-circuit: the first `REJECT` wins, but we still record
which filter rejected each subtree so the statistics can cross-tabulate.

```python
@dataclass(frozen=True)
class FilterVerdict:
    accept: bool
    reason: str | None          # None if accepted

@dataclass
class FilterConfig:
    min_node_count: int = 8
    min_depth: int = 3
    min_unique_non_keyword: int = 4
    halstead_volume_min: float = 30.0
    block_root_kinds: frozenset[str] = frozenset({
        "return_statement",
        "pass_statement",
        "raise_statement",
    })
    block_trivial_patterns: bool = True   # enables the hand-written cases below
```

## Filter catalog

### `size_floor`
`accept = sub.node_count >= cfg.min_node_count`.
Removes most of the boilerplate in one shot.

### `depth_floor`
`accept = sub.depth >= cfg.min_depth`.
Prunes flat statement lists and single-line constructs.

### `token_diversity`
```python
unique_non_kw = len(set(sub.token_seq) - PYTHON_KEYWORDS)
accept = unique_non_kw >= cfg.min_unique_non_keyword
```
Rules out `return None`, `self.x = x`, `a = b`, etc. This is the direct
answer to "$bound1=$free1".

### `root_kind_blocklist`
Reject if the candidate root kind is in `cfg.block_root_kinds`. These should
have been filtered by `size_floor`, but we keep the explicit check so the
stats report counts them separately.

### `halstead_lite`
```python
n = sub.node_count
vocab = len(set(sub.kind_seq)) + len(set(sub.token_seq))
volume = n * log2(max(vocab, 2))
accept = volume >= cfg.halstead_volume_min
```
A cheap proxy for Kolmogorov complexity. Drops low-information subtrees
regardless of raw size.

### `stereotyped_dunder`
Pattern-match trivial implementations:
- `__init__` whose body is entirely `self.<name> = <param>` assignments
- `__repr__` returning a single f-string / format
- `__eq__` / `__lt__` that is a single `isinstance` + attribute comparison
- `__hash__` that is `return hash((self.a, self.b, ...))`
- `@property` getters that are `return self._<name>`

Detected via the canonical subtree's `kind_seq`/`token_seq` shape. Each
case has a short matching function; the catalog is extensible.

### `identity_pattern`
Reject subtrees whose canonical form reduces to:
- `<ident> = <ident>`
- `<ident>.<name>`
- `return <ident>`
- `return <ident>.<name>`
- `<ident>(<ident>)`

Implemented as a set of small canonical-form templates checked against
`token_seq` after whitespace/keyword normalization.

### `regex_free` (optional, off by default)
The user's request warns that "Regexes in analysis code are a big code
smell." This filter is excluded unless explicitly enabled via config, and if
enabled it uses tree-sitter pattern matching (`find_pattern(...)` on the
file, rebuilt from a persisted pattern set), NOT Python `re`. Intentionally
gated because pattern matching at filter time is expensive.

## Reporting

Every filter records `(subtree_key, filter_name, verdict)`. The Phase 6
report aggregates:

```
Filter removal counts
---------------------
size_floor             18,422
depth_floor             2,153
token_diversity         4,011
root_kind_blocklist       412
halstead_lite           1,089
stereotyped_dunder        587
identity_pattern          330
---------------------
accepted candidates    12,941
```

…and, for each filter, samples 5 *rejected* subtrees so a human can sanity
check the filter isn't dropping real signal. This is the critical feedback
loop for iterating on the filters themselves.

## Tests

`experiments/ast_dedup/tests/test_filter.py`:

1. `size_floor` drops a 3-node `return None` subtree.
2. `token_diversity` drops a subtree with only `self`, `x`, `=` tokens.
3. `halstead_lite` drops a long but low-vocab subtree (e.g. 40 identical
   `self.x += 1` lines) — ensure this is actually desired, since such a
   subtree is arguably interesting; mark the test as
   documenting-behavior-not-prescribing.
4. `stereotyped_dunder` drops a canonical `__init__` assigning 5 params.
5. `identity_pattern` drops `def f(x): return x`.
6. A real refactor target from `src/emend/` is NOT dropped (pick a
   hand-chosen non-trivial duplicate, pin it in the test).

## Checklist

- [x] `experiments/ast_dedup/filter.py` with all filters + `FilterConfig`
- [x] Each filter has a removal-count counter and a sample buffer
- [x] `tests/test_filter.py` with cases 1-6
- [x] Hand-chosen non-trivial duplicate from emend's own source for test 6
      (`_resolve_cache_root` in `src/emend/transform.py`)
