"""Tests for DSL support: detection, extraction, and link resolution."""

import json
import pytest

from emend.dsl import (
    DslKind,
    DslMatch,
    DslRegion,
    DslSymbol,
    DslSymbolKind,
    LinkHint,
    DslLink,
    RegexNamedGroup,
    detect_dsl_regions,
    extract_sql_symbols,
    extract_jinja_symbols,
    extract_graphql_symbols,
    extract_regex_named_groups,
    find_dsl_impact,
    find_in_dsl,
    find_regex_group_references,
    resolve_orm_links,
    resolve_jinja_links,
    resolve_graphql_links,
    analyze_file,
    format_symbols,
    _singularize,
    _to_pascal_case,
    _find_tablename_mapping,
)


class TestNamingHelpers:
    def test_singularize_regular(self):
        assert _singularize("users") == "user"
        assert _singularize("posts") == "post"

    def test_singularize_ies(self):
        assert _singularize("categories") == "category"
        assert _singularize("entries") == "entry"

    def test_singularize_ses(self):
        assert _singularize("addresses") == "address"
        assert _singularize("buses") == "bus"

    def test_singularize_xes_zes_sses(self):
        """Words ending in xes/zes/sses should strip 'es', not just 's'.

        English plurals: box→boxes, buzz→buzzes, class→classes.
        The plural suffix is 'es', so singularizing must remove 2 chars.
        Removing only 1 char ('s') produces wrong stems like 'boxe', 'buzze', 'classe'.
        """
        assert _singularize("boxes") == "box"
        assert _singularize("classes") == "class"
        assert _singularize("buzzes") == "buzz"

    def test_singularize_already_singular(self):
        assert _singularize("user") == "user"
        assert _singularize("class") == "class"  # ends in ss

    def test_singularize_ches_shes(self):
        """Words ending in -ches/-shes need 'es' stripped, not just 's'."""
        assert _singularize("watches") == "watch"
        assert _singularize("batches") == "batch"
        assert _singularize("churches") == "church"
        assert _singularize("dishes") == "dish"
        assert _singularize("crashes") == "crash"
        # vowel + ches: singular ends in -che, just strip -s
        assert _singularize("caches") == "cache"
        assert _singularize("niches") == "niche"

    def test_singularize_already_singular_ending_in_s(self):
        """Words like 'status' are already singular—must not strip the trailing 's'."""
        # 'status' ends in 's' but NOT 'ss', so the naive rule would strip it to 'statu'.
        # The correct answer is 'status' (unchanged) because the word is already singular.
        assert _singularize("status") == "status"
        assert _singularize("nexus") == "nexus"
        assert _singularize("corpus") == "corpus"

    def test_to_pascal_case(self):
        assert _to_pascal_case("user") == "User"
        assert _to_pascal_case("user_profile") == "UserProfile"
        assert _to_pascal_case("order_item") == "OrderItem"


class TestFindTablenameMapping:
    """Tests for _find_tablename_mapping()."""

    def test_basic_class_tablename(self, tmp_path):
        """Finds __tablename__ assignments inside a class."""
        f = tmp_path / "models.py"
        f.write_text(
            "class User:\n"
            "    __tablename__ = 'users'\n"
        )
        result = _find_tablename_mapping(str(f))
        assert "users" in result
        assert result["users"][0] == "User"

    def test_module_level_tablename_not_attributed_to_class(self, tmp_path):
        """A __tablename__ at module level after a class must NOT be mapped to that class.

        Previously current_class was never reset after a class definition, so any
        module-level __tablename__ was wrongly attributed to the last class seen.
        """
        f = tmp_path / "models.py"
        f.write_text(
            "class User:\n"
            "    __tablename__ = 'users'\n"
            "\n"
            "class Post:\n"
            "    __tablename__ = 'posts'\n"
            "\n"
            "# Module-level – should NOT be attributed to Post\n"
            "__tablename__ = 'orphan_table'\n"
        )
        result = _find_tablename_mapping(str(f))
        # The two class tablenames should be found
        assert result.get("users", (None,))[0] == "User"
        assert result.get("posts", (None,))[0] == "Post"
        # The module-level tablename must not be attributed to any class
        assert "orphan_table" not in result


class TestDetectDslRegions:
    def test_detect_sql_in_execute(self, tmp_path):
        """Detects SQL in cursor.execute() calls."""
        f = tmp_path / "app.py"
        f.write_text(
            'def query(cursor):\n'
            '    cursor.execute("SELECT name FROM users WHERE id = 1")\n'
        )
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1
        assert regions[0].dsl == DslKind.SQL
        assert "SELECT" in regions[0].content

    def test_detect_sql_in_string_literal(self, tmp_path):
        """Detects SQL keywords in standalone string literals."""
        f = tmp_path / "queries.py"
        f.write_text(
            'QUERY = "SELECT id, name FROM users"\n'
        )
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1
        assert regions[0].dsl == DslKind.SQL

    def test_detect_sql_magic_comment(self, tmp_path):
        """Detects SQL via magic comment."""
        f = tmp_path / "app.py"
        f.write_text(
            '# language=sql\n'
            'q = "SELECT * FROM orders"\n'
        )
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1

    def test_no_detection_for_plain_strings(self, tmp_path):
        """Does not detect DSL in ordinary strings."""
        f = tmp_path / "app.py"
        f.write_text(
            'message = "Hello, world!"\n'
            'name = "Alice"\n'
        )
        regions = detect_dsl_regions(str(f))
        assert len(regions) == 0

    def test_detect_multiline_sql(self, tmp_path):
        """Detects SQL spanning multiple lines."""
        f = tmp_path / "app.py"
        f.write_text(
            'def query(cursor):\n'
            '    sql = """\n'
            '        SELECT name, email\n'
            '        FROM users\n'
            '        WHERE active = 1\n'
            '    """\n'
            '    cursor.execute(sql)\n'
        )
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1
        assert "SELECT" in regions[0].content

    def test_detect_update_with_multi_char_table(self, tmp_path):
        """UPDATE statements with multi-character table names must be detected.

        The SQL regex previously used UPDATE\\s+\\w (exactly one word char) which
        meant 'UPDATE users ...' was not matched while 'UPDATE u ...' was.
        """
        f = tmp_path / "app.py"
        f.write_text('q = "UPDATE users SET name=\'Alice\' WHERE id=1"\n')
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1, "UPDATE with multi-char table name must be detected as SQL"
        assert regions[0].dsl == DslKind.SQL

    def test_detect_with_cte_multi_char_name(self, tmp_path):
        """WITH clause with multi-character CTE name must be detected.

        The SQL regex previously used WITH\\s+\\w (exactly one word char) which
        missed 'WITH cte AS ...' while matching 'WITH x AS ...'.
        Test uses a magic comment so that the string is parsed as SQL regardless
        of keyword detection, then checks the raw regex directly.
        """
        from emend.dsl import _SQL_KEYWORD_RE
        # Single-char CTE name: should match
        assert _SQL_KEYWORD_RE.search("WITH x AS (...)") is not None
        # Multi-char CTE name: should also match
        assert _SQL_KEYWORD_RE.search("WITH cte AS (...)") is not None, (
            "WITH with multi-char CTE name must be detected by the SQL keyword regex"
        )

    def test_two_triple_quoted_strings_are_detected_separately(self, tmp_path):
        """Two separate triple-quoted strings must each be detected individually.

        The old regex approach with ``re.DOTALL`` could span string boundaries
        in some edge cases.  Tree-sitter correctly identifies each string node
        as a separate entity, so only the string that actually contains SQL
        keywords should be returned as an SQL region.

        The second string uses a marker phrase that must NOT appear in the SQL
        region content — confirming the two strings were never merged.
        """
        f = tmp_path / "queries.py"
        f.write_text(
            'sql = """\n'
            '    SELECT name FROM users\n'
            '"""\n'
            '\n'
            'marker = """\n'
            '    PLAIN_DOCSTRING_MARKER\n'
            '"""\n'
        )
        regions = detect_dsl_regions(str(f))
        # Only the first string contains SQL — the second must NOT be included
        sql_contents = [r.content for r in regions if r.dsl == DslKind.SQL]
        assert len(sql_contents) == 1, (
            f"Expected exactly 1 SQL region but got {len(sql_contents)}: {sql_contents}"
        )
        assert "SELECT" in sql_contents[0]
        # Confirm the plain docstring did NOT get merged or misidentified
        for content in sql_contents:
            assert "PLAIN_DOCSTRING_MARKER" not in content


