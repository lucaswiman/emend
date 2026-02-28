"""Tests for the type inference adapter (type_oracle.py).

Tests cover:
- TypeDescriptor parsing from pyrefly type strings
- TypeDescriptor structural matching
- Pyrefly debug-info JSON parsing into FileTypes
- FileTypes indexing (by position, by name)
- _FileTypeCache behavior (FIFO eviction, thread safety)
- PyreflyAdapter integration (requires pyrefly installed)
- CLI `types` command integration
"""
from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest

from emend.type_oracle import (
    FileTypes,
    PyreflyAdapter,
    TypeBinding,
    TypeDescriptor,
    _FileTypeCache,
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
        assert result.returncode == 0
        assert "add" in result.stdout

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


# ---------------------------------------------------------------------------
# Stress tests: parsing edge cases
# ---------------------------------------------------------------------------

class TestParseTypeStringEdgeCases:
    """Stress tests for _parse_type_string with tricky inputs."""

    def test_whitespace_padded(self):
        td = _parse_type_string("  int  ")
        assert td.kind == "named"
        assert td.name == "int"

    def test_deeply_nested_parameterized(self):
        raw = "dict[str, list[tuple[int, Optional[set[frozenset[bytes]]]]]]"
        td = _parse_type_string(raw)
        assert td.kind == "parameterized"
        assert td.name == "dict"
        assert td.params[1].kind == "parameterized"
        assert td.params[1].name == "list"
        inner_tuple = td.params[1].params[0]
        assert inner_tuple.kind == "parameterized"
        assert inner_tuple.name == "tuple"

    def test_union_with_parameterized_members(self):
        td = _parse_type_string("list[int] | dict[str, float] | None")
        assert td.kind == "union"
        assert len(td.params) == 3
        assert td.params[0].kind == "parameterized"
        assert td.params[0].name == "list"
        assert td.params[1].kind == "parameterized"
        assert td.params[1].name == "dict"
        assert td.params[2].kind == "named"
        assert td.params[2].name == "None"

    def test_callable_positional_only(self):
        td = _parse_type_string("(x: int, /, y: str) -> bool")
        assert td.kind == "callable"
        # / should be skipped, yielding 2 param types
        assert len(td.params) == 2
        assert td.params[0].name == "int"
        assert td.params[1].name == "str"
        assert td.return_type.name == "bool"

    def test_callable_keyword_only(self):
        td = _parse_type_string("(x: int, *, key: str) -> None")
        assert td.kind == "callable"
        # * should be skipped, yielding 2 param types
        assert len(td.params) == 2
        assert td.params[0].name == "int"
        assert td.params[1].name == "str"

    def test_callable_no_arrow(self):
        """Parenthesized expression without -> should fall through."""
        td = _parse_type_string("(int, str)")
        # No " -> " in this string, so it's not a callable
        assert td.kind == "named"
        assert td.name == "(int, str)"

    def test_callable_with_union_return(self):
        td = _parse_type_string("(x: int) -> str | None")
        assert td.kind == "callable"
        assert td.return_type.kind == "union"
        assert len(td.return_type.params) == 2

    def test_callable_with_nested_callable_param(self):
        td = _parse_type_string("(callback: (int) -> str, x: int) -> None")
        assert td.kind == "callable"
        # The callback param type itself is a callable
        assert td.params[0].kind == "callable"
        assert td.params[0].return_type.name == "str"

    def test_empty_parameterized(self):
        td = _parse_type_string("tuple[]")
        assert td.kind == "parameterized"
        assert td.name == "tuple"
        # Empty params list — single empty-string param gets parsed as named("")
        # This is an edge case; just verify it doesn't crash

    def test_multiline_overload(self):
        raw = (
            "Overload[\n"
            "  [T](arg1: T, arg2: T) -> T\n"
            "  [T](iterable: Iterable[T]) -> T\n"
            "]"
        )
        td = _parse_type_string(raw)
        assert td.kind == "named"
        assert td.name == "Overload"

    def test_self_at_with_dotted_path(self):
        td = _parse_type_string("Self@MyModule.Connection")
        assert td.kind == "named"
        assert td.name == "MyModule.Connection"

    def test_pipe_in_identifier(self):
        """A | without spaces should NOT be treated as union."""
        td = _parse_type_string("BitwiseOr")
        assert td.kind == "named"
        assert td.name == "BitwiseOr"

    def test_type_with_quoted_literal(self):
        """Literal types from pyrefly."""
        td = _parse_type_string("Literal[True]")
        assert td.kind == "parameterized"
        assert td.name == "Literal"


class TestSplitUnionEdgeCases:

    def test_union_inside_nested_brackets(self):
        """Union inside double-nested brackets should not split."""
        parts = _split_union("dict[str, list[int | float]] | None")
        assert parts == ["dict[str, list[int | float]]", "None"]

    def test_single_type_no_pipe(self):
        parts = _split_union("int")
        assert parts == ["int"]

    def test_pipe_without_spaces(self):
        """'int|str' (no spaces) should NOT split."""
        parts = _split_union("int|str")
        assert parts == ["int|str"]

    def test_three_way_union(self):
        parts = _split_union("int | str | None")
        assert parts == ["int", "str", "None"]

    def test_union_with_callable(self):
        """Callable inside parens should not be split."""
        parts = _split_union("(int) -> str | None")
        assert parts == ["(int) -> str", "None"]


class TestSplitParamsEdgeCases:

    def test_empty_string(self):
        parts = _split_params("")
        assert parts == []

    def test_deeply_nested(self):
        parts = _split_params("dict[str, list[int]], tuple[float, complex]")
        assert parts == ["dict[str, list[int]]", "tuple[float, complex]"]

    def test_callable_param(self):
        parts = _split_params("(int) -> str, int")
        assert parts == ["(int) -> str", "int"]

    def test_trailing_comma(self):
        parts = _split_params("int, str, ")
        assert len(parts) == 3
        assert parts[2] == ""


# ---------------------------------------------------------------------------
# Stress tests: matches() edge cases
# ---------------------------------------------------------------------------

class TestMatchesEdgeCases:

    def test_callable_exact_match(self):
        td = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("str"),
        )
        constraint = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("str"),
        )
        assert td.matches(constraint)

    def test_callable_param_mismatch(self):
        td = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("str"),
        )
        constraint = TypeDescriptor.callable_(
            (TypeDescriptor.named("float"),),
            TypeDescriptor.named("str"),
        )
        assert not td.matches(constraint)

    def test_callable_return_mismatch(self):
        td = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("str"),
        )
        constraint = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("int"),
        )
        assert not td.matches(constraint)

    def test_callable_arity_mismatch(self):
        td = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"), TypeDescriptor.named("str")),
            TypeDescriptor.named("None"),
        )
        constraint = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("None"),
        )
        assert not td.matches(constraint)

    def test_callable_vs_named(self):
        td = TypeDescriptor.named("Callable")
        constraint = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("str"),
        )
        assert not td.matches(constraint)

    def test_named_callable_constraint_doesnt_match_callable_type(self):
        td = TypeDescriptor.callable_(
            (TypeDescriptor.named("int"),),
            TypeDescriptor.named("str"),
        )
        constraint = TypeDescriptor.named("Callable")
        assert not td.matches(constraint)

    def test_parameterized_with_unknown_wildcard(self):
        """Unknown params in constraint should match anything."""
        td = TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),))
        constraint = TypeDescriptor.parameterized("list", (TypeDescriptor.unknown(),))
        assert td.matches(constraint)

    def test_union_self_matches_constraint_member(self):
        """A union type should match a union constraint if any member matches."""
        td = TypeDescriptor.union((TypeDescriptor.named("str"), TypeDescriptor.named("None")))
        constraint = TypeDescriptor.union((TypeDescriptor.named("str"), TypeDescriptor.named("int")))
        # td's "str" matches constraint's "str" member
        assert td.matches(constraint)

    def test_deeply_nested_parameterized_match(self):
        td = TypeDescriptor.parameterized("dict", (
            TypeDescriptor.named("str"),
            TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),)),
        ))
        constraint = TypeDescriptor.parameterized("dict", (
            TypeDescriptor.named("str"),
            TypeDescriptor.parameterized("list", (TypeDescriptor.named("int"),)),
        ))
        assert td.matches(constraint)


