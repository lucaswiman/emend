"""Tests for emend.transform.dispatch module."""

import pytest
from emend.transform import cmd_lookup


class TestDispatchAnnotations:
    """Runtime-evaluation of module annotations (guards against missing imports)."""

    def test_single_fn_annotation_resolves(self):
        """`_dispatch_with_returns_filter`'s `single_fn` param is annotated
        `Callable[[ExtendedSelector], str]`. With `from __future__ import
        annotations` the annotation is stored as a string and only fails when
        evaluated. Evaluating it against the module namespace raises NameError
        if `Callable` was never imported into the module.
        """
        from emend.transform import dispatch

        ann = dispatch._dispatch_with_returns_filter.__annotations__["single_fn"]
        # Stored as a string due to `from __future__ import annotations`.
        assert isinstance(ann, str)
        # Evaluating against the module globals must not raise NameError.
        eval(ann, vars(dispatch))  # noqa: S307


class TestCmdLookupLineBasedMetadata:
    """Tests for cmd_lookup with line-based selectors and metadata=True."""

    def test_line_with_no_symbol_raises_value_error(self, tmp_path):
        """When a line-based selector points to an empty line (no symbol),
        cmd_lookup should raise ValueError, not SystemExit."""
        source = tmp_path / "example.py"
        source.write_text(
            "def foo():\n"
            "    pass\n"
            "\n"  # line 3: empty line, no symbol
            "def bar():\n"
            "    pass\n"
        )
        # Line 3 is an empty line with no symbol.
        selector_str = f"{source}:3"
        with pytest.raises(ValueError, match="No symbol found at line 3"):
            cmd_lookup(str(source), selector_str=selector_str, metadata=True)
