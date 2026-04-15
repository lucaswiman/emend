# Phase 10 — MCP Duplicate Analysis Tool

## Purpose

Expose duplicate findings to model clients through MCP in a bounded way that
matches the production CLI and does not leak the experimental surface area.

## Required production changes

1. Add one MCP tool (or one `analyze(mode=duplicates)` branch) with these
   parameters:

   - `scope`: project / path / selector
   - `mode`: `exact`, `sequence`, or `all`
   - `limit`
   - `min_lines`
   - `min_score`
   - `cross_file`

2. The MCP response must be bounded and explanation-oriented. For each finding:

   - kind
   - score
   - top 2-5 members
   - file + line range
   - short rationale
   - short representative snippet or normalized summary

3. Reuse the same backend query function as the CLI. MCP must not have its own
   duplicate-analysis implementation.

4. Add the tool to the appropriate MCP profile/tool registry but keep it
   read-only.

5. MCP explanations should match the production semantics:

   - variable names may be normalized
   - literal constants are preserved for exact duplicates
   - comments are ignored

## Deliberately out of scope

- Streaming the full duplicate corpus
- Exposing raw canonical token sequences by default
- Separate MCP-only filtering/scoring logic
- Cross-repo corpus access

## Tests

1. MCP tool returns duplicate findings on a synthetic repo.
2. MCP tool respects `limit`.
3. MCP tool returns empty results when only trivial duplicates exist.
4. CLI and MCP for the same input produce the same underlying findings.

## Checklist

- [ ] MCP duplicate-analysis tool exists
- [ ] CLI and MCP share one backend
- [ ] MCP response is bounded and stable
- [ ] MCP tests cover non-trivial + empty-result cases
