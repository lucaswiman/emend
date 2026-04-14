"""Phase 3 — Pluggable hashing / fingerprinting strategies.

Each strategy implements a ``Hasher`` that produces a ``Fingerprint`` from a
``CanonicalSubtree`` and a paired ``Index`` for near-neighbour lookup.

External deps (optional):
    - ``datasketch`` — MinHash and MinHashLSH; gracefully falls back to
      a hand-rolled MinHash when unavailable.
    - ``xxhash`` — fast hashing; falls back to ``hashlib.blake2b``.

All five strategies work without any external dependencies.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import ClassVar, Iterable, Iterator

from experiments.ast_dedup.canonicalize import CanonicalSubtree

# ---------------------------------------------------------------------------
# Optional external deps
# ---------------------------------------------------------------------------

try:
    from datasketch import MinHash as _DatasketchMinHash
    from datasketch import MinHashLSH as _DatasketchMinHashLSH

    _DATASKETCH_AVAILABLE = True
except ImportError:
    _DATASKETCH_AVAILABLE = False
    warnings.warn(
        "datasketch not installed; falling back to hand-rolled MinHash "
        "(slower, no LSH — near-neighbour queries iterate all entries).",
        ImportWarning,
        stacklevel=1,
    )

try:
    import xxhash as _xxhash

    def _hash_bytes(data: bytes, seed: int = 0) -> int:
        return _xxhash.xxh64(data, seed=seed).intdigest()

except ImportError:
    def _hash_bytes(data: bytes, seed: int = 0) -> int:  # type: ignore[misc]
        """Fall back to blake2b if xxhash is unavailable."""
        h = blake2b(seed.to_bytes(8, "little") + data, digest_size=8)
        return int.from_bytes(h.digest(), "big")


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

SubtreeKey = tuple[str, int, int]  # (file, start_byte, end_byte)

# ---------------------------------------------------------------------------
# Fallback MinHash implementation
# ---------------------------------------------------------------------------


class _FallbackMinHash:
    """Hand-rolled MinHash that stores the ``num_perm`` smallest hashes seen.

    This is a *simplified* MinHash: each permutation is simulated by a
    distinct hash seed.  The resulting Jaccard estimate is deterministic for
    fixed inputs, which makes it suitable for tests that check exact
    thresholds.

    Limitations:
    - No band-split LSH — the paired ``NaiveMinHashIndex`` does a linear scan.
    - Slightly less statistically accurate than the datasketch implementation
      for large shingle sets, but correct for small test corpora.
    """

    def __init__(self, num_perm: int = 128) -> None:
        self.num_perm = num_perm
        # For permutation i we track the minimum hash seen so far.
        self._mins: list[int] = [2**64 - 1] * num_perm

    def update(self, b: bytes) -> None:
        for i in range(self.num_perm):
            h = _hash_bytes(b, seed=i)
            if h < self._mins[i]:
                self._mins[i] = h

    def jaccard(self, other: "_FallbackMinHash") -> float:
        assert self.num_perm == other.num_perm
        eq = sum(a == b for a, b in zip(self._mins, other._mins))
        return eq / self.num_perm


# ---------------------------------------------------------------------------
# MinHash factory — returns datasketch or fallback depending on availability
# ---------------------------------------------------------------------------


def _make_minhash(num_perm: int = 128):
    """Return a MinHash object (datasketch or fallback)."""
    if _DATASKETCH_AVAILABLE:
        return _DatasketchMinHash(num_perm=num_perm)
    return _FallbackMinHash(num_perm=num_perm)


def _minhash_jaccard(a, b) -> float:
    """Compute Jaccard estimate between two MinHash objects."""
    if _DATASKETCH_AVAILABLE:
        return a.jaccard(b)
    return a.jaccard(b)  # same API for fallback


# ---------------------------------------------------------------------------
# Protocols (runtime_checkable so tests can use hasattr / isinstance)
# ---------------------------------------------------------------------------

from typing import Protocol, runtime_checkable


@runtime_checkable
class Fingerprint(Protocol):
    """One strategy's fingerprint of a single subtree."""

    name: ClassVar[str]


