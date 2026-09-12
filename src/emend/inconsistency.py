"""Experimental near-clone differences; candidates for review, not bug verdicts.

Run with ``emend dupes src/ --near``. Shares the duplicate detector's
tree-sitter parsing and canonicalizer, with language-configured structural roles.
"""

from collections import Counter, defaultdict
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

from emend.duplicate import (
    _find_containing_symbol,
    _preparse_files,
    canonicalize_subtree,
)
from emend.language_registry import detect_language, load_config, registry_snapshot


def _is_docstring(node):
    if node.kind in {"concatenated_string", "parenthesized_expression"}:
        return all(_is_docstring(child) for child in node.named_children())
    return node.kind == "string" and node.text().lstrip("rRuU").startswith(("'", '"'))


def _with_blocks(node, leaves, config):
    """Retain indentation structure alongside the shared canonical tokens."""
    if node.kind in config["comment_nodes"]:
        return
    if node.child_count == 0 or node.kind == "string_content":
        yield next(leaves)
        return
    if node.kind in config["block_nodes"]:
        yield ("block", "start")
    children = [child for child in node.children() if child.kind not in config["comment_nodes"]]
    for index, child in enumerate(children):
        if node.kind in config["argument_nodes"] and child.kind == "," and index == len(children) - 2:
            next(leaves)  # A call's optional trailing comma has no behavior.
            continue
        yield from _with_blocks(child, leaves, config)
    if node.kind in config["block_nodes"]:
        yield ("block", "end")


def _function_nodes(node, kinds):
    if node.kind in kinds:
        yield node
    for child in node.named_children():
        yield from _function_nodes(child, kinds)


def _parsed_inputs(files):
    registry = registry_snapshot()
    groups = defaultdict(list)
    for path in sorted({str(Path(p).resolve()) for p in files}):
        language = detect_language(path, registry=registry)
        if language in {"python", "typescript", "rust"}:
            groups[language, Path(path).suffix.lstrip(".")].append(path)
    for (language, extension), paths in groups.items():
        document = load_config(language)
        config = dict(document["duplicates"],
            name_field=document["symbols"]["name_field"],
            function_nodes=document["cfg"]["function_nodes"],
            block_nodes=document["cfg"]["block_nodes"],
            class_nodes=[s["node"] for s in document["scoping"]["scope_creators"] if s["kind"] == "class"],
            label_fields={document["pattern_matching"]["attribute"]: document["pattern_matching"]["attr_field"]},
        )
        config["label_fields"].update(config.get("extra_label_fields", {}))
        _, parsed = _preparse_files(paths, None, extension=extension)
        for path, data in parsed.items():
            yield language, config, path, data


def find_inconsistencies(files, *, min_similarity=0.85, max_regions=2, max_changed_tokens=20):
    """Find function bodies differing in a small number of token regions.

    Five-token shingles retrieve pairs before sequence alignment. Very common
    shingles (>40 functions) are ignored to bound boilerplate candidate fanout.
    These are deliberately heuristic recall limits, not soundness guarantees.
    """
    functions = []
    for language, config, path, (content, tree, qn_at, def_loc, symbols) in _parsed_inputs(files):
        source_lines = content.splitlines(keepends=True)
        for node in _function_nodes(tree.root, config["function_nodes"]):
            body = node.child_by_field_name("body")
            if body is None:
                continue
            statements = ([s for s in body.named_children() if s.kind not in config["comment_nodes"]]
                          if body.kind in config["block_nodes"] else [body])
            # Documentation is not an executable difference. Only the leading
            # string expression is a docstring; preserve other string literals.
            if config.get("ignore_docstrings") and statements and statements[0].kind == "expression_statement":
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
                    config=config,
                )
                tokens.extend(_with_blocks(statement, iter(part), config))
            if len(tokens) < 32:
                continue
            line = node.start_point[0]
            symbol = _find_containing_symbol(line, symbols)
            if not symbol and (name := node.child_by_field_name(config["name_field"])) is not None:
                symbol = name.text()
            parent = node.parent()
            name_field = config.get("binding_name_fields", {}).get(parent.kind) if parent else None
            if name_field and (name := parent.child_by_field_name(name_field)) is not None:
                symbol = f"{symbol}.{name.text()}" if symbol else name.text()
            functions.append({
                "location": f"{path}:{line + 1}::{symbol}",
                "tokens": tuple(tokens),
                "source": source_lines[line:node.end_point[0] + 1],
                "path": path, "start": node.start_byte, "end": node.end_byte,
                "language": language,
            })

    postings = defaultdict(list)
    for index, function in enumerate(functions):
        tokens = function["tokens"]
        for shingle in set(zip(*(tokens[offset:] for offset in range(5)))):
            postings[function["language"], shingle].append(index)
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
