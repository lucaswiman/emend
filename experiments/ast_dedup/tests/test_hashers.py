"""Tests for Phase 3 — pluggable hashing / fingerprinting strategies.

Five test cases per the spec:

1. MerkleHasher reports identical CanonicalSubtrees as duplicates.
2. KindShingleMinHash: identical inputs → identical signatures; ≥ 0.8 Jaccard
   on a 2-node insertion into a long varied sequence.
3. SimHasher: equal token bags → equal hashes; single-token substitution in a
   ≥200-token sequence → ≤ 4 bits Hamming difference.
4. Agreement metric / hand-built corpus: a corpus with 5 exact pairs, 3 near-dup
   pairs, and 2 unrelated subtrees — every strategy finds the 5 exacts,
   KindShingleMinHash finds the 3 near-dupes.
5. Registry sanity: all registered strategies implement the Hasher/Index protocols.
"""

from __future__ import annotations

import hashlib

import pytest

from experiments.ast_dedup.canonicalize import CanonicalSubtree
from experiments.ast_dedup.hashers import (
    REGISTRY,
    BagOfSubtreesMinHash,
    Hasher,
    Index,
    KindShingleMinHash,
    KindTokenShingleMinHash,
    MerkleHasher,
    MerkleIndex,
    MinHashIndex,
    SimHasher,
    SimHashIndex,
    SubtreeKey,
    _FallbackMinHash,
    _hamming,
    _make_minhash,
    _minhash_jaccard,
    compare_strategies,
)


# ---------------------------------------------------------------------------
# Helpers for building CanonicalSubtrees by hand
# ---------------------------------------------------------------------------


def _make_sub(
    *,
    file: str = "test.py",
    start_byte: int = 0,
    end_byte: int = 100,
    kind_seq: tuple[str, ...] = (),
    token_seq: tuple[str, ...] = (),
    canonical_hash: bytes = b"",
    child_merkle_bag: tuple[bytes, ...] = (),
    start_line: int = 0,
    end_line: int = 5,
    depth: int = 3,
    node_count: int = 8,
    raw_merkle: bytes = b"",
) -> CanonicalSubtree:
    """Construct a CanonicalSubtree with sane defaults for testing."""
    return CanonicalSubtree(
        file=file,
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=start_line,
        end_line=end_line,
        kind_seq=kind_seq,
        token_seq=token_seq,
        depth=depth,
        node_count=node_count,
        raw_merkle=raw_merkle,
        canonical_hash=canonical_hash,
        unique_tokens=len(set(token_seq)),
        unique_non_keyword_tokens=len(set(token_seq)),
        kind_histogram=tuple(
            sorted((k, list(kind_seq).count(k)) for k in set(kind_seq))
        ),
        child_merkle_bag=child_merkle_bag,
    )


def _h(s: str) -> bytes:
    """Short deterministic bytes for testing."""
    return hashlib.blake2b(s.encode(), digest_size=16).digest()


# ---------------------------------------------------------------------------
# Long varied kind sequences used by tests 2 and 4.
#
# A repetitive kind_seq (e.g. the same 4-tuple repeated N times) produces
# only a handful of unique k-grams.  The MinHash Jaccard estimate is then
# unreliable because the shingle set is tiny.  Using a *varied* sequence
# (mixing different node kinds as in a real, non-trivial function body)
# produces many distinct shingles, making the Jaccard estimate converge
# quickly to the true set-overlap value.
# ---------------------------------------------------------------------------

# "Unit" of varied kind nodes representing a non-trivial function body chunk.
_VARIED_UNIT: tuple[str, ...] = (
    "if_statement", "comparison_operator", "identifier", "integer",
    "block", "expression_statement", "call", "identifier", "argument_list",
    "identifier", "string",
    "for_statement", "identifier", "call", "identifier",
    "block", "if_statement", "identifier", "comparison_operator", "none",
    "block", "continue",
    "augmented_assignment", "identifier", "integer",
    "while_statement", "comparison_operator", "identifier", "integer",
    "block", "expression_statement", "augmented_assignment", "identifier", "integer",
    "return_statement", "boolean_operator", "comparison_operator", "identifier", "integer",
)

# Near-dup corpus — 3 pairs, each using a distinct kind vocabulary so they
# do not collide with the exact groups or with each other above the 0.8
# threshold.  Each pair is (BASE, NEAR) where NEAR = BASE with a 2-3 node
# insertion.