@runtime_checkable
class Hasher(Protocol):
    """Produces a Fingerprint from a CanonicalSubtree."""

    name: str

    def of(self, sub: CanonicalSubtree) -> object:
        ...


@runtime_checkable
class Index(Protocol):
    """Near-neighbour index paired with a Hasher."""

    name: str

    def insert(self, key: SubtreeKey, fp: object) -> None:
        ...

    def query(self, fp: object, threshold: float) -> Iterable[tuple[SubtreeKey, float]]:
        ...


# ---------------------------------------------------------------------------
# 1. Merkle exact
# ---------------------------------------------------------------------------


class MerkleHasher:
    """Exact structural dedup via the alpha-renamed Merkle hash from Phase 2."""

    name: str = "merkle_exact"

    def of(self, sub: CanonicalSubtree) -> bytes:
        return sub.canonical_hash


class MerkleIndex:
    """Plain dict: hash → list of SubtreeKeys."""

    name: str = "merkle_exact"

    def __init__(self) -> None:
        self._store: dict[bytes, list[SubtreeKey]] = {}

    def insert(self, key: SubtreeKey, fp: bytes) -> None:
        self._store.setdefault(fp, []).append(key)

    def query(self, fp: bytes, threshold: float = 1.0) -> Iterator[tuple[SubtreeKey, float]]:
        """Threshold is ignored — only exact matches are returned."""
        for k in self._store.get(fp, []):
            yield k, 1.0

    def clusters(self) -> list[list[SubtreeKey]]:
        """Return groups of keys that share the same hash (size >= 2)."""
        return [v for v in self._store.values() if len(v) >= 2]


# ---------------------------------------------------------------------------
# 2. Shingled MinHash over kind sequence
# ---------------------------------------------------------------------------


class KindShingleMinHash:
    """MinHash over k-gram shingles of ``kind_seq``.

    Captures reordered statements, inserted/deleted single nodes, similar
    control flow.  Misses different statement *kinds* with the same *shape*.
    """

    name: str = "kind_shingles_minhash"

    def __init__(self, k: int = 5, num_perm: int = 128) -> None:
        self.k = k
        self.num_perm = num_perm

    def of(self, sub: CanonicalSubtree):
        shingles = {
            tuple(sub.kind_seq[i : i + self.k])
            for i in range(len(sub.kind_seq) - self.k + 1)
        }
        mh = _make_minhash(self.num_perm)
        for s in sorted(shingles):  # deterministic order for fallback
            mh.update("|".join(s).encode())
        return mh


class MinHashIndex:
    """Near-neighbour index for MinHash fingerprints.

    Uses ``datasketch.MinHashLSH`` when available; falls back to a naive
    linear scan over all stored fingerprints.
    """

    name: str = "minhash"

    def __init__(self, threshold: float = 0.8, num_perm: int = 128) -> None:
        self.threshold = threshold
        self.num_perm = num_perm
        if _DATASKETCH_AVAILABLE:
            self._lsh = _DatasketchMinHashLSH(threshold=threshold, num_perm=num_perm)
            self._store: dict[str, tuple[SubtreeKey, object]] = {}
            self._counter = 0
        else:
            self._naive: list[tuple[SubtreeKey, object]] = []

    def insert(self, key: SubtreeKey, fp) -> None:
        if _DATASKETCH_AVAILABLE:
            label = str(self._counter)
            self._counter += 1
            self._lsh.insert(label, fp)
            self._store[label] = (key, fp)
        else:
            self._naive.append((key, fp))

    def query(self, fp, threshold: float | None = None) -> Iterator[tuple[SubtreeKey, float]]:
        thr = threshold if threshold is not None else self.threshold
        if _DATASKETCH_AVAILABLE:
            results = self._lsh.query(fp)
            for label in results:
                stored_key, stored_fp = self._store[label]
                j = fp.jaccard(stored_fp)
                if j >= thr:
                    yield stored_key, j
        else:
            for stored_key, stored_fp in self._naive:
                j = _minhash_jaccard(fp, stored_fp)
                if j >= thr:
                    yield stored_key, j


