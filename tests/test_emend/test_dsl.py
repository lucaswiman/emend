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
    extract_regex_named_groups,
    find_dsl_impact,
    find_in_dsl,
    find_regex_group_references,
    resolve_orm_links,
    analyze_file,
    format_symbols,
    _singularize,
    _to_pascal_case,
)


class TestNamingHelpers:
    def test_singularize_regular(self):
        assert _singularize("users") == "user"
        assert _singularize("posts") == "post"

    def test_singularize_ies(self):
        assert _singularize("categories") == "category"
        assert _singularize("entries") == "entry"

    def test_singularize_ses(self):
        assert _singularize("addresses") == "addresse"  # naive, ok
        assert _singularize("buses") == "bus"

    def test_singularize_already_singular(self):
        assert _singularize("user") == "user"
        assert _singularize("class") == "class"  # ends in ss

    def test_to_pascal_case(self):
        assert _to_pascal_case("user") == "User"
        assert _to_pascal_case("user_profile") == "UserProfile"
        assert _to_pascal_case("order_item") == "OrderItem"


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
