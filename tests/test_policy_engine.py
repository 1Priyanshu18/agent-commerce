"""Golden, table-driven tests for the runtime engine against the real policies/default.yaml.
Every rule gets an ALLOW (non-triggering) case, a triggering case, and a boundary case —
documenting inclusive/exclusive behavior at the threshold as we go.
"""

from pathlib import Path

import pytest

from agent_commerce.ledger.models import Actor, PolicyVerdict
from agent_commerce.policy.compiler import compile_policy, compile_policy_text
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.expr import ExprError

REPO_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(compile_policy(REPO_POLICY_PATH))


# --- budget_ceiling: deny_if cart.total_paise > session.buyer_budget_paise (strict) ---


def test_budget_ceiling_allow_when_under(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.ORCHESTRATOR,
        "checkout.confirm",
        arguments={"cart": {"total_paise": 150_000}},
        state={"session": {"buyer_budget_paise": 200_000}},
    )
    assert v.outcome == PolicyVerdict.ALLOW


def test_budget_ceiling_boundary_exactly_at_ceiling_is_allowed(engine: PolicyEngine) -> None:
    # Documented as inclusive of the ceiling: strict '>' means total == budget is fine.
    v = engine.evaluate(
        Actor.ORCHESTRATOR,
        "checkout.confirm",
        arguments={"cart": {"total_paise": 200_000}},
        state={"session": {"buyer_budget_paise": 200_000}},
    )
    assert v.outcome == PolicyVerdict.ALLOW


def test_budget_ceiling_denies_one_paise_over(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.ORCHESTRATOR,
        "checkout.confirm",
        arguments={"cart": {"total_paise": 200_001}},
        state={"session": {"buyer_budget_paise": 200_000}},
    )
    assert v.outcome == PolicyVerdict.DENY
    assert v.matched_rule_ids == ["budget_ceiling"]
    assert v.machine_reason == "BUDGET_CEILING_EXCEEDED"
    assert v.human_reason == "cart total ₹2,000.01 exceeds buyer budget ceiling ₹2,000.00"


# --- discount_cap: transform_if offer.discount_pct > merchant.max_discount_pct (15) ---


def test_discount_cap_allow_when_under(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.UPSELL_AGENT, "upsell.make_offer", arguments={"offer": {"discount_pct": 10}}, state={}
    )
    assert v.outcome == PolicyVerdict.ALLOW
    assert v.adjustments == []


def test_discount_cap_boundary_exactly_at_cap_not_transformed(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.UPSELL_AGENT, "upsell.make_offer", arguments={"offer": {"discount_pct": 15}}, state={}
    )
    assert v.outcome == PolicyVerdict.ALLOW


def test_discount_cap_clamps_when_over(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.UPSELL_AGENT, "upsell.make_offer", arguments={"offer": {"discount_pct": 20}}, state={}
    )
    assert v.outcome == PolicyVerdict.TRANSFORM
    assert v.matched_rule_ids == ["discount_cap"]
    assert v.machine_reason == "DISCOUNT_CAPPED"
    # Reports the original (pre-cap) value, not the already-adjusted value on both sides.
    assert v.human_reason == "discount 20% capped to merchant maximum 15%"
    assert len(v.adjustments) == 1
    adj = v.adjustments[0]
    assert (adj.field, adj.from_value, adj.to_value) == ("offer.discount_pct", 20, 15)
    assert adj.rule_id == "discount_cap"
    assert v.transformed_arguments == {"offer": {"discount_pct": 15}}


# --- margin_floor: deny_if cart.projected_margin_pct < merchant.min_margin_pct (12) ---
# on cart.accept_upsell, which discount_cap also targets — every case here must also supply a
# neutral (non-capping) offer.discount_pct so discount_cap's own field reference resolves.


def test_margin_floor_allow_when_above(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.BUYER_AGENT,
        "cart.accept_upsell",
        arguments={"cart": {"projected_margin_pct": 20}, "offer": {"discount_pct": 5}},
        state={},
    )
    assert v.outcome == PolicyVerdict.ALLOW


def test_margin_floor_boundary_exactly_at_floor_is_allowed(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.BUYER_AGENT,
        "cart.accept_upsell",
        arguments={"cart": {"projected_margin_pct": 12}, "offer": {"discount_pct": 5}},
        state={},
    )
    assert v.outcome == PolicyVerdict.ALLOW


