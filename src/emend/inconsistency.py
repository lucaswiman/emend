"""Experimental near-clone differences; candidates for review, not bug verdicts.

Run with ``emend dupes src/emend --near``. Python-only for
now, sharing the duplicate detector's tree-sitter parsing and canonicalizer.
"""

from collections import Counter, defaultdict
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

from emend.duplicate import (
    _find_containing_symbol,
    _iter_candidates,
    _preparse_files,
    canonicalize_subtree,
)
from emend.language_registry import detect_language


def _is_docstring(node):
    if node.kind in {"concatenated_string", "parenthesized_expression"}:
        return all(_is_docstring(child) for child in node.named_children())
    return node.kind == "string" and node.text().lstrip("rRuU").startswith(("'", '"'))


def _with_blocks(node, leaves):
    """Retain indentation structure alongside the shared canonical tokens."""
    if node.kind == "comment":
        return
    if node.child_count == 0 or node.kind == "string_content":
        yield next(leaves)
        return
    if node.kind == "block":
        yield ("block", "start")
    children = [child for child in node.children() if child.kind != "comment"]
    for index, child in enumerate(children):
        if node.kind == "argument_list" and child.kind == "," and index == len(children) - 2:
            next(leaves)  # A call's optional trailing comma has no behavior.
            continue
        yield from _with_blocks(child, leaves)
    if node.kind == "block":
        yield ("block", "end")


def find_inconsistencies(files, *, min_similarity=0.85, max_regions=2, max_changed_tokens=20):
    """Find function bodies differing in a small number of token regions.

    Five-token shingles retrieve pairs before sequence alignment. Very common
    shingles (>40 functions) are ignored to bound boilerplate candidate fanout.
    These are deliberately heuristic recall limits, not soundness guarantees.
    """
    _, parsed = _preparse_files(sorted({
        str(Path(p).resolve()) for p in files if detect_language(str(p)) == "python"
    }), None)
    functions = []
    for path, (content, tree, qn_at, def_loc, symbols) in parsed.items():
        source_lines = content.splitlines(keepends=True)
        for node in _iter_candidates(tree):
            if node.kind != "function_definition":
                continue
            body = node.child_by_field_name("body")
            if body is None:
                continue
            statements = [s for s in body.named_children() if s.kind != "comment"]
            # Documentation is not an executable difference. Only the leading
            # string expression is a docstring; preserve other string literals.
            if statements and statements[0].kind == "expression_statement":
                children = statements[0].named_children()
                if children and _is_docstring(children[0]):
                    statements.pop(0)
            tokens = []
            bindings = {}
            for statement in statements:
                _, part = canonicalize_subtree(
                    statement, qn_at, def_loc,
                    binding_scope=(node.start_point[0], node.end_point[0]),
                    bound_map=bindings,
                )
                tokens.extend(_with_blocks(statement, iter(part)))
            if len(tokens) < 32:
                continue
            line = node.start_point[0]
            functions.append({
                "location": f"{path}:{line + 1}::{_find_containing_symbol(line, symbols)}",
                "tokens": tuple(tokens),
                "source": source_lines[line:node.end_point[0] + 1],
                "path": path, "start": node.start_byte, "end": node.end_byte,
            })

    postings = defaultdict(list)
    for index, function in enumerate(functions):
        tokens = function["tokens"]
        for shingle in set(zip(*(tokens[offset:] for offset in range(5)))):
            postings[shingle].append(index)
    neighbors = defaultdict(Counter)
    for members in postings.values():
        if len(members) <= 40:
            for position, right in enumerate(members):
                neighbors[right].update(members[:position])

    findings = []
    for right, counts in neighbors.items():
        b = functions[right]
        for left, shared in counts.items():
            a = functions[left]
            if shared < 4 or a["tokens"] == b["tokens"]:
                continue
            # Nested functions share text with their enclosing function.
            if a["path"] == b["path"] and max(a["start"], b["start"]) < min(a["end"], b["end"]):
                continue
            matcher = SequenceMatcher(None, a["tokens"], b["tokens"], autojunk=False)
            if matcher.quick_ratio() < min_similarity or matcher.ratio() < min_similarity:
                continue
            changes = [
                {"kind": kind, "left": a["tokens"][i:j], "right": b["tokens"][k:l]}
                for kind, i, j, k, l in matcher.get_opcodes() if kind != "equal"
            ]
            if len(changes) > max_regions or sum(max(len(c["left"]), len(c["right"])) for c in changes) > max_changed_tokens:
                continue
            findings.append({
                "left": a["location"], "right": b["location"],
                "similarity": round(matcher.ratio(), 4), "changes": changes,
                "category": "addition/deletion" if any(c["kind"] != "replace" for c in changes) else "replacement",
                "diff": "".join(unified_diff(a["source"], b["source"], a["location"], b["location"])),
            })
    return sorted(findings, key=lambda f: (f["category"], -f["similarity"], f["left"], f["right"]))
