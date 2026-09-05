"""Phase 7: the four failure paths (stock_conflict, payment_failure, missing_webhook,
policy_deny_recovery), each reproducible on demand via BuyerSessionRunner.run(inject_failure=...)
or the INJECT_FAILURE env var. Covers both the failure itself and, where the brief calls for
recovery, that the buyer agent genuinely adapts (not a blind retry) and the ledger's
provenance chain stays intact and verifiable through it.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.core.config import Config
from agent_commerce.core.llm import FakeLLMClient, text_response, tool_response
from agent_commerce.ledger.models import ActionType, PolicyVerdict
from agent_commerce.orchestrator.run_session import KNOWN_INJECTIONS, BuyerSessionRunner
from agent_commerce.payments import PaymentStack, build_payment_stack
from agent_commerce.payments.adapter import PaymentRetryableError
from agent_commerce.payments.models import OrderStatus, ReconciliationStatus
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

pytestmark = pytest.mark.anyio

REPO_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"


class _AlwaysFailingPaymentAdapter:
    """A raw adapter that always raises retryable — proves the orchestrator's own bounded
    retry genuinely aborts (with a ledger explanation) once its retry budget is exhausted,
    independent of the payment_failure injection mechanism (which only ever fails once).
    """

    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ):
        raise PaymentRetryableError("simulated permanent gateway outage")

    def fetch_payments(self, order_id: str) -> list:
        return []


def _config(tmp_path: Path) -> Config:
    return Config(
        app_env="test",
        log_level="INFO",
        llm_provider="gemini",
        gemini_api_key="",
        gemini_model="gemini-3.6-flash",
        groq_api_key="",
        groq_model="llama-3.3-70b-versatile",
        anthropic_api_key="",
        anthropic_model="claude-haiku-4-5-20251001",
        llm_max_calls_per_run=200,
        payment_mode="simulated",
        razorpay_key_id="",
        razorpay_key_secret="",
        razorpay_webhook_secret="test_webhook_secret",
        reconcile_poll_interval_seconds=30,
        pending_reconciliation_threshold_seconds=30,
        demo_passphrase="",
        demo_max_calls_per_session=20,
        demo_daily_call_budget=50,
        data_dir=str(tmp_path),
    )


def _payment_stack(mcp_stack: SimpleNamespace, tmp_path: Path) -> PaymentStack:
    return build_payment_stack(_config(tmp_path), ledger=mcp_stack.ledger, data_dir=tmp_path)


def _runner(
    mcp_stack: SimpleNamespace, tmp_path: Path, scripted_responses: list, *, stack: PaymentStack
) -> BuyerSessionRunner:
    llm = FakeLLMClient(scripted_responses)
    agent = BuyerAgent(llm)
    engine = PolicyEngine(compile_policy(REPO_POLICY_PATH))
    policy = PolicyService(engine, mcp_stack.ledger)
    return BuyerSessionRunner(
        agent=agent,
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        catalog=mcp_stack.catalog,
        ledger=mcp_stack.ledger,
        policy=policy,
        payment=stack.adapter,
        simulated_payment_adapter=stack.simulated_adapter,
    )


_CONSTRAINTS_RESPONSE = tool_response(
    "extract_buyer_constraints",
    {
        "budget_ceiling_paise": 200000,
        "soft_target_paise": None,
        "category": "Toys & Games",
        "recipient_context": "10-year-old nephew",
        "must_have": [],
        "deadline": None,
    },
)


# --- Mechanism: precedence, validation, ledger logging ----------------------------------


async def test_unknown_inject_failure_raises_with_valid_options_listed(mcp_stack, tmp_path) -> None:
    stack = _payment_stack(mcp_stack, tmp_path)
    runner = _runner(mcp_stack, tmp_path, [], stack=stack)

    with pytest.raises(ValueError) as exc_info:
        await runner.run("txn_bad_flag", "buy something", inject_failure="not_a_real_failure")

    message = str(exc_info.value)
    for known in KNOWN_INJECTIONS:
        assert known in message


async def test_env_var_is_used_when_no_explicit_param(monkeypatch, mcp_stack, tmp_path) -> None:
    monkeypatch.setenv("INJECT_FAILURE", "stock_conflict")
    stack = _payment_stack(mcp_stack, tmp_path)
    responses = [_CONSTRAINTS_RESPONSE, text_response("nothing suitable")]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    result = await runner.run("txn_env_fallback", "buy something")

    assert result.injected_failure == "stock_conflict"


async def test_explicit_param_takes_precedence_over_env_var(monkeypatch, mcp_stack, tmp_path) -> None:
    monkeypatch.setenv("INJECT_FAILURE", "stock_conflict")
    stack = _payment_stack(mcp_stack, tmp_path)
    responses = [_CONSTRAINTS_RESPONSE, text_response("nothing suitable")]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    result = await runner.run("txn_explicit_wins", "buy something", inject_failure="payment_failure")

    assert result.injected_failure == "payment_failure"


async def test_injected_failure_is_logged_to_the_ledger(mcp_stack, tmp_path) -> None:
    stack = _payment_stack(mcp_stack, tmp_path)
    txn = "txn_injection_logged"
    responses = [_CONSTRAINTS_RESPONSE, text_response("nothing suitable")]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    await runner.run(txn, "buy something", inject_failure="stock_conflict")

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    injected = [e for e in entries if e.machine_reason == "INJECTED_FAILURE_STOCK_CONFLICT"]
    assert len(injected) == 1
    assert "stock_conflict" in injected[0].human_reason


async def test_missing_webhook_injection_requires_simulated_mode(mcp_stack, tmp_path) -> None:
    stack = _payment_stack(mcp_stack, tmp_path)
    runner = BuyerSessionRunner(
        agent=BuyerAgent(FakeLLMClient([_CONSTRAINTS_RESPONSE])),
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        catalog=mcp_stack.catalog,
        ledger=mcp_stack.ledger,
        policy=PolicyService(PolicyEngine(compile_policy(REPO_POLICY_PATH)), mcp_stack.ledger),
        payment=stack.adapter,
        # simulated_payment_adapter deliberately omitted
    )

    with pytest.raises(ValueError, match="simulated"):
        await runner.run("txn_no_sim_ref", "buy something", inject_failure="missing_webhook")


# --- 1. stock_conflict: injected, then recovered --------------------------------------------


async def test_stock_conflict_injection_and_recovery(mcp_stack, tmp_path) -> None:
    stack = _payment_stack(mcp_stack, tmp_path)
    txn = "txn_stock_conflict"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # hits STOCK_CONFLICT
        tool_response("cart.remove", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0004", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # succeeds
    ]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    result = await runner.run(txn, "buy a toy for my nephew", inject_failure="stock_conflict")

    assert result.outcome == "order_created"
    assert result.cart_view["items"][0]["sku"] == "SKU-0004"
    assert result.order is not None
    assert result.denial_reason is None

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    conflict_entries = [e for e in entries if e.machine_reason == "STOCK_CONFLICT"]
    assert len(conflict_entries) == 1
    conflict_entry = conflict_entries[0]
    assert "SKU-0001" in conflict_entry.human_reason
    assert "0 left in stock" in conflict_entry.human_reason

    # The recovery action (removing the conflicted item) must be causally linked to the
    # conflict itself, not floating disconnected in the ledger.
    remove_entries = [
        e for e in entries if e.action_type == ActionType.SELECT and e.input.get("op") == "remove"
    ]
    assert len(remove_entries) == 1
    assert remove_entries[0].caused_by == [conflict_entry.entry_id]

    assert mcp_stack.ledger.verify_chain().ok is True


# --- 2. payment_failure: bounded retry, same idempotency key --------------------------------


async def test_payment_failure_injection_retries_with_same_idempotency_key(mcp_stack, tmp_path) -> None:
    stack = _payment_stack(mcp_stack, tmp_path)
    txn = "txn_payment_failure"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    result = await runner.run(txn, "buy a toy", inject_failure="payment_failure")

    assert result.outcome == "order_created"
    assert result.order["receipt"] == f"{txn}:1"

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    payment_entries = [e for e in entries if e.action_type == ActionType.PAYMENT_CALL]
    assert len(payment_entries) == 2
    assert payment_entries[0].machine_reason == "PAYMENT_RETRY"
    assert payment_entries[1].output["order_id"] == result.order["order_id"]

    # THE single most important assertion in this phase: the retry reused the same
    # idempotency key (transaction_id:1, never incremented) and only one order row exists for
    # this transaction — not a second order created by the retry.
    row_count = stack.order_store._conn.execute(
        "SELECT COUNT(*) FROM orders WHERE transaction_id = ?", (txn,)
    ).fetchone()[0]
    assert row_count == 1


async def test_payment_failure_exhausting_retry_budget_aborts_with_ledger_explanation(
    mcp_stack, tmp_path
) -> None:
    txn = "txn_payment_abort"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    runner = BuyerSessionRunner(
        agent=BuyerAgent(FakeLLMClient(responses)),
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        catalog=mcp_stack.catalog,
        ledger=mcp_stack.ledger,
        policy=PolicyService(PolicyEngine(compile_policy(REPO_POLICY_PATH)), mcp_stack.ledger),
        payment=_AlwaysFailingPaymentAdapter(),
    )

    result = await runner.run(txn, "buy a toy")

    assert result.outcome == "payment_failed"
    assert "gateway outage" in result.denial_reason

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    payment_entries = [e for e in entries if e.action_type == ActionType.PAYMENT_CALL]
    assert len(payment_entries) == 2
    assert payment_entries[0].machine_reason == "PAYMENT_RETRY"
    assert payment_entries[1].machine_reason == "PAYMENT_ABORTED"


# --- 3. missing_webhook: order created, no webhook, poller path is what catches it ----------


async def test_missing_webhook_injection_leaves_order_unfulfilled(mcp_stack, tmp_path) -> None:
    stack = _payment_stack(mcp_stack, tmp_path)
    txn = "txn_missing_webhook"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    result = await runner.run(txn, "buy a toy", inject_failure="missing_webhook")

    assert result.outcome == "order_created"
    order_id = result.order["order_id"]

    webhook_entries = [
        e for e in mcp_stack.ledger.entries_for_transaction(txn) if e.action_type == ActionType.WEBHOOK
    ]
    assert webhook_entries == []
    assert stack.order_store.get(order_id).status != OrderStatus.PAID

    # The payment genuinely succeeded (a fresh GET would confirm it) — only the webhook is
    # missing, and reconciling right away is still PENDING (normal latency), not an alarm.
    # This is the observable proof that "order created" != "paid" in this system. The
    # time-based escalation to pending_reconciliation once that gap persists is covered by
    # test_payments_reconciler.py directly.
    poll_result = stack.reconciler.reconcile(order_id)
    assert poll_result.status == ReconciliationStatus.PENDING


# --- 4. policy_deny_recovery: a real DENY the agent must actually act on --------------------


async def test_policy_deny_recovery_injection_and_recovery(mcp_stack, tmp_path) -> None:
    stack = _payment_stack(mcp_stack, tmp_path)
    txn = "txn_policy_deny_recovery"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # forced DENY
        tool_response("cart.remove", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("catalog.search", {"transaction_id": txn, "category": "Books"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0016", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # succeeds
    ]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    result = await runner.run(txn, "buy a toy for my nephew", inject_failure="policy_deny_recovery")

    assert result.outcome == "order_created"
    assert result.cart_view["items"][0]["sku"] == "SKU-0016"
    assert result.denial_reason is None

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    deny_entries = [
        e
        for e in entries
        if e.action_type == ActionType.POLICY_CHECK and e.policy_verdict == PolicyVerdict.DENY
    ]
    assert len(deny_entries) == 1
    deny_entry = deny_entries[0]
    assert "exceeds buyer budget ceiling" in deny_entry.human_reason

    # The buyer agent's recovery action (dropping the too-expensive item) must be caused by
    # the denial it's reacting to, not a coincidence — this is what makes it genuine recovery
    # rather than a blind retry that happened to work.
    remove_entries = [
        e for e in entries if e.action_type == ActionType.SELECT and e.input.get("op") == "remove"
    ]
    assert len(remove_entries) == 1
    assert remove_entries[0].caused_by == [deny_entry.entry_id]

    assert mcp_stack.ledger.verify_chain().ok is True


async def test_budget_denied_result_includes_an_actionable_hint(mcp_stack, tmp_path) -> None:
    # A bare human_reason string proved too weak in practice — a live agent repeatedly added a
    # second item on top of the over-budget one instead of removing it, making the total worse.
    # The DENY result for a budget-ceiling denial must carry the exact numbers and an explicit
    # instruction, not just prose the model has to interpret unaided.
    stack = _payment_stack(mcp_stack, tmp_path)
    txn = "txn_deny_hint"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # forced DENY
        text_response("stopping to inspect the hint"),
    ]
    llm = FakeLLMClient(responses)
    agent = BuyerAgent(llm)
    engine = PolicyEngine(compile_policy(REPO_POLICY_PATH))
    policy = PolicyService(engine, mcp_stack.ledger)
    runner = BuyerSessionRunner(
        agent=agent,
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        catalog=mcp_stack.catalog,
        ledger=mcp_stack.ledger,
        policy=policy,
        payment=stack.adapter,
        simulated_payment_adapter=stack.simulated_adapter,
    )

    await runner.run(txn, "buy a toy for my nephew", inject_failure="policy_deny_recovery")

    # The DENY result is whatever tool message follows the checkout.confirm call in the
    # final (text-only) turn's request history.
    last_call_messages = llm.calls[-1]["messages"]
    tool_messages = [m for m in last_call_messages if m.role == "tool"]
    deny_result = json.loads(tool_messages[-1].content)

    assert deny_result["error"] == "policy_denied"
    assert deny_result["cart_total_paise"] == 89900
    assert deny_result["budget_ceiling_paise"] == 44950
    assert "remove" in deny_result["hint"].lower()
    assert "do not add another item" in deny_result["hint"].lower()


async def test_policy_deny_recovery_ceiling_persists_so_blind_retry_cannot_succeed(
    mcp_stack, tmp_path
) -> None:
    # If the agent just retries checkout.confirm unchanged after the denial, it must keep
    # failing — otherwise the injection wouldn't actually be forcing recovery, just a delay.
    stack = _payment_stack(mcp_stack, tmp_path)
    txn = "txn_policy_deny_blind_retry"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # forced DENY
        tool_response("checkout.confirm", {"transaction_id": txn}),  # blind retry: still DENY
        text_response("giving up"),
    ]
    runner = _runner(mcp_stack, tmp_path, responses, stack=stack)

    result = await runner.run(txn, "buy a toy", inject_failure="policy_deny_recovery")

    assert result.outcome == "policy_denied"
    entries = mcp_stack.ledger.entries_for_transaction(txn)
    deny_entries = [
        e
        for e in entries
        if e.action_type == ActionType.POLICY_CHECK and e.policy_verdict == PolicyVerdict.DENY
    ]
    assert len(deny_entries) == 2
