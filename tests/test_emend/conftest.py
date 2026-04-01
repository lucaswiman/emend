"""Shared test fixtures and utilities for emend tests."""

import ast
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Use emend from the venv
EMEND = str(Path(sys.executable).parent / "emend")


@pytest.fixture
def emend_cmd():
    """Fixture for tests that use emend path (str)."""
    return str(Path(sys.executable).parent / "emend")


@pytest.fixture
def emend_cmd_list():
    """Fixture for tests that use emend command list."""
    return [sys.executable, "-m", "emend.cli"]


@pytest.fixture
def run_emend_cmd():
    """Fixture for tests that use run_emend() helper."""
    cmd_path = str(Path(sys.executable).parent / "emend")

    def _run(args, check=True):
        full_cmd = [cmd_path] + args
        result = subprocess.run(full_cmd, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            print(f"STDOUT:\n{result.stdout}")
            print(f"STDERR:\n{result.stderr}")
            raise subprocess.CalledProcessError(
                result.returncode, full_cmd, result.stdout, result.stderr
            )
        return result

    return _run


def assert_valid_python(content: str):
    """Assert that content is syntactically valid Python."""
    try:
        ast.parse(content)
    except SyntaxError as e:
        pytest.fail(f"Generated code is not valid Python:\n{content}\n\nError: {e!r}")


def get_import_statements(code: str) -> list[str]:
    """Extract import statements from Python code.

    Args:
        code: Python source code as a string.

    Returns:
        List of import statements found in the code (as strings).
    """
    imports = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(alias.name for alias in node.names)
            imports.append(f"from {module} import {names}")

    return imports


def build_indexed_project(
    tmp_path: Path,
    files: dict[str, str],
) -> Path:
    """Create a small Python project with indexed symbols.

    Args:
        tmp_path: pytest tmp_path for the project root.
        files: Mapping of filename (e.g. ``"app.py"``) to source content.

    Returns:
        Path to the project root directory.
    """
    from emend.transform import _index_batch

    proj = tmp_path / "proj"
    proj.mkdir()
    cache = proj / ".emend" / "cache"
    cache.mkdir(parents=True)
    db_path = cache / "parse.db"

    for name, content in files.items():
        (proj / name).write_text(content)

    batch = [(str(proj / name), content) for name, content in files.items()]
    _index_batch((str(db_path), str(proj), str(proj), batch))

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS index_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_manifest ("
        "  worktree_id TEXT NOT NULL DEFAULT '',"
        "  path TEXT NOT NULL,"
        "  mtime_ns INTEGER NOT NULL,"
        "  size INTEGER NOT NULL,"
        "  content_hash BLOB NOT NULL,"
        "  indexed_at REAL NOT NULL,"
        "  PRIMARY KEY (worktree_id, path)"
        ")"
    )
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        ("schema_version", "4"),
    )
    conn.commit()
    conn.close()

    return proj


def get_decorator_names(code: str, target_path: str = None) -> list[str]:
    """Extract decorator names from a function/class in order.

    Args:
        code: Python source code as a string.
        target_path: Optional dot-separated path to target function/class (e.g., "ClassName.method_name").
                    If None, returns decorators from the first function/class found.

    Returns:
        List of decorator names in order (without @ symbol).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    def find_target(nodes, path_parts):
        """Recursively find the target node by path."""
        if not path_parts:
            return None
        target_name = path_parts[0]
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == target_name:
                    if len(path_parts) == 1:
                        return node
                    return find_target(node.body, path_parts[1:])
        return None

    if target_path:
        path_parts = target_path.split(".")
        target = find_target(tree.body, path_parts)
    else:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                target = node
                break
        else:
            target = None

    if not target:
        return []

    decorator_names = []
    for decorator in target.decorator_list:
        if isinstance(decorator, ast.Name):
            decorator_names.append(decorator.id)
        elif isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Name):
                decorator_names.append(decorator.func.id)
            elif isinstance(decorator.func, ast.Attribute):
                decorator_names.append(ast.unparse(decorator.func))
        elif isinstance(decorator, ast.Attribute):
            decorator_names.append(ast.unparse(decorator))

    return decorator_names


# ---------------------------------------------------------------------------
# Shared trace test helpers (Phase 11+)
# ---------------------------------------------------------------------------


def make_sql_injection_config():
    """Return a standard SQL-injection TraceConfig for interprocedural tests."""
    from emend.trace import TraceConfig, TraceSanitizer, TraceSink, TraceSource

    return TraceConfig(
        labels=["user_input"],
        sources=[TraceSource(pattern="request.args.get($X)", label="user_input")],
        sinks=[
            TraceSink(
                pattern="cursor.execute($X)",
                label="user_input",
                message="SQL injection: user input reaches cursor.execute()",
            ),
        ],
        sanitizers=[TraceSanitizer(pattern="escape($X)", label="user_input")],
    )


SQL_INJECTION_CONFIG_YAML = textwrap.dedent("""\
    trace:
      labels:
        - user_input
      sources:
        - pattern: "request.args.get($X)"
          label: user_input
      sinks:
        - pattern: "cursor.execute($X)"
          label: user_input
          message: "SQL injection: user input reaches cursor.execute()"
      sanitizers:
        - pattern: "escape($X)"
          label: user_input
""")

CROSS_FUNCTION_SOURCE = textwrap.dedent("""\
    def run_query(cursor, query):
        cursor.execute(query)

    def handle_request(request, cursor):
        name = request.args.get('name')
        run_query(cursor, name)
""")


def setup_trace_fixture(tmp_path, source=None):
    """Write *source* and the standard SQL-injection YAML config to *tmp_path*.

    Returns ``(source_path, config_path)``.
    """
    src = tmp_path / "app.py"
    src.write_text(source or CROSS_FUNCTION_SOURCE)
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(SQL_INJECTION_CONFIG_YAML)
    return src, cfg