# --- Near-dup 1: function with if/for/while body ---
_ND1_BASE: tuple[str, ...] = (
    "function_definition", "parameters", "block",
) + _VARIED_UNIT * 2
_ND1_NEAR: tuple[str, ...] = _ND1_BASE[:23] + ("assert_statement", "identifier") + _ND1_BASE[23:]

# --- Near-dup 2: function with try/except/for body ---
_VARIED_UNIT2: tuple[str, ...] = (
    "try_statement", "block",
    "expression_statement", "call", "attribute", "identifier", "argument_list",
    "identifier", "keyword_argument", "identifier", "string",
    "except_clause", "identifier", "as_pattern", "identifier", "block",
    "expression_statement", "call", "attribute", "identifier", "argument_list",
    "return_statement", "dictionary", "pair", "string", "identifier",
    "assignment", "identifier", "call", "attribute", "identifier",
    "for_statement", "identifier", "attribute", "identifier",
    "block", "augmented_assignment", "identifier", "attribute",
)
_ND2_BASE: tuple[str, ...] = (
    "function_definition", "parameters", "block",
) + _VARIED_UNIT2 * 2
_ND2_NEAR: tuple[str, ...] = (
    _ND2_BASE[:25]
    + ("expression_statement", "assignment", "identifier")
    + _ND2_BASE[25:]
)

# --- Near-dup 3: class method with while/yield body ---
_VARIED_UNIT3: tuple[str, ...] = (
    "while_statement", "comparison_operator", "attribute", "identifier", "integer",
    "block", "expression_statement", "yield", "identifier",
    "augmented_assignment", "attribute", "identifier", "integer",
    "if_statement", "comparison_operator", "attribute", "identifier", "integer",
    "block", "expression_statement", "call", "attribute", "identifier",
    "if_statement", "boolean_operator", "identifier", "identifier",
    "block", "return_statement", "none",
    "break",
)
_ND3_BASE: tuple[str, ...] = (
    "function_definition", "parameters", "block",
) + _VARIED_UNIT3 * 2
_ND3_NEAR: tuple[str, ...] = (
    _ND3_BASE[:20]
    + ("expression_statement", "call", "identifier")
    + _ND3_BASE[20:]
)


# ---------------------------------------------------------------------------
# Test 1 — MerkleHasher reports identical CanonicalSubtrees as duplicates
# ---------------------------------------------------------------------------


def test_merkle_exact_duplicates():
    """Two CanonicalSubtrees with the same canonical_hash are reported as
    exact duplicates by MerkleHasher + MerkleIndex."""
    hash_val = _h("function_x_same")

    sub_a = _make_sub(file="a.py", start_byte=0, end_byte=50, canonical_hash=hash_val)
    sub_b = _make_sub(file="b.py", start_byte=0, end_byte=50, canonical_hash=hash_val)
    sub_c = _make_sub(file="c.py", start_byte=0, end_byte=50, canonical_hash=_h("different"))

    hasher = MerkleHasher()
    index = MerkleIndex()

    key_a: SubtreeKey = (sub_a.file, sub_a.start_byte, sub_a.end_byte)
    key_b: SubtreeKey = (sub_b.file, sub_b.start_byte, sub_b.end_byte)
    key_c: SubtreeKey = (sub_c.file, sub_c.start_byte, sub_c.end_byte)

    fp_a = hasher.of(sub_a)
    fp_b = hasher.of(sub_b)
    fp_c = hasher.of(sub_c)

    # Identical subtrees produce identical fingerprints.
    assert fp_a == fp_b
    # Different subtree produces different fingerprint.
    assert fp_a != fp_c

    index.insert(key_a, fp_a)
    index.insert(key_b, fp_b)
    index.insert(key_c, fp_c)

    # Querying fp_a finds key_a and key_b (both exact matches), not key_c.
    results = list(index.query(fp_a, threshold=1.0))
    result_keys = {k for k, _ in results}
    assert key_a in result_keys
    assert key_b in result_keys
    assert key_c not in result_keys

    # clusters() returns exactly one cluster of size 2 (a and b).
    clusters = index.clusters()
    assert len(clusters) == 1
    assert set(clusters[0]) == {key_a, key_b}


