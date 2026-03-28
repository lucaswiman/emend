"""Tests for DSL support: detection, extraction, and link resolution."""

import json
import pytest

from emend.dsl import (
    DslKind,
    DslRegion,
    DslSymbol,
    DslSymbolKind,
    LinkHint,
    DslLink,
    detect_dsl_regions,
    extract_sql_symbols,
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
