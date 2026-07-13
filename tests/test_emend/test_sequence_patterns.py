"""Tests for Phase 5: Temporal Sequence Patterns.

Tests the ``compile_sequence_rule()`` / ``_compile_sequence_query()`` Datalog
compiler and the ``SequenceCheck`` policy integration.

Two levels of testing:
  1. YAML parsing and validation (no DB needed)
  2. Datalog query compilation via ``_compile_sequence_query()`` against
     manually-built ``FactGraph`` instances, plus the higher-level
     ``compile_sequence_rule()`` API.
"""

from __future__ import annotations

import textwrap

import pytest

from emend.fact_graph import (
    CfgBlockFact,
    CfgEdgeFact,
    DefUseFact,
    FactGraph,
    MethodCallFact,
    _compile_sequence_query,
)
from emend.policy import (
    Policy,
    PolicyViolation,
    SequenceCheck,
    SequencePathConstraint,
    SequenceStep,
    _parse_check,
    _run_sequence_check,
    load_policies,
    validate_policies,
)


# ---------------------------------------------------------------------------
# YAML parsing tests
# ---------------------------------------------------------------------------


class TestSequenceCheckParsing:
    """Test YAML parsing of sequence check definitions."""

    def test_parse_basic_sequence(self):
        raw = {
            "type": "sequence",
            "name": "toctou",
            "message": "TOCTOU detected",
            "sequence": [
                {"bind": "load", "pattern": "$OBJ = session.query($MODEL)"},
                {"bind": "mutate", "effect": "writes($OBJ)"},
            ],
        }
        check = _parse_check(raw)
        assert isinstance(check, SequenceCheck)
        assert check.name == "toctou"
        assert check.message == "TOCTOU detected"
        assert len(check.sequence) == 2
        assert check.sequence[0].bind == "load"
        assert check.sequence[0].pattern == "$OBJ = session.query($MODEL)"
        assert check.sequence[1].bind == "mutate"
        assert check.sequence[1].effect == "writes($OBJ)"

    def test_parse_sequence_with_path_constraints(self):
        raw = {
            "type": "sequence",
            "name": "toctou-full",
            "message": "TOCTOU detected",
            "sequence": [
                {"bind": "load", "pattern": "$OBJ = session.query($MODEL)"},
                {"bind": "mutate", "effect": "writes($OBJ)"},
            ],
            "path": {
                "load -> mutate": {
                    "not_through": [
                        {"pattern": "$Q.with_for_update()"},
                    ],
                    "not_through_scope": [
                        {"pattern": "session.commit()"},
                    ],
                },
            },
        }
        check = _parse_check(raw)
        assert isinstance(check, SequenceCheck)
        assert len(check.path_constraints) == 1
        pc = check.path_constraints[0]
        assert pc.from_step == "load"
        assert pc.to_step == "mutate"
        assert len(pc.not_through) == 1
        assert pc.not_through[0] == "$Q.with_for_update()"
        assert len(pc.not_through_scope) == 1
        assert pc.not_through_scope[0] == "session.commit()"

    def test_parse_sequence_with_type_constraint(self):
        raw = {
            "type": "sequence",
            "name": "typed-seq",
            "message": "test",
            "sequence": [
                {
                    "bind": "load",
                    "pattern": "$OBJ = session.query($MODEL)",
                    "type_constraint": "!int & !float",
                },
                {"bind": "mutate", "effect": "writes($OBJ)"},
            ],
        }
        check = _parse_check(raw)
        assert check.sequence[0].type_constraint == "!int & !float"

    def test_parse_requires_name(self):
        raw = {
            "type": "sequence",
            "message": "test",
            "sequence": [
                {"bind": "a", "pattern": "x"},
                {"bind": "b", "pattern": "y"},
            ],
        }
        with pytest.raises(ValueError, match="name"):
            _parse_check(raw)

    def test_parse_requires_at_least_2_steps(self):
        raw = {
            "type": "sequence",
            "name": "bad",
            "message": "test",
            "sequence": [
                {"bind": "a", "pattern": "x"},
            ],
        }
        with pytest.raises(ValueError, match="2 steps"):
            _parse_check(raw)

    def test_parse_step_requires_bind(self):
        """Steps without a bind name should get empty string, caught by validation."""
        raw = {
            "type": "sequence",
            "name": "test",
            "message": "test",
            "sequence": [
                {"pattern": "x"},
                {"bind": "b", "pattern": "y"},
            ],
        }
        check = _parse_check(raw)
        # Empty bind is parsed but caught by validate_policies
        assert check.sequence[0].bind == ""

    def test_parse_severity_default(self):
        raw = {
            "type": "sequence",
            "name": "test",
            "message": "test",
            "sequence": [
                {"bind": "a", "pattern": "x"},
                {"bind": "b", "pattern": "y"},
            ],
        }
        check = _parse_check(raw)
        assert check.severity == "error"

    def test_parse_custom_severity(self):
        raw = {
            "type": "sequence",
            "name": "test",
            "message": "test",
            "severity": "warning",
            "sequence": [
                {"bind": "a", "pattern": "x"},
                {"bind": "b", "pattern": "y"},
            ],
        }
        check = _parse_check(raw)
        assert check.severity == "warning"


