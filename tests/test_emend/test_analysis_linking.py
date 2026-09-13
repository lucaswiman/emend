"""Contracts for pure project linking of cached local analysis facts."""

from pathlib import Path

import pytest

from emend.analysis_linking import ModuleCatalog, link_extracted_files
from emend.analysis_snapshot import ExtractedFile, FileRevision


def _file(tmp_path, *, language="python", module="pkg.mod", rows=None):
    suffix = {"python": "py", "typescript": "ts", "rust": "rs"}[language]
    revision = FileRevision.create(
        tmp_path, tmp_path / f"mod.{suffix}", "hash", language, module
    )
    return ExtractedFile(revision, rows=rows or {})


def _ref(
    lexical, *, local="", target="unresolved", binding="", kind="call",
    func="pkg.mod.run", block=2, caller="pkg.mod.run", line=4,
):
    return [
        "mod.py", lexical, local, target, binding, kind, line, 3, func, block,
        caller, 20,
    ]


def _import(
    binding, local, raw, imported="", *, language="python", module="pkg.mod",
    file_path="mod.py", star=False,
):
    return [
        file_path, binding, local, raw, imported, star, 0, 0, 100, 0, 1,
        language, module,
    ]


def test_linking_is_pure_and_preserves_unresolved_calls(tmp_path):
    original = {
        "local_refs": [_ref("missing")], "local_imports": [],
        "fg_refs": [], "calls": [],
    }
    extracted = _file(tmp_path, rows=original)

    linked = link_extracted_files([extracted], ModuleCatalog({"pkg.mod"}))[0]

    assert linked.rows["fg_refs"] == [
        ["missing", "mod.py", 4, 3, "call", "pkg.mod.run", 2]
    ]
    assert linked.rows["calls"] == [
        ["pkg.mod.run", "missing", "mod.py", 4, 3, "pkg.mod.run", 2]
    ]
    assert linked.rows["method_calls"] == []
    assert original["fg_refs"] == []
    assert linked is not extracted and linked.rows is not extracted.rows


@pytest.mark.parametrize(
    "language,module,file_path,raw,imported,lexical,catalog,target",
    [
        ("python", "pkg.sub.mod", "pkg/sub/mod.py", "..helpers", "run", "go", {"pkg.helpers"}, "pkg.helpers.run"),
        ("python", "pkg", "pkg/__init__.py", ".helpers", "run", "go", {"pkg.helpers"}, "pkg.helpers.run"),
        ("python", "mod", "mod.py", ".helpers", "run", "go", set(), "go"),
        ("python", "pkg.mod", "pkg/mod.py", "os.path", "", "os.path.join", set(), "os.path.join"),
        ("python", "pkg.mod", "pkg/mod.py", "pkg.sub", "", "p.go", set(), "pkg.sub.go"),
        ("typescript", "pkg/ui/mod", "pkg/ui/mod.ts", "../shared", "run", "go", {"pkg.shared"}, "pkg.shared.run"),
        ("typescript", "pkg/mod", "pkg/mod.ts", "./tools", "", "tools.run", {"pkg.tools"}, "pkg.tools.run"),
        ("typescript", "mod", "mod.ts", "../../tools", "run", "go", set(), "go"),
        ("typescript", "mod", "mod.ts", "tools", "run", "go", {"tools"}, "<external>.tools.run"),
        ("rust", "nested::deep", "nested/deep.rs", "crate::helpers", "run", "go", {"helpers"}, "helpers.run"),
        ("rust", "nested::deep", "nested/deep.rs", "super::helpers", "run", "go", {"nested.helpers"}, "nested.helpers.run"),
        ("rust", "nested::deep", "nested/deep.rs", "self::helpers", "run", "go", {"nested.deep.helpers"}, "nested.deep.helpers.run"),
    ],
)
def test_imports_link_from_original_name_and_raw_module(
    tmp_path, language, module, file_path, raw, imported, lexical, catalog, target,
):
    rows = {
        "local_refs": [_ref(lexical, target="import", binding="i1")],
        "local_imports": [_import(
            "i1", lexical.split(".", 1)[0], raw, imported, language=language,
            module=module, file_path=file_path,
        )],
    }
    linked = link_extracted_files(
        [_file(tmp_path, language=language, module=module, rows=rows)],
        ModuleCatalog(catalog),
    )[0]
    assert linked.rows["fg_refs"][0][0] == target
    assert linked.rows["calls"][0][1] == target


@pytest.mark.parametrize("specifier", ["react", "./react"])
def test_package_imports_do_not_reference_same_named_local_module(tmp_path, specifier, monkeypatch):
    from unittest.mock import Mock
    from emend import analysis_extraction, analysis_linking
    from emend.analysis_store import AnalysisStore

    (tmp_path / "react.ts").write_text("export function greet() {}\n")
    (tmp_path / "consumer.ts").write_text(
        f"import {{ greet }} from '{specifier}';\ngreet();\n"
    )
    store = AnalysisStore.open(tmp_path)
    graph = store.query_facts()
    callers = graph.callers_datalog("react.greet")
    assert bool(callers) == (specifier == "./react")
    if specifier == "react":
        assert graph.callers_datalog("<external>.react.greet")
    extract = Mock(wraps=analysis_extraction._extract_file_facts)
    monkeypatch.setattr(analysis_extraction, "_extract_file_facts", extract)
    monkeypatch.setattr(analysis_linking, "LINKER_VERSION", "next")
    relinked = store.query_facts()
    assert relinked.snapshot.snapshot_id != graph.snapshot.snapshot_id
    assert relinked.callers_datalog("react.greet") == callers
    extract.assert_not_called()


