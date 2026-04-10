"""Tests for dead-code detection on TypeScript projects."""
from pathlib import Path

import pytest


class TestDeadCodeTypeScript:
    """Dead code detection for TypeScript files."""

    def test_unreferenced_function_ts(self, tmp_path):
        """An unreferenced TypeScript function is flagged as dead code."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function used(): number { return 1; }\n"
            "function unused(): number { return 2; }\n"
            "const x = used();\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "unused" in dead_names
        assert "used" not in dead_names

    def test_exported_function_not_dead(self, tmp_path):
        """An exported TypeScript function is not flagged as dead code.

        Exported symbols are part of the public API and must not be flagged,
        even if they have no call sites within the same file/project.
        """
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "export function publicApi(): number { return 1; }\n"
            "function internal(): number { return 2; }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "publicApi" not in dead_names, "exported function should not be dead"
        assert "internal" in dead_names, "non-exported function with no callers is dead"

    def test_export_default_not_dead(self, tmp_path):
        """An `export default` function is not flagged as dead code."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "export default function main() { return 1; }\n"
            "function helper() { return 2; }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "main" not in dead_names, "export default function should not be dead"
        assert "helper" in dead_names

    def test_entry_point_names_not_dead(self, tmp_path):
        """Functions matching TypeScript entry point names are not flagged.

        The TypeScript config declares entry_point_names including:
        describe, it, test, beforeAll, afterAll, beforeEach, afterEach.
        These must never be reported as dead.
        """
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function describe() {}\n"
            "function it() {}\n"
            "function test() {}\n"
            "function beforeAll() {}\n"
            "function afterAll() {}\n"
            "function regularUnused() {}\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "describe" not in dead_names
        assert "it" not in dead_names
        assert "test" not in dead_names
        assert "beforeAll" not in dead_names
        assert "afterAll" not in dead_names
        assert "regularUnused" in dead_names

    def test_entry_point_name_prefixes_not_dead(self, tmp_path):
        """Functions with entry-point name prefixes (test, Test) are not dead.

        The TypeScript config declares entry_point_name_prefixes: [test, Test].
        """
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function testSomething(): void {}\n"
            "function TestWidget(): void {}\n"
            "function regularUnused(): void {}\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "testSomething" not in dead_names, "test-prefixed function should not be dead"
        assert "TestWidget" not in dead_names, "Test-prefixed function should not be dead"
        assert "regularUnused" in dead_names

    def test_class_unreferenced_ts(self, tmp_path):
        """An unreferenced TypeScript class is flagged as dead code."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "class UsedClass { method(): number { return 1; } }\n"
            "class UnusedClass { method(): number { return 2; } }\n"
            "const obj = new UsedClass();\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "UnusedClass" in dead_names
        assert "UsedClass" not in dead_names

    def test_noqa_suppression_ts(self, tmp_path):
        """// noqa: emend:deadcode suppresses a TypeScript dead code violation."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function suppressed(): number { return 1; } // noqa: emend:deadcode\n"
            "function unsuppressed(): number { return 2; }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "suppressed" not in dead_names, "noqa-annotated function should be suppressed"
        assert "unsuppressed" in dead_names

    def test_exported_function_not_dead_cross_file(self, tmp_path):
        """Exported symbols are not dead even in a multi-file TS project."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "lib.ts").write_text(
            "export function helper(): number { return 1; }\n"
            "function orphan(): number { return 2; }\n"
        )
        (project / "main.ts").write_text(
            "const x = helper();\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "orphan" in dead_names, "non-exported function with no callers is dead"
        assert "helper" not in dead_names, "exported function should not be dead"

    def test_test_file_detection_ts(self, tmp_path):
        """Symbols in *.test.ts files should not cause the referenced function to be dead."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        src = project / "src"
        src.mkdir()
        (src / "mod.ts").write_text(
            "export function helper(): number { return 1; }\n"
            "function orphan(): number { return 2; }\n"
        )
        (src / "mod.test.ts").write_text(
            "helper();\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        # helper is exported so not dead; orphan has no callers and is not exported
        assert "orphan" in dead_names
        assert "helper" not in dead_names

    def test_framework_decorator_entry_point_ts(self, tmp_path):
        """Classes with framework decorator base names are not flagged.

        The TypeScript config declares entry_point_decorator_basenames including
        route, get, post, handler, middleware, controller, component.
        In TypeScript, decorators apply to classes and class members (not
        standalone functions).
        """
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function Component(cls: any) { return cls; }\n"
            "@Component\n"
            "class MyWidget {\n"
            "    render(): string { return 'html'; }\n"
            "}\n"
            "function regularUnused(): void {}\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "MyWidget" not in dead_names, "decorated class should not be dead"
        assert "regularUnused" in dead_names

    def test_dead_symbol_fields_ts(self, tmp_path):
        """DeadSymbol has correct file_path, name, kind, line, selector for TypeScript."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        ts_file = project / "mod.ts"
        ts_file.write_text(
            "function orphan(): number {\n"
            "    return 42;\n"
            "}\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_syms = [d for d in dead if isinstance(d, DeadSymbol)]
        assert len(dead_syms) == 1
        d = dead_syms[0]
        assert d.name == "orphan"
        assert d.kind in ("function", "async_function")
        assert d.line == 1
        assert "mod.ts" in d.file_path
        assert "orphan" in d.selector
        assert d.reason == "no references found"

    def test_intra_file_call_not_dead_ts(self, tmp_path):
        """A TypeScript function called within the same file is not dead."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function normalize(s: string): string {\n"
            "    return s.trim().toLowerCase();\n"
            "}\n"
            "class Processor {\n"
            "    process(val: string): string {\n"
            "        return normalize(val);\n"
            "    }\n"
            "}\n"
            "const p = new Processor();\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "normalize" not in dead_names, "normalize is called by Processor.process"

    def test_main_function_not_dead_ts(self, tmp_path):
        """The `main` function name is an entry point and must not be flagged."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "index.ts").write_text(
            "function main(): void {}\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "main" not in dead_names

    def test_private_prefix_skipped_ts(self, tmp_path):
        """Functions starting with _ are skipped by default (private prefix heuristic)."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function _privateHelper(): number { return 1; }\n"
            "function publicUnused(): number { return 2; }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "_privateHelper" not in dead_names, "private-prefixed symbol excluded by default"
        assert "publicUnused" in dead_names

    def test_include_private_ts(self, tmp_path):
        """With include_private=True, _private TypeScript symbols are checked."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(
            "function _privateHelper(): number { return 1; }\n"
        )

        dead = list(find_dead_code(str(project), include_private=True, show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        assert "_privateHelper" in dead_names

    def test_empty_ts_project(self, tmp_path):
        """An empty TypeScript project returns no dead code."""
        from emend.transform import find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text("")

        dead = list(find_dead_code(str(project), show_last_reference=False))
        assert dead == []

    def test_spec_file_functions_not_dead_ts(self, tmp_path):
        """Functions in *.spec.ts files are entry points (they're test files)."""
        from emend.transform import DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "widget.ts").write_text(
            "export function renderWidget(): string { return 'html'; }\n"
        )
        (project / "widget.spec.ts").write_text(
            "function describeWidget(): void { renderWidget(); }\n"
        )

        dead = list(find_dead_code(str(project), show_last_reference=False))
        dead_names = {d.name for d in dead if isinstance(d, DeadSymbol)}
        # describeWidget starts with "describe" prefix — that's an entry_point_name
        # renderWidget is exported — not dead
        assert "renderWidget" not in dead_names
        assert "describeWidget" not in dead_names