# ---------------------------------------------------------------------------
# 3. Token + kind shingle MinHash
# ---------------------------------------------------------------------------


class KindTokenShingleMinHash:
    """MinHash over k-gram shingles of the interleaved ``kind_seq ⊕ token_seq``.

    Strictly more selective than ``KindShingleMinHash`` because it also captures
    operator/identifier differences.
    """

    name: str = "kind_token_shingles_minhash"

    def __init__(self, k: int = 5, num_perm: int = 128) -> None:
        self.k = k
        self.num_perm = num_perm

    def of(self, sub: CanonicalSubtree):
        interleaved = list(sub.kind_seq) + list(sub.token_seq)
        shingles = {
            tuple(interleaved[i : i + self.k])
            for i in range(len(interleaved) - self.k + 1)
        }
        mh = _make_minhash(self.num_perm)
        for s in sorted(shingles):
            mh.update("|".join(s).encode())
        return mh


# KindTokenShingleMinHash reuses MinHashIndex — instantiated per use.

# ---------------------------------------------------------------------------
# 4. SimHash (Charikar)
# ---------------------------------------------------------------------------


class SimHasher:
    """Charikar SimHash over ``token_seq + kind_seq``.

    Returns an ``int`` fingerprint of ``f`` bits.
    Captures token-frequency similarity independent of order.
    """

    name: str = "simhash"

    def __init__(self, f: int = 64) -> None:
        self.f = f

    def of(self, sub: CanonicalSubtree) -> int:
        bits = [0] * self.f
        for tok in sub.token_seq + sub.kind_seq:
            h = int.from_bytes(
                blake2b(tok.encode(), digest_size=8).digest(), "big"
            )
            for i in range(self.f):
                bits[i] += 1 if (h >> i) & 1 else -1
        fp = 0
        for i, b in enumerate(bits):
            if b > 0:
                fp |= 1 << i
        return fp


def _hamming(a: int, b: int) -> int:
    """Count differing bits between two integers."""
    return bin(a ^ b).count("1")


class SimHashIndex:
    """Band-split SimHash index.

    Splits the ``f``-bit fingerprint into ``b`` bands of ``f // b`` bits.
    A pair is a candidate if any band matches (union of band collisions).
    Then Hamming distance is verified against the threshold.

    Threshold here is expressed as a maximum Hamming distance fraction
    (``hamming / f <= 1 - threshold``).
    """

    name: str = "simhash"

    def __init__(self, f: int = 64, b: int = 8, threshold: float = 0.9) -> None:
        self.f = f
        self.b = b
        self.threshold = threshold
        self._band_width = f // b
        # band_index[band_idx][band_value] = list of (SubtreeKey, full_fp)
        self._band_index: list[dict[int, list[tuple[SubtreeKey, int]]]] = [
            {} for _ in range(b)
        ]
        self._all: list[tuple[SubtreeKey, int]] = []

    def insert(self, key: SubtreeKey, fp: int) -> None:
        self._all.append((key, fp))
        for band_idx in range(self.b):
            shift = band_idx * self._band_width
            mask = (1 << self._band_width) - 1
            band_val = (fp >> shift) & mask
            self._band_index[band_idx].setdefault(band_val, []).append((key, fp))

    def query(self, fp: int, threshold: float | None = None) -> Iterator[tuple[SubtreeKey, float]]:
        thr = threshold if threshold is not None else self.threshold
        max_hamming = int(self.f * (1 - thr))
        seen: set[SubtreeKey] = set()
        for band_idx in range(self.b):
            shift = band_idx * self._band_width
            mask = (1 << self._band_width) - 1
            band_val = (fp >> shift) & mask
            for candidate_key, candidate_fp in self._band_index[band_idx].get(
                band_val, []
            ):
                if candidate_key in seen:
                    continue
                seen.add(candidate_key)
                hd = _hamming(fp, candidate_fp)
                if hd <= max_hamming:
                    # Convert Hamming distance to a similarity score in [0, 1]
                    sim = 1.0 - hd / self.f
                    yield candidate_key, sim


