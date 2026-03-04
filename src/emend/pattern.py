"""Pattern parsing with metavariables."""
from __future__ import annotations
from dataclasses import dataclass, field
from lark import Lark, Transformer, Token, Tree
import importlib.resources
import libcst as cst
from libcst import matchers as m
from typing import Union


@dataclass
class MetaVar:
    name: str
    ellipsis: bool = False
    type_constraint: str | None = None


@dataclass
class Pattern:
    raw: str
    metavars: list[MetaVar]
    tree: Tree | None = field(default=None, repr=False)


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

    Examples:
        >>> pat = parse_pattern("print($MSG)")
        >>> len(pat.metavars)
        1
        >>> pat.metavars[0].name
        'MSG'

        >>> pat = parse_pattern("func($...ARGS)")
        >>> pat.metavars[0].ellipsis
        True

        >>> pat = parse_pattern("print($MSG:str)")
        >>> pat.metavars[0].type_constraint
        'str'
    """
    tree = _parser.parse(pattern_str)
    transformer = PatternTransformer()
    transformer.transform(tree)

    return Pattern(
        raw=pattern_str,
        metavars=transformer.metavars,
        tree=tree,
    )


def _comp_for_to_matcher(
    comp_for: cst.CompFor,
    metavar_map: dict[str, MetaVar],
    ellipsis_info: dict | None = None
) -> m.BaseMatcherNode:
    """Convert CompFor node to matcher.

    Args:
        comp_for: CompFor node from comprehension
        metavar_map: Mapping from placeholder name to MetaVar
        ellipsis_info: Dict to accumulate ellipsis metavar position info

    Returns:
        Matcher for the CompFor structure
    """
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
    """Parse an oracle type constraint like 'type[Connection]' or 'returns[Optional[str]]'.

    Returns:
        (kind, type_string) where kind is 'type' or 'returns'.
    """
    bracket_pos = constraint.index("[")
    kind = constraint[:bracket_pos]
    # Extract the inner type string, handling nested brackets
    inner = constraint[bracket_pos + 1:-1]
    return kind, inner


def _type_constraint_matcher(constraint: str | None) -> m.BaseMatcherNode:
    """Convert type constraint string to appropriate matcher.

    Args:
        constraint: Type constraint string like "int", "str", "!int", "!str",
            "identifier", "float", "call", "attr", "stmt", "expr", or None.
            Prefix with "!" for negation (matches anything except that type).
            Also supports "type[X]" and "returns[X]" for TypeOracle constraints,
            which match any node syntactically (post-filtered by type at match time).

    Returns:
        Matcher for the specified type
    """
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
        # Match any small statement type (for type-filtering in find operations)
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
    """Convert a comparison operator to a matcher.

    Args:
        operator: CST comparison operator node

    Returns:
        Matcher for the specific operator type
    """
    # Map operator types to their matcher equivalents
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
        # Fallback for unknown operators
        return m.DoNotCare()


def _binary_operator_to_matcher(operator: cst.BaseBinaryOp) -> m.BaseMatcherNode:
    """Convert a binary operator to a matcher.

    Args:
        operator: CST binary operator node

    Returns:
        Matcher for the specific operator type
    """
    # Map operator types to their matcher equivalents
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
        # Fallback for unknown operators
        return m.DoNotCare()


def _boolean_operator_to_matcher(operator: cst.BaseBooleanOp) -> m.BaseMatcherNode:
    """Convert a boolean operator to a matcher.

    Args:
        operator: CST boolean operator node

    Returns:
        Matcher for the specific operator type
    """
    if isinstance(operator, cst.And):
        return m.And()
    elif isinstance(operator, cst.Or):
        return m.Or()
    else:
        return m.DoNotCare()


def _unary_operator_to_matcher(operator: cst.BaseUnaryOp) -> m.BaseMatcherNode:
    """Convert a unary operator to a matcher.

    Args:
        operator: CST unary operator node

    Returns:
        Matcher for the specific operator type
    """
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
    """Convert an augmented assignment operator to a matcher.

    Args:
        operator: CST augmented assignment operator node

    Returns:
        Matcher for the specific operator type
    """
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
    """Recursively convert CST node to matcher, replacing placeholders with SaveMatchedNode.

    Args:
        node: CST node to convert
        metavar_map: Mapping from placeholder name to MetaVar
        ellipsis_info: Dict to accumulate ellipsis metavar position info (modified in place)

    Returns:
        Matcher that matches the pattern structure
    """
    if ellipsis_info is None:
        ellipsis_info = {}
    if isinstance(node, cst.Name):
        # Check if this is a metavar placeholder
        if node.value in metavar_map:
            metavar = metavar_map[node.value]
            # Use type constraint matcher if specified
            inner_matcher = _type_constraint_matcher(metavar.type_constraint)
            # $_ is anonymous - don't capture it
            if metavar.name == "_":
                return inner_matcher
            return m.SaveMatchedNode(inner_matcher, metavar.name)
        else:
            # Regular name - match exact name
            return m.Name(node.value)

    elif isinstance(node, cst.Call):
        # Match function call structure
        func_matcher = _cst_to_matcher(node.func, metavar_map, ellipsis_info)

        # Convert arguments, checking for ellipsis metavars and star args
        arg_matchers = []
        for i, arg in enumerate(node.args):
            # Check if this arg is an ellipsis placeholder
            if isinstance(arg.value, cst.Name) and arg.value.value in metavar_map:
                metavar = metavar_map[arg.value.value]
                if metavar.ellipsis:
                    # Record ellipsis position info
                    ellipsis_info[metavar.name] = {
                        "position": i,
                        "total_args": len(node.args)
                    }
                    # Use ZeroOrMore for ellipsis args
                    arg_matchers.append(m.ZeroOrMore(m.Arg()))
                    continue

            arg_value_matcher = _cst_to_matcher(arg.value, metavar_map, ellipsis_info)
            # Preserve star/double-star matching for *$X and **$X patterns
            if arg.star in ("*", "**"):
                arg_matchers.append(m.Arg(value=arg_value_matcher, star=arg.star))
            else:
                arg_matchers.append(m.Arg(value=arg_value_matcher))

        return m.Call(func=func_matcher, args=arg_matchers)

    elif isinstance(node, cst.SimpleString):
        # Match exact string literal
        return m.SimpleString(node.value)

    elif isinstance(node, cst.Integer):
        # Match exact integer literal
        return m.Integer(node.value)

    elif isinstance(node, cst.Float):
        # Match exact float literal
        return m.Float(node.value)

    elif isinstance(node, cst.Attribute):
        # Match attribute access like obj.attr
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        attr_matcher = _cst_to_matcher(node.attr, metavar_map, ellipsis_info)
        return m.Attribute(value=value_matcher, attr=attr_matcher)

    elif isinstance(node, cst.Arg):
        # Match function argument
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        return m.Arg(value=value_matcher)

    elif isinstance(node, cst.Comparison):
        # Match comparison operations like $A == $B, $X is None
        left_matcher = _cst_to_matcher(node.left, metavar_map, ellipsis_info)
        # Convert each comparison target (operator + comparator)
        comparison_matchers = []
        for target in node.comparisons:
            comparator_matcher = _cst_to_matcher(target.comparator, metavar_map, ellipsis_info)
            # Match the specific operator type from the pattern
            operator_matcher = _operator_to_matcher(target.operator)
            comparison_matchers.append(
                m.ComparisonTarget(
                    operator=operator_matcher,
                    comparator=comparator_matcher
                )
            )
        return m.Comparison(left=left_matcher, comparisons=comparison_matchers)

    elif isinstance(node, cst.BinaryOperation):
        # Match binary operations like $A + $B, $X * $Y
        left_matcher = _cst_to_matcher(node.left, metavar_map, ellipsis_info)
        right_matcher = _cst_to_matcher(node.right, metavar_map, ellipsis_info)
        operator_matcher = _binary_operator_to_matcher(node.operator)
        return m.BinaryOperation(left=left_matcher, operator=operator_matcher, right=right_matcher)

    elif isinstance(node, cst.BooleanOperation):
        # Match boolean operations like $A and $B, $A or $B
        left_matcher = _cst_to_matcher(node.left, metavar_map, ellipsis_info)
        right_matcher = _cst_to_matcher(node.right, metavar_map, ellipsis_info)
        operator_matcher = _boolean_operator_to_matcher(node.operator)
        return m.BooleanOperation(left=left_matcher, operator=operator_matcher, right=right_matcher)

    elif isinstance(node, cst.UnaryOperation):
        # Match unary operations like not $X, -$X
        expression_matcher = _cst_to_matcher(node.expression, metavar_map, ellipsis_info)
        operator_matcher = _unary_operator_to_matcher(node.operator)
        return m.UnaryOperation(operator=operator_matcher, expression=expression_matcher)

    elif isinstance(node, cst.Subscript):
        # Match subscript access like $X[$Y]
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        # Handle different slice types
        slice_matchers = []
        for slice_element in node.slice:
            if isinstance(slice_element, cst.SubscriptElement):
                slice_item_matcher = _cst_to_matcher(slice_element.slice, metavar_map, ellipsis_info)
                slice_matchers.append(m.SubscriptElement(slice=slice_item_matcher))
            else:
                slice_matchers.append(m.DoNotCare())
        return m.Subscript(value=value_matcher, slice=slice_matchers)

    elif isinstance(node, cst.Index):
        # Match index nodes (used in subscripts)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        return m.Index(value=value_matcher)

    elif isinstance(node, cst.IfExp):
        # Match ternary expressions like $A if $B else $C
        body_matcher = _cst_to_matcher(node.body, metavar_map, ellipsis_info)
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        orelse_matcher = _cst_to_matcher(node.orelse, metavar_map, ellipsis_info)
        return m.IfExp(body=body_matcher, test=test_matcher, orelse=orelse_matcher)

    elif isinstance(node, cst.Await):
        # Match await expressions like await $X
        expression_matcher = _cst_to_matcher(node.expression, metavar_map, ellipsis_info)
        return m.Await(expression=expression_matcher)

    elif isinstance(node, cst.Tuple):
        # Match tuples like ($A, $B) or ($FIRST, $...REST)
        element_matchers = []
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.Element):
                # Check if this element is an ellipsis metavar
                if isinstance(elem.value, cst.Name) and elem.value.value in metavar_map:
                    metavar = metavar_map[elem.value.value]
                    if metavar.ellipsis:
                        # Record ellipsis position info
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        # Use ZeroOrMore for ellipsis elements
                        element_matchers.append(m.ZeroOrMore(m.Element()))
                        continue

                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.Element(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())
        return m.Tuple(elements=element_matchers)

    elif isinstance(node, cst.List):
        # Match lists like [$A, $B] or [$FIRST, $...REST]
        element_matchers = []
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.Element):
                # Check if this element is an ellipsis metavar
                if isinstance(elem.value, cst.Name) and elem.value.value in metavar_map:
                    metavar = metavar_map[elem.value.value]
                    if metavar.ellipsis:
                        # Record ellipsis position info
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        # Use ZeroOrMore for ellipsis elements
                        element_matchers.append(m.ZeroOrMore(m.Element()))
                        continue

                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.Element(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())
        return m.List(elements=element_matchers)

    elif isinstance(node, cst.Set):
        # Match sets like {$A, $B} or {$FIRST, $...REST}
        element_matchers = []
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.Element):
                # Check if this element is an ellipsis metavar
                if isinstance(elem.value, cst.Name) and elem.value.value in metavar_map:
                    metavar = metavar_map[elem.value.value]
                    if metavar.ellipsis:
                        # Record ellipsis position info
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        # Use ZeroOrMore for ellipsis elements
                        element_matchers.append(m.ZeroOrMore(m.Element()))
                        continue

                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.Element(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())
        return m.Set(elements=element_matchers)

    elif isinstance(node, cst.Dict):
        # Match dict literals like {$K: $V} or {$K: $V, $...REST}
        # Also handles partial dict matching with ... (spread)
        element_matchers = []
        has_partial_spread = False
        for i, elem in enumerate(node.elements):
            if isinstance(elem, cst.DictElement):
                # Check if the key is an ellipsis metavar (for dict ellipsis we check the key)
                if isinstance(elem.key, cst.Name) and elem.key.value in metavar_map:
                    metavar = metavar_map[elem.key.value]
                    if metavar.ellipsis:
                        # Record ellipsis position info
                        ellipsis_info[metavar.name] = {
                            "position": i,
                            "total_elements": len(node.elements)
                        }
                        # Use ZeroOrMore for ellipsis elements
                        element_matchers.append(m.ZeroOrMore(m.DictElement()))
                        continue

                key_matcher = _cst_to_matcher(elem.key, metavar_map, ellipsis_info)
                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.DictElement(key=key_matcher, value=value_matcher))
            elif isinstance(elem, cst.StarredDictElement):
                # Check if this is a partial dict spread marker (**__EMEND_SPREAD__)
                if isinstance(elem.value, cst.Name) and elem.value.value == "__EMEND_SPREAD__":
                    has_partial_spread = True
                    continue
                value_matcher = _cst_to_matcher(elem.value, metavar_map, ellipsis_info)
                element_matchers.append(m.StarredDictElement(value=value_matcher))
            else:
                element_matchers.append(m.DoNotCare())

        if has_partial_spread:
            # Build partial dict matcher using MatchIfTrue on elements
            # to allow keys in any order and extra keys
            required = element_matchers[:]

            def _make_checker(req_matchers):
                def check_elements(elements):
                    for req in req_matchers:
                        if not any(m.matches(e, req) for e in elements):
                            return False
                    return True
                return check_elements

            # Store per-element matchers for manual capture extraction
            ellipsis_info["__partial_dict__"] = {
                "element_matchers": required,
            }
            return m.Dict(elements=m.MatchIfTrue(_make_checker(required)))

        return m.Dict(elements=element_matchers)

    elif isinstance(node, cst.Lambda):
        # Match lambda expressions like 'lambda $X: $EXPR' or 'lambda: $EXPR'
        body_matcher = _cst_to_matcher(node.body, metavar_map, ellipsis_info)

        # Build params matcher from the pattern's lambda params
        params = node.params
        all_params = list(params.params)
        has_star_arg = isinstance(params.star_arg, cst.Param)
        has_star_kwarg = params.star_kwarg is not None

        if not all_params and not has_star_arg and not has_star_kwarg and not params.kwonly_params:
            # Pattern is 'lambda: ...' — match only parameterless lambdas
            params_matcher = m.Parameters(params=[], kwonly_params=[], star_kwarg=None)
        else:
            # Match each param name (metavar or literal)
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
                # $...ARGS matches any number of params
                params_matcher = m.DoNotCare()
            else:
                params_kwargs = dict(params=param_matchers)

                # Handle *$ARGS pattern in lambda
                if has_star_arg:
                    star_param = params.star_arg
                    if isinstance(star_param.name, cst.Name) and star_param.name.value in metavar_map:
                        metavar = metavar_map[star_param.name.value]
                        star_name_matcher = m.SaveMatchedNode(m.Name(), metavar.name)
                    else:
                        star_name_matcher = m.Name(star_param.name.value)
                    params_kwargs["star_arg"] = m.Param(name=star_name_matcher, star="*")
                    # If pattern has no **kwargs, restrict to lambdas without star_kwarg
                    if not has_star_kwarg:
                        params_kwargs["star_kwarg"] = None

                # Handle **$KWARGS pattern in lambda
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
        # Match function definitions like 'def $FUNC($...ARGS):' or '@$DEC\ndef $FUNC(...):'
        name_matcher = _cst_to_matcher(node.name, metavar_map, ellipsis_info)

        # Handle parameters
        params = node.params
        has_ellipsis_params = False

        # Check all param categories for metavar placeholders
        all_params = list(params.params) + list(params.posonly_params or []) + list(params.kwonly_params or [])
        for p in all_params:
            if isinstance(p.name, cst.Name) and p.name.value in metavar_map:
                metavar = metavar_map[p.name.value]
                if metavar.ellipsis:
                    has_ellipsis_params = True

        if has_ellipsis_params:
            # $...ARGS means "match any params"
            param_matchers_node = m.DoNotCare()
        else:
            param_matchers_node = m.DoNotCare()

        kwargs = dict(
            name=name_matcher,
            params=param_matchers_node,
            body=m.DoNotCare(),
            leading_lines=m.DoNotCare(),
            lines_after_decorators=m.DoNotCare(),
        )

        # Handle async keyword
        if node.asynchronous is not None:
            kwargs["asynchronous"] = m.Asynchronous()

        # Handle decorators
        if node.decorators:
            dec_matchers = []
            for dec in node.decorators:
                dec_expr_matcher = _cst_to_matcher(dec.decorator, metavar_map, ellipsis_info)
                dec_matchers.append(m.Decorator(decorator=dec_expr_matcher))
            kwargs["decorators"] = dec_matchers

        return m.FunctionDef(**kwargs)

    elif isinstance(node, cst.If):
        # Match if statements like 'if $COND:'
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        return m.If(test=test_matcher, body=m.DoNotCare(), leading_lines=m.DoNotCare(), orelse=m.DoNotCare())

    elif isinstance(node, cst.While):
        # Match while statements like 'while $COND:'
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        return m.While(test=test_matcher, body=m.DoNotCare(), leading_lines=m.DoNotCare(), orelse=m.DoNotCare())

    elif isinstance(node, cst.For):
        # Match for statements like 'for $VAR in $ITER:' or 'async for $VAR in $ITER:'
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        iter_matcher = _cst_to_matcher(node.iter, metavar_map, ellipsis_info)
        kwargs = dict(target=target_matcher, iter=iter_matcher, body=m.DoNotCare(), leading_lines=m.DoNotCare(), orelse=m.DoNotCare())
        if node.asynchronous is not None:
            # Pattern is 'async for ...' - only match async for loops
            kwargs["asynchronous"] = m.Asynchronous()
        return m.For(**kwargs)

    elif isinstance(node, cst.With):
        # Match with statements like 'with $CTX as $VAR:' or 'async with $CTX as $VAR:'
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
            # Pattern is 'async with ...' - only match async with statements
            kwargs["asynchronous"] = m.Asynchronous()
        return m.With(**kwargs)

    elif isinstance(node, cst.Try):
        # Match try: block - body is ignored, just matches any try statement
        return m.Try(body=m.DoNotCare(), leading_lines=m.DoNotCare())

    elif isinstance(node, cst.ExceptHandler):
        # Match except clauses like 'except $E:' or 'except $E as $VAR:'
        if node.type is not None:
            type_matcher = _cst_to_matcher(node.type, metavar_map, ellipsis_info)
            if node.name is not None:
                # 'except $E as $VAR:'
                name_matcher = _cst_to_matcher(node.name.name, metavar_map, ellipsis_info)
                return m.ExceptHandler(
                    type=type_matcher,
                    name=m.AsName(name=name_matcher),
                    body=m.DoNotCare(),
                )
            else:
                # 'except $E:'
                return m.ExceptHandler(type=type_matcher, body=m.DoNotCare())
        else:
            # bare 'except:'
            return m.ExceptHandler(type=None, body=m.DoNotCare())

    elif isinstance(node, cst.NamedExpr):
        # Match walrus operator like ($X := $Y)
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        return m.NamedExpr(target=target_matcher, value=value_matcher)

    elif isinstance(node, cst.Assign):
        # Match assignment like $X = $Y
        # Note: Assign can have multiple targets (a = b = c), we match the first
        target_matcher = _cst_to_matcher(node.targets[0].target, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        # Match with flexible target count (to handle single or chained assignments)
        return m.Assign(
            targets=[m.AtLeastN(n=0), m.AssignTarget(target=target_matcher), m.AtLeastN(n=0)],
            value=value_matcher
        )

    elif isinstance(node, cst.AugAssign):
        # Match augmented assignment like $X += $Y
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        # Match the operator
        operator_matcher = _aug_operator_to_matcher(node.operator)
        return m.AugAssign(target=target_matcher, operator=operator_matcher, value=value_matcher)

    elif isinstance(node, cst.Return):
        # Match return statements like return $X
        if node.value is not None:
            value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
            return m.Return(value=value_matcher)
        else:
            return m.Return(value=m.DoNotCare())

    elif isinstance(node, cst.Assert):
        # Match assert statements like assert $X or assert $X, $Y
        test_matcher = _cst_to_matcher(node.test, metavar_map, ellipsis_info)
        if node.msg is not None:
            msg_matcher = _cst_to_matcher(node.msg, metavar_map, ellipsis_info)
            return m.Assert(test=test_matcher, msg=msg_matcher)
        else:
            return m.Assert(test=test_matcher, msg=m.DoNotCare())

    elif isinstance(node, cst.Raise):
        # Match raise statements like raise $X
        if node.exc is not None:
            exc_matcher = _cst_to_matcher(node.exc, metavar_map, ellipsis_info)
            return m.Raise(exc=exc_matcher)
        else:
            return m.Raise(exc=m.DoNotCare())

    elif isinstance(node, cst.Del):
        # Match del statements like del $X
        target_matcher = _cst_to_matcher(node.target, metavar_map, ellipsis_info)
        return m.Del(target=target_matcher)

    elif isinstance(node, cst.Global):
        # Match global statements like global $X
        # Global has names which is a sequence of NameItem
        # For simplicity, match on the first name if pattern has single metavar
        if len(node.names) == 1:
            name_item = node.names[0]
            name_matcher = _cst_to_matcher(name_item.name, metavar_map, ellipsis_info)
            return m.Global(names=[m.NameItem(name=name_matcher)])
        else:
            # Multiple names - match with DoNotCare for now
            return m.Global()

    elif isinstance(node, cst.Nonlocal):
        # Match nonlocal statements like nonlocal $X
        # Nonlocal has same structure as Global
        if len(node.names) == 1:
            name_item = node.names[0]
            name_matcher = _cst_to_matcher(name_item.name, metavar_map, ellipsis_info)
            return m.Nonlocal(names=[m.NameItem(name=name_matcher)])
        else:
            # Multiple names - match with DoNotCare for now
            return m.Nonlocal()

    elif isinstance(node, cst.ImportFrom):
        # Match 'from ... import ...' statements like from $MOD import $NAME
        # Module can be Name (simple) or Attribute (dotted like os.path)
        if node.module is not None:
            module_matcher = _cst_to_matcher(node.module, metavar_map, ellipsis_info)
        else:
            module_matcher = m.DoNotCare()

        # Handle names - for single name patterns, match first ImportAlias
        if not isinstance(node.names, cst.ImportStar) and len(node.names) == 1:
            import_alias = node.names[0]
            name_matcher = _cst_to_matcher(import_alias.name, metavar_map, ellipsis_info)
            # Check if pattern has asname
            if import_alias.asname is not None:
                asname_matcher = _cst_to_matcher(import_alias.asname.name, metavar_map, ellipsis_info)
                return m.ImportFrom(
                    module=module_matcher,
                    names=[m.ImportAlias(name=name_matcher, asname=m.AsName(name=asname_matcher))]
                )
            else:
                return m.ImportFrom(
                    module=module_matcher,
                    names=[m.ImportAlias(name=name_matcher)]
                )
        else:
            # Multiple names or star import - match with DoNotCare
            return m.ImportFrom(module=module_matcher)

    elif isinstance(node, cst.Import):
        # Match 'import' statements like import $MOD or import $MOD as $ALIAS
        if len(node.names) == 1:
            import_alias = node.names[0]
            name_matcher = _cst_to_matcher(import_alias.name, metavar_map, ellipsis_info)
            # Check if pattern has asname
            if import_alias.asname is not None:
                asname_matcher = _cst_to_matcher(import_alias.asname.name, metavar_map, ellipsis_info)
                return m.Import(names=[m.ImportAlias(name=name_matcher, asname=m.AsName(name=asname_matcher))])
            else:
                return m.Import(names=[m.ImportAlias(name=name_matcher)])
        else:
            # Multiple names - match with DoNotCare
            return m.Import()

    elif isinstance(node, cst.ListComp):
        # Match list comprehensions like [$X for $X in $Y]
        elt_matcher = _cst_to_matcher(node.elt, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.ListComp(elt=elt_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.SetComp):
        # Match set comprehensions like {$X for $X in $Y}
        elt_matcher = _cst_to_matcher(node.elt, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.SetComp(elt=elt_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.DictComp):
        # Match dict comprehensions like {$K: $V for $K, $V in $ITEMS}
        key_matcher = _cst_to_matcher(node.key, metavar_map, ellipsis_info)
        value_matcher = _cst_to_matcher(node.value, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.DictComp(key=key_matcher, value=value_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.GeneratorExp):
        # Match generator expressions like ($X for $X in $Y)
        elt_matcher = _cst_to_matcher(node.elt, metavar_map, ellipsis_info)
        for_in_matcher = _comp_for_to_matcher(node.for_in, metavar_map, ellipsis_info)
        return m.GeneratorExp(elt=elt_matcher, for_in=for_in_matcher)

    elif isinstance(node, cst.FormattedString):
        # Match f-strings like f"hello {$X}"
        part_matchers = []
        for part in node.parts:
            if isinstance(part, cst.FormattedStringText):
                # Literal text - match exactly
                part_matchers.append(m.FormattedStringText(part.value))
            elif isinstance(part, cst.FormattedStringExpression):
                # Interpolation - recursively match the expression
                expr_matcher = _cst_to_matcher(part.expression, metavar_map, ellipsis_info)
                part_matchers.append(m.FormattedStringExpression(expression=expr_matcher))
        return m.FormattedString(parts=part_matchers)

    else:
        # For other node types, use DoNotCare for now
        # This is a simplified implementation
        return m.DoNotCare()


import re as _re

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
    """Check if code looks like a compound statement header (ends with ':').

    Handles: if/elif/while/for/with headers, and also
    decorated or undecorated def/class headers.
    """
    if _COMPOUND_HEADER_RE.match(code):
        return True
    # Check for decorated def/class: starts with @ and ends with :
    if _DEF_HEADER_RE.search(code) and code.rstrip().endswith(":"):
        return True
    return False


def _is_except_header(code: str) -> bool:
    """Check if code is an except clause header like 'except $E:' or 'except $E as $VAR:'."""
    return bool(_EXCEPT_HEADER_RE.match(code))


def compile_pattern_to_matcher(pattern: Pattern) -> tuple[m.BaseMatcherNode, dict]:
    """Compile parsed pattern to LibCST matcher for finding matches.

    Args:
        pattern: Parsed pattern with metavariables

    Returns:
        Tuple of (matcher, ellipsis_info) where:
        - matcher: LibCST matcher that can be used to find matches
        - ellipsis_info: Dict mapping ellipsis metavar names to position info

    Example:
        >>> pat = parse_pattern("print($X)")
        >>> matcher, ellipsis_info = compile_pattern_to_matcher(pat)
        >>> # Use matcher with libcst.matchers.matches(node, matcher)
    """
    # Step 1: Replace metavars with valid Python identifiers
    # Process longest patterns first to avoid partial replacements
    # Order: $...NAME:type > $...NAME > $NAME:type > $NAME
    temp_code = pattern.raw
    metavar_map = {}

    # Sort metavars by replacement pattern length (longest first)
    sorted_metavars = sorted(pattern.metavars, key=lambda mv: (
        -len(f"$...{mv.name}:{mv.type_constraint or ''}"),
        -len(f"$...{mv.name}"),
        -len(f"${mv.name}:{mv.type_constraint or ''}"),
        -len(f"${mv.name}")
    ))

    for mv in sorted_metavars:
        placeholder = f"__META_{mv.name}__"
        metavar_map[placeholder] = mv

        # Build the full metavar pattern to replace
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
    # This makes `{__META_KEY__: __META_VAL__, __META_REST__}` valid as
    # `{__META_KEY__: __META_VAL__, __META_REST__: None}`
    for mv in sorted_metavars:
        if not mv.ellipsis:
            continue
        placeholder = f"__META_{mv.name}__"
        idx = temp_code.find(placeholder)
        if idx == -1:
            continue

        # Check if placeholder is already followed by `:` (already has a value)
        after_placeholder = temp_code[idx + len(placeholder):].lstrip()
        if after_placeholder.startswith(':'):
            continue

        # Scan backward to find enclosing `{` to detect dict context
        brace_depth = 0
        for i in range(idx - 1, -1, -1):
            c = temp_code[i]
            if c == '}':
                brace_depth += 1
            elif c == '{':
                if brace_depth == 0:
                    # Found the enclosing brace - check if dict context
                    segment = temp_code[i + 1:idx]
                    if ':' in segment:  # Dict context (has key:value pairs)
                        # Append `: None` to make it a valid dict element
                        temp_code = (
                            temp_code[:idx + len(placeholder)]
                            + ': None'
                            + temp_code[idx + len(placeholder):]
                        )
                    break
                brace_depth -= 1

    # Step 1b: Replace literal `...` in dict context with `**__EMEND_SPREAD__`
    # This makes patterns like `{'type': 'user', ...}` parseable as Python
    temp_code = _re.sub(
        r'\.\.\.\s*}',
        '**__EMEND_SPREAD__}',
        temp_code
    )

    # Step 2: Detect compound statement headers and add dummy body
    # Patterns like "if $COND:", "for $VAR in $ITER:", "while $COND:", "with $CTX:"
    # Also handle "try:" and "except $E:" / "except $E as $VAR:"
    is_except_header = _is_except_header(temp_code)
    is_compound_header = _is_compound_statement_header(temp_code)
    is_try = temp_code.strip() == "try:"
    parse_code = temp_code
    if is_try:
        # Wrap as a minimal try/except block
        parse_code = "try:\n    pass\nexcept Exception:\n    pass"
    elif is_except_header:
        # Wrap the except clause in a try block to make it parseable
        parse_code = "try:\n    pass\n" + temp_code + "\n    pass"
    elif is_compound_header:
        parse_code = temp_code + "\n    pass"

    # Step 3: Parse as Python expression
    try:
        expr = cst.parse_expression(parse_code)
    except Exception:
        # If it doesn't parse as expression, try as statement
        try:
            module = cst.parse_module(parse_code)
            # Extract first statement
            if module.body:
                expr = module.body[0]
                # Unwrap SimpleStatementLine to get the actual statement
                if isinstance(expr, cst.SimpleStatementLine) and len(expr.body) == 1:
                    expr = expr.body[0]
                # For try/except wrapper: extract the relevant node
                if is_try and isinstance(expr, cst.Try):
                    # Keep the Try node as-is for matching
                    pass
                elif is_except_header and isinstance(expr, cst.Try):
                    # Extract the first ExceptHandler from the wrapped try block
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

    return temp_code, metavar_map


def _compile_generators_to_ir(
    for_in, metavar_map: dict[str, MetaVar],
) -> list[dict] | None:
    """Compile comprehension generators to Rust IR dicts."""
    generators_ir: list[dict] = []
    for gen in for_in:
        target_ir = _cst_to_rust_ir(gen.target, metavar_map)
        if target_ir is None:
            return None
        iter_ir = _cst_to_rust_ir(gen.iter, metavar_map)
        if iter_ir is None:
            return None
        ifs_ir: list[dict] = []
        for if_clause in gen.ifs:
            if_ir = _cst_to_rust_ir(if_clause.test, metavar_map)
            if if_ir is None:
                return None
            ifs_ir.append(if_ir)
        generators_ir.append({"target": target_ir, "iter": iter_ir, "ifs": ifs_ir})
    return generators_ir


def _compile_decorators_to_ir(
    decorators, metavar_map: dict[str, MetaVar],
) -> list[dict] | None:
    """Compile decorators to Rust IR dicts."""
    decorators_ir: list[dict] = []
    for dec in decorators:
        dec_ir = _cst_to_rust_ir(dec.decorator, metavar_map)
        if dec_ir is None:
            return None
        decorators_ir.append(dec_ir)
    return decorators_ir


def _cst_to_rust_ir(node: cst.CSTNode, metavar_map: dict[str, MetaVar]) -> dict | None:
    """Convert a LibCST node to a Rust IR dict.

    Returns None if the node type is not yet supported by the Rust matcher.
    """
    if isinstance(node, cst.Name):
        if node.value in metavar_map:
            metavar = metavar_map[node.value]
            # Type constraints support
            if metavar.type_constraint in (":int", ":str", ":call"):
                return {"type": "type_constraint", "kind": metavar.type_constraint[1:]}
            if metavar.type_constraint is not None:
                return None
            if metavar.ellipsis:
                return {"type": "ellipsis"}
            else:
                return {"type": "any_expr"}
        else:
            # Map Python builtins to tree-sitter node types
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
            # Check if this is an ellipsis metavar in arg position
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
                # keyword arguments are handled as special IR if needed,
                # but currently Rust's KeywordArg is a PatternNode,
                # whereas Call args are ArgPattern.
                # Actually, tree-sitter treats keyword_argument as a named node.
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
                return None  # StarredElement or other unsupported
        return {"type": "list", "elements": elems_ir}

    elif isinstance(node, cst.FunctionDef):
        # Async functions not yet distinguished in Rust matcher — fall back
        if node.asynchronous is not None:
            return None

        name_ir = _cst_to_rust_ir(node.name, metavar_map)
        if name_ir is None:
            return None

        decorators_ir = _compile_decorators_to_ir(node.decorators, metavar_map)
        if decorators_ir is None:
            return None

        params = node.params
        param_patterns = []
        all_params = list(params.params) + list(params.posonly_params or []) + list(params.kwonly_params or [])

        for p in all_params:
            if isinstance(p.name, cst.Name) and p.name.value in metavar_map:
                metavar = metavar_map[p.name.value]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                    continue
                # Non-ellipsis metavar param
                if p.default is not None:
                    dv_ir = _cst_to_rust_ir(p.default, metavar_map)
                    if dv_ir is None:
                        return None
                    param_patterns.append({"type": "with_default", "default_value": dv_ir})
                else:
                    param_patterns.append({"type": "any"})
            else:
                # Literal param name
                if p.default is not None:
                    dv_ir = _cst_to_rust_ir(p.default, metavar_map)
                    if dv_ir is None:
                        return None
                    param_patterns.append({"type": "with_default", "default_value": dv_ir})
                else:
                    pname = p.name.value if isinstance(p.name, cst.Name) else str(p.name)
                    param_patterns.append({"type": "name", "value": pname})

        # Handle *args and **kwargs in params
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
            "decorators": decorators_ir
        }

    elif isinstance(node, cst.ClassDef):
        name_ir = _cst_to_rust_ir(node.name, metavar_map)
        if name_ir is None:
            return None

        decorators_ir = _compile_decorators_to_ir(node.decorators, metavar_map)
        if decorators_ir is None:
            return None

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

    elif isinstance(node, (cst.BinaryOperation, cst.BooleanOperation)):
        left_ir = _cst_to_rust_ir(node.left, metavar_map)
        if left_ir is None:
            return None
        right_ir = _cst_to_rust_ir(node.right, metavar_map)
        if right_ir is None:
            return None
        # Map the operator to a string
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
        # Map the operator to a string
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

        generators_ir = _compile_generators_to_ir(node.for_in, metavar_map)
        if generators_ir is None:
            return None

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

        generators_ir = _compile_generators_to_ir(node.for_in, metavar_map)
        if generators_ir is None:
            return None

        return {
            "type": "dict_comprehension",
            "key": key_ir,
            "value": value_ir,
            "generators": generators_ir
        }

    elif isinstance(node, cst.Float):
        return {"type": "string", "value": node.value}

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
        # Unsupported node type — fall back to LibCST path
        return None


# ---------------------------------------------------------------------------
# Lark → stdlib ast → Rust IR path (bypasses LibCST parsing)
# ---------------------------------------------------------------------------

import ast as _stdlib_ast


def _ast_to_rust_ir(node: _stdlib_ast.AST, metavar_map: dict[str, MetaVar]) -> dict | None:
    """Convert a stdlib ast node to a Rust IR dict.

    Returns None if the node type is not yet supported by the Rust matcher.
    This mirrors _cst_to_rust_ir but uses the stdlib ast module instead of LibCST.
    """
    if isinstance(node, _stdlib_ast.Name):
        name = node.id
        if name in metavar_map:
            metavar = metavar_map[name]
            # Type constraints not yet supported in Rust matcher — fall back
            if metavar.type_constraint is not None:
                return None
            if metavar.ellipsis:
                return {"type": "ellipsis"}
            else:
                return {"type": "any_expr"}
        else:
            if name == "None":
                return {"type": "none"}
            if name == "True":
                return {"type": "bool", "value": True}
            if name == "False":
                return {"type": "bool", "value": False}
            return {"type": "name", "value": name}

    elif isinstance(node, _stdlib_ast.Constant):
        v = node.value
        if v is None:
            return {"type": "none"}
        if v is True:
            return {"type": "bool", "value": True}
        if v is False:
            return {"type": "bool", "value": False}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "string", "value": repr(v)}
        if isinstance(v, str):
            return {"type": "string", "value": repr(v)}
        if isinstance(v, bytes):
            return {"type": "string", "value": repr(v)}
        # Ellipsis literal, complex, etc.
        return None

    elif isinstance(node, _stdlib_ast.Call):
        func_ir = _ast_to_rust_ir(node.func, metavar_map)
        if func_ir is None:
            return None

        for kw in node.keywords:
            if kw.arg is None:
                # **kwargs spread — not supported
                return None

        args_ir = []
        has_ellipsis = False
        for arg in node.args:
            # Check for starred arg (*arg)
            if isinstance(arg, _stdlib_ast.Starred):
                return None
            # Check if this is an ellipsis metavar
            if isinstance(arg, _stdlib_ast.Name) and arg.id in metavar_map:
                metavar = metavar_map[arg.id]
                if metavar.ellipsis:
                    args_ir.append({"type": "ellipsis"})
                    has_ellipsis = True
                    continue
            arg_ir = _ast_to_rust_ir(arg, metavar_map)
            if arg_ir is None:
                return None
            args_ir.append(arg_ir)

        # Keyword arguments: if the pattern has keyword args, fall back
        if node.keywords:
            return None

        return {
            "type": "call",
            "func": func_ir,
            "args": args_ir,
            "exact_args": not has_ellipsis,
        }

    elif isinstance(node, _stdlib_ast.Attribute):
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        return {"type": "attr", "value": value_ir, "attr": node.attr}

    elif isinstance(node, _stdlib_ast.List):
        if len(node.elts) == 0:
            return {"type": "empty_list"}
        elems_ir = []
        for elt in node.elts:
            if isinstance(elt, _stdlib_ast.Starred):
                return None
            e_ir = _ast_to_rust_ir(elt, metavar_map)
            if e_ir is None:
                return None
            elems_ir.append(e_ir)
        return {"type": "list", "elements": elems_ir}

    elif isinstance(node, _stdlib_ast.Tuple):
        elems_ir = []
        for elt in node.elts:
            if isinstance(elt, _stdlib_ast.Starred):
                return None
            e_ir = _ast_to_rust_ir(elt, metavar_map)
            if e_ir is None:
                return None
            elems_ir.append(e_ir)
        return {"type": "tuple", "elements": elems_ir}

    elif isinstance(node, _stdlib_ast.Subscript):
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        slice_node = node.slice
        # dict[K, V] → slice is a Tuple; expand to multiple slices (mirrors LibCST behavior)
        if isinstance(slice_node, _stdlib_ast.Tuple):
            slices_ir = []
            for elt in slice_node.elts:
                if isinstance(elt, _stdlib_ast.Starred):
                    return None
                s_ir = _ast_to_rust_ir(elt, metavar_map)
                if s_ir is None:
                    return None
                slices_ir.append(s_ir)
            return {"type": "subscript", "value": value_ir, "slices": slices_ir}
        slice_ir = _ast_to_rust_ir(slice_node, metavar_map)
        if slice_ir is None:
            return None
        return {"type": "subscript", "value": value_ir, "slices": [slice_ir]}

    elif isinstance(node, _stdlib_ast.BinOp):
        left_ir = _ast_to_rust_ir(node.left, metavar_map)
        if left_ir is None:
            return None
        right_ir = _ast_to_rust_ir(node.right, metavar_map)
        if right_ir is None:
            return None
        op_map = {
            _stdlib_ast.Add: "+", _stdlib_ast.Sub: "-", _stdlib_ast.Mult: "*",
            _stdlib_ast.Div: "/", _stdlib_ast.FloorDiv: "//", _stdlib_ast.Mod: "%",
            _stdlib_ast.Pow: "**", _stdlib_ast.BitAnd: "&", _stdlib_ast.BitOr: "|",
            _stdlib_ast.BitXor: "^", _stdlib_ast.LShift: "<<", _stdlib_ast.RShift: ">>",
            _stdlib_ast.MatMult: "@",
        }
        op_str = op_map.get(type(node.op))
        if op_str is None:
            return None
        return {"type": "binary_op", "left": left_ir, "op": op_str, "right": right_ir}

    elif isinstance(node, _stdlib_ast.BoolOp):
        if len(node.values) != 2:
            # Only binary boolean ops supported
            return None
        left_ir = _ast_to_rust_ir(node.values[0], metavar_map)
        if left_ir is None:
            return None
        right_ir = _ast_to_rust_ir(node.values[1], metavar_map)
        if right_ir is None:
            return None
        op_map = {_stdlib_ast.And: "and", _stdlib_ast.Or: "or"}
        op_str = op_map.get(type(node.op))
        if op_str is None:
            return None
        return {"type": "binary_op", "left": left_ir, "op": op_str, "right": right_ir}

    elif isinstance(node, _stdlib_ast.UnaryOp):
        op_map = {
            _stdlib_ast.USub: "-", _stdlib_ast.UAdd: "+",
            _stdlib_ast.Invert: "~", _stdlib_ast.Not: "not",
        }
        op_str = op_map.get(type(node.op))
        if op_str is None:
            return None
        operand_ir = _ast_to_rust_ir(node.operand, metavar_map)
        if operand_ir is None:
            return None
        return {"type": "unary_op", "op": op_str, "operand": operand_ir}

    elif isinstance(node, _stdlib_ast.Compare):
        left_ir = _ast_to_rust_ir(node.left, metavar_map)
        if left_ir is None:
            return None
        comp_op_map = {
            _stdlib_ast.Eq: "==", _stdlib_ast.NotEq: "!=",
            _stdlib_ast.Lt: "<", _stdlib_ast.Gt: ">",
            _stdlib_ast.LtE: "<=", _stdlib_ast.GtE: ">=",
            _stdlib_ast.Is: "is", _stdlib_ast.IsNot: "is not",
            _stdlib_ast.In: "in", _stdlib_ast.NotIn: "not in",
        }
        ops_ir = []
        for op, comparator in zip(node.ops, node.comparators):
            op_str = comp_op_map.get(type(op))
            if op_str is None:
                return None
            comp_ir = _ast_to_rust_ir(comparator, metavar_map)
            if comp_ir is None:
                return None
            ops_ir.append([op_str, comp_ir])
        return {"type": "compare", "left": left_ir, "ops": ops_ir}

    elif isinstance(node, _stdlib_ast.Assign):
        if len(node.targets) != 1:
            return None
        target_ir = _ast_to_rust_ir(node.targets[0], metavar_map)
        if target_ir is None:
            return None
        value_ir = _ast_to_rust_ir(node.value, metavar_map)
        if value_ir is None:
            return None
        return {"type": "assign", "target": target_ir, "value": value_ir}

    elif isinstance(node, _stdlib_ast.FunctionDef):
        if node.decorator_list:
            return None
        name_ir = _ast_to_rust_ir(_stdlib_ast.Name(id=node.name, ctx=_stdlib_ast.Load()), metavar_map)
        if name_ir is None:
            return None

        args = node.args
        param_patterns = []
        all_params = list(args.posonlyargs or []) + list(args.args) + list(args.kwonlyargs or [])

        for p in all_params:
            placeholder = p.arg
            if placeholder in metavar_map:
                metavar = metavar_map[placeholder]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                    continue
                if p.annotation is not None:
                    return None
                param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "name", "value": placeholder})

        if args.vararg is not None:
            placeholder = args.vararg.arg
            if placeholder in metavar_map:
                metavar = metavar_map[placeholder]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "any"})

        if args.kwarg is not None:
            placeholder = args.kwarg.arg
            if placeholder in metavar_map:
                metavar = metavar_map[placeholder]
                if metavar.ellipsis:
                    param_patterns.append({"type": "ellipsis"})
                else:
                    param_patterns.append({"type": "any"})
            else:
                param_patterns.append({"type": "any"})

        return {"type": "funcdef", "name": name_ir, "params": param_patterns}

    elif isinstance(node, _stdlib_ast.AsyncFunctionDef):
        return None

    elif isinstance(node, _stdlib_ast.ClassDef):
        name_ir = _ast_to_rust_ir(_stdlib_ast.Name(id=node.name, ctx=_stdlib_ast.Load()), metavar_map)
        if name_ir is None:
            return None

        bases_ir = []
        for base in node.bases:
            base_ir = _ast_to_rust_ir(base, metavar_map)
            if base_ir is None:
                return None
            bases_ir.append(base_ir)

        return {"type": "classdef", "name": name_ir, "bases": bases_ir}

    elif isinstance(node, _stdlib_ast.Expr):
        return _ast_to_rust_ir(node.value, metavar_map)

    elif isinstance(node, _stdlib_ast.Module):
        if node.body:
            return _ast_to_rust_ir(node.body[0], metavar_map)
        return None

    elif isinstance(node, _stdlib_ast.JoinedStr):
        return {"type": "string", "value": None}

    else:
        return None


def _reconstruct_code_from_lark(tree: Tree, metavar_map: dict[str, MetaVar]) -> tuple[str, dict[str, MetaVar]]:
    """Reconstruct the pattern code string from a Lark parse tree.

    Replaces metavar tokens with valid Python identifier placeholders,
    preserving the code_chunk text verbatim.

    Returns (reconstructed_code, metavar_placeholder_map) where
    metavar_placeholder_map maps placeholder identifier names to MetaVar objects.
    """
    parts: list[str] = []
    placeholder_map: dict[str, MetaVar] = {}
    mv_counter: dict[str, int] = {}

    def visit(node):
        if isinstance(node, Tree):
            if node.data == "metavar":
                has_ellipsis = False
                name = None
                for child in node.children:
                    if isinstance(child, Token):
                        if child.type == "ELLIPSIS":
                            has_ellipsis = True
                        elif child.type == "METAVAR_NAME":
                            name = str(child)
                        elif child.type == "UNDERSCORE":
                            name = "_"
                    elif isinstance(child, str) and child == "...":
                        has_ellipsis = True

                if name is None:
                    name = "_"

                count = mv_counter.get(name, 0)
                mv_counter[name] = count + 1
                placeholder = f"__META_{name}__" if count == 0 else f"__META_{name}_{count}__"

                type_constraint = None
                for child in node.children:
                    if isinstance(child, Tree) and child.data == "type_constraint":
                        for tc in child.children:
                            if isinstance(tc, Token):
                                constraint_raw = str(tc)
                                type_constraint = constraint_raw[1:] if constraint_raw.startswith(":") else constraint_raw
                    elif isinstance(child, Token) and child.type in ("SIMPLE_TYPE_CONSTRAINT", "ORACLE_TYPE_CONSTRAINT"):
                        constraint_raw = str(child)
                        type_constraint = constraint_raw[1:] if constraint_raw.startswith(":") else constraint_raw

                mv = MetaVar(name=name, ellipsis=has_ellipsis, type_constraint=type_constraint)
                placeholder_map[placeholder] = mv
                parts.append(placeholder)

            elif node.data == "code_chunk":
                for child in node.children:
                    if isinstance(child, Token):
                        parts.append(str(child))
                    elif isinstance(child, str):
                        parts.append(child)

            else:
                for child in node.children:
                    visit(child)

        elif isinstance(node, Token):
            pass

    visit(tree)
    return "".join(parts), placeholder_map


def _lark_to_rust_ir(tree: Tree, metavar_map: dict[str, MetaVar]) -> dict | None:
    """Convert a Lark pattern parse tree to a Rust IR dict.

    This bypasses LibCST by using the stdlib ast module for Python expression
    parsing. Returns None if the pattern is not yet supported.
    """
    try:
        reconstructed, placeholder_map = _reconstruct_code_from_lark(tree, metavar_map)

        is_except_header = _is_except_header(reconstructed)
        is_compound_header = _is_compound_statement_header(reconstructed)
        is_try = reconstructed.strip() == "try:"
        parse_code = reconstructed

        if is_try:
            parse_code = "try:\n    pass\nexcept Exception:\n    pass"
        elif is_except_header:
            parse_code = "try:\n    pass\n" + reconstructed + "\n    pass"
        elif is_compound_header:
            parse_code = reconstructed + "\n    pass"

        try:
            parsed = _stdlib_ast.parse(parse_code, mode="eval")
            root = parsed.body
        except SyntaxError:
            try:
                parsed = _stdlib_ast.parse(parse_code, mode="exec")
                if not parsed.body:
                    return None
                root = parsed.body[0]
                if isinstance(root, _stdlib_ast.Expr):
                    root = root.value
                if is_try and isinstance(root, _stdlib_ast.Try):
                    pass
                elif is_except_header and isinstance(root, _stdlib_ast.Try):
                    if root.handlers:
                        return None
            except SyntaxError:
                return None

        return _ast_to_rust_ir(root, placeholder_map)

    except Exception:
        return None


def compile_pattern_to_rust_ir(pattern_str: str) -> dict | None:
    """Compile a pattern string to Rust IR dict for the tree-sitter fast path.

    Returns None if the pattern is not yet supported by the Rust matcher
    (e.g., assignments, comprehensions, binary ops, f-strings).
    In that case, the caller should fall back to the LibCST path.

    Args:
        pattern_str: Raw pattern string, e.g. "print($...ARGS)" or
                     "$X.objects.filter($...ARGS)"

    Returns:
        Dict IR like {"type": "call", "func": ..., "args": [...]} or None.
    """
    try:
        pattern = parse_pattern(pattern_str)

        # Try the Lark → stdlib ast → Rust IR path first (no LibCST needed)
        if pattern.tree is not None:
            mv_by_name = {mv.name: mv for mv in pattern.metavars}
            ir = _lark_to_rust_ir(pattern.tree, mv_by_name)
            if ir is not None:
                return ir

        # Fall back to the LibCST path
        temp_code, metavar_map = _build_metavar_map_and_replace(pattern)

        # Handle compound statement headers
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

        # Parse as Python expression or statement
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
    """Compile an inside/not_inside constraint string to Rust IR dict.

    Handles:
    - "def" → any function definition
    - "class" → any class definition
    - "def test_*" → function with name matching glob
    - "class MyClass:" → class with specific name
    - Full pattern strings like "class $C(TestCase):"

    Returns None if the constraint is not supported by the Rust matcher.
    """
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
        # tree-sitter doesn't distinguish async/sync in our matcher yet
        # Fall back to LibCST for async def constraints
        return None

    if constraint == "class":
        return {
            "type": "classdef",
            "name": {"type": "any_expr"},
            "bases": [{"type": "ellipsis"}],
            "decorators": [{"type": "ellipsis"}],
        }

    # "def name_pattern" or "class name_pattern"
    for keyword in ("def", "class"):
        if constraint.startswith(keyword + " "):
            name_pattern = constraint[len(keyword) + 1:].strip()
            # Remove trailing colon if present (e.g., "def test_*:")
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
                }
            else:
                return {
                    "type": "classdef",
                    "name": name_ir,
                    "bases": [{"type": "ellipsis"}],
                    "decorators": [{"type": "ellipsis"}],
                }

    # Try as a full pattern (e.g., "class $C(TestCase):")
    # Strip trailing colon first for pattern compilation
    stripped = constraint.rstrip()
    if stripped.endswith(":"):
        # It might be a compound statement header — try compiling as pattern
        ir = compile_pattern_to_rust_ir(stripped)
        if ir is not None:
            return ir

    # Try as a plain pattern
    ir = compile_pattern_to_rust_ir(constraint)
    return ir
