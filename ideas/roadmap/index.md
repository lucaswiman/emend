# Vim Improvements Roadmap

This roadmap breaks the Vim plugin improvements into phases with separate specs.
Each phase is scoped to a concrete set of user-visible wins and implementation
constraints.

## Phases

- [x] [Phase 1: Picker Workflow](/Users/lucaswiman/personal/emend/ideas/roadmap/vim-phase-1-picker-workflow.md)
- [x] [Phase 2: Local Navigation](/Users/lucaswiman/personal/emend/ideas/roadmap/vim-phase-2-local-navigation.md)
- [x] [Phase 3: Hot Buffer Protocol](/Users/lucaswiman/personal/emend/ideas/roadmap/vim-phase-3-hot-buffer.md)
- [x] [Phase 4: Analysis-Aware Intelligence](/Users/lucaswiman/personal/emend/ideas/roadmap/vim-phase-4-analysis-aware.md)
- [ ] [Deferred / Future](/Users/lucaswiman/personal/emend/ideas/roadmap/vim-deferred-future.md)

## Scope

- [ ] Keep unified search; do not add a dedicated file mode
- [ ] Preserve graceful degradation when the symbol index is unavailable
- [ ] Prioritize repeated navigation and common editor actions over feature count
- [ ] Use Cozo/CFG/trace for bounded ranking and quick actions, not keystroke-path analysis
