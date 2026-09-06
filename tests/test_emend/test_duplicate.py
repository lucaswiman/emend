"""Tests for production duplicate detection (Phases 8-11).

Tests cover:
- Production canonicalization (literal-preserving, variable-renaming)
- Exact subtree duplicate detection
- Sibling-sequence duplicate detection
- Cache + facts integration
- CLI command (emend analyze dupes)
- Lint integration
- MCP tool
- Production heuristics and scoring
"""

from __future__ import annotations

import json
import os
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUPLICATE_HELPER_A = '''\
def process_data(items):
    result = []
    for item in items:
        if item.is_valid():
            transformed = item.transform()
            result.append(transformed)
    return result


def unused_func():
    pass
'''

DUPLICATE_HELPER_B = '''\
def handle_records(records):
    output = []
    for record in records:
        if record.is_valid():
            converted = record.transform()
            output.append(converted)
    return output


def another_func():
    x = 1
    y = 2
    return x + y
'''

SEQUENCE_DUP_A = '''\
def setup_a(config):
    db = connect(config.db_url)
    db.initialize()
    cache = Cache(config.cache_size)
    cache.warm()
    logger = Logger(config.log_level)
    logger.start()
    return App(db, cache, logger)
'''

SEQUENCE_DUP_B = '''\
def setup_b(settings):
    database = connect(settings.db_url)
    database.initialize()
    store = Cache(settings.cache_size)
    store.warm()
    log = Logger(settings.log_level)
    log.start()
    return App(database, store, log)
'''

TRIVIAL_DUP = '''\
def __repr__(self):
    return f"{self.__class__.__name__}({self.name})"

class Foo:
    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"
'''

NON_TRIVIAL_DUP = '''\
def validate_and_transform(data, schema):
    errors = []
    for field_name, field_type in schema.items():
        value = data.get(field_name)
        if value is None:
            errors.append(f"Missing field: {field_name}")
            continue
        if not isinstance(value, field_type):
            errors.append(f"Wrong type for {field_name}")
            continue
        data[field_name] = field_type(value)
    if errors:
        raise ValidationError(errors)
    return data


def check_and_convert(payload, spec):
    issues = []
    for attr_name, attr_type in spec.items():
        val = payload.get(attr_name)
        if val is None:
            issues.append(f"Missing field: {attr_name}")
            continue
        if not isinstance(val, attr_type):
            issues.append(f"Wrong type for {attr_name}")
            continue
        payload[attr_name] = attr_type(val)
    if issues:
        raise ValidationError(issues)
    return payload
'''

ABSTRACT_STUB = '''\
class BaseHandler:
    def handle(self, request):
        raise NotImplementedError

    def validate(self, data):
        raise NotImplementedError

class AnotherBase:
    def handle(self, request):
        raise NotImplementedError

    def validate(self, data):
        raise NotImplementedError
'''

CONSTANT_TABLE_A = '''\
STATUS_CODES = {
    200: "OK",
    201: "Created",
    400: "Bad Request",
    404: "Not Found",
    500: "Internal Server Error",
}
'''

CONSTANT_TABLE_B = '''\
ERROR_MESSAGES = {
    200: "Success",
    201: "Resource Created",
    400: "Invalid Input",
    404: "Resource Missing",
    500: "Server Failure",
}
'''


