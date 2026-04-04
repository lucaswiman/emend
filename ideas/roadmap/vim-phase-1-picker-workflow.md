# Phase 1: Picker Workflow

## Goal

Make the picker faster for repeated open and revisit workflows without changing
the unified search model.

## TODO

- [x] Add recent-query recall from inside the picker
- [x] Design the recent-query overlay interaction
- [x] Decide whether query history should be session-only or partially persisted
- [x] Expose result provenance in the picker header
- [x] Keep file-path hits visible when richer index-backed results are unavailable
- [x] Validate that the new actions do not conflict with existing keymaps

## Notes

- Query recall should not use arrow-key input history.
- A compact overlay opened by a dedicated hotkey is the preferred interaction.
- Query history is persisted per project in `.emend/.vimhistory`.
- Provenance should make it obvious whether results came from hot-buffer,
  indexed, files-only, or grep-style fallback sources.
