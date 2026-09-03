from pathlib import Path

from agent_commerce.ledger.models import ActionType, Actor, PolicyVerdict
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.policy.approvals import ApprovalStore
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

REPO_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"


def _service(
    tmp_path, with_approvals: bool = True
) -> tuple[PolicyService, LedgerStore, ApprovalStore | None]:
    ledger = LedgerStore(tmp_path / "ledger.db")
    engine = PolicyEngine(compile_policy(REPO_POLICY_PATH))
    approvals = ApprovalStore(tmp_path / "approvals.db") if with_approvals else None
    return PolicyService(engine, ledger, approvals), ledger, approvals


def test_allow_check_writes_ledger_entry(tmp_path) -> None:
    service, ledger, _ = _service(tmp_path)
    service.check(
        actor=Actor.BUYER_AGENT,
        tool_name="cart.add",
        arguments={"product": {"sku": "SKU-0001"}},
        state=None,
        transaction_id="txn_1",
        caused_by=[],
    )
    entries = ledger.entries_for_transaction("txn_1")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor == Actor.POLICY_ENGINE
    assert entry.action_type == ActionType.POLICY_CHECK
    assert entry.policy_verdict == PolicyVerdict.ALLOW
    assert entry.policy_version is not None
    assert entry.reasoning_summary is not None


def test_deny_check_records_machine_and_human_reason(tmp_path) -> None:
    service, ledger, _ = _service(tmp_path)
    verdict = service.check(
        actor=Actor.BUYER_AGENT,
        tool_name="cart.add",
        arguments={"product": {"sku": "SKU-0042"}},
        state=None,
        transaction_id="txn_1",
        caused_by=[],
    )
    assert verdict.outcome == PolicyVerdict.DENY
    entry = ledger.entries_for_transaction("txn_1")[0]
    assert entry.policy_verdict == PolicyVerdict.DENY
    assert entry.machine_reason == "SKU_BLACKLISTED"
    assert entry.human_reason == "SKU-0042 is not available for agent-initiated purchase"


def test_transform_check_records_adjustments_in_output(tmp_path) -> None:
    service, ledger, _ = _service(tmp_path)
    service.check(
        actor=Actor.UPSELL_AGENT,
        tool_name="upsell.make_offer",
        arguments={"offer": {"discount_pct": 25}},
        state=None,
        transaction_id="txn_1",
        caused_by=[],
    )
    entry = ledger.entries_for_transaction("txn_1")[0]
    assert entry.policy_verdict == PolicyVerdict.TRANSFORM
    assert entry.output["adjustments"] == [
        {"field": "offer.discount_pct", "from": 25, "to": 15, "rule_id": "discount_cap"}
    ]


def test_require_approval_creates_linked_approval_row(tmp_path) -> None:
    service, ledger, approvals = _service(tmp_path)
    assert approvals is not None
    verdict = service.check(
        actor=Actor.ORCHESTRATOR,
        tool_name="checkout.confirm",
        arguments={"cart": {"total_paise": 600_000}},
        state={"session": {"buyer_budget_paise": 10_000_000}},
        transaction_id="txn_1",
        caused_by=[],
    )
    assert verdict.outcome == PolicyVerdict.REQUIRE_APPROVAL

    entry = ledger.entries_for_transaction("txn_1")[0]
    pending = approvals.list_pending()
    assert len(pending) == 1
    assert pending[0].ledger_entry_id == entry.entry_id
    assert pending[0].transaction_id == "txn_1"


def test_require_approval_without_approval_store_still_writes_ledger(tmp_path) -> None:
    service, ledger, approvals = _service(tmp_path, with_approvals=False)
    assert approvals is None
    verdict = service.check(
        actor=Actor.ORCHESTRATOR,
        tool_name="checkout.confirm",
        arguments={"cart": {"total_paise": 600_000}},
        state={"session": {"buyer_budget_paise": 10_000_000}},
        transaction_id="txn_1",
        caused_by=[],
    )
    assert verdict.outcome == PolicyVerdict.REQUIRE_APPROVAL
    assert ledger.entries_for_transaction("txn_1")[0].policy_verdict == PolicyVerdict.REQUIRE_APPROVAL


def test_caused_by_is_recorded_on_policy_check_entry(tmp_path) -> None:
    service, ledger, _ = _service(tmp_path)
    service.check(
        actor=Actor.BUYER_AGENT,
        tool_name="cart.add",
        arguments={"product": {"sku": "SKU-0001"}},
        state=None,
        transaction_id="txn_1",
        caused_by=["entry_upstream"],
    )
    entry = ledger.entries_for_transaction("txn_1")[0]
    assert entry.caused_by == ["entry_upstream"]


def test_chain_stays_valid_across_multiple_checks(tmp_path) -> None:
    service, ledger, _ = _service(tmp_path)
    service.check(
        actor=Actor.BUYER_AGENT,
        tool_name="cart.add",
        arguments={"product": {"sku": "SKU-0001"}},
        state=None,
        transaction_id="txn_1",
        caused_by=[],
    )
    service.check(
        actor=Actor.UPSELL_AGENT,
        tool_name="upsell.make_offer",
        arguments={"offer": {"discount_pct": 25}},
        state=None,
        transaction_id="txn_1",
        caused_by=[],
    )
    service.check(
        actor=Actor.ORCHESTRATOR,
        tool_name="checkout.confirm",
        arguments={"cart": {"total_paise": 100_000}},
        state={"session": {"buyer_budget_paise": 200_000}},
        transaction_id="txn_1",
        caused_by=[],
    )
    result = ledger.verify_chain()
    assert result.ok is True
    assert result.entries_checked == 3
