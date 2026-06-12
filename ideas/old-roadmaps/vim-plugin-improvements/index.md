# Vim Improvements Roadmap

This roadmap breaks the Vim plugin improvements into phases with separate specs.
Each phase is scoped to a concrete set of user-visible wins and implementation
constraints.

## Phases

- [x] [Phase 1: Picker Workflow](vim-phase-1-picker-workflow.md)
- [x] [Phase 2: Local Navigation](vim-phase-2-local-navigation.md)
- [x] [Phase 3: Hot Buffer Protocol](vim-phase-3-hot-buffer.md)
- [x] [Phase 4: Analysis-Aware Intelligence](vim-phase-4-analysis-aware.md)
- [ ] [Deferred / Future](vim-deferred-future.md)

## Scope

- [x] Keep unified search; do not add a dedicated file mode
- [x] Preserve graceful degradation when the symbol index is unavailable
- [x] Prioritize repeated navigation and common editor actions over feature count
- [x] Use Cozo/CFG/trace for bounded ranking and quick actions, not keystroke-path analysis
