"""Phase 5 — tests for sibling-sequence clone detection.

Six test cases from the spec:
1. Two functions share a 6-statement prelude; both methods find it with length==6.
2. Loop rename (i -> j) does not break the match.
3. Literal change (3 -> 5) does not break the match.
4. Operator change (+  -> -) breaks the match, splitting into shorter runs.
5. Agreement check: on a 10-function fixture, winnowing covers every SA run.
6. All-assignment run is filtered by the non-trivial statement mix rule.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

import pytest

from emend import emend_core

from experiments.ast_dedup.sequence import (
    StatementSeq,
    SequenceClone,
    build_statement_seqs,
    find_clones_suffix_array,
    find_clones_winnowing,
    filter_sequence_clones,
    winnowing,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path, name: str, source: str) -> str:
    p = tmp_path / name
    p.write_text(source)
    return str(p)


def _seqs(tmp_path, name: str, source: str) -> list[StatementSeq]:
    path = _write(tmp_path, name, source)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    return build_statement_seqs(path, resolver)


def _covered(sa_clone: SequenceClone, win_clones: list[SequenceClone]) -> bool:
    """Return True if *sa_clone* is contained within some winnowing clone."""
    for wc in win_clones:
        if (
            wc.left.function_qn == sa_clone.left.function_qn
            and wc.right.function_qn == sa_clone.right.function_qn
            and wc.left_range[0] <= sa_clone.left_range[0]
            and wc.left_range[1] >= sa_clone.left_range[1]
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Test 1 — shared 6-statement prelude
# ---------------------------------------------------------------------------


def test_shared_6_statement_prelude(tmp_path):
    """Both winnowing and suffix array detect a shared 6-statement prelude."""
    src = """\
def f(x, y, z):
    a = x + y
    b = a * 3
    c = b - 1
    d = c + a
    e = d * b
    f_val = e - c
    return f_val + z

def g(p, q, r):
    a = p + q
    b = a * 5
    c = b - 1
    d = c + a
    e = d * b
    g_val = e - c
    result = g_val + r
    return result
"""
    seqs = _seqs(tmp_path, "t1.py", src)
    assert len(seqs) == 2, f"expected 2 seqs, got {len(seqs)}"

    # Manually verify that stmts 0-5 (6 statements) match across the two functions.
    assert len(seqs[0].hashes) >= 6
    assert len(seqs[1].hashes) >= 6
    per_stmt = [h1 == h2 for h1, h2 in zip(seqs[0].hashes, seqs[1].hashes)]
    assert per_stmt[:6] == [True] * 6, f"first 6 stmts should match: {per_stmt}"

    # Winnowing must detect a run of at least 6.
    clones_w = find_clones_winnowing(seqs, w=4, min_run=4)
    max_len_w = max((c.length for c in clones_w), default=0)
    assert max_len_w >= 6, (
        f"winnowing should find a run of at least 6 statements, got {max_len_w}"
    )

    # Suffix array must also detect a run of at least 6.
    clones_sa = find_clones_suffix_array(seqs, min_run=4)
    max_len_sa = max((c.length for c in clones_sa), default=0)
    assert max_len_sa >= 6, (
        f"suffix array should find a run of at least 6 statements, got {max_len_sa}"
    )


# ---------------------------------------------------------------------------
# Test 2 — loop rename does not break the match
# ---------------------------------------------------------------------------


def test_loop_rename_preserved(tmp_path):
    """Renaming the loop counter (i → j) does not break statement hashes."""
    src = """\
def f(data):
    total = 0
    for i in range(len(data)):
        total += data[i]
    result = total * 2
    return result

def g(items):
    total = 0
    for j in range(len(items)):
        total += items[j]
    result = total * 2
    return result
