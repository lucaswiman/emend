"""Pattern parsing with metavariables."""
from __future__ import annotations
from dataclasses import dataclass
from lark import Lark, Transformer, Token
import importlib.resources
from typing import Union
import re as _re


@dataclass
class MetaVar:
    name: str
    ellipsis: bool = False
    type_constraint: str | None = None


@dataclass
class Pattern:
    raw: str
    metavars: list[MetaVar]


class PatternTransformer(Transformer):
    """Transform parse tree to Pattern with extracted metavars."""

    def __init__(self):
        super().__init__()
        self.metavars = []

    def start(self, items):
        return items[0]

    def pattern(self, items):
        # Items are code chunks and metavars - we just need to extract metavars
        return None

    def type_constraint(self, items):
        """Handle the type_constraint rule wrapping either terminal."""
        return str(items[0])

    def metavar(self, items):
        # Separate type constraint strings from other items
        type_constraint_str = None
        other_items = []

        for item in items:
            if isinstance(item, Token):
                if item.type in ("SIMPLE_TYPE_CONSTRAINT", "ORACLE_TYPE_CONSTRAINT"):
                    type_constraint_str = str(item)
                # Skip DOLLAR, UNDERSCORE, etc.
            elif isinstance(item, str) and item.startswith(":"):
                # Result of type_constraint rule
                type_constraint_str = item
            else:
                other_items.append(item)

        if not other_items:
            # This is $_ (anonymous metavar)
            metavar = MetaVar(name="_")
        else:
            # Check if first item is ellipsis marker
            if other_items[0] == "...":
                name = other_items[1]
                ellipsis = True
            else:
                name = other_items[0]
                ellipsis = False

            # Extract type constraint (remove leading ':')
            type_constraint = type_constraint_str[1:] if type_constraint_str else None

            metavar = MetaVar(
                name=name,
                ellipsis=ellipsis,
                type_constraint=type_constraint
            )

        self.metavars.append(metavar)
        return metavar

    def code_chunk(self, items):
        # Handle both regular code chunks and standalone colons
        # items can be empty if there's a standalone colon at the boundary
        if not items:
            return ""
        return str(items[0])

    def ELLIPSIS(self, token):
        return "..."

    def METAVAR_NAME(self, token):
        return str(token)

    def TYPE_NAME(self, token):
        return str(token)


# Load grammar from package
_grammar_text = importlib.resources.read_text("emend.grammars", "pattern.lark")
_parser = Lark(_grammar_text, parser="lalr")


def parse_pattern(pattern_str: str) -> Pattern:
    """Parse pattern with metavariables.

    Args:
        pattern_str: Pattern string with metavariables like "print($MSG)"

    Returns:
        Pattern object with raw string and extracted metavariables
    """
    tree = _parser.parse(pattern_str)
    transformer = PatternTransformer()
    transformer.transform(tree)

    return Pattern(
        raw=pattern_str,
        metavars=transformer.metavars
    )


def is_oracle_type_constraint(constraint: str | None) -> bool:
    """Check if a type constraint requires the TypeOracle (type[X] or returns[X])."""
    if constraint is None:
        return False
    return constraint.startswith("type[") or constraint.startswith("returns[")


def parse_oracle_type_constraint(constraint: str) -> tuple[str, str]:
    """Parse an oracle type constraint like 'type[Connection]' or 'returns[Optional[str]]'."""
    bracket_pos = constraint.index("[")
    kind = constraint[:bracket_pos]
    # Extract the inner type string, handling nested brackets
    inner = constraint[bracket_pos + 1:-1]
    return kind, inner



_COMPOUND_HEADER_RE = _re.compile(
    r"^\s*(?:if|elif|while|for|with|async\s+for|async\s+with)\s+.*:\s*$"
)

_DEF_HEADER_RE = _re.compile(
    r"(?:^|\n)\s*(?:async\s+)?(?:def|class)\s+\w.*:\s*$", _re.DOTALL
)

_EXCEPT_HEADER_RE = _re.compile(
    r"^\s*except\b.*:\s*$"
)


def _is_compound_statement_header(code: str) -> bool:
    """Check if code looks like a compound statement header (ends with ':')."""
    if _COMPOUND_HEADER_RE.match(code):
        return True
    if _DEF_HEADER_RE.search(code) and code.rstrip().endswith(":"):
        return True
    return False


def _is_except_header(code: str) -> bool:
    """Check if code is an except clause header."""
    return bool(_EXCEPT_HEADER_RE.match(code))



