# Phase 2 — Canonicalizer

## Purpose

Given a `PyTree` (from Phase 1) and a `PyScopeResolver` (existing), produce a
canonical form of each candidate subtree where variables are alpha-renamed to
`bound_{i}` / `free_{i}` and literals are replaced with placeholders. The
canonical form is what we hash in Phase 3.

## Inputs

- `PyTree` for the file
- Authoritative qualified-name lookup: `PyScopeResolver.references_in_file(path)`
  returns `[(qn, line, col, start_byte, end_byte, ref_kind, in_annotation)]`.
  We build `qn_at: dict[(line, col), str]` from this.
- Optional: the scope structure from `scopes_in_file(path)` for tie-breaking
  "does this qn bind inside the current subtree?".

## Outputs

```python
@dataclass(frozen=True)
class CanonicalSubtree:
    # Source location (for reporting + going back to the code)
    file: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int

    # Structural shape (names stripped)
    kind_seq: tuple[str, ...]            # pre-order named-node kinds
    token_seq: tuple[str, ...]            # pre-order leaf canonical tokens
    depth: int
    node_count: int

    # Recursive Merkle hash over (kind, field_name, child_hashes, leaf_token)
    # using the *raw* non-renamed leaves (see compositionality note in index.md).
    raw_merkle: bytes

    # Alpha-renamed canonical hash (not compositional; computed at candidate
    # roots only).
    canonical_hash: bytes

    # Stats for filters
    unique_tokens: int
    unique_non_keyword_tokens: int
    kind_histogram: tuple[tuple[str, int], ...]
```

## Candidate roots

Not every node is worth canonicalizing. We collect "candidates" = roots at
which we actually run the alpha-renaming pass:

1. Every `function_definition` / `class_definition` / `decorated_definition`.
2. Every `block` child of a function (the body).
3. Every maximal run of ≥ 3 sibling statements inside a `block`. (These are
   the sibling-sequence candidates for Phase 5; Phase 2 only emits them.)
4. Every `if_statement`, `for_statement`, `while_statement`, `try_statement`
   whose body has ≥ 2 statements.

Everything else is seen only as part of the *raw Merkle* bottom-up pass, which
gives us exact-structure dedup for free.

## Algorithm

### Pass A: raw Merkle hash (every node)

Recursive post-order walk of `tree.root`:

```python
def raw_hash(node: PyNode) -> bytes:
    h = blake2b(digest_size=16)
    h.update(node.kind.encode())
    if node.is_named:
        for field, child in node.named_children_with_fields():
            h.update((field or "").encode())
            h.update(raw_hash(child))
    else:
        # leaf or anonymous token: include text directly so syntactic tokens
        # don't collapse (e.g. "+" vs "-")
        h.update(node.text().encode())
    return h.digest()
```

Result: `raw_hash_of: dict[(start_byte, end_byte), bytes]`. Exact-duplicate
detection = group by `raw_merkle`.

### Pass B: alpha-renamed canonicalization (candidates only)

For each candidate root `R`:

```python
def canonicalize(R: PyNode, qn_at, scope_of_qn) -> CanonicalSubtree:
    rename: dict[str, str] = {}       # qn -> canonical token
    bound_counter = itertools.count()
    free_counter = itertools.count()
    kind_seq, token_seq = [], []

    def assign(qn: str) -> str:
        if qn in rename:
            return rename[qn]
        if binds_inside(qn, R):
            tok = f"bound_{next(bound_counter)}"
        else:
            tok = f"free_{next(free_counter)}"
        rename[qn] = tok
        return tok

    def walk(n: PyNode) -> bytes:
        if n.is_named:
            kind_seq.append(n.kind)
        child_hashes: list[bytes] = []
        if n.named_child_count == 0:
            tok = leaf_token(n, assign, qn_at)
            if tok is not None:
                token_seq.append(tok)
            return blake2b(n.kind.encode() + (tok or "").encode(),
                           digest_size=16).digest()
        for field, child in n.named_children_with_fields():
            child_hashes.append(blake2b(
                (field or "").encode() + walk(child),
                digest_size=16).digest())
        h = blake2b(digest_size=16)
        h.update(n.kind.encode())
        for ch in child_hashes:
            h.update(ch)
        return h.digest()

    canonical = walk(R)
    return CanonicalSubtree(
        file=R.file_path,
        start_byte=R.start_byte,
        end_byte=R.end_byte,
        start_line=R.start_point[0],
        end_line=R.end_point[0],
        kind_seq=tuple(kind_seq),
        token_seq=tuple(token_seq),
        depth=compute_depth(R),
        node_count=len(kind_seq),
        raw_merkle=raw_hash_of[(R.start_byte, R.end_byte)],
        canonical_hash=canonical,
        unique_tokens=len(set(token_seq)),
        unique_non_keyword_tokens=len(
            set(token_seq) - PYTHON_KEYWORDS),
        kind_histogram=tuple(sorted(Counter(kind_seq).items())),
    )
```

