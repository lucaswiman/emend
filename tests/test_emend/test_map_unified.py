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
    (external_root / "somesubmodule.py").write_text("class SomeSymbol: pass\n")
    
    # Add module mapping: map 'somemodule' to 'external_root'
    # This means somemodule.somesubmodule is external_root/somesubmodule.py
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="somemodule",
        local_path=str(external_root)
    ))
    kb.close()
    
    # From the app/ directory, resolve somemodule.somesubmodule.SomeSymbol
    # It should succeed and show the resolved file path.
    os.chdir(str(proj))
    
    # Use emend map resolve
    result = run_emend_cmd(["map", "resolve", "somemodule.somesubmodule.SomeSymbol"])
    
    # Check output
    assert "somesubmodule.py::SomeSymbol" in result.stdout
    assert str(external_root) in result.stdout

def test_search_include_map(tmp_path, emend_cmd_list, run_emend_cmd):
    # Setup: a project with a module mapping
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "somesubmodule.py").write_text("def target_func():\n    print('hello')\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="somemodule",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # Use emend search --include-map somemodule.somesubmodule.target_func
    result = run_emend_cmd(["search", "--include-map", "somemodule.somesubmodule.target_func"])
    
    # Check output: should contain the code from the external module
    assert "def target_func():" in result.stdout
    assert "hello" in result.stdout

def test_map_resolve_file_command(tmp_path, emend_cmd_list, run_emend_cmd):
    # Setup: same as above
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "somesubmodule.py").write_text("class SomeSymbol:\n    pass\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="somemodule",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # Use emend map resolve-file
    result = run_emend_cmd(["map", "resolve-file", "somemodule.somesubmodule.SomeSymbol"])
    
    # Check output: should contain path and line number
    assert "somesubmodule.py" in result.stdout
    assert "Line: 1" in result.stdout

def test_map_resolve_snake_case_file(tmp_path, emend_cmd_list, run_emend_cmd):
    # Setup: a project with a module mapping where the file name is snake_case
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    external_root.mkdir()
    (external_root / "models").mkdir()
    # MedicalCodingEncounter -> medical_coding_encounter.py
    (external_root / "models" / "medical_coding_encounter.py").write_text("class MedicalCodingEncounterModel: pass\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="common",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # Use emend map resolve
    # common.models.MedicalCodingEncounter.MedicalCodingEncounterModel
    # Should resolve to external_root/models/medical_coding_encounter.py::MedicalCodingEncounterModel
    result = run_emend_cmd(["map", "resolve", "common.models.MedicalCodingEncounter.MedicalCodingEncounterModel"])
    
    assert "medical_coding_encounter.py::MedicalCodingEncounterModel" in result.stdout
    assert str(external_root) in result.stdout

def test_map_resolve_deep_snake_case(tmp_path, emend_cmd_list, run_emend_cmd):
    # common.db.models.MedicalCodingEncounter.MedicalCodingEncounterModel
    # mapped common -> .../common/common
    # db is a directory
    # models is a directory
    # MedicalCodingEncounter is medical_coding_encounter.py
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    common_common = external_root / "common"
    common_common.mkdir(parents=True)
    (common_common / "db" / "models").mkdir(parents=True)
    (common_common / "db" / "models" / "medical_coding_encounter.py").write_text("class MedicalCodingEncounterModel: pass\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="common",
        local_path=str(common_common)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    result = run_emend_cmd(["map", "resolve", "common.db.models.MedicalCodingEncounter.MedicalCodingEncounterModel"])
    assert "common/db/models/medical_coding_encounter.py::MedicalCodingEncounterModel" in result.stdout

def test_map_resolve_deep_symbols(tmp_path, emend_cmd_list, run_emend_cmd):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").touch()
    
    external_root = tmp_path / "external_root"
    (external_root / "pkg").mkdir(parents=True)
    (external_root / "pkg" / "my_module.py").write_text("class MyClass:\n    def my_method(self): pass\n")
    
    kb = KnowledgeBase(str(proj))
    kb.add_module_mapping(ModuleMapping(
        module_prefix="ext",
        local_path=str(external_root)
    ))
    kb.close()
    
    os.chdir(str(proj))
    
    # ext.pkg.MyModule.MyClass.my_method
    # pkg is dir, MyModule is my_module.py
    # MyClass.my_method are symbols
    result = run_emend_cmd(["map", "resolve", "ext.pkg.MyModule.MyClass.my_method"])
    assert "pkg/my_module.py::MyClass.my_method" in result.stdout