# ---------------------------------------------------------------------------
# Rust IR compiler (for tree-sitter fast path)
# ---------------------------------------------------------------------------

def _build_metavar_map_and_replace(pattern: Pattern) -> tuple[str, dict[str, MetaVar]]:
    """Shared step 1 of pattern compilation: replace metavars with placeholders.

    Returns (temp_code, metavar_map) where metavar_map maps placeholder names
    to MetaVar objects.
    """
    temp_code = pattern.raw
    metavar_map: dict[str, MetaVar] = {}

    sorted_metavars = sorted(pattern.metavars, key=lambda mv: (
        -len(f"$...{mv.name}:{mv.type_constraint or ''}"),
        -len(f"$...{mv.name}"),
        -len(f"${mv.name}:{mv.type_constraint or ''}"),
        -len(f"${mv.name}")
    ))

    for mv in sorted_metavars:
        placeholder = f"__META_{mv.name}__"
        metavar_map[placeholder] = mv

        if mv.ellipsis and mv.type_constraint:
            pattern_str = f"$...{mv.name}:{mv.type_constraint}"
        elif mv.ellipsis:
            pattern_str = f"$...{mv.name}"
        elif mv.type_constraint:
            pattern_str = f"${mv.name}:{mv.type_constraint}"
        else:
            pattern_str = f"${mv.name}"

        temp_code = temp_code.replace(pattern_str, placeholder)

    # Fix ellipsis metavars in dict context by appending `: None`
    for mv in sorted_metavars:
        if not mv.ellipsis:
            continue
        placeholder = f"__META_{mv.name}__"
        idx = temp_code.find(placeholder)
        if idx == -1:
            continue

        after_placeholder = temp_code[idx + len(placeholder):].lstrip()
        if after_placeholder.startswith(':'):
            continue

        brace_depth = 0
        for i in range(idx - 1, -1, -1):
            c = temp_code[i]
            if c == '}':
                brace_depth += 1
            elif c == '{':
                if brace_depth == 0:
                    found_colon = False
                    inner_depth = 0
                    for j in range(i + 1, len(temp_code)):
                        cj = temp_code[j]
                        if cj in '{[(':
                            inner_depth += 1
                        elif cj in '}])':
                            if inner_depth == 0:
                                break
                            inner_depth -= 1
                        elif cj == ':' and inner_depth == 0:
                            found_colon = True
                            break

                    if found_colon:
                        temp_code = (
                            temp_code[:idx + len(placeholder)]
                            + ': None'
                            + temp_code[idx + len(placeholder):]
                        )
                    break
                brace_depth -= 1

    # Replace literal `...` in dict context with `**__EMEND_SPREAD__`
    temp_code = _re.sub(
        r'\.\.\.\s*}',
        '**__EMEND_SPREAD__}',
        temp_code
    )

    return temp_code, metavar_map


