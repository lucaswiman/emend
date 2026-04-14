# Phase 3 — Pluggable hashing / fingerprinting

## Purpose

Keep the hashing mechanism flexible so we can empirically compare exact,
LSH, SimHash, winnowing, and bag-of-subtrees schemes on the same corpus of
`CanonicalSubtree`s from Phase 2.

## Protocols

```python
# experiments/ast_dedup/hashers.py

class Fingerprint(Protocol):
    """One strategy's fingerprint of a single subtree."""
    name: ClassVar[str]

class Hasher(Protocol):
    name: str
    def of(self, sub: CanonicalSubtree) -> Fingerprint: ...

class Index(Protocol):
    """Near-neighbour index for a given Hasher."""
    name: str
    def insert(self, key: SubtreeKey, fp: Fingerprint) -> None: ...
    def query(self, fp: Fingerprint, threshold: float) -> Iterable[tuple[SubtreeKey, float]]: ...

SubtreeKey = tuple[str, int, int]     # (file, start_byte, end_byte)
```

A registry maps a name to a `(Hasher, Index, default_threshold)` triple. The
runner iterates the registry and runs every strategy on every candidate,
producing a per-strategy report in Phase 6.

## Strategies

### 1. Merkle exact

```python
class MerkleHasher:
    name = "merkle_exact"
    def of(self, sub): return sub.canonical_hash
```

Index is a plain `dict[bytes, list[SubtreeKey]]`. Near-dup threshold is
meaningless here — it only reports exact duplicates. This is the ground
truth for "structural exact match after alpha-renaming".

### 2. Shingled MinHash over kind sequence

```python
class KindShingleMinHash:
    name = "kind_shingles_minhash"
    def __init__(self, k=5, num_perm=128):
        self.k, self.num_perm = k, num_perm
    def of(self, sub):
        shingles = {tuple(sub.kind_seq[i:i+self.k])
                    for i in range(len(sub.kind_seq) - self.k + 1)}
        mh = MinHash(num_perm=self.num_perm)
        for s in shingles:
            mh.update("|".join(s).encode())
        return mh
```

Index is `datasketch.MinHashLSH(threshold=0.8, num_perm=128)`. Captures:
reordered statements, inserted/deleted single nodes, similar control flow.
Misses: different statement *kinds* with the same *shape* (e.g. `if` vs
`while`).

### 3. Token + kind shingle MinHash

Same as #2 but shingles are drawn from the interleaved `kind_seq ⊕ token_seq`
pre-order. Captures operator differences that `KindShingleMinHash` misses.
Expected to be strictly more selective.

### 4. SimHash (Charikar)

```python
class SimHasher:
    name = "simhash"
    def __init__(self, f=64):
        self.f = f
    def of(self, sub):
        bits = [0] * self.f
        for tok in sub.token_seq + sub.kind_seq:
            h = int.from_bytes(blake2b(tok.encode(), digest_size=8).digest(), "big")
            for i in range(self.f):
                bits[i] += 1 if (h >> i) & 1 else -1
        fp = 0
        for i, b in enumerate(bits):
            if b > 0:
                fp |= 1 << i
        return fp
```

Index: a classic SimHash band-split — store `f`-bit fingerprints in `b`
bands of `f/b` bits each, query with Hamming distance ≤ `k`. Captures: token
frequency similarity independent of order. Misses: structure changes that
preserve token frequency.

### 5. Winnowing

See Phase 5 — winnowing is primarily a *sequence* fingerprint over statement
hashes, and its natural home is sibling-sequence detection rather than
whole-subtree comparison. We still register it here for completeness, using
winnowed fingerprints of `kind_seq` as a subtree fingerprint. Its real
payoff is Phase 5.

### 6. Bag-of-subtrees MinHash

```python
class BagOfSubtreesMinHash:
    name = "bag_of_subtrees"
    def __init__(self, num_perm=128):
        self.num_perm = num_perm
    def of(self, sub):
        # Multiset of child Merkle hashes at depth ≤ 2. Requires access to
        # the subtree's intermediate Merkle hashes — Phase 2 must expose them.
        mh = MinHash(num_perm=self.num_perm)
        for h in sub.child_merkle_bag:
            mh.update(h)
        return mh
```

This treats a subtree as a bag of its child subtree hashes. Captures: "same
set of helpers, reordered". Needs Phase 2 to emit `child_merkle_bag` as an
additional field — cheap to collect during the raw Merkle pass.

## Comparison driver

```python
@dataclass
class StrategyResult:
    name: str
    fingerprint_count: int
    index_insert_secs: float
    query_secs: float
    duplicate_clusters: list[list[SubtreeKey]]
    near_duplicate_pairs: list[tuple[SubtreeKey, SubtreeKey, float]]
    peak_rss_mb: float

def compare_strategies(corpus: list[CanonicalSubtree],
                       registry: dict[str, tuple[Hasher, Index, float]]
                      ) -> list[StrategyResult]:
    ...
```

The comparison output feeds directly into the Phase 6 markdown report.

## Agreement metric

To answer "does LSH add real value over exact Merkle?", compute:

- `merkle_clusters = clusters produced by MerkleHasher`
- For each other strategy `S`, count:
  - `S_only_pairs` = near-dup pairs reported by `S` that are NOT in any
    Merkle exact cluster
  - `merkle_only_pairs` = exact Merkle duplicates that `S` did not group

The report surfaces the top 10 `S_only_pairs` by size so an agent can
manually judge whether they're real near-duplicates or noise.

## External deps (dev-only, optional)

- `datasketch` (MinHash, MinHashLSH) — falls back to a hand-rolled MinHash
  if unavailable
- `xxhash` — optional fast hash, defaults to `hashlib.blake2b`

Installed into `.venv` via `uv pip install datasketch xxhash`. Not added to
`pyproject.toml`; the experiment module gracefully degrades with a warning
if deps are missing.

## Tests

`experiments/ast_dedup/tests/test_hashers.py`:

1. `MerkleHasher` reports two identical `CanonicalSubtree`s as duplicates.
2. `KindShingleMinHash.of` produces identical signatures for identical
   inputs, different signatures for meaningfully different inputs, and ≥ 0.8
   Jaccard for a 2-line insertion.
3. `SimHasher` produces equal hashes for equal token bags and differs by
   ≤ 4 bits for a single-token substitution in a 200-token sequence.
4. Agreement metric: on a hand-built corpus of 10 pairs (5 exact, 3
   near-dupes, 2 unrelated), every strategy reports the 5 exacts, and at
   least `KindShingleMinHash` finds the 3 near-dupes.
5. Registry sanity check: all registered strategies implement the
   `Hasher`/`Index` protocols via `typing.runtime_checkable`.

## Checklist

- [ ] `experiments/ast_dedup/hashers.py` with the protocol + 5 strategies
- [ ] Registry dict with default thresholds
- [ ] `tests/test_hashers.py` with cases 1-5
- [ ] Graceful degradation when `datasketch`/`xxhash` missing
