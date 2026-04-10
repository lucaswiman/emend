"""Tests for unreachable block detection in TypeScript and Rust source files."""

from __future__ import annotations

import textwrap


# ---------------------------------------------------------------------------
# TypeScript unreachable block tests
# ---------------------------------------------------------------------------


class TestUnreachableBlocksTypeScript:
    """Unreachable block detection for TypeScript source files."""

    def _build_index(self, project_path: str) -> None:
        from emend.transform import warm_caches

        warm_caches(project_path, type_engine="none")

    def test_unreachable_after_return_ts(self, tmp_path):
        """Code after a return statement in a TypeScript function is unreachable."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(textwrap.dedent("""\
            function f(): number {
                return 1;
                const x = 2;
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) >= 1
        assert any(b.start_line >= 3 for b in blocks)

    def test_unreachable_after_throw_ts(self, tmp_path):
        """Code after a throw statement in a TypeScript function is unreachable."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(textwrap.dedent("""\
            function f(): never {
                throw new Error("oops");
                const x = 2;
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) >= 1
        assert any(b.start_line >= 3 for b in blocks)

    def test_reachable_after_if_return_ts(self, tmp_path):
        """Code after a conditional return IS reachable - should not be flagged."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(textwrap.dedent("""\
            function f(x: number): number {
                if (x > 0) {
                    return x;
                }
                return -x;
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) == 0

    def test_unreachable_after_if_else_both_return_ts(self, tmp_path):
        """Code after an if/else where both branches return is unreachable."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.ts").write_text(textwrap.dedent("""\
            function f(x: number): number {
                if (x > 0) {
                    return x;
                } else {
                    return -x;
                }
                const dead = 1;
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) >= 1
        assert any(b.start_line >= 7 for b in blocks)


# ---------------------------------------------------------------------------
# Rust unreachable block tests
# ---------------------------------------------------------------------------


class TestUnreachableBlocksRust:
    """Unreachable block detection for Rust source files."""

    def _build_index(self, project_path: str) -> None:
        from emend.transform import warm_caches

        warm_caches(project_path, type_engine="none")

    def test_unreachable_after_return_rs(self, tmp_path):
        """Code after a return statement in a Rust function is unreachable."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.rs").write_text(textwrap.dedent("""\
            fn f() -> i32 {
                return 1;
                let x = 2;
                x
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) >= 1
        assert any(b.start_line >= 3 for b in blocks)

    def test_unreachable_after_break_rs(self, tmp_path):
        """Code after a break statement in a Rust loop is unreachable."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.rs").write_text(textwrap.dedent("""\
            fn f() -> i32 {
                loop {
                    break;
                    let x = 2;
                }
                1
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) >= 1
        assert any(b.start_line >= 4 for b in blocks)

    def test_reachable_after_if_return_rs(self, tmp_path):
        """Code after a conditional return in Rust IS reachable - should not be flagged."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.rs").write_text(textwrap.dedent("""\
            fn f(x: i32) -> i32 {
                if x > 0 {
                    return x;
                }
                -x
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) == 0

    def test_unreachable_after_match_all_return_rs(self, tmp_path):
        """Code after a match expression where all arms return is unreachable."""
        from emend.transform import DeadBlock, DeadSymbol, find_dead_code

        project = tmp_path / "project"
        project.mkdir()
        (project / "mod.rs").write_text(textwrap.dedent("""\
            fn f(x: i32) -> i32 {
                match x {
                    0 => return 0,
                    _ => return 1,
                }
                let dead = 2;
                dead
            }
        """))
        self._build_index(str(project))
        dead = list(find_dead_code(str(project), show_last_reference=False))
        blocks = [d for d in dead if isinstance(d, DeadBlock)]
        assert len(blocks) >= 1
        assert any(b.start_line >= 6 for b in blocks)
