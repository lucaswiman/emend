"""Tests for the dead-code detection command."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

def make_project_dir(tmp_path) -> Path:
    """Create an isolated project root with an explicit project marker."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'deadcode-fixture'\nversion = '0'\n"
    )
    return project


def make_project(tmp_path, files: dict[str, str]) -> Path:
    """Create a ``project/`` dir under *tmp_path* populated from *files*.

    Keys are relative paths (subdirectories are created as needed); values
    are file contents.
    """
    project = make_project_dir(tmp_path)
    for name, content in files.items():
        p = project / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return project


def dead_names(tmp_path, files: dict[str, str], **kwargs) -> set[str]:
    """Run find_dead_code over a throwaway project and return the dead names."""
    from emend.transform import find_dead_code

    project = make_project(tmp_path, files)
    return {d.name for d in find_dead_code(str(project), **kwargs)}


def dead_module_names(project: Path, **kwargs) -> set[str]:
    """Return only unused-module qualified names for an existing project."""
    from emend.transform import DeadModule, find_dead_code

    return {
        item.module_name
        for item in find_dead_code(str(project), **kwargs)
        if isinstance(item, DeadModule)
    }


def make_test_reference_project(
    tmp_path,
    *,
    module: str = "lib",
    function: str = "only_tested",
) -> tuple[Path, Path]:
    """Create a module referenced only by a conventional test file."""
    project = make_project(tmp_path, {
        f"{module}.py": f"def {function}():\n    return 42\n",
        f"tests/test_{module}.py": (
            f"from {module} import {function}\n\n"
            f"def test_it():\n    assert {function}() == 42\n"
        ),
    })
    return project, project / "tests"


def load_deadcode_config(tmp_path, deadcode, *, filename="rules.yaml"):
    """Write a ``{deadcode: ...}`` rules doc and return ``load_rules()`` output."""
    from emend.lint import load_rules

    config_file = tmp_path / filename
    config_file.write_text(yaml.dump({"deadcode": deadcode}))
    return load_rules(str(config_file))