# ---------------------------------------------------------------------------
# 5. Bag-of-subtrees MinHash
# ---------------------------------------------------------------------------


class BagOfSubtreesMinHash:
    """MinHash over the multiset of child Merkle hashes (``child_merkle_bag``).

    Treats a subtree as a bag of its child subtree hashes (depth <= 2).
    Captures "same set of helpers, reordered".
    """

    name: str = "bag_of_subtrees"

    def __init__(self, num_perm: int = 128) -> None:
        self.num_perm = num_perm

    def of(self, sub: CanonicalSubtree):
        mh = _make_minhash(self.num_perm)
        for h in sub.child_merkle_bag:
            mh.update(h)
        return mh


# BagOfSubtreesMinHash also reuses MinHashIndex.

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Registry maps strategy name → (Hasher instance, Index factory fn, default threshold).
# Stored as (hasher_class, index_class, default_threshold) per spec; instances
# are created on demand in compare_strategies.

REGISTRY: dict[str, tuple[type, type, float]] = {
    "merkle_exact": (MerkleHasher, MerkleIndex, 1.0),
    "kind_shingles_minhash": (KindShingleMinHash, MinHashIndex, 0.8),
    "kind_token_shingles_minhash": (KindTokenShingleMinHash, MinHashIndex, 0.8),
    "simhash": (SimHasher, SimHashIndex, 0.9),
    "bag_of_subtrees": (BagOfSubtreesMinHash, MinHashIndex, 0.8),
}

# ---------------------------------------------------------------------------
# Comparison driver
# ---------------------------------------------------------------------------


@dataclass
class StrategyResult:
    """Per-strategy summary from ``compare_strategies``."""

    name: str
    fingerprint_count: int
    index_insert_secs: float
    query_secs: float
    duplicate_clusters: list[list[SubtreeKey]]
    near_duplicate_pairs: list[tuple[SubtreeKey, SubtreeKey, float]]
    peak_rss_mb: float  # TODO: measure via resource.getrusage(RUSAGE_SELF)


def _make_index(index_cls: type, strategy_name: str, threshold: float, num_perm: int = 128) -> object:
    """Instantiate an index with the right kwargs for each strategy type."""
    if strategy_name == "merkle_exact":
        return index_cls()
    elif strategy_name == "simhash":
        return index_cls(threshold=threshold)
    else:
        return index_cls(threshold=threshold, num_perm=num_perm)


def _make_hasher(hasher_cls: type, strategy_name: str, num_perm: int = 128) -> object:
    """Instantiate a hasher with the right kwargs."""
    if strategy_name == "merkle_exact":
        return hasher_cls()
    elif strategy_name == "simhash":
        return hasher_cls()
    else:
        return hasher_cls(num_perm=num_perm)