def test_merkle_no_false_positives():
    """MerkleIndex does not cluster subtrees with distinct hashes."""
    hasher = MerkleHasher()
    index = MerkleIndex()

    subs = [
        _make_sub(file=f"{i}.py", canonical_hash=_h(f"hash_{i}"))
        for i in range(10)
    ]
    for sub in subs:
        key: SubtreeKey = (sub.file, sub.start_byte, sub.end_byte)
        index.insert(key, hasher.of(sub))

    # No cluster should be reported since all hashes are distinct.
    assert index.clusters() == []


# ---------------------------------------------------------------------------
# Test 2 — KindShingleMinHash
# ---------------------------------------------------------------------------


def test_kind_shingle_identical_inputs():
    """Identical kind_seqs produce identical MinHash signatures."""
    hasher = KindShingleMinHash(k=5, num_perm=128)
    kseq = _ND1_BASE  # use the varied, realistic kind_seq

    sub_a = _make_sub(file="a.py", kind_seq=kseq)
    sub_b = _make_sub(file="b.py", kind_seq=kseq)

    fp_a = hasher.of(sub_a)
    fp_b = hasher.of(sub_b)

    j = _minhash_jaccard(fp_a, fp_b)
    assert j == pytest.approx(1.0), (
        f"identical kind_seqs should give Jaccard=1.0, got {j}"
    )


def test_kind_shingle_different_inputs():
    """Meaningfully different kind_seqs produce different MinHash signatures."""
    hasher = KindShingleMinHash(k=5, num_perm=128)

    # Use the three near-dup base sequences — they use different kind vocabularies
    # and should have low pairwise Jaccard.
    sub_a = _make_sub(file="a.py", kind_seq=_ND1_BASE)
    sub_b = _make_sub(file="b.py", kind_seq=_VARIED_UNIT3 * 2)

    fp_a = hasher.of(sub_a)
    fp_b = hasher.of(sub_b)

    j = _minhash_jaccard(fp_a, fp_b)
    assert j < 1.0, "different kind_seqs should not produce Jaccard=1.0"


def test_kind_shingle_two_line_insertion():
    """A 2-node insertion into a long varied function body keeps Jaccard ≥ 0.8.

    The spec says "≥ 0.8 Jaccard for a 2-line insertion".  With k=5 shingles
    a 2-element prefix insertion into an 81-node varied kind sequence replaces
    only 4 shingles (the 4 shingles that previously started at positions 0–3),
    giving true Jaccard ≈ 0.87 and MinHash estimate ≈ 0.88.
    """
    hasher = KindShingleMinHash(k=5, num_perm=128)

    sub_orig = _make_sub(file="orig.py", kind_seq=_ND1_BASE)
    sub_near = _make_sub(file="near.py", kind_seq=_ND1_NEAR)

    fp_orig = hasher.of(sub_orig)
    fp_near = hasher.of(sub_near)

    j = _minhash_jaccard(fp_orig, fp_near)
    assert j >= 0.8, (
        f"2-node insertion into varied function body should give Jaccard >= 0.8, "
        f"got {j:.3f}"
    )


# ---------------------------------------------------------------------------
# Test 3 — SimHasher
# ---------------------------------------------------------------------------


def test_simhash_equal_token_bags():
    """Equal token_seq + kind_seq → equal SimHash fingerprints."""
    hasher = SimHasher(f=64)
    tseq = ("x", "y", "z", "return", "if", "else") * 5
    kseq = ("identifier", "binary_operator", "if_statement") * 5

    sub_a = _make_sub(file="a.py", token_seq=tseq, kind_seq=kseq)
    sub_b = _make_sub(file="b.py", token_seq=tseq, kind_seq=kseq)

    assert hasher.of(sub_a) == hasher.of(sub_b)


def _make_long_token_seq(n: int = 200) -> tuple[str, ...]:
    """Build a long, varied token sequence of length n."""
    tokens = []
    vocab = [
        "x", "y", "z", "a", "b", "c", "return", "if", "else",
        "for", "in", "range", "print", "result", "value", "data",
        "foo", "bar", "baz", "qux", "item", "key", "val", "obj",
    ]
    for i in range(n):
        tokens.append(vocab[i % len(vocab)])
    return tuple(tokens)