def _write_files(tmp_path: Path, files: dict[str, str]) -> None:
    """Write a dict of {relative_path: content} to tmp_path."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# ---------------------------------------------------------------------------
# Phase 8: Core duplicate detection
# ---------------------------------------------------------------------------


class TestCanonicalization:
    """Test the production canonicalizer."""

    @pytest.fixture
    def canonical_form(self, tmp_path):
        from emend import emend_core
        from emend.duplicate import canonicalize_subtree, _build_qn_at

        scope = emend_core.PyScopeResolver(str(tmp_path))
        path = str(tmp_path / "sample.py")

        def form(source, *, body=False):
            scope.index_file(path, source)
            node = emend_core.parse_source(source, "py").root.named_child(0)
            if body:
                node = node.child_by_field_name("body")
            return canonicalize_subtree(node, *_build_qn_at(path, scope))

        return form

    @pytest.mark.parametrize("left,right,equal", [
        ("y = x + 1; return y", "z = x + 1; return z", True),
        ("return x + 1", "return x - 1", False),
        ("return x + 1", "return x + 2", False),
        ('return "one"', 'return "two"', False),
        ("return len(x)", "return sum(x)", False),
        ("return call(x=x)", "return call(y=x)", False),
        ("y = x; return call(y=y)", "z = x; return call(z=z)", False),
        ("return x.first", "return x.second", False),
        ("return x + 1 # comment", "return x + 1", True),
    ])
    def test_semantic_equivalence(self, canonical_form, left, right, equal):
        assert (canonical_form(f"def f(x):\n    {left}\n") ==
                canonical_form(f"def g(x):\n    {right}\n")) == equal

    @pytest.mark.parametrize("left,right,equal", [
        ("class A:\n    x: int\n", "class B:\n    y: int\n", False),
        ("class A:\n    def f(self): return 1\n", "class B:\n    def g(self): return 1\n", False),
        ("class A:\n    def f(self, x): return x\n", "class B:\n    def f(self, y): return y\n", True),
        ("def f(x):\n    def g(y): return x\n", "def f(x):\n    def g(x): return x\n", False),
        ("def f(x):\n    def g(y): return x + y\n", "def f(a):\n    def g(b): return a + b\n", True),
        ("def f(x, y): return x\n", "def f(x, y): return y\n", False),
    ])
    def test_scope_bindings(self, canonical_form, left, right, equal):
        assert (canonical_form(left) == canonical_form(right)) == equal
        if left.startswith("class"):
            assert (canonical_form(left, body=True) == canonical_form(right, body=True)) == equal


class TestExactDuplicates:
    """Test exact (Merkle) duplicate detection."""

    def test_finds_exact_duplicates(self, tmp_path):
        """Two alpha-equivalent functions are detected as exact duplicates."""
        from emend.duplicate import query_duplicates

        _write_files(tmp_path, {
            "a.py": DUPLICATE_HELPER_A,
            "b.py": DUPLICATE_HELPER_B,
        })

        clusters = query_duplicates(
            str(tmp_path), mode="exact", min_lines=3, min_score=0.0,
        )
        # Should find at least one exact duplicate cluster
        exact = [c for c in clusters if c.kind == "exact"]
        assert len(exact) >= 1
        # The cluster should have members from both files
        cluster = exact[0]
        files = {m.file for m in cluster.members}
        assert len(files) >= 2

    def test_no_false_exact_for_different_code(self, tmp_path):
        """Structurally different functions are not exact duplicates."""
        from emend.duplicate import query_duplicates

        _write_files(tmp_path, {
            "a.py": "def add(x, y):\n    return x + y\n",
            "b.py": "def multiply(x, y):\n    result = x * y\n    print(result)\n    return result\n",
        })

        clusters = query_duplicates(
            str(tmp_path), mode="exact", min_lines=2, min_score=0.0,
        )
        # Should not find cross-file exact duplicates for different code
        cross = [c for c in clusters if len({m.file for m in c.members}) > 1]
        assert len(cross) == 0


class TestSequenceDuplicates:
    @pytest.mark.parametrize("left,right,wrong_return", [
        ("x = a\ny = b\nx = adjust(x)\nreturn x",
         "u = c\nv = d\nu = adjust(u)\nreturn u", "v"),
        ("record(a)\nrecord(b)\nfinish()\nreturn a",
         "record(c)\nrecord(d)\nfinish()\nreturn c", "d"),
    ])
    def test_binding_relationships_across_statements(self, tmp_path, left, right, wrong_return):
        from emend.duplicate import query_duplicates

        for name, params, body in (("f", "a, b", left), ("g", "c, d", right)):
            (tmp_path / f"{name}.py").write_text(
                f"def {name}({params}):\n" + "\n".join(f"    {line}" for line in body.splitlines()) + "\n")
        assert len(query_duplicates(str(tmp_path), mode="sequence")) == 1
        path = tmp_path / "g.py"
        path.write_text(path.read_text().rsplit("return ", 1)[0] + f"return {wrong_return}\n")
        assert query_duplicates(str(tmp_path), mode="sequence") == []

    """Test sibling-sequence duplicate detection."""

    @pytest.mark.parametrize("bodies, expected", [
        (["a" * 10000] * 2, [10000]),
        (["a" * 10000 + "Q", "a" * 10000 + "Z", "WXYZ"], [10000]),
        (["a" * 10000], [5000]),
        (["abcdWXYZ", "WXYZabcd"], [4, 4]),
        (["abcdWXYZabcd"], [4]),
    ])
    def test_maximal_nonoverlapping_runs(self, bodies, expected):
        from emend.duplicate import _sequence_clusters_from_seqs

        sequences = [dict(file=str(i), function_qn=str(i), hashes=list(body),
                          line_ranges=[[n, n] for n in range(len(body))], kinds=["call"] * len(body))
                     for i, body in enumerate(bodies)]
        clusters = _sequence_clusters_from_seqs(sequences, 3, None)
        assert sorted(c.members[0].stmt_count for c in clusters) == expected
        assert all(len(c.members) == 2 for c in clusters)
        for cluster in clusters:
            a, b = cluster.members
            assert a.file != b.file or a.end_line < b.start_line

    @pytest.mark.parametrize("cached", [False, True])
    def test_reports_only_shared_contiguous_runs(self, tmp_path, cached):
        from emend import emend_core
        from emend.duplicate import build_statement_seqs_for_cache, _clusters_from_cached_sequences, query_duplicates

        first = "    obj.open()\n    obj.read()\n    obj.check()\n    obj.close()\n"
        tail = "    obj.disconnect()\n"
        bodies = {"a": first, "bridge": first + tail, "long": first + tail,
                  "c": "".join(first.splitlines(keepends=True)[1:]) + tail,
                  "short": "".join(first.splitlines(keepends=True)[:3]),
                  "unrelated": "    obj.open()\n    obj.send()\n    obj.check()\n    obj.disconnect()\n"}
        bodies.update({f"comments_{name}": f"    obj.{name}()\n" + "    # one\n    # two\n    # three\n    # four\n" + f"    obj.{name}()\n"
                       for name in ("left", "right")})
        payloads = {}
        resolver = emend_core.PyScopeResolver(str(tmp_path))
        for name, body in bodies.items():
            path = tmp_path / f"{name}.py"
            source = f"def {name}(obj):\n{body}"
            path.write_text(source)
            resolver.index_file(str(path), source)
            payloads[str(path)] = {"sequences": build_statement_seqs_for_cache(str(path), source, resolver)}
        clusters = (_clusters_from_cached_sequences(payloads, 3, None) if cached
                    else query_duplicates(str(tmp_path), mode="sequence"))
        assert {frozenset(Path(m.file).stem for m in c.members) for c in clusters} == {
            frozenset({"a", "bridge", "long"}), frozenset({"bridge", "long", "c"}),
            frozenset({"bridge", "long"})}
        assert all(m.stmt_count == (5 if len(c.members) == 2 else 4)
                   for c in clusters for m in c.members)
        assert all(m.symbol == Path(m.file).stem for c in clusters for m in c.members)
        assert {(m.start_line, m.end_line) for c in clusters for m in c.members} == {(2, 5), (3, 6), (2, 6)}
        long_runs = (_clusters_from_cached_sequences(payloads, 5, None) if cached
                     else query_duplicates(str(tmp_path), mode="sequence", min_lines=5))
        assert len(long_runs) == 1
        assert all((m.start_line, m.end_line, m.stmt_count) == (2, 6, 5) for m in long_runs[0].members)

    def test_min_lines_counts_source_lines_not_statements(self, tmp_path):
        from emend.duplicate import query_duplicates

        body = "".join(f"    obj.{name}(\n        value,\n    )\n" for name in ("open", "read", "check", "close"))
        _write_files(tmp_path, {f"{name}.py": f"def {name}(obj, value):\n{body}" for name in ("a", "b")})
        clusters = query_duplicates(str(tmp_path), mode="sequence", min_lines=10)
        assert len(clusters) == 1
        assert all((m.start_line, m.end_line, m.stmt_count) == (2, 13, 4) for m in clusters[0].members)

    def test_short_occurrence_does_not_hide_overlapping_eligible_run(self):
        from emend.duplicate import _sequence_clusters_from_seqs

        ranges = [[(0, 0), (1, 1), (2, 2), (3, 3), (4, 13)],
                  [(0, 3), (4, 7), (8, 11), (12, 15)]]
        sequences = [dict(file=str(i), function_qn=str(i), hashes=["a"] * len(lines),
                          line_ranges=lines, kinds=["call"] * len(lines)) for i, lines in enumerate(ranges)]
        clusters = _sequence_clusters_from_seqs(sequences, 10, None)
        assert len(clusters) == 1
        assert {(m.start_line, m.end_line) for m in clusters[0].members} == {(2, 14), (1, 16)}

    def test_finds_shared_statement_runs(self, tmp_path):
        """Functions sharing a long initialization sequence are detected as a
        cross-file sequence cluster."""
        from emend.duplicate import query_duplicates

        _write_files(tmp_path, {
            "a.py": SEQUENCE_DUP_A,
            "b.py": SEQUENCE_DUP_B.replace("\n    ", "\n    # explanation\n    "),
        })

        clusters = query_duplicates(
            str(tmp_path), mode="sequence", min_lines=2, min_score=0.0,
        )
        seq = [c for c in clusters if c.kind == "sequence"]
        assert len(seq) >= 1
        # The shared initialization run spans both files.
        files = {m.file for m in seq[0].members}
        assert len(files) >= 2
        assert all(m.stmt_count == 7 for m in seq[0].members)
        assert {(m.start_line, m.end_line) for m in seq[0].members} == {(2, 8), (3, 15)}


# ---------------------------------------------------------------------------
# Phase 8: Cache + facts integration
# ---------------------------------------------------------------------------


class TestCacheIntegration:
    """Test parse.db cache and facts.db integration."""

    def test_dup_cache_table_created(self, tmp_path):
        """emend index creates the dup_cache table in parse.db."""
        import sqlite3
        from emend.transform import _init_cache_schema

        db_path = str(tmp_path / "parse.db")
        conn = sqlite3.connect(db_path)
        _init_cache_schema(conn)
        tables = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "dup_cache" in tables
        conn.close()



# ---------------------------------------------------------------------------
# Phase 9: CLI + JSON output
# ---------------------------------------------------------------------------


class TestCLI:
    """Test the emend analyze dupes CLI command."""

    def test_cli_returns_exact_duplicate(self, tmp_path):
        """CLI text output names the duplicated helper functions."""
        from emend.duplicate import query_duplicates, format_duplicates_text

        _write_files(tmp_path, {
            "a.py": DUPLICATE_HELPER_A,
            "b.py": DUPLICATE_HELPER_B,
        })
        clusters = query_duplicates(str(tmp_path), mode="all", min_score=0.0)
        text = format_duplicates_text(clusters)
        assert "process_data" in text
        assert "handle_records" in text

    def test_json_output_roundtrips(self, tmp_path):
        """--json output is valid JSON with expected fields."""
        from emend.duplicate import query_duplicates, format_duplicates_json

        _write_files(tmp_path, {
            "a.py": DUPLICATE_HELPER_A,
            "b.py": DUPLICATE_HELPER_B,
        })
        clusters = query_duplicates(str(tmp_path), mode="all", min_score=0.0)
        json_str = format_duplicates_json(clusters)
        data = json.loads(json_str)
        assert isinstance(data, list)
        assert data, "expected the duplicated helpers to produce a cluster"
        members = {
            member["symbol"]
            for cluster in data
            for member in cluster["members"]
        }
        assert {"process_data", "handle_records"} <= members
        assert all("kind" in cluster for cluster in data)
        assert all("score" in cluster for cluster in data)


# ---------------------------------------------------------------------------
# Phase 9: Lint integration
# ---------------------------------------------------------------------------


class TestLintIntegration:
    """Test lint duplicate-code check."""

    def test_lint_emits_nothing_for_trivial(self, tmp_path):
        """Trivial duplicated snippets below threshold produce no violations."""
        from emend.lint import _check_duplicate_code, DuplicateCodeConfig

        _write_files(tmp_path, {
            "a.py": TRIVIAL_DUP,
        })

        config = DuplicateCodeConfig(
            enabled=True,
            min_lines=5,
            min_score=50.0,
        )
        violations = _check_duplicate_code(
            [str(tmp_path / "a.py")],
            config,
            str(tmp_path),
        )
        assert len(violations) == 0

    def test_lint_emits_warning_for_nontrivial(self, tmp_path):
        """A non-trivial duplicate across two files emits lint violations."""
        from emend.lint import _check_duplicate_code, DuplicateCodeConfig

        # Duplicate detection needs the copies in separate files.
        funcs = NON_TRIVIAL_DUP.split("\n\n\n")
        assert len(funcs) == 2
        _write_files(tmp_path, {
            "a.py": funcs[0] + "\n",
            "b.py": funcs[1] + "\n",
        })

        config = DuplicateCodeConfig(
            enabled=True,
            min_lines=3,
            min_score=0.0,
        )
        violations = _check_duplicate_code(
            [str(tmp_path / "a.py"), str(tmp_path / "b.py")],
            config,
            str(tmp_path),
        )
        assert len(violations) >= 1
        assert all(v.rule_name == "duplicate-code" for v in violations)


# ---------------------------------------------------------------------------
# Phase 11: Production heuristics
# ---------------------------------------------------------------------------


class TestProductionHeuristics:
    """Integration tests: suppressions are applied through query_duplicates."""

    def test_abstract_stubs_suppressed(self, tmp_path):
        """Abstract stubs are penalised below the score threshold while a
        genuine duplicate in the same project survives.

        Paired control: at a threshold that keeps the real duplicate, the
        stub-only clusters must be filtered out entirely.
        """
        from emend.duplicate import query_duplicates

        _write_files(tmp_path, {
            "real.py": NON_TRIVIAL_DUP,
            "stub.py": ABSTRACT_STUB,
        })
        clusters = query_duplicates(
            str(tmp_path), mode="exact", min_lines=1, min_score=100.0,
        )
        symbols = {m.symbol for c in clusters for m in c.members}
        # Positive control: the genuine duplicate survives.
        assert "validate_and_transform" in symbols
        assert "check_and_convert" in symbols
        # Negative control: the abstract-stub classes/methods are suppressed.
        assert not any(
            "BaseHandler" in s or "AnotherBase" in s for s in symbols
        )

    def test_constant_tables_distinguished(self, tmp_path):
        """Literal-preserving hashing distinguishes constant tables with different values."""
        from emend.duplicate import query_duplicates

        _write_files(tmp_path, {
            "a.py": CONSTANT_TABLE_A,
            "b.py": CONSTANT_TABLE_B,
        })

        clusters = query_duplicates(
            str(tmp_path), mode="exact", min_lines=2, min_score=0.0,
        )
        cross = [
            c for c in clusters
            if len({m.file for m in c.members}) > 1
        ]
        assert len(cross) == 0

    def test_heuristics_unit_stubs(self):
        """Suppression helpers return expected penalties."""
        from emend.duplicate_heuristics import is_abstract_stub, is_property_wrapper

        ks = ("function_definition", "block", "raise_statement", "identifier")
        ts = ("def", "handle", "raise", "NotImplementedError")
        assert is_abstract_stub(ks, ts) >= 500.0

        ks2 = ("function_definition", "block", "return_statement", "attribute", "identifier", "identifier")
        ts2 = ("def", "name", "return", "self", ".", "name")
        assert is_property_wrapper(ks2, ts2) >= 300.0

    def test_trivial_validator_isinstance_suppressed(self):
        """isinstance validators (small if-statements) must be suppressed."""
        from emend.duplicate_heuristics import is_trivial_validator

        ks = ("if_statement", "call", "identifier", "raise_statement")
        ts = ("if", "isinstance", "x", "int", "raise", "TypeError")
        assert is_trivial_validator(8, ks, ts) >= 400.0

    def test_trivial_validator_non_isinstance_call_not_suppressed(self):
        """A small if-statement with a non-isinstance call must NOT be
        suppressed.  Regression: 'call' in kind_seq matched any function
        call, not just isinstance."""
        from emend.duplicate_heuristics import is_trivial_validator

        ks = ("if_statement", "call", "identifier", "block", "expression_statement")
        ts = ("if", "validate", "data", "process", "data")
        assert is_trivial_validator(8, ks, ts) == 0.0

    def test_min_score_filters_low_clusters(self, tmp_path):
        """min_score strictly removes clusters scoring below the threshold."""
        from emend.duplicate import query_duplicates

        _write_files(tmp_path, {"a.py": NON_TRIVIAL_DUP})
        all_clusters = query_duplicates(
            str(tmp_path), mode="exact", min_lines=1, min_score=0.0,
        )
        assert all_clusters
        maximum = max(c.score for c in all_clusters)
        assert query_duplicates(str(tmp_path), mode="exact", min_lines=1, min_score=maximum)
        assert query_duplicates(str(tmp_path), mode="exact", min_lines=1, min_score=maximum + 1) == []


# ---------------------------------------------------------------------------
# Bug: symbol_index misinterprets (line, col) as (start_line, end_line)
# ---------------------------------------------------------------------------


DUNDER_BOILERPLATE_DUP = '''\
class Foo:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"

    def process(self):
        x = 1
        y = 2
        z = 3
        return x + y + z

class Bar:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"

    def process(self):
        a = 1
        b = 2
        c = 3
        return a + b + c
'''


class TestSymbolIndexLineRanges:
    """Regression: symbol_index must use (start_line, end_line) not (start_line, col)."""

    def test_find_containing_symbol_uses_line_ranges(self, tmp_path):
        """_find_containing_symbol should find symbols for lines deep in a method.

        The bug: _build_qn_at returns def_loc[qn] = (start_line_0idx, col),
        but the destructuring treats col as end_line. Since col is usually 0-8,
        lines > col would never match the 'sl <= line <= el' check in
        _find_containing_symbol. Even when it does match, it matches the
        wrong symbol (a local variable instead of the enclosing function).
        """
        from emend.duplicate import _find_containing_symbol, _preparse_files

        src = '''\
class MyClass:
    def method_a(self):
        x = 1
        y = 2
        z = 3
        w = 4
        return x + y + z + w

    def method_b(self):
        a = 10
        b = 20
        return a + b
'''
        p = tmp_path / "test.py"
        p.write_text(src)

        _scope, file_data = _preparse_files([str(p)], symbol_scope=None)
        _content, _tree, _qn_at, _def_loc, symbol_index = file_data[str(p)]

        # symbol_index should only contain class/function definitions with
        # proper (start_line, end_line) ranges — NOT local variables.
        # With the bug, symbol_index entries use column numbers as end_line,
        # and include local variables like x, y, z which pollute the lookup.
        sym_names = [qn for qn, _sl, _el in symbol_index]
        # Should contain the class and methods
        assert any("MyClass" in n and "method" not in n for n in sym_names), (
            f"Expected MyClass in symbol_index, got {sym_names}"
        )
        assert any("method_a" in n for n in sym_names), (
            f"Expected method_a in symbol_index, got {sym_names}"
        )
        # Should NOT contain local variables like x, y, z, w, a, b
        for qn, _sl, _el in symbol_index:
            leaf = qn.rsplit(".", 1)[-1]
            assert leaf not in ("x", "y", "z", "w", "a", "b", "self"), (
                f"symbol_index should not contain local variable '{qn}', "
                f"but it does. Full index: {symbol_index}"
            )

        # Verify line ranges are correct: method_a spans lines 1-6 (0-indexed)
        method_a_entries = [(qn, sl, el) for qn, sl, el in symbol_index
                           if "method_a" in qn and "method_b" not in qn]
        assert len(method_a_entries) >= 1
        _qn, sl, el = method_a_entries[0]
        assert el > sl, (
            f"end_line ({el}) should be > start_line ({sl}) for method_a. "
            f"If el is a column number, this indicates the bug."
        )
        assert el >= 6, (
            f"method_a's end_line should be >= 6 (0-indexed), got {el}. "
            f"This looks like a column number, not an end line."
        )

        # Line 5 (0-indexed) is deep inside method_a.
        # Should return method_a (or MyClass.method_a), not a local variable.
        sym = _find_containing_symbol(5, symbol_index)
        assert sym != "", (
            f"Expected to find containing symbol for line 5, but got empty string. "
            f"symbol_index={symbol_index}"
        )
        # Should be the method itself, not a local variable
        leaf = sym.rsplit(".", 1)[-1]
        assert leaf == "method_a", (
            f"Expected innermost symbol for line 5 to be 'method_a', "
            f"got '{sym}' (leaf='{leaf}')"
        )

    def test_dunder_boilerplate_suppressed_via_symbol_index(self, tmp_path):
        """Dunder boilerplate suppression needs symbol names from symbol_index.

        When symbol_index is broken, symbol names are wrong (e.g. local
        variable names instead of the enclosing function), so
        is_dunder_boilerplate() never fires, leading to false-positive
        duplicate clusters for trivial __repr__/__eq__ methods.
        """
        from emend.duplicate import query_duplicates

        _write_files(tmp_path, {"a.py": DUNDER_BOILERPLATE_DUP})

        clusters = query_duplicates(
            str(tmp_path), mode="exact", min_lines=1, min_score=0.0,
        )

        # With a working symbol_index, member.symbol should be the actual
        # enclosing class/method name, not a local variable.
        for cluster in clusters:
            for member in cluster.members:
                if member.symbol:
                    leaf = member.symbol.rsplit(".", 1)[-1]
                    # symbol should never be a local variable or parameter
                    assert leaf not in ("self", "name", "x", "y", "z", "a", "b", "c"), (
                        f"member.symbol is '{member.symbol}' which looks like a "
                        f"local variable, not an enclosing class/method. "
                        f"This indicates symbol_index is using col as end_line."
                    )


# ---------------------------------------------------------------------------
# Bug: run_duplicate_code_check only inspects first cluster member
# ---------------------------------------------------------------------------


class TestDuplicateCheckFirstMemberBug:
    """run_duplicate_code_check uses cluster.members[:1], skipping other members."""

    def test_reports_violation_when_first_member_not_in_file_set(self, tmp_path):
        """If the first cluster member is NOT in the linted file set but a
        later member IS, the violation should still be reported."""
        from unittest.mock import patch, MagicMock
        from emend.checks.duplicates import run_duplicate_code_check, DuplicateCodeConfig

        member_a = MagicMock()
        member_a.file = str(tmp_path / "not_linted.py")
        member_a.start_line = 1

        member_b = MagicMock()
        member_b.file = str(tmp_path / "linted.py")
        member_b.start_line = 10

        cluster = MagicMock()
        cluster.members = [member_a, member_b]
        cluster.explanation = "exact duplicate"

        config = DuplicateCodeConfig(enabled=True, min_lines=1, min_score=0.0)

        with patch("emend.duplicate.query_duplicates", return_value=[cluster]):
            violations = run_duplicate_code_check(
                [str(tmp_path / "linted.py")],
                config,
                str(tmp_path),
            )

        assert len(violations) >= 1, (
            "Expected a violation for linted.py but got none — "
            "run_duplicate_code_check only checks the first cluster member"
        )


# ---------------------------------------------------------------------------
# Bug: duplicate-code config is never wired through run_checks
# ---------------------------------------------------------------------------


class TestDuplicateCodeViaRunChecks:
    """run_checks must honour the duplicate-code section of rules.yaml.

    The canonical key is ``duplicate-code`` (matching the rule name, the
    violation kind, and ``--kind duplicate-code``); ``duplicate`` is accepted
    as a legacy alias.
    """

    def test_canonical_duplicate_code_key(self, tmp_path):
        import yaml
        from emend.lint import load_duplicate_code_config

        config_dir = tmp_path / ".emend"
        config_dir.mkdir()
        config_file = config_dir / "rules.yaml"
        config_file.write_text(yaml.dump({
            "duplicate-code": {"enabled": True, "min-lines": 3},
        }))

        config = load_duplicate_code_config(str(config_file))
        assert config is not None
        assert config.enabled
        assert config.min_lines == 3

    def test_legacy_duplicate_key_still_accepted(self, tmp_path):
        import yaml
        from emend.lint import load_duplicate_code_config

        config_dir = tmp_path / ".emend"
        config_dir.mkdir()
        config_file = config_dir / "rules.yaml"
        config_file.write_text(yaml.dump({
            "duplicate": {"enabled": True, "min-lines": 4},
        }))

        config = load_duplicate_code_config(str(config_file))
        assert config is not None
        assert config.enabled
        assert config.min_lines == 4

    def test_duplicate_code_section_produces_violations(self, tmp_path):
        import yaml
        from emend.checks.engine import run_checks

        config_dir = tmp_path / ".emend"
        config_dir.mkdir()
        config_file = config_dir / "rules.yaml"
        config_file.write_text(yaml.dump({
            "duplicate": {
                "enabled": True,
                "min-lines": 3,
                "min-score": 0.0,
                "cross-file-only": True,
            },
        }))

        funcs = NON_TRIVIAL_DUP.split("\n\n\n")
        assert len(funcs) == 2
        (tmp_path / "a.py").write_text(funcs[0] + "\n")
        (tmp_path / "b.py").write_text(funcs[1] + "\n")

        violations = run_checks(
            [str(tmp_path / "a.py"), str(tmp_path / "b.py")],
            config=str(config_file),
            project_path=str(tmp_path),
        )
        dup = [v for v in violations if v.rule_name == "duplicate-code"]
        assert len(dup) >= 1, (
            "Expected duplicate-code violations from run_checks but got none — "
            "the duplicate section was not wired through"
        )
        assert all(v.kind == "duplicate-code" for v in dup)
