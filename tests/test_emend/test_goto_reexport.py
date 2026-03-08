
import pytest
from pathlib import Path
from emend.editor_search import EditorSearchEngine, _resolve_selector_to_goto_item

def test_resolve_reexport(tmp_path):
    # 1. Create codes.py with a class definition
    codes_py = tmp_path / "codes.py"
    codes_py.write_text("class DiagnosisCodeEntry:\n    pass\n")
    
    # 2. Create coding_output.py that re-exports it
    coding_output_py = tmp_path / "coding_output.py"
    coding_output_py.write_text("from .codes import *\n")
    
    # 3. Create an index (optional but good for realism)
    # Actually _resolve_selector_to_goto_item reads the file directly.
    
    engine = EditorSearchEngine(str(tmp_path))
    try:
        # Resolve via coding_output.py
        selector = f"{coding_output_py}::DiagnosisCodeEntry"
        result = _resolve_selector_to_goto_item(engine, selector)
        
        assert result is not None
        # Should point to codes.py, not coding_output.py
        assert Path(result["file_path"]).name == "codes.py"
        assert result["line"] == 1
    finally:
        engine.close()

def test_resolve_explicit_reexport(tmp_path):
    # 1. Create codes.py with a class definition
    codes_py = tmp_path / "codes.py"
    codes_py.write_text("class DiagnosisCodeEntry:\n    pass\n")
    
    # 2. Create coding_output.py that re-exports it explicitly
    coding_output_py = tmp_path / "coding_output.py"
    coding_output_py.write_text("from .codes import DiagnosisCodeEntry\n")
    
    engine = EditorSearchEngine(str(tmp_path))
    try:
        selector = f"{coding_output_py}::DiagnosisCodeEntry"
        result = _resolve_selector_to_goto_item(engine, selector)
        
        assert result is not None
        assert Path(result["file_path"]).name == "codes.py"
        assert result["line"] == 1
    finally:
        engine.close()

def test_cli_resolve_reexport(tmp_path):
    from emend.cli import app
    from typer.testing import CliRunner
    runner = CliRunner()
    
    # 1. Create codes.py with a class definition
    codes_py = tmp_path / "codes.py"
    codes_py.write_text("class DiagnosisCodeEntry:\n    pass\n")
    
    # 2. Create coding_output.py that re-exports it
    coding_output_py = tmp_path / "coding_output.py"
    coding_output_py.write_text("from .codes import *\n")
    
    # Use absolute path for selector to bypass KB module resolution
    selector = f"{coding_output_py}::DiagnosisCodeEntry"
    
    result = runner.invoke(app, ["map", "resolve-file", selector, "--json"])
    assert result.exit_code == 0
    
    import json
    data = json.loads(result.output)
    assert Path(data["file"]).name == "codes.py"
    assert data["line"] == 1
