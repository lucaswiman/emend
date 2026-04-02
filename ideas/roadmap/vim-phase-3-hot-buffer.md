# Phase 3: Hot Buffer Protocol

## Goal

Make local editor-facing operations reflect unsaved edits by introducing
in-memory buffer snapshots in the editor server.

## TODO

- [ ] Add `buffer_open` RPC
- [ ] Add `buffer_update` RPC
- [ ] Add `buffer_close` RPC
- [ ] Decide on snapshot identity: `(path, bufnr)` plus version or equivalent
- [ ] Start with full-text updates rather than incremental diffs
- [ ] Maintain in-memory hot-buffer snapshots in the editor server
- [ ] Teach `complete` to prefer hot-buffer text
- [ ] Teach `goto_definition` to prefer hot-buffer text
- [ ] Teach `types_at_cursor` or equivalent type query paths to prefer hot-buffer text
- [ ] Teach `file_symbols` to prefer hot-buffer text
- [ ] Keep whole-project indexed queries backed by the project index
- [ ] Avoid trying to maintain a fully hot whole-project fact graph

## Notes

- This is the main architectural change in the roadmap.
- The goal is editor-native behavior for current-file operations, not live
  whole-project reanalysis on every keystroke.