class TestExtractSqlSymbols:
    def test_extract_table_from_select(self):
        region = DslRegion(
            dsl=DslKind.SQL,
            content="SELECT name FROM users WHERE id = 1",
            host_file="app.py",
            host_start_line=2,
            host_start_col=20,
            host_end_line=2,
            host_end_col=55,
            trigger="call",
        )
        symbols = extract_sql_symbols(region)
        table_names = [s.name for s in symbols if s.kind == DslSymbolKind.TABLE]
        assert "users" in table_names

    def test_extract_table_from_join(self):
        region = DslRegion(
            dsl=DslKind.SQL,
            content="SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id",
            host_file="app.py",
            host_start_line=2,
            host_start_col=0,
            host_end_line=2,
            host_end_col=60,
            trigger="call",
        )
        symbols = extract_sql_symbols(region)
        table_names = [s.name for s in symbols if s.kind == DslSymbolKind.TABLE]
        assert "users" in table_names
        assert "orders" in table_names

    def test_extract_columns_from_select(self):
        region = DslRegion(
            dsl=DslKind.SQL,
            content="SELECT name, email FROM users",
            host_file="app.py",
            host_start_line=1,
            host_start_col=0,
            host_end_line=1,
            host_end_col=30,
            trigger="call",
        )
        symbols = extract_sql_symbols(region)
        col_names = [s.name for s in symbols if s.kind == DslSymbolKind.COLUMN]
        assert "name" in col_names
        assert "email" in col_names

    def test_extract_table_from_insert(self):
        region = DslRegion(
            dsl=DslKind.SQL,
            content="INSERT INTO users (name, email) VALUES (:name, :email)",
            host_file="app.py",
            host_start_line=1,
            host_start_col=0,
            host_end_line=1,
            host_end_col=55,
            trigger="call",
        )
        symbols = extract_sql_symbols(region)
        table_names = [s.name for s in symbols if s.kind == DslSymbolKind.TABLE]
        assert "users" in table_names

    def test_link_hints_for_table(self):
        region = DslRegion(
            dsl=DslKind.SQL,
            content="SELECT * FROM users",
            host_file="app.py",
            host_start_line=1,
            host_start_col=0,
            host_end_line=1,
            host_end_col=20,
            trigger="call",
        )
        symbols = extract_sql_symbols(region)
        tables = [s for s in symbols if s.kind == DslSymbolKind.TABLE]
        assert len(tables) >= 1
        assert any(h.strategy == "orm_model" for h in tables[0].link_hints)
        assert any(h.target_pattern == "User" for h in tables[0].link_hints)


class TestResolvOrmLinks:
    def test_resolve_table_to_class(self, tmp_path):
        """Resolves SQL table name to ORM model class."""
        model_file = tmp_path / "models.py"
        model_file.write_text(
            'from sqlalchemy import Column, Integer, String\n'
            'from sqlalchemy.ext.declarative import declarative_base\n'
            '\n'
            'Base = declarative_base()\n'
            '\n'
            'class User(Base):\n'
            '    __tablename__ = "users"\n'
            '    id = Column(Integer, primary_key=True)\n'
            '    name = Column(String)\n'
            '    email = Column(String)\n'
        )
        symbol = DslSymbol(
            name="users",
            kind=DslSymbolKind.TABLE,
            dsl=DslKind.SQL,
            host_file="app.py",
            host_line=5,
            host_col=20,
            link_hints=[
                LinkHint(strategy="orm_model", target_pattern="User", target_kind="class"),
            ],
        )
        links = resolve_orm_links([symbol], str(tmp_path))
        assert len(links) >= 1
        assert "User" in links[0].target_qualified_name

    def test_resolve_no_match(self, tmp_path):
        """Returns empty when no matching class found."""
        model_file = tmp_path / "models.py"
        model_file.write_text("x = 1\n")
        symbol = DslSymbol(
            name="widgets",
            kind=DslSymbolKind.TABLE,
            dsl=DslKind.SQL,
            host_file="app.py",
            host_line=1,
            host_col=0,
            link_hints=[
                LinkHint(strategy="orm_model", target_pattern="Widget", target_kind="class"),
            ],
        )
        links = resolve_orm_links([symbol], str(tmp_path))
        assert len(links) == 0


class TestAnalyzeFile:
    def test_analyze_file_with_sql(self, tmp_path):
        """End-to-end: detect SQL, extract symbols."""
        f = tmp_path / "app.py"
        f.write_text(
            'def query(cursor):\n'
            '    cursor.execute("SELECT name FROM users WHERE id = 1")\n'
        )
        symbols, links = analyze_file(str(f))
        assert len(symbols) >= 1
        table_names = [s.name for s in symbols if s.kind == DslSymbolKind.TABLE]
        assert "users" in table_names

    def test_analyze_file_no_dsl(self, tmp_path):
        """No symbols from files without DSL content."""
        f = tmp_path / "app.py"
        f.write_text("x = 1\nprint(x)\n")
        symbols, links = analyze_file(str(f))
        assert len(symbols) == 0


class TestFormatSymbols:
    def test_text_format(self):
        symbols = [
            DslSymbol(
                name="users", kind=DslSymbolKind.TABLE, dsl=DslKind.SQL,
                host_file="app.py", host_line=5, host_col=20,
            ),
        ]
        output = format_symbols(symbols)
        assert "users" in output
        assert "table" in output.lower()

    def test_json_format(self):
        symbols = [
            DslSymbol(
                name="users", kind=DslSymbolKind.TABLE, dsl=DslKind.SQL,
                host_file="app.py", host_line=5, host_col=20,
            ),
        ]
        output = format_symbols(symbols, json_output=True)
        data = json.loads(output)
        assert isinstance(data, list)
        assert data[0]["name"] == "users"
        assert data[0]["kind"] == "table"