class TestSequenceCheckValidation:
    """Test validation of sequence checks in policies."""

    def test_valid_sequence_policy(self):
        policy = Policy(
            name="test",
            description="test",
            severity="error",
            checks=[
                SequenceCheck(
                    name="toctou",
                    message="TOCTOU",
                    sequence=[
                        SequenceStep(bind="load", pattern="$OBJ = query()"),
                        SequenceStep(bind="mutate", effect="writes($OBJ)"),
                    ],
                )
            ],
        )
        errors = validate_policies([policy])
        assert errors == []

    def test_sequence_too_few_steps(self):
        policy = Policy(
            name="test",
            description="test",
            severity="error",
            checks=[
                SequenceCheck(
                    name="bad",
                    message="bad",
                    sequence=[
                        SequenceStep(bind="only", pattern="x"),
                    ],
                )
            ],
        )
        errors = validate_policies([policy])
        assert any("at least 2 steps" in e for e in errors)

    def test_sequence_step_needs_pattern_or_effect(self):
        policy = Policy(
            name="test",
            description="test",
            severity="error",
            checks=[
                SequenceCheck(
                    name="bad",
                    message="bad",
                    sequence=[
                        SequenceStep(bind="a", pattern="x"),
                        SequenceStep(bind="b"),  # no pattern or effect
                    ],
                )
            ],
        )
        errors = validate_policies([policy])
        assert any("pattern" in e and "effect" in e for e in errors)

    def test_duplicate_step_bind_names(self):
        policy = Policy(
            name="test",
            description="test",
            severity="error",
            checks=[
                SequenceCheck(
                    name="dup",
                    message="dup",
                    sequence=[
                        SequenceStep(bind="a", pattern="x"),
                        SequenceStep(bind="a", pattern="y"),
                    ],
                )
            ],
        )
        errors = validate_policies([policy])
        assert any("duplicate" in e for e in errors)

    def test_path_references_unknown_step(self):
        policy = Policy(
            name="test",
            description="test",
            severity="error",
            checks=[
                SequenceCheck(
                    name="bad-path",
                    message="bad",
                    sequence=[
                        SequenceStep(bind="a", pattern="x"),
                        SequenceStep(bind="b", pattern="y"),
                    ],
                    path_constraints=[
                        SequencePathConstraint(from_step="a", to_step="c"),
                    ],
                )
            ],
        )
        errors = validate_policies([policy])
        assert any("unknown step" in e for e in errors)


# ---------------------------------------------------------------------------
# _compile_sequence_query() unit tests (manually-built FactGraphs)
# ---------------------------------------------------------------------------


def _build_linear_graph(
    fp: str = "app.py",
    fq: str = "app.process",
    num_blocks: int = 3,
) -> FactGraph:
    """Build a FactGraph with a linear chain of CFG blocks."""
    g = FactGraph()
    for i in range(num_blocks):
        g.add_cfg_block(CfgBlockFact(fp, fq, i, is_entry=(i == 0), is_exit=(i == num_blocks - 1)))
    for i in range(num_blocks - 1):
        g.add_cfg_edge(CfgEdgeFact(fp, fq, i, i + 1, "fallthrough", 5 + i, 6 + i))
    return g


