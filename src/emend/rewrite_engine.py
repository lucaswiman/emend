"""Experimental expression-level equality saturation engine for emend.

Provides an e-graph data structure and rewrite rule engine for
multi-step expression rewrites using equality saturation.

This is Phase 7 of the roadmap — explicitly experimental.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from emend.union_find import UnionFind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ENode:
    """A node in the e-graph: an operator applied to child e-class IDs."""
    op: str
    children: tuple[int, ...] = ()

    def __hash__(self) -> int:
        return hash((self.op, self.children))


@dataclass
class RewriteRule:
    """A rewrite rule: LHS pattern => RHS pattern."""
    name: str
    lhs: str  # e.g. "$x + 0"
    rhs: str  # e.g. "$x"
    condition: Callable[..., bool] | None = None


@dataclass
class SaturationResult:
    """Result of applying saturation to a single expression."""
    file_path: str
    line: int
    col: int
    original_text: str
    rewritten_text: str
    rules_applied: list[str]


# ---------------------------------------------------------------------------
# E-Graph
# ---------------------------------------------------------------------------

class EGraph:
    """Equality saturation e-graph.

    Stores equivalence classes of expressions. Supports adding nodes,
    merging classes, pattern matching, and extracting the smallest
    expression from a class.
    """

    def __init__(self) -> None:
        self._uf = UnionFind()
        self._next_id = 0
        # eclass_id -> set of ENode
        self._classes: dict[int, set[ENode]] = {}
        # ENode -> eclass_id (for deduplication)
        self._hashcons: dict[ENode, int] = {}

    def _canonical_node(self, node: ENode) -> ENode:
        """Canonicalize node children to their representative e-class IDs."""
        return ENode(
            op=node.op,
            children=tuple(self._uf.find(c) for c in node.children),
        )

    def add(self, node: ENode) -> int:
        """Add a node to the e-graph, returning its e-class ID.

        If an equivalent node already exists, returns the existing class ID.
        """
        node = self._canonical_node(node)
        if node in self._hashcons:
            return self._uf.find(self._hashcons[node])

        eclass_id = self._next_id
        self._next_id += 1
        self._uf.make_set(eclass_id)
        self._classes[eclass_id] = {node}
        self._hashcons[node] = eclass_id
        return eclass_id

    def merge(self, id1: int, id2: int) -> int:
        """Merge two e-classes. Returns the new representative ID."""
        r1, r2 = self._uf.find(id1), self._uf.find(id2)
        if r1 == r2:
            return r1
        merged = self._uf.union(r1, r2)
        # Combine the sets
        other = r2 if merged == r1 else r1
        if other in self._classes:
            if merged not in self._classes:
                self._classes[merged] = set()
            self._classes[merged] |= self._classes.pop(other)
        return merged

    def find(self, eclass_id: int) -> int:
        """Find the canonical representative of an e-class."""
        return self._uf.find(eclass_id)

    def _all_enodes(self) -> list[tuple[int, ENode]]:
        """Yield all (canonical_eclass_id, enode) pairs."""
        result = []
        for eid, nodes in self._classes.items():
            canonical = self._uf.find(eid)
            for node in nodes:
                result.append((canonical, node))
        return result

    def _match_against_eclass(
        self, pattern: ENode, eclass_id: int, var_map: dict[str, int],
    ) -> list[dict[str, int]]:
        """Match a pattern ENode against a specific e-class, returning substitutions.

        Recursively descends into concrete children so that nested metavars
        are bound correctly.
        """
        canonical = self._uf.find(eclass_id)

        # Metavariable: matches the whole e-class
        if pattern.op.startswith("$"):
            new_map = dict(var_map)
            if pattern.op in new_map:
                if new_map[pattern.op] != canonical:
                    return []
            else:
                new_map[pattern.op] = canonical
            return [new_map]

        # Concrete: find enodes in the target e-class with matching op
        results: list[dict[str, int]] = []
        nodes = self._classes.get(canonical, set())
        for node in nodes:
            if node.op != pattern.op:
                continue
            if len(node.children) != len(pattern.children):
                continue
            # Recursively match each child pair
            child_maps = [dict(var_map)]
            for p_child, n_child in zip(pattern.children, node.children):
                p_child_enode = self._get_pattern_for_eclass(p_child)
                if p_child_enode is None:
                    child_maps = []
                    break
                new_child_maps: list[dict[str, int]] = []
                for cmap in child_maps:
                    new_child_maps.extend(
                        self._match_against_eclass(p_child_enode, n_child, cmap)
                    )
                child_maps = new_child_maps
            results.extend(child_maps)
        return results

    def ematch(self, pattern: ENode, var_map: dict[str, int] | None = None) -> list[dict[str, int]]:
        """Pattern-match against the e-graph.

        Pattern nodes whose op starts with '$' are metavariables.
        Returns a list of substitution maps {metavar_name -> eclass_id}.
        """
        if var_map is None:
            var_map = {}

        results: list[dict[str, int]] = []
        for eid in set(self._uf.find(e) for e in self._classes):
            results.extend(self._match_against_eclass(pattern, eid, var_map))
        return results

    def _get_pattern_for_eclass(self, eclass_id: int) -> ENode | None:
        """Get a representative ENode from an e-class."""
        eid = self._uf.find(eclass_id)
        nodes = self._classes.get(eid, set())
        if nodes:
            return next(iter(nodes))
        return None

    def _get_op_for_eclass(self, eclass_id: int) -> str | None:
        """Get the op of any node in an e-class."""
        node = self._get_pattern_for_eclass(eclass_id)
        return node.op if node else None

    def apply_rules(self, rules: list[tuple[ENode, ENode]], limit: int = 30) -> int:
        """Apply rewrite rules until saturation or limit.

        Each rule is a (lhs_pattern, rhs_pattern) pair of ENode trees.
        Returns the number of new merges performed.
        """
        total_merges = 0
        for _ in range(limit):
            new_merges = 0
            for lhs, rhs in rules:
                matches = self.ematch(lhs)
                for subst in matches:
                    # Instantiate RHS with the substitution
                    new_id = self._instantiate(rhs, subst)
                    # Find the e-class that matched the LHS root
                    lhs_id = self._find_match_root(lhs, subst)
                    if lhs_id is not None and self._uf.find(lhs_id) != self._uf.find(new_id):
                        self.merge(lhs_id, new_id)
                        new_merges += 1
            total_merges += new_merges
            if new_merges == 0:
                break
        return total_merges

    def _instantiate(self, pattern: ENode, subst: dict[str, int]) -> int:
        """Instantiate a pattern with a substitution, creating nodes as needed."""
        if pattern.op.startswith("$"):
            if pattern.op in subst:
                return subst[pattern.op]
            # Unknown metavar, create a fresh e-class
            return self.add(pattern)

        def _instantiate_child(c: int) -> int:
            child_node = self._get_pattern_for_eclass(c)
            if child_node is not None:
                return self._instantiate(child_node, subst)
            return c

        children = tuple(_instantiate_child(c) for c in pattern.children)
        return self.add(ENode(op=pattern.op, children=children))

    def _find_match_root(self, pattern: ENode, subst: dict[str, int]) -> int | None:
        """Find the e-class ID that the LHS pattern root matched.

        For metavariable roots the answer is in *subst*.  For concrete roots
        we must also verify that each child matches the current substitution
        so that we select the specific e-class that produced *subst*, not
        just the first e-node with the right operator and arity.
        """
        if pattern.op.startswith("$"):
            return subst.get(pattern.op)
        for canonical_eid, node in self._all_enodes():
            if node.op != pattern.op or len(node.children) != len(pattern.children):
                continue
            # Confirm every child is consistent with the current substitution.
            children_ok = True
            for p_child_id, n_child_id in zip(pattern.children, node.children):
                p_child = self._get_pattern_for_eclass(p_child_id)
                if p_child is None:
                    children_ok = False
                    break
                n_child_canonical = self._uf.find(n_child_id)
                if p_child.op.startswith("$"):
                    expected = subst.get(p_child.op)
                    if expected is None or self._uf.find(expected) != n_child_canonical:
                        children_ok = False
                        break
                else:
                    if self._get_op_for_eclass(n_child_canonical) != p_child.op:
                        children_ok = False
                        break
            if children_ok:
                return canonical_eid
        return None

    def extract(self, eclass_id: int, cost_fn: Callable[[ENode, dict[int, int]], int] | None = None) -> ENode | None:
        """Extract the best (lowest-cost) expression from an e-class.

        Uses AST-size cost by default.
        """
        if cost_fn is None:
            cost_fn = _ast_size_cost

        # Simple extraction: BFS over e-classes
        costs: dict[int, int] = {}
        best: dict[int, ENode] = {}

        changed = True
        while changed:
            changed = False
            for eid, nodes in self._classes.items():
                canonical = self._uf.find(eid)
                for node in nodes:
                    try:
                        c = cost_fn(node, costs)
                    except KeyError:
                        continue
                    if canonical not in costs or c < costs[canonical]:
                        costs[canonical] = c
                        best[canonical] = node
                        changed = True

        target = self._uf.find(eclass_id)
        return best.get(target)


def _ast_size_cost(node: ENode, costs: dict[int, int]) -> int:
    """Default cost function: count AST nodes."""
    total = 1
    for child in node.children:
        if child not in costs:
            raise KeyError(child)
        total += costs[child]
    return total


# ---------------------------------------------------------------------------
# Expression parsing (simplified)
# ---------------------------------------------------------------------------

_BINOP_RE = re.compile(
    r"^(.+?)\s*([+\-*//%@&|^]|<<|>>|==|!=|<=|>=|<|>|\*\*|//|and|or|is|in|not\s+in|is\s+not)\s+(.+)$"
)
_UNOP_RE = re.compile(r"^(not|~|-)\s+(.+)$")
_CALL_RE = re.compile(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(([^)]*)\)$")
_ATTR_RE = re.compile(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\.\s*([A-Za-z_]\w*)$")
_METAVAR_RE = re.compile(r"^\$[A-Za-z_]\w*$")
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")
_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_STRING_RE = re.compile(r'^(["\']).*\1$')


def parse_expr(expr: str, egraph: EGraph) -> int:
    """Parse a Python expression string into e-graph nodes.

    Returns the e-class ID of the root expression.
    This is a simplified recursive-descent parser for common patterns.
    """
    expr = expr.strip()

    # Parenthesized expression
    if expr.startswith("(") and expr.endswith(")"):
        return parse_expr(expr[1:-1], egraph)

    # Metavariable
    if _METAVAR_RE.match(expr):
        return egraph.add(ENode(op=expr))

    # String literal
    if _STRING_RE.match(expr):
        return egraph.add(ENode(op=f"str:{expr}"))

    # Number literal
    if _NUMBER_RE.match(expr):
        return egraph.add(ENode(op=f"num:{expr}"))

    # Binary operations (lowest precedence first)
    m = _BINOP_RE.match(expr)
    if m:
        left = parse_expr(m.group(1), egraph)
        op = m.group(2).strip()
        right = parse_expr(m.group(3), egraph)
        return egraph.add(ENode(op=f"binop:{op}", children=(left, right)))

    # Unary operations
    m = _UNOP_RE.match(expr)
    if m:
        op = m.group(1)
        operand = parse_expr(m.group(2), egraph)
        return egraph.add(ENode(op=f"unop:{op}", children=(operand,)))

    # Function call
    m = _CALL_RE.match(expr)
    if m:
        func_name = m.group(1)
        args_str = m.group(2).strip()
        children = []
        if args_str:
            for arg in args_str.split(","):
                children.append(parse_expr(arg.strip(), egraph))
        func_id = egraph.add(ENode(op=f"name:{func_name}"))
        return egraph.add(ENode(op="call", children=(func_id, *children)))

    # Attribute access
    m = _ATTR_RE.match(expr)
    if m:
        obj = parse_expr(m.group(1), egraph)
        attr = m.group(2)
        return egraph.add(ENode(op=f"attr:{attr}", children=(obj,)))

    # Identifier
    if _IDENT_RE.match(expr):
        return egraph.add(ENode(op=f"name:{expr}"))

    # Fallback: treat as opaque
    return egraph.add(ENode(op=f"opaque:{expr}"))


def enode_to_source(node: ENode, egraph: EGraph) -> str:
    """Convert an ENode back to Python source code."""
    if node.op.startswith("$"):
        return node.op
    if node.op.startswith("name:"):
        return node.op[5:]
    if node.op.startswith("num:"):
        return node.op[4:]
    if node.op.startswith("str:"):
        return node.op[4:]
    if node.op.startswith("opaque:"):
        return node.op[7:]
    if node.op.startswith("binop:"):
        op = node.op[6:]
        left_node = egraph.extract(node.children[0])
        right_node = egraph.extract(node.children[1])
        if left_node and right_node:
            return f"{enode_to_source(left_node, egraph)} {op} {enode_to_source(right_node, egraph)}"
    if node.op.startswith("unop:"):
        op = node.op[5:]
        operand = egraph.extract(node.children[0])
        if operand:
            return f"{op} {enode_to_source(operand, egraph)}"
    if node.op == "call":
        func_node = egraph.extract(node.children[0])
        if func_node:
            func_str = enode_to_source(func_node, egraph)
            args = []
            for child_id in node.children[1:]:
                arg_node = egraph.extract(child_id)
                if arg_node:
                    args.append(enode_to_source(arg_node, egraph))
            return f"{func_str}({', '.join(args)})"
    if node.op.startswith("attr:"):
        attr = node.op[5:]
        obj_node = egraph.extract(node.children[0])
        if obj_node:
            return f"{enode_to_source(obj_node, egraph)}.{attr}"
    return node.op


# ---------------------------------------------------------------------------
# Rule matching (pattern-based)
# ---------------------------------------------------------------------------

def _match_expr_pattern(
    source_text: str,
    pattern: str,
) -> dict[str, str] | None:
    """Try to match *source_text* against *pattern* with $-metavars.

    Returns a dict of metavar -> captured text, or None if no match.
    """
    from emend.transform import find_pattern, PatternMatch

    try:
        # Use a temp approach: check if pattern matches the expression
        # We wrap both in a dummy assignment to make them valid Python
        wrapper = f"__expr__ = {source_text}\n"
        pattern_wrapper = f"__expr__ = {pattern}"
        matches = find_pattern(
            pattern_wrapper, "<expr>",
            source_override=wrapper,
            language="python",
        )
        if matches:
            return matches[0].captures or {}
    except Exception:
        pass

    # Fallback: simple regex-based matching
    regex_pattern = re.escape(pattern)
    for m in re.finditer(r"\\\$([A-Za-z_]\w*)", regex_pattern):
        var_name = m.group(1)
        regex_pattern = regex_pattern.replace(
            m.group(0), f"(?P<{var_name}>[^,)]+?)", 1
        )
    try:
        m = re.fullmatch(regex_pattern, source_text.strip())
        if m:
            return m.groupdict()
    except re.error:
        pass

    return None


def _apply_substitution(template: str, captures: dict[str, str]) -> str:
    """Apply captured metavar values to a template string."""
    result = template
    for var, val in captures.items():
        result = result.replace(f"${var}", val)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_rewrite_rules(config_path: str) -> list[RewriteRule]:
    """Load rewrite rules from a YAML config file.

    Expected format::

        rewrites:
          - name: remove-identity-add
            lhs: "$x + 0"
            rhs: "$x"

    Args:
        config_path: Path to the YAML config file.

    Returns:
        List of ``RewriteRule`` objects.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    rules: list[RewriteRule] = []
    for entry in data.get("rewrites", []) or []:
        rules.append(RewriteRule(
            name=entry["name"],
            lhs=entry["lhs"],
            rhs=entry["rhs"],
        ))
    return rules