class TestDslDebugCommand:
    """Tests for the renamed dsl-debug CLI command."""

    def test_dsl_debug_command_exists(self):
        """The dsl-debug command is registered."""
        from typer.testing import CliRunner
        from emend.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["dsl-debug", "--help"])
        assert result.exit_code == 0
        assert "Debug" in result.output or "debug" in result.output or "DSL" in result.output

    def test_dsl_hidden_alias(self):
        """The old 'dsl' command still works as a hidden alias."""
        from typer.testing import CliRunner
        from emend.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["dsl", "--help"])
        assert result.exit_code == 0

    def test_dsl_debug_detects_sql(self, tmp_path):
        """dsl-debug detects SQL in files."""
        from typer.testing import CliRunner
        from emend.cli import app
        f = tmp_path / "app.py"
        f.write_text(
            'def query(cursor):\n'
            '    cursor.execute("SELECT name FROM users WHERE id = 1")\n'
        )
        runner = CliRunner()
        result = runner.invoke(app, ["dsl-debug", str(f)])
        assert result.exit_code == 0
        assert "users" in result.output


class TestSearchDslSymbols:
    """Tests for automatic DSL symbol discovery in search."""

    def test_search_finds_sql_tables(self, tmp_path):
        """search surfaces SQL table symbols alongside host-language results."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "app.py"
        f.write_text(
            'class User:\n'
            '    pass\n'
            '\n'
            'QUERY = "SELECT name FROM users WHERE id = 1"\n'
        )
        runner = CliRunner()
        result = runner.invoke(app, ["grep", str(f) + "::User"])
        assert result.exit_code == 0

    def test_search_no_crash_without_dsl(self, tmp_path):
        """search doesn't crash on files without DSL content."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "app.py"
        f.write_text('def hello():\n    pass\n')
        runner = CliRunner()
        result = runner.invoke(app, ["grep", str(f) + "::hello"])
        assert result.exit_code == 0


class TestRefsDslSymbols:
    """Tests for automatic DSL reference discovery in refs."""

    def test_refs_finds_sql_references(self, tmp_path):
        """refs surfaces SQL table references for ORM models."""
        from typer.testing import CliRunner
        from emend.cli import app

        model_file = tmp_path / "models.py"
        model_file.write_text(
            'class User:\n'
            '    __tablename__ = "users"\n'
            '    pass\n'
        )
        query_file = tmp_path / "queries.py"
        query_file.write_text(
            'QUERY = "SELECT name FROM users WHERE active = 1"\n'
        )
        runner = CliRunner()
        result = runner.invoke(app, [
            "refs", str(model_file) + "::User",
            "--project", str(tmp_path),
        ])
        assert result.exit_code == 0
        assert "queries.py" in result.output

    def test_refs_no_crash_without_matches(self, tmp_path):
        """refs doesn't crash when no DSL matches exist."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "app.py"
        f.write_text('def foo():\n    pass\n')
        runner = CliRunner()
        result = runner.invoke(app, [
            "refs", str(f) + "::foo",
            "--project", str(tmp_path),
        ])
        assert result.exit_code == 0

    def test_refs_json_includes_dsl(self, tmp_path):
        """refs --json includes DSL references."""
        model_file = tmp_path / "models.py"
        model_file.write_text(
            'class User:\n'
            '    __tablename__ = "users"\n'
            '    pass\n'
        )
        query_file = tmp_path / "queries.py"
        query_file.write_text(
            'QUERY = "SELECT name FROM users WHERE active = 1"\n'
        )
        from typer.testing import CliRunner
        from emend.cli import app
        runner = CliRunner()
        result = runner.invoke(app, [
            "refs", str(model_file) + "::User",
            "--json", "--project", str(tmp_path),
        ])
        assert result.exit_code == 0

    def test_refs_json_single_valid_json_with_dsl(self, tmp_path):
        """refs --json should output a single valid JSON array, not two."""
        model_file = tmp_path / "models.py"
        model_file.write_text(
            'class User:\n'
            '    __tablename__ = "users"\n'
            '    pass\n'
        )
        query_file = tmp_path / "queries.py"
        query_file.write_text(
            'QUERY = "SELECT name FROM users WHERE active = 1"\n'
        )
        import json
        from typer.testing import CliRunner
        from emend.cli import app
        runner = CliRunner()
        result = runner.invoke(app, [
            "refs", str(model_file) + "::User",
            "--json", "--project", str(tmp_path),
        ])
        assert result.exit_code == 0
        output = result.stdout.strip()
        if output:
            data = json.loads(output)
            assert isinstance(data, list), "Output should be a single JSON array"


class TestGotoLocalDslFallback:
    """Tests for DSL fallback in goto_definition."""

    def test_goto_definition_resolves_sql_to_orm_model(self, tmp_path):
        """goto_definition resolves SQL table name to ORM model class."""
        model_file = tmp_path / "models.py"
        model_file.write_text(
            'class User:\n'
            '    __tablename__ = "users"\n'
            '    pass\n'
        )
        query_file = tmp_path / "queries.py"
        query_file.write_text(
            'QUERY = "SELECT name FROM users WHERE active = 1"\n'
        )
        from emend.editor_search import EditorSearchEngine
        engine = EditorSearchEngine(str(tmp_path))
        result = engine.goto_definition(file=str(query_file), line=1, col=25)
        assert result.mode == "symbol"
        if result.items:
            assert any("User" in item.get("qualified_name", "") for item in result.items)

    def test_goto_definition_no_dsl_when_normal_ref(self, tmp_path):
        """goto_definition uses normal resolution for non-DSL code."""
        f = tmp_path / "app.py"
        f.write_text('def hello():\n    pass\nhello()\n')
        from emend.editor_search import EditorSearchEngine
        engine = EditorSearchEngine(str(tmp_path))
        result = engine.goto_definition(file=str(f), line=3, col=1)
        assert result.mode == "symbol"

    def test_goto_definition_empty_for_plain_string(self, tmp_path):
        """goto_definition returns empty for non-DSL strings."""
        f = tmp_path / "app.py"
        f.write_text('msg = "hello world"\n')
        from emend.editor_search import EditorSearchEngine
        engine = EditorSearchEngine(str(tmp_path))
        result = engine.goto_definition(file=str(f), line=1, col=8)
        # No DSL content, no identifier — empty is fine
        assert result.mode == "symbol"


class TestFindInDsl:
    """Tests for find_in_dsl — pattern matching inside DSL regions."""

    def test_find_select_star(self, tmp_path):
        """Finds SELECT * pattern in SQL regions."""
        f = tmp_path / "app.py"
        f.write_text(
            'QUERY = "SELECT * FROM users"\n'
        )
        matches = find_in_dsl("SELECT * FROM $TABLE", str(f), dsl_type="sql")
        assert len(matches) >= 1
        assert matches[0].captures.get("TABLE") == "users"

    def test_find_with_metavar(self, tmp_path):
        """Metavar captures work in DSL find patterns."""
        f = tmp_path / "app.py"
        f.write_text(
            'q = "SELECT name, email FROM users WHERE active = 1"\n'
        )
        matches = find_in_dsl("SELECT $COLS FROM $TABLE", str(f), dsl_type="sql")
        assert len(matches) >= 1
        assert "users" in matches[0].captures.get("TABLE", "")

    def test_find_no_match(self, tmp_path):
        """Returns empty when pattern doesn't match."""
        f = tmp_path / "app.py"
        f.write_text('x = "hello world"\n')
        matches = find_in_dsl("SELECT * FROM $TABLE", str(f), dsl_type="sql")
        assert len(matches) == 0

    def test_find_reports_host_line(self, tmp_path):
        """Reports correct host file line number."""
        f = tmp_path / "app.py"
        f.write_text(
            'x = 1\n'
            'y = 2\n'
            'QUERY = "SELECT id FROM orders"\n'
        )
        matches = find_in_dsl("SELECT $COLS FROM $TABLE", str(f), dsl_type="sql")
        assert len(matches) >= 1
        assert matches[0].host_line == 3

    def test_find_multiline_sql(self, tmp_path):
        """Finds patterns in multiline SQL strings."""
        f = tmp_path / "app.py"
        f.write_text(
            'sql = """\n'
            '    SELECT name\n'
            '    FROM users\n'
            '    WHERE active = 1\n'
            '"""\n'
        )
        matches = find_in_dsl("SELECT $COLS FROM $TABLE", str(f), dsl_type="sql")
        assert len(matches) >= 1


