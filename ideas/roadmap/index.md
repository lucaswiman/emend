# AST Canonicalization & Near-Duplicate Detection Experiment

**Status:** design
**Type:** experimental script (not a new `emend` command)
**Output:** JSON + markdown reports an agent can skim to evaluate whether AST
canonicalization + recursive hashing surfaces interesting near-duplicate code.

## Goal

Take tree-sitter ASTs of Python files, canonicalize subtrees by alpha-renaming
variables (`bound_{i}` / `free_{i}`), and hash the canonical forms to find
duplicate and near-duplicate code across several large codebases (including
`emend` itself). Keep the hashing pluggable so we can compare exact, LSH,
SimHash, shingled-MinHash, and winnowing schemes on the same corpus.

## Non-goals

- Not a new `emend` command. Lives under `experiments/ast_dedup/`.
- Not a production clone detector. We're looking for signal, not building a UI.
- Not cross-language (Python-first; structure should generalize later).

## Phases

- [x] Phase 1: Minimal Rust AST exposure (`PyTree` / `PyNode`) in `emend_core`
- [x] Phase 2: Python canonicalizer using `PyNode` + `PyScopeResolver`
- [x] Phase 3: Pluggable hashing / fingerprinting layer
- [x] Phase 4: Triviality filters
- [x] Phase 5: Sibling-sequence clone detection (winnowing / k-shingles)
- [ ] Phase 6: Corpus fetcher + runner + statistics report
- [ ] Phase 7: Evaluation writeup for the agent

Details for each phase are in the sibling files in this directory.

---

## Phase overview (for quick reference)

### Phase 1 — Minimal Rust AST exposure

`rust/src/tree_py.rs` adds `PyTree` and `PyNode` wrapping `tree_sitter::Tree`
and `tree_sitter::Node`. Lifetime is handled by storing `Arc<Tree>` alongside
a `Node<'static>` transmuted from the borrowed node (safe because the `Arc`
outlives every `PyNode` derived from it). Module-level `parse_source(source,
ext)` and `parse_file(path)` return `Option[PyTree]`.

API (stable, narrow):

```python
class PyTree:
    root: PyNode
    source: bytes
    language: str   # "python", "typescript", ...

class PyNode:
    kind: str                              # grammar rule name
    is_named: bool
    start_byte: int
    end_byte: int
    start_point: tuple[int, int]           # (row, col)
    end_point: tuple[int, int]
    child_count: int
    named_child_count: int

    def children(self) -> list[PyNode]: ...
    def named_children(self) -> list[PyNode]: ...
    def named_children_with_fields(self) -> list[tuple[Optional[str], PyNode]]: ...
    def child(self, i: int) -> Optional[PyNode]: ...
    def named_child(self, i: int) -> Optional[PyNode]: ...
    def child_by_field_name(self, name: str) -> Optional[PyNode]: ...
    def parent(self) -> Optional[PyNode]: ...
    def text(self) -> str
    def byte_range(self) -> tuple[int, int]

def parse_source(source: str, ext: str) -> Optional[PyTree]: ...
def parse_file(path: str) -> Optional[PyTree]: ...
```

**Deliberately omitted** (YAGNI for the experiment; add later if needed):
tree edit API, cursor exposure, S-expression dump, queries (we already have
`find_pattern`), node IDs (we use `(file, start_byte, end_byte)` as identity).

**Reuse opportunities** — once `PyNode` exists, these helpers become one-line
Python wrappers and can optionally be deleted from Rust or kept as fast paths:
- `collect_identifier_positions` (`rust/src/lib.rs:244`)
- `get_statement_ranges` (`rust/src/symbols.rs`)
- `collect_string_literals` / `collect_comments` (thin wrappers)
- Parts of `ast_utils.py` that still walk via `collect_symbols_from_str` dicts
- DSL region detection (`dsl.py`) which currently uses heuristic passes

**Tests:** `rust/` currently has Rust-side tests for matcher/scope; add a
Python-side test file `tests/test_emend/test_ast_nodes.py` that:
1. Parses a tiny Python snippet, checks `root.kind == "module"`.
2. Walks to a function def, checks `child_by_field_name("name").text == "f"`.
3. Verifies `byte_range` round-trips through `source[start:end]`.
4. Parses TS and Rust snippets, checks top-level node kind for each.
5. Confirms `PyNode` can outlive the local `PyTree` binding (Arc safety).

### Phase 2 — Canonicalizer

`experiments/ast_dedup/canonicalize.py`:

For each file, build:
- A `PyTree` via `parse_file`.
- A `qn_at: dict[(line, col), str]` from
  `PyScopeResolver.references_in_file(path)` — this is the authoritative
  binding→identifier map. Qualified names distinguish locals with the same
  spelling in different scopes (`foo.x` vs `bar.x`), so they already give us
  per-binding canonical IDs without reimplementing scope analysis.

For a given subtree root `N`:

1. Walk `N` bottom-up, emitting `CanonicalToken` for every named child.
2. For identifier leaves (`identifier` node in Python grammar), look up
   `qn_at[(row, col)]`. If the qn binds inside `N` (line range check against
   its defining scope), rename to `bound_{i}` where `i` is the index in
   first-occurrence order within `N`. Otherwise rename to `free_{i}` keyed by
   qn. Keep a per-subtree `{qn -> token}` map; reset it at every new root.
3. Attribute names (`attribute.attribute` field) and method call names are
   semantically load-bearing — leave them as-is by default, behind a
   `--rename-attrs` flag for ablation.
4. String literals canonicalize to `"str"` (or `"str_{i}"` keeping equality
   classes if `--keep-literal-equality`).
5. Numeric literals canonicalize to `"num"` / `"num_{i}"` likewise.
6. Comments and trivia are skipped via `is_named` filter.

Output: a `CanonicalSubtree` containing:
- `kind_seq: tuple[str, ...]` — pre-order sequence of node kinds
- `token_seq: tuple[str, ...]` — pre-order sequence of leaf canonical tokens
- `child_hashes: tuple[bytes, ...]` — bottom-up Merkle hashes of children
- `node_count: int`, `depth: int`, `unique_tokens: int`
- `location: (file, start_byte, end_byte, start_line)` for reporting

### Phase 3 — Hashing / fingerprinting

`experiments/ast_dedup/hashers.py` defines two protocols:

```python
class Fingerprint(Protocol):
    name: str
    def of(self, sub: CanonicalSubtree) -> Any: ...

class Index(Protocol):
    name: str
    def insert(self, key, fp: Any) -> None: ...
    def query(self, fp: Any, threshold: float) -> Iterable[tuple[Any, float]]: ...
```

Implementations to compare:

1. **Merkle exact** — recursive hash `H(node) = blake2b(kind || field_name ||
   H(child_1) || ... || H(child_n))` where leaves hash their canonical token.
   This is compositional: every subtree's hash is derivable from its children,
   so we get `O(nodes)` exact-dedup in one pass. Exact-only; no near matches.
2. **Shingled MinHash** over `kind_seq` k-grams (`k=5`, `num_perm=128`). Uses
   `datasketch.MinHashLSH`. Captures reorderings and small insertions.
3. **SimHash** (Charikar) over tokenized canonical serialization with a
   frequency-weighted projection and Hamming-distance threshold.
4. **Winnowing** (Moss-style) over `kind_seq` with window `w=4`, hash `k=5`;
   select local minima as document fingerprints. Handles long shared runs
   robustly (see Phase 5).
5. **Bag-of-subtrees MinHash** — multiset of child Merkle hashes; MinHash of
   that multiset. Captures "same set of sub-parts in a different order".

Every strategy is registered in a registry so Phase 6 can run them in parallel
over the same corpus and produce a comparison table. Near-dup thresholds are
parameters, not baked in.

**Important compositionality note:** Merkle hashing is compositional only for
the *raw* tree shape (kinds + non-renamed leaves). It is NOT compositional for
the alpha-renamed canonical form, because `bound_1` in a subtree becomes
`bound_7` in a larger containing subtree. Two workable schemes:

- **Two-pass:** first compute raw Merkle hashes bottom-up for cheap exact-dup
  prefiltering. Then, for "candidate roots" (functions, nontrivial statement
  lists) re-canonicalize with alpha-renaming and hash independently. The
  candidate set is O(functions + blocks), not O(nodes), so the cost is fine.
- **Locally-canonical leaves:** identifiers hash as `bound` or `free(qn)` at
  the leaf level, using the global qn as the "free" token. Two subtrees that
  match with alpha equivalence also match under this scheme as long as all
  their identifiers bind outside both — which is usually not what we want, but
  it's a cheap recursive approximation worth benchmarking against the two-pass
  ground truth.

We run both and report agreement.

### Phase 4 — Triviality filters

`experiments/ast_dedup/filter.py`:

- **Size floor:** `node_count >= MIN_NODES` (default 8)
- **Depth floor:** `depth >= MIN_DEPTH` (default 3)
- **Token diversity:** `unique_non_keyword_tokens >= 4`
- **Kind blocklist:** drop subtrees whose root kind is in
  `{return_statement, pass_statement, expression_statement(assignment)}`
  unless they are nested inside a larger candidate
- **Boilerplate patterns:** drop `__init__` bodies that are entirely
  `self.x = x` assignments; drop `__repr__` returning an f-string; drop
  `__eq__` that is a single `isinstance` + attribute comparison chain
- **Halstead-lite score:** approximate volume as
  `node_count * log2(unique_kinds + unique_tokens)`; drop below threshold
- **$bound = $free guard:** explicitly drop anything whose canonical token
  sequence reduces to `<ident> = <ident>`, `<ident>.<name>`, or similar trivial
  patterns (the user called this out specifically in the request)

Each filter is configurable and its effect is reported in the statistics
output, so the agent can see how many duplicates each filter removed and
decide if the filter is too aggressive.

### Phase 5 — Sibling-sequence duplicates

This handles the case "long sequences of duplicate sibling nodes but their
parent nodes aren't necessarily close overall": two functions that share a
10-statement initialization block, for example.

`experiments/ast_dedup/sequence.py`:

1. For every function/method/block, flatten the body into a sequence of
   per-statement canonical Merkle hashes.
2. Apply **winnowing** (Schleimer-Wilkerson-Aiken) with window `w` over the
   hash sequence. Each document emits a set of `(position, hash)` fingerprints.
3. Two documents share a duplicated run iff they share a winnowing fingerprint.
   Map fingerprint → set of `(file, function, statement_index)` to recover the
   run.
4. Alternatively, build a **generalized suffix array** over all statement-hash
   sequences (all functions concatenated with unique separators). Longest
   common substrings ≥ `L` statements are the duplicates. Linear time with
   `pydivsufsort`.
5. Output: a list of `SequenceClone` records `(file_a, func_a, lines_a,
   file_b, func_b, lines_b, length, method)`.

The two methods (winnowing, suffix array) serve as cross-checks. Winnowing is
cheap and scales; suffix arrays give ground truth on small corpora.

### Phase 6 — Corpus runner + statistics

`experiments/ast_dedup/run.py`:

Corpora (cached in `experiments/ast_dedup/.corpora/`):
- `emend` itself (`src/emend/`) — sanity check: we should find near-dupes of
  our own helpers
- Django 5.2 (already cached by `benchmarks/.django-checkout/`)
- CPython stdlib (`cpython/Lib/`, pinned tag)
- Flask + Werkzeug
- pandas (`pandas/core/`) — heavy internal templating, expected high dup rate

For each `(corpus, fingerprint_strategy, filter_set)` we emit:

- Count of candidate subtrees before/after each filter
- Duplicate cluster count, size histogram, and top-20 largest clusters
- Cross-corpus duplication pairs (does `emend` reimplement a stdlib helper?)
- Near-dup pair list with `(similarity, size, location_a, location_b)` sorted
  by `similarity * node_count`
- For sibling-sequence clones: length histogram, top-20 longest shared runs
- Precision proxy: on the intersection of Merkle-exact and each LSH strategy,
  report agreement; on disagreements, sample 20 and tag them
  "real / spurious / boilerplate" via the triviality filter labels
- Per-strategy wall-clock and peak RSS

Output: `experiments/ast_dedup/reports/{corpus}-{timestamp}.json` +
`{corpus}-{timestamp}.md`. The markdown is short (under 2 KB each) and is the
artifact the evaluating agent is expected to read.

### Phase 7 — Evaluation writeup

A short `experiments/ast_dedup/EVALUATION.md` synthesizes results across
corpora and hashing strategies, and calls out:

- Which strategy finds the most real duplicates per unit compute
- Whether recursive Merkle hashing alone is sufficient or whether LSH adds real
  value on top
- What the triviality filters are hiding (error count from the sample review)
- Candidate real-world refactor targets found in emend itself

## Open questions

- Qualified-name stability: does `PyScopeResolver` emit consistent qns for
  comprehension variables and walrus bindings? Spot-check in Phase 2.
- Whether to run Phase 5 on the entire sequence across a corpus (cross-function
  clones) vs. only intra-function. Default: cross-function.
- Should we expose `PyNode` in `ast_utils.py` as the public face, or keep it in
  `emend_core` directly? Lean toward public face so the experiment doesn't
  depend on the Rust import name.
- Arc-lifetime transmute in Phase 1: need a soundness review before merge.
