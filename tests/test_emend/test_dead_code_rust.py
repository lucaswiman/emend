"""Tests for dead code detection in Rust source files."""

from __future__ import annotations

import pytest


class TestDeadCodeRust:
    """Tests for find_dead_code() applied to Rust source files."""

    def test_unreferenced_function_rs(self, tmp_path):
        """An unreferenced Rust function is flagged as dead; a called function is not."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            "fn used() -> i32 { 1 }\n"
            "fn unused() -> i32 { 2 }\n"
            "fn main() { let _x = used(); }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "unused" in dead_names
        assert "used" not in dead_names
        assert "main" not in dead_names

    def test_pub_function_not_dead(self, tmp_path):
        """A `pub` function is a public API and should never be flagged as dead."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            "pub fn public_api() -> i32 { 1 }\n"
            "fn internal() -> i32 { 2 }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "public_api" not in dead_names
        assert "internal" in dead_names

    def test_pub_struct_not_dead(self, tmp_path):
        """A `pub` struct is public API and should not be flagged as dead code."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            "pub struct PublicStruct { pub x: i32 }\n"
            "struct UnusedStruct { x: i32 }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "PublicStruct" not in dead_names, "pub struct is exported"
        assert "UnusedStruct" in dead_names, "private unreferenced struct is dead"

    def test_fn_main_not_dead(self, tmp_path):
        """fn main is an entry point and should never be reported as dead code."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "main.rs").write_text(
            "fn helper() -> i32 { 1 }\n"
            "fn main() { let _x = helper(); }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "main" not in dead_names
        assert "helper" not in dead_names

    def test_test_attribute_not_dead(self, tmp_path):
        """Functions marked with #[test] are entry points and must not be flagged."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            "fn unused_helper() -> i32 { 1 }\n"
            "#[test]\n"
            "fn test_something() { assert_eq!(unused_helper(), 1); }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        # test_something has #[test] so it is an entry point
        assert "test_something" not in dead_names
        # unused_helper is referenced by test_something, so it should not be dead
        assert "unused_helper" not in dead_names

    def test_no_mangle_not_dead(self, tmp_path):
        """Functions with #[no_mangle] are FFI entry points and must not be flagged."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            '#[no_mangle]\n'
            'pub extern "C" fn ffi_function() -> i32 { 1 }\n'
            "fn internal_helper() -> i32 { 2 }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "ffi_function" not in dead_names
        assert "internal_helper" in dead_names

    def test_noqa_suppression_rs(self, tmp_path):
        """// noqa: emend:deadcode on a Rust definition line suppresses flagging."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            "fn suppressed() -> i32 { 1 } // noqa: emend:deadcode\n"
            "fn unsuppressed() -> i32 { 2 }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "suppressed" not in dead_names
        assert "unsuppressed" in dead_names

    def test_test_prefix_not_dead(self, tmp_path):
        """Functions whose names start with test_ are entry points in Rust config."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            "fn test_something() {}\n"
            "fn regular_unused() {}\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "test_something" not in dead_names
        assert "regular_unused" in dead_names

    def test_trait_impl_not_dead(self, tmp_path):
        """Methods that implement a public trait should not be flagged as dead code."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.rs").write_text(
            "pub trait MyTrait {\n"
            "    fn do_thing(&self) -> i32;\n"
            "}\n"
            "\n"
            "struct MyImpl;\n"
            "\n"
            "impl MyTrait for MyImpl {\n"
            "    fn do_thing(&self) -> i32 { 42 }\n"
            "}\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        # MyTrait is pub so it is not dead.
        assert "MyTrait" not in dead_names
        # do_thing inside the impl satisfies the trait contract and should not
        # be flagged.
        assert "do_thing" not in dead_names