class TestDeadCodeWarmPath:
    """Tests for the index-accelerated warm path of find_dead_code()."""

    def _build_index(self, project_path: str):
        """Helper: build the parse.db index for a project."""
        from emend.transform import warm_caches

        warm_caches(project_path, type_engine="none")

    def test_warm_path_finds_dead_function(self, tmp_path):
        """Warm path should detect an unreferenced function."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text(
            "def used():\n    return 1\n\n"
            "def unused():\n    return 2\n\n"
            "x = used()\n"
        )

        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "unused" in dead_names
        assert "used" not in dead_names

    def test_warm_path_skips_entry_points(self, tmp_path):
        """Warm path should skip test_, describe_, and dunder functions."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "tests.py").write_text(
            "def test_foo():\n    pass\n\n"
            "def describe_feature():\n    pass\n\n"
            "def __init__():\n    pass\n\n"
            "def real_dead():\n    pass\n"
        )

        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "test_foo" not in dead_names
        assert "describe_feature" not in dead_names
        assert "__init__" not in dead_names
        assert "real_dead" in dead_names

    def test_warm_path_respects_all_exports(self, tmp_path):
        """Warm path should exclude symbols listed in __all__."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text(
            "__all__ = ['exported']\n\n"
            "def exported():\n    return 1\n\n"
            "def not_exported():\n    return 2\n"
        )

        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "exported" not in dead_names
        assert "not_exported" in dead_names

    def test_warm_path_cross_file_reference(self, tmp_path):
        """Warm path should detect cross-file references."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "lib.py").write_text(
            "def helper():\n    return 1\n\n"
            "def orphan():\n    return 2\n"
        )
        (project / "main.py").write_text(
            "from lib import helper\n\n"
            "x = helper()\n"
        )

        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "helper" not in dead_names
        assert "orphan" in dead_names

    def test_warm_path_intra_file_class_instantiation(self, tmp_path):
        """Warm path: class instantiated within the same file should not be dead."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text(
            "class Inner:\n"
            "    def run(self): return 1\n\n"
            "class Outer:\n"
            "    def __init__(self):\n"
            "        self._inner = Inner()\n"
        )

        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "Inner" not in dead_names, "Inner is instantiated by Outer.__init__"

    def test_warm_path_intra_file_type_annotation(self, tmp_path):
        """Warm path: class used only in a type annotation in the same file is not dead."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text(
            "from __future__ import annotations\n\n"
            "class Client:\n"
            "    def connect(self): pass\n\n"
            "class Service:\n"
            "    def __init__(self):\n"
            "        self._client: Client | None = None\n"
        )

        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "Client" not in dead_names, "Client is referenced in Service type annotation"

    def test_fact_graph_bootstrap_persists_facts_db(self, tmp_path, monkeypatch):
        """Fact-dependent commands should materialize ``facts.db`` on first use."""
        from emend.transform import _cache_db_dir, _get_or_build_fact_graph

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text("def unused():\n    return 1\n")

        cache_dir = _cache_db_dir(str(project))
        facts_db = cache_dir / "facts.db"
        assert not facts_db.exists()

        monkeypatch.setattr("emend.transform.warm_caches", lambda *args, **kwargs: {})
        graph = _get_or_build_fact_graph(str(project))

        assert facts_db.exists()
        graph.close()

    def test_partial_scan_keeps_references_from_project_root(self, tmp_path):
        """Scanning src/ must still count references from root entry scripts."""
        from emend.transform import find_dead_code

        project = make_project(tmp_path, {
            "src/lib.py": "def used_from_root():\n    return 1\n",
            "manage.py": "from src.lib import used_from_root\n\nused_from_root()\n",
        })

        names = {
            item.name
            for item in find_dead_code(
                str(project / "src"),
                show_last_reference=False,
                unused_modules=False,
            )
        }
        assert "used_from_root" not in names

    def test_forced_rebuild_clears_removed_decorators(self, tmp_path):
        """A full fact replacement must not preserve deleted decorators."""
        from emend.fact_graph import FactGraph
        from emend.transform import _cache_db_dir, warm_caches

        project = make_project(tmp_path, {
            "api.py": "@custom.route\ndef handler():\n    return 1\n",
        })
        warm_caches(str(project), type_engine="none")
        (project / "api.py").write_text("def handler():\n    return 1\n")
        warm_caches(str(project), type_engine="none", force_facts=True)

        graph = FactGraph(db_path=str(_cache_db_dir(project) / "facts.db"))
        rows = graph._client.run("?[qn, dec] := *decorator_on[qn, dec]")["rows"]
        graph.close()
        assert rows == []

    def test_warm_path_intra_file_function_call(self, tmp_path):
        """Warm path: helper function called only within the same file is not dead."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text(
            "def normalize(s: str) -> str:\n"
            "    return s.strip().lower()\n\n"
            "class Processor:\n"
            "    def process(self, val: str) -> str:\n"
            "        return normalize(val)\n"
        )

        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "normalize" not in dead_names, "normalize is called by Processor.process"

    def test_cold_path_builds_only_deadcode_dependencies(self, tmp_path, monkeypatch):
        """Cold deadcode startup skips type, FTS, and duplicate indexing."""
        from emend.fact_graph import FactGraph, SymbolFact
        from emend.transform import _cache_db_dir, _get_or_build_fact_graph

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text("def unused():\n    return 1\n")
        calls = []

        def fake_warm_caches(path, **kwargs):
            calls.append((path, kwargs))
            cache_dir = _cache_db_dir(path)
            cache_dir.mkdir(parents=True, exist_ok=True)
            graph = FactGraph(db_path=str(cache_dir / "facts.db"))
            graph.add_symbol(SymbolFact(
                file_path="mod.py",
                name="unused",
                qualified_name="mod.unused",
                kind="function",
                line=1,
                end_line=2,
            ))
            graph.close()
            return {}

        monkeypatch.setattr("emend.transform.index.warm_caches", fake_warm_caches)
        graph = _get_or_build_fact_graph(str(project))

        assert calls == [(str(project), {
            "type_engine": "none",
            "build_fts": False,
            "build_duplicates": False,
            "force_facts": True,
        })]
        graph.close()

    def test_cold_path_loads_facts_from_shared_worktree_cache(
        self, tmp_path, monkeypatch,
    ):
        """A worktree must load the facts database that ``warm_caches`` built."""
        from emend.fact_graph import FactGraph, SymbolFact
        from emend.transform import _cache_db_dir, _get_or_build_fact_graph
        from emend.transform import cache as cache_module

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text("def unused():\n    return 1\n")
        shared_root = tmp_path / "main"
        shared_root.mkdir()
        monkeypatch.setattr(cache_module, "_resolve_cache_root", lambda _path: shared_root)

        def fake_warm_caches(path, **_kwargs):
            cache_dir = _cache_db_dir(path)
            cache_dir.mkdir(parents=True, exist_ok=True)
            graph = FactGraph(db_path=str(cache_dir / "facts.db"))
            graph.add_symbol(SymbolFact(
                file_path="mod.py",
                name="unused",
                qualified_name="mod.unused",
                kind="function",
                line=1,
                end_line=2,
            ))
            graph.close()

        monkeypatch.setattr("emend.transform.index.warm_caches", fake_warm_caches)
        monkeypatch.setattr(
            FactGraph,
            "build_from_project",
            Mock(side_effect=AssertionError("shared facts.db was not loaded")),
        )

        graph = _get_or_build_fact_graph(str(project))

        assert graph._client.run("?[count(qn)] := *symbol[qn, _, _, _, _, _, _]")["rows"] == [[1]]
        graph.close()

    def test_in_memory_fallback_can_skip_type_inference(self, tmp_path, monkeypatch):
        """The fallback fact builder must not invoke a type engine for deadcode."""
        from emend.fact_graph import FactGraph

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text("def unused():\n    return 1\n")

        def fail_if_called(*args, **kwargs):
            raise AssertionError("type inference should stay lazy")

        monkeypatch.setattr("emend.type_oracle.create_type_oracle", fail_if_called)
        graph = FactGraph.build_from_project(str(project), include_types=False)
        graph.close()

    def test_warm_path_reports_unused_module_by_default(self, tmp_path):
        """Unused modules are reported by default and can be disabled."""
        from emend.transform import DeadModule, find_dead_code

        project = make_project_dir(tmp_path)
        (project / "used.py").write_text("VALUE = 1\n")
        (project / "main.py").write_text("from used import VALUE\nprint(VALUE)\n")
        (project / "orphan.py").write_text("VALUE = 2\n")

        default_results = list(find_dead_code(str(project), show_last_reference=False))
        dead_modules = {d.module_name for d in default_results if isinstance(d, DeadModule)}
        assert "orphan" in dead_modules
        assert "used" not in dead_modules

        without_modules = list(find_dead_code(
            str(project), show_last_reference=False, unused_modules=False,
        ))
        assert not any(isinstance(d, DeadModule) for d in without_modules)

    def test_kind_filter_suppresses_other_result_kinds(self, tmp_path):
        from emend.transform import DeadBlock, DeadModule, find_dead_code

        project = make_project(tmp_path, {
            "orphan.py": "def unused():\n    return 1\n",
        })
        results = list(find_dead_code(
            str(project),
            kind="function",
            show_last_reference=False,
        ))

        assert not any(isinstance(item, (DeadBlock, DeadModule)) for item in results)

    def test_relative_import_keeps_module_alive(self, tmp_path):
        project = make_project(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/foo.py": "VALUE = 1\n",
            "pkg/bar.py": "from . import foo\n\nVALUE = foo.VALUE\n",
        })

        assert "pkg.foo" not in dead_module_names(
            project, show_last_reference=False,
        )


class TestFindDeadCode:
    """Tests for find_dead_code() in transform.py."""

    def test_finds_unreferenced_function(self, tmp_path):
        """A function with no references is flagged as dead."""
        names = dead_names(tmp_path, {"main.py":
            "def used_func():\n"
            "    return 1\n"
            "\n"
            "def unused_func():\n"
            "    return 2\n"
            "\n"
            "result = used_func()\n"
        })
        assert "unused_func" in names
        assert "used_func" not in names

    def test_finds_unreferenced_class(self, tmp_path):
        """A class with no references is flagged as dead."""
        names = dead_names(tmp_path, {"main.py":
            "class UsedClass:\n"
            "    pass\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
            "\n"
            "obj = UsedClass()\n"
        })
        assert "UnusedClass" in names
        assert "UsedClass" not in names

    def test_cross_file_reference_not_dead(self, tmp_path):
        """A function referenced from another file is not dead."""
        names = dead_names(tmp_path, {
            "lib.py":
                "def helper():\n"
                "    return 42\n"
                "\n"
                "def orphan():\n"
                "    return 0\n",
            "user.py":
                "from lib import helper\n"
                "\n"
                "result = helper()\n",
        })
        assert "orphan" in names
        assert "helper" not in names

    def test_skips_dunder_methods(self, tmp_path):
        """Dunder methods like __init__ are never flagged."""
        names = dead_names(tmp_path, {"main.py": "def __init__():\n    pass\n"})
        assert "__init__" not in names

    def test_skips_test_functions(self, tmp_path):
        """Functions starting with test_ are never flagged."""
        names = dead_names(tmp_path, {"test_example.py":
            "def test_something():\n"
            "    assert True\n"
            "\n"
            "class TestSuite:\n"
            "    pass\n"
        })
        assert "test_something" not in names
        assert "TestSuite" not in names

    def test_skips_describe_functions(self, tmp_path):
        """Functions starting with describe_ are never flagged (pytest-describe)."""
        names = dead_names(tmp_path, {"test_describe.py":
            "def describe_feature():\n"
            "    def it_works():\n"
            "        assert True\n"
            "\n"
            "def describe_another_feature():\n"
            "    pass\n"
        })
        assert "describe_feature" not in names
        assert "describe_another_feature" not in names

    def test_skips_main(self, tmp_path):
        """The 'main' function is never flagged."""
        names = dead_names(tmp_path, {"main.py": "def main():\n    pass\n"})
        assert "main" not in names

    def test_includes_private_by_default(self, tmp_path):
        """Private symbols (_name) are checked by default."""
        names = dead_names(tmp_path, {"main.py":
            "def _private_helper():\n"
            "    return 1\n"
            "\n"
            "def public_unused():\n"
            "    return 2\n"
        })
        assert "_private_helper" in names
        assert "public_unused" in names

    def test_exclude_private(self, tmp_path):
        """With include_private=False, _private symbols are skipped."""
        names = dead_names(
            tmp_path,
            {"main.py": "def _private_helper():\n    return 1\n"},
            include_private=False,
        )
        assert "_private_helper" not in names

    def test_skips_all_exports(self, tmp_path):
        """Symbols listed in __all__ are not flagged."""
        names = dead_names(tmp_path, {"main.py":
            "__all__ = ['exported_func']\n"
            "\n"
            "def exported_func():\n"
            "    return 1\n"
            "\n"
            "def not_exported():\n"
            "    return 2\n"
        })
        assert "exported_func" not in names
        assert "not_exported" in names

    def test_kind_filter_function(self, tmp_path):
        """With kind='function', only functions are checked."""
        names = dead_names(
            tmp_path,
            {"main.py":
                "def unused_func():\n"
                "    return 1\n"
                "\n"
                "class UnusedClass:\n"
                "    pass\n"
            },
            kind="function",
        )
        assert "unused_func" in names
        assert "UnusedClass" not in names

    def test_kind_filter_class(self, tmp_path):
        """With kind='class', only classes are checked."""
        names = dead_names(
            tmp_path,
            {"main.py":
                "def unused_func():\n"
                "    return 1\n"
                "\n"
                "class UnusedClass:\n"
                "    pass\n"
            },
            kind="class",
        )
        assert "UnusedClass" in names
        assert "unused_func" not in names

    def test_returns_correct_fields(self, tmp_path):
        """DeadSymbol has correct file_path, name, kind, line, selector."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        dead = list(find_dead_code(str(project), unused_modules=False))
        assert len(dead) == 1
        d = dead[0]
        assert d.name == "orphan"
        assert d.kind == "function"
        assert d.line == 1
        assert "main.py" in d.file_path
        assert "orphan" in d.selector
        assert d.reason == "no references found"

    def test_empty_project(self, tmp_path):
        """An empty project returns no dead code."""
        assert dead_names(tmp_path, {}) == set()

    def test_skips_decorated_entry_points(self, tmp_path):
        """Functions decorated with framework decorators are skipped."""
        names = dead_names(tmp_path, {"main.py":
            "def route(f):\n"
            "    return f\n"
            "\n"
            "@route\n"
            "def my_handler():\n"
            "    return 'hello'\n"
            "\n"
            "def truly_unused():\n"
            "    return 'bye'\n"
        })
        assert "my_handler" not in names
        assert "truly_unused" in names

    def test_skips_fastapi_router_decorators(self, tmp_path):
        """FastAPI-style @router.get/post/etc decorators are skipped."""
        names = dead_names(tmp_path, {"main.py":
            "class Router:\n"
            "    def get(self, path): return lambda f: f\n"
            "    def post(self, path): return lambda f: f\n"
            "\n"
            "router = Router()\n"
            "\n"
            "@router.get('/users')\n"
            "def list_users():\n"
            "    return []\n"
            "\n"
            "@router.post('/users')\n"
            "def create_user():\n"
            "    return {}\n"
            "\n"
            "def truly_unused():\n"
            "    return 'bye'\n"
        })
        assert "list_users" not in names
        assert "create_user" not in names
        assert "truly_unused" in names

    @pytest.mark.parametrize(
        ("import_line", "decorator"),
        [
            ("from fastapi import APIRouter as Factory", "api_route('/users')"),
            ("from typer import Typer as Factory", "callback()"),
            ("from fastapi import FastAPI as Factory", "exception_handler(Exception)"),
            ("from flask import Flask as Factory", "before_request()"),
        ],
        ids=["fastapi-route", "typer-callback", "fastapi-hook", "flask-hook"],
    )
    def test_framework_entry_point_by_receiver_type(
        self, tmp_path, import_line, decorator,
    ):
        names = dead_names(tmp_path, {"entry.py": (
            f"{import_line}\n\nreceiver = Factory()\n\n"
            f"@receiver.{decorator}\n"
            "def registered():\n    return 1\n\n"
            "def truly_unused():\n    return 2\n"
        )})
        assert "registered" not in names
        assert "truly_unused" in names

    def test_cached_type_identifies_decorator_receiver_without_inference(self, tmp_path):
        from emend.transform import find_dead_code, warm_caches
        from emend.transform.cache import _cache_db_dir
        from emend.type_oracle import (
            FileTypes,
            TypeBinding,
            TypeDescriptor,
            _TypeOracleDiskCache,
            _content_hash,
        )

        project = make_project_dir(tmp_path)
        module = project / "api.py"
        module.write_text(
            "from fastapi import APIRouter\n"
            "routes: APIRouter\n\n"
            "@routes.api_route('/health', methods=['GET'])\n"
            "def healthcheck():\n"
            "    return {'ok': True}\n"
        )
        warm_caches(str(project), type_engine="none")

        file_types = FileTypes(path=str(module), bindings=[TypeBinding(
            name="routes",
            line=2,
            col_start=0,
            col_end=6,
            type_descriptor=TypeDescriptor.named("APIRouter"),
            raw_type="APIRouter",
            binding_kind="definition",
        )])
        file_types.build_index()
        cache = _TypeOracleDiskCache(str(_cache_db_dir(project) / "parse.db"))
        cache.put(_content_hash(module), file_types)

        names = {
            result.name
            for result in find_dead_code(str(project), show_last_reference=False)
        }
        assert "healthcheck" not in names

    def test_unknown_receiver_type_does_not_hide_callback(self, tmp_path):
        """Typed decorator matching does not bless arbitrary same-named methods."""
        names = dead_names(tmp_path, {"main.py":
            "class Registry:\n"
            "    def api_route(self, path):\n"
            "        return lambda func: func\n"
            "\n"
            "registry = Registry()\n"
            "\n"
            "@registry.api_route('/users')\n"
            "def not_a_framework_entry_point():\n"
            "    return []\n"
        })
        assert "not_a_framework_entry_point" in names

    def test_pyproject_script_target_is_an_entry_point(self, tmp_path):
        names = dead_names(tmp_path, {
            "pyproject.toml":
                "[project]\n"
                "name = 'sample'\n"
                "version = '0'\n"
                "[project.scripts]\n"
                "sample = 'sample.cli:launch'\n",
            "src/sample/__init__.py": "",
            "src/sample/cli.py":
                "def launch():\n"
                "    return 0\n"
                "\n"
                "def old_command():\n"
                "    return 1\n",
        })
        assert "launch" not in names
        assert "old_command" in names
        assert "cli" not in names

    def test_main_guard_keeps_directly_executed_module_alive(self, tmp_path):
        names = dead_names(tmp_path, {"runner.py":
            "def launch():\n"
            "    return 0\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(launch())\n"
        })
        assert "runner" not in names

    def test_parenthesized_main_guard_keeps_module_alive(self, tmp_path):
        project = make_project(tmp_path, {
            "runner.py": (
                "def launch():\n    return 0\n\n"
                "if (__name__ == '__main__'):\n    raise SystemExit(launch())\n"
            ),
        })
        assert "runner" not in dead_module_names(
            project, show_last_reference=False,
        )

    def test_argparse_callback_in_dunder_main_is_followed(self, tmp_path):
        names = dead_names(tmp_path, {"pkg/__main__.py":
            "import argparse\n"
            "\n"
            "def serve(args):\n"
            "    return args\n"
            "\n"
            "def unused_command(args):\n"
            "    return args\n"
            "\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.set_defaults(func=serve)\n"
            "args = parser.parse_args()\n"
            "args.func(args)\n"
        })
        assert "serve" not in names
        assert "unused_command" in names

    def test_sorted_output(self, tmp_path):
        """Results are sorted by file path then line number."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        file_b = project / "b.py"
        file_b.write_text(
            "def zeta():\n"
            "    pass\n"
            "\n"
            "def alpha():\n"
            "    pass\n"
        )

        file_a = project / "a.py"
        file_a.write_text(
            "def gamma():\n"
            "    pass\n"
        )

        dead = list(find_dead_code(str(project), unused_modules=False))
        # Should be sorted: a.py first, then b.py, then by line within file
        assert len(dead) >= 3
        locations = [(d.file_path, d.line) for d in dead]
        assert locations == sorted(locations)

    def test_private_methods_are_checked_for_live_classes(self, tmp_path):
        """Unreferenced private methods on otherwise-live classes are dead."""
        names = dead_names(tmp_path, {"main.py":
            "class MyClass:\n"
            "    def _unused_method(self):\n"
            "        pass\n"
            "    def public_extension_hook(self):\n"
            "        pass\n"
            "\n"
            "obj = MyClass()\n"
        })
        assert "_unused_method" in names
        # Public methods remain conservative because protocols/subclasses may call them.
        assert "public_extension_hook" not in names
        assert "MyClass" not in names

    def test_referenced_private_method_is_not_dead(self, tmp_path):
        names = dead_names(tmp_path, {"main.py":
            "class MyClass:\n"
            "    def run(self):\n"
            "        return self._helper()\n"
            "\n"
            "    def _helper(self):\n"
            "        return 1\n"
            "\n"
            "obj = MyClass()\n"
            "obj.run()\n"
        })
        assert "_helper" not in names

    def test_private_method_callback_reference_is_not_dead(self, tmp_path):
        names = dead_names(tmp_path, {"main.py":
            "class Service:\n"
            "    def callbacks(self):\n"
            "        return [self._helper]\n"
            "\n"
            "    def _helper(self):\n"
            "        return 1\n"
            "\n"
            "Service().callbacks()\n"
        })
        assert "_helper" not in names

    def test_private_method_name_suffix_does_not_count_as_reference(self, tmp_path):
        names = dead_names(tmp_path, {"main.py":
            "class Service:\n"
            "    def run(self):\n"
            "        return self.other_helper()\n"
            "\n"
            "    def _helper(self):\n"
            "        return 1\n"
            "\n"
            "    def other_helper(self):\n"
            "        return 2\n"
            "\n"
            "Service().run()\n"
        })
        assert "_helper" in names

    def test_inherited_private_method_call_keeps_base_method_alive(self, tmp_path):
        names = dead_names(tmp_path, {"main.py":
            "class Base:\n"
            "    def _helper(self):\n"
            "        return 1\n"
            "\n"
            "class Child(Base):\n"
            "    def run(self):\n"
            "        return self._helper()\n"
            "\n"
            "Child().run()\n"
        })
        assert "_helper" not in names

    def test_metadata_method_target_keeps_class_and_module_alive(self, tmp_path):
        from emend.transform import DeadModule, find_dead_code

        project = make_project(tmp_path, {
            "pyproject.toml": (
                "[project]\nname = 'sample'\nversion = '0'\n"
                "[project.scripts]\nsample = 'sample.cli:App.run'\n"
            ),
            "src/sample/__init__.py": "",
            "src/sample/cli.py": (
                "class App:\n"
                "    def run(self):\n"
                "        return 0\n"
            ),
        })
        results = list(find_dead_code(
            str(project), show_last_reference=False,
        ))
        assert "App" not in {item.name for item in results}
        assert "sample.cli" not in {
            item.module_name for item in results if isinstance(item, DeadModule)
        }


class TestDeadCodeCLI:
    """Tests for the dead-code CLI command."""

    def test_cli_finds_dead_code(self, tmp_path, run_emend_cmd):
        """CLI command reports dead code."""
        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def used_func():\n"
            "    return 1\n"
            "\n"
            "def dead_func():\n"
            "    return 2\n"
            "\n"
            "result = used_func()\n"
        )

        result = run_emend_cmd(["deadcode", str(project)])
        assert "dead_func" in result.stdout
        assert "used_func" not in result.stdout

    def test_cli_json_output(self, tmp_path, run_emend_cmd):
        """CLI --json produces valid JSON output."""
        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        result = run_emend_cmd(["deadcode", str(project), "--json"])
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "orphan"
        assert data[0]["kind"] == "function"
        assert data[0]["line"] == 1

    def test_cli_kind_filter(self, tmp_path, run_emend_cmd):
        """CLI --kind filters to specific symbol types."""
        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def unused_func():\n"
            "    pass\n"
            "\n"
            "class UnusedClass:\n"
            "    pass\n"
        )

        result = run_emend_cmd(["deadcode", str(project), "--kind", "function"])
        assert "unused_func" in result.stdout
        assert "UnusedClass" not in result.stdout

    def test_cli_no_dead_code(self, tmp_path, run_emend_cmd):
        """CLI reports 'No dead code found.' when everything is used."""
        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "result = helper()\n"
        )

        result = run_emend_cmd(["deadcode", str(project)])
        assert "No dead code found" in result.stdout

    def test_cli_private_symbols_default_and_opt_out(self, tmp_path, run_emend_cmd):
        """CLI reports private symbols unless --exclude-private is passed."""
        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def _private_unused():\n"
            "    return 1\n"
        )

        # Included by default
        result = run_emend_cmd(["deadcode", str(project)])
        assert "_private_unused" in result.stdout

        # Explicit opt-out
        result = run_emend_cmd(["deadcode", str(project), "--exclude-private"])
        assert "_private_unused" not in result.stdout

    def test_cli_unused_modules_default_and_opt_out(self, tmp_path, run_emend_cmd):
        """CLI reports unimported modules unless --no-unused-modules is passed."""
        project = make_project_dir(tmp_path)

        (project / "used.py").write_text("VALUE = 1\n")
        (project / "main.py").write_text("from used import VALUE\nprint(VALUE)\n")
        (project / "orphan.py").write_text("VALUE = 2\n")

        result = run_emend_cmd(["deadcode", str(project)])
        assert "orphan (module)" in result.stdout
        assert "used (module)" not in result.stdout

        result = run_emend_cmd(["deadcode", str(project), "--no-unused-modules"])
        assert "orphan (module)" not in result.stdout

    def test_cli_unused_modules_json(self, tmp_path, run_emend_cmd):
        """CLI JSON includes module entries when --unused-modules is enabled."""
        project = make_project_dir(tmp_path)
        (project / "main.py").write_text("print('hi')\n")
        (project / "orphan.py").write_text("VALUE = 2\n")

        result = run_emend_cmd(["deadcode", str(project), "--unused-modules", "--json"])
        data = json.loads(result.stdout)
        modules = [entry for entry in data if entry["kind"] == "module"]
        assert any(entry["module_name"] == "orphan" for entry in modules)


class TestExcludeReferencesFrom:
    """Tests for --exclude-references-from."""

    def test_exclude_references_from_directory(self, tmp_path):
        """References in excluded directories are ignored."""
        from emend.transform import find_dead_code

        project, _tests_dir = make_test_reference_project(tmp_path)

        # Test references are excluded by default.
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "only_tested" in dead_names

        # Opting test references back in keeps the symbol alive.
        dead = list(find_dead_code(
            str(project),
            exclude_test_references=False,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "only_tested" not in dead_names

    @pytest.mark.parametrize("exclude_tests", [False, True], ids=["included", "excluded"])
    def test_cli_test_reference_controls(
        self, tmp_path, run_emend_cmd, exclude_tests,
    ):
        project, tests_dir = make_test_reference_project(tmp_path)
        args = [
            "deadcode", str(project), "--include-test-references",
            "--no-last-reference", "--no-unused-modules",
        ]
        if exclude_tests:
            args.extend(["--exclude-references-from", str(tests_dir)])

        result = run_emend_cmd(args)
        assert ("only_tested" in result.stdout) is exclude_tests

    @pytest.mark.parametrize(
        ("excluded_dir", "expect_dead"),
        [("legacy", False), ("tests", True)],
    )
    def test_excluded_references_affect_test_only_module(
        self, tmp_path, excluded_dir, expect_dead,
    ):
        project, tests_dir = make_test_reference_project(
            tmp_path, module="helper", function="do_it",
        )
        excluded = tests_dir
        if excluded_dir == "legacy":
            excluded = project / "legacy"
            excluded.mkdir()
            (excluded / "old.py").write_text("X = 1\n")

        modules = dead_module_names(
            project,
            show_last_reference=False,
            exclude_references_from=[str(excluded)],
            exclude_test_references=False,
        )
        assert ("helper" in modules) is expect_dead


class TestStringsCountAsReferences:
    """Tests for --strings-count-as-references / --no-strings."""

    def test_string_literal_counts_as_reference(self, tmp_path):
        """String containing symbol name prevents dead-code flagging."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def dynamic_handler():\n"
            "    return 'handled'\n"
            "\n"
            "registry = {'dynamic_handler': True}\n"
        )

        # With strings (default): not flagged
        dead = list(find_dead_code(
            str(project), strings_count_as_references=True,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "dynamic_handler" not in dead_names

        # Without strings: flagged
        dead = list(find_dead_code(
            str(project), strings_count_as_references=False,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "dynamic_handler" in dead_names

    def test_short_names_not_string_matched(self, tmp_path):
        """Names <= 3 chars are not matched in strings to avoid noise."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "x = 'foo bar'\n"
        )

        dead = list(find_dead_code(
            str(project), strings_count_as_references=True,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        # "foo" is only 3 chars — string matching should not protect it
        assert "foo" in dead_names

    def test_comment_mention_does_not_count_as_reference(self, tmp_path):
        """A name appearing only in a comment must NOT keep the symbol alive."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def process_data():\n"
            "    return 1\n"
        )
        other_file = project / "other.py"
        other_file.write_text(
            "# TODO: fix process_data later\n"
            "x = 1\n"
        )

        dead = list(find_dead_code(
            str(project), strings_count_as_references=True,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        # The only mention is in a comment, so it should still be dead.
        assert "process_data" in dead_names

    def test_own_docstring_mention_does_not_count_when_in_comment(self, tmp_path):
        """A name appearing only in a comment in the same file stays dead."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def lonely_function():\n"
            "    return 1\n"
            "\n"
            "# lonely_function is not called anywhere\n"
        )

        dead = list(find_dead_code(
            str(project), strings_count_as_references=True,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "lonely_function" in dead_names

    def test_getattr_string_literal_still_suppresses(self, tmp_path):
        """A name inside a genuine string literal still suppresses (cross-file)."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        mod_file = project / "mod.py"
        mod_file.write_text(
            "def func_name():\n"
            "    return 1\n"
        )
        caller_file = project / "caller.py"
        caller_file.write_text(
            "import mod\n"
            "fn = getattr(mod, \"func_name\")\n"
        )

        dead = list(find_dead_code(
            str(project), strings_count_as_references=True,
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "func_name" not in dead_names

    def test_cli_no_strings(self, tmp_path, run_emend_cmd):
        """CLI --no-strings disables string-based reference detection."""
        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def dynamic_handler():\n"
            "    return 'handled'\n"
            "\n"
            "registry = {'dynamic_handler': True}\n"
        )

        # Default: not flagged because string contains the name
        result = run_emend_cmd([
            "deadcode", str(project), "--no-last-reference",
        ])
        assert "dynamic_handler" not in result.stdout or "No dead code" in result.stdout

        # --no-strings: flagged
        result = run_emend_cmd([
            "deadcode", str(project), "--no-strings", "--no-last-reference",
        ])
        assert "dynamic_handler" in result.stdout


class TestNoqaDeadcode:
    """Tests for # noqa: emend:deadcode annotation."""

    def test_noqa_suppresses_deadcode(self, tmp_path):
        """# noqa: emend:deadcode on the definition line suppresses flagging."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def suppressed():  # noqa: emend:deadcode\n"
            "    return 1\n"
            "\n"
            "def not_suppressed():\n"
            "    return 2\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "suppressed" not in dead_names
        assert "not_suppressed" in dead_names

    def test_bare_noqa_also_suppresses(self, tmp_path):
        """A bare # noqa suppresses all rules including deadcode."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def suppressed():  # noqa\n"
            "    return 1\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "suppressed" not in dead_names


class TestShowLastReference:
    """Tests for --show-last-reference."""

    def test_last_reference_disabled(self, tmp_path):
        """With show_last_reference=False, no git info is attached."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        assert len(dead) == 1
        assert dead[0].last_reference_commit is None

    def test_last_reference_in_git_repo(self, tmp_path):
        """In a git repo, last_reference_commit is populated."""
        import subprocess
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        env = {
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "PATH": "/usr/bin:/bin",
        }
        subprocess.run(["git", "init"], cwd=str(project),
                        capture_output=True, check=True, env=env)

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan():\n"
            "    return 42\n"
        )
        subprocess.run(["git", "add", "."], cwd=str(project),
                        capture_output=True, check=True, env=env)
        subprocess.run(["git", "commit", "-m", "initial"],
                        cwd=str(project), capture_output=True, check=True,
                        env=env)

        dead = list(find_dead_code(str(project), show_last_reference=True))
        assert len(dead) == 1
        assert dead[0].last_reference_commit is not None
        assert "initial" in dead[0].last_reference_commit


class TestDeadCodeLint:
    """Tests for deadcode integration in the lint engine."""

    def test_lint_deadcode_config(self, tmp_path):
        """Lint config with deadcode section triggers dead code analysis."""
        from emend.lint import load_rules, run_lint

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan_func():\n"
            "    return 42\n"
        )

        config_file = project / ".emend" / "rules.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(yaml.dump({
            "deadcode": {
                "enabled": True,
            },
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        assert dc_config is not None
        assert dc_config.enabled is True

        violations = run_lint(
            rules, [str(main_file)],
            deadcode_config=dc_config,
            project_path=str(project),
        )

        dc_violations = [v for v in violations if v.rule_name == "deadcode"]
        assert len(dc_violations) == 1
        assert "orphan_func" in dc_violations[0].message

    def test_lint_deadcode_boolean_shorthand(self, tmp_path):
        """deadcode: true in config enables with defaults."""
        _, _, dc_config = load_deadcode_config(tmp_path, True)
        assert dc_config is not None
        assert dc_config.enabled is True
        assert dc_config.include_private is True
        assert dc_config.exclude_test_references is True
        assert dc_config.unused_modules is True

    def test_lint_deadcode_with_options(self, tmp_path):
        """deadcode config supports all options."""
        _, _, dc_config = load_deadcode_config(tmp_path, {
            "enabled": True,
            "kind": "function",
            "include-private": True,
            "exclude-references-from": ["tests/"],
            "exclude-test-references": False,
            "strings-count-as-references": False,
            "unused-modules": False,
            "message": "Custom dead code message",
        })
        assert dc_config.kind == "function"
        assert dc_config.include_private is True
        assert dc_config.exclude_references_from == ["tests/"]
        assert dc_config.exclude_test_references is False
        assert dc_config.strings_count_as_references is False
        assert dc_config.unused_modules is False
        assert dc_config.message == "Custom dead code message"

    def test_lint_deadcode_disabled(self, tmp_path):
        """deadcode: {enabled: false} does not run analysis."""
        from emend.lint import load_rules, run_lint

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def orphan_func():\n"
            "    return 42\n"
        )

        config_file = project / ".emend" / "rules.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(yaml.dump({
            "deadcode": {"enabled": False},
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        violations = run_lint(
            rules, [str(main_file)],
            deadcode_config=dc_config,
            project_path=str(project),
        )

        dc_violations = [v for v in violations if v.rule_name == "deadcode"]
        assert len(dc_violations) == 0

    def test_lint_deadcode_noqa_suppresses(self, tmp_path):
        """# noqa: emend:deadcode suppresses lint violations too."""
        from emend.lint import load_rules, run_lint

        project = make_project_dir(tmp_path)

        main_file = project / "main.py"
        main_file.write_text(
            "def suppressed():  # noqa: emend:deadcode\n"
            "    return 1\n"
            "\n"
            "def flagged():\n"
            "    return 2\n"
        )

        config_file = project / ".emend" / "rules.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(yaml.dump({
            "deadcode": True,
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        violations = run_lint(
            rules, [str(main_file)],
            deadcode_config=dc_config,
            project_path=str(project),
        )

        dc_violations = [v for v in violations if v.rule_name == "deadcode"]
        names = [v.message for v in dc_violations]
        assert any("flagged" in m for m in names)
        assert not any("suppressed" in m for m in names)


class TestEntryPointDecorators:
    """Tests for custom entry-point-decorators config."""

    @pytest.mark.parametrize(
        "source, decorators, alive",
        [
            pytest.param(
                "def my_handler(f):\n"
                "    return f\n"
                "\n"
                "@my_handler\n"
                "def process_event():\n"
                "    return 'done'\n"
                "\n"
                "def truly_unused():\n"
                "    return 'bye'\n",
                ["my_handler"],
                "process_event",
                id="bare-name",
            ),
            pytest.param(
                "class Router:\n"
                "    def sync_post(self, path): return lambda f: f\n"
                "\n"
                "router = Router()\n"
                "\n"
                "@router.sync_post('/endpoint')\n"
                "def my_endpoint():\n"
                "    return {}\n"
                "\n"
                "def truly_unused():\n"
                "    return 'bye'\n",
                ["sync_post"],
                "my_endpoint",
                id="basename-of-attribute",
            ),
            pytest.param(
                "class App:\n"
                "    def on_event(self, event): return lambda f: f\n"
                "\n"
                "app = App()\n"
                "\n"
                "@app.on_event('startup')\n"
                "def startup_handler():\n"
                "    pass\n"
                "\n"
                "def truly_unused():\n"
                "    return 'bye'\n",
                ["app.on_event"],
                "startup_handler",
                id="full-dotted-name",
            ),
        ],
    )
    def test_custom_decorator_excludes_symbol(self, tmp_path, source, decorators, alive):
        """A symbol with a configured entry-point decorator is not flagged."""
        names = dead_names(
            tmp_path,
            {"mod.py": source},
            entry_point_decorators=decorators,
            show_last_reference=False,
        )
        assert alive not in names
        assert "truly_unused" in names

    def test_without_custom_decorator_still_flagged(self, tmp_path):
        """Without custom entry-point-decorators, symbol is flagged."""
        names = dead_names(
            tmp_path,
            {"mod.py":
                "def my_handler(f):\n"
                "    return f\n"
                "\n"
                "@my_handler\n"
                "def process_event():\n"
                "    return 'done'\n"
            },
            show_last_reference=False,
        )
        assert "process_event" in names


class TestEntryPointNames:
    """Tests for custom entry-point-names config."""

    def test_custom_name_excludes_symbol(self, tmp_path):
        """Symbols with a custom entry-point name are not flagged."""
        names = dead_names(
            tmp_path,
            {"mod.py":
                "def plugin_init():\n"
                "    return 'initialized'\n"
                "\n"
                "def truly_unused():\n"
                "    return 'bye'\n"
            },
            entry_point_names=["plugin_init"],
            show_last_reference=False,
        )
        assert "plugin_init" not in names
        assert "truly_unused" in names

    def test_multiple_custom_names(self, tmp_path):
        """Multiple custom entry-point names all work."""
        names = dead_names(
            tmp_path,
            {"mod.py":
                "def plugin_init():\n"
                "    pass\n"
                "\n"
                "def on_startup():\n"
                "    pass\n"
                "\n"
                "def truly_unused():\n"
                "    pass\n"
            },
            entry_point_names=["plugin_init", "on_startup"],
            show_last_reference=False,
        )
        assert "plugin_init" not in names
        assert "on_startup" not in names
        assert "truly_unused" in names


class TestEntryPointConfig:
    """Tests for entry-point config loading from rules.yaml."""

    def test_load_entry_point_decorators(self, tmp_path):
        """entry-point-decorators config is loaded from YAML."""
        _, _, dc_config = load_deadcode_config(tmp_path, {
            "enabled": True,
            "entry-point-decorators": ["my_framework.handler", "sync_post"],
        })
        assert dc_config is not None
        assert dc_config.entry_point_decorators == [
            "my_framework.handler", "sync_post",
        ]

    def test_load_entry_point_names(self, tmp_path):
        """entry-point-names config is loaded from YAML."""
        _, _, dc_config = load_deadcode_config(tmp_path, {
            "enabled": True,
            "entry-point-names": ["plugin_init", "on_startup"],
        })
        assert dc_config is not None
        assert dc_config.entry_point_names == ["plugin_init", "on_startup"]

    def test_load_single_string_entry_point_decorators(self, tmp_path):
        """A single string for entry-point-decorators is wrapped in a list."""
        _, _, dc_config = load_deadcode_config(tmp_path, {
            "enabled": True,
            "entry-point-decorators": "my_decorator",
        })
        assert dc_config.entry_point_decorators == ["my_decorator"]

    def test_load_single_string_entry_point_names(self, tmp_path):
        """A single string for entry-point-names is wrapped in a list."""
        _, _, dc_config = load_deadcode_config(tmp_path, {
            "enabled": True,
            "entry-point-names": "plugin_init",
        })
        assert dc_config.entry_point_names == ["plugin_init"]

    def test_lint_integration_with_entry_point_decorators(self, tmp_path):
        """Lint engine passes entry-point-decorators to find_dead_code."""
        from emend.lint import load_rules, run_lint

        project = make_project_dir(tmp_path)

        (project / "mod.py").write_text(
            "def my_handler(f):\n"
            "    return f\n"
            "\n"
            "@my_handler\n"
            "def process_event():\n"
            "    return 'done'\n"
            "\n"
            "def truly_unused():\n"
            "    return 'bye'\n"
        )

        config_file = project / ".emend" / "rules.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(yaml.dump({
            "deadcode": {
                "enabled": True,
                "entry-point-decorators": ["my_handler"],
            },
        }))

        rules, macros, dc_config = load_rules(str(config_file))
        violations = run_lint(
            rules, [str(project / "mod.py")],
            deadcode_config=dc_config,
            project_path=str(project),
        )

        dc_violations = [v for v in violations if v.rule_name == "deadcode"]
        names = [v.message for v in dc_violations]
        assert not any("process_event" in m for m in names)
        assert any("truly_unused" in m for m in names)

    def test_cli_entry_point_decorator(self, tmp_path, run_emend_cmd):
        """CLI --entry-point-decorator excludes decorated symbols."""
        project = make_project_dir(tmp_path)

        (project / "mod.py").write_text(
            "def my_handler(f):\n"
            "    return f\n"
            "\n"
            "@my_handler\n"
            "def process_event():\n"
            "    return 'done'\n"
            "\n"
            "def truly_unused():\n"
            "    return 'bye'\n"
        )

        result = run_emend_cmd([
            "deadcode", str(project),
            "--entry-point-decorator", "my_handler",
            "--no-last-reference",
        ])
        assert "process_event" not in result.stdout
        assert "truly_unused" in result.stdout

    def test_cli_entry_point_name(self, tmp_path, run_emend_cmd):
        """CLI --entry-point-name excludes named symbols."""
        project = make_project_dir(tmp_path)

        (project / "mod.py").write_text(
            "def plugin_init():\n"
            "    pass\n"
            "\n"
            "def truly_unused():\n"
            "    pass\n"
        )

        result = run_emend_cmd([
            "deadcode", str(project),
            "--entry-point-name", "plugin_init",
            "--no-last-reference",
        ])
        assert "plugin_init" not in result.stdout
        assert "truly_unused" in result.stdout


class TestExcludePaths:
    """Tests for exclude-paths config (excluding directories from analysis)."""

    def test_exclude_paths_skips_directory(self, tmp_path):
        """Symbols in excluded directories are not reported."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        scripts = project / "scripts"
        scripts.mkdir()

        (project / "lib.py").write_text(
            "def truly_unused():\n"
            "    return 1\n"
        )
        (scripts / "run.py").write_text(
            "def script_func():\n"
            "    return 2\n"
        )

        # Without exclude: both flagged
        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "truly_unused" in dead_names
        assert "script_func" in dead_names

        # With exclude: only lib.py symbol flagged
        dead = list(find_dead_code(
            str(project),
            exclude_paths=[str(scripts)],
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "truly_unused" in dead_names
        assert "script_func" not in dead_names

    def test_exclude_paths_config_loading(self, tmp_path):
        """exclude-paths is loaded from the rules config."""
        _, _, dc_config = load_deadcode_config(tmp_path, {
            "enabled": True,
            "exclude-paths": ["scripts/", "frontends/devtools/"],
        })
        assert dc_config is not None
        assert dc_config.exclude_paths == ["scripts/", "frontends/devtools/"]

    def test_exclude_paths_single_string(self, tmp_path):
        """A single string for exclude-paths is wrapped in a list."""
        _, _, dc_config = load_deadcode_config(tmp_path, {
            "enabled": True,
            "exclude-paths": "scripts/",
        })
        assert dc_config.exclude_paths == ["scripts/"]

    def test_cli_exclude_path(self, tmp_path, run_emend_cmd):
        """CLI --exclude-path excludes directories from analysis."""
        project = make_project_dir(tmp_path)
        scripts = project / "scripts"
        scripts.mkdir()

        (project / "lib.py").write_text(
            "def truly_unused():\n"
            "    return 1\n"
        )
        (scripts / "run.py").write_text(
            "def script_func():\n"
            "    return 2\n"
        )

        result = run_emend_cmd([
            "deadcode", str(project),
            "--exclude-path", str(scripts),
            "--no-last-reference",
        ])
        assert "truly_unused" in result.stdout
        assert "script_func" not in result.stdout

    def test_exclude_paths_glob_pattern(self, tmp_path):
        """Glob patterns like **/scripts/ work in exclude-paths."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        nested = project / "pkg" / "scripts"
        nested.mkdir(parents=True)

        (project / "lib.py").write_text(
            "def truly_unused():\n"
            "    return 1\n"
        )
        (nested / "run.py").write_text(
            "def script_func():\n"
            "    return 2\n"
        )

        dead = list(find_dead_code(
            str(project),
            exclude_paths=["**/scripts/"],
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "truly_unused" in dead_names
        assert "script_func" not in dead_names

    def test_exclude_paths_star_glob(self, tmp_path):
        """Single * glob matches within one path segment."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        gen_a = project / "gen_alpha"
        gen_a.mkdir()
        lib = project / "lib"
        lib.mkdir()

        (gen_a / "mod.py").write_text(
            "def generated_func():\n"
            "    return 1\n"
        )
        (lib / "mod.py").write_text(
            "def real_unused():\n"
            "    return 2\n"
        )

        dead = list(find_dead_code(
            str(project),
            exclude_paths=[str(project / "gen_*")],
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "real_unused" in dead_names
        assert "generated_func" not in dead_names


class TestExcludeReferencesFromGlob:
    """Tests for glob patterns in exclude-references-from."""

    def test_exclude_refs_glob(self, tmp_path):
        """Glob patterns in exclude-references-from work."""
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        nested_tests = project / "pkg" / "tests"
        nested_tests.mkdir(parents=True)

        (project / "lib.py").write_text(
            "def only_tested():\n"
            "    return 42\n"
        )
        (nested_tests / "test_lib.py").write_text(
            "from lib import only_tested\n"
            "\n"
            "def test_it():\n"
            "    assert only_tested() == 42\n"
        )

        # Explicitly counting test references keeps the symbol alive.
        dead = list(find_dead_code(
            str(project), show_last_reference=False,
            exclude_test_references=False,
        ))
        dead_names = {d.name for d in dead}
        assert "only_tested" not in dead_names

        # With glob exclusion: dead
        dead = list(find_dead_code(
            str(project),
            exclude_references_from=["**/tests/"],
            show_last_reference=False,
        ))
        dead_names = {d.name for d in dead}
        assert "only_tested" in dead_names


class TestBuiltinSyncPostDecorator:
    """Tests for sync_post and similar built-in decorator basenames."""

    @pytest.mark.parametrize(
        "method, decorator, alive",
        [
            ("sync_post", "@router.sync_post('/endpoint')", "my_endpoint"),
            ("websocket", "@router.websocket('/ws')", "ws_handler"),
        ],
    )
    def test_builtin_decorator_is_entry_point(self, tmp_path, method, decorator, alive):
        """Built-in router decorator basenames are recognized as entry points."""
        names = dead_names(
            tmp_path,
            {"mod.py":
                "class Router:\n"
                f"    def {method}(self, path): return lambda f: f\n"
                "\n"
                "router = Router()\n"
                "\n"
                f"{decorator}\n"
                f"def {alive}():\n"
                "    pass\n"
                "\n"
                "def truly_unused():\n"
                "    return 'bye'\n"
            },
            show_last_reference=False,
        )
        assert alive not in names
        assert "truly_unused" in names


class TestDeadCodeUnreachableBlocks:
    """Tests for unreachable block detection in dead code analysis."""

    def _build_index(self, project_path: str):
        from emend.transform import warm_caches
        warm_caches(project_path, type_engine="none")

    def test_deadcode_reports_unreachable_after_return(self, tmp_path):
        """Code after a return statement should be reported as unreachable dead code."""
        import textwrap
        from emend.transform import find_dead_code, DeadBlock

        project = make_project_dir(tmp_path)
        (project / "example.py").write_text(textwrap.dedent("""\
            def foo():
                return 42
                x = 1
                print(x)
        """))
        self._build_index(str(project))
        results = list(find_dead_code(str(project), show_last_reference=False))
        unreachable = [r for r in results if isinstance(r, DeadBlock)]
        assert len(unreachable) >= 1
        assert any(b.func_qn.endswith("foo") for b in unreachable)

    def test_deadcode_reports_unreachable_after_raise(self, tmp_path):
        """Code after a raise statement should be reported as unreachable."""
        import textwrap
        from emend.transform import find_dead_code, DeadBlock

        project = make_project_dir(tmp_path)
        (project / "example.py").write_text(textwrap.dedent("""\
            def bar():
                raise ValueError("oops")
                cleanup()
        """))
        self._build_index(str(project))
        results = list(find_dead_code(str(project), show_last_reference=False))
        unreachable = [r for r in results if isinstance(r, DeadBlock)]
        assert len(unreachable) >= 1

    def test_deadcode_no_false_unreachable_for_normal_code(self, tmp_path):
        """Normal code should not be reported as unreachable."""
        import textwrap
        from emend.transform import find_dead_code, DeadBlock

        project = make_project_dir(tmp_path)
        (project / "example.py").write_text(textwrap.dedent("""\
            def baz():
                x = 1
                y = x + 2
                return y
        """))
        self._build_index(str(project))
        results = list(find_dead_code(str(project), show_last_reference=False))
        unreachable = [r for r in results if isinstance(r, DeadBlock)]
        assert len(unreachable) == 0

    def test_deadcode_unreachable_in_json_output(self, tmp_path):
        """JSON output should include unreachable blocks."""
        import textwrap
        import json
        from typer.testing import CliRunner
        from emend.cli import app

        project = make_project_dir(tmp_path)
        (project / "example.py").write_text(textwrap.dedent("""\
            def foo():
                return 42
                x = 1
        """))
        self._build_index(str(project))
        runner = CliRunner()
        result = runner.invoke(app, ["deadcode", str(project), "--json", "--no-last-reference"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        unreachable = [d for d in data if d.get("kind") == "unreachable_block"]
        assert len(unreachable) >= 1

    @pytest.mark.xfail(reason=(
        "References in unreachable code may not be attributed to the unreachable "
        "block if the Rust CFG builder's block line ranges don't cover all post-return "
        "statements. This is a known limitation of the current CFG range computation."
    ))
    def test_deadcode_ref_from_unreachable_not_counted(self, tmp_path):
        """A reference from unreachable code should not keep a symbol alive.

        Known limitation: the Rust CFG builder may assign end_line to the
        unreachable block such that lines after a return statement aren't
        covered by any block's line range. In that case the reference falls
        through to the module-level live_ref rule and keeps the symbol alive.
        """
        import textwrap
        from emend.transform import find_dead_code, DeadSymbol

        project = make_project_dir(tmp_path)
        (project / "example.py").write_text(textwrap.dedent("""\
            def helper():
                pass

            def caller():
                return 1
                helper()
        """))
        self._build_index(str(project))
        results = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = [r.name for r in results if isinstance(r, DeadSymbol)]
        assert "helper" in dead_names

    def test_deadcode_no_false_unreachable_in_except_handler(self, tmp_path):
        """Except handler bodies (including elif chains) should not be flagged as unreachable.

        Regression: the Rust CFG builder created a separate block for the
        exception edge target and another for the handler body, but never
        connected them — causing the entire except body to appear unreachable.
        """
        import textwrap
        from emend.transform import find_dead_code, DeadBlock

        project = make_project_dir(tmp_path)
        (project / "example.py").write_text(textwrap.dedent("""\
            def execute_with_retry():
                for attempt in range(3):
                    try:
                        return do_work()
                    except Exception as e:
                        error_str = str(e).lower()
                        if "409" in error_str:
                            continue
                        elif "429" in error_str:
                            continue
                        elif "timeout" in error_str:
                            continue
                        else:
                            raise
        """))
        self._build_index(str(project))
        results = list(find_dead_code(str(project), show_last_reference=False))
        unreachable = [r for r in results if isinstance(r, DeadBlock)]
        # The except handler body is fully reachable — no blocks should
        # be reported as unreachable dead code.
        assert len(unreachable) == 0, (
            f"False-positive unreachable blocks: "
            f"{[(b.func_qn, b.start_line, b.end_line) for b in unreachable]}"
        )


class TestExtractAllExportsText:
    """Unit tests for _extract_all_exports_text (Phase 2: tree-sitter migration)."""

    def test_basic_list(self):
        """Basic __all__ = ['foo', 'bar'] is extracted correctly."""
        from emend.transform import _extract_all_exports_text

        source = "__all__ = ['foo', 'bar']\n"
        result = _extract_all_exports_text(source)
        assert result == {"foo", "bar"}

    def test_basic_double_quotes(self):
        """Double-quoted names in __all__ are extracted."""
        from emend.transform import _extract_all_exports_text

        source = '__all__ = ["alpha", "beta"]\n'
        result = _extract_all_exports_text(source)
        assert result == {"alpha", "beta"}

    def test_multiline_tuple(self):
        """Multi-line tuple assignment is handled correctly."""
        from emend.transform import _extract_all_exports_text

        source = '__all__ = (\n    "foo",\n    "bar",\n)\n'
        result = _extract_all_exports_text(source)
        assert result == {"foo", "bar"}

    def test_multiline_list(self):
        """Multi-line list assignment is handled correctly."""
        from emend.transform import _extract_all_exports_text

        source = "__all__ = [\n    'alpha',\n    'beta',\n    'gamma',\n]\n"
        result = _extract_all_exports_text(source)
        assert result == {"alpha", "beta", "gamma"}

    def test_no_all(self):
        """Files without __all__ return an empty set."""
        from emend.transform import _extract_all_exports_text

        source = "def foo():\n    pass\n"
        result = _extract_all_exports_text(source)
        assert result == set()

    def test_all_in_docstring_no_false_positive(self):
        """__all__ appearing inside a string literal must not be detected."""
        from emend.transform import _extract_all_exports_text

        source = (
            'def helper():\n'
            '    """Example: __all__ = ["not_exported"]"""\n'
            '    pass\n'
        )
        result = _extract_all_exports_text(source)
        assert result == set(), (
            "False positive: __all__ inside a docstring should not be detected"
        )

    def test_all_exports_used_by_dead_code_detection(self, tmp_path):
        """Integration: symbols in __all__ (multi-line tuple) must not be flagged as dead.

        Uses names longer than 3 characters to exercise the string-literal
        filter path (which skips names of length <= 3).
        """
        from emend.transform import find_dead_code

        project = make_project_dir(tmp_path)
        (project / "mod.py").write_text(
            '__all__ = (\n'
            '    "public_alpha",\n'
            '    "public_beta",\n'
            ')\n\n'
            'def public_alpha():\n    return 1\n\n'
            'def public_beta():\n    return 2\n\n'
            'def hidden_func():\n    return 3\n'
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead}
        assert "public_alpha" not in dead_names, "__all__ member 'public_alpha' should not be dead"
        assert "public_beta" not in dead_names, "__all__ member 'public_beta' should not be dead"
        assert "hidden_func" in dead_names, "non-exported function should be dead"
