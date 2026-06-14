"""Pin the line/column indexing convention of every emend_core API that
returns source positions.

The Python/Rust boundary is standardised on **0-indexed rows and columns**
(tree-sitter's native form) wherever practical.  A handful of APIs remain
1-indexed for the *line* component because their convention is relied upon by
dozens of FactGraph / display consumers; those are pinned here as explicit,
documented exceptions so the convention can't drift unnoticed.

Each test names the convention it pins.  The fixtures place content on known
rows/columns, including a multibyte-unicode line, so byte/char column
confusion would be caught.
"""
from __future__ import annotations

from pathlib import Path

from emend import emend_core
from emend.transform import find_pattern


# Shared fixture.  Rows are 0-indexed in the comments below:
#   row 0: ``import os``
#   row 1: ``from sys import path``
#   row 2: (blank)
#   row 3: ``def greet(name):``
#   row 4: ``    msg = "héllo"``   (contains a multibyte char 'é')
#   row 5: ``    return msg``
SOURCE = (
    "import os\n"
    "from sys import path\n"
    "\n"
    "def greet(name):\n"
    '    msg = "héllo"\n'
    "    return msg\n"
)


# ---------------------------------------------------------------------------
# 0-indexed APIs (migrated / native tree-sitter form)
# ---------------------------------------------------------------------------

def test_collect_identifier_positions_is_0indexed_row_and_col():
    """collect_identifier_positions: 0-indexed row AND 0-indexed column.

    Migrated from its former 1-indexed convention to match
    collect_string_literals and tree-sitter's native form.
    """
    src = "x = 1\nfoo.bar(y)\n"
    positions = emend_core.collect_identifier_positions(src)
    # row 0: ``x`` at columns [0, 1)
    assert ("x", 0, 0, 1) in positions
    # row 1: ``foo.bar`` spans columns [0, 7); ``y`` at columns [8, 9)
    assert ("foo.bar", 1, 0, 7) in positions
    assert ("y", 1, 8, 9) in positions


def test_collect_identifier_positions_multibyte_columns_are_byte_based():
    """collect_identifier_positions columns are 0-indexed byte columns.

    Tree-sitter reports columns as byte offsets within the line, so the
    two-byte ``é`` shifts later columns by one relative to character counts.
    """
    # bytes: h(0) é(1,2) l(3) l(4) o(5) ' '(6) '='(7) ' '(8) a(9)...
    src = "héllo = after\n"
    positions = emend_core.collect_identifier_positions(src)
    names = {name: (line, c0, c1) for name, line, c0, c1 in positions}
    assert names["héllo"] == (0, 0, 6)
    assert names["after"] == (0, 9, 14)


def test_structured_imports_lines_are_0indexed():
    """StructuredImport start_line/end_line: 0-indexed.

    Migrated to match Scope.start_line/end_line (both 0-indexed).
    """
    resolver = emend_core.PyScopeResolver(".", "py")
    imports = resolver.collect_structured_imports_from_source(SOURCE, "py")
    by_module = {(i["module"], i["is_plain"]): i for i in imports}
    plain = by_module[("", True)]  # ``import os`` on row 0
    assert plain["start_line"] == 0
    assert plain["end_line"] == 0
    from_sys = by_module[("sys", False)]  # ``from sys import path`` on row 1
    assert from_sys["start_line"] == 1
    assert from_sys["end_line"] == 1


def test_scopes_in_file_lines_are_0indexed(tmp_path: Path):
    """PyScopeResolver.scopes_in_file: 0-indexed start_line/end_line."""
    f = tmp_path / "scopes.py"
    f.write_text(SOURCE)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    resolver.index_file(str(f), SOURCE)
    scopes = {kind: (start, end) for kind, start, end, _b in resolver.scopes_in_file(str(f))}
    # Module scope spans the whole file (rows 0..6).
    assert scopes["Module"][0] == 0
    # ``def greet`` is on row 3.
    assert scopes["Function"][0] == 3