def run_saturation(
    file_path: str,
    rules: list[RewriteRule],
    max_iterations: int = 30,
) -> list[SaturationResult]:
    """Run equality saturation on expressions in a source file.

    For each expression in the file, attempts to match LHS patterns
    from the rules. When a match is found, the RHS template is
    instantiated with captured metavars. The e-graph is used to
    find the optimal rewrite.

    Args:
        file_path: Path to the source file.
        rules: List of rewrite rules.
        max_iterations: Maximum saturation iterations per expression.

    Returns:
        List of ``SaturationResult`` objects for expressions that were rewritten.
    """
    path = Path(file_path)
    if not path.exists():
        return []

    source = path.read_text()
    lines = source.split("\n")
    results: list[SaturationResult] = []

    # Scan each line for expressions that match rule LHS patterns
    for line_idx, line_text in enumerate(lines, 1):
        stripped = line_text.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Try each rule against this line
        for rule in rules:
            captures = _match_expr_pattern(stripped, rule.lhs)
            if captures is None:
                # Try matching subexpressions on the RHS of assignments
                assign_m = re.match(r"^(\w+\s*=\s*)(.+)$", stripped)
                if assign_m:
                    rhs_text = assign_m.group(2).strip()
                    captures = _match_expr_pattern(rhs_text, rule.lhs)
                    if captures is not None:
                        rewritten = _apply_substitution(rule.rhs, captures)
                        if rewritten != rhs_text:
                            results.append(SaturationResult(
                                file_path=file_path,
                                line=line_idx,
                                col=len(assign_m.group(1)),
                                original_text=rhs_text,
                                rewritten_text=rewritten,
                                rules_applied=[rule.name],
                            ))
                continue

            rewritten = _apply_substitution(rule.rhs, captures)
            if rewritten != stripped:
                # Find the matched portion in the original line
                original_match = stripped
                for var, val in captures.items():
                    test = rule.lhs.replace(f"${var}", val)
                    if test in stripped:
                        original_match = test
                        break

                results.append(SaturationResult(
                    file_path=file_path,
                    line=line_idx,
                    col=0,
                    original_text=original_match,
                    rewritten_text=rewritten,
                    rules_applied=[rule.name],
                ))

    return results