"""
    seqs = _seqs(tmp_path, "t2.py", src)
    assert len(seqs) == 2

    # All statements in these 4-statement functions should hash identically
    # because the loop counter is alpha-renamed to the same bound token.
    assert len(seqs[0].hashes) == len(seqs[1].hashes)
    mismatches = [
        i
        for i, (h1, h2) in enumerate(zip(seqs[0].hashes, seqs[1].hashes))
        if h1 != h2
    ]
    assert mismatches == [], (
        f"all statements should match after loop rename; mismatch at indices {mismatches}"
    )

    # Both methods should detect a run covering the entire function body.
    clones_w = find_clones_winnowing(seqs, w=4, min_run=4)
    clones_sa = find_clones_suffix_array(seqs, min_run=4)
    assert clones_w, "winnowing should find at least one clone"
    assert clones_sa, "suffix array should find at least one clone"


# ---------------------------------------------------------------------------
# Test 3 — literal change does not break the match
# ---------------------------------------------------------------------------


def test_literal_change_preserved(tmp_path):
    """Changing a numeric literal (3 → 5) does not break statement hashes."""
    src = """\
def f(x, y):
    a = x + y
    b = a * 3
    c = b - 1
    d = c + a
    e = d * b
    f_val = e - c
    return f_val

def g(p, q):
    a = p + q
    b = a * 5
    c = b - 1
    d = c + a
    e = d * b
    f_val = e - c
    return f_val
"""
    seqs = _seqs(tmp_path, "t3.py", src)
    assert len(seqs) == 2

    # Every statement should match because literals are canonicalised to
    # ``num_N`` placeholders during the walk.
    mismatches = [
        i
        for i, (h1, h2) in enumerate(zip(seqs[0].hashes, seqs[1].hashes))
        if h1 != h2
    ]
    assert mismatches == [], (
        f"literal change should not break any statement hash; mismatch at {mismatches}"
    )


# ---------------------------------------------------------------------------
# Test 4 — operator change breaks the match
# ---------------------------------------------------------------------------


def test_operator_change_splits_run(tmp_path):
    """An operator change (+ → -) breaks the run at the changed statement.

    f: a=x+y, b=a*3, c=b-1, d=c+a, e=d*b, f_=e-c, return
    g: a=p+q, b=a+3, c=b-1, d=c+a, e=d*b, f_=e-c, return
                   ^
               operator change at stmt 1 (* → +)

    Stmt 0 and stmts 2-6 should still match; stmt 1 breaks.
    """
    src = """\
def f(x, y):
    a = x + y
    b = a * 3
    c = b - 1
    d = c + a
    e = d * b
    f_val = e - c
    return f_val

def g(p, q):
    a = p + q
    b = a + 3
    c = b - 1
    d = c + a
    e = d * b
    f_val = e - c
    return f_val
"""
    seqs = _seqs(tmp_path, "t4.py", src)
    assert len(seqs) == 2

    per_stmt = [h1 == h2 for h1, h2 in zip(seqs[0].hashes, seqs[1].hashes)]
    # Stmt 1 should NOT match (operator changed).
    assert per_stmt[1] is False, (
        "stmt 1 (b = a * 3 vs b = a + 3) should differ due to operator change"
    )
    # The segment after the break (stmts 2-6, length 5) should all match.
    assert all(per_stmt[2:]), (
        f"stmts 2-6 should all match after the operator-changed stmt: {per_stmt}"
    )

    # Winnowing (w=2 to detect short runs) should find the post-break run.
    clones_w = find_clones_winnowing(seqs, w=2, min_run=2)
    # The longest run found should be the post-break segment (stmts 2-6 = 5 stmts).
    assert clones_w, "winnowing should find at least one run after the break"
    max_len = max(c.length for c in clones_w)
    # There are 5 matching statements after the break plus 1 before = but
    # the break means they are in two separate runs.  The longest run ≥ 5.
    assert max_len >= 5, f"longest post-break run should be ≥ 5 statements, got {max_len}"

    # No single run should span the full 7 statements (the break is there).
    assert all(c.length < 7 for c in clones_w), (
        "no run should span the full 7 statements (operator change broke it)"
    )

    # Suffix array should also find the post-break run but not the full match.
    clones_sa = find_clones_suffix_array(seqs, min_run=4)
    assert any(c.length >= 5 for c in clones_sa), (
        "suffix array should detect the 5-statement post-break run"
    )
    assert all(c.length < 7 for c in clones_sa), (
        "suffix array should not find a 7-statement run (operator change)"
    )


# ---------------------------------------------------------------------------
# Test 5 — agreement: winnowing covers every SA run
# ---------------------------------------------------------------------------


def test_winnowing_covers_suffix_array(tmp_path):
    """On a multi-function fixture, winnowing finds every run SA finds.

    We use a coverage check: for every SA clone of length ≥ w, there exists
    a winnowing clone that contains it (same pair of function QNs, and the
    winnowing run's statement-index range subsumes the SA run's range).
    """
    src = """\
