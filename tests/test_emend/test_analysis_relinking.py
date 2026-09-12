"""Incremental project linking reuses local analysis across inventory edits."""
from collections import Counter
from pathlib import Path

from emend import analysis_extraction, analysis_linking
from emend.analysis_store import AnalysisStore


def test_module_inventory_relinks_callers_without_reextracting_them(tmp_path, monkeypatch):
    caller = tmp_path / "main.ts"
    caller.write_text('import {run} from "./dep"; export function entry() { return run(); }')
    target = tmp_path / "dep" / "index.ts"
    extracted, linked = Counter(), []
    extract = analysis_extraction._extract_file_facts
    link = analysis_linking.link_extracted_files

    def tracked_extract(revision, *args):
        extracted[Path(revision.file_path).name] += 1
        return extract(revision, *args)

    def tracked_link(files, catalog):
        files = list(files)
        linked.append({Path(file.revision.file_path).name for file in files})
        return link(files, catalog)

    monkeypatch.setattr(analysis_extraction, "_extract_file_facts", tracked_extract)
    monkeypatch.setattr(analysis_linking, "link_extracted_files", tracked_link)
    store = AnalysisStore(tmp_path)
    try:
        initial = store.query_facts()
        assert [call.callee_qn for call in initial.calls_from("main.entry")] == ["dep.run"]
        target.parent.mkdir()
        target.write_text("export function run() { return 1; }")
        added = store.query_facts()
        assert [call.callee_qn for call in added.calls_from("main.entry")] == ["dep.index.run"]
        assert linked[-1] == {"main.ts", "index.ts"}
        target.write_text("export function run() { return 200; }")
        store.query_facts()
        assert linked[-1] == {"index.ts"}
        target.unlink()
        removed = store.query_facts()
        assert [call.callee_qn for call in removed.calls_from("main.entry")] == ["dep.run"]
        assert extracted == {"main.ts": 1, "index.ts": 2}
        assert [call.callee_qn for call in initial.calls_from("main.entry")] == ["dep.run"]
    finally:
        store.close()