def _ast_to_rust_ir(node, metavar_map: dict[str, MetaVar]) -> dict | None:
    """Convert a stdlib ast node to a Rust IR dict."""
    import ast as _ast

    if isinstance(node, _ast.Name):
        if node.id in metavar_map:
            metavar = metavar_map[node.id]
            tc = metavar.type_constraint
            if tc in ("int", "str", "call", "float",
                      "identifier", "attr", "stmt"):
                return {"type": "type_constraint", "kind": tc, "name": metavar.name}
            if tc is not None and tc.startswith("!"):
                inner = tc[1:]
                if inner in ("int", "str", "call", "float",
                             "identifier", "attr", "stmt"):
                    return {"type": "type_constraint", "kind": tc, "name": metavar.name}
            if tc is not None:
                # Oracle constraints (e.g., :type[X], :returns[X]) are checked
                # post-match. Treat the metavar as a regular capture.
                if is_oracle_type_constraint(tc):
                    return {"type": "metavar", "name": metavar.name}
                return None
            if metavar.ellipsis:
                return {"type": "ellipsis", "name": metavar.name}
            else:
                return {"type": "metavar", "name": metavar.name}
        else:
            if node.id == "None":
                return {"type": "none"}
            if node.id == "True":
                return {"type": "bool", "value": True}
            if node.id == "False":
                return {"type": "bool", "value": False}
            return {"type": "name", "value": node.id}

    elif isinstance(node, _ast.Constant):
        if node.value is None:
            return {"type": "none"}
        if isinstance(node.value, bool):
            return {"type": "bool", "value": node.value}
        if isinstance(node.value, int):
            return {"type": "integer", "value": str(node.value)}
        if isinstance(node.value, float):
            return {"type": "float", "value": str(node.value)}
        if isinstance(node.value, str):
            return {"type": "string", "value": repr(node.value)}
        if node.value is ...:
            return {"type": "ellipsis_literal"}
        return None

    elif isinstance(node, _ast.Call):
        func_ir = _ast_to_rust_ir(node.func, metavar_map)
        if func_ir is None:
            return None
        args_ir = []
        has_ellipsis = False
        for arg in node.args:
            if isinstance(arg, _ast.Starred):
                inner_ir = _ast_to_rust_ir(arg.value, metavar_map)
                if inner_ir is None:
                    return None
                args_ir.append({"type": "star", "value": inner_ir})
                continue
            if isinstance(arg, _ast.Name) and arg.id in metavar_map:
                metavar = metavar_map[arg.id]
                if metavar.ellipsis:
                    args_ir.append({"type": "ellipsis", "name": metavar.name})
                    has_ellipsis = True
                    continue
            arg_ir = _ast_to_rust_ir(arg, metavar_map)
            if arg_ir is None:
                return None
            args_ir.append(arg_ir)
        for kw in node.keywords:
            if kw.arg is None:
                # **kwargs
                inner_ir = _ast_to_rust_ir(kw.value, metavar_map)
                if inner_ir is None:
                    return None
                args_ir.append({"type": "double_star", "value": inner_ir})
            else:
                val_ir = _ast_to_rust_ir(kw.value, metavar_map)
                if val_ir is None:
                    return None
                args_ir.append({"type": "keyword_arg", "key": kw.arg, "value": val_ir})
        return {
            "type": "call",
            "func": func_ir,
            "args": args_ir,
            "exact_args": not has_ellipsis,
        }

    elif isinstance(node, _ast.Attribute):
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        return {"type": "attr", "value": value_ir, "attr": node.attr}

    elif isinstance(node, _ast.List):
        if len(node.elts) == 0:
            return {"type": "empty_list"}
        elems_ir = []
        for elt in node.elts:
            e_ir = _ast_to_rust_ir(elt, metavar_map)
            if e_ir is None:
                return None
            elems_ir.append(e_ir)
        return {"type": "list", "elements": elems_ir}

    elif isinstance(node, _ast.Tuple):
        elems_ir = []
        for elt in node.elts:
            e_ir = _ast_to_rust_ir(elt, metavar_map)
            if e_ir is None:
                return None
            elems_ir.append(e_ir)
        return {"type": "tuple", "elements": elems_ir}

    elif isinstance(node, _ast.Set):
        elems_ir = []
        for elt in node.elts:
            e_ir = _ast_to_rust_ir(elt, metavar_map)
            if e_ir is None:
                return None
            elems_ir.append(e_ir)
        return {"type": "set", "elements": elems_ir}

    elif isinstance(node, _ast.Dict):
        elems_ir = []
        for k, v in zip(node.keys, node.values):
            if k is None:
                # **spread
                if isinstance(v, _ast.Name) and v.id in metavar_map:
                    metavar = metavar_map[v.id]
                    if metavar.ellipsis:
                        elems_ir.append({"type": "ellipsis", "name": metavar.name})
                        continue
                if isinstance(v, _ast.Name) and v.id == "__EMEND_SPREAD__":
                    elems_ir.append({"type": "ellipsis"})
                    continue
                v_ir = _ast_to_rust_ir(v, metavar_map)
                if v_ir is None:
                    return None
                elems_ir.append({"type": "spread", "value": v_ir})
            else:
                if isinstance(k, _ast.Name) and k.id in metavar_map:
                    metavar = metavar_map[k.id]
                    if metavar.ellipsis:
                        elems_ir.append({"type": "ellipsis", "name": metavar.name})
                        continue
                k_ir = _ast_to_rust_ir(k, metavar_map)
                v_ir = _ast_to_rust_ir(v, metavar_map)
                if k_ir is None or v_ir is None:
                    return None
                elems_ir.append({"type": "pair", "key": k_ir, "value": v_ir})
        return {"type": "dict", "elements": elems_ir}

    elif isinstance(node, _ast.Subscript):
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        if isinstance(node.slice, _ast.Tuple):
            slices_ir = []
            for elt in node.slice.elts:
                s_ir = _ast_to_rust_ir(elt, metavar_map)
                if s_ir is None:
                    return None
                slices_ir.append(s_ir)
        else:
            s_ir = _ast_to_rust_ir(node.slice, metavar_map)
            if s_ir is None:
                return None
            slices_ir = [s_ir]
        return {"type": "subscript", "value": value_ir, "slices": slices_ir}

    elif isinstance(node, _ast.BinOp):
        left_ir = _ast_to_rust_ir(node.left, metavar_map)
        if left_ir is None:
            return None
        right_ir = _ast_to_rust_ir(node.right, metavar_map)
        if right_ir is None:
            return None
        op_map = {
            _ast.Add: "+", _ast.Sub: "-", _ast.Mult: "*",
            _ast.Div: "/", _ast.FloorDiv: "//", _ast.Mod: "%",
            _ast.Pow: "**", _ast.BitAnd: "&", _ast.BitOr: "|",
            _ast.BitXor: "^", _ast.LShift: "<<", _ast.RShift: ">>",
            _ast.MatMult: "@",
        }
        op_str = op_map.get(type(node.op))
        if op_str is None:
            return None
        return {"type": "binary_op", "left": left_ir, "op": op_str, "right": right_ir}

    elif isinstance(node, _ast.BoolOp):
        op_map = {_ast.And: "and", _ast.Or: "or"}
        op_str = op_map.get(type(node.op))
        if op_str is None:
            return None
        # BoolOp has a list of values; build nested binary ops
        result = _ast_to_rust_ir(node.values[0], metavar_map)
        if result is None:
            return None
        for val in node.values[1:]:
            right = _ast_to_rust_ir(val, metavar_map)
            if right is None:
                return None
            result = {"type": "binary_op", "left": result, "op": op_str, "right": right}
        return result

    elif isinstance(node, _ast.Compare):
        left_ir = _ast_to_rust_ir(node.left, metavar_map)
        if left_ir is None:
            return None
        comp_op_map = {
            _ast.Eq: "==", _ast.NotEq: "!=",
            _ast.Lt: "<", _ast.Gt: ">",
            _ast.LtE: "<=", _ast.GtE: ">=",
            _ast.Is: "is", _ast.IsNot: "is not",
            _ast.In: "in", _ast.NotIn: "not in",
        }
        ops_ir = []
        for op, comparator in zip(node.ops, node.comparators):
            op_str = comp_op_map.get(type(op))
            if op_str is None:
                return None
            comp_ir = _ast_to_rust_ir(comparator, metavar_map)
            if comp_ir is None:
                return None
            ops_ir.append({"op": op_str, "comparator": comp_ir})
        return {"type": "compare", "left": left_ir, "ops": ops_ir}

    elif isinstance(node, _ast.UnaryOp):
        op_map = {
            _ast.USub: "-", _ast.UAdd: "+", _ast.Invert: "~", _ast.Not: "not",
        }
        op_str = op_map.get(type(node.op))
        if op_str is None:
            return None
        operand_ir = _ast_to_rust_ir(node.operand, metavar_map)
        if operand_ir is None:
            return None
        return {"type": "unary_op", "op": op_str, "operand": operand_ir}

    elif isinstance(node, _ast.Assign):
        if len(node.targets) != 1:
            return None
        target_ir = _ast_to_rust_ir(node.targets[0], metavar_map)
        if target_ir is None:
            return None
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        return {"type": "assign", "target": target_ir, "value": value_ir}

    elif isinstance(node, _ast.AugAssign):
        target_ir = _ast_to_rust_ir(node.target, metavar_map)
        if target_ir is None:
            return None
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        op_map = {
            _ast.Add: "+=", _ast.Sub: "-=", _ast.Mult: "*=",
            _ast.Div: "/=", _ast.FloorDiv: "//=", _ast.Mod: "%=",
            _ast.Pow: "**=", _ast.BitAnd: "&=", _ast.BitOr: "|=",
            _ast.BitXor: "^=", _ast.LShift: "<<=", _ast.RShift: ">>=",
            _ast.MatMult: "@=",
        }
        op_str = op_map.get(type(node.op))
        if op_str is None:
            return None
        return {"type": "aug_assign", "target": target_ir, "op": op_str, "value": value_ir}

    elif isinstance(node, _ast.AnnAssign):
        target_ir = _ast_to_rust_ir(node.target, metavar_map)
        if target_ir is None:
            return None
        annotation_ir = _ast_to_rust_ir(node.annotation, metavar_map)
        if annotation_ir is None:
            return None
        value_ir = None
        if node.value:
            value_ir = _ast_to_rust_ir(node.value, metavar_map)
            if value_ir is None:
                return None
        return {
            "type": "ann_assign",
            "target": target_ir,
            "annotation": annotation_ir,
            "value": value_ir
        }

    elif isinstance(node, _ast.FunctionDef) or isinstance(node, _ast.AsyncFunctionDef):
        name = node.name
        if name in metavar_map:
            name_ir = {"type": "metavar", "name": metavar_map[name].name}
        else:
            name_ir = {"type": "name", "value": name}
        decorators_ir = []
        for dec in node.decorator_list:
            dec_ir = _ast_to_rust_ir(dec, metavar_map)
            if dec_ir is None:
                return None
            decorators_ir.append(dec_ir)
        param_patterns = []
        args = node.args
        # positional-only params
        for p in args.posonlyargs:
            if p.arg in metavar_map:
                metavar = metavar_map[p.arg]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "name", "value": p.arg})
        # regular params
        n_defaults = len(args.defaults)
        n_args = len(args.args)
        for i, p in enumerate(args.args):
            default_idx = i - (n_args - n_defaults)
            has_default = default_idx >= 0
            if p.arg in metavar_map:
                metavar = metavar_map[p.arg]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                    continue
                if has_default:
                    dv_ir = _ast_to_rust_ir(args.defaults[default_idx], metavar_map)
                    if dv_ir is None:
                        return None
                    param_patterns.append({"type": "with_default", "default_value": dv_ir})
                else:
                    param_patterns.append({"type": "any"})
            else:
                if has_default:
                    dv_ir = _ast_to_rust_ir(args.defaults[default_idx], metavar_map)
                    if dv_ir is None:
                        return None
                    param_patterns.append({"type": "with_default", "default_value": dv_ir})
                else:
                    param_patterns.append({"type": "name", "value": p.arg})
        # *args
        if args.vararg:
            p = args.vararg
            if p.arg in metavar_map:
                metavar = metavar_map[p.arg]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "any"})
        # keyword-only params
        for i, p in enumerate(args.kwonlyargs):
            if p.arg in metavar_map:
                metavar = metavar_map[p.arg]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                default = args.kw_defaults[i]
                if default is not None:
                    dv_ir = _ast_to_rust_ir(default, metavar_map)
                    if dv_ir is None:
                        return None
                    param_patterns.append({"type": "with_default", "default_value": dv_ir})
                else:
                    param_patterns.append({"type": "name", "value": p.arg})
        # **kwargs
        if args.kwarg:
            p = args.kwarg
            if p.arg in metavar_map:
                metavar = metavar_map[p.arg]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "any"})
        return {
            "type": "funcdef",
            "name": name_ir,
            "params": param_patterns,
            "decorators": decorators_ir,
            "is_async": isinstance(node, _ast.AsyncFunctionDef),
        }

    elif isinstance(node, _ast.ClassDef):
        name = node.name
        if name in metavar_map:
            name_ir = {"type": "metavar", "name": metavar_map[name].name}
        else:
            name_ir = {"type": "name", "value": name}
        decorators_ir = []
        for dec in node.decorator_list:
            dec_ir = _ast_to_rust_ir(dec, metavar_map)
            if dec_ir is None:
                return None
            decorators_ir.append(dec_ir)
        bases_ir = []
        for base in node.bases:
            base_ir = _ast_to_rust_ir(base, metavar_map)
            if base_ir is None:
                return None
            bases_ir.append(base_ir)
        return {
            "type": "classdef",
            "name": name_ir,
            "bases": bases_ir,
            "decorators": decorators_ir
        }

    elif isinstance(node, (_ast.ListComp, _ast.SetComp, _ast.GeneratorExp)):
        elt_ir = _ast_to_rust_ir(node.elt, metavar_map)
        if elt_ir is None:
            return None
        generators_ir = []
        for gen in node.generators:
            target_ir = _ast_to_rust_ir(gen.target, metavar_map)
            if target_ir is None:
                return None
            iter_ir = _ast_to_rust_ir(gen.iter, metavar_map)
            if iter_ir is None:
                return None
            ifs_ir = []
            for if_clause in gen.ifs:
                if_ir = _ast_to_rust_ir(if_clause, metavar_map)
                if if_ir is None:
                    return None
                ifs_ir.append(if_ir)
            generators_ir.append({
                "target": target_ir,
                "iter": iter_ir,
                "ifs": ifs_ir
            })
        kind = "list_comprehension"
        if isinstance(node, _ast.SetComp):
            kind = "set_comprehension"
        elif isinstance(node, _ast.GeneratorExp):
            kind = "generator_expression"
        return {
            "type": "comprehension",
            "kind": kind,
            "elt": elt_ir,
            "generators": generators_ir
        }

    elif isinstance(node, _ast.DictComp):
        key_ir = _ast_to_rust_ir(node.key, metavar_map)
        if key_ir is None:
            return None
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        generators_ir = []
        for gen in node.generators:
            target_ir = _ast_to_rust_ir(gen.target, metavar_map)
            if target_ir is None:
                return None
            iter_ir = _ast_to_rust_ir(gen.iter, metavar_map)
            if iter_ir is None:
                return None
            ifs_ir = []
            for if_clause in gen.ifs:
                if_ir = _ast_to_rust_ir(if_clause, metavar_map)
                if if_ir is None:
                    return None
                ifs_ir.append(if_ir)
            generators_ir.append({
                "target": target_ir,
                "iter": iter_ir,
                "ifs": ifs_ir
            })
        return {
            "type": "dict_comprehension",
            "key": key_ir,
            "value": value_ir,
            "generators": generators_ir
        }

    elif isinstance(node, _ast.JoinedStr):
        parts_ir = []
        for val in node.values:
            if isinstance(val, _ast.Constant) and isinstance(val.value, str):
                parts_ir.append({"type": "fstring_text", "value": val.value})
            elif isinstance(val, _ast.FormattedValue):
                expr_ir = _ast_to_rust_ir(val.value, metavar_map)
                if expr_ir is None:
                    return None
                parts_ir.append({"type": "fstring_expr", "value": expr_ir})
            else:
                return None
        return {"type": "fstring", "parts": parts_ir}

    elif isinstance(node, _ast.Starred):
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        return {"type": "star", "value": value_ir}

    elif isinstance(node, _ast.IfExp):
        test_ir = _ast_to_rust_ir(node.test, metavar_map)
        body_ir = _ast_to_rust_ir(node.body, metavar_map)
        orelse_ir = _ast_to_rust_ir(node.orelse, metavar_map)
        if test_ir is None or body_ir is None or orelse_ir is None:
            return None
        return {"type": "ifexp", "test": test_ir, "body": body_ir, "orelse": orelse_ir}

    elif isinstance(node, _ast.Lambda):
        # Build param patterns similar to FunctionDef
        param_patterns = []
        args = node.args
        n_defaults = len(args.defaults)
        n_args = len(args.args)
        for i, p in enumerate(args.args):
            default_idx = i - (n_args - n_defaults)
            has_default = default_idx >= 0
            if p.arg in metavar_map:
                metavar = metavar_map[p.arg]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "name", "value": p.arg})
        if args.vararg:
            if args.vararg.arg in metavar_map:
                metavar = metavar_map[args.vararg.arg]
                param_patterns.append({"type": "star", "name": metavar.name})
            else:
                param_patterns.append({"type": "star"})
        if args.kwarg:
            if args.kwarg.arg in metavar_map:
                metavar = metavar_map[args.kwarg.arg]
                param_patterns.append({"type": "double_star", "name": metavar.name})
            else:
                param_patterns.append({"type": "double_star"})
        body_ir = _ast_to_rust_ir(node.body, metavar_map)
        if body_ir is None:
            return None
        return {
            "type": "lambda",
            "params": param_patterns,
            "body": body_ir,
        }

    elif isinstance(node, _ast.NamedExpr):
        target_ir = _ast_to_rust_ir(node.target, metavar_map)
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if target_ir is None or value_ir is None:
            return None
        return {"type": "named_expr", "target": target_ir, "value": value_ir}

    elif isinstance(node, _ast.If):
        test_ir = _ast_to_rust_ir(node.test, metavar_map)
        if test_ir is None:
            return None
        return {"type": "if_stmt", "test": test_ir}

    elif isinstance(node, _ast.While):
        test_ir = _ast_to_rust_ir(node.test, metavar_map)
        if test_ir is None:
            return None
        return {"type": "while_stmt", "test": test_ir}

    elif isinstance(node, _ast.For) or isinstance(node, _ast.AsyncFor):
        target_ir = _ast_to_rust_ir(node.target, metavar_map)
        iter_ir = _ast_to_rust_ir(node.iter, metavar_map)
        if target_ir is None or iter_ir is None:
            return None
        return {
            "type": "for_stmt",
            "target": target_ir,
            "iter": iter_ir,
            "is_async": isinstance(node, _ast.AsyncFor),
        }

    elif isinstance(node, _ast.With) or isinstance(node, _ast.AsyncWith):
        if not node.items:
            return None
        item = node.items[0]
        ctx_ir = _ast_to_rust_ir(item.context_expr, metavar_map)
        if ctx_ir is None:
            return None
        var_ir = None
        if item.optional_vars:
            var_ir = _ast_to_rust_ir(item.optional_vars, metavar_map)
        return {
            "type": "with_stmt",
            "context": ctx_ir,
            "var": var_ir,
            "is_async": isinstance(node, _ast.AsyncWith),
        }

    elif isinstance(node, _ast.Try):
        return {"type": "try_stmt"}

    elif isinstance(node, _ast.ExceptHandler):
        type_ir = None
        if node.type:
            type_ir = _ast_to_rust_ir(node.type, metavar_map)
        name = node.name
        name_ir = None
        if name:
            if name in metavar_map:
                name_ir = {"type": "metavar", "name": metavar_map[name].name}
            else:
                name_ir = {"type": "name", "value": name}
        return {"type": "except_handler", "exception_type": type_ir, "name": name_ir}

    elif isinstance(node, _ast.Expr):
        return _ast_to_rust_ir(node.value, metavar_map)

    elif isinstance(node, _ast.Return):
        value_ir = None
        if node.value is not None:
            value_ir = _ast_to_rust_ir(node.value, metavar_map)
            if value_ir is None:
                return None
        return {"type": "return", "value": value_ir}

    elif isinstance(node, _ast.Assert):
        test_ir = _ast_to_rust_ir(node.test, metavar_map)
        if test_ir is None:
            return None
        msg_ir = None
        if node.msg is not None:
            msg_ir = _ast_to_rust_ir(node.msg, metavar_map)
            if msg_ir is None:
                return None
        return {"type": "assert", "test": test_ir, "msg": msg_ir}

    elif isinstance(node, _ast.Raise):
        exc_ir = None
        if node.exc is not None:
            exc_ir = _ast_to_rust_ir(node.exc, metavar_map)
            if exc_ir is None:
                return None
        return {"type": "raise", "exc": exc_ir}

    elif isinstance(node, _ast.Delete):
        if len(node.targets) != 1:
            return None
        target_ir = _ast_to_rust_ir(node.targets[0], metavar_map)
        if target_ir is None:
            return None
        return {"type": "delete", "target": target_ir}

    elif isinstance(node, _ast.Global):
        # Global has identifier names, but metavars need special handling
        # If any name is a metavar, we can't express this as a simple name list
        # Convert to a positional match
        items_ir = []
        for name in node.names:
            if name in metavar_map:
                metavar = metavar_map[name]
                items_ir.append({"type": "metavar", "name": metavar.name})
            else:
                items_ir.append(name)
        return {"type": "global", "names": items_ir}

    elif isinstance(node, _ast.Nonlocal):
        items_ir = []
        for name in node.names:
            if name in metavar_map:
                metavar = metavar_map[name]
                items_ir.append({"type": "metavar", "name": metavar.name})
            else:
                items_ir.append(name)
        return {"type": "nonlocal", "names": items_ir}

    elif isinstance(node, _ast.Await):
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        return {"type": "await", "value": value_ir}

    elif isinstance(node, _ast.ImportFrom):
        module = node.module
        module_ir = None
        if module is not None:
            if module in metavar_map:
                module_ir = {"type": "metavar", "name": metavar_map[module].name}
            else:
                module_ir = module
        names = []
        for alias in node.names:
            name = alias.name
            asname = alias.asname
            name_ir = name
            asname_ir = asname
            if name in metavar_map:
                name_ir = {"type": "metavar", "name": metavar_map[name].name}
            if asname and asname in metavar_map:
                asname_ir = {"type": "metavar", "name": metavar_map[asname].name}
            names.append({"name": name_ir, "asname": asname_ir})
        return {"type": "import_from", "module": module_ir, "names": names}

    elif isinstance(node, _ast.Import):
        names = []
        for alias in node.names:
            name = alias.name
            asname = alias.asname
            name_ir = name
            asname_ir = asname
            if name in metavar_map:
                name_ir = {"type": "metavar", "name": metavar_map[name].name}
            if asname and asname in metavar_map:
                asname_ir = {"type": "metavar", "name": metavar_map[asname].name}
            names.append({"name": name_ir, "asname": asname_ir})
        return {"type": "import", "names": names}

    return None


