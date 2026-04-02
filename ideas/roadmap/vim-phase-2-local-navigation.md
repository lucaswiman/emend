# Phase 2: Local Navigation

## Goal

Make current-file navigation feel instantaneous and support fast semantic
fan-out from the current selection.

## TODO

- [ ] Turn `EmendOutlineFilter` into a true local filter
- [ ] Fetch file symbols once and reuse them during local filtering
- [ ] Re-fetch only on explicit refresh or meaningful buffer change
- [ ] Add a hotkey-driven navigation interface for semantic next hops
- [ ] Define goto-symbol navigation from the current result
- [ ] Define callers navigation from the current result
- [ ] Define callees navigation from the current result
- [ ] Define sinks navigation from the current result when available
- [ ] Define impact navigation from the current result
- [ ] Ensure the interface feels like a small launcher, not a deep modal flow

## Notes

- This phase should improve current-buffer workflows even before hot-buffer RPC
  exists.
- The navigation interface should optimize for one-keystroke semantic traversal.
