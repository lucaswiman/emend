"""The same duplicate contracts apply to every supported source grammar."""

import pytest
import sqlite3

from emend import duplicate
from emend.duplicate import query_duplicates
from emend.transform import _cache_db_dir, _compute_duplicate_payloads
from emend.transform.cache import _init_cache_schema


@pytest.mark.parametrize("extension,source", [
    ("py", "def compute(value):\n    first = value + 17\n    second = first * 23\n    third = second - 41\n    fourth = third / 7\n    return fourth\n"),
    ("rs", "fn compute(value: i32) -> i32 {\n    let first = value + 17;\n    let second = first * 23;\n    let third = second - 41;\n    let fourth = third / 7;\n    fourth\n}\n"),
    ("ts", "function compute(value: number) {\n    const first = value + 17;\n    const second = first * 23;\n    const third = second - 41;\n    const fourth = third / 7;\n    return fourth;\n}\n"),
    ("js", "const compute = value => {\n    const first = value + 17;\n    const second = first * 23;\n    const third = second - 41;\n    const fourth = third / 7;\n    return fourth;\n};\n"),
])
@pytest.mark.parametrize("mode", ["exact", "sequence"])
@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_language_contract(tmp_path, monkeypatch, extension, source, mode, nested):
    if nested:
        if extension == "py":
            source = "class Container:\n" + "".join("    " + line for line in source.splitlines(True))
        elif extension == "rs":
            source = "struct Container;\nimpl Container {\n" + source + "}\n"
        elif extension == "ts":
            source = "class Container {\n" + source.replace("function ", "") + "}\n"
            extension = "tsx"
        else:
            source = "function container() {\n" + source + "}\n"
            extension = "jsx"
    for name in ("one", "two"):
        (tmp_path / f"{name}.{extension}").write_text(
            source if name == "one" else source.replace("first", "renamed"))
    clusters = query_duplicates(str(tmp_path), mode=mode)
    assert clusters
    assert all({m.file.rsplit("/", 1)[-1] for m in c.members} ==
               {f"one.{extension}", f"two.{extension}"} for c in clusters)
    db = _cache_db_dir(str(tmp_path)) / "parse.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        _init_cache_schema(conn)
    _compute_duplicate_payloads(str(db), str(tmp_path), [
        (str(p), p.read_text()) for p in tmp_path.glob(f"*.{extension}")])
    with monkeypatch.context() as patch:
        def no_parse(*args, **kwargs):
            pytest.fail("A warm duplicate query must not parse again")
        patch.setattr(duplicate, "_preparse_files", no_parse)
        assert query_duplicates(str(tmp_path), mode=mode) == clusters
    (tmp_path / f"two.{extension}").write_text(source.replace("41", "79"))
    assert query_duplicates(str(tmp_path), mode=mode) == []


def test_duplicate_cache_keys_include_grammar_but_not_path():
    key = duplicate._duplicate_cache_key
    source = "const name = <T>value;"
    assert key("one/name.ts", source) == key("two/name.ts", source)
    assert key("name.ts", source) != key("name.tsx", source)


def test_duplicate_sequences_do_not_mix_languages():
    # Even identical canonical statement hashes cannot link different languages.
    sequences = [dict(file=f"file.{ext}", function_qn="compute", hashes=["a"] * 4,
                      line_ranges=[[n, n] for n in range(4)], kinds=["statement"] * 4)
                 for ext in ("py", "ts", "rs")]
    assert duplicate._sequence_clusters_from_seqs(sequences, 1, None) == []