def _extract_clusters_and_pairs(
    index: object,
    fingerprints: list[tuple[SubtreeKey, object]],
    threshold: float,
    strategy_name: str,
) -> tuple[list[list[SubtreeKey]], list[tuple[SubtreeKey, SubtreeKey, float]]]:
    """Extract duplicate clusters and near-dup pairs from an index."""
    if strategy_name == "merkle_exact":
        clusters = index.clusters()  # type: ignore[attr-defined]
        pairs: list[tuple[SubtreeKey, SubtreeKey, float]] = []
        for cluster in clusters:
            for i in range(len(cluster)):
                for j in range(i + 1, len(cluster)):
                    pairs.append((cluster[i], cluster[j], 1.0))
        return clusters, pairs

    # For MinHash/SimHash strategies: query each fingerprint against the index.
    seen_pairs: set[frozenset] = set()
    pairs = []
    clusters_dict: dict[int, list[SubtreeKey]] = {}
    cluster_id: dict[SubtreeKey, int] = {}
    next_id = [0]

    for key, fp in fingerprints:
        results = list(index.query(fp, threshold))  # type: ignore[attr-defined]
        for other_key, sim in results:
            if other_key == key:
                continue
            pair_set = frozenset([key, other_key])
            if pair_set in seen_pairs:
                continue
            seen_pairs.add(pair_set)
            pairs.append((key, other_key, sim))

            # Union-find style cluster merging (simple version)
            if key not in cluster_id and other_key not in cluster_id:
                cid = next_id[0]
                next_id[0] += 1
                cluster_id[key] = cid
                cluster_id[other_key] = cid
                clusters_dict[cid] = [key, other_key]
            elif key in cluster_id and other_key not in cluster_id:
                cid = cluster_id[key]
                cluster_id[other_key] = cid
                clusters_dict[cid].append(other_key)
            elif other_key in cluster_id and key not in cluster_id:
                cid = cluster_id[other_key]
                cluster_id[key] = cid
                clusters_dict[cid].append(key)
            # else: both already assigned (possibly different clusters — ignore for simplicity)

    return list(clusters_dict.values()), pairs


def compare_strategies(
    corpus: list[CanonicalSubtree],
    registry: dict[str, tuple[type, type, float]] | None = None,
) -> list[StrategyResult]:
    """Run every strategy in ``registry`` on ``corpus`` and return results.

    Parameters
    ----------
    corpus:
        List of CanonicalSubtrees to compare (all pairs).
    registry:
        Strategy registry; defaults to the module-level ``REGISTRY``.
    """
    if registry is None:
        registry = REGISTRY

    results: list[StrategyResult] = []

    for strategy_name, (hasher_cls, index_cls, default_threshold) in registry.items():
        hasher = _make_hasher(hasher_cls, strategy_name)
        index = _make_index(index_cls, strategy_name, default_threshold)

        # Compute fingerprints
        fingerprints: list[tuple[SubtreeKey, object]] = []
        for sub in corpus:
            key: SubtreeKey = (sub.file, sub.start_byte, sub.end_byte)
            fp = hasher.of(sub)  # type: ignore[attr-defined]
            fingerprints.append((key, fp))

        # Insert into index
        t0 = time.perf_counter()
        for key, fp in fingerprints:
            index.insert(key, fp)  # type: ignore[attr-defined]
        insert_secs = time.perf_counter() - t0

        # Query index for near-duplicates
        t1 = time.perf_counter()
        clusters, pairs = _extract_clusters_and_pairs(
            index, fingerprints, default_threshold, strategy_name
        )
        query_secs = time.perf_counter() - t1

        results.append(
            StrategyResult(
                name=strategy_name,
                fingerprint_count=len(fingerprints),
                index_insert_secs=insert_secs,
                query_secs=query_secs,
                duplicate_clusters=clusters,
                near_duplicate_pairs=pairs,
                peak_rss_mb=0.0,  # TODO: measure via resource.getrusage(RUSAGE_SELF)
            )
        )

    return results


__all__ = [
    "CanonicalSubtree",
    "SubtreeKey",
    "Fingerprint",
    "Hasher",
    "Index",
    "MerkleHasher",
    "MerkleIndex",
    "KindShingleMinHash",
    "KindTokenShingleMinHash",
    "SimHasher",
    "SimHashIndex",
    "BagOfSubtreesMinHash",
    "MinHashIndex",
    "REGISTRY",
    "StrategyResult",
    "compare_strategies",
]