class TestCompileSequenceRuleBasic:
    """Test _compile_sequence_query() with pre-built FactGraphs."""

    def test_basic_two_step_violation(self):
        """Two pattern-matched steps, both present and reachable → violation."""
        g = _build_linear_graph()
        fp, fq = "app.py", "app.process"

        # obj defined in block 0, live to block 1
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=1, def_line=5, use_line=6))

        check = SequenceCheck(
            name="use-after-close",
            message="Use after close",
            sequence=[
                SequenceStep(bind="close", pattern="$FD.close()"),
                SequenceStep(bind="use", pattern="$FD.read()"),
            ],
        )

        step_locations = {
            "close": [("app.py", "app.process", 0, 5, {"FD": "obj"})],
            "use": [("app.py", "app.process", 1, 6, {"FD": "obj"})],
        }

        query = _compile_sequence_query(check, step_locations)
        assert query is not None
        result = g.run_query(query)
        assert len(result["rows"]) >= 1
        # Check output columns
        assert "fp" in result["headers"]
        assert "fq" in result["headers"]
        assert "first_line" in result["headers"]
        assert "last_line" in result["headers"]

    def test_effect_based_writes_violation(self):
        """Step 2 uses writes($OBJ) effect, resolved via def_use."""
        g = _build_linear_graph()
        fp, fq = "app.py", "app.process"

        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=1, def_line=5, use_line=6))
        # The mutation: obj.name = "new"
        g.add_def_use(DefUseFact(fp, fq, "obj.name", "write", def_block=1, use_block=1, def_line=6))

        check = SequenceCheck(
            name="toctou",
            message="TOCTOU detected",
            sequence=[
                SequenceStep(bind="load", pattern="$OBJ = session.query($MODEL)"),
                SequenceStep(bind="mutate", effect="writes($OBJ)"),
            ],
        )

        step_locations = {
            "load": [("app.py", "app.process", 0, 5, {"OBJ": "obj", "MODEL": "User"})],
            "mutate": [],  # effect-based, resolved in Datalog
        }

        query = _compile_sequence_query(check, step_locations)
        assert query is not None
        result = g.run_query(query)
        assert len(result["rows"]) >= 1

    def test_effect_writes_method_call(self):
        """writes($OBJ) detects method calls like obj.append()."""
        g = _build_linear_graph()
        fp, fq = "app.py", "app.process"

        g.add_def_use(DefUseFact(fp, fq, "items", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "items", "write", def_block=0, use_block=1, def_line=5, use_line=6))
        g.add_method_call(MethodCallFact(fp, fq, "items", "append", block_id=1, line=6))

        check = SequenceCheck(
            name="load-mutate",
            message="mutation detected",
            sequence=[
                SequenceStep(bind="load", pattern="$OBJ = load()"),
                SequenceStep(bind="mutate", effect="writes($OBJ)"),
            ],
        )

        step_locations = {
            "load": [("app.py", "app.process", 0, 5, {"OBJ": "items"})],
            "mutate": [],
        }

        query = _compile_sequence_query(check, step_locations)
        assert query is not None
        result = g.run_query(query)
        assert len(result["rows"]) >= 1

    def test_no_matching_steps_returns_none(self):
        """If no step locations were resolved, return None."""
        check = SequenceCheck(
            name="empty",
            message="test",
            sequence=[
                SequenceStep(bind="a", pattern="x"),
                SequenceStep(bind="b", pattern="y"),
            ],
        )
        step_locations = {"a": [], "b": []}
        result = _compile_sequence_query(check, step_locations)
        assert result is None

    def test_steps_in_different_functions_no_violation(self):
        """Steps in different functions should not produce a violation."""
        g = FactGraph()
        fp = "app.py"

        # Two functions
        g.add_cfg_block(CfgBlockFact(fp, "app.foo", 0, is_entry=True))
        g.add_cfg_block(CfgBlockFact(fp, "app.foo", 1, is_exit=True))
        g.add_cfg_edge(CfgEdgeFact(fp, "app.foo", 0, 1, "fallthrough", 5, 6))

        g.add_cfg_block(CfgBlockFact(fp, "app.bar", 0, is_entry=True))
        g.add_cfg_block(CfgBlockFact(fp, "app.bar", 1, is_exit=True))
        g.add_cfg_edge(CfgEdgeFact(fp, "app.bar", 0, 1, "fallthrough", 10, 11))

        check = SequenceCheck(
            name="cross-func",
            message="test",
            sequence=[
                SequenceStep(bind="a", pattern="x"),
                SequenceStep(bind="b", pattern="y"),
            ],
        )

        step_locations = {
            "a": [("app.py", "app.foo", 0, 5, {})],
            "b": [("app.py", "app.bar", 0, 10, {})],
        }

        query = _compile_sequence_query(check, step_locations)
        assert query is not None
        result = g.run_query(query)
        # Different functions — reachability won't connect
        assert len(result["rows"]) == 0

    def test_unreachable_blocks_no_violation(self):
        """If step B's block is not CFG-reachable from step A's → no violation."""
        g = FactGraph()
        fp, fq = "app.py", "app.process"

        # Block 0 → Block 2 (exit), Block 1 is disconnected
        g.add_cfg_block(CfgBlockFact(fp, fq, 0, is_entry=True))
        g.add_cfg_block(CfgBlockFact(fp, fq, 1))
        g.add_cfg_block(CfgBlockFact(fp, fq, 2, is_exit=True))
        g.add_cfg_edge(CfgEdgeFact(fp, fq, 0, 2, "fallthrough", 5, 7))
        # No edge from 0 to 1

        check = SequenceCheck(
            name="unreach",
            message="test",
            sequence=[
                SequenceStep(bind="a", pattern="x"),
                SequenceStep(bind="b", pattern="y"),
            ],
        )

        step_locations = {
            "a": [("app.py", "app.process", 0, 5, {})],
            "b": [("app.py", "app.process", 1, 6, {})],
        }

        query = _compile_sequence_query(check, step_locations)
        result = g.run_query(query)
        assert len(result["rows"]) == 0


