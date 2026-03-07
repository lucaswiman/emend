"""Tests for the universal Tree-sitter-based pattern compiler."""
import pytest
from emend.language_plugins import TreeSitterPatternCompiler

def test_python_compile_call():
    compiler = TreeSitterPatternCompiler("python")
    ir = compiler.compile("print($X)")
    assert ir is not None
    assert ir["type"] == "call"
    assert ir["func"]["type"] == "name"
    assert ir["func"]["value"] == "print"
    assert ir["args"][0]["value"]["type"] == "metavar"
    assert ir["args"][0]["value"]["name"] == "X"

def test_python_compile_assign():
    compiler = TreeSitterPatternCompiler("python")
    ir = compiler.compile("x = $V")
    assert ir is not None
    assert ir["type"] == "assign"
    assert ir["target"]["value"] == "x"
    assert ir["value"]["type"] == "metavar"
    assert ir["value"]["name"] == "V"

def test_python_compile_import():
    compiler = TreeSitterPatternCompiler("python")
    ir = compiler.compile("import $M")
    assert ir is not None
    assert ir["type"] == "import"
    assert ir["names"][0]["name"] == "M"

def test_typescript_compile_call():
    compiler = TreeSitterPatternCompiler("typescript")
    ir = compiler.compile("console.log($X)")
    assert ir is not None
    assert ir["type"] == "call"
    assert ir["func"]["type"] == "attr"
    assert ir["func"]["attr"] == "log"
    assert ir["args"][0]["value"]["name"] == "X"

def test_anonymous_metavar():
    compiler = TreeSitterPatternCompiler("python")
    ir = compiler.compile("func($_)")
    assert ir is not None
    assert ir["args"][0]["value"]["type"] == "any_expr"

def test_ellipsis_capture():
    compiler = TreeSitterPatternCompiler("python")
    ir = compiler.compile("func($...ARGS)")
    assert ir is not None
    assert ir["args"][0]["type"] == "ellipsis"
    assert ir["args"][0]["name"] == "ARGS"
    assert ir["exact_args"] is False
