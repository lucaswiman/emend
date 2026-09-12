import pytest

from emend.component_selector import parse_extended_selector
from emend.transform.rename_move import move_module, move_symbol, rename_module


@pytest.mark.parametrize("operation", ["move", "rename"])
@pytest.mark.parametrize("apply", [False, True])
def test_module_destination_collision_preserves_project(tmp_path, operation, apply):
    source = tmp_path / "source.py"
    dest = tmp_path / "dest.py" if operation == "rename" else tmp_path / "pkg/source.py"
    dest.parent.mkdir(exist_ok=True)
    source.write_text("SOURCE = 1\n")
    dest.write_text("IRREPLACEABLE = 42\n")
    (tmp_path / "consumer.py").write_text("import source\n")
    before = {p: p.read_text() for p in tmp_path.rglob("*.py")}
    with pytest.raises(ValueError, match="exist"):
        if operation == "rename":
            rename_module(str(source), "dest", str(tmp_path), apply=apply)
        else:
            move_module(str(source), str(dest.parent), str(tmp_path), apply=apply)
    assert {p: p.read_text() for p in tmp_path.rglob("*.py")} == before


@pytest.mark.parametrize("consumer", [
    "from pkg.source import f; result = f()\n",
    "marker = 'é'; from pkg.source import f\nresult = f()\n",
    "from .source import f\nresult = f()\n",
    "from .source import f as alias\nresult = alias()\n",
    "import pkg.source\nresult = pkg.source.f()\n",
    "import pkg.source as module\nresult = module.f()\n",
    "import pkg.source as module\ndef call(dest):\n    return module.f() + dest\nresult = call(0)\n",
    "def call():\n    import pkg.source as module\n    return module.f()\nresult = call()\n",
    "if True: from pkg.source import f; result = f()\n",
])
def test_move_preserves_consumer_behavior(tmp_path, consumer):
    import subprocess
    import sys

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    source = pkg / "source.py"
    source.write_text("def f():\n    return 42\n")
    client = pkg / "consumer.py"
    client.write_text(consumer)
    selector = parse_extended_selector(f"{source}::f")
    before = {p: p.read_text() for p in pkg.glob("*.py")}
    dry = move_symbol(selector, str(pkg / "dest.py"), project_path=str(tmp_path))
    assert {p: p.read_text() for p in pkg.glob("*.py")} == before
    actual = move_symbol(selector, str(pkg / "dest.py"), project_path=str(tmp_path), apply=True)
    assert actual == dry
    result = subprocess.run(
        [sys.executable, "-c", "from pkg.consumer import result; assert result == 42"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    if "marker" in consumer:
        assert "marker = 'é'" in client.read_text()


def test_move_same_file_rejects_without_changes(tmp_path):
    source = tmp_path / "source.py"
    original = "def f():\n    return 42\n"
    source.write_text(original)
    with pytest.raises(ValueError, match="different"):
        move_symbol(parse_extended_selector(f"{source}::f"), str(source), apply=True)
    assert source.read_text() == original


@pytest.mark.parametrize("destination", [
    "from source import f\ndef caller():\n    return f()\n",
    "def f():\n    return 2\ndef caller():\n    return f()\n",
])
@pytest.mark.parametrize("apply", [False, True])
def test_move_destination_binding_conflicts_reject_before_writes(tmp_path, destination, apply):
    source, dest = tmp_path / "source.py", tmp_path / "dest.py"
    source.write_text("def f():\n    return 1\n")
    dest.write_text(destination)
    before = {p: p.read_text() for p in tmp_path.glob("*.py")}
    with pytest.raises(ValueError, match="Destination"):
        move_symbol(parse_extended_selector(f"{source}::f"), str(dest), apply=apply)
    assert {p: p.read_text() for p in tmp_path.glob("*.py")} == before


@pytest.mark.parametrize("apply", [False, True])
def test_move_module_rejects_file_as_parent_before_rewriting(tmp_path, apply):
    source, client, blocker = [tmp_path / name for name in ("source.py", "client.py", "blocked")]
    source.write_text("VALUE = 1\n")
    client.write_text("from source import VALUE\n")
    blocker.write_text("keep\n")
    before = {p: p.read_text() for p in (source, client, blocker)}
    with pytest.raises(ValueError, match="directory"):
        move_module(str(source), str(blocker), str(tmp_path), apply=apply)
    assert {p: p.read_text() for p in before} == before


def test_move_wildcard_consumer_rejects_before_publication(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def f():\n    return 42\n")
    (tmp_path / "consumer.py").write_text("from source import *\n")
    before = {p: p.read_text() for p in tmp_path.glob("*.py")}
    with pytest.raises(ValueError, match="wildcard"):
        move_symbol(parse_extended_selector(f"{source}::f"), str(tmp_path / "dest.py"), apply=True)
    assert {p: p.read_text() for p in tmp_path.glob("*.py")} == before


@pytest.mark.parametrize("container", ["class A:", "def A():"])
@pytest.mark.parametrize("apply", [False, True])
def test_move_rejects_empty_required_suite(tmp_path, container, apply):
    source, dest = tmp_path / "source.py", tmp_path / "dest.py"
    original = f"{container}\n    def f():\n        return 42\n"
    source.write_text(original)
    with pytest.raises(ValueError, match="syntax"):
        move_symbol(parse_extended_selector(f"{source}::A.f"), str(dest), dedent=True, apply=apply)
    assert source.read_text() == original
    assert not dest.exists()


@pytest.mark.parametrize("import_text, expression", [
    ("import pkg.sub.source", "pkg.sub.source.f()"),
    ("import pkg.sub.source as module", "module.f()"),
    ("import pkg.sub.source as pkg", "pkg.f()"),
])
def test_python_dotted_import_binds_root_or_explicit_alias(tmp_path, import_text, expression):
    from emend import emend_core

    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    path = str(tmp_path / "consumer.py")
    resolver.index_file(path, f"{import_text}\n{expression}\n")
    assert [qn for qn, *_, kind, _ann in resolver.references_in_file(path)
            if kind == "call"] == ["pkg.sub.source.f"]


@pytest.mark.parametrize("class_binding, class_target", [
    ("from other import f", "other.f"),
    ("def f():\n        return 0", "consumer.C.f"),
])
def test_class_namespace_is_not_a_method_closure(tmp_path, class_binding, class_target):
    from emend import emend_core

    resolver = emend_core.PyScopeResolver(str(tmp_path), "py")
    path = str(tmp_path / "consumer.py")
    resolver.index_file(path, (
        "from pkg.source import f\nclass C:\n"
        f"    {class_binding}\n    value = f()\n"
        "    def method(self):\n        return f()\n"
    ))
    assert [qn for qn, *_, kind, _ann in resolver.references_in_file(path)
            if kind == "call"] == [class_target, "pkg.source.f"]


@pytest.mark.parametrize("used", [False, True])
def test_nested_move_does_not_retarget_same_leaf_import(tmp_path, used):
    source = tmp_path / "source.py"
    source.write_text("def f():\n    return 1\ndef outer():\n    def f():\n        return 2\n    return " + ("f()" if used else "0") + "\n")
    client = tmp_path / "client.py"
    client.write_text("from source import f\nresult = f()\n")
    before = source.read_text()
    def move():
        return move_symbol(parse_extended_selector(f"{source}::outer.f"), str(tmp_path / "dest.py"), dedent=True, apply=True)
    if used:
        with pytest.raises(ValueError, match="shadow"):
            move()
        assert source.read_text() == before
    else:
        move()
    assert client.read_text() == "from source import f\nresult = f()\n"
    assert "from dest import f" not in source.read_text()  # Would shadow the module's original f.
