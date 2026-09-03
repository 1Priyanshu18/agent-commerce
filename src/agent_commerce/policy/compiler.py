"""Boot-time policy compilation: parse policies/default.yaml, validate every rule against a
schema, resolve field references, and compile predicates to callables. A malformed policy
file must fail hard here — never degrade silently into a permissive runtime.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent_commerce.core.json_canonical import canonical_json

from .expr import ExprError, compile_expr, parse_expr, resolve_path

KNOWN_NAMESPACES = {"cart", "offer", "product", "session", "merchant", "budget"}
VALID_CONDITION_KEYS = ("allow_if", "deny_if", "require_approval_if", "transform_if")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")
_DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300


class PolicyCompileError(Exception):
    """Raised when a policy file is malformed. Boot must fail hard on this, never fall back
    to a permissive default.
    """


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyCompileError(message)


def _referenced_namespaces(tree: Any) -> set[str]:
    # Exclude Call.func names (e.g. `clamp` in clamp(offer.discount_pct, ...)) — those are
    # function references, not field-path roots, and are validated separately by expr.py.
    call_func_ids = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in call_func_ids
    }


def _check_expr_namespaces(expr: str, tree: Any, where: str) -> None:
    unknown = _referenced_namespaces(tree) - KNOWN_NAMESPACES
    _require(not unknown, f"{where}: unknown namespace(s) {sorted(unknown)} in expression {expr!r}")


def _check_reason_namespaces(reason: str, where: str) -> None:
    for match in _PLACEHOLDER_RE.finditer(reason):
        root = match.group(1).split(".")[0]
        _require(
            root in KNOWN_NAMESPACES,
            f"{where}: unknown namespace '{root}' in reason template {reason!r}",
        )


@dataclass(frozen=True)
class CompiledRule:
    id: str
    on: frozenset[str]
    condition_kind: str
    condition_fn: Any  # Callable[[dict], bool]
    transform_fn: Any | None  # Callable[[dict], Any]
    transform_target: tuple[str, ...] | None
    machine_reason: str
    reason_template: str


@dataclass(frozen=True)
class CompiledPolicy:
    version: int
    policy_version: str
    merchant: dict
    budget: dict
    approval_timeout_seconds: int
    rules: list[CompiledRule]
    source_name: str


def _compile_rule(raw_rule: Any, index: int, source_name: str, seen_ids: set[str]) -> CompiledRule:
    where = f"{source_name}: rules[{index}]"
    _require(isinstance(raw_rule, dict), f"{where}: rule must be a mapping")

    rule_id = raw_rule.get("id")
    _require(isinstance(rule_id, str) and bool(rule_id), f"{where}: 'id' must be a non-empty string")
    where = f"{source_name}: rules[{index}] ({rule_id})"
    _require(rule_id not in seen_ids, f"{where}: duplicate rule id")
    seen_ids.add(rule_id)

    on = raw_rule.get("on")
    _require(
        isinstance(on, list) and len(on) > 0 and all(isinstance(t, str) and t for t in on),
        f"{where}: 'on' must be a non-empty list of tool name strings",
    )

    present_condition_keys = [k for k in VALID_CONDITION_KEYS if k in raw_rule]
    _require(
        len(present_condition_keys) == 1,
        f"{where}: exactly one of {VALID_CONDITION_KEYS} must be present, found {present_condition_keys}",
    )
    condition_kind = present_condition_keys[0]
    condition_expr = raw_rule[condition_kind]
    _require(
        isinstance(condition_expr, str) and bool(condition_expr.strip()),
        f"{where}: '{condition_kind}' must be a non-empty string",
    )

    reason = raw_rule.get("reason")
    _require(
        isinstance(reason, str) and bool(reason.strip()), f"{where}: 'reason' must be a non-empty string"
    )
    machine_reason = raw_rule.get("machine_reason", rule_id.upper())
    _require(
        isinstance(machine_reason, str) and bool(machine_reason),
        f"{where}: 'machine_reason' must be a non-empty string",
    )

    try:
        condition_tree = parse_expr(condition_expr)
        _check_expr_namespaces(condition_expr, condition_tree, where)
        condition_fn = compile_expr(condition_expr)
    except ExprError as e:
        raise PolicyCompileError(f"{where}: invalid '{condition_kind}' expression: {e}") from e
    _check_reason_namespaces(reason, where)

    transform_fn = None
    transform_target = None
    if condition_kind == "transform_if":
        transform_expr = raw_rule.get("transform")
        _require(
            isinstance(transform_expr, str) and bool(transform_expr.strip()),
            f"{where}: 'transform_if' rules require a non-empty 'transform' expression",
        )
        try:
            transform_tree = parse_expr(transform_expr)
            _check_expr_namespaces(transform_expr, transform_tree, where)
            _require(
                isinstance(transform_tree.body, ast.Call), f"{where}: 'transform' must be a function call"
            )
            _require(
                len(transform_tree.body.args) > 0,
                f"{where}: 'transform' call needs at least one argument (the field to adjust)",
            )
            transform_target = tuple(resolve_path(transform_tree.body.args[0]))
            transform_fn = compile_expr(transform_expr)
        except ExprError as e:
            raise PolicyCompileError(f"{where}: invalid 'transform' expression: {e}") from e
    else:
        _require("transform" not in raw_rule, f"{where}: 'transform' is only valid alongside 'transform_if'")

    return CompiledRule(
        id=rule_id,
        on=frozenset(on),
        condition_kind=condition_kind,
        condition_fn=condition_fn,
        transform_fn=transform_fn,
        transform_target=transform_target,
        machine_reason=machine_reason,
        reason_template=reason,
    )


def _compile_dict(raw: Any, source_name: str) -> CompiledPolicy:
    _require(isinstance(raw, dict), f"{source_name}: policy must be a mapping")
    for key in ("version", "merchant", "budget", "rules"):
        _require(key in raw, f"{source_name}: missing required key '{key}'")

    merchant = raw["merchant"]
    _require(isinstance(merchant, dict), f"{source_name}: 'merchant' must be a mapping")
    _require(
        isinstance(merchant.get("max_discount_pct"), (int, float))
        and 0 <= merchant["max_discount_pct"] <= 100,
        f"{source_name}: merchant.max_discount_pct must be a number in [0, 100]",
    )
    _require(
        isinstance(merchant.get("min_margin_pct"), (int, float)),
        f"{source_name}: merchant.min_margin_pct must be a number",
    )
    blacklist_skus = merchant.get("blacklist_skus", [])
    _require(
        isinstance(blacklist_skus, list) and all(isinstance(s, str) for s in blacklist_skus),
        f"{source_name}: merchant.blacklist_skus must be a list of strings",
    )

    budget = raw["budget"]
    _require(isinstance(budget, dict), f"{source_name}: 'budget' must be a mapping")
    _require(
        isinstance(budget.get("ceiling_source"), str) and bool(budget["ceiling_source"]),
        f"{source_name}: budget.ceiling_source must be a non-empty string",
    )

    approval_timeout_seconds = raw.get("approval_timeout_seconds", _DEFAULT_APPROVAL_TIMEOUT_SECONDS)
    _require(
        isinstance(approval_timeout_seconds, int) and approval_timeout_seconds > 0,
        f"{source_name}: approval_timeout_seconds must be a positive integer",
    )

    rules_raw = raw["rules"]
    _require(
        isinstance(rules_raw, list) and len(rules_raw) > 0,
        f"{source_name}: 'rules' must be a non-empty list",
    )

    seen_ids: set[str] = set()
    rules = [_compile_rule(r, i, source_name, seen_ids) for i, r in enumerate(rules_raw)]

    policy_version = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()[:16]

    return CompiledPolicy(
        version=raw["version"],
        policy_version=policy_version,
        merchant=merchant,
        budget=budget,
        approval_timeout_seconds=approval_timeout_seconds,
        rules=rules,
        source_name=source_name,
    )


def compile_policy(path: str | Path) -> CompiledPolicy:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise PolicyCompileError(f"{path}: invalid YAML: {e}") from e
    return _compile_dict(raw, str(path))


def compile_policy_text(text: str, source_name: str = "<inline policy>") -> CompiledPolicy:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyCompileError(f"{source_name}: invalid YAML: {e}") from e
    return _compile_dict(raw, source_name)
