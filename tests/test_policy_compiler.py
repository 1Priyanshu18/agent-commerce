"""Boot-time validation: a malformed policy file must fail hard, never degrade into a
permissive runtime.
"""

from pathlib import Path

import pytest

from agent_commerce.policy.compiler import PolicyCompileError, compile_policy, compile_policy_text

REPO_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"

VALID_MINIMAL = """
version: 1
budget:
  ceiling_source: session.buyer_budget_paise
merchant:
  max_discount_pct: 15
  min_margin_pct: 12
  blacklist_skus: []
rules:
  - id: budget_ceiling
    "on": [checkout.confirm]
    deny_if: "cart.total_paise > session.buyer_budget_paise"
    reason: "cart total {cart.total} exceeds buyer budget ceiling {session.buyer_budget}"
"""


def test_real_default_yaml_compiles() -> None:
    policy = compile_policy(REPO_POLICY_PATH)
    rule_ids = {r.id for r in policy.rules}
    assert rule_ids == {"budget_ceiling", "discount_cap", "margin_floor", "high_value_review", "blacklist"}
    assert policy.merchant["blacklist_skus"] == ["SKU-0042"]
    assert policy.approval_timeout_seconds == 300


def test_policy_version_is_deterministic_hash() -> None:
    a = compile_policy(REPO_POLICY_PATH)
    b = compile_policy(REPO_POLICY_PATH)
    assert a.policy_version == b.policy_version
    assert len(a.policy_version) == 16


def test_policy_version_changes_when_content_changes() -> None:
    a = compile_policy_text(VALID_MINIMAL)
    b = compile_policy_text(VALID_MINIMAL.replace("15", "20"))
    assert a.policy_version != b.policy_version


def test_valid_minimal_policy_compiles() -> None:
    policy = compile_policy_text(VALID_MINIMAL)
    assert policy.version == 1
    assert len(policy.rules) == 1
    assert policy.approval_timeout_seconds == 300  # default applied


@pytest.mark.parametrize(
    "bad_text,expected_fragment",
    [
        ("not: valid: yaml: [", "invalid YAML"),
        ("- just\n- a\n- list", "must be a mapping"),
        ("version: 1\nmerchant: {}\nbudget: {}\n", "missing required key 'rules'"),
        (
            "version: 1\nbudget:\n  ceiling_source: session.buyer_budget_paise\n"
            "merchant:\n  min_margin_pct: 12\n  blacklist_skus: []\nrules: []\n",
            "max_discount_pct",
        ),
        (
            "version: 1\nbudget:\n  ceiling_source: session.buyer_budget_paise\n"
            "merchant:\n  max_discount_pct: 150\n  min_margin_pct: 12\n  blacklist_skus: []\nrules: []\n",
            "max_discount_pct",
        ),
        (
            "version: 1\nbudget: {}\nmerchant:\n  max_discount_pct: 15\n  min_margin_pct: 12\n"
            "  blacklist_skus: []\nrules: []\n",
            "ceiling_source",
        ),
        (
            "version: 1\nbudget:\n  ceiling_source: session.buyer_budget_paise\n"
            "merchant:\n  max_discount_pct: 15\n  min_margin_pct: 12\n  blacklist_skus: []\nrules: []\n",
            "non-empty list",
        ),
    ],
)
def test_missing_or_invalid_top_level_fields_fail_hard(bad_text: str, expected_fragment: str) -> None:
    with pytest.raises(PolicyCompileError, match=expected_fragment):
        compile_policy_text(bad_text)


def _rule_case(rule_yaml: str, expected_fragment: str) -> None:
    text = f"""
version: 1
budget:
  ceiling_source: session.buyer_budget_paise
merchant:
  max_discount_pct: 15
  min_margin_pct: 12
  blacklist_skus: []
rules:
{rule_yaml}
"""
    with pytest.raises(PolicyCompileError, match=expected_fragment):
        compile_policy_text(text)


def test_rule_missing_id_fails_hard() -> None:
    _rule_case(
        '  - "on": [checkout.confirm]\n    deny_if: "cart.total_paise > 0"\n    reason: "x"\n',
        "'id' must be a non-empty string",
    )


def test_rule_duplicate_id_fails_hard() -> None:
    text = """
version: 1
budget:
  ceiling_source: session.buyer_budget_paise
merchant:
  max_discount_pct: 15
  min_margin_pct: 12
  blacklist_skus: []
rules:
  - id: dup
    "on": [checkout.confirm]
    deny_if: "cart.total_paise > 0"
    reason: "x"
  - id: dup
    "on": [checkout.confirm]
    deny_if: "cart.total_paise > 1"
    reason: "y"
"""
    with pytest.raises(PolicyCompileError, match="duplicate rule id"):
        compile_policy_text(text)


def test_rule_missing_on_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    deny_if: "cart.total_paise > 0"\n    reason: "x"\n',
        "'on' must be a non-empty list",
    )


def test_rule_with_zero_condition_keys_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    reason: "x"\n',
        "exactly one of",
    )


def test_rule_with_two_condition_keys_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    deny_if: "cart.total_paise > 0"\n'
        '    require_approval_if: "cart.total_paise > 1"\n    reason: "x"\n',
        "exactly one of",
    )


def test_rule_missing_reason_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    deny_if: "cart.total_paise > 0"\n',
        "'reason' must be a non-empty string",
    )


def test_rule_condition_with_unknown_namespace_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    deny_if: "nonsense.field > 0"\n    reason: "x"\n',
        "unknown namespace",
    )


def test_rule_reason_with_unknown_namespace_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    deny_if: "cart.total_paise > 0"\n'
        '    reason: "value is {nonsense.field}"\n',
        "unknown namespace",
    )


def test_rule_condition_with_invalid_syntax_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    deny_if: "cart.total_paise >>"\n    reason: "x"\n',
        "invalid expression syntax",
    )


def test_rule_condition_calling_disallowed_function_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    deny_if: "__import__(\'os\').system(\'x\')"\n'
        '    reason: "x"\n',
        "unsupported",
    )


def test_transform_rule_without_transform_key_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [upsell.make_offer]\n    transform_if: "offer.discount_pct > 15"\n'
        '    reason: "x"\n',
        "require a non-empty 'transform'",
    )


def test_non_transform_rule_with_transform_key_fails_hard() -> None:
    _rule_case(
        '  - id: r1\n    "on": [checkout.confirm]\n    deny_if: "cart.total_paise > 0"\n'
        '    transform: "clamp(cart.total_paise, 0)"\n    reason: "x"\n',
        "only valid alongside 'transform_if'",
    )


def test_transform_expression_must_be_a_call() -> None:
    _rule_case(
        '  - id: r1\n    "on": [upsell.make_offer]\n    transform_if: "offer.discount_pct > 15"\n'
        '    transform: "offer.discount_pct"\n    reason: "x"\n',
        "must be a function call",
    )
