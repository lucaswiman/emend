# Phase 1: Picker Workflow

## Goal

Make the picker faster for repeated open and revisit workflows without changing
the unified search model.

## TODO

- [ ] Add recent-query recall from inside the picker
- [ ] Design the recent-query overlay interaction
- [ ] Decide whether query history should be session-only or partially persisted
- [ ] Expose result provenance in the picker header
- [ ] Keep file-path hits visible when richer index-backed results are unavailable
- [x] Validate that the new actions do not conflict with existing keymaps

## Notes

- Query recall should not use arrow-key input history.
- A compact overlay opened by a dedicated hotkey is the preferred interaction.
- Provenance should make it obvious whether results came from hot-buffer,
  indexed, files-only, or grep-style fallback sources.