### Leaf tokenization

```python
def leaf_token(n: PyNode, assign, qn_at) -> str | None:
    k = n.kind
    if k == "identifier":
        qn = qn_at.get((n.start_point[0], n.start_point[1]))
        if qn is None:
            return "free_unresolved"   # conservative
        return assign(qn)
    if k == "attribute":
        # .attribute field: leave method/attr names intact by default
        return None   # handled by recursion
    if k == "string":
        return "str"
    if k == "integer" or k == "float":
        return "num"
    if k == "true" or k == "false" or k == "none":
        return k
    if k == "type_identifier":
        return assign(qn_at.get((n.start_point[0], n.start_point[1]), "T"))
    # operators and punctuation come through as anonymous nodes; named-only
    # iteration elsewhere drops them.
    return n.text()  # fallback: keyword literals like `return`, `if`, ...
```

### `binds_inside(qn, R)`

Use `PyScopeResolver.scopes_in_file(path)` to find the defining scope of
`qn`. If that scope's `(start_line, end_line)` is strictly contained within
`R.start_point[0]..R.end_point[0]`, the variable is bound inside the subtree
and gets `bound_*`. Otherwise `free_*`.

Edge cases:
- Comprehension variables: Python gives them their own scope; they bind inside
  the comprehension. A subtree rooted at the comprehension will correctly see
  them as bound.
- Walrus `:=` bindings: may bind in an enclosing scope. Treat by scope kind.
- Class attributes (`self.x = 1`): `self` is a parameter (bound), `x` is an
  attribute (left alone). No renaming confusion.
- Global/nonlocal: `global x` declares the name; the qn maps to the module
  scope, so `binds_inside` returns False inside any nested function.

## Flags / ablations

```python
@dataclass
class CanonicalizerConfig:
    rename_attrs: bool = False          # rename attribute/method names too
    rename_string_literals: bool = True
    rename_numeric_literals: bool = True
    keep_literal_equality: bool = False  # use str_{i} / num_{i} keyed by value
    min_candidate_nodes: int = 8
    min_candidate_depth: int = 3
```

## Tests

`experiments/ast_dedup/tests/test_canonicalize.py`:

1. Two functions differing only in parameter/local names hash to the same
   `canonical_hash`.
2. Two functions differing in operator (`+` vs `-`) do NOT hash the same.
3. Shadowed variables (`x` inside inner function vs outer function) get
   different tokens inside the outer subtree.
4. A free variable used at multiple positions gets the same `free_k` token.
5. Attribute and method names are preserved unless `rename_attrs=True`.
6. String literal equality classes honored only when
   `keep_literal_equality=True`.

## Checklist

- [ ] `experiments/ast_dedup/canonicalize.py` with `CanonicalSubtree`,
      `canonicalize()`, `iter_candidates()`
- [ ] `experiments/ast_dedup/tests/test_canonicalize.py` with cases 1-6
- [ ] Spot-check `PyScopeResolver` qn stability on comprehensions and walrus
      (open question from index.md) — record findings in the file
- [ ] Works against `src/emend/transform.py` end-to-end (smoke test in runner)