class TestDslLintRules:
    """Tests for DSL-aware lint rules."""

    def test_dsl_lint_rule_detects_select_star(self, tmp_path):
        """DSL lint rules detect patterns in SQL regions."""
        from emend.lint import LintRule, run_lint

        f = tmp_path / "app.py"
        f.write_text('QUERY = "SELECT * FROM users"\n')

        rule = LintRule(
            name="no-select-star",
            find="SELECT * FROM $TABLE",
            message="Avoid SELECT *; enumerate columns explicitly",
            dsl="sql",
        )
        violations = run_lint([rule], [str(f)])
        assert len(violations) >= 1
        assert violations[0].rule_name == "no-select-star"

    def test_dsl_lint_rule_no_false_positive(self, tmp_path):
        """DSL lint rules don't match non-DSL content."""
        from emend.lint import LintRule, run_lint

        f = tmp_path / "app.py"
        f.write_text('msg = "hello world"\n')

        rule = LintRule(
            name="no-select-star",
            find="SELECT * FROM $TABLE",
            message="Avoid SELECT *",
            dsl="sql",
        )
        violations = run_lint([rule], [str(f)])
        assert len(violations) == 0

    def test_dsl_lint_from_yaml(self, tmp_path):
        """DSL lint rules load from YAML config."""
        from emend.lint import load_rules

        config = tmp_path / "patterns.yaml"
        config.write_text(
            'rules:\n'
            '  no-select-star:\n'
            '    dsl: sql\n'
            '    find: "SELECT * FROM $TABLE"\n'
            '    message: "Avoid SELECT *"\n'
        )
        rules, macros, dc = load_rules(str(config))
        assert len(rules) == 1
        assert rules[0].dsl == "sql"
        assert rules[0].name == "no-select-star"


class TestRegexNamedGroups:
    """Tests for regex named group navigation."""

    def test_extract_named_groups(self, tmp_path):
        """Extracts named groups from regex patterns."""
        f = tmp_path / "app.py"
        f.write_text(
            'import re\n'
            'PATTERN = re.compile(r"(?P<year>\\d{4})-(?P<month>\\d{2})-(?P<day>\\d{2})")\n'
            'm = PATTERN.match(text)\n'
            'year = m.group("year")\n'
            'month = m.group("month")\n'
        )
        groups = extract_regex_named_groups(str(f))
        names = {g.name for g in groups}
        assert "year" in names
        assert "month" in names
        assert "day" in names

    def test_named_groups_link_to_usages(self, tmp_path):
        """Named groups are linked to .group() call sites."""
        f = tmp_path / "app.py"
        f.write_text(
            'import re\n'
            'p = re.compile(r"(?P<slug>[a-z-]+)")\n'
            'm = p.match(url)\n'
            'slug = m.group("slug")\n'
        )
        groups = extract_regex_named_groups(str(f))
        slug_groups = [g for g in groups if g.name == "slug"]
        assert len(slug_groups) == 1
        assert len(slug_groups[0].usages) == 1
        assert slug_groups[0].usages[0][1] == 4  # line 4

    def test_no_groups_in_plain_code(self, tmp_path):
        """Returns empty for files without regex named groups."""
        f = tmp_path / "app.py"
        f.write_text('x = 1\nprint(x)\n')
        groups = extract_regex_named_groups(str(f))
        assert len(groups) == 0

    def test_find_group_references(self, tmp_path):
        """Finds .group() call sites across project."""
        f1 = tmp_path / "patterns.py"
        f1.write_text('import re\np = re.compile(r"(?P<slug>[a-z]+)")\n')
        f2 = tmp_path / "views.py"
        f2.write_text('slug = m.group("slug")\n')
        refs = find_regex_group_references("slug", str(tmp_path))
        assert len(refs) >= 1
        assert any("views.py" in r[0] for r in refs)


class TestDslImpact:
    """Tests for impact command DSL integration."""

    def test_find_dsl_impact(self, tmp_path):
        """Changes to ORM model surface affected SQL queries."""
        models = tmp_path / "models.py"
        models.write_text(
            'class User:\n'
            '    __tablename__ = "users"\n'
            '    name = ""\n'
        )
        queries = tmp_path / "queries.py"
        queries.write_text(
            'QUERY = "SELECT name FROM users WHERE active = 1"\n'
        )
        impacts = find_dsl_impact(
            ["models.py::User"], str(tmp_path)
        )
        assert len(impacts) >= 1
        assert any("users" in reason for _, _, reason in impacts)

    def test_find_dsl_impact_no_match(self, tmp_path):
        """No impacts when no SQL references changed classes."""
        f = tmp_path / "app.py"
        f.write_text('x = 1\n')
        impacts = find_dsl_impact(["app.py::Foo"], str(tmp_path))
        assert len(impacts) == 0

    def test_find_dsl_impact_convention_based(self, tmp_path):
        """Convention-based matching (PascalCase -> snake_case plural)."""
        models = tmp_path / "models.py"
        models.write_text('class OrderItem:\n    pass\n')
        queries = tmp_path / "queries.py"
        queries.write_text(
            'q = "SELECT id FROM order_items WHERE status = 1"\n'
        )
        impacts = find_dsl_impact(
            ["models.py::OrderItem"], str(tmp_path)
        )
        assert len(impacts) >= 1


