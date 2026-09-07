"""Tests for extended selector parsing."""
import pytest
from emend.component_selector import parse_extended_selector, ExtendedSelector


@pytest.mark.parametrize("query, expected", [
    ("file.py::func", ExtendedSelector("file.py", ["func"])),
    ("file.py::Class.method", ExtendedSelector("file.py", ["Class", "method"])),
    ("src/module/file.py::func[params]",
     ExtendedSelector("src/module/file.py", ["func"], "params")),
    ("file.py:42", ExtendedSelector("file.py", [], line_start=42, line_end=42)),
    ("file.py:10-20", ExtendedSelector("file.py", [], line_start=10, line_end=20)),
    ("path.to.file.SomeSymbol", ExtendedSelector("", ["path", "to", "file", "SomeSymbol"])),
    ("path.to.file.SomeSymbol[params][0]",
     ExtendedSelector("", ["path", "to", "file", "SomeSymbol"], "params", 0)),
])
def test_selector_structure(query, expected):
    assert parse_extended_selector(query) == expected


@pytest.mark.parametrize("component", ["params", "returns", "decorators", "bases", "body"])
def test_components(component):
    assert parse_extended_selector(f"file.py::func[{component}]") == ExtendedSelector(
        "file.py", ["func"], component,
    )


@pytest.mark.parametrize("pseudo_class", [
    None, "KEYWORD_ONLY", "POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD",
])
@pytest.mark.parametrize("component, accessor", [("params", None), ("params", "ctx"), ("decorators", 0)])
def test_accessor_and_pseudo_class(component, accessor, pseudo_class):
    query = f"file.py::func[{component}]"
    if accessor is not None:
        query += f"[{accessor}]"
    if pseudo_class is not None:
        query += f":{pseudo_class}"
    assert parse_extended_selector(query) == ExtendedSelector(
        "file.py", ["func"], component, accessor, pseudo_class,
    )
