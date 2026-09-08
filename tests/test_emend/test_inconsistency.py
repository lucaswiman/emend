"""Behavioral probes for the experimental near-clone comparison."""

import pytest

from emend.inconsistency import find_inconsistencies


@pytest.mark.parametrize("change", ["guard", "operator", "literal", "rename", "docstring", "indentation", "fstring", "comma"])
def test_near_clone_differences(tmp_path, change):
    original = '''def process(items):
    result = []
    for item in items:
        if not allowed(item):
            continue
        value = transform(item)
        if value > 10:
            result.append(value)
    save(result)
    return result
'''
    variants = {
        "guard": original.replace("        if not allowed(item):\n            continue\n", ""),
        "operator": original.replace("value > 10", "value < 10"),
        "literal": original.replace("value > 10", "value > 11"),
        "rename": original.replace("items", "records").replace("result", "output"),
        "docstring": original.replace("    result =", '    "Different documentation."\n    result ='),
        "indentation": original.replace("    save(result)", "        save(result)"),
        "fstring": original.replace("    result =", '    f"{audit()}"\n    result ='),
        "comma": original.replace("save(result)", "save(result,)"),
    }
    files = [tmp_path / "a.py", tmp_path / "b.py"]
    for path, content in zip(files, [original, variants[change]]):
        path.write_text(content)
    findings = find_inconsistencies(files)
    assert len(findings) == (0 if change in {"rename", "docstring", "comma"} else 1)
    if findings:
        finding = findings[0]
        assert finding["left"].endswith("a.py:1::process")
        assert finding["right"].endswith("b.py:1::process")
        assert finding["changes"] and finding["diff"]
        delta = [token for edit in finding["changes"] for side in ("left", "right") for token in edit[side]]
        assert {
            "guard": "allowed", "operator": "<", "literal": "11",
            "indentation": ("block", "end"), "fstring": "audit",
        }[change] in delta


def test_unrelated_and_trivial_functions_are_not_candidates(tmp_path):
    path = tmp_path / "functions.py"
    path.write_text('''def first(x):
    return x + 1
def second(x):
    return x + 2
def unrelated(resource):
    with resource.acquire() as connection:
        try:
            connection.execute("SELECT * FROM records")
        finally:
            connection.close()
''')
    assert find_inconsistencies([path]) == []