def compile_pattern_to_rust_ir(pattern_str: str) -> dict | None:
    """Compile a pattern string to Rust IR dict for the tree-sitter fast path."""
    import ast as _ast

    try:
        pattern = parse_pattern(pattern_str)
        temp_code, metavar_map = _build_metavar_map_and_replace(pattern)

        is_except_header = _is_except_header(temp_code)
        is_compound_header = _is_compound_statement_header(temp_code)
        is_try = temp_code.strip() == "try:"
        parse_code = temp_code
        if is_try:
            parse_code = "try:\n    pass\nexcept Exception:\n    pass"
        elif is_except_header:
            parse_code = "try:\n    pass\n" + temp_code + "\n    pass"
        elif is_compound_header:
            parse_code = temp_code + "\n    pass"

        try:
            tree = _ast.parse(parse_code, mode='eval')
            expr = tree.body
        except SyntaxError:
            try:
                tree = _ast.parse(parse_code, mode='exec')
                if tree.body:
                    expr = tree.body[0]
                    if is_try and isinstance(expr, _ast.Try):
                        pass
                    elif is_except_header and isinstance(expr, _ast.Try):
                        if expr.handlers:
                            expr = expr.handlers[0]
                else:
                    return None
            except SyntaxError:
                return None

        return _ast_to_rust_ir(expr, metavar_map)

    except Exception:
        return None