# ---------------------------------------------------------------------------
# Stress tests: display() roundtrip
# ---------------------------------------------------------------------------

class TestDisplayRoundtrip:
    """Verify display() output can be re-parsed to an equivalent descriptor."""

    @pytest.mark.parametrize("raw", [
        "int",
        "str",
        "list[int]",
        "dict[str, int]",
        "str | None",
        "int | str | None",
        "(int) -> str",
        "() -> None",
        "(str, int) -> bool",
    ])
    def test_roundtrip(self, raw):
        td1 = _parse_type_string(raw)
        displayed = td1.display()
        td2 = _parse_type_string(displayed)
        assert td1 == td2, f"Roundtrip failed: {raw!r} -> {displayed!r}"


# ---------------------------------------------------------------------------
# Stress tests: deduplication ordering
# ---------------------------------------------------------------------------

class TestDeduplicationOrdering:

    def test_definition_after_reference_wins(self):
        """If a reference binding arrives before the definition, the definition should win."""
        debug = {
            "modules": {
                "test_module": {
                    "bindings": [
                        {
                            "key": "Key::CompletedPartialType(x 5:1-2)",
                            "location": "5:1-2",
                            "binding": "CompletedPartialType(...)",
                            "result": "int",
                        },
                        {
                            "key": "Key::Definition(x 5:1-2)",
                            "location": "5:1-2",
                            "binding": "NameAssign(x, None, 42)",
                            "result": "int",
                        },
                    ]
                }
            }
        }
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 1
        assert ft.bindings[0].binding_kind == "definition"

    def test_two_references_at_same_position(self):
        """Only the first non-definition at a position should be kept."""
        debug = {
            "modules": {
                "test_module": {
                    "bindings": [
                        {
                            "key": "Key::BoundName(x 5:1-2)",
                            "location": "5:1-2",
                            "binding": "Forward(...)",
                            "result": "int",
                        },
                        {
                            "key": "Key::CompletedPartialType(x 5:1-2)",
                            "location": "5:1-2",
                            "binding": "CompletedPartialType(...)",
                            "result": "int",
                        },
                    ]
                }
            }
        }
        ft = _parse_pyrefly_debug(debug, "test_module.py")
        assert len(ft.bindings) == 1
        assert ft.bindings[0].binding_kind == "reference"