class TestCompileSequenceBlockers:
    """Test not_through and not_through_scope blockers."""

    def test_not_through_blocks_reachability(self):
        """Blocker pattern on the path suppresses violation."""
        g = _build_linear_graph(num_blocks=4)
        fp, fq = "app.py", "app.process"

        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=2, def_line=5, use_line=7))

        check = SequenceCheck(
            name="toctou",
            message="TOCTOU",
            sequence=[
                SequenceStep(bind="load", pattern="$OBJ = query()"),
                SequenceStep(bind="mutate", effect="writes($OBJ)"),
            ],
            path_constraints=[
                SequencePathConstraint(
                    from_step="load",
                    to_step="mutate",
                    not_through=["with_for_update()"],
                ),
            ],
        )

        step_locations = {
            "load": [("app.py", "app.process", 0, 5, {"OBJ": "obj"})],
            "mutate": [],
        }
        # Blocker in block 1 (between load at 0 and mutation at 2)
        blocker_locations = {
            ("load", "mutate"): {
                "not_through": [("app.py", "app.process", 1)],
                "not_through_scope": [],
            }
        }

        query = _compile_sequence_query(check, step_locations, blocker_locations)
        assert query is not None
        result = g.run_query(query)
        assert len(result["rows"]) == 0

    def test_scope_kill_blocks_reachability(self):
        """Scope boundary sanitizer suppresses violation."""
        g = _build_linear_graph(num_blocks=4)
        fp, fq = "app.py", "app.process"

        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=2, def_line=5, use_line=7))
        g.add_def_use(DefUseFact(fp, fq, "obj.name", "write", def_block=2, use_block=2, def_line=7))

        check = SequenceCheck(
            name="toctou",
            message="TOCTOU",
            sequence=[
                SequenceStep(bind="load", pattern="$OBJ = query()"),
                SequenceStep(bind="mutate", effect="writes($OBJ)"),
            ],
            path_constraints=[
                SequencePathConstraint(
                    from_step="load",
                    to_step="mutate",
                    not_through_scope=["session.commit()"],
                ),
            ],
        )

        step_locations = {
            "load": [("app.py", "app.process", 0, 5, {"OBJ": "obj"})],
            "mutate": [],
        }
        blocker_locations = {
            ("load", "mutate"): {
                "not_through": [],
                "not_through_scope": [("app.py", "app.process", 1)],
            }
        }

        query = _compile_sequence_query(check, step_locations, blocker_locations)
        assert query is not None
        result = g.run_query(query)
        assert len(result["rows"]) == 0

    def test_blocker_not_on_path_still_violates(self):
        """Blocker in a block NOT on the path → violation still fires."""
        g = FactGraph()
        fp, fq = "app.py", "app.process"

        # Branching CFG: 0 → 1 → 3, 0 → 2 → 3
        g.add_cfg_block(CfgBlockFact(fp, fq, 0, is_entry=True))
        g.add_cfg_block(CfgBlockFact(fp, fq, 1))
        g.add_cfg_block(CfgBlockFact(fp, fq, 2))
        g.add_cfg_block(CfgBlockFact(fp, fq, 3, is_exit=True))
        g.add_cfg_edge(CfgEdgeFact(fp, fq, 0, 1, "true_branch", 5, 6))
        g.add_cfg_edge(CfgEdgeFact(fp, fq, 0, 2, "false_branch", 5, 7))
        g.add_cfg_edge(CfgEdgeFact(fp, fq, 1, 3, "fallthrough", 6, 8))
        g.add_cfg_edge(CfgEdgeFact(fp, fq, 2, 3, "fallthrough", 7, 8))

        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=3, def_line=5, use_line=8))

        check = SequenceCheck(
            name="toctou",
            message="TOCTOU",
            sequence=[
                SequenceStep(bind="load", pattern="query()"),
                SequenceStep(bind="mutate", pattern="mutate()"),
            ],
        )

        step_locations = {
            "load": [("app.py", "app.process", 0, 5, {"OBJ": "obj"})],
            "mutate": [("app.py", "app.process", 3, 8, {"OBJ": "obj"})],
        }
        # Blocker only on branch 1, but branch 2 is clear
        blocker_locations = {
            ("load", "mutate"): {
                "not_through": [("app.py", "app.process", 1)],
                "not_through_scope": [],
            }
        }

        query = _compile_sequence_query(check, step_locations, blocker_locations)
        result = g.run_query(query)
        # There exists a path 0 → 2 → 3 that avoids the blocker
        assert len(result["rows"]) >= 1