class TestDslSearchCommand:
    """Tests for --dsl flag in search command."""

    def test_search_dsl_sql(self, tmp_path):
        """search --dsl sql finds patterns in SQL regions."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "app.py"
        f.write_text('QUERY = "SELECT * FROM users"\n')
        runner = CliRunner()
        result = runner.invoke(app, [
            "grep", "SELECT", str(f), "--dsl", "sql",
        ])
        assert result.exit_code == 0
        assert "SELECT" in result.output

    def test_search_dsl_no_match(self, tmp_path):
        """search --dsl returns nothing for non-DSL content."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "app.py"
        f.write_text('x = "hello"\n')
        runner = CliRunner()
        result = runner.invoke(app, [
            "grep", "SELECT", str(f), "--dsl", "sql",
        ])
        assert result.exit_code == 0
        assert "SELECT" not in result.output

    def test_pattern_find_does_not_include_dsl_noise_by_default(self, tmp_path):
        """Pattern find should NOT include DSL symbols by default.

        When running a Python pattern search (e.g. '$X.objects.get($...ARGS)'),
        the output should NOT include [sql:table] or [graphql:graphql_type]
        matches from docstrings/comments. DSL symbols should only appear when
        --dsl is explicitly provided.
        """
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "app.py"
        f.write_text(
            '"""SELECT * FROM users WHERE id = %s"""\n'
            "\n"
            "class User:\n"
            "    def get_user(self, uid):\n"
            "        obj = User.objects.get(id=uid)\n"
            "        return obj\n"
        )
        runner = CliRunner()
        result = runner.invoke(app, ["find", "$X.objects.get($...ARGS)", str(f), "--output", "code"])
        assert result.exit_code == 0
        # Should find the Python pattern match
        assert "objects.get" in result.output
        # Should NOT include DSL noise in default mode
        assert "[sql:" not in result.output
        assert "[graphql:" not in result.output

    def test_pattern_find_includes_dsl_with_explicit_flag(self, tmp_path):
        """Pattern find with --dsl flag explicitly searches DSL regions."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "app.py"
        f.write_text('QUERY = "SELECT * FROM users"\n')
        runner = CliRunner()
        result = runner.invoke(app, ["find", "SELECT $...REST", str(f), "--dsl", "sql"])
        assert result.exit_code == 0
        assert "[sql" in result.output or "users" in result.output


class TestDslIndexTables:
    """Tests for dsl_symbols and dsl_links tables in parse.db."""

    def test_dsl_tables_created(self, tmp_path):
        """parse.db schema includes dsl_symbols and dsl_links tables."""
        import sqlite3
        from emend.transform import _init_cache_schema

        db_path = tmp_path / "parse.db"
        conn = sqlite3.connect(str(db_path))
        _init_cache_schema(conn)

        # Check dsl_symbols table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dsl_symbols'"
        )
        assert cursor.fetchone() is not None

        # Check dsl_links table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dsl_links'"
        )
        assert cursor.fetchone() is not None

        conn.close()

    def test_dsl_symbols_insertable(self, tmp_path):
        """Can insert DSL symbols into dsl_symbols table."""
        import sqlite3
        from emend.transform import _init_cache_schema

        db_path = tmp_path / "parse.db"
        conn = sqlite3.connect(str(db_path))
        _init_cache_schema(conn)

        conn.execute(
            "INSERT INTO dsl_symbols "
            "(name, kind, dsl, host_file, host_start_line, host_start_col, "
            "host_end_line, host_end_col, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("users", "table", "sql", "app.py", 1, 0, 1, 30, b"abc123"),
        )
        conn.commit()

        rows = conn.execute("SELECT name, kind, dsl FROM dsl_symbols").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("users", "table", "sql")

        conn.close()


# ====================================================================
# Jinja2 / Django template support (Phase 4)
# ====================================================================


class TestDetectJinjaRegions:
    """Tests for Jinja2 template region detection."""

    def test_detect_jinja_in_standalone_html(self, tmp_path):
        """Detects Jinja2 syntax in .html template files."""
        f = tmp_path / "profile.html"
        f.write_text(
            '{% extends "base.html" %}\n'
            '{% block content %}\n'
            '  <h1>{{ user.name }}</h1>\n'
            '{% endblock %}\n'
        )
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1
        assert regions[0].dsl == DslKind.JINJA
        assert "{{ user.name }}" in regions[0].content

    def test_detect_jinja_in_j2_file(self, tmp_path):
        """Detects Jinja2 in .jinja2 files."""
        f = tmp_path / "email.jinja2"
        f.write_text('Hello {{ name }},\nWelcome to {{ site }}!\n')
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1
        assert regions[0].dsl == DslKind.JINJA
        assert regions[0].trigger == "file_extension"

    def test_detect_jinja_in_python_string(self, tmp_path):
        """Detects Jinja2 template in Python string literal."""
        f = tmp_path / "app.py"
        f.write_text(
            'template = """\n'
            '{% for item in items %}\n'
            '  <li>{{ item.name }}</li>\n'
            '{% endfor %}\n'
            '"""\n'
        )
        regions = detect_dsl_regions(str(f))
        jinja_regions = [r for r in regions if r.dsl == DslKind.JINJA]
        assert len(jinja_regions) >= 1

    def test_no_jinja_in_plain_strings(self, tmp_path):
        """Does not detect Jinja2 in ordinary strings."""
        f = tmp_path / "app.py"
        f.write_text('msg = "Hello, world!"\nname = "Alice"\n')
        regions = detect_dsl_regions(str(f))
        jinja_regions = [r for r in regions if r.dsl == DslKind.JINJA]
        assert len(jinja_regions) == 0


