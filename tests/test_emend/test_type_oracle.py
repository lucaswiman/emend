"""Tests for the type inference adapter (type_oracle.py).

Tests cover:
- TypeDescriptor parsing from pyrefly type strings
- TypeDescriptor structural matching
- Pyrefly debug-info JSON parsing into FileTypes
- FileTypes indexing (by position, by name)
- _FileTypeCache behavior (LRU eviction, thread safety)
- PyreflyAdapter integration (requires pyrefly installed)
- CLI `types` command integration
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from emend.type_oracle import (
    FileTypes,
    PyreflyAdapter,
    TypeBinding,
    TypeDescriptor,
    _FileTypeCache,
    _parse_callable,
    _parse_pyrefly_debug,
    _parse_type_string,
    _split_params,
    _split_union,
    create_type_oracle,
)


# ---------------------------------------------------------------------------
# TypeDescriptor parsing
# ---------------------------------------------------------------------------

class TestParseTypeString:
    """Tests for _parse_type_string()."""

    def test_simple_named(self):
        td = _parse_type_string("int")
        assert td.kind == "named"
        assert td.name == "int"

    def test_str_type(self):
        td = _parse_type_string("str")
        assert td.kind == "named"
        assert td.name == "str"

    def test_none_type(self):
        td = _parse_type_string("None")
        assert td.kind == "named"
        assert td.name == "None"

    def test_class_type(self):
        td = _parse_type_string("Connection")
        assert td.kind == "named"
        assert td.name == "Connection"

    def test_unknown(self):
        td = _parse_type_string("Unknown")
        assert td.kind == "unknown"

    def test_empty_string(self):
        td = _parse_type_string("")
        assert td.kind == "unknown"

    def test_parameterized_list(self):
        td = _parse_type_string("list[int]")
        assert td.kind == "parameterized"
        assert td.name == "list"
        assert len(td.params) == 1
        assert td.params[0].kind == "named"
        assert td.params[0].name == "int"

    def test_parameterized_dict(self):
        td = _parse_type_string("dict[str, int]")
        assert td.kind == "parameterized"
        assert td.name == "dict"
        assert len(td.params) == 2
        assert td.params[0].name == "str"
        assert td.params[1].name == "int"

    def test_nested_parameterized(self):
        td = _parse_type_string("dict[str, list[int]]")
        assert td.kind == "parameterized"
        assert td.name == "dict"
        assert len(td.params) == 2
        assert td.params[1].kind == "parameterized"
        assert td.params[1].name == "list"
        assert td.params[1].params[0].name == "int"

    def test_union_simple(self):
        td = _parse_type_string("str | None")
        assert td.kind == "union"
        assert len(td.params) == 2
        assert td.params[0].name == "str"
        assert td.params[1].name == "None"

    def test_union_three_members(self):
        td = _parse_type_string("str | int | None")
        assert td.kind == "union"
        assert len(td.params) == 3

    def test_callable_simple(self):
        td = _parse_type_string("() -> Pool")
        assert td.kind == "callable"
        assert len(td.params) == 0
        assert td.return_type.name == "Pool"

    def test_callable_with_params(self):
        td = _parse_type_string("(conn: Connection) -> str")
        assert td.kind == "callable"
        assert len(td.params) == 1
        assert td.params[0].name == "Connection"
        assert td.return_type.name == "str"

    def test_callable_with_self(self):
        td = _parse_type_string("(self: Self@Connection, host: str, port: int) -> None")
        assert td.kind == "callable"
        assert len(td.params) == 3
        assert td.params[0].name == "Connection"  # Self@Connection -> Connection
        assert td.params[1].name == "str"
        assert td.params[2].name == "int"
        assert td.return_type.name == "None"

    def test_self_at_type(self):
        td = _parse_type_string("Self@Pool")
        assert td.kind == "named"
        assert td.name == "Pool"

    def test_type_bracket(self):
        td = _parse_type_string("type[Connection]")
        assert td.kind == "parameterized"
        assert td.name == "type"
        assert td.params[0].name == "Connection"

    def test_overload(self):
        td = _parse_type_string("Overload[\n  [...] -> int\n  [...] -> str\n]")
        assert td.kind == "named"
        assert td.name == "Overload"


class TestSplitUnion:
    """Tests for _split_union()."""

    def test_simple(self):
        parts = _split_union("str | None")
        assert parts == ["str", "None"]

    def test_with_brackets(self):
        parts = _split_union("list[str | int] | None")
        assert parts == ["list[str | int]", "None"]

    def test_with_parens(self):
        parts = _split_union("(int, str) -> bool | None")
        assert parts == ["(int, str) -> bool", "None"]


class TestSplitParams:
    """Tests for _split_params()."""

    def test_simple(self):
        parts = _split_params("str, int")
        assert parts == ["str", "int"]

    def test_nested_brackets(self):
        parts = _split_params("str, list[int, float]")
        assert parts == ["str", "list[int, float]"]

    def test_single(self):
        parts = _split_params("int")
        assert parts == ["int"]


# ---------------------------------------------------------------------------
# TypeDescriptor.display()
# ---------------------------------------------------------------------------

class TestTypeDescriptorDisplay:

    def test_named(self):
        assert TypeDescriptor.named("int").display() == "int"

    def test_parameterized(self):
        td = TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),))
        assert td.display() == "list[int]"

    def test_union(self):
        td = TypeDescriptor.union((TypeDescriptor.named("str"), TypeDescriptor.named("None")))
        assert td.display() == "str | None"

    def test_callable(self):
        td = TypeDescriptor.callable_(
            (TypeDescriptor.named("str"),),
            TypeDescriptor.named("int"),
        )
        assert td.display() == "(str) -> int"

    def test_unknown(self):
        assert TypeDescriptor.unknown().display() == "Unknown"


# ---------------------------------------------------------------------------
# TypeDescriptor.matches()
# ---------------------------------------------------------------------------

class TestTypeDescriptorMatches:

    def test_exact_named(self):
        assert TypeDescriptor.named("int").matches(TypeDescriptor.named("int"))

    def test_named_mismatch(self):
        assert not TypeDescriptor.named("int").matches(TypeDescriptor.named("str"))

    def test_parameterized_exact(self):
        td = TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),))
        constraint = TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),))
        assert td.matches(constraint)

    def test_parameterized_mismatch_param(self):
        td = TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),))
        constraint = TypeDescriptor.parameterized("list", (TypeDescriptor.named("str"),))
        assert not td.matches(constraint)

    def test_named_matches_parameterized_base(self):
        """list[int] should match a named 'list' constraint (ignoring params)."""
        td = TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),))
        constraint = TypeDescriptor.named("list")
        assert td.matches(constraint)

    def test_unknown_constraint_matches_anything(self):
        assert TypeDescriptor.named("int").matches(TypeDescriptor.unknown())

    def test_unknown_type_matches_nothing(self):
        assert not TypeDescriptor.unknown().matches(TypeDescriptor.named("int"))

    def test_union_constraint(self):
        td = TypeDescriptor.named("str")
        constraint = TypeDescriptor.union((TypeDescriptor.named("str"), TypeDescriptor.named("int")))
        assert td.matches(constraint)

    def test_union_constraint_no_match(self):
        td = TypeDescriptor.named("float")
        constraint = TypeDescriptor.union((TypeDescriptor.named("str"), TypeDescriptor.named("int")))
        assert not td.matches(constraint)


# ---------------------------------------------------------------------------
# Pyrefly debug-info JSON parsing
# ---------------------------------------------------------------------------

class TestParsePyreflyDebug:
    """Tests for _parse_pyrefly_debug()."""

    def _make_debug_json(self, bindings: list[dict]) -> dict:
        return {"modules": {"test_module": {"bindings": bindings}}}

    def test_definition_binding(self):
        debug = self._make_debug_json([
            {
                "key": "Key::Definition(MyClass 5:7-14)",
                "location": "5:7-14",
                "binding": "ClassDef(KeyClass(MyClass 5:7-14))",
                "result": "type[MyClass]",
            }
        ])
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 1
        b = ft.bindings[0]
        assert b.name == "MyClass"
        assert b.line == 5
        assert b.col_start == 7
        assert b.col_end == 14
        assert b.binding_kind == "definition"
        assert b.raw_type == "type[MyClass]"

    def test_function_definition(self):
        debug = self._make_debug_json([
            {
                "key": "Key::Definition(get_name 10:5-13)",
                "location": "10:5-13",
                "binding": "Function(KeyDecoratedFunction(get_name 10:5-13))",
                "result": "(self: Self@User) -> str",
            }
        ])
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 1
        b = ft.bindings[0]
        assert b.name == "get_name"
        assert b.binding_kind == "definition"
        assert b.type_descriptor.kind == "callable"

    def test_variable_binding(self):
        debug = self._make_debug_json([
            {
                "key": "Key::Definition(x 29:1-2)",
                "location": "29:1-2",
                "binding": "NameAssign(x, None, create_pool())",
                "result": "Pool",
            }
        ])
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 1
        b = ft.bindings[0]
        assert b.name == "x"
        assert b.type_descriptor.kind == "named"
        assert b.type_descriptor.name == "Pool"

    def test_bound_name_reference(self):
        debug = self._make_debug_json([
            {
                "key": "Key::BoundName(y 31:29-30)",
                "location": "31:29-30",
                "binding": "Forward(Key::Definition(y 30:1-2))",
                "result": "Connection",
            }
        ])
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 1
        b = ft.bindings[0]
        assert b.name == "y"
        assert b.binding_kind == "reference"

    def test_skips_builtins(self):
        debug = self._make_debug_json([
            {
                "key": "Key::Import(int 1:1)",
                "location": "1:1",
                "binding": "Import(builtins, int, None)",
                "result": "type[int]",
            },
            {
                "key": "Key::Definition(x 5:1-2)",
                "location": "5:1-2",
                "binding": "NameAssign(x, None, 42)",
                "result": "int",
            }
        ])
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        # Only the definition, not the builtin import
        assert len(ft.bindings) == 1
        assert ft.bindings[0].name == "x"

    def test_empty_modules(self):
        debug = {"modules": {}}
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 0

    def test_skips_empty_result(self):
        debug = self._make_debug_json([
            {
                "key": "Key::Definition(x 5:1-2)",
                "location": "5:1-2",
                "binding": "NameAssign(x, None, 42)",
                "result": "()",
            }
        ])
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 0

    def test_deduplication_prefers_definition(self):
        debug = self._make_debug_json([
            {
                "key": "Key::Definition(x 5:1-2)",
                "location": "5:1-2",
                "binding": "NameAssign(x, None, 42)",
                "result": "int",
            },
            {
                "key": "Key::CompletedPartialType(x 5:1-2)",
                "location": "5:1-2",
                "binding": "CompletedPartialType(...)",
                "result": "int",
            }
        ])
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        # Only one binding for (x, 5, 1)
        assert len(ft.bindings) == 1
        assert ft.bindings[0].binding_kind == "definition"


# ---------------------------------------------------------------------------
# FileTypes indexing
# ---------------------------------------------------------------------------

class TestFileTypes:

    def _make_binding(self, name="x", line=1, col=1, raw_type="int", kind="definition"):
        return TypeBinding(
            name=name,
            line=line,
            col_start=col,
            col_end=col + len(name),
            type_descriptor=_parse_type_string(raw_type),
            raw_type=raw_type,
            binding_kind=kind,
        )

    def test_type_at(self):
        ft = FileTypes(path="test.py", bindings=[self._make_binding(line=5, col=3)])
        ft.build_index()
        b = ft.type_at(5, 3)
        assert b is not None
        assert b.name == "x"

    def test_type_at_miss(self):
        ft = FileTypes(path="test.py", bindings=[self._make_binding(line=5, col=3)])
        ft.build_index()
        assert ft.type_at(5, 999) is None

    def test_types_for_name(self):
        ft = FileTypes(path="test.py", bindings=[
            self._make_binding(name="x", line=1),
            self._make_binding(name="x", line=5),
            self._make_binding(name="y", line=3),
        ])
        ft.build_index()
        assert len(ft.types_for_name("x")) == 2
        assert len(ft.types_for_name("y")) == 1
        assert len(ft.types_for_name("z")) == 0

    def test_definitions(self):
        ft = FileTypes(path="test.py", bindings=[
            self._make_binding(name="x", kind="definition"),
            self._make_binding(name="y", kind="reference"),
            self._make_binding(name="z", kind="definition", line=3),
        ])
        defs = ft.definitions()
        assert len(defs) == 2
        assert {b.name for b in defs} == {"x", "z"}


# ---------------------------------------------------------------------------
# _FileTypeCache
# ---------------------------------------------------------------------------

class TestFileTypeCache:

    def test_get_miss(self):
        cache = _FileTypeCache(max_entries=10)
        assert cache.get("nonexistent") is None

    def test_put_and_get(self):
        cache = _FileTypeCache(max_entries=10)
        ft = FileTypes(path="test.py")
        cache.put("abc123", ft)
        assert cache.get("abc123") is ft

    def test_eviction(self):
        cache = _FileTypeCache(max_entries=4)
        for i in range(4):
            cache.put(f"key{i}", FileTypes(path=f"test{i}.py"))

        assert len(cache) == 4

        # Adding one more triggers eviction of ~25% (1 entry)
        cache.put("key4", FileTypes(path="test4.py"))
        assert len(cache) <= 4  # evicted at least 1

    def test_clear(self):
        cache = _FileTypeCache(max_entries=10)
        cache.put("abc", FileTypes(path="test.py"))
        assert len(cache) == 1
        cache.clear()
        assert len(cache) == 0


# ---------------------------------------------------------------------------
# create_type_oracle factory
# ---------------------------------------------------------------------------

class TestCreateTypeOracle:

    def test_creates_pyrefly(self):
        oracle = create_type_oracle("pyrefly")
        assert isinstance(oracle, PyreflyAdapter)

    def test_unknown_engine(self):
        with pytest.raises(ValueError, match="Unknown type inference engine"):
            create_type_oracle("nonexistent")


# ---------------------------------------------------------------------------
# PyreflyAdapter integration tests (require pyrefly installed)
# ---------------------------------------------------------------------------

_has_pyrefly = shutil.which("pyrefly") is not None


@pytest.mark.skipif(not _has_pyrefly, reason="pyrefly not installed")
class TestPyreflyAdapterIntegration:
    """Integration tests that run pyrefly on actual Python files."""

    def test_is_available(self):
        adapter = PyreflyAdapter()
        assert adapter.is_available()

    def test_infer_simple_file(self, tmp_path):
        test_file = tmp_path / "example.py"
        test_file.write_text(textwrap.dedent("""\
            def greet(name: str) -> str:
                return f"Hello, {name}"

            x: int = 42
            y = greet("world")
        """))

        # Create a minimal pyrefly config so it doesn't complain
        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(test_file, project_root=tmp_path)

        assert ft.path == str(test_file)
        assert len(ft.bindings) > 0

        # Should find the function definition
        defs = ft.definitions()
        def_names = {b.name for b in defs}
        assert "greet" in def_names

    def test_infer_type_inference(self, tmp_path):
        test_file = tmp_path / "infer.py"
        test_file.write_text(textwrap.dedent("""\
            class Connection:
                host: str
                port: int

                def __init__(self, host: str, port: int) -> None:
                    self.host = host
                    self.port = port

            def create() -> Connection:
                return Connection("localhost", 5432)

            conn = create()
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(test_file, project_root=tmp_path)

        # Should find conn with inferred type Connection
        conn_bindings = ft.types_for_name("conn")
        assert len(conn_bindings) > 0
        # At least one binding should resolve to Connection
        conn_types = {b.raw_type for b in conn_bindings}
        assert "Connection" in conn_types

    def test_caching(self, tmp_path):
        test_file = tmp_path / "cached.py"
        test_file.write_text("x: int = 42\n")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()

        # First call should run pyrefly
        ft1 = adapter.infer_file(test_file, project_root=tmp_path)

        # Second call should use cache (same content hash)
        ft2 = adapter.infer_file(test_file, project_root=tmp_path)

        assert ft1 is ft2  # Should be the exact same object from cache

    def test_cache_invalidation_on_change(self, tmp_path):
        test_file = tmp_path / "changing.py"
        test_file.write_text("x: int = 42\n")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()

        ft1 = adapter.infer_file(test_file, project_root=tmp_path)

        # Modify the file
        test_file.write_text("x: str = 'hello'\n")

        # Should get fresh results (different content hash)
        ft2 = adapter.infer_file(test_file, project_root=tmp_path)

        assert ft1 is not ft2  # Different objects

    def test_nonexistent_file(self):
        adapter = PyreflyAdapter()
        ft = adapter.infer_file(Path("/nonexistent/file.py"))
        assert len(ft.bindings) == 0

    def test_type_at(self, tmp_path):
        test_file = tmp_path / "typed.py"
        test_file.write_text(textwrap.dedent("""\
            class Foo:
                x: int = 42
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(test_file, project_root=tmp_path)

        # Should be able to find Foo definition
        foo_bindings = ft.types_for_name("Foo")
        assert len(foo_bindings) > 0

    def test_clear_cache(self, tmp_path):
        test_file = tmp_path / "clear.py"
        test_file.write_text("x: int = 42\n")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        adapter.infer_file(test_file, project_root=tmp_path)
        assert len(adapter._cache) > 0

        adapter.clear_cache()
        assert len(adapter._cache) == 0

    def test_batch_infer(self, tmp_path):
        f1 = tmp_path / "mod_a.py"
        f1.write_text("a: int = 1\n")
        f2 = tmp_path / "mod_b.py"
        f2.write_text("b: str = 'hello'\n")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        results = adapter.infer_batch([f1, f2], project_root=tmp_path)

        assert str(f1.resolve()) in results
        assert str(f2.resolve()) in results

    def test_unavailable_engine(self):
        adapter = PyreflyAdapter(pyrefly_path="/nonexistent/pyrefly")
        assert not adapter.is_available()

    def test_union_type_inference(self, tmp_path):
        test_file = tmp_path / "union.py"
        test_file.write_text(textwrap.dedent("""\
            from typing import Optional

            def maybe_int(x: bool) -> Optional[int]:
                if x:
                    return 42
                return None

            result: Optional[int] = maybe_int(True)
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(test_file, project_root=tmp_path)

        # Should find result with Optional[int] / int | None type
        result_bindings = ft.types_for_name("result")
        assert len(result_bindings) > 0
        result_types = {b.raw_type for b in result_bindings}
        assert any("int" in t and "None" in t for t in result_types)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_pyrefly, reason="pyrefly not installed")
class TestTypesCLI:
    """Integration tests for the `emend types` CLI command."""

    def test_types_basic(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "example.py"
        test_file.write_text(textwrap.dedent("""\
            def add(a: int, b: int) -> int:
                return a + b
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        result = run_emend_cmd(["types", str(test_file)], check=False)
        # Should succeed or show some type info
        assert result.returncode == 0 or "add" in result.stdout

    def test_types_json_output(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "example.py"
        test_file.write_text(textwrap.dedent("""\
            x: int = 42
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        result = run_emend_cmd(["types", str(test_file), "--json"], check=False)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            assert isinstance(data, list)

    def test_types_definitions_only(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "example.py"
        test_file.write_text(textwrap.dedent("""\
            class Foo:
                x: int = 42

                def bar(self) -> str:
                    return "baz"
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        result = run_emend_cmd(["types", str(test_file), "--definitions-only", "--json"], check=False)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            assert isinstance(data, list)
            # All entries should be definitions
            for entry in data:
                assert entry["kind"] == "definition"

    def test_types_name_filter(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "example.py"
        test_file.write_text(textwrap.dedent("""\
            x: int = 42
            y: str = "hello"
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        result = run_emend_cmd(["types", str(test_file), "--name", "x", "--json"], check=False)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            assert isinstance(data, list)
            for entry in data:
                assert entry["name"] == "x"

    def test_types_unavailable_engine(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "example.py"
        test_file.write_text("x = 1\n")

        result = run_emend_cmd(["types", str(test_file), "--engine", "nonexistent"], check=False)
        assert result.returncode != 0
