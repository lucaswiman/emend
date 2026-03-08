import pytest
import os
from pathlib import Path
from emend.knowledge import KnowledgeBase, ModuleMapping

def test_map_resolve_dotted_selector(tmp_path, emend_cmd_list, run_emend_cmd):
    # Setup: a project with a module mapping
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "module_a.py").write_text("class MySymbol: pass\n")
    
    # Add module mapping: map 'ext' to 'external_root'
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="ext",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # Use emend map resolve
    result = run_emend_cmd(["map", "resolve", "ext.module_a.MySymbol"])
    
    # Check output
    assert "module_a.py::MySymbol" in result.stdout
    assert str(external_root) in result.stdout

def test_map_resolve_file_command(tmp_path, emend_cmd_list, run_emend_cmd):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "module_b.py").write_text("class MySymbol:\n    pass\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="ext",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # Use emend map resolve-file
    result = run_emend_cmd(["map", "resolve-file", "ext.module_b.MySymbol"])
    
    assert "module_b.py" in result.stdout
    assert "Line: 1" in result.stdout

def test_map_resolve_snake_case_file(tmp_path, emend_cmd_list, run_emend_cmd):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "models").mkdir()
    # MyModel -> my_model.py
    (external_root / "models" / "my_model.py").write_text("class MyModel: pass\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="common",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # Resolve common.models.MyModel
    # Should resolve to models/my_model.py::MyModel (heuristic)
    result = run_emend_cmd(["map", "resolve", "common.models.MyModel"])
    
    assert "my_model.py::MyModel" in result.stdout

def test_map_resolve_deep_snake_case(tmp_path, emend_cmd_list, run_emend_cmd):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "db" / "models").mkdir(parents=True)
    (external_root / "db" / "models" / "my_model.py").write_text("class MyModel: pass\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="common",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    result = run_emend_cmd(["map", "resolve", "common.db.models.MyModel.MyModel"])
    assert "db/models/my_model.py::MyModel" in result.stdout

def test_map_resolve_init_reexport(tmp_path, emend_cmd_list, run_emend_cmd):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "models").mkdir(parents=True)
    # File name does NOT match symbol name (no snake_case match)
    (external_root / "models" / "the_source.py").write_text("class MySymbol: pass\n")
    (external_root / "models" / "__init__.py").write_text("from .the_source import MySymbol\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="common",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # common.models.MySymbol
    # Should follow re-export in models/__init__.py to the_source.py
    result = run_emend_cmd(["map", "resolve", "common.models.MySymbol"])
    assert "the_source.py::MySymbol" in result.stdout

def test_map_resolve_file_deep_reexport(tmp_path, emend_cmd_list, run_emend_cmd):
    """map resolve-file should follow re-exports in __init__.py at any depth."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()

    external_root = tmp_path / "external_root"
    (external_root / "sub1" / "sub2").mkdir(parents=True)
    # Widget is defined in sub1/sub2/widget_impl.py but re-exported via __init__.py
    (external_root / "sub1" / "sub2" / "widget_impl.py").write_text(
        "class Widget:\n    pass\n"
    )
    (external_root / "sub1" / "sub2" / "__init__.py").write_text(
        "from .widget_impl import Widget\n"
    )

    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="pkg",
        local_path=str(external_root)
    ))
    kb.close()

    os.chdir(str(proj))

    result = run_emend_cmd(["map", "resolve-file", "pkg.sub1.sub2.Widget"])
    assert result.returncode == 0, result.stderr
    assert "widget_impl.py" in result.stdout
    assert "Widget" in result.stdout or "Line:" in result.stdout


def test_map_resolve_directory(tmp_path, emend_cmd_list, run_emend_cmd):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "db").mkdir()
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="common",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    result = run_emend_cmd(["map", "resolve", "common.db"])
    assert "db" in result.stdout
    assert result.returncode == 0

def test_search_include_map(tmp_path, emend_cmd_list, run_emend_cmd):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "utils.py").write_text("def my_func():\n    print('hello')\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="ext",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    result = run_emend_cmd(["search", "--include-map", "ext.utils.my_func"])
    assert "def my_func():" in result.stdout
    assert "hello" in result.stdout
