"""Pattern parsing with metavariables."""
from __future__ import annotations
from dataclasses import dataclass
from lark import Lark, Transformer, Token
import importlib.resources
import libcst as cst
from libcst import matchers as m
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


def _comp_for_to_matcher(
    comp_for: cst.CompFor,
    metavar_map: dict[str, MetaVar],
    ellipsis_info: dict | None = None
) -> m.BaseMatcherNode:
    """Convert CompFor node to matcher."""
    if ellipsis_info is None:
        ellipsis_info = {}

    # Match the target (loop variable)
    target_matcher = _cst_to_matcher(comp_for.target, metavar_map, ellipsis_info)

    # Match the iterable
    iter_matcher = _cst_to_matcher(comp_for.iter, metavar_map, ellipsis_info)

    # Match any if clauses
    if comp_for.ifs:
        ifs_matchers = []
        for comp_if in comp_for.ifs:
            test_matcher = _cst_to_matcher(comp_if.test, metavar_map, ellipsis_info)
            ifs_matchers.append(m.CompIf(test=test_matcher))
        return m.CompFor(target=target_matcher, iter=iter_matcher, ifs=ifs_matchers)
    else:
        return m.CompFor(target=target_matcher, iter=iter_matcher)


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


def _type_constraint_matcher(constraint: str | None) -> m.BaseMatcherNode:
    """Convert type constraint string to appropriate matcher."""
    if constraint is not None and constraint.startswith("!"):
        # Negated constraint: match anything EXCEPT the specified type
        inner = _type_constraint_matcher(constraint[1:])
        return m.DoesNotMatch(inner)

    # Oracle type constraints match any node syntactically;
    # actual type checking is done post-match by the finder.
    if constraint is not None and is_oracle_type_constraint(constraint):
        return m.DoNotCare()

    if constraint == "int":
        return m.Integer()
    elif constraint == "str":
        # Match any kind of string literal
        return m.OneOf(m.SimpleString(), m.FormattedString(), m.ConcatenatedString())
    elif constraint == "identifier":
        return m.Name()
    elif constraint == "float":
        return m.Float()
    elif constraint == "call":
        return m.Call()
    elif constraint == "attr":
        return m.Attribute()
    elif constraint == "stmt":
        # Match any small statement type
        return m.OneOf(
            m.Return(), m.Assert(), m.Raise(),
            m.Assign(), m.AugAssign(),
            m.Del(), m.Global(), m.Nonlocal(),
            m.Import(), m.ImportFrom()
        )
    elif constraint == "expr" or constraint == "any" or constraint is None:
        return m.DoNotCare()
    else:
        # Unknown constraint - fall back to DoNotCare
        return m.DoNotCare()


def _operator_to_matcher(operator: cst.BaseCompOp) -> m.BaseMatcherNode:
    """Convert a comparison operator to a matcher."""
    if isinstance(operator, cst.Equal):
        return m.Equal()
    elif isinstance(operator, cst.NotEqual):
        return m.NotEqual()
    elif isinstance(operator, cst.LessThan):
        return m.LessThan()
    elif isinstance(operator, cst.LessThanEqual):
        return m.LessThanEqual()
    elif isinstance(operator, cst.GreaterThan):
        return m.GreaterThan()
    elif isinstance(operator, cst.GreaterThanEqual):
        return m.GreaterThanEqual()
    elif isinstance(operator, cst.Is):
        return m.Is()
    elif isinstance(operator, cst.IsNot):
        return m.IsNot()
    elif isinstance(operator, cst.In):
        return m.In()
    elif isinstance(operator, cst.NotIn):
        return m.NotIn()
    else:
        return m.DoNotCare()


def _binary_operator_to_matcher(operator: cst.BaseBinaryOp) -> m.BaseMatcherNode:
    """Convert a binary operator to a matcher."""
    if isinstance(operator, cst.Add):
        return m.Add()
    elif isinstance(operator, cst.Subtract):
        return m.Subtract()
    elif isinstance(operator, cst.Multiply):
        return m.Multiply()
    elif isinstance(operator, cst.Divide):
        return m.Divide()
    elif isinstance(operator, cst.FloorDivide):
        return m.FloorDivide()
    elif isinstance(operator, cst.Modulo):
        return m.Modulo()
    elif isinstance(operator, cst.Power):
        return m.Power()
    elif isinstance(operator, cst.LeftShift):
        return m.LeftShift()
    elif isinstance(operator, cst.RightShift):
        return m.RightShift()
    elif isinstance(operator, cst.BitOr):
        return m.BitOr()
    elif isinstance(operator, cst.BitXor):
        return m.BitXor()
    elif isinstance(operator, cst.BitAnd):
        return m.BitAnd()
    elif isinstance(operator, cst.MatrixMultiply):
        return m.MatrixMultiply()
    else:
        return m.DoNotCare()


def _boolean_operator_to_matcher(operator: cst.BaseBooleanOp) -> m.BaseMatcherNode:
    """Convert a boolean operator to a matcher."""
    if isinstance(operator, cst.And):
        return m.And()
    elif isinstance(operator, cst.Or):
        return m.Or()
    else:
        return m.DoNotCare()