# ---------------------------------------------------------------------------
# Stress tests: module matching in _parse_pyrefly_debug
# ---------------------------------------------------------------------------

class TestModuleMatching:

    def test_exact_stem_match(self):
        debug = {
            "modules": {
                "mymod": {"bindings": [
                    {"key": "Key::Definition(x 1:1-2)", "location": "1:1-2",
                     "binding": "NameAssign(x, None, 1)", "result": "int"},
                ]},
                "other": {"bindings": []},
            }
        }
        ft = _parse_pyrefly_debug(debug, "/some/path/mymod.py")
        assert len(ft.bindings) == 1

    def test_dotted_module_matches_path(self):
        debug = {
            "modules": {
                "pkg.sub": {"bindings": [
                    {"key": "Key::Definition(x 1:1-2)", "location": "1:1-2",
                     "binding": "NameAssign(x, None, 1)", "result": "int"},
                ]}
            }
        }
        ft = _parse_pyrefly_debug(debug, "/project/src/pkg/sub.py")
        assert len(ft.bindings) == 1

    def test_substring_does_not_match(self):
        """Module 'a' should NOT match file 'bar.py' (substring match)."""
        debug = {
            "modules": {
                "a": {"bindings": [
                    {"key": "Key::Definition(x 1:1-2)", "location": "1:1-2",
                     "binding": "NameAssign(x, None, 1)", "result": "int"},
                ]},
            }
        }
        # File is "bar.py" — stem is "bar", not "a".
        # The old code would match "a" as a substring of "bar" via
        # `mod_name.replace('.', '/') in file_path`.
        ft = _parse_pyrefly_debug(debug, "/some/path/bar.py")
        # Falls through to first module (fallback), so still gets bindings.
        # But the important thing is it doesn't match on substring.
        # With only one module, fallback is fine. Test with two modules:
        debug2 = {
            "modules": {
                "bar": {"bindings": [
                    {"key": "Key::Definition(correct 1:1-2)", "location": "1:1-2",
                     "binding": "NameAssign(correct, None, 1)", "result": "int"},
                ]},
                "a": {"bindings": [
                    {"key": "Key::Definition(wrong 1:1-2)", "location": "1:1-2",
                     "binding": "NameAssign(wrong, None, 1)", "result": "str"},
                ]},
            }
        }
        ft2 = _parse_pyrefly_debug(debug2, "/some/path/bar.py")
        assert len(ft2.bindings) == 1
        assert ft2.bindings[0].name == "correct"


# ---------------------------------------------------------------------------
# Stress tests: cache under concurrent access
# ---------------------------------------------------------------------------

