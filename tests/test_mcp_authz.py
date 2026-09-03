import pytest

from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.mcp.authz import RoleViolationError, authorize, is_permitted


@pytest.fixture
def ledger(tmp_path) -> LedgerStore:
    return LedgerStore(tmp_path / "ledger.db")


@pytest.mark.parametrize(
    "actor,tool_name,expected",
    [
        (Actor.BUYER_AGENT, "catalog.search", True),
        (Actor.BUYER_AGENT, "cart.add", True),
        (Actor.BUYER_AGENT, "checkout.confirm", True),
        (Actor.BUYER_AGENT, "upsell.make_offer", False),
        (Actor.BUYER_AGENT, "cart.read_at_checkout", False),
        (Actor.UPSELL_AGENT, "upsell.make_offer", True),
        (Actor.UPSELL_AGENT, "cart.read_at_checkout", True),
        (Actor.UPSELL_AGENT, "cart.add", False),
        (Actor.UPSELL_AGENT, "checkout.confirm", False),
        (Actor.ORCHESTRATOR, "cart.add", False),
        (Actor.POLICY_ENGINE, "checkout.confirm", False),
    ],
)
def test_is_permitted_table(actor: Actor, tool_name: str, expected: bool) -> None:
    assert is_permitted(actor, tool_name) is expected


def test_authorize_is_noop_and_writes_no_ledger_entry_when_permitted(ledger: LedgerStore) -> None:
    authorize(Actor.BUYER_AGENT, "cart.add", ledger, transaction_id="txn_1", caused_by=[])
    assert ledger.entries_for_transaction("txn_1") == []


def test_authorize_raises_and_logs_when_buyer_attempts_merchant_only_tool(ledger: LedgerStore) -> None:
    # The scenario from the brief: the buyer agent (e.g. via a prompt-injection-influenced
    # tool-call attempt) tries to call a merchant-only tool. Structurally it can't reach this
    # tool through the buyer server at all (see test_mcp_buyer_server.py), but the shared
    # authz layer catches and logs it independently too.
    with pytest.raises(RoleViolationError) as exc_info:
        authorize(
            Actor.BUYER_AGENT,
            "upsell.make_offer",
            ledger,
            transaction_id="txn_1",
            caused_by=["entry_prior"],
        )

    assert exc_info.value.actor == Actor.BUYER_AGENT
    assert exc_info.value.tool_name == "upsell.make_offer"

    entries = ledger.entries_for_transaction("txn_1")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action_type == ActionType.ROLE_VIOLATION
    assert entry.actor == Actor.BUYER_AGENT
    assert entry.caused_by == ["entry_prior"]
    assert entry.machine_reason == "ROLE_NOT_PERMITTED"
    assert entry.human_reason == (
        "buyer_agent attempted to call 'upsell.make_offer', which is outside its permitted tool set"
    )
    assert entry.input == {"attempted_tool": "upsell.make_offer"}


def test_authorize_raises_and_logs_when_upsell_agent_attempts_buyer_only_tool(ledger: LedgerStore) -> None:
    with pytest.raises(RoleViolationError):
        authorize(Actor.UPSELL_AGENT, "checkout.confirm", ledger, transaction_id="txn_1", caused_by=[])

    entry = ledger.entries_for_transaction("txn_1")[0]
    assert entry.actor == Actor.UPSELL_AGENT
    assert entry.machine_reason == "ROLE_NOT_PERMITTED"


def test_role_violation_entry_keeps_chain_valid(ledger: LedgerStore) -> None:
    with pytest.raises(RoleViolationError):
        authorize(Actor.BUYER_AGENT, "upsell.no_offer", ledger, transaction_id="txn_1", caused_by=[])
    assert ledger.verify_chain().ok is True