class TestCompileSequenceDefUseLiveness:
    """Test def-use liveness checks between sequence steps."""

    def test_variable_redefined_no_violation(self):
        """Variable reassigned between steps → binding dead → no violation."""
        g = _build_linear_graph(num_blocks=4)
        fp, fq = "app.py", "app.process"

        # obj defined in block 0, then REDEFINED in block 1
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=0, def_line=5))
        # No def_use from block 0 to block 2 — the redefinition kills it
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=1, use_block=1, def_line=6))
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=1, use_block=2, def_line=6, use_line=7))
        g.add_def_use(DefUseFact(fp, fq, "obj.name", "write", def_block=2, use_block=2, def_line=7))

        check = SequenceCheck(
            name="toctou",
            message="TOCTOU",
            sequence=[
                SequenceStep(bind="load", pattern="$OBJ = query()"),
                SequenceStep(bind="mutate", effect="writes($OBJ)"),
            ],
        )

        step_locations = {
            "load": [("app.py", "app.process", 0, 5, {"OBJ": "obj"})],
            "mutate": [],
        }

        query = _compile_sequence_query(check, step_locations)
        assert query is not None
        result = g.run_query(query)
        # Block 0's obj has no def_use reaching block 2, so liveness fails
        assert len(result["rows"]) == 0

    def test_variable_still_live_produces_violation(self):
        """Variable not reassigned → binding live → violation fires."""
        g = _build_linear_graph()
        fp, fq = "app.py", "app.process"

        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "obj", "write", def_block=0, use_block=1, def_line=5, use_line=6))
        g.add_def_use(DefUseFact(fp, fq, "obj.name", "write", def_block=1, use_block=1, def_line=6))

        check = SequenceCheck(
            name="toctou",
            message="TOCTOU",
            sequence=[
                SequenceStep(bind="load", pattern="$OBJ = query()"),
                SequenceStep(bind="mutate", effect="writes($OBJ)"),
            ],
        )

        step_locations = {
            "load": [("app.py", "app.process", 0, 5, {"OBJ": "obj"})],
            "mutate": [],
        }

        query = _compile_sequence_query(check, step_locations)
        result = g.run_query(query)
        assert len(result["rows"]) >= 1


