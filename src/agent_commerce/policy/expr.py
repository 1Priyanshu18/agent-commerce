"""A restricted expression language for policy predicates and transforms.

Deliberately not Python's eval()/exec() — policy expressions come from a YAML file that could
be malformed or (in principle) attacker-influenced, so the parser only accepts a narrow,
enumerated AST node set: dotted field references, comparisons, boolean logic, list/constant
literals, and calls to a fixed function registry. Anything else is rejected at compile time.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any


class ExprError(Exception):
    """Raised for both malformed expression syntax and unresolvable field references."""


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "clamp": lambda value, cap: min(value, cap),
}

_ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.Constant,
    ast.List,
    ast.Name,
    ast.Attribute,
    ast.Load,
    ast.Compare,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Call,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
    ast.In,
    ast.NotIn,
)


def resolve_path(node: ast.AST) -> list[str]:
    """Turn a Name/Attribute chain like `cart.total_paise` into ['cart', 'total_paise']."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        raise ExprError(f"unsupported reference expression: {ast.dump(node)}")
    return list(reversed(parts))


def _lookup(context: dict, path: list[str]) -> Any:
    value: Any = context
    for part in path:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ExprError(f"unresolved field reference: {'.'.join(path)}")
    return value


def _apply_compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    raise ExprError(f"unsupported comparison operator: {op}")  # pragma: no cover — guarded by validate_node


def _eval_node(node: ast.AST, context: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_eval_node(el, context) for el in node.elts]
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _lookup(context, resolve_path(node))
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        result = True
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, context)
            result = result and _apply_compare(op, left, right)
            left = right
        return result
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_node(v, context) for v in node.values)
        return any(_eval_node(v, context) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return not _eval_node(node.operand, context)
    if isinstance(node, ast.Call):
        func_name = node.func.id  # type: ignore[union-attr] — validated at compile time
        args = [_eval_node(a, context) for a in node.args]
        return _FUNCTIONS[func_name](*args)
    raise ExprError(f"unsupported expression node: {ast.dump(node)}")  # pragma: no cover


def validate_node(node: ast.AST) -> None:
    """Recursively reject any AST construct outside the allowed set. Called once at compile
    time so a malformed or unsafe expression fails at boot, never at first runtime hit.
    """
    if not isinstance(node, _ALLOWED_NODE_TYPES):
        raise ExprError(f"disallowed expression construct: {type(node).__name__}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ExprError(f"unsupported function call in expression: {ast.dump(node)}")
        if node.keywords:
            raise ExprError("keyword arguments are not supported in policy expressions")
    for child in ast.iter_child_nodes(node):
        validate_node(child)


def parse_expr(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"invalid expression syntax: {expr!r} ({e})") from e
    validate_node(tree)
    return tree


def compile_expr(expr: str) -> Callable[[dict], Any]:
    tree = parse_expr(expr)
    return lambda context: _eval_node(tree, context)