def _unary_operator_to_matcher(operator: cst.BaseUnaryOp) -> m.BaseMatcherNode:
    """Convert a unary operator to a matcher."""
    if isinstance(operator, cst.Plus):
        return m.Plus()
    elif isinstance(operator, cst.Minus):
        return m.Minus()
    elif isinstance(operator, cst.BitInvert):
        return m.BitInvert()
    elif isinstance(operator, cst.Not):
        return m.Not()
    else:
        return m.DoNotCare()


def _aug_operator_to_matcher(operator: cst.BaseAugOp) -> m.BaseMatcherNode:
    """Convert an augmented assignment operator to a matcher."""
    if isinstance(operator, cst.AddAssign):
        return m.AddAssign()
    elif isinstance(operator, cst.SubtractAssign):
        return m.SubtractAssign()
    elif isinstance(operator, cst.MultiplyAssign):
        return m.MultiplyAssign()
    elif isinstance(operator, cst.DivideAssign):
        return m.DivideAssign()
    elif isinstance(operator, cst.FloorDivideAssign):
        return m.FloorDivideAssign()
    elif isinstance(operator, cst.ModuloAssign):
        return m.ModuloAssign()
    elif isinstance(operator, cst.PowerAssign):
        return m.PowerAssign()
    elif isinstance(operator, cst.LeftShiftAssign):
        return m.LeftShiftAssign()
    elif isinstance(operator, cst.RightShiftAssign):
        return m.RightShiftAssign()
    elif isinstance(operator, cst.BitAndAssign):
        return m.BitAndAssign()
    elif isinstance(operator, cst.BitXorAssign):
        return m.BitXorAssign()
    elif isinstance(operator, cst.BitOrAssign):
        return m.BitOrAssign()
    elif isinstance(operator, cst.MatrixMultiplyAssign):
        return m.MatrixMultiplyAssign()
    else:
        return m.DoNotCare()