class TestCompileSequenceMultiStep:
    """Test multi-step (>2) sequence rules."""

    def test_three_step_sequence(self):
        """Three-step sequence: acquire → use → release."""
        g = _build_linear_graph(num_blocks=4)
        fp, fq = "app.py", "app.process"

        g.add_def_use(DefUseFact(fp, fq, "lock", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "lock", "write", def_block=0, use_block=1, def_line=5, use_line=6))
        g.add_def_use(DefUseFact(fp, fq, "lock", "write", def_block=0, use_block=2, def_line=5, use_line=7))

        check = SequenceCheck(
            name="lock-order",
            message="Lock order violation",
            sequence=[
                SequenceStep(bind="acquire", pattern="$L = acquire_lock()"),
                SequenceStep(bind="use", pattern="use_resource($L)"),
                SequenceStep(bind="release", pattern="release_lock($L)"),
            ],
        )

        step_locations = {
            "acquire": [("app.py", "app.process", 0, 5, {"L": "lock"})],
            "use": [("app.py", "app.process", 1, 6, {"L": "lock"})],
            "release": [("app.py", "app.process", 2, 7, {"L": "lock"})],
        }

        query = _compile_sequence_query(check, step_locations)
        assert query is not None
        result = g.run_query(query)
        assert len(result["rows"]) >= 1

    def test_three_step_with_blocker_between_first_two(self):
        """Blocker between steps 1 and 2, but not between 2 and 3."""
        g = _build_linear_graph(num_blocks=5)
        fp, fq = "app.py", "app.process"

        g.add_def_use(DefUseFact(fp, fq, "x", "write", def_block=0, use_block=0, def_line=5))
        g.add_def_use(DefUseFact(fp, fq, "x", "write", def_block=0, use_block=2, def_line=5, use_line=7))
        g.add_def_use(DefUseFact(fp, fq, "x", "write", def_block=0, use_block=3, def_line=5, use_line=8))

        check = SequenceCheck(
            name="three-step",
            message="test",
            sequence=[
                SequenceStep(bind="a", pattern="$X = start()"),
                SequenceStep(bind="b", pattern="middle($X)"),
                SequenceStep(bind="c", pattern="end($X)"),
            ],
            path_constraints=[
                SequencePathConstraint(from_step="a", to_step="b", not_through=["blocker()"]),
            ],
        )

        step_locations = {
            "a": [("app.py", "app.process", 0, 5, {"X": "x"})],
            "b": [("app.py", "app.process", 2, 7, {"X": "x"})],
            "c": [("app.py", "app.process", 3, 8, {"X": "x"})],
        }
        # Blocker in block 1 between steps a(0) and b(2)
        blocker_locations = {
            ("a", "b"): {
                "not_through": [("app.py", "app.process", 1)],
                "not_through_scope": [],
            }
        }

        query = _compile_sequence_query(check, step_locations, blocker_locations)
        result = g.run_query(query)
        # Blocker blocks reachability from block 0 → block 2
        assert len(result["rows"]) == 0


