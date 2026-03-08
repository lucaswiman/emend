"""Tests for extended selector parsing."""
import pytest
from emend.component_selector import parse_extended_selector, ExtendedSelector


class TestExtendedSelectorParsing:
    def test_simple_symbol(self):
        sel = parse_extended_selector("file.py::func")
        assert sel.file_path == "file.py"
        assert sel.symbol_path == ["func"]
        assert sel.component is None

    def test_nested_symbol(self):
        sel = parse_extended_selector("file.py::Class.method")
        assert sel.symbol_path == ["Class", "method"]

    def test_with_component(self):
        sel = parse_extended_selector("file.py::func[params]")
        assert sel.component == "params"
        assert sel.accessor is None

    def test_with_named_accessor(self):
        sel = parse_extended_selector("file.py::func[params][ctx]")
        assert sel.component == "params"
        assert sel.accessor == "ctx"

    def test_with_index_accessor(self):
        sel = parse_extended_selector("file.py::func[decorators][0]")
        assert sel.component == "decorators"
        assert sel.accessor == 0

    def test_all_components(self):
        for comp in ["params", "returns", "decorators", "bases", "body"]:
            sel = parse_extended_selector(f"file.py::func[{comp}]")
            assert sel.component == comp

    def test_path_with_directories(self):
        sel = parse_extended_selector("src/module/file.py::func[params]")
        assert sel.file_path == "src/module/file.py"

    def test_with_pseudo_class_keyword_only(self):
        sel = parse_extended_selector("file.py::func[params]:KEYWORD_ONLY")
        assert sel.component == "params"
        assert sel.accessor is None
        assert sel.pseudo_class == "KEYWORD_ONLY"

    def test_with_pseudo_class_positional_only(self):
        sel = parse_extended_selector("file.py::func[params]:POSITIONAL_ONLY")
        assert sel.pseudo_class == "POSITIONAL_ONLY"

    def test_with_pseudo_class_positional_or_keyword(self):
        sel = parse_extended_selector("file.py::func[params]:POSITIONAL_OR_KEYWORD")
        assert sel.pseudo_class == "POSITIONAL_OR_KEYWORD"

    def test_without_pseudo_class(self):
        sel = parse_extended_selector("file.py::func[params]")
        assert sel.pseudo_class is None

    def test_with_accessor_and_pseudo_class(self):
        sel = parse_extended_selector("file.py::func[params][ctx]:KEYWORD_ONLY")
        assert sel.component == "params"
        assert sel.accessor == "ctx"
        assert sel.pseudo_class == "KEYWORD_ONLY"

    def test_with_accessor_and_pseudo_class_index(self):
        sel = parse_extended_selector("file.py::func[decorators][0]:KEYWORD_ONLY")
        assert sel.component == "decorators"
        assert sel.accessor == 0
        assert sel.pseudo_class == "KEYWORD_ONLY"

    def test_line_selector_unaffected(self):
        sel = parse_extended_selector("file.py:42")
        assert sel.line_start == 42
        assert sel.pseudo_class is None

    def test_line_range_unaffected(self):
        sel = parse_extended_selector("file.py:10-20")
        assert sel.line_start == 10
        assert sel.line_end == 20
        assert sel.pseudo_class is None

    def test_dotted_selector(self):
        sel = parse_extended_selector("path.to.file.SomeSymbol")
        assert sel.file_path == ""
        assert sel.symbol_path == ["path", "to", "file", "SomeSymbol"]

    def test_dotted_selector_with_component(self):
        sel = parse_extended_selector("path.to.file.SomeSymbol[params][0]")
        assert sel.file_path == ""
        assert sel.symbol_path == ["path", "to", "file", "SomeSymbol"]
        assert sel.component == "params"
        assert sel.accessor == 0
