# Phase 4: Analysis-Aware Intelligence

## Goal

Use existing Cozo, CFG, and type information to improve ranking and quick
actions without harming typing latency.

## TODO

- [x] Add CFG-informed completion ranking for locals in scope
- [x] Avoid ranking obviously unavailable or uninitialized names too highly
- [x] Improve dotted-member completion using imports, bases, and cached facts
- [x] Add graph-aware quick actions from the picker
- [x] Keep expensive graph and trace work out of the keystroke path
- [x] Measure latency impact before enabling any ranking changes by default
- [x] Define which signals are safe for synchronous completion use

## Notes

- This phase depends on the earlier workflow and hot-buffer work.
- The main use of analysis here is bounded ranking and quick actions, not
  full-program reasoning in the completion loop.