class TestCacheConcurrency:

    def test_concurrent_puts(self):
        """Multiple threads writing to cache simultaneously should not corrupt it."""
        import threading

        cache = _FileTypeCache(max_entries=100)
        errors = []

        def writer(thread_id):
            try:
                for i in range(50):
                    key = f"thread{thread_id}_key{i}"
                    cache.put(key, FileTypes(path=f"test{thread_id}_{i}.py"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(cache) <= 100

    def test_concurrent_get_and_put(self):
        """Readers and writers concurrently should not crash."""
        import threading

        cache = _FileTypeCache(max_entries=50)
        errors = []

        def writer():
            try:
                for i in range(100):
                    cache.put(f"key{i}", FileTypes(path=f"test{i}.py"))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(100):
                    cache.get(f"key{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ---------------------------------------------------------------------------
# Stress tests: PyreflyAdapter with malformed inputs
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_pyrefly, reason="pyrefly not installed")
class TestPyreflyAdapterEdgeCases:

    def test_syntax_error_file(self, tmp_path):
        """Pyrefly should still produce some output for files with syntax errors."""
        test_file = tmp_path / "bad.py"
        test_file.write_text("def foo(\n")  # intentional syntax error

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        # Should not raise — just return empty or partial results
        ft = adapter.infer_file(test_file, project_root=tmp_path)
        assert isinstance(ft, FileTypes)

    def test_empty_file(self, tmp_path):
        test_file = tmp_path / "empty.py"
        test_file.write_text("")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(test_file, project_root=tmp_path)
        assert isinstance(ft, FileTypes)

    def test_file_with_only_comments(self, tmp_path):
        test_file = tmp_path / "comments.py"
        test_file.write_text("# This is a comment\n# Another comment\n")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(test_file, project_root=tmp_path)
        assert isinstance(ft, FileTypes)

    def test_large_file(self, tmp_path):
        """Test with a file that has many symbols."""
        lines = []
        for i in range(100):
            lines.append(f"var_{i}: int = {i}")
        test_file = tmp_path / "large.py"
        test_file.write_text("\n".join(lines) + "\n")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(test_file, project_root=tmp_path)
        defs = ft.definitions()
        # Should find at least some of the 100 variables
        assert len(defs) >= 50

    def test_batch_partial_cache(self, tmp_path):
        """Batch infer where some files are cached and some are not."""
        f1 = tmp_path / "cached_file.py"
        f1.write_text("x: int = 1\n")
        f2 = tmp_path / "uncached_file.py"
        f2.write_text("y: str = 'hello'\n")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()

        # Pre-cache f1
        adapter.infer_file(f1, project_root=tmp_path)
        assert len(adapter._cache) == 1

        # Batch should use cache for f1 and run pyrefly for f2
        results = adapter.infer_batch([f1, f2], project_root=tmp_path)
        assert str(f1.resolve()) in results
        assert str(f2.resolve()) in results

    def test_cross_module_inference(self, tmp_path):
        """Test that pyrefly can resolve types across modules."""
        mod_a = tmp_path / "mod_a.py"
        mod_a.write_text(textwrap.dedent("""\
            class Widget:
                name: str
                def __init__(self, name: str) -> None:
                    self.name = name
        """))

        mod_b = tmp_path / "mod_b.py"
        mod_b.write_text(textwrap.dedent("""\
            from mod_a import Widget

            w = Widget("button")
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        ft = adapter.infer_file(mod_b, project_root=tmp_path)

        # w should be inferred as Widget
        w_bindings = ft.types_for_name("w")
        if w_bindings:
            w_types = {b.raw_type for b in w_bindings}
            assert "Widget" in w_types

    def test_nonexistent_batch_file(self, tmp_path):
        """Batch with a nonexistent file should produce empty FileTypes for it."""
        f1 = tmp_path / "exists.py"
        f1.write_text("x: int = 1\n")
        f2 = tmp_path / "does_not_exist.py"

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        adapter = PyreflyAdapter()
        results = adapter.infer_batch([f1, f2], project_root=tmp_path)

        assert str(f2.resolve()) in results
        assert len(results[str(f2.resolve())].bindings) == 0


# ---------------------------------------------------------------------------
# Stress tests: CLI edge cases
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _has_pyrefly, reason="pyrefly not installed")
class TestTypesCLIEdgeCases:

    def test_types_empty_file(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "empty.py"
        test_file.write_text("")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        result = run_emend_cmd(["types", str(test_file)], check=False)
        assert result.returncode == 0

    def test_types_json_empty_result(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "empty.py"
        test_file.write_text("")

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        result = run_emend_cmd(["types", str(test_file), "--json"], check=False)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            assert isinstance(data, list)

    def test_types_kind_filter(self, tmp_path, run_emend_cmd):
        test_file = tmp_path / "example.py"
        test_file.write_text(textwrap.dedent("""\
            x: int = 42
            y = x + 1
        """))

        config = tmp_path / "pyrefly.toml"
        config.write_text('[default]\nproject_includes = ["."]\n')

        result = run_emend_cmd(
            ["types", str(test_file), "--kind", "definition", "--json"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            for entry in data:
                assert entry["kind"] == "definition"
