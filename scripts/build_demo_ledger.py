"""Builds demo_data/demo_ledger.db — the curated, committed, read-only ledger the Streamlit
app's Session replay tab reads (Phase 9, docs/PHASE_9_SPEC.md).

Makes NO real LLM calls: every session here is driven by FakeLLMClient with the exact same
scripted tool-call sequences already proven correct by the test suite (see
tests/test_orchestrator_run_session.py and tests/test_orchestrator_failure_injection.py) —
this script exists to make those same, already-verified sessions durably visible in the demo
app, not to test anything new.

Four sessions, matching the spec's "interesting sessions" list:
  1. happy path (no injection, straightforward order_created)
  2. stock_conflict, with genuine recovery (remove -> add a different item -> order_created)
  3. policy_deny_recovery, with genuine recovery (remove -> add a cheaper item -> order_created)
  4. a structural role violation (an actor calling a tool outside its allowed set)

Usage:
    python scripts/build_demo_ledger.py
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.cart.service import CartService
from agent_commerce.catalog.service import CatalogService
from agent_commerce.catalog.store import CatalogStore
from agent_commerce.core.config import Config
from agent_commerce.core.llm import FakeLLMClient, tool_response
from agent_commerce.ledger.models import Actor
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.mcp.authz import RoleViolationError, authorize
from agent_commerce.mcp.buyer_server import build_buyer_server
from agent_commerce.orchestrator.run_session import BuyerSessionRunner
from agent_commerce.orchestrator.session import SessionRegistry
from agent_commerce.payments import build_payment_stack
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "demo_data" / "demo_ledger.db"

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


def _build_session_stack(ledger: LedgerStore, data_dir: Path):
    catalog = CatalogStore()
    sessions = SessionRegistry()
    catalog_service = CatalogService(catalog, ledger)
    cart_service = CartService(catalog, ledger)
    buyer_mcp = build_buyer_server(
        catalog=catalog,
        catalog_service=catalog_service,
        cart_service=cart_service,
        sessions=sessions,
        ledger=ledger,
    )
    policy_engine = PolicyEngine(compile_policy(REPO_ROOT / "policies" / "default.yaml"))
    policy_service = PolicyService(policy_engine, ledger)
    config = Config(
        app_env="demo",
        log_level="INFO",
        llm_provider="groq",
        gemini_api_key="",
        gemini_model="gemini-3.6-flash",
        groq_api_key="",
        groq_model="openai/gpt-oss-120b",
        anthropic_api_key="",
        anthropic_model="claude-haiku-4-5-20251001",
        llm_max_calls_per_run=200,
        payment_mode="simulated",
        razorpay_key_id="",
        razorpay_key_secret="",
        razorpay_webhook_secret="demo_webhook_secret",
        reconcile_poll_interval_seconds=30,
        pending_reconciliation_threshold_seconds=30,
        demo_passphrase="",
        demo_max_calls_per_session=20,
        demo_daily_call_budget=50,
        data_dir=str(data_dir),
    )
    payment_stack = build_payment_stack(config, ledger=ledger, data_dir=data_dir)
    return catalog, sessions, buyer_mcp, policy_service, payment_stack


async def build_happy_path(ledger: LedgerStore, data_dir: Path) -> str:
    txn = "demo_happy_path"
    catalog, sessions, buyer_mcp, policy_service, payment_stack = _build_session_stack(
        ledger, data_dir / "happy"
    )
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response(
            "catalog.search", {"transaction_id": txn, "category": "Toys & Games", "max_price_paise": 200000}
        ),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    agent = BuyerAgent(FakeLLMClient(responses))
    runner = BuyerSessionRunner(
        agent=agent,
        buyer_mcp=buyer_mcp,
        sessions=sessions,
        catalog=catalog,
        ledger=ledger,
        policy=policy_service,
        payment=payment_stack.adapter,
        simulated_payment_adapter=payment_stack.simulated_adapter,
    )
    result = await runner.run(txn, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")
    assert result.outcome == "order_created", f"happy path build failed: {result.outcome}"
    return txn


async def build_stock_conflict(ledger: LedgerStore, data_dir: Path) -> str:
    txn = "demo_stock_conflict"
    catalog, sessions, buyer_mcp, policy_service, payment_stack = _build_session_stack(
        ledger, data_dir / "stock"
    )
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # hits STOCK_CONFLICT
        tool_response("cart.remove", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0004", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),  # succeeds
    ]
    agent = BuyerAgent(FakeLLMClient(responses))
    runner = BuyerSessionRunner(
        agent=agent,
        buyer_mcp=buyer_mcp,
        sessions=sessions,
        catalog=catalog,
        ledger=ledger,
        policy=policy_service,
        payment=payment_stack.adapter,
        simulated_payment_adapter=payment_stack.simulated_adapter,
    )
    result = await runner.run(
        txn, "buy a toy for my nephew", inject_failure="stock_conflict"
    )
    assert result.outcome == "order_created", f"stock_conflict build failed: {result.outcome}"
    return txn


async def build_policy_deny_recovery(ledger: LedgerStore, data_dir: Path) -> str:
    txn = "demo_policy_deny_recovery"
    catalog, sessions, buyer_mcp, policy_service, payment_stack = _build_session_stack(
        ledger, data_dir / "deny"
    )
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
    agent = BuyerAgent(FakeLLMClient(responses))
    runner = BuyerSessionRunner(
        agent=agent,
        buyer_mcp=buyer_mcp,
        sessions=sessions,
        catalog=catalog,
        ledger=ledger,
        policy=policy_service,
        payment=payment_stack.adapter,
        simulated_payment_adapter=payment_stack.simulated_adapter,
    )
    result = await runner.run(
        txn, "buy a toy for my nephew", inject_failure="policy_deny_recovery"
    )
    assert result.outcome == "order_created", f"policy_deny_recovery build failed: {result.outcome}"
    return txn


def build_role_violation(ledger: LedgerStore) -> str:
    txn = "demo_role_violation"
    try:
        # The upsell agent's role is structurally confined to cart.read_at_checkout /
        # upsell.make_offer / upsell.no_offer (see mcp/merchant_server.py) — this call is
        # outside that set. authorize() is the defense-in-depth check every tool handler
        # calls first; it logs a role_violation entry and raises before anything else happens.
        authorize(Actor.UPSELL_AGENT, "cart.add", ledger, transaction_id=txn, caused_by=[])
    except RoleViolationError:
        pass
    return txn


async def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()  # rebuild from scratch each time this script runs

    ledger = LedgerStore(OUTPUT_PATH)
    data_dir = OUTPUT_PATH.parent / ".build_tmp"

    happy = await build_happy_path(ledger, data_dir)
    stock = await build_stock_conflict(ledger, data_dir)
    deny = await build_policy_deny_recovery(ledger, data_dir)
    role = build_role_violation(ledger)

    verification = ledger.verify_chain()
    print(f"Sessions written: {happy}, {stock}, {deny}, {role}")
    print(f"verify_chain(): ok={verification.ok} entries_checked={verification.entries_checked}")
    print(f"Written to {OUTPUT_PATH}")

    shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