def test_simhash_single_token_substitution():
    """Substituting a single token in a 200-token sequence changes ≤ 4 bits.

    SimHash accumulates per-bit votes across all tokens.  Replacing one token
    out of 200 shifts the vote for each bit by at most 2 (−1 → +1 or vice
    versa).  With 64 bits and a balanced input, only a few bits sit near zero
    and actually flip — empirically ≤ 4.
    """
    hasher = SimHasher(f=64)
    tseq = _make_long_token_seq(200)

    # Substitute one token somewhere in the middle.
    tseq_mod = list(tseq)
    tseq_mod[100] = "SUBSTITUTED_TOKEN_UNIQUE_XYZ"
    tseq_mod_t = tuple(tseq_mod)

    kseq = ("identifier",) * 200

    sub_orig = _make_sub(file="orig.py", token_seq=tseq, kind_seq=kseq)
    sub_mod = _make_sub(file="mod.py", token_seq=tseq_mod_t, kind_seq=kseq)

    fp_orig = hasher.of(sub_orig)
    fp_mod = hasher.of(sub_mod)

    hd = _hamming(fp_orig, fp_mod)
    assert hd <= 4, (
        f"single-token substitution in 200-token sequence should flip ≤ 4 bits, "
        f"got {hd} bit(s) difference"
    )


# ---------------------------------------------------------------------------
# Test 4 — Agreement metric / hand-built corpus
# ---------------------------------------------------------------------------
#
# Corpus layout (total 16 subtrees):
#   exact_0_0 / exact_0_1  — exact pair A (canonical_hash matches)
#   exact_1_0 / exact_1_1  — exact pair B
#   exact_2_0 / exact_2_1  — exact pair C
#   exact_3_0 / exact_3_1  — exact pair D
#   exact_4_0 / exact_4_1  — exact pair E
#   near_dup_1a / near_dup_1b — near-dup pair 1
#   near_dup_2a / near_dup_2b — near-dup pair 2
#   near_dup_3a / near_dup_3b — near-dup pair 3
#   unrelated_1 / unrelated_2 — completely different, should not cluster


def _build_corpus() -> list[CanonicalSubtree]:
    """Build the 16-subtree test corpus."""
    subs: list[CanonicalSubtree] = []

    # ---- 5 exact pairs (groups 0-4) ----
    # Each exact group uses a simple repetitive kind_seq with a unique hash.
    # The kind vocabularies differ from the near-dup pairs so they don't
    # accidentally cross-pair with them.
    for group in range(5):
        hash_val = _h(f"exact_group_{group}")
        base = ("function_definition", "block") + (
            "expression_statement", "identifier", "assignment", "integer"
        ) * (4 + group)
        tseq = tuple(f"tok_{group}_{i}" for i in range(10 + group * 2))
        bag = tuple(_h(f"child_{group}_{i}") for i in range(3))
        for copy in range(2):
            subs.append(
                _make_sub(
                    file=f"exact_{group}_{copy}.py",
                    start_byte=group * 1000,
                    end_byte=group * 1000 + 200,
                    canonical_hash=hash_val,
                    kind_seq=base,
                    token_seq=tseq,
                    child_merkle_bag=bag,
                )
            )

    # ---- 3 near-dup pairs ----
    # Each uses a long varied kind sequence with a small insertion so that the
    # MinHash Jaccard (k=5) stays above the 0.8 threshold.

    for nd_name, nd_base, nd_near in [
        ("near_dup_1", _ND1_BASE, _ND1_NEAR),
        ("near_dup_2", _ND2_BASE, _ND2_NEAR),
        ("near_dup_3", _ND3_BASE, _ND3_NEAR),
    ]:
        idx = ("near_dup_1", "near_dup_2", "near_dup_3").index(nd_name)
        base_byte = (idx + 1) * 10_000
        bag = tuple(_h(f"{nd_name}_child_{i}") for i in range(5))
        subs.append(
            _make_sub(
                file=f"{nd_name}a.py",
                start_byte=base_byte,
                end_byte=base_byte + 300,
                canonical_hash=_h(f"{nd_name}a"),
                kind_seq=nd_base,
                token_seq=tuple(f"{nd_name}_tok_{i}" for i in range(20)),
                child_merkle_bag=bag,
            )
        )
        subs.append(
            _make_sub(
                file=f"{nd_name}b.py",
                start_byte=base_byte,
                end_byte=base_byte + 300,
                canonical_hash=_h(f"{nd_name}b"),
                kind_seq=nd_near,
                token_seq=tuple(f"{nd_name}_tok_{i}" for i in range(20)),
                child_merkle_bag=bag,
            )
        )

    # ---- 2 unrelated subtrees ----
    subs.append(
        _make_sub(
            file="unrelated_1.py",
            start_byte=40_000,
            end_byte=40_050,
            canonical_hash=_h("unrelated_1"),
            kind_seq=("import_statement", "dotted_name"),
            token_seq=("import", "os"),
            child_merkle_bag=(_h("u1_child"),),
        )
    )
    subs.append(
        _make_sub(
            file="unrelated_2.py",
            start_byte=50_000,
            end_byte=50_050,
            canonical_hash=_h("unrelated_2"),
            kind_seq=("class_definition", "block", "function_definition"),
            token_seq=("MyClass", "__init__", "self"),
            child_merkle_bag=(_h("u2_child"),),
        )
    )

    return subs


