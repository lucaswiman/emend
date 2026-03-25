"""Tests for the semantic-context command."""
import json
from pathlib import Path

import pytest

from emend.component_selector import ExtendedSelector


class TestSemanticContext:
    """Tests for semantic_context() in transform.py."""

    def test_basic_function_context(self, tmp_path):
        """Basic function returns correct metadata."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def helper(x: int, y: str = 'default') -> bool:\n"
            "    return len(y) > x\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["helper"])
        result = semantic_context(sel, project_path=str(project))

        assert result.kind == "function"
        assert result.file == str(lib)
        assert result.line == 1
        assert result.is_async is False
        assert len(result.parameters) == 2
        assert "x: int" in result.parameters[0] or "x" in result.parameters[0]

    def test_async_function_detected(self, tmp_path):
        """Async functions are flagged correctly."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "async def fetch_data(url: str) -> dict:\n"
            "    return {}\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["fetch_data"])
        result = semantic_context(sel, project_path=str(project))

        assert result.is_async is True
        assert result.kind == "async_function"

    def test_callers_detected(self, tmp_path):
        """Callers of a function are found."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def helper(x):\n    return x + 1\n")

        app = project / "app.py"
        app.write_text(
            "from lib import helper\n"
            "\n"
            "def main():\n"
            "    return helper(42)\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["helper"])
        result = semantic_context(sel, project_path=str(project))

        assert len(result.callers) >= 1
        caller_files = [c.file for c in result.callers]
        assert any("app.py" in f for f in caller_files)

    def test_callees_detected(self, tmp_path):
        """Callees of a function are found."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def do_stuff():\n"
            "    return 42\n"
            "\n"
            "def main():\n"
            "    x = do_stuff()\n"
            "    print(x)\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["main"])
        result = semantic_context(sel, project_path=str(project))

        assert len(result.callees) >= 1
        callee_names = [c.rsplit('.', 1)[-1] if '.' in c else c for c in result.callees]
        assert "do_stuff" in callee_names or "print" in callee_names

    def test_danger_external_interface_decorator(self, tmp_path):
        """External interface decorators are flagged as dangers."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "@app.route('/users')\n"
            "def get_users():\n"
            "    return []\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["get_users"])
        result = semantic_context(sel, project_path=str(project))

        ext_dangers = [d for d in result.dangers if d.category == "external_interface"]
        assert len(ext_dangers) >= 1
        assert "external API" in ext_dangers[0].message or "route" in ext_dangers[0].message.lower()

    def test_danger_async_side_effect(self, tmp_path):
        """Async side effects (celery .delay()) are flagged."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def process_order(order):\n"
            "    save(order)\n"
            "    send_email.delay(order.email)\n"
            "    return True\n"
            "\n"
            "def save(x): pass\n"
            "\n"
            "class send_email:\n"
            "    @staticmethod\n"
            "    def delay(x): pass\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["process_order"])
        result = semantic_context(sel, project_path=str(project))

        async_dangers = [d for d in result.dangers if d.category == "async_side_effect"]
        assert len(async_dangers) >= 1
        assert "delay" in async_dangers[0].message

    def test_danger_high_fan_out(self, tmp_path):
        """High fan-out (many callers) is flagged."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def utility(x):\n    return x\n")

        # Create 12 files that each call utility (threshold is 5 for medium)
        for i in range(12):
            f = project / f"mod{i}.py"
            f.write_text(
                f"from lib import utility\n"
                f"\n"
                f"def func{i}():\n"
                f"    return utility({i})\n"
            )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["utility"])
        result = semantic_context(sel, project_path=str(project))

        fan_out_dangers = [d for d in result.dangers if d.category == "high_fan_out"]
        assert len(fan_out_dangers) >= 1

    def test_danger_no_test_coverage(self, tmp_path):
        """Functions with no test callers get a coverage warning."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def untested_func():\n    return 42\n")

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["untested_func"])
        result = semantic_context(sel, project_path=str(project))

        coverage_dangers = [d for d in result.dangers if d.category == "no_test_coverage"]
        assert len(coverage_dangers) >= 1

    def test_test_callers_classified(self, tmp_path):
        """Callers from test files are classified as tests."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def helper(x):\n    return x + 1\n")

        tests_dir = project / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_lib.py"
        test_file.write_text(
            "from lib import helper\n"
            "\n"
            "def test_helper():\n"
            "    assert helper(1) == 2\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["helper"])
        result = semantic_context(sel, project_path=str(project))

        test_callers = [c for c in result.callers if c.kind == "test"]
        assert len(test_callers) >= 1
        assert len(result.tests.direct) >= 1

        # Should not have no_test_coverage danger
        coverage_dangers = [d for d in result.dangers if d.category == "no_test_coverage"]
        assert len(coverage_dangers) == 0

    def test_side_effects_detected(self, tmp_path):
        """Side effects from callees are detected."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def save_data(data):\n"
            "    db.save(data)\n"
            "    cache.delete('key')\n"
            "\n"
            "class db:\n"
            "    @staticmethod\n"
            "    def save(x): pass\n"
            "\n"
            "class cache:\n"
            "    @staticmethod\n"
            "    def delete(x): pass\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["save_data"])
        result = semantic_context(sel, project_path=str(project))

        effect_kinds = {se.kind for se in result.side_effects}
        # save → db_write, delete → cache
        assert "db_write" in effect_kinds or "cache" in effect_kinds

    def test_data_in_from_parameters(self, tmp_path):
        """Data inputs are built from function parameters."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "def process(name: str, age: int, active: bool = True):\n"
            "    pass\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["process"])
        result = semantic_context(sel, project_path=str(project))

        param_names = [di.name for di in result.data_in]
        assert "name" in param_names
        assert "age" in param_names
        assert "active" in param_names

    def test_caching_decorator_danger(self, tmp_path):
        """Caching decorators are flagged."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "from functools import lru_cache\n"
            "\n"
            "@lru_cache(maxsize=128)\n"
            "def expensive(x):\n"
            "    return x ** 2\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["expensive"])
        result = semantic_context(sel, project_path=str(project))

        cache_dangers = [d for d in result.dangers if d.category == "caching"]
        assert len(cache_dangers) >= 1
        assert "cached" in cache_dangers[0].message.lower()

    def test_custom_interface_decorators(self, tmp_path):
        """Custom interface decorators are detected when passed."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "@my_custom_api\n"
            "def handler():\n"
            "    return 'ok'\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["handler"])

        # Without custom decorator — no external_interface danger
        result = semantic_context(sel, project_path=str(project))
        ext_dangers = [d for d in result.dangers if d.category == "external_interface"]
        assert len(ext_dangers) == 0

        # With custom decorator — should flag it
        result = semantic_context(
            sel, project_path=str(project),
            extra_interface_decorators=["my_custom_api"],
        )
        ext_dangers = [d for d in result.dangers if d.category == "external_interface"]
        assert len(ext_dangers) >= 1

    def test_to_dict_serialization(self, tmp_path):
        """SemanticContext.to_dict() produces valid JSON-serializable dict."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "@app.route('/test')\n"
            "def handler(request):\n"
            "    return respond(request)\n"
            "\n"
            "def respond(x): return x\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["handler"])
        result = semantic_context(sel, project_path=str(project))

        d = result.to_dict()
        # Should be JSON-serializable
        json_str = json.dumps(d, indent=2)
        parsed = json.loads(json_str)

        assert parsed["symbol"] == str(lib) + "::handler"
        assert "dangers" in parsed
        assert "flow" in parsed
        assert "callers" in parsed
        assert "signature" in parsed

    def test_references_counted(self, tmp_path):
        """References count includes non-definition, non-import refs."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def helper(x):\n    return x\n")

        app = project / "app.py"
        app.write_text(
            "from lib import helper\n"
            "\n"
            "def a():\n"
            "    return helper(1)\n"
            "\n"
            "def b():\n"
            "    return helper(2)\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["helper"])
        result = semantic_context(sel, project_path=str(project))

        # At least 2 call-site references (excluding definition and import)
        assert result.references_count >= 2

    def test_string_reference_danger(self, tmp_path):
        """String references to a symbol name are flagged."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def process_order(x):\n    return x\n")

        registry = project / "registry.py"
        registry.write_text(
            "tasks = {\n"
            "    'process_order': handle,\n"
            "}\n"
            "\n"
            "def handle(): pass\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["process_order"])
        result = semantic_context(sel, project_path=str(project))

        str_dangers = [d for d in result.dangers if d.category == "dynamic_reference"]
        assert len(str_dangers) >= 1
        assert "string literal" in str_dangers[0].message

    def test_method_context(self, tmp_path):
        """Semantic context works for class methods."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text(
            "class MyService:\n"
            "    def process(self, data: dict) -> bool:\n"
            "        return True\n"
        )

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["MyService", "process"])
        result = semantic_context(sel, project_path=str(project))

        assert result.kind in ("method", "function")
        # 'self' should be excluded from data_in
        param_names = [di.name for di in result.data_in]
        assert "self" not in param_names
        assert "data" in param_names

    def test_symbol_not_found_raises(self, tmp_path):
        """ValueError raised when symbol doesn't exist."""
        from emend.transform import semantic_context

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def existing():\n    pass\n")

        sel = ExtendedSelector(file_path=str(lib), symbol_path=["nonexistent"])
        with pytest.raises(ValueError, match="not found"):
            semantic_context(sel, project_path=str(project))


class TestSemanticContextCLI:
    """Tests for the CLI command."""

    def test_cli_json_output(self, tmp_path):
        """CLI --json flag produces valid JSON."""
        from typer.testing import CliRunner
        from emend.cli import app

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def my_func(x: int) -> str:\n    return str(x)\n")

        runner = CliRunner()
        result = runner.invoke(app, [
            "semantic-context", f"{lib}::my_func", "--json",
            "--project", str(project),
        ])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["kind"] == "function"
        assert "dangers" in data

    def test_cli_human_readable_output(self, tmp_path):
        """CLI default output is human-readable."""
        from typer.testing import CliRunner
        from emend.cli import app

        project = tmp_path / "project"
        project.mkdir()

        lib = project / "lib.py"
        lib.write_text("def my_func(x: int) -> str:\n    return str(x)\n")

        runner = CliRunner()
        result = runner.invoke(app, [
            "semantic-context", f"{lib}::my_func",
            "--project", str(project),
        ])

        assert result.exit_code == 0, result.output
        assert "Symbol:" in result.output
        assert "Kind:" in result.output
        assert "Callers:" in result.output