class TestExtractJinjaSymbols:
    """Tests for Jinja2 symbol extraction."""

    def test_extract_template_variables(self):
        """Extracts template variables from {{ expr }}."""
        region = DslRegion(
            dsl=DslKind.JINJA,
            content='<h1>{{ user.name }}</h1>\n<p>{{ posts }}</p>',
            host_file="profile.html",
            host_start_line=1,
            host_start_col=0,
            host_end_line=2,
            host_end_col=20,
            trigger="file_extension",
        )
        symbols = extract_jinja_symbols(region)
        var_names = [s.name for s in symbols if s.kind == DslSymbolKind.TEMPLATE_VAR]
        assert "user" in var_names
        assert "posts" in var_names

    def test_extract_block_definitions(self):
        """Extracts block names from {% block name %}."""
        region = DslRegion(
            dsl=DslKind.JINJA,
            content='{% block content %}\n  <p>body</p>\n{% endblock %}\n{% block sidebar %}{% endblock %}',
            host_file="layout.html",
            host_start_line=1,
            host_start_col=0,
            host_end_line=4,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_jinja_symbols(region)
        block_names = [s.name for s in symbols if any(h.strategy == "template_block" for h in s.link_hints)]
        assert "content" in block_names
        assert "sidebar" in block_names

    def test_extract_macro_definitions(self):
        """Extracts macro names from {% macro name() %}."""
        region = DslRegion(
            dsl=DslKind.JINJA,
            content='{% macro render_field(field) %}\n  <div>{{ field.label }}</div>\n{% endmacro %}',
            host_file="forms.html",
            host_start_line=1,
            host_start_col=0,
            host_end_line=3,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_jinja_symbols(region)
        macro_names = [s.name for s in symbols]
        assert "render_field" in macro_names

    def test_extract_for_loop_variables(self):
        """Extracts iterable variable from {% for x in items %}."""
        region = DslRegion(
            dsl=DslKind.JINJA,
            content='{% for post in posts %}\n  <h2>{{ post.title }}</h2>\n{% endfor %}',
            host_file="blog.html",
            host_start_line=1,
            host_start_col=0,
            host_end_line=3,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_jinja_symbols(region)
        var_names = [s.name for s in symbols if s.kind == DslSymbolKind.TEMPLATE_VAR]
        assert "posts" in var_names

    def test_skip_jinja_builtins(self):
        """Does not extract Jinja2 built-in variables."""
        region = DslRegion(
            dsl=DslKind.JINJA,
            content='{{ loop.index }}\n{{ range(10) }}\n{{ true }}',
            host_file="tmpl.html",
            host_start_line=1,
            host_start_col=0,
            host_end_line=3,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_jinja_symbols(region)
        var_names = [s.name for s in symbols if s.kind == DslSymbolKind.TEMPLATE_VAR]
        assert "loop" not in var_names
        assert "range" not in var_names
        assert "true" not in var_names

    def test_link_hints_for_template_var(self):
        """Template variables get template_var link hints."""
        region = DslRegion(
            dsl=DslKind.JINJA,
            content='{{ user.name }}',
            host_file="profile.html",
            host_start_line=1,
            host_start_col=0,
            host_end_line=1,
            host_end_col=15,
            trigger="file_extension",
        )
        symbols = extract_jinja_symbols(region)
        assert len(symbols) >= 1
        assert any(h.strategy == "template_var" for h in symbols[0].link_hints)
        assert any(h.target_pattern == "user" for h in symbols[0].link_hints)


class TestResolveJinjaLinks:
    """Tests for Jinja2 template variable resolution."""

    def test_resolve_template_var_to_render_call(self, tmp_path):
        """Resolves template variable to render_template() context."""
        views = tmp_path / "views.py"
        views.write_text(
            'from flask import render_template\n'
            '\n'
            '@app.route("/profile")\n'
            'def profile():\n'
            '    user = get_current_user()\n'
            '    return render_template("profile.html", user=user, posts=user.posts)\n'
        )
        symbol = DslSymbol(
            name="user",
            kind=DslSymbolKind.TEMPLATE_VAR,
            dsl=DslKind.JINJA,
            host_file=str(tmp_path / "profile.html"),
            host_line=3,
            host_col=6,
            link_hints=[
                LinkHint(strategy="template_var", target_pattern="user", target_kind="variable"),
            ],
        )
        links = resolve_jinja_links([symbol], str(tmp_path))
        assert len(links) >= 1
        assert "render_template" in links[0].target_qualified_name

    def test_resolve_block_to_parent_template(self, tmp_path):
        """Resolves block to matching block in parent template."""
        base = tmp_path / "base.html"
        base.write_text(
            '<html>\n'
            '{% block content %}{% endblock %}\n'
            '{% block sidebar %}{% endblock %}\n'
            '</html>\n'
        )
        child = tmp_path / "page.html"
        child.write_text(
            '{% extends "base.html" %}\n'
            '{% block content %}\n'
            '  <p>Page content</p>\n'
            '{% endblock %}\n'
        )
        symbol = DslSymbol(
            name="content",
            kind=DslSymbolKind.TEMPLATE_VAR,
            dsl=DslKind.JINJA,
            host_file=str(child),
            host_line=2,
            host_col=0,
            link_hints=[
                LinkHint(strategy="template_block", target_pattern="content", target_kind="block"),
            ],
        )
        links = resolve_jinja_links([symbol], str(tmp_path))
        assert len(links) >= 1
        assert "base" in links[0].target_file
        assert links[0].strategy == "template_block"

    def test_resolve_block_parent_gets_higher_confidence_than_unrelated(self, tmp_path):
        """Block in the actual parent template should get higher confidence
        than a block in an unrelated template that happens to share the name.

        The confidence heuristic should check whether the *target* file is
        the parent of the current file, not merely whether the current file
        has any extends relationship.
        """
        base = tmp_path / "base.html"
        base.write_text(
            '<html>\n'
            '{% block content %}{% endblock %}\n'
            '</html>\n'
        )
        unrelated = tmp_path / "other.html"
        unrelated.write_text(
            '<html>\n'
            '{% block content %}{% endblock %}\n'
            '</html>\n'
        )
        child = tmp_path / "page.html"
        child.write_text(
            '{% extends "base.html" %}\n'
            '{% block content %}\n'
            '  <p>Page content</p>\n'
            '{% endblock %}\n'
        )
        symbol = DslSymbol(
            name="content",
            kind=DslSymbolKind.TEMPLATE_VAR,
            dsl=DslKind.JINJA,
            host_file=str(child),
            host_line=2,
            host_col=0,
            link_hints=[
                LinkHint(strategy="template_block", target_pattern="content", target_kind="block"),
            ],
        )
        links = resolve_jinja_links([symbol], str(tmp_path))
        assert len(links) == 2, f"expected links to base and other, got {len(links)}"
        link_by_file = {link.target_file: link for link in links}
        base_link = link_by_file.get(str(base))
        other_link = link_by_file.get(str(unrelated))
        assert base_link is not None, "expected link to parent template base.html"
        assert other_link is not None, "expected link to unrelated template other.html"
        assert base_link.confidence > other_link.confidence, (
            f"parent template block should have higher confidence ({base_link.confidence}) "
            f"than unrelated template block ({other_link.confidence})"
        )

    def test_resolve_no_match(self, tmp_path):
        """Returns empty when no matching render_template() call found."""
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        symbol = DslSymbol(
            name="unknown_var",
            kind=DslSymbolKind.TEMPLATE_VAR,
            dsl=DslKind.JINJA,
            host_file=str(tmp_path / "missing.html"),
            host_line=1,
            host_col=0,
            link_hints=[
                LinkHint(strategy="template_var", target_pattern="unknown_var", target_kind="variable"),
            ],
        )
        links = resolve_jinja_links([symbol], str(tmp_path))
        assert len(links) == 0


class TestJinjaAnalyzeFile:
    """End-to-end Jinja2 analysis tests."""

    def test_analyze_jinja_template(self, tmp_path):
        """End-to-end: detect Jinja2 template, extract symbols."""
        f = tmp_path / "template.html"
        f.write_text(
            '{% extends "base.html" %}\n'
            '{% block content %}\n'
            '  <h1>{{ title }}</h1>\n'
            '  {% for item in items %}\n'
            '    <p>{{ item.name }}</p>\n'
            '  {% endfor %}\n'
            '{% endblock %}\n'
        )
        symbols, links = analyze_file(str(f))
        assert len(symbols) >= 2
        sym_names = [s.name for s in symbols]
        assert "title" in sym_names
        assert "items" in sym_names
        assert "content" in sym_names

    def test_analyze_file_with_jinja_in_python(self, tmp_path):
        """Detects Jinja2 embedded in Python string."""
        f = tmp_path / "app.py"
        f.write_text(
            'tpl = """\n'
            '{% if show %}\n'
            '  <p>{{ message }}</p>\n'
            '{% endif %}\n'
            '"""\n'
        )
        symbols, links = analyze_file(str(f))
        jinja_syms = [s for s in symbols if s.dsl == DslKind.JINJA]
        assert len(jinja_syms) >= 1
        names = [s.name for s in jinja_syms]
        assert "message" in names


class TestJinjaDslDebugCommand:
    """Tests for Jinja2 in dsl-debug command."""

    def test_dsl_debug_jinja(self, tmp_path):
        """dsl-debug --type jinja detects Jinja2 templates."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "template.html"
        f.write_text(
            '{% block header %}\n'
            '  <h1>{{ title }}</h1>\n'
            '{% endblock %}\n'
        )
        runner = CliRunner()
        result = runner.invoke(app, ["dsl-debug", str(f), "--type", "jinja"])
        assert result.exit_code == 0
        assert "title" in result.output or "header" in result.output


class TestJinjaFindInDsl:
    """Tests for find_in_dsl with Jinja2 regions."""

    def test_find_jinja_pattern(self, tmp_path):
        """Finds patterns inside Jinja2 template files."""
        f = tmp_path / "template.html"
        f.write_text(
            '{% block content %}\n'
            '  <h1>{{ title }}</h1>\n'
            '{% endblock %}\n'
        )
        matches = find_in_dsl("{{ $VAR }}", str(f), dsl_type="jinja")
        assert len(matches) >= 1
        assert matches[0].captures.get("VAR") is not None


# ====================================================================
# GraphQL support (Phase 4)
# ====================================================================


class TestDetectGraphqlRegions:
    """Tests for GraphQL region detection."""

    def test_detect_graphql_in_standalone_file(self, tmp_path):
        """Detects GraphQL in .graphql files."""
        f = tmp_path / "schema.graphql"
        f.write_text(
            'type User {\n'
            '  id: ID!\n'
            '  email: String!\n'
            '  posts: [Post!]!\n'
            '}\n'
            '\n'
            'type Query {\n'
            '  user(id: ID!): User\n'
            '}\n'
        )
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1
        assert regions[0].dsl == DslKind.GRAPHQL
        assert regions[0].trigger == "file_extension"

    def test_detect_graphql_in_gql_file(self, tmp_path):
        """Detects GraphQL in .gql files."""
        f = tmp_path / "queries.gql"
        f.write_text('query GetUser { user { id name } }\n')
        regions = detect_dsl_regions(str(f))
        assert len(regions) >= 1
        assert regions[0].dsl == DslKind.GRAPHQL

    def test_detect_graphql_in_python_string(self, tmp_path):
        """Detects GraphQL schema in Python string literal."""
        f = tmp_path / "schema.py"
        f.write_text(
            'SCHEMA = """\n'
            'type User {\n'
            '  id: ID!\n'
            '  name: String!\n'
            '}\n'
            '"""\n'
        )
        regions = detect_dsl_regions(str(f))
        gql_regions = [r for r in regions if r.dsl == DslKind.GRAPHQL]
        assert len(gql_regions) >= 1

    def test_no_graphql_in_plain_strings(self, tmp_path):
        """Does not detect GraphQL in ordinary strings."""
        f = tmp_path / "app.py"
        f.write_text('msg = "Hello, world!"\n')
        regions = detect_dsl_regions(str(f))
        gql_regions = [r for r in regions if r.dsl == DslKind.GRAPHQL]
        assert len(gql_regions) == 0


class TestExtractGraphqlSymbols:
    """Tests for GraphQL symbol extraction."""

    def test_extract_type_definitions(self):
        """Extracts type names from GraphQL schema."""
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='type User {\n  id: ID!\n  email: String!\n}\n\ntype Post {\n  title: String!\n}',
            host_file="schema.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=8,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        type_names = [s.name for s in symbols if s.kind == DslSymbolKind.GRAPHQL_TYPE]
        assert "User" in type_names
        assert "Post" in type_names

    def test_extract_field_definitions(self):
        """Extracts field names from GraphQL types."""
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='type User {\n  email: String!\n  posts: [Post!]!\n}',
            host_file="schema.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=4,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        field_names = [s.name for s in symbols if s.kind == DslSymbolKind.GRAPHQL_FIELD]
        assert "email" in field_names
        assert "posts" in field_names

    def test_extract_input_and_enum(self):
        """Extracts input and enum type definitions."""
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='input CreateUserInput {\n  name: String!\n}\n\nenum Role {\n  ADMIN\n  USER\n}',
            host_file="schema.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=8,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        type_names = [s.name for s in symbols if s.kind == DslSymbolKind.GRAPHQL_TYPE]
        assert "CreateUserInput" in type_names
        assert "Role" in type_names

    def test_extract_query_operations(self):
        """Extracts query/mutation operation names."""
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='query GetUser($id: ID!) {\n  user(id: $id) {\n    name\n  }\n}\n\nmutation CreateUser($input: CreateUserInput!) {\n  createUser(input: $input) {\n    id\n  }\n}',
            host_file="queries.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=11,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        op_names = [s.name for s in symbols if s.kind == DslSymbolKind.GRAPHQL_TYPE]
        assert "GetUser" in op_names
        assert "CreateUser" in op_names

    def test_skip_builtin_types(self):
        """Does not extract GraphQL built-in types."""
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='type User {\n  name: String\n  age: Int\n  active: Boolean\n}',
            host_file="schema.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=5,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        type_names = [s.name for s in symbols if s.kind == DslSymbolKind.GRAPHQL_TYPE]
        assert "String" not in type_names
        assert "Int" not in type_names
        assert "Boolean" not in type_names

    def test_link_hints_for_types(self):
        """Type symbols get graphql_type link hints with resolver name."""
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='type User {\n  id: ID!\n}',
            host_file="schema.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=3,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        user_types = [s for s in symbols if s.name == "User"]
        assert len(user_types) >= 1
        hints = user_types[0].link_hints
        assert any(h.strategy == "graphql_type" and h.target_pattern == "UserResolver" for h in hints)

    def test_link_hints_for_fields(self):
        """Field symbols get graphql_field link hints with parent type."""
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='type User {\n  posts: [Post!]!\n}',
            host_file="schema.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=3,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        fields = [s for s in symbols if s.kind == DslSymbolKind.GRAPHQL_FIELD]
        assert len(fields) >= 1
        assert any(h.strategy == "graphql_field" and h.module_hint == "User" for h in fields[0].link_hints)

    def test_field_line_numbers_offset_from_region_start(self):
        """GraphQL field host_line should reflect actual position within region.

        When a region starts at line 5 of the host file, a field on the 2nd
        line of the region content should report host_line=6, not 5.
        """
        # Region starts at line 5: "type User {" is at offset 0 (line 5),
        # "  name: String!" is at offset 1 (line 6), "  email: String!" at offset 2 (line 7)
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='type User {\n  name: String!\n  email: String!\n}',
            host_file="schema.graphql",
            host_start_line=5,
            host_start_col=0,
            host_end_line=8,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        fields_by_name = {s.name: s for s in symbols if s.kind == DslSymbolKind.GRAPHQL_FIELD}
        assert "name" in fields_by_name
        assert "email" in fields_by_name
        # "name" is on line 2 of content (offset 1), so host_line should be 5+1=6
        assert fields_by_name["name"].host_line == 6, (
            f"Expected name field at line 6, got {fields_by_name['name'].host_line}"
        )
        # "email" is on line 3 of content (offset 2), so host_line should be 5+2=7
        assert fields_by_name["email"].host_line == 7, (
            f"Expected email field at line 7, got {fields_by_name['email'].host_line}"
        )

    def test_field_named_id_is_not_filtered_as_builtin(self):
        """A GraphQL field named 'id' should not be filtered out.

        The _GQL_BUILTINS set contains 'id' to prevent the scalar type 'ID'
        from being extracted as a user-defined type. But a *field* named 'id'
        is perfectly valid and must not be silently dropped.
        """
        region = DslRegion(
            dsl=DslKind.GRAPHQL,
            content='type User {\n  id: ID!\n  name: String!\n}',
            host_file="schema.graphql",
            host_start_line=1,
            host_start_col=0,
            host_end_line=4,
            host_end_col=0,
            trigger="file_extension",
        )
        symbols = extract_graphql_symbols(region)
        field_names = [s.name for s in symbols if s.kind == DslSymbolKind.GRAPHQL_FIELD]
        assert "id" in field_names, (
            f"Field 'id' was incorrectly filtered out; got fields: {field_names}"
        )


class TestResolveGraphqlLinks:
    """Tests for GraphQL resolver linking."""

    def test_resolve_type_to_resolver_class(self, tmp_path):
        """Resolves GraphQL type to resolver class."""
        resolver_file = tmp_path / "resolvers.py"
        resolver_file.write_text(
            'class UserResolver:\n'
            '    async def user(self, id: str):\n'
            '        return get_user(id)\n'
            '\n'
            '    async def posts(self, user):\n'
            '        return user.posts\n'
        )
        symbol = DslSymbol(
            name="User",
            kind=DslSymbolKind.GRAPHQL_TYPE,
            dsl=DslKind.GRAPHQL,
            host_file="schema.graphql",
            host_line=1,
            host_col=0,
            link_hints=[
                LinkHint(strategy="graphql_type", target_pattern="User", target_kind="class"),
                LinkHint(strategy="graphql_type", target_pattern="UserResolver", target_kind="class"),
            ],
        )
        links = resolve_graphql_links([symbol], str(tmp_path))
        assert len(links) >= 1
        assert "UserResolver" in links[0].target_qualified_name

    def test_resolve_field_to_method(self, tmp_path):
        """Resolves GraphQL field to method on resolver class."""
        resolver_file = tmp_path / "resolvers.py"
        resolver_file.write_text(
            'class UserResolver:\n'
            '    async def posts(self, user):\n'
            '        return user.posts\n'
        )
        symbol = DslSymbol(
            name="posts",
            kind=DslSymbolKind.GRAPHQL_FIELD,
            dsl=DslKind.GRAPHQL,
            host_file="schema.graphql",
            host_line=3,
            host_col=2,
            link_hints=[
                LinkHint(
                    strategy="graphql_field",
                    target_pattern="posts",
                    target_kind="function",
                    module_hint="User",
                ),
            ],
        )
        links = resolve_graphql_links([symbol], str(tmp_path))
        assert len(links) >= 1
        assert "posts" in links[0].target_qualified_name
        assert "UserResolver" in links[0].target_qualified_name

    def test_resolve_type_to_model_class(self, tmp_path):
        """Resolves GraphQL type to model class with matching name."""
        model_file = tmp_path / "models.py"
        model_file.write_text(
            'class User:\n'
            '    id: int\n'
            '    email: str\n'
        )
        symbol = DslSymbol(
            name="User",
            kind=DslSymbolKind.GRAPHQL_TYPE,
            dsl=DslKind.GRAPHQL,
            host_file="schema.graphql",
            host_line=1,
            host_col=0,
            link_hints=[
                LinkHint(strategy="graphql_type", target_pattern="User", target_kind="class"),
                LinkHint(strategy="graphql_type", target_pattern="UserResolver", target_kind="class"),
            ],
        )
        links = resolve_graphql_links([symbol], str(tmp_path))
        assert len(links) >= 1
        assert "User" in links[0].target_qualified_name

    def test_resolve_no_match(self, tmp_path):
        """Returns empty when no matching resolver found."""
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        symbol = DslSymbol(
            name="Widget",
            kind=DslSymbolKind.GRAPHQL_TYPE,
            dsl=DslKind.GRAPHQL,
            host_file="schema.graphql",
            host_line=1,
            host_col=0,
            link_hints=[
                LinkHint(strategy="graphql_type", target_pattern="Widget", target_kind="class"),
                LinkHint(strategy="graphql_type", target_pattern="WidgetResolver", target_kind="class"),
            ],
        )
        links = resolve_graphql_links([symbol], str(tmp_path))
        assert len(links) == 0


class TestGraphqlAnalyzeFile:
    """End-to-end GraphQL analysis tests."""

    def test_analyze_graphql_schema(self, tmp_path):
        """End-to-end: detect GraphQL, extract symbols."""
        f = tmp_path / "schema.graphql"
        f.write_text(
            'type User {\n'
            '  id: ID!\n'
            '  email: String!\n'
            '  posts: [Post!]!\n'
            '}\n'
            '\n'
            'type Post {\n'
            '  title: String!\n'
            '  author: User!\n'
            '}\n'
        )
        symbols, links = analyze_file(str(f))
        assert len(symbols) >= 2
        type_names = [s.name for s in symbols if s.kind == DslSymbolKind.GRAPHQL_TYPE]
        assert "User" in type_names
        assert "Post" in type_names

    def test_analyze_graphql_in_python(self, tmp_path):
        """Detects GraphQL schema embedded in Python string."""
        f = tmp_path / "schema.py"
        f.write_text(
            'SCHEMA = """\n'
            'type User {\n'
            '  id: ID!\n'
            '  name: String!\n'
            '}\n'
            '"""\n'
        )
        symbols, links = analyze_file(str(f))
        gql_syms = [s for s in symbols if s.dsl == DslKind.GRAPHQL]
        assert len(gql_syms) >= 1
        names = [s.name for s in gql_syms]
        assert "User" in names

    def test_analyze_graphql_with_resolution(self, tmp_path):
        """End-to-end: detect GraphQL + resolve to Python resolvers."""
        schema = tmp_path / "schema.graphql"
        schema.write_text('type User {\n  id: ID!\n  posts: [Post!]!\n}\n')
        resolver = tmp_path / "resolvers.py"
        resolver.write_text(
            'class UserResolver:\n'
            '    async def posts(self):\n'
            '        pass\n'
        )
        symbols, links = analyze_file(str(schema), project_root=str(tmp_path))
        assert len(links) >= 1
        assert any("UserResolver" in lnk.target_qualified_name for lnk in links)


class TestGraphqlDslDebugCommand:
    """Tests for GraphQL in dsl-debug command."""

    def test_dsl_debug_graphql(self, tmp_path):
        """dsl-debug --type graphql detects GraphQL schemas."""
        from typer.testing import CliRunner
        from emend.cli import app

        f = tmp_path / "schema.graphql"
        f.write_text('type User {\n  id: ID!\n  name: String!\n}\n')
        runner = CliRunner()
        result = runner.invoke(app, ["dsl-debug", str(f), "--type", "graphql"])
        assert result.exit_code == 0
        assert "User" in result.output


class TestGraphqlFindInDsl:
    """Tests for find_in_dsl with GraphQL regions."""

    def test_find_graphql_type_pattern(self, tmp_path):
        """Finds patterns inside GraphQL files."""
        f = tmp_path / "schema.graphql"
        f.write_text('type User {\n  id: ID!\n  name: String!\n}\n')
        matches = find_in_dsl("type $TYPE", str(f), dsl_type="graphql")
        assert len(matches) >= 1
        assert matches[0].captures.get("TYPE") is not None