def test_margin_floor_denies_below(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.BUYER_AGENT,
        "cart.accept_upsell",
        arguments={"cart": {"projected_margin_pct": 11.99}, "offer": {"discount_pct": 5}},
        state={},
    )
    assert v.outcome == PolicyVerdict.DENY
    assert v.matched_rule_ids == ["margin_floor"]
    assert v.machine_reason == "MARGIN_BELOW_FLOOR"
    assert v.human_reason == "projected margin 11.99% below floor 12%"


# --- high_value_review: require_approval_if cart.total_paise > 500000 (strict) ---


def test_high_value_review_allow_when_under(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.ORCHESTRATOR,
        "checkout.confirm",
        arguments={"cart": {"total_paise": 400_000}},
        state={"session": {"buyer_budget_paise": 10_000_000}},
    )
    assert v.outcome == PolicyVerdict.ALLOW


def test_high_value_review_boundary_exactly_at_threshold_is_allowed(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.ORCHESTRATOR,
        "checkout.confirm",
        arguments={"cart": {"total_paise": 500_000}},
        state={"session": {"buyer_budget_paise": 10_000_000}},
    )
    assert v.outcome == PolicyVerdict.ALLOW


def test_high_value_review_requires_approval_over_threshold(engine: PolicyEngine) -> None:
    v = engine.evaluate(
        Actor.ORCHESTRATOR,
        "checkout.confirm",
        arguments={"cart": {"total_paise": 500_001}},
        state={"session": {"buyer_budget_paise": 10_000_000}},
    )
    assert v.outcome == PolicyVerdict.REQUIRE_APPROVAL
    assert v.matched_rule_ids == ["high_value_review"]
    assert v.machine_reason == "HIGH_VALUE_REVIEW_REQUIRED"


# --- blacklist: deny_if product.sku in merchant.blacklist_skus ([SKU-0042]) ---


def test_blacklist_allow_for_ordinary_sku(engine: PolicyEngine) -> None:
    v = engine.evaluate(Actor.BUYER_AGENT, "cart.add", arguments={"product": {"sku": "SKU-0001"}}, state={})
    assert v.outcome == PolicyVerdict.ALLOW


def test_blacklist_boundary_similar_but_different_sku_is_allowed(engine: PolicyEngine) -> None:
    # Exact-match semantics: neither a longer SKU sharing the prefix nor a different case
    # variant should false-positive against the blacklist.
    v1 = engine.evaluate(Actor.BUYER_AGENT, "cart.add", arguments={"product": {"sku": "SKU-00420"}}, state={})
    v2 = engine.evaluate(Actor.BUYER_AGENT, "cart.add", arguments={"product": {"sku": "sku-0042"}}, state={})
    assert v1.outcome == PolicyVerdict.ALLOW
    assert v2.outcome == PolicyVerdict.ALLOW


def test_blacklist_denies_exact_sku(engine: PolicyEngine) -> None:
    v = engine.evaluate(Actor.BUYER_AGENT, "cart.add", arguments={"product": {"sku": "SKU-0042"}}, state={})
    assert v.outcome == PolicyVerdict.DENY
    assert v.matched_rule_ids == ["blacklist"]
    assert v.machine_reason == "SKU_BLACKLISTED"
    assert v.human_reason == "SKU-0042 is not available for agent-initiated purchase"


# --- cross-cutting behavior ---


def test_tool_level_only_reproduces_zero_percent_prevention(engine: PolicyEngine) -> None:
    tool_level_engine = PolicyEngine(compile_policy(REPO_POLICY_PATH), tool_level_only=True)
    # A blatant violation on every axis: over budget, blacklisted-equivalent scale, etc.
    v = tool_level_engine.evaluate(
        Actor.ORCHESTRATOR,
        "checkout.confirm",
        arguments={"cart": {"total_paise": 99_999_999}},
        state={"session": {"buyer_budget_paise": 1}},
    )
    assert v.outcome == PolicyVerdict.ALLOW
    assert v.matched_rule_ids == []


def test_unmatched_tool_name_defaults_to_allow(engine: PolicyEngine) -> None:
    v = engine.evaluate(Actor.BUYER_AGENT, "catalog.search", arguments={}, state={})
    assert v.outcome == PolicyVerdict.ALLOW
    assert v.matched_rule_ids == []


