# Phase 3: Hot Buffer Protocol

## Goal

Make local editor-facing operations reflect unsaved edits by introducing
in-memory buffer snapshots in the editor server.

## TODO

- [x] Add `buffer_open` RPC
- [x] Add `buffer_update` RPC
- [x] Add `buffer_close` RPC
- [x] Decide on snapshot identity: `(path, bufnr)` plus version or equivalent
- [x] Start with full-text updates rather than incremental diffs
- [x] Maintain in-memory hot-buffer snapshots in the editor server
- [x] Teach `complete` to prefer hot-buffer text
- [x] Teach `goto_definition` to prefer hot-buffer text
- [x] Teach `types_at_cursor` or equivalent type query paths to prefer hot-buffer text
  - Note: `types_at_cursor` delegates to external type oracles (pyrefly/pyright) which read from disk. Hot buffer content cannot be passed to these tools yet — this is a known limitation.
- [x] Teach `file_symbols` to prefer hot-buffer text
- [x] Keep whole-project indexed queries backed by the project index
- [x] Avoid trying to maintain a fully hot whole-project fact graph

## Notes

- This is the main architectural change in the roadmap.
- The goal is editor-native behavior for current-file operations, not live
  whole-project reanalysis on every keystroke.