class TestSequencePolicyYAML:
    """Test loading sequence policies from YAML files."""

    def test_load_full_sequence_policy(self, tmp_path):
        config = tmp_path / ".emend" / "policies.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(textwrap.dedent("""\
            policies:
              - name: toctou-check
                description: "Detect TOCTOU race conditions"
                severity: error
                checks:
                  - type: sequence
                    name: toctou-unlocked-mutation
                    message: "Mutation on ORM object without SELECT FOR UPDATE"
                    sequence:
                      - bind: load
                        pattern: "$OBJ = session.query($MODEL)"
                      - bind: mutate
                        effect: "writes($OBJ)"
                    path:
                      load -> mutate:
                        not_through:
                          - pattern: "$Q.with_for_update()"
                        not_through_scope:
                          - pattern: "session.commit()"
        """))
        policies = load_policies(config)
        assert len(policies) == 1
        p = policies[0]
        assert p.name == "toctou-check"
        assert len(p.checks) == 1
        check = p.checks[0]
        assert isinstance(check, SequenceCheck)
        assert check.name == "toctou-unlocked-mutation"
        assert len(check.sequence) == 2
        assert check.sequence[0].bind == "load"
        assert check.sequence[1].effect == "writes($OBJ)"
        assert len(check.path_constraints) == 1
        pc = check.path_constraints[0]
        assert pc.from_step == "load"
        assert pc.to_step == "mutate"
        assert "$Q.with_for_update()" in pc.not_through
        assert "session.commit()" in pc.not_through_scope

    def test_load_use_after_close_policy(self, tmp_path):
        config = tmp_path / ".emend" / "policies.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(textwrap.dedent("""\
            policies:
              - name: use-after-close
                description: "Detect use after close"
                severity: warning
                checks:
                  - type: sequence
                    name: fd-use-after-close
                    message: "File descriptor used after close"
                    severity: warning
                    sequence:
                      - bind: close_op
                        pattern: "$FD.close()"
                      - bind: use_op
                        pattern: "$FD.read()"
        """))
        policies = load_policies(config)
        check = policies[0].checks[0]
        assert isinstance(check, SequenceCheck)
        assert check.severity == "warning"
        assert check.sequence[0].bind == "close_op"
        assert check.sequence[1].bind == "use_op"

    def test_validate_loaded_policy(self, tmp_path):
        config = tmp_path / ".emend" / "policies.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(textwrap.dedent("""\
            policies:
              - name: good-policy
                description: "Valid sequence policy"
                severity: error
                checks:
                  - type: sequence
                    name: test-seq
                    message: "test"
                    sequence:
                      - bind: step1
                        pattern: "foo()"
                      - bind: step2
                        pattern: "bar()"
        """))
        policies = load_policies(config)
        errors = validate_policies(policies)
        assert errors == []


class TestParseEffectHelper:
    """Test the _parse_effect helper."""

    def test_parse_writes(self):
        from emend.fact_graph import _parse_effect
        result = _parse_effect("writes($OBJ)")
        assert result == ("writes", "OBJ")

    def test_parse_reads(self):
        from emend.fact_graph import _parse_effect
        result = _parse_effect("reads($VAR)")
        assert result == ("reads", "VAR")

    def test_parse_invalid(self):
        from emend.fact_graph import _parse_effect
        assert _parse_effect("unknown($X)") is None
        assert _parse_effect("writes(no_dollar)") is None
        assert _parse_effect("") is None


# ---------------------------------------------------------------------------
# compile_sequence_rule() — higher-level API tests
# ---------------------------------------------------------------------------


class TestCompileSequenceRule:
    """Test the public ``compile_sequence_rule(graph, check)`` API.

    ``compile_sequence_rule`` returns ``(cozoscript_query, step_data) | None``.
    Unlike ``_compile_sequence_query`` (which takes pre-resolved step locations),
    ``compile_sequence_rule`` resolves step locations from the graph's source files
    or from pre-populated facts, then delegates to ``_compile_sequence_query``.
    """

    def test_import_available(self):
        """compile_sequence_rule is importable from fact_graph."""
        from emend.fact_graph import compile_sequence_rule  # noqa: F401

    def test_returns_none_for_empty_graph(self):
        """Empty graph with no facts → no matches → returns None."""
        from emend.fact_graph import compile_sequence_rule

        g = FactGraph()
        check = SequenceCheck(
            name="test",
            message="test",
            sequence=[
                SequenceStep(bind="a", pattern="never_matches()"),
                SequenceStep(bind="b", pattern="also_never()"),
            ],
        )
        result = compile_sequence_rule(g, check)
        assert result is None

    def test_returns_query_and_step_data_tuple(self, tmp_path):
        """When step patterns resolve against real source, returns a
        ``(query_str, step_data)`` tuple whose query runs against the graph."""
        from emend.fact_graph import compile_sequence_rule

        (tmp_path / "svc.py").write_text(
            "def handler():\n"
            "    fd = open_conn()\n"
            "    fd.close()\n"
            "    fd.read()\n"
        )
        check = SequenceCheck(
            name="use-after-close",
            message="Use after close",
            sequence=[
                SequenceStep(bind="close", pattern="$FD.close()"),
                SequenceStep(bind="use", pattern="$FD.read()"),
            ],
        )
        g = FactGraph.build_from_project(str(tmp_path))
        result = compile_sequence_rule(g, check, project_path=str(tmp_path))

        assert isinstance(result, tuple)
        assert len(result) == 2
        query, step_data = result
        assert isinstance(query, str)
        assert isinstance(step_data, dict)
        assert step_data["bindings"] == {"FD": "fd"}
        assert len(step_data["step_locations"]["close"]) == 1
        assert len(step_data["step_locations"]["use"]) == 1
        # The compiled query is runnable and finds the resolved sequence.
        run = g.run_query(query)
        assert "headers" in run
        assert run["rows"]


# ---------------------------------------------------------------------------
# _run_sequence_check() policy integration
# ---------------------------------------------------------------------------


class TestRunSequenceCheckIntegration:
    """Test ``_run_sequence_check()`` with tmp_path project files."""

    def test_no_violations_empty_project(self, tmp_path):
        """Empty project directory produces no violations."""
        policy = Policy(
            name="test",
            description="test",
            severity="error",
            checks=[
                SequenceCheck(
                    name="toctou",
                    message="TOCTOU",
                    sequence=[
                        SequenceStep(bind="load", pattern="$OBJ = session.query($MODEL)"),
                        SequenceStep(bind="mutate", effect="writes($OBJ)"),
                    ],
                )
            ],
        )
        violations = _run_sequence_check(policy.checks[0], policy, str(tmp_path))
        assert isinstance(violations, list)
        assert len(violations) == 0

    def test_violation_format(self, tmp_path):
        """When violations are found, they are PolicyViolation instances."""
        # Write a Python file with the TOCTOU pattern
        (tmp_path / "app.py").write_text(
            "def process(session, should_mutate):\n"
            "    obj = session.query(User)\n"
            "    if should_mutate:\n"
            "        obj.update()\n"
        )

        policy = Policy(
            name="toctou-check",
            description="TOCTOU check",
            severity="error",
            checks=[
                SequenceCheck(
                    name="toctou",
                    message="TOCTOU detected",
                    sequence=[
                        SequenceStep(bind="load", pattern="$OBJ = session.query($MODEL)"),
                        SequenceStep(bind="mutate", effect="writes($OBJ)"),
                    ],
                )
            ],
        )

        violations = _run_sequence_check(policy.checks[0], policy, str(tmp_path))
        assert violations, "expected the TOCTOU sequence to produce a violation"
        for v in violations:
            assert isinstance(v, PolicyViolation)
            assert v.policy_name == "toctou-check"
            assert "sequence:toctou" in v.check_name
            assert v.severity == "error"


# ---------------------------------------------------------------------------
# Bug: sequence step resolution drops steps inside `async def` methods
# ---------------------------------------------------------------------------


class TestSequenceRuleAsyncMethod:
    """``compile_sequence_rule`` must resolve steps inside ``async def``
    methods.  The enclosing-function lookup previously omitted the
    ``async_method`` symbol kind, so every step matched inside an async
    method of a class was silently discarded and the rule reported nothing.
    """

    _CHECK = SequenceCheck(
        name="use-after-close",
        message="Use after close",
        sequence=[
            SequenceStep(bind="close", pattern="$FD.close()"),
            SequenceStep(bind="use", pattern="$FD.read()"),
        ],
    )

    def _resolve(self, tmp_path, method_def: str):
        from emend.fact_graph import compile_sequence_rule

        src = (
            "class Service:\n"
            f"    {method_def}\n"
            "        fd = open_conn()\n"
            "        fd.close()\n"
            "        fd.read()\n"
        )
        (tmp_path / "svc.py").write_text(src)
        g = FactGraph.build_from_project(str(tmp_path))
        return compile_sequence_rule(g, self._CHECK, project_path=str(tmp_path))

    def test_async_method_steps_resolved(self, tmp_path):
        result = self._resolve(tmp_path, "async def handler(self):")
        assert result is not None, (
            "sequence steps inside an async method were not resolved — "
            "the async_method symbol kind is missing from the enclosing "
            "function lookup"
        )

    def test_sync_method_steps_resolved(self, tmp_path):
        # Control: the identical sequence in a plain method already resolves.
        result = self._resolve(tmp_path, "def handler(self):")
        assert result is not None