def test_exact_import_binding_prevents_file_global_alias_guess(tmp_path):
    rows = {
        "local_refs": [
            _ref("local", target="import", binding="first", line=3),
            _ref("local", target="import", binding="second", line=6),
        ],
        "local_imports": [
            _import("first", "local", "one", "work"),
            _import("second", "local", "two", "work"),
        ],
    }
    linked = link_extracted_files(
        [_file(tmp_path, rows=rows)], ModuleCatalog({"one", "two"})
    )[0]
    assert [row[0] for row in linked.rows["fg_refs"]] == ["one.work", "two.work"]


def test_linker_builds_block_and_lexical_method_projections(tmp_path):
    rows = {
        "fg_sym": [["pkg.mod.C._helper", "mod.py", "_helper", "method", 9, 10, "pkg.mod.C"]],
        "local_refs": [
            _ref("obj._helper", local="pkg.mod.C._helper", target="local", kind="read"),
            _ref("obj.run", local="pkg.mod.C.run", target="local", line=5),
            _ref("top.run", local="pkg.mod.top.run", target="local", func="", block=-1, caller="pkg.mod", line=8),
        ],
        "local_imports": [],
    }
    linked = link_extracted_files(
        [_file(tmp_path, rows=rows)], ModuleCatalog({"pkg.mod"})
    )[0]
    assert linked.rows["ref_by_block"] == [
        ["mod.py", "pkg.mod.run", 2, "pkg.mod.C._helper"],
        ["mod.py", "pkg.mod.run", 2, "pkg.mod.C.run"],
    ]
    assert linked.rows["noncall_private_member_refs"] == [
        ["mod.py", "pkg.mod.run", 2, "_helper"]
    ]
    assert linked.rows["module_level_refs"] == [["pkg.mod.top.run", "mod.py", 8]]
    assert linked.rows["method_calls"] == [
        ["mod.py", "pkg.mod.run", "obj", "run", 2, 4],
        ["mod.py", "<module>", "top", "run", 0, 7],
    ]


def _revision(tmp_path, relative_path, language):
    from emend.language_registry import language_config_snapshot
    from emend.project_config import module_name_for_file

    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    separator = {"python": ".", "typescript": "/", "rust": "::"}[language]
    module = module_name_for_file(
        path, tmp_path, language=language, module_separator=separator
    )
    return FileRevision.create(
        tmp_path, path, "hash", language, module,
        analysis_config=language_config_snapshot(language, tmp_path),
    )


def test_catalog_uses_actual_revision_language_and_module_identity(tmp_path):
    revisions = [
        _revision(tmp_path, "src/pkg/tools/index.ts", "typescript"),
        _revision(tmp_path, "src/pkg/index.py", "python"),
        _revision(tmp_path, "src/pkg/tools.py", "python"),
        _revision(tmp_path, "src/lib.rs", "rust"),
        _revision(tmp_path, "src/foo.rs", "rust"),
    ]
    catalog = ModuleCatalog.from_revisions(revisions)
    assert catalog.resolve("pkg.tools", "typescript") == "pkg.tools.index"
    assert catalog.resolve("pkg.tools", "python") == "pkg.tools"

    rows = {
        "local_refs": [
            _ref("root_func", target="import", binding="root"),
            _ref("run", target="import", binding="module", line=5),
            _ref("foo.run", target="import", binding="split", line=6),
        ],
        "local_imports": [
            _import("root", "root_func", "crate", "root_func", language="rust", module="foo"),
            _import("module", "run", "crate::foo", "run", language="rust", module="foo"),
            _import("split", "foo", "crate", "foo", language="rust", module="foo"),
        ],
    }
    linked = link_extracted_files(
        [_file(tmp_path, language="rust", module="foo", rows=rows)], catalog
    )[0]
    assert [row[0] for row in linked.rows["fg_refs"]] == [
        "lib.root_func", "foo.run", "foo.run",
    ]


@pytest.mark.parametrize(
    "language,caller_path,target_path,source,expected",
    [
        ("python", "src/pkg/mod.py", "src/pkg/helpers.py", "from .helpers import run as go\ndef f():\n    go()\n    missing()\n", "pkg.helpers.run"),
        ("typescript", "src/pkg/mod.ts", "src/pkg/helpers.ts", 'import { run as go } from "./helpers";\nfunction f() { go(); missing(); }\n', "pkg.helpers.run"),
        ("typescript", "src/mod.ts", "src/index.ts", 'import { run as go } from "./index";\nfunction f() { go(); missing(); }\n', "index.run"),
        ("typescript", "src/pkg/mod.ts", "src/pkg/helpers.ts", 'import * as helpers from "./helpers";\nfunction f() { helpers.run(); missing(); }\n', "pkg.helpers.run"),
        ("rust", "src/main.rs", "src/helpers.rs", "use crate::helpers::run as go;\nfn f() { go(); missing(); }\n", "helpers.run"),
        ("rust", "src/main.rs", "src/helpers.rs", "use crate::helpers;\nfn f() { helpers::run(); missing(); }\n", "helpers.run"),
    ],
)
def test_native_local_facts_link_end_to_end(
    tmp_path, language, caller_path, target_path, source, expected,
):
    from emend.analysis_extraction import _extract_file_facts

    if language == "python":
        package_init = tmp_path / "src/pkg/__init__.py"
        package_init.parent.mkdir(parents=True)
        package_init.touch()
    target = _revision(tmp_path, target_path, language)
    caller = _revision(tmp_path, caller_path, language)
    extracted = _extract_file_facts(
        caller, str(Path(caller.file_path).relative_to(tmp_path)), source
    )
    linked = link_extracted_files(
        [extracted], ModuleCatalog.from_revisions([caller, target])
    )[0]
    assert {expected, "missing"} <= {row[1] for row in linked.rows["calls"]}
