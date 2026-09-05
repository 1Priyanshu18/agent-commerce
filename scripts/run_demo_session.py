"""Thin CLI wrapper around BuyerSessionRunner — the live demo entry point.

Deliberately has no logic of its own beyond argument parsing, wiring the real components
together the same way the app does, and printing the resulting ledger trace: there's nothing
here that can drift out of sync with how a session actually runs, because it doesn't
reimplement anything about how a session runs.

Usage:
    python scripts/run_demo_session.py --goal "buy a birthday gift under Rs 2000"
    python scripts/run_demo_session.py --goal "buy a birthday gift" --inject-failure stock_conflict
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ledger human_reason text includes rupee amounts (₹) — force UTF-8 stdout so this doesn't
# crash under Windows' default console codepage (cp1252), which can't encode U+20B9.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.cart.service import CartService
from agent_commerce.catalog.service import CatalogService
from agent_commerce.catalog.store import CatalogStore
from agent_commerce.core.config import load_config
from agent_commerce.core.ids import generate_id
from agent_commerce.core.llm import build_client
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.mcp.buyer_server import build_buyer_server
from agent_commerce.orchestrator.run_session import KNOWN_INJECTIONS, BuyerSessionRunner
from agent_commerce.orchestrator.session import SessionRegistry
from agent_commerce.payments import build_payment_stack
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

REPO_ROOT = Path(__file__).resolve().parent.parent


def _render_ledger_trace(ledger: LedgerStore, transaction_id: str) -> None:
    print(f"\n--- Ledger trace for {transaction_id} ---")
    for entry in ledger.entries_for_transaction(transaction_id):
        header = f"[{entry.action_type.value}] actor={entry.actor.value}"
        if entry.machine_reason:
            header += f" machine_reason={entry.machine_reason}"
        if entry.policy_verdict:
            header += f" policy_verdict={entry.policy_verdict.value}"
        print(header)
        if entry.human_reason:
            print(f"    human_reason: {entry.human_reason}")
        if entry.reasoning_summary:
            print(f"    reasoning: {entry.reasoning_summary}")

    verification = ledger.verify_chain()
    print(f"\nverify_chain(): ok={verification.ok} entries_checked={verification.entries_checked}")
    if not verification.ok:
        print(f"    error: {verification.error}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--goal", required=True, help="Natural-language buyer goal, e.g. 'buy a birthday gift under Rs 2000'"
    )
    parser.add_argument(
        "--inject-failure",
        choices=sorted(KNOWN_INJECTIONS),
        default=None,
        help="Force one of the four failure paths for this session, reproducibly.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Bypass the LLM response cache for this run's calls only. Use this to isolate a "
            "cache-invalidation problem — never delete .cache/llm/ by hand, since a cold cache "
            "burns real quota on every subsequent run."
        ),
    )
    args = parser.parse_args()

    config = load_config()

    catalog = CatalogStore()
    ledger = LedgerStore(Path(config.data_dir) / "demo_ledger.db")
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

    payment_stack = build_payment_stack(config, ledger=ledger, data_dir=config.data_dir)

    llm = build_client(config, bypass_cache=args.no_cache)
    agent = BuyerAgent(llm)

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

    transaction_id = generate_id("txn_demo")
    print(f"transaction_id={transaction_id}")
    print(f"goal: {args.goal}")
    if args.inject_failure:
        print(f"injecting failure: {args.inject_failure}")

    result = asyncio.run(runner.run(transaction_id, args.goal, inject_failure=args.inject_failure))

    print(f"\noutcome: {result.outcome}")
    if result.denial_reason:
        print(f"reason: {result.denial_reason}")
    if result.order:
        print(f"order: {result.order}")

    _render_ledger_trace(ledger, transaction_id)


if __name__ == "__main__":
    main()
