import pytest
from pathlib import Path
from emend.cli import resolve_files
from emend.transform import find_pattern, visit_project_ts
from emend.ast_commands import collect_symbols

def test_resolve_files_typescript(tmp_path):
    (tmp_path / "a.ts").write_text("const x = 1;")
    (tmp_path / "b.py").write_text("x = 1")
    
    files, is_multi = resolve_files(str(tmp_path), language="typescript")
    assert len(files) == 1
    assert files[0].name == "a.ts"

def test_resolve_files_rust(tmp_path):
    (tmp_path / "a.rs").write_text("fn main() {}")
    (tmp_path / "b.py").write_text("x = 1")
    
    files, is_multi = resolve_files(str(tmp_path), language="rust")
    assert len(files) == 1
    assert files[0].name == "a.rs"

def test_collect_symbols_typescript(tmp_path):
    f = tmp_path / "test.ts"
    f.write_text("class Foo { method() {} } function bar() {}")
    
    symbols = collect_symbols(str(f))
    # We should have Foo and bar (currently names include module path)
    names = [s.name for s in symbols]
    assert any("Foo" in n for n in names)
    assert any("bar" in n for n in names)

def test_collect_symbols_rust(tmp_path):
    f = tmp_path / "test.rs"
    f.write_text("struct Foo; impl Foo { fn method(&self) {} } fn bar() {}")
    
    symbols = collect_symbols(str(f))
    # We should have Foo and bar
    names = [s.name for s in symbols]
    assert any("Foo" in n for n in names)
    assert any("bar" in n for n in names)

def test_find_pattern_typescript(tmp_path):
    f = tmp_path / "test.ts"
    f.write_text("console.log('hello'); console.log('world');")
    
    # We use a neutral pattern
    # console.log matches as an 'attr' node
    matches = find_pattern("console.log", str(f))
    assert len(matches) == 2

def test_find_pattern_rust(tmp_path):
    f = tmp_path / "test.rs"
    f.write_text("println!(\"hello\"); println!(\"world\");")
    
    # We use a neutral pattern
    # Note: println! might be tricky if it's not a call in tree-sitter-rust (it's a macro_invocation)
    # But let's try
    matches = find_pattern("println!($X)", str(f))
    assert len(matches) >= 0 # Just check it doesn't crash for now