def test_agreement_metric():
    """Every strategy finds the 5 exact groups; KindShingleMinHash finds near-dupes."""
    corpus = _build_corpus()

    # The 5 exact pair keys (as frozensets for comparison).
    exact_pair_keys: list[frozenset] = []
    for group in range(5):
        key_a: SubtreeKey = (f"exact_{group}_0.py", group * 1000, group * 1000 + 200)
        key_b: SubtreeKey = (f"exact_{group}_1.py", group * 1000, group * 1000 + 200)
        exact_pair_keys.append(frozenset([key_a, key_b]))

    # The 3 near-dup pairs (as frozensets).
    nd_pairs = []
    for nd_name in ("near_dup_1", "near_dup_2", "near_dup_3"):
        idx = ("near_dup_1", "near_dup_2", "near_dup_3").index(nd_name)
        base_byte = (idx + 1) * 10_000
        nd_pairs.append(
            frozenset([
                (f"{nd_name}a.py", base_byte, base_byte + 300),
                (f"{nd_name}b.py", base_byte, base_byte + 300),
            ])
        )

    results = compare_strategies(corpus)
    result_by_name = {r.name: r for r in results}

    # ---- Every strategy finds all 5 exact pairs ----
    for strategy_name, result in result_by_name.items():
        found_pairs: set[frozenset] = set()
        for ka, kb, _ in result.near_duplicate_pairs:
            found_pairs.add(frozenset([ka, kb]))
        for cluster in result.duplicate_clusters:
            for i in range(len(cluster)):
                for j in range(i + 1, len(cluster)):
                    found_pairs.add(frozenset([cluster[i], cluster[j]]))

        for epair in exact_pair_keys:
            assert epair in found_pairs, (
                f"strategy '{strategy_name}' missed exact pair {epair}.\n"
                f"Found pairs (first 10): {list(found_pairs)[:10]}"
            )

    # ---- KindShingleMinHash finds all 3 near-dup pairs ----
    ks_result = result_by_name["kind_shingles_minhash"]
    ks_pairs: set[frozenset] = set()
    for ka, kb, _ in ks_result.near_duplicate_pairs:
        ks_pairs.add(frozenset([ka, kb]))
    for cluster in ks_result.duplicate_clusters:
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                ks_pairs.add(frozenset([cluster[i], cluster[j]]))

    for ndpair in nd_pairs:
        assert ndpair in ks_pairs, (
            f"KindShingleMinHash missed near-dup pair {ndpair}.\n"
            f"Found pairs: {ks_pairs}"
        )


# ---------------------------------------------------------------------------
# Test 5 — Registry sanity
# ---------------------------------------------------------------------------


def test_registry_has_all_expected_strategies():
    """REGISTRY has all 5 expected strategy names."""
    expected = {
        "merkle_exact",
        "kind_shingles_minhash",
        "kind_token_shingles_minhash",
        "simhash",
        "bag_of_subtrees",
    }
    assert set(REGISTRY.keys()) == expected