def test_cfg_lines_are_0indexed():
    """PyCfg func_start_line/func_end_line and block lines: 0-indexed."""
    src = "def foo():\n    return bar()\n"
    cfg = emend_core.build_cfgs(src, "py")[0]
    # ``def foo`` is on row 0; body ends on row 1.
    assert cfg.func_start_line == 0
    assert cfg.func_end_line == 1
    block_lines = {b["start_line"] for b in cfg.get_blocks()}
    # The ``return bar()`` statement is on row 1.
    assert 1 in block_lines


def test_pynode_points_are_0indexed():
    """PyNode.start_point/end_point: 0-indexed (row, col), tree-sitter native."""
    tree = emend_core.parse_source("x = 1\n", "py")
    root = tree.root
    assert root.start_point == (0, 0)
    # Single-line source: the root node ends at the start of row 1.
    assert root.end_point[0] == 1


# ---------------------------------------------------------------------------
# 1-indexed-line exceptions (documented, pervasively relied upon)
# ---------------------------------------------------------------------------

def test_collect_symbols_from_str_lines_are_1indexed():
    """collect_symbols_from_str line/end_line: 1-indexed (EXCEPTION).

    Feeds the FactGraph SymbolFact layer where ``.line`` is uniformly
    1-indexed across every fact kind; migrating would be a sweeping change.
    """
    syms = emend_core.collect_symbols_from_str(SOURCE)
    greet = next(s for s in syms if s["name"] == "greet")
    # ``def greet`` is on row 3 == 1-indexed line 4.
    assert greet["line"] == 4
    assert greet["end_line"] == 6


def test_references_in_file_lines_are_1indexed(tmp_path: Path):
    """PyScopeResolver.references_in_file line: 1-indexed (EXCEPTION).

    Populates FactGraph ReferenceFact.line, which is 1-indexed.
    """
    src = "def foo():\n    return bar()\n"
    f = tmp_path / "refs.py"
    f.write_text(src)
    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    resolver.index_file(str(f), src)
    refs = resolver.references_in_file(str(f))
    # The ``foo`` definition reference is on row 0 == 1-indexed line 1.
    foo_refs = [r for r in refs if r[0].endswith("foo")]
    assert foo_refs
    assert foo_refs[0][1] == 1


def test_pattern_matches_line_1indexed_col_0indexed(tmp_path: Path):
    """find_pattern matches: 1-indexed line, 0-indexed column (EXCEPTION).

    PatternMatch.line is 1-indexed (FactGraph + display); .column is the
    raw 0-indexed tree-sitter column.
    """
    f = tmp_path / "pat.py"
    f.write_text(SOURCE)
    matches = find_pattern("return $X", str(f))
    assert matches
    m = matches[0]
    # ``return msg`` is on row 5 == 1-indexed line 6, starting at column 4.
    assert m.line == 6
    assert m.col == 4


def test_get_statement_ranges_are_1indexed():
    """get_statement_ranges: 1-indexed (start_line, end_line) (EXCEPTION).

    Used to map noqa comments to statement ranges; consumers are 1-indexed.
    """
    ranges = emend_core.get_statement_ranges("x = 1\ny = 2\n", "py")
    assert (1, 1) in ranges
    assert (2, 2) in ranges


def test_collect_string_literals_line_1indexed_col_0indexed():
    """collect_string_literals: 1-indexed line, 0-indexed column (EXCEPTION).

    Matches DslRegion's documented contract (host_start_line 1-based,
    host_start_col 0-based).
    """
    src = 's = "hi"\n'
    results = emend_core.collect_string_literals(src, "py")
    assert results
    _sb, _eb, start_line, start_col, end_line, end_col, content = results[0]
    assert content == "hi"
    # ``"hi"`` is on row 0 == 1-indexed line 1; opening quote at column 4.
    assert start_line == 1
    assert end_line == 1
    assert start_col == 4


def test_collect_comments_line_1indexed_col_0indexed():
    """collect_comments: 1-indexed line, 0-indexed column (EXCEPTION)."""
    src = "x = 1  # note\n"
    results = emend_core.collect_comments(src, "py")
    assert results
    line, col, text = results[0]
    # The comment is on row 0 == 1-indexed line 1, starting at column 7.
    assert line == 1
    assert col == 7
    assert "note" in text