def compile_constraint_to_rust_ir(constraint: str | None) -> dict | None:
    """Compile an inside/not_inside constraint string to Rust IR dict."""
    if constraint is None:
        return None

    if constraint == "def":
        return {
            "type": "funcdef",
            "name": {"type": "any_expr"},
            "params": [{"type": "ellipsis"}],
            "decorators": [{"type": "ellipsis"}],
        }

    if constraint == "async def":
        return {
            "type": "funcdef",
            "name": {"type": "any_expr"},
            "params": [{"type": "ellipsis"}],
            "decorators": [{"type": "ellipsis"}],
            "is_async": True,
        }

    if constraint == "class":
        return {
            "type": "classdef",
            "name": {"type": "any_expr"},
            "bases": [{"type": "ellipsis"}],
            "decorators": [{"type": "ellipsis"}],
        }

    for keyword in ("async def", "def", "class"):
        if constraint.startswith(keyword + " "):
            name_pattern = constraint[len(keyword) + 1:].strip()
            name_pattern = name_pattern.rstrip(":").strip()
            if "*" in name_pattern:
                name_ir = {"type": "name_glob", "value": name_pattern}
            else:
                name_ir = {"type": "name", "value": name_pattern}
            
            if keyword == "def":
                return {
                    "type": "funcdef",
                    "name": name_ir,
                    "params": [{"type": "ellipsis"}],
                    "decorators": [{"type": "ellipsis"}],
                    "is_async": False,
                }
            elif keyword == "async def":
                return {
                    "type": "funcdef",
                    "name": name_ir,
                    "params": [{"type": "ellipsis"}],
                    "decorators": [{"type": "ellipsis"}],
                    "is_async": True,
                }
            else:
                return {
                    "type": "classdef",
                    "name": name_ir,
                    "bases": [{"type": "ellipsis"}],
                    "decorators": [{"type": "ellipsis"}],
                }

    # Simple keyword constraints for compound statements
    # Use NodeKindMatch to match related tree-sitter node types
    if constraint == "if":
        return {
            "type": "node_kind_match",
            "kinds": ["if_statement", "conditional_expression"],
        }

    if constraint == "for":
        return {
            "type": "node_kind_match",
            "kinds": [
                "for_statement",
                "list_comprehension",
                "set_comprehension",
                "dictionary_comprehension",
                "generator_expression",
            ],
        }

    if constraint == "while":
        return {
            "type": "node_kind_match",
            "kinds": ["while_statement"],
        }

    if constraint == "try":
        return {
            "type": "node_kind_match",
            "kinds": ["try_statement"],
        }

    if constraint == "with":
        return {
            "type": "node_kind_match",
            "kinds": ["with_statement"],
        }

    stripped = constraint.rstrip()
    if stripped.endswith(":"):
        ir = compile_pattern_to_rust_ir(stripped)
        if ir is not None:
            return ir

    return None