def test_missing_referenced_field_raises(engine: PolicyEngine) -> None:
    with pytest.raises(ExprError):
        engine.evaluate(
            Actor.ORCHESTRATOR, "checkout.confirm", arguments={"cart": {"total_paise": 100}}, state={}
        )


def test_policy_version_is_stable_across_calls(engine: PolicyEngine) -> None:
    v1 = engine.evaluate(Actor.BUYER_AGENT, "cart.add", arguments={"product": {"sku": "SKU-0001"}}, state={})
    v2 = engine.evaluate(Actor.BUYER_AGENT, "cart.add", arguments={"product": {"sku": "SKU-0002"}}, state={})
    assert v1.policy_version == v2.policy_version == engine.policy_version


def test_budget_ceiling_source_is_available_as_metadata() -> None:
    policy = compile_policy(REPO_POLICY_PATH)
    assert policy.budget["ceiling_source"] == "session.buyer_budget_paise"


_TWO_DENY_RULES = """
version: 1
budget:
  ceiling_source: session.buyer_budget_paise
merchant:
  max_discount_pct: 15
  min_margin_pct: 12
  blacklist_skus: []
rules:
  - id: first_deny
    "on": [checkout.confirm]
    deny_if: "cart.total_paise > 100"
    reason: "first triggered"
  - id: second_deny
    "on": [checkout.confirm]
    deny_if: "cart.total_paise > 50"
    reason: "second triggered"
"""


def test_declaration_order_wins_when_multiple_deny_rules_match() -> None:
    engine = PolicyEngine(compile_policy_text(_TWO_DENY_RULES))
    v = engine.evaluate(
        Actor.ORCHESTRATOR, "checkout.confirm", arguments={"cart": {"total_paise": 200}}, state={}
    )
    assert v.outcome == PolicyVerdict.DENY
    assert v.matched_rule_ids == ["first_deny"]


_APPROVAL_THEN_DENY = """
version: 1
budget:
  ceiling_source: session.buyer_budget_paise
merchant:
  max_discount_pct: 15
  min_margin_pct: 12
  blacklist_skus: []
rules:
  - id: needs_approval
    "on": [checkout.confirm]
    require_approval_if: "cart.total_paise > 50"
    reason: "needs approval"
  - id: hard_deny
    "on": [checkout.confirm]
    deny_if: "cart.total_paise > 100"
    reason: "hard deny"
"""


def test_deny_wins_over_require_approval_when_both_match() -> None:
    engine = PolicyEngine(compile_policy_text(_APPROVAL_THEN_DENY))
    v = engine.evaluate(
        Actor.ORCHESTRATOR, "checkout.confirm", arguments={"cart": {"total_paise": 200}}, state={}
    )
    assert v.outcome == PolicyVerdict.DENY
    assert v.matched_rule_ids == ["hard_deny"]


_TRANSFORM_THEN_DENY_SAME_TOOL = """
version: 1
budget:
  ceiling_source: session.buyer_budget_paise
merchant:
  max_discount_pct: 15
  min_margin_pct: 12
  blacklist_skus: []
rules:
  - id: cap_discount
    "on": [cart.accept_upsell]
    transform_if: "offer.discount_pct > merchant.max_discount_pct"
    transform: "clamp(offer.discount_pct, merchant.max_discount_pct)"
    reason: "capped"
  - id: reject_low_margin
    "on": [cart.accept_upsell]
    deny_if: "cart.projected_margin_pct < merchant.min_margin_pct"
    reason: "margin too low"
"""


def test_transform_still_applies_but_deny_wins_final_outcome() -> None:
    # Both rules fire in the same call: the discount does get capped (visible in
    # adjustments), but the overall verdict is DENY because margin_floor's condition is
    # evaluated against the cart.projected_margin_pct the caller supplied — capping the
    # discount does not retroactively recompute it within this single evaluate() call.
    engine = PolicyEngine(compile_policy_text(_TRANSFORM_THEN_DENY_SAME_TOOL))
    v = engine.evaluate(
        Actor.BUYER_AGENT,
        "cart.accept_upsell",
        arguments={"offer": {"discount_pct": 25}, "cart": {"projected_margin_pct": 5}},
        state={},
    )
    assert v.outcome == PolicyVerdict.DENY
    assert v.matched_rule_ids == ["reject_low_margin"]
