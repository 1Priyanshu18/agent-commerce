"""Runtime policy evaluation. Pure: no I/O, no LLM calls, deterministic given (policy,
context). Every call to evaluate() must be paired by the caller with a ledger write — see
policy/service.py — evaluate() itself just returns a Verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent_commerce.core.money import Money
from agent_commerce.ledger.models import Actor, PolicyVerdict

from .compiler import CompiledPolicy, CompiledRule

_MONEY_SUFFIX = "_paise"
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


@dataclass(frozen=True)
class Adjustment:
    field: str
    from_value: Any
    to_value: Any
    rule_id: str

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "from": self.from_value,
            "to": self.to_value,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True)
class Verdict:
    outcome: PolicyVerdict
    matched_rule_ids: list[str]
    machine_reason: str | None
    human_reason: str | None
    reasoning_summary: str
    policy_version: str
    transformed_arguments: dict | None
    adjustments: list[Adjustment]


def _with_display_fields(namespace: dict) -> dict:
    """Auto-derive rupee-formatted display fields for every *_paise key, e.g.
    cart.total_paise=240000 also exposes cart.total = '₹2,400.00', so reason templates can
    interpolate human-readable amounts without any rule author hand-formatting money — the
    human_reason is always generated from real values, never hand-written at the call site.
    """
    enriched = dict(namespace)
    for key, value in namespace.items():
        if key.endswith(_MONEY_SUFFIX) and isinstance(value, int) and not isinstance(value, bool):
            display_key = key[: -len(_MONEY_SUFFIX)]
            enriched.setdefault(display_key, Money(value).format_inr())
    return enriched


def build_context(arguments: dict, state: dict, policy: CompiledPolicy) -> dict:
    context: dict[str, Any] = {}
    for namespace, values in {**state, **arguments}.items():
        context[namespace] = _with_display_fields(values) if isinstance(values, dict) else values
    context["merchant"] = policy.merchant
    context["budget"] = policy.budget
    return context


def _get_path(context: dict, path: tuple[str, ...]) -> Any:
    value: Any = context
    for part in path:
        value = value[part]
    return value


def _set_path(context: dict, path: tuple[str, ...], value: Any) -> None:
    target = context
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def _interpolate(template: str, context: dict) -> str:
    def replace(match: re.Match) -> str:
        path = tuple(match.group(1).split("."))
        try:
            return str(_get_path(context, path))
        except (KeyError, TypeError):
            return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, template)


class PolicyEngine:
    """Deterministic, argument-level authorization gate.

    tool_level_only reproduces the "tool-level gating only" baseline from the Guardrails
    paper on this system: when set, no rule predicates are evaluated at all — a tool call
    that reaches evaluate() is always ALLOWed, regardless of cart total, discount, or margin.
    Used only as an eval-harness comparison point; never the default for real checks.
    """

    def __init__(self, policy: CompiledPolicy, *, tool_level_only: bool = False) -> None:
        self._policy = policy
        self._tool_level_only = tool_level_only

    @property
    def policy_version(self) -> str:
        return self._policy.policy_version

    @property
    def approval_timeout_seconds(self) -> int:
        return self._policy.approval_timeout_seconds

    def evaluate(
        self,
        actor: Actor,
        tool_name: str,
        arguments: dict,
        state: dict | None = None,
    ) -> Verdict:
        state = state or {}
        matched: list[CompiledRule] = [r for r in self._policy.rules if tool_name in r.on]

        if self._tool_level_only:
            return Verdict(
                outcome=PolicyVerdict.ALLOW,
                matched_rule_ids=[],
                machine_reason=None,
                human_reason=None,
                reasoning_summary=(
                    f"tool_level_only: {actor.value} permitted to call {tool_name} without "
                    "argument-level checks"
                ),
                policy_version=self._policy.policy_version,
                transformed_arguments=None,
                adjustments=[],
            )

        if not matched:
            return Verdict(
                outcome=PolicyVerdict.ALLOW,
                matched_rule_ids=[],
                machine_reason=None,
                human_reason=None,
                reasoning_summary=f"no rule targets {tool_name}; default allow",
                policy_version=self._policy.policy_version,
                transformed_arguments=None,
                adjustments=[],
            )

        context = build_context(arguments, state, self._policy)

        # Pass 1: TRANSFORM rules mutate the context in place, so any later rule in this same
        # call sees the adjusted value. Transforms only touch the single field they target —
        # they do not recompute derived state elsewhere (e.g. capping offer.discount_pct does
        # not itself recompute cart.projected_margin_pct; that recomputation is Cart's job,
        # invoked by the orchestrator before the next check that depends on it).
        adjustments: list[Adjustment] = []
        transform_hits: list[CompiledRule] = []
        transform_reasons: list[str] = []
        for rule in matched:
            if rule.condition_kind != "transform_if":
                continue
            if rule.condition_fn(context):
                # Interpolate the reason before mutating context, so it reports the
                # original (pre-cap) value, not the already-adjusted one on both sides.
                transform_reasons.append(_interpolate(rule.reason_template, context))
                old_value = _get_path(context, rule.transform_target)
                new_value = rule.transform_fn(context)
                _set_path(context, rule.transform_target, new_value)
                adjustments.append(
                    Adjustment(
                        field=".".join(rule.transform_target),
                        from_value=old_value,
                        to_value=new_value,
                        rule_id=rule.id,
                    )
                )
                transform_hits.append(rule)

        # Pass 2: gating rules in declaration order. DENY is terminal and wins over
        # REQUIRE_APPROVAL if both would otherwise fire; the first matching DENY (in YAML
        # order) is the one reported.
        deny_hit: CompiledRule | None = None
        approval_hit: CompiledRule | None = None
        for rule in matched:
            if rule.condition_kind == "deny_if" and rule.condition_fn(context):
                deny_hit = rule
                break
            if rule.condition_kind == "allow_if" and not rule.condition_fn(context):
                deny_hit = rule
                break
            if (
                rule.condition_kind == "require_approval_if"
                and approval_hit is None
                and rule.condition_fn(context)
            ):
                approval_hit = rule

        if deny_hit is not None:
            reason = _interpolate(deny_hit.reason_template, context)
            return Verdict(
                outcome=PolicyVerdict.DENY,
                matched_rule_ids=[deny_hit.id],
                machine_reason=deny_hit.machine_reason,
                human_reason=reason,
                reasoning_summary=f"rule '{deny_hit.id}' denied {tool_name}: {reason}",
                policy_version=self._policy.policy_version,
                transformed_arguments=None,
                adjustments=[],
            )

        if approval_hit is not None:
            reason = _interpolate(approval_hit.reason_template, context)
            return Verdict(
                outcome=PolicyVerdict.REQUIRE_APPROVAL,
                matched_rule_ids=[approval_hit.id],
                machine_reason=approval_hit.machine_reason,
                human_reason=reason,
                reasoning_summary=f"rule '{approval_hit.id}' queued {tool_name} for approval: {reason}",
                policy_version=self._policy.policy_version,
                transformed_arguments=None,
                adjustments=[],
            )

        if adjustments:
            transform_ids = [r.id for r in transform_hits]
            reasons = "; ".join(transform_reasons)
            return Verdict(
                outcome=PolicyVerdict.TRANSFORM,
                matched_rule_ids=transform_ids,
                machine_reason=(
                    transform_hits[0].machine_reason if len(transform_hits) == 1 else "MULTIPLE_TRANSFORMS"
                ),
                human_reason=reasons,
                reasoning_summary=f"applied transform(s) {transform_ids} to {tool_name}: {reasons}",
                policy_version=self._policy.policy_version,
                transformed_arguments={k: v for k, v in context.items() if k in arguments},
                adjustments=adjustments,
            )

        return Verdict(
            outcome=PolicyVerdict.ALLOW,
            matched_rule_ids=[],
            machine_reason=None,
            human_reason=None,
            reasoning_summary=f"no rule blocked {tool_name}; allowed",
            policy_version=self._policy.policy_version,
            transformed_arguments=None,
            adjustments=[],
        )