def _cst_to_matcher(
    node: cst.CSTNode,
    metavar_map: dict[str, MetaVar],
    ellipsis_info: dict | None = None
) -> m.BaseMatcherNode:
    """Recursively convert CST node to matcher."""
    if ellipsis_info is None:
        ellipsis_info = {}
    if isinstance(node, cst.Name):
        if node.value in metavar_map:
            metavar = metavar_map[node.value]
            inner_matcher = _type_constraint_matcher(metavar.type_constraint)
            if metavar.name == "_":
                return inner_matcher
            return m.SaveMatchedNode(inner_matcher, metavar.name)
        else:
            return m.Name(node.value)

    elif isinstance(node, cst.Call):
        func_matcher = _cst_to_matcher(node.func, metavar_map, ellipsis_info)
        arg_matchers = []
        for i, arg in enumerate(node.args):
            if isinstance(arg.value, cst.Name) and arg.value.value in metavar_map:
                metavar = metavar_map[arg.value.value]
                if metavar.ellipsis:
                    ellipsis_info[metavar.name] = {
                        "position": i,
                        "total_args": len(node.args)
                    }
                    arg_matchers.append(m.ZeroOrMore(m.Arg()))
                    continue

            arg_value_matcher = _cst_to_matcher(arg.value, metavar_map, ellipsis_info)
            if arg.star in ("*", "**"):
                arg_matchers.append(m.Arg(value=arg_value_matcher, star=arg.star))
            else:
                arg_matchers.append(m.Arg(value=arg_value_matcher))

        return m.Call(func=func_matcher, args=arg_matchers)

    elif isinstance(node, cst.SimpleString):
        return m.SimpleString(node.value)

    elif isinstance(node, cst.Integer):
        return m.Integer(node.value)

    elif isinstance(node, cst.Float):
        return m.Float(node.value)

    elif isinstance(node, cst.Attribute):
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        attr_matcher = _cst_to_matcher(node.attr, metavar_map, ellipsis_info)
        return m.Attribute(value=value_matcher, attr=attr_matcher)

    elif isinstance(node, cst.Arg):
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        return m.Arg(value=value_matcher)

    elif isinstance(node, cst.Comparison):
        left_matcher = _cst_to_matcher(node.left, metavar_map, ellipsis_info)
        comparison_matchers = []
        for target in node.comparisons:
            comparator_matcher = _cst_to_matcher(target.comparator, metavar_map, ellipsis_info)
            operator_matcher = _operator_to_matcher(target.operator)
            comparison_matchers.append(
                m.ComparisonTarget(
                    operator=operator_matcher,
                    comparator=comparator_matcher
                )
            )
        return m.Comparison(left=left_matcher, comparisons=comparison_matchers)

    elif isinstance(node, cst.BinaryOperation):
        left_matcher = _cst_to_matcher(node.left, metavar_map, ellipsis_info)
        right_matcher = _cst_to_matcher(node.right, metavar_map, ellipsis_info)
        operator_matcher = _binary_operator_to_matcher(node.operator)
        return m.BinaryOperation(left=left_matcher, operator=operator_matcher, right=right_matcher)

    elif isinstance(node, cst.BooleanOperation):
        left_matcher = _cst_to_matcher(node.left, metavar_map, ellipsis_info)
        right_matcher = _cst_to_matcher(node.right, metavar_map, ellipsis_info)
        operator_matcher = _boolean_operator_to_matcher(node.operator)
        return m.BooleanOperation(left=left_matcher, operator=operator_matcher, right=right_matcher)

    elif isinstance(node, cst.UnaryOperation):
        expression_matcher = _cst_to_matcher(node.expression, metavar_map, ellipsis_info)
        operator_matcher = _unary_operator_to_matcher(node.operator)
        return m.UnaryOperation(operator=operator_matcher, expression=expression_matcher)

    elif isinstance(node, cst.Subscript):
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        slice_matchers = []
        for slice_element in node.slice:
            if isinstance(slice_element, cst.SubscriptElement):
                slice_item_matcher = _cst_to_matcher(slice_element.slice, metavar_map, ellipsis_info)
                slice_matchers.append(m.SubscriptElement(slice=slice_item_matcher))
            else:
                slice_matchers.append(m.DoNotCare())
        return m.Subscript(value=value_matcher, slice=slice_matchers)

    elif isinstance(node, cst.Index):
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        return m.Index(value=value_matcher)

    elif isinstance(node, cst.IfExp):
        body_matcher = _cst_to_matcher(node.body, metavar_map, ellipsis_info)
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        orelse_matcher = _cst_to_matcher(node.orelse, metavar_map, ellipsis_info)
        return m.IfExp(body=body_matcher, test=test_matcher, orelse=orelse_matcher)

    elif isinstance(node, cst.Await):
        expression_matcher = _cst_to_matcher(node.expression, metavar_map, ellipsis_info)
        return m.Await(expression=expression_matcher)

    elif isinstance(node, cst.Tuple):
        element_matchers = []
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.Element):
                if isinstance(elem.value, cst.Name) and elem.value.value in metavar_map:
                    metavar = metavar_map[elem.value.value]
                    if metavar.ellipsis:
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        element_matchers.append(m.ZeroOrMore(m.Element()))
                        continue

                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.Element(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())
        return m.Tuple(elements=element_matchers)

    elif isinstance(node, cst.List):
        element_matchers = []
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.Element):
                if isinstance(elem.value, cst.Name) and elem.value.value in metavar_map:
                    metavar = metavar_map[elem.value.value]
                    if metavar.ellipsis:
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        element_matchers.append(m.ZeroOrMore(m.Element()))
                        continue

                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.Element(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())
        return m.List(elements=element_matchers)

    elif isinstance(node, cst.Set):
        element_matchers = []
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.Element):
                if isinstance(elem.value, cst.Name) and elem.value.value in metavar_map:
                    metavar = metavar_map[elem.value.value]
                    if metavar.ellipsis:
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        element_matchers.append(m.ZeroOrMore(m.Element()))
                        continue

                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.Element(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())
        return m.Set(elements=element_matchers)

    elif isinstance(node, cst.Dict):
        element_matchers = []
        has_partial_spread = False
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.DictElement):
                if isinstance(elem.key, cst.Name) and elem.key.value in metavar_map:
                    metavar = metavar_map[elem.key.value]
                    if metavar.ellipsis:
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        element_matchers.append(m.ZeroOrMore(m.OneOf(m.DictElement(), m.StarredDictElement())))
                        continue

                key_matcher = _cst_to_matcher(elem.key, metavar_map, ellipsis_info)
                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.DictElement(key=key_matcher, value=value_matcher))
            elif isinstance(elem, cst.StarredDictElement):
                if isinstance(elem.value, cst.Name) and elem.value.value == "__EMEND_SPREAD__":
                    has_partial_spread = True
                    continue
                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.StarredDictElement(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())

        if has_partial_spread:
            required = element_matchers[:]
            def _make_checker(req_matchers):
                def check_elements(elements):
                    for req in req_matchers:
                        if not any(m.matches(e, req) for e in elements):
                            return False
                    return True
                return check_elements
            ellipsis_info["__partial_dict__"] = {
                "element_matchers": required,
            }
            return m.Dict(elements=m.MatchIfTrue(_make_checker(required)))

        return m.Dict(elements=element_matchers)

    elif isinstance(node, cst.Lambda):
        body_matcher = _cst_to_matcher(node.body, metavar_map, ellipsis_info)
        params = node.params
        all_params = list(params.params)
        has_star_arg = isinstance(params.star_arg, cst.Param)
        has_star_kwarg = params.star_kwarg is not None

        if not all_params and not has_star_arg and not has_star_kwarg and not params.kwonly_params:
            params_matcher = m.Parameters(params=[], kwonly_params=[], star_kwarg=None)
        else:
            param_matchers = []
            has_ellipsis = False
            for p in all_params:
                if isinstance(p.name, cst.Name) and p.name.value in metavar_map:
                    metavar = metavar_map[p.name.value]
                    if metavar.ellipsis:
                        has_ellipsis = True
                        continue
                    name_matcher = m.SaveMatchedNode(m.Name(), metavar.name)
                else:
                    name_matcher = m.Name(p.name.value)
                param_matchers.append(m.Param(name=name_matcher))

            if has_ellipsis:
                params_matcher = m.DoNotCare()
            else:
                params_kwargs = dict(params=param_matchers)
                if has_star_arg:
                    star_param = params.star_arg
                    if isinstance(star_param.name, cst.Name) and star_param.name.value in metavar_map:
                        metavar = metavar_map[star_param.name.value]
                        star_name_matcher = m.SaveMatchedNode(m.Name(), metavar.name)
                    else:
                        star_name_matcher = m.Name(star_param.name.value)
                    params_kwargs["star_arg"] = m.Param(name=star_name_matcher, star="*")
                    if not has_star_kwarg:
                        params_kwargs["star_kwarg"] = None
                if has_star_kwarg:
                    kwarg_param = params.star_kwarg
                    if isinstance(kwarg_param.name, cst.Name) and kwarg_param.name.value in metavar_map:
                        metavar = metavar_map[kwarg_param.name.value]
                        kwarg_name_matcher = m.SaveMatchedNode(m.Name(), metavar.name)
                    else:
                        kwarg_name_matcher = m.Name(kwarg_param.name.value)
                    params_kwargs["star_kwarg"] = m.Param(name=kwarg_name_matcher, star="**")
                params_matcher = m.Parameters(**params_kwargs)

        return m.Lambda(body=body_matcher, params=params_matcher)

    elif isinstance(node, cst.FunctionDef):
        name_matcher = _cst_to_matcher(node.name, metavar_map, ellipsis_info)
        params = node.params
        has_ellipsis_params = False
        all_params = list(params.params) + list(params.posonly_params or []) + list(params.kwonly_params or [])
        for p in all_params:
            if isinstance(p.name, cst.Name) and p.name.value in metavar_map:
                metavar = metavar_map[p.name.value]
                if metavar.ellipsis:
                    has_ellipsis_params = True

        param_matchers_node = m.DoNotCare()
        kwargs = dict(
            name=name_matcher,
            params=param_matchers_node,
            body=m.DoNotCare(),
            leading_lines=m.DoNotCare(),
            lines_after_decorators=m.DoNotCare(),
        )
        if node.asynchronous is not None:
            kwargs["asynchronous"] = m.Asynchronous()
        if node.decorators:
            dec_matchers = []
            for dec in node.decorators:
                dec_expr_matcher = _cst_to_matcher(dec.decorator, metavar_map, ellipsis_info)
                dec_matchers.append(m.Decorator(decorator=dec_expr_matcher))
            kwargs["decorators"] = dec_matchers
        return m.FunctionDef(**kwargs)

    elif isinstance(node, cst.If):
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        return m.If(test=test_matcher, body=m.DoNotCare(), leading_lines=m.DoNotCare(), orelse=m.DoNotCare())

    elif isinstance(node, cst.While):
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        return m.While(test=test_matcher, body=m.DoNotCare(), leading_lines=m.DoNotCare(), orelse=m.DoNotCare())

    elif isinstance(node, cst.For):
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        iter_matcher = _cst_to_matcher(node.iter, metavar_map, ellipsis_info)
        kwargs = dict(target=target_matcher, iter=iter_matcher, body=m.DoNotCare(), leading_lines=m.DoNotCare(), orelse=m.DoNotCare())
        if node.asynchronous is not None:
            kwargs["asynchronous"] = m.Asynchronous()
        return m.For(**kwargs)

    elif isinstance(node, cst.With):
        item_matchers = []
        for item in node.items:
            item_matcher = _cst_to_matcher(item.item, metavar_map, ellipsis_info)
            if item.asname is not None:
                asname_matcher = _cst_to_matcher(item.asname.name, metavar_map, ellipsis_info)
                item_matchers.append(m.WithItem(item=item_matcher, asname=m.AsName(name=asname_matcher)))
            else:
                item_matchers.append(m.WithItem(item=item_matcher))
        kwargs = dict(items=item_matchers, body=m.DoNotCare(), leading_lines=m.DoNotCare())
        if node.asynchronous is not None:
            kwargs["asynchronous"] = m.Asynchronous()
        return m.With(**kwargs)

    elif isinstance(node, cst.Try):
        return m.Try(body=m.DoNotCare(), leading_lines=m.DoNotCare())

    elif isinstance(node, cst.ExceptHandler):
        if node.type is not None:
            type_matcher = _cst_to_matcher(node.type, metavar_map, ellipsis_info)
            if node.name is not None:
                name_matcher = _cst_to_matcher(node.name.name, metavar_map, ellipsis_info)
                return m.ExceptHandler(type=type_matcher, name=m.AsName(name=name_matcher), body=m.DoNotCare())
            else:
                return m.ExceptHandler(type=type_matcher, body=m.DoNotCare())
        else:
            return m.ExceptHandler(type=None, body=m.DoNotCare())

    elif isinstance(node, cst.NamedExpr):
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        return m.NamedExpr(target=target_matcher, value=value_matcher)

    elif isinstance(node, cst.Assign):
        target_matcher = _cst_to_matcher(node.targets[0].target, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        return m.Assign(targets=[m.AtLeastN(n=0), m.AssignTarget(target=target_matcher), m.AtLeastN(n=0)], value=value_matcher)

    elif isinstance(node, cst.AugAssign):
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        operator_matcher = _aug_operator_to_matcher(node.operator)
        return m.AugAssign(target=target_matcher, operator=operator_matcher, value=value_matcher)

    elif isinstance(node, cst.Return):
        if node.value is not None:
            value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
            return m.Return(value=value_matcher)
        else:
            return m.Return(value=m.DoNotCare())

    elif isinstance(node, cst.Assert):
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        if node.msg is not None:
            msg_matcher = _cst_to_matcher(node.msg, metavar_map, ellipsis_info)
            return m.Assert(test=test_matcher, msg=msg_matcher)
        else:
            return m.Assert(test=test_matcher, msg=m.DoNotCare())

    elif isinstance(node, cst.Raise):
        if node.exc is not None:
            exc_matcher = _cst_to_matcher(node.exc, metavar_map, ellipsis_info)
            return m.Raise(exc=exc_matcher)
        else:
            return m.Raise(exc=m.DoNotCare())

    elif isinstance(node, cst.Del):
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        return m.Del(target=target_matcher)

    elif isinstance(node, cst.Global):
        if len(node.names) == 1:
            name_item = node.names[0]
            name_matcher = _cst_to_matcher(name_item.name, metavar_map, ellipsis_info)
            return m.Global(names=[m.NameItem(name=name_matcher)])
        else:
            return m.Global()

    elif isinstance(node, cst.Nonlocal):
        if len(node.names) == 1:
            name_item = node.names[0]
            name_matcher = _cst_to_matcher(name_item.name, metavar_map, ellipsis_info)
            return m.Nonlocal(names=[m.NameItem(name=name_matcher)])
        else:
            return m.Nonlocal()

    elif isinstance(node, cst.ImportFrom):
        if node.module is not None:
            module_matcher = _cst_to_matcher(node.module, metavar_map, ellipsis_info)
        else:
            module_matcher = m.DoNotCare()
        if not isinstance(node.names, cst.ImportStar) and len(node.names) == 1:
            import_alias = node.names[0]
            name_matcher = _cst_to_matcher(import_alias.name, metavar_map, ellipsis_info)
            if import_alias.asname is not None:
                asname_matcher = _cst_to_matcher(import_alias.asname.name, metavar_map, ellipsis_info)
                return m.ImportFrom(module=module_matcher, names=[m.ImportAlias(name=name_matcher, asname=m.AsName(name=asname_matcher))])
            else:
                return m.ImportFrom(module=module_matcher, names=[m.ImportAlias(name=name_matcher)])
        else:
            return m.ImportFrom(module=module_matcher)

    elif isinstance(node, cst.Import):
        if len(node.names) == 1:
            import_alias = node.names[0]
            name_matcher = _cst_to_matcher(import_alias.name, metavar_map, ellipsis_info)
            if import_alias.asname is not None:
                asname_matcher = _cst_to_matcher(import_alias.asname.name, metavar_map, ellipsis_info)
                return m.Import(names=[m.ImportAlias(name=name_matcher, asname=m.AsName(name=asname_matcher))])
            else:
                return m.Import(names=[m.ImportAlias(name=name_matcher)])
        else:
            return m.Import()

    elif isinstance(node, cst.ListComp):
        elt_matcher = _cst_to_matcher(node.elt, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.ListComp(elt=elt_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.SetComp):
        elt_matcher = _cst_to_matcher(node.elt, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.SetComp(elt=elt_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.DictComp):
        key_matcher = _cst_to_matcher(node.key, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.DictComp(key=key_matcher, value=value_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.GeneratorExp):
        elt_matcher = _cst_to_matcher(node.elt, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.GeneratorExp(elt=elt_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.FormattedString):
        part_matchers = []
        for part in node.parts:
            if isinstance(part, cst.FormattedStringText):
                part_matchers.append(m.FormattedStringText(part.value))
            elif isinstance(part, cst.FormattedStringExpression):
                expr_matcher = _cst_to_matcher(part.expression, metavar_map, ellipsis_info)
                part_matchers.append(m.FormattedStringExpression(expression=expr_matcher))
        return m.FormattedString(parts=part_matchers)

    else:
        return m.DoNotCare()


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


def compile_pattern_to_matcher(pattern: Pattern) -> tuple[m.BaseMatcherNode, dict]:
    """Compile parsed pattern to LibCST matcher for finding matches."""
    temp_code, metavar_map = _build_metavar_map_and_replace(pattern)

    # Step 2: Detect compound statement headers and add dummy body
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

    # Step 3: Parse as Python expression
    try:
        expr = cst.parse_expression(parse_code)
    except Exception:
        try:
            module = cst.parse_module(parse_code)
            if module.body:
                expr = module.body[0]
                if isinstance(expr, cst.SimpleStatementLine) and len(expr.body) == 1:
                    expr = expr.body[0]
                if is_try and isinstance(expr, cst.Try):
                    pass
                elif is_except_header and isinstance(expr, cst.Try):
                    if expr.handlers:
                        expr = expr.handlers[0]
            else:
                raise ValueError(f"Pattern '{pattern.raw}' could not be parsed as Python code")
        except Exception as e:
            raise ValueError(f"Pattern '{pattern.raw}' could not be parsed as Python code") from e

    # Step 4: Convert to matcher, collecting ellipsis info
    ellipsis_info = {}
    matcher = _cst_to_matcher(expr, metavar_map, ellipsis_info)

    return matcher, ellipsis_info


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


def _cst_to_rust_ir(node: cst.CSTNode, metavar_map: dict[str, MetaVar]) -> dict | None:
    """Convert a LibCST node to a Rust IR dict."""
    if isinstance(node, cst.Name):
        if node.value in metavar_map:
            metavar = metavar_map[node.value]
            if metavar.type_constraint in (":int", ":str", ":call"):
                return {"type": "type_constraint", "kind": metavar.type_constraint[1:]}
            if metavar.type_constraint is not None:
                return None
            if metavar.ellipsis:
                return {"type": "ellipsis"}
            else:
                return {"type": "any_expr"}
        else:
            if node.value == "None":
                return {"type": "none"}
            if node.value == "True":
                return {"type": "bool", "value": True}
            if node.value == "False":
                return {"type": "bool", "value": False}
            return {"type": "name", "value": node.value}

    elif isinstance(node, cst.Call):
        func_ir = _cst_to_rust_ir(node.func, metavar_map)
        if func_ir is None:
            return None
        args_ir = []
        has_ellipsis = False
        for arg in node.args:
            if arg.star == "" and isinstance(arg.value, cst.Name) and arg.value.value in metavar_map:
                metavar = metavar_map[arg.value.value]
                if metavar.ellipsis:
                    args_ir.append({"type": "ellipsis"})
                    has_ellipsis = True
                    continue
            arg_ir = _cst_to_rust_ir(arg.value, metavar_map)
            if arg_ir is None:
                return None
            if arg.star == "*":
                args_ir.append({"type": "star", "value": arg_ir})
            elif arg.star == "**":
                args_ir.append({"type": "double_star", "value": arg_ir})
            elif arg.keyword is not None:
                kname = arg.keyword.value
                args_ir.append({"type": "keyword_arg", "key": kname, "value": arg_ir})
            else:
                args_ir.append(arg_ir)
        return {
            "type": "call",
            "func": func_ir,
            "args": args_ir,
            "exact_args": not has_ellipsis,
        }

    elif isinstance(node, cst.Attribute):
        value_ir = _cst_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        attr_name = node.attr.value if isinstance(node.attr, cst.Name) else str(node.attr)
        return {"type": "attr", "value": value_ir, "attr": attr_name}

    elif isinstance(node, cst.Integer):
        return {"type": "integer", "value": node.value}

    elif isinstance(node, cst.SimpleString):
        return {"type": "string", "value": node.value}

    elif isinstance(node, cst.List):
        if len(node.elements) == 0:
            return {"type": "empty_list"}
        elems_ir = []
        for elem in node.elements:
            if isinstance(elem, cst.Element):
                elem_ir = _cst_to_rust_ir(elem.value, metavar_map)
                if elem_ir is None:
                    return None
                elems_ir.append(elem_ir)
            else:
                return None
        return {"type": "list", "elements": elems_ir}

    elif isinstance(node, cst.FunctionDef):
        name_ir = _cst_to_rust_ir(node.name, metavar_map)
        if name_ir is None:
            return None
        decorators_ir = []
        for dec in node.decorators:
            dec_ir = _cst_to_rust_ir(dec.decorator, metavar_map)
            if dec_ir is None:
                return None
            decorators_ir.append(dec_ir)
        params = node.params
        param_patterns = []
        all_params = list(params.params) + list(params.posonly_params or []) + list(params.kwonly_params or [])
        for p in all_params:
            if isinstance(p.name, cst.Name) and p.name.value in metavar_map:
                metavar = metavar_map[p.name.value]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                    continue
                if p.default is not None:
                    dv_ir = _cst_to_rust_ir(p.default, metavar_map)
                    if dv_ir is None:
                        return None
                    param_patterns.append({"type": "with_default", "default_value": dv_ir})
                else:
                    param_patterns.append({"type": "any"})
            else:
                if p.default is not None:
                    dv_ir = _cst_to_rust_ir(p.default, metavar_map)
                    if dv_ir is None:
                        return None
                    param_patterns.append({"type": "with_default", "default_value": dv_ir})
                else:
                    pname = p.name.value if isinstance(p.name, cst.Name) else str(p.name)
                    param_patterns.append({"type": "name", "value": pname})
        if params.star_arg and isinstance(params.star_arg, cst.Param):
            p = params.star_arg
            if isinstance(p.name, cst.Name) and p.name.value in metavar_map:
                metavar = metavar_map[p.name.value]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "any"})
        if params.star_kwarg:
            p = params.star_kwarg
            if isinstance(p.name, cst.Name) and p.name.value in metavar_map:
                metavar = metavar_map[p.name.value]
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
            "is_async": node.asynchronous is not None,
        }

    elif isinstance(node, cst.ClassDef):
        name_ir = _cst_to_rust_ir(node.name, metavar_map)
        if name_ir is None:
            return None
        decorators_ir = []
        for dec in node.decorators:
            dec_ir = _cst_to_rust_ir(dec.decorator, metavar_map)
            if dec_ir is None:
                return None
            decorators_ir.append(dec_ir)
        bases_ir = []
        for base_arg in node.bases:
            base_ir = _cst_to_rust_ir(base_arg.value, metavar_map)
            if base_ir is None:
                return None
            bases_ir.append(base_ir)
        return {
            "type": "classdef",
            "name": name_ir,
            "bases": bases_ir,
            "decorators": decorators_ir
        }

    elif isinstance(node, cst.Subscript):
        value_ir = _cst_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        slices_ir = []
        for sl in node.slice:
            if isinstance(sl, cst.SubscriptElement):
                s_ir = _cst_to_rust_ir(sl.slice, metavar_map)
                if s_ir is None:
                    return None
                slices_ir.append(s_ir)
            else:
                return None
        return {"type": "subscript", "value": value_ir, "slices": slices_ir}

    elif isinstance(node, cst.Index):
        return _cst_to_rust_ir(node.value, metavar_map)

    elif isinstance(node, cst.Tuple):
        elems_ir = []
        for elem in node.elements:
            if isinstance(elem, cst.Element):
                e_ir = _cst_to_rust_ir(elem.value, metavar_map)
                if e_ir is None:
                    return None
                elems_ir.append(e_ir)
            else:
                return None
        return {"type": "tuple", "elements": elems_ir}

    elif isinstance(node, cst.Dict):
        elems_ir = []
        for elem in node.elements:
            if isinstance(elem, cst.DictElement):
                # Check if the key is an ellipsis placeholder
                if isinstance(elem.key, cst.Name) and elem.key.value in metavar_map:
                    metavar = metavar_map[elem.key.value]
                    if metavar.ellipsis:
                        elems_ir.append({"type": "ellipsis"})
                        continue
                
                k_ir = _cst_to_rust_ir(elem.key, metavar_map)
                v_ir = _cst_to_rust_ir(elem.value, metavar_map)
                if k_ir is None or v_ir is None:
                    return None
                elems_ir.append({"type": "pair", "key": k_ir, "value": v_ir})
            elif isinstance(elem, cst.StarredDictElement):
                # Check for ellipsis metavar or partial dict spread marker
                if isinstance(elem.value, cst.Name):
                    if elem.value.value in metavar_map:
                        metavar = metavar_map[elem.value.value]
                        if metavar.ellipsis:
                            elems_ir.append({"type": "ellipsis"})
                            continue
                    elif elem.value.value == "__EMEND_SPREAD__":
                        elems_ir.append({"type": "ellipsis"})
                        continue

                v_ir = _cst_to_rust_ir(elem.value, metavar_map)
                if v_ir is None:
                    return None
                elems_ir.append({"type": "spread", "value": v_ir})
            else:
                return None
        return {"type": "dict", "elements": elems_ir}

    elif isinstance(node, cst.Set):
        elems_ir = []
        for elem in node.elements:
            if isinstance(elem, cst.Element):
                e_ir = _cst_to_rust_ir(elem.value, metavar_map)
                if e_ir is None:
                    return None
                elems_ir.append(e_ir)
            else:
                return None
        return {"type": "set", "elements": elems_ir}

    elif isinstance(node, (cst.BinaryOperation, cst.BooleanOperation)):
        left_ir = _cst_to_rust_ir(node.left, metavar_map)
        if left_ir is None:
            return None
        right_ir = _cst_to_rust_ir(node.right, metavar_map)
        if right_ir is None:
            return None
        op_map = {
            cst.Add: "+", cst.Subtract: "-", cst.Multiply: "*",
            cst.Divide: "/", cst.FloorDivide: "//", cst.Modulo: "%",
            cst.Power: "**", cst.BitAnd: "&", cst.BitOr: "|",
            cst.BitXor: "^", cst.LeftShift: "<<", cst.RightShift: ">>",
            cst.And: "and", cst.Or: "or", cst.MatrixMultiply: "@",
        }
        op_str = op_map.get(type(node.operator))
        if op_str is None:
            return None
        return {"type": "binary_op", "left": left_ir, "op": op_str, "right": right_ir}

    elif isinstance(node, cst.Assign):
        if len(node.targets) != 1:
            return None
        target_ir = _cst_to_rust_ir(node.targets[0].target, metavar_map)
        if target_ir is None:
            return None
        value_ir = _cst_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        return {"type": "assign", "target": target_ir, "value": value_ir}

    elif isinstance(node, cst.AugAssign):
        target_ir = _cst_to_rust_ir(node.target, metavar_map)
        if target_ir is None:
            return None
        value_ir = _cst_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        op_map = {
            cst.AddAssign: "+=", cst.SubtractAssign: "-=", cst.MultiplyAssign: "*=",
            cst.DivideAssign: "/=", cst.FloorDivideAssign: "//=", cst.ModuloAssign: "%=",
            cst.PowerAssign: "**=", cst.BitAndAssign: "&=", cst.BitOrAssign: "|=",
            cst.BitXorAssign: "^=", cst.LeftShiftAssign: "<<=", cst.RightShiftAssign: ">>=",
            cst.MatrixMultiplyAssign: "@=",
        }
        op_str = op_map.get(type(node.operator))
        if op_str is None:
            return None
        return {"type": "aug_assign", "target": target_ir, "op": op_str, "value": value_ir}

    elif isinstance(node, cst.AnnAssign):
        target_ir = _cst_to_rust_ir(node.target, metavar_map)
        if target_ir is None:
            return None
        annotation_ir = _cst_to_rust_ir(node.annotation, metavar_map)
        if annotation_ir is None:
            return None
        value_ir = None
        if node.value:
            value_ir = _cst_to_rust_ir(node.value, metavar_map)
            if value_ir is None:
                return None
        return {
            "type": "ann_assign",
            "target": target_ir,
            "annotation": annotation_ir,
            "value": value_ir
        }

    elif isinstance(node, cst.Comparison):
        left_ir = _cst_to_rust_ir(node.left, metavar_map)
        if left_ir is None:
            return None
        ops_ir = []
        comp_op_map = {
            cst.Equal: "==", cst.NotEqual: "!=",
            cst.LessThan: "<", cst.GreaterThan: ">",
            cst.LessThanEqual: "<=", cst.GreaterThanEqual: ">=",
            cst.Is: "is", cst.IsNot: "is not",
            cst.In: "in", cst.NotIn: "not in",
        }
        for comp_target in node.comparisons:
            op_str = comp_op_map.get(type(comp_target.operator))
            if op_str is None:
                return None
            comp_ir = _cst_to_rust_ir(comp_target.comparator, metavar_map)
            if comp_ir is None:
                return None
            ops_ir.append([op_str, comp_ir])
        return {"type": "compare", "left": left_ir, "ops": ops_ir}

    elif isinstance(node, cst.UnaryOperation):
        op_map = {
            cst.Minus: "-", cst.Plus: "+", cst.BitInvert: "~", cst.Not: "not",
        }
        op_str = op_map.get(type(node.operator))
        if op_str is None:
            return None
        operand_ir = _cst_to_rust_ir(node.expression, metavar_map)
        if operand_ir is None:
            return None
        return {"type": "unary_op", "op": op_str, "operand": operand_ir}

    elif isinstance(node, (cst.ListComp, cst.SetComp, cst.GeneratorExp)):
        elt_ir = _cst_to_rust_ir(node.elt, metavar_map)
        if elt_ir is None:
            return None
        generators_ir = []
        for gen in node.for_in:
            target_ir = _cst_to_rust_ir(gen.target, metavar_map)
            if target_ir is None:
                return None
            iter_ir = _cst_to_rust_ir(gen.iter, metavar_map)
            if iter_ir is None:
                return None
            ifs_ir = []
            for if_clause in gen.ifs:
                if_ir = _cst_to_rust_ir(if_clause.test, metavar_map)
                if if_ir is None:
                    return None
                ifs_ir.append(if_ir)
            generators_ir.append({
                "target": target_ir,
                "iter": iter_ir,
                "ifs": ifs_ir
            })
        kind = "list_comprehension"
        if isinstance(node, cst.SetComp):
            kind = "set_comprehension"
        elif isinstance(node, cst.GeneratorExp):
            kind = "generator_expression"
        return {
            "type": "comprehension",
            "kind": kind,
            "elt": elt_ir,
            "generators": generators_ir
        }

    elif isinstance(node, cst.DictComp):
        key_ir = _cst_to_rust_ir(node.key, metavar_map)
        if key_ir is None:
            return None
        value_ir = _cst_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        generators_ir = []
        for gen in node.for_in:
            target_ir = _cst_to_rust_ir(gen.target, metavar_map)
            if target_ir is None:
                return None
            iter_ir = _cst_to_rust_ir(gen.iter, metavar_map)
            if iter_ir is None:
                return None
            ifs_ir = []
            for if_clause in gen.ifs:
                if_ir = _cst_to_rust_ir(if_clause.test, metavar_map)
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

    elif isinstance(node, cst.Float):
        return {"type": "float", "value": node.value}

    elif isinstance(node, cst.ConcatenatedString):
        return {"type": "string", "value": None}

    elif isinstance(node, cst.FormattedString):
        parts_ir = []
        for part in node.parts:
            if isinstance(part, cst.FormattedStringText):
                parts_ir.append({"type": "fstring_text", "value": part.value})
            elif isinstance(part, cst.FormattedStringExpression):
                expr_ir = _cst_to_rust_ir(part.expression, metavar_map)
                if expr_ir is None:
                    return None
                parts_ir.append({"type": "fstring_expr", "value": expr_ir})
            else:
                return None
        return {"type": "fstring", "parts": parts_ir}

    else:
        return None


def compile_pattern_to_rust_ir(pattern_str: str) -> dict | None:
    """Compile a pattern string to Rust IR dict for the tree-sitter fast path."""
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
            expr = cst.parse_expression(parse_code)
        except Exception:
            try:
                module = cst.parse_module(parse_code)
                if module.body:
                    expr = module.body[0]
                    if isinstance(expr, cst.SimpleStatementLine) and len(expr.body) == 1:
                        expr = expr.body[0]
                    if is_try and isinstance(expr, cst.Try):
                        pass
                    elif is_except_header and isinstance(expr, cst.Try):
                        if expr.handlers:
                            expr = expr.handlers[0]
                else:
                    return None
            except Exception:
                return None

        return _cst_to_rust_ir(expr, metavar_map)

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

    stripped = constraint.rstrip()
    if stripped.endswith(":"):
        ir = compile_pattern_to_rust_ir(stripped)
        if ir is not None:
            return ir

    return None