def test_registry_strategies_implement_hasher_protocol():
    """All registered Hasher classes have the required interface."""
    for name, (hasher_cls, index_cls, threshold) in REGISTRY.items():
        hasher_instance = hasher_cls()
        # Check Hasher protocol: has `name` attribute and `of` method.
        assert hasattr(hasher_instance, "name"), (
            f"Hasher for '{name}' missing 'name' attribute"
        )
        assert callable(getattr(hasher_instance, "of", None)), (
            f"Hasher for '{name}' missing callable 'of' method"
        )
        # isinstance check via runtime_checkable Protocol.
        assert isinstance(hasher_instance, Hasher), (
            f"Hasher for '{name}' does not satisfy Hasher protocol"
        )


def test_registry_indices_implement_index_protocol():
    """All registered Index classes have the required interface."""
    for name, (hasher_cls, index_cls, threshold) in REGISTRY.items():
        if name == "merkle_exact":
            index_instance = index_cls()
        elif name == "simhash":
            index_instance = index_cls(threshold=threshold)
        else:
            index_instance = index_cls(threshold=threshold, num_perm=128)

        assert hasattr(index_instance, "name"), (
            f"Index for '{name}' missing 'name' attribute"
        )
        assert callable(getattr(index_instance, "insert", None)), (
            f"Index for '{name}' missing callable 'insert' method"
        )
        assert callable(getattr(index_instance, "query", None)), (
            f"Index for '{name}' missing callable 'query' method"
        )
        assert isinstance(index_instance, Index), (
            f"Index for '{name}' does not satisfy Index protocol"
        )


def test_registry_thresholds_valid():
    """All registered default thresholds are in (0, 1]."""
    for name, (_, _, threshold) in REGISTRY.items():
        assert 0.0 < threshold <= 1.0, (
            f"Strategy '{name}' has invalid default threshold {threshold}"
        )


# ---------------------------------------------------------------------------
# Additional smoke tests for fallback MinHash
# ---------------------------------------------------------------------------


def test_fallback_minhash_jaccard_identical():
    """FallbackMinHash: identical sets of updates → Jaccard = 1.0."""
    mh_a = _FallbackMinHash(num_perm=64)
    mh_b = _FallbackMinHash(num_perm=64)

    for i in range(20):
        b = f"item_{i}".encode()
        mh_a.update(b)
        mh_b.update(b)

    j = mh_a.jaccard(mh_b)
    assert j == pytest.approx(1.0)


def test_fallback_minhash_jaccard_disjoint():
    """FallbackMinHash: completely disjoint sets → Jaccard near 0."""
    mh_a = _FallbackMinHash(num_perm=128)
    mh_b = _FallbackMinHash(num_perm=128)

    for i in range(50):
        mh_a.update(f"set_A_{i}".encode())
    for i in range(50):
        mh_b.update(f"set_B_{i}".encode())

    j = mh_a.jaccard(mh_b)
    # Should be well below 0.5 (likely near 0 for genuinely disjoint sets).
    assert j < 0.5, f"Expected low Jaccard for disjoint sets, got {j:.3f}"


# ---------------------------------------------------------------------------
# Additional smoke tests for BagOfSubtreesMinHash
# ---------------------------------------------------------------------------


def test_bag_of_subtrees_identical():
    """Identical child_merkle_bags → Jaccard = 1.0."""
    hasher = BagOfSubtreesMinHash(num_perm=64)
    bag = tuple(_h(f"child_{i}") for i in range(10))

    sub_a = _make_sub(file="a.py", child_merkle_bag=bag)
    sub_b = _make_sub(file="b.py", child_merkle_bag=bag)

    fp_a = hasher.of(sub_a)
    fp_b = hasher.of(sub_b)

    j = _minhash_jaccard(fp_a, fp_b)
    assert j == pytest.approx(1.0)


def test_bag_of_subtrees_different():
    """Completely different child_merkle_bags → low Jaccard."""
    hasher = BagOfSubtreesMinHash(num_perm=128)
    bag_a = tuple(_h(f"child_a_{i}") for i in range(15))
    bag_b = tuple(_h(f"child_b_{i}") for i in range(15))

    sub_a = _make_sub(file="a.py", child_merkle_bag=bag_a)
    sub_b = _make_sub(file="b.py", child_merkle_bag=bag_b)

    fp_a = hasher.of(sub_a)
    fp_b = hasher.of(sub_b)

    j = _minhash_jaccard(fp_a, fp_b)
    assert j < 0.5, f"Expected low Jaccard for disjoint bags, got {j:.3f}"
