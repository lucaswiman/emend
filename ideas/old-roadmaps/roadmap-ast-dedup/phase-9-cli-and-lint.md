# Phase 9 — CLI + Lint Surface

## Purpose

Expose duplicate analysis in the production CLI and lint flow with a small,
read-only surface tuned for actionable results rather than exhaustive output.

## Required production changes

1. Add one read-only CLI entry point under the analysis surface:

   - preferred shape: `emend analyze dupes`
   - acceptable alternative: `emend dupes`

2. Minimum CLI options:

   - `--mode exact|sequence|all` (default `all`)
   - `--file` / path scope
   - `--symbol` or selector scope
   - `--limit`
   - `--json`
   - `--min-lines`
   - `--min-score`
   - `--cross-file/--intra-file`

3. Output shape must include, per finding:

   - kind (`exact` or `sequence`)
   - score
   - size / statement count
   - file + line range for every member kept in the response
   - a short normalized explanation (`same canonical subtree`, `shared stmt run`)

4. Add conservative lint integration:

   - `lint` can opt into duplicate-code checks via config
   - only findings above a score/length threshold emit diagnostics
   - default excludes tests, generated files, and same-function duplicates

5. Lint diagnostics must point to a primary location plus one comparison
   location, not an unbounded set.

6. The production CLI/lint surface must describe duplicate findings in terms of
   the literal-preserving canonicalizer: variable names are alpha-renamed, but
   literal constants are part of the exact duplicate identity.

## Deliberately out of scope

- Auto-fix
- Refactoring suggestions that rewrite code automatically
- User-facing knobs for every experimental hash strategy
- Cross-repo analysis in the production CLI

## Tests

1. CLI returns one exact duplicate cluster on a synthetic repo with duplicated
   helper functions.
2. CLI returns one sibling-sequence finding when two functions share a
   repeated block but differ overall.
3. `--json` output round-trips and includes line ranges.
4. Lint emits nothing for trivial duplicated snippets below threshold.
5. Lint emits one warning for a hand-chosen non-trivial duplicate.

## Checklist

- [x] Production duplicate CLI command exists
- [x] JSON + text output are stable
- [x] Lint integration is config-gated and conservative
- [x] Tests cover exact + sequence duplicates