def func1(x, y):
    a = x + y
    b = a * 2
    c = b - x
    d = c + 1
    e = d * a
    f = e / b
    g = f + c
    return g

def func2(p, q):
    a = p + q
    b = a * 2
    c = b - p
    d = c + 1
    e = d * a
    f = e / b
    g = f + c
    return g

def func3(m, n):
    a = m + n
    b = a * 2
    c = b - m
    d = c + 1
    e = d * a
    f = e / b
    g = f + c
    return g

def func4(u, v):
    x = u + v
    y = x * 3
    z = y - u
    return z

def func5(r, s):
    x = r + s
    y = x * 3
    z = y - r
    return z

def func6(a, b):
    result = a + b
    return result

def func7(c, d):
    result = c + d
    return result

def func8(a, b, c):
    total = a + b + c
    half = total / 2
    return half

def func9(a, b, c):
    total = a + b + c
    half = total / 2
    return half

def func10(x):
    return x * 2
"""
    seqs = _seqs(tmp_path, "t5.py", src)
    assert len(seqs) == 10, f"expected 10 seqs, got {len(seqs)}"

    w = 4
    min_run = w  # winnowing guarantees detection of runs ≥ w

    clones_w = find_clones_winnowing(seqs, w=w, min_run=min_run)
    clones_sa = find_clones_suffix_array(seqs, min_run=min_run)

    # Every SA clone of length ≥ min_run should be covered by a winnowing clone.
    uncovered = [c for c in clones_sa if not _covered(c, clones_w)]
    assert not uncovered, (
        f"winnowing missed {len(uncovered)} SA run(s):\n"
        + "\n".join(
            f"  {c.left.function_qn}[{c.left_range}] ~ "
            f"{c.right.function_qn}[{c.right_range}] len={c.length}"
            for c in uncovered
        )
    )


# ---------------------------------------------------------------------------
# Test 6 — non-trivial statement mix filter
# ---------------------------------------------------------------------------


def test_all_assignment_run_filtered(tmp_path):
    """A run consisting entirely of ``self.x = x`` assignments is filtered out."""
    src = """\
class Foo:
    def __init__(self, a, b, c, d, e):
        self.a = a
        self.b = b
        self.c = c
        self.d = d
        self.e = e

class Bar:
    def __init__(self, a, b, c, d, e):
        self.a = a
        self.b = b
        self.c = c
        self.d = d
        self.e = e
"""
    seqs = _seqs(tmp_path, "t6.py", src)
    assert len(seqs) == 2

    # Pre-condition: both methods find a clone (5 identical statements).
    clones_w = find_clones_winnowing(seqs, w=4, min_run=4)
    clones_sa = find_clones_suffix_array(seqs, min_run=4)
    assert clones_w or clones_sa, "should find raw clones before filtering"

    # After filtering, the all-assignment run should be removed.
    # (All kinds will be ``expression_statement`` for ``self.x = x`` in Python.)
    filtered_w = filter_sequence_clones(clones_w, min_run=4)
    filtered_sa = filter_sequence_clones(clones_sa, min_run=4)

    assert len(filtered_w) == 0, (
        f"winnowing: all-assignment run should be filtered, got {len(filtered_w)}"
    )
    assert len(filtered_sa) == 0, (
        f"suffix array: all-assignment run should be filtered, got {len(filtered_sa)}"
    )
