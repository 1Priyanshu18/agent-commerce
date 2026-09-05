"""Phase 8 eval grid runner. See docs/PHASE_8_SPEC.md for the full spec and the tiered-plan
amendment. Usage:

    python -m eval.runner --dry-run --tier A
    python -m eval.runner --tier A
    python -m eval.runner --tier B   # resumes from eval/results.json, only runs new cells

Never wipes eval/results.json or the LLM response cache — both are checkpoints, and both are
appended-to incrementally so an interruption never loses completed work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.agents.upsell.llm import LLMStrategy
from agent_commerce.agents.upsell.none import NoneStrategy
from agent_commerce.agents.upsell.rules import RulesStrategy
from agent_commerce.agents.upsell.strategy import MerchantRules
from agent_commerce.cart.service import CartService
from agent_commerce.catalog.service import CatalogService
from agent_commerce.catalog.store import CatalogStore
from agent_commerce.core.config import load_config
from agent_commerce.core.llm import build_client
from agent_commerce.demo.usage_tracker import UsageTrackingLLMClient
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.mcp.buyer_server import build_buyer_server
from agent_commerce.mcp.merchant_server import build_merchant_server
from agent_commerce.orchestrator.run_session import BuyerSessionRunner
from agent_commerce.orchestrator.session import SessionRegistry
from agent_commerce.payments import build_payment_stack
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

from .goal_loader import Goal, load_goals
from .metrics import SessionMetrics, compute_session_metrics

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = Path(__file__).parent / "results.json"

CONDITIONS = ["none", "rules", "llm"]
ENFORCEMENT_LEVELS = ["tool_level_only", "argument_level"]
BLACKLIST_SKUS = frozenset({"SKU-0042"})

# Measured, not guessed — from 103 real Groq calls during Phase 7's live demo runs (see
# docs/PROGRESS.md). Used only for --dry-run's estimate; a real run reports real numbers.
MEASURED_AVG_TOKENS_PER_CALL = 2558
MEASURED_AVG_CALLS_PER_SESSION = 15

TIER_DEFAULTS = {"A": (10, 1), "B": (20, 1), "C": (20, 3)}


def _cell_id(condition: str, enforcement_level: str, goal_id: str, seed: int) -> str:
    return f"{condition}__{enforcement_level}__{goal_id}__seed{seed}"


def _model_for(config) -> str:
    return {
        "gemini": config.gemini_model,
        "groq": config.groq_model,
        "anthropic": config.anthropic_model,
    }[config.llm_provider]


def _load_checkpoint() -> dict[str, dict]:
    if not RESULTS_PATH.exists():
        return {}
    with open(RESULTS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {r["cell_id"]: r for r in data.get("sessions", [])}


def _save_checkpoint(sessions: list[dict], meta: dict) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "sessions": sessions}, f, indent=2, default=str)


def _build_upsell_strategy(condition: str, catalog: CatalogStore, llm_client):
    if condition == "none":
        return NoneStrategy()
    if condition == "rules":
        return RulesStrategy(catalog)
    if condition == "llm":
        return LLMStrategy(llm_client, catalog)
    raise ValueError(f"unknown condition: {condition!r}")


def build_grid(num_goals: int, num_seeds: int) -> list[tuple[str, str, Goal, int]]:
    # goal, seed outermost: the 6 (condition, enforcement_level) cells for one (goal, seed)
    # run back-to-back, so the shared buyer trajectory they have in common (see "trajectory
    # replay" in docs/PHASE_8_SPEC.md) is still warm in the LLM response cache when the next
    # cell needs it, and cache-hit/miss can be measured cleanly per goal.
    goals = load_goals(limit=num_goals)
    return [
        (condition, enforcement_level, goal, seed)
        for goal in goals
        for seed in range(1, num_seeds + 1)
        for condition in CONDITIONS
        for enforcement_level in ENFORCEMENT_LEVELS
    ]


async def run_cell(
    *,
    condition: str,
    enforcement_level: str,
    goal: Goal,
    seed: int,
    tracker: UsageTrackingLLMClient,
    provider: str,
    model: str,
    data_dir: Path,
) -> SessionMetrics:
    catalog = CatalogStore()  # frozen snapshot, fresh per cell
    ledger = LedgerStore(":memory:")
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
    merchant_mcp = build_merchant_server(catalog=catalog, sessions=sessions, ledger=ledger)

    policy_engine = PolicyEngine(
        compile_policy(REPO_ROOT / "policies" / "default.yaml"),
        tool_level_only=(enforcement_level == "tool_level_only"),
    )
    policy_service = PolicyService(policy_engine, ledger)

    config = load_config()
    payment_stack = build_payment_stack(config, ledger=ledger, data_dir=data_dir)

    start_call_index = len(tracker.calls)
    agent = BuyerAgent(tracker)
    merchant_rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=BLACKLIST_SKUS)
    upsell_strategy = _build_upsell_strategy(condition, catalog, tracker)

    runner = BuyerSessionRunner(
        agent=agent,
        buyer_mcp=buyer_mcp,
        sessions=sessions,
        catalog=catalog,
        ledger=ledger,
        policy=policy_service,
        payment=payment_stack.adapter,
        simulated_payment_adapter=payment_stack.simulated_adapter,
        upsell_strategy=upsell_strategy,
        merchant_rules=merchant_rules,
        merchant_mcp=merchant_mcp,
    )

    transaction_id = _cell_id(condition, enforcement_level, goal.goal_id, seed)
    t0 = time.monotonic()
    result = await runner.run(transaction_id, goal.goal_text)
    wall_clock = time.monotonic() - t0

    cell_calls = tracker.calls[start_call_index:]
    return compute_session_metrics(
        goal=goal,
        result=result,
        ledger=ledger,
        transaction_id=transaction_id,
        condition=condition,
        enforcement_level=enforcement_level,
        provider=provider,
        model=model,
        seed=seed,
        wall_clock_seconds=wall_clock,
        llm_calls=cell_calls,
    )


def dry_run_report(num_goals: int, num_seeds: int) -> None:
    cells = build_grid(num_goals, num_seeds)
    checkpoint = _load_checkpoint()
    remaining = [c for c in cells if _cell_id(c[0], c[1], c[2].goal_id, c[3]) not in checkpoint]

    total_sessions = len(cells)
    remaining_sessions = len(remaining)
    total_calls = remaining_sessions * MEASURED_AVG_CALLS_PER_SESSION
    total_tokens = total_calls * MEASURED_AVG_TOKENS_PER_CALL

    print(f"Grid: {len(CONDITIONS)} conditions x {len(ENFORCEMENT_LEVELS)} enforcement levels "
          f"x {num_goals} goals x {num_seeds} seed(s) = {total_sessions} sessions")
    print(f"Already checkpointed: {total_sessions - remaining_sessions}")
    print(f"Remaining to run: {remaining_sessions}")
    print(f"Estimated LLM calls: {total_calls} (~{MEASURED_AVG_CALLS_PER_SESSION}/session)")
    print(
        f"Estimated tokens: {total_tokens:,} (~{MEASURED_AVG_TOKENS_PER_CALL}/call, measured "
        "from Phase 7 live Groq runs — cache hits on repeat cells cost nothing extra)"
    )
    print(f"  vs Groq free-tier RPD=1,000: {total_calls / 1000:.2f} days needed")
    print(f"  vs Groq free-tier TPD=200,000: {total_tokens / 200_000:.2f} days needed")


async def main_async(args: SimpleNamespace) -> None:
    checkpoint = _load_checkpoint()
    sessions_results: list[dict] = list(checkpoint.values())

    config = load_config()
    raw_client = build_client(config)  # cache + rate-limit + retry, shared for the whole run
    tracker = UsageTrackingLLMClient(raw_client)
    provider = config.llm_provider
    model = _model_for(config)

    data_dir = REPO_ROOT / ".cache" / "eval_data"
    cells = build_grid(args.num_goals, args.num_seeds)
    total_cells = len(cells)
    done = sum(1 for c in cells if _cell_id(c[0], c[1], c[2].goal_id, c[3]) in checkpoint)

    cumulative_tokens = 0
    for condition, enforcement_level, goal, seed in cells:
        cell_id = _cell_id(condition, enforcement_level, goal.goal_id, seed)
        if cell_id in checkpoint:
            continue

        if cumulative_tokens >= args.max_tokens_budget:
            print(
                f"\nABORTING: cumulative token budget ({args.max_tokens_budget:,}) reached "
                f"after {done}/{total_cells} sessions. Results so far are saved at "
                f"{RESULTS_PATH} — rerun the same command to resume."
            )
            return

        start_idx = len(tracker.calls)
        print(f"[{done + 1}/{total_cells}] running {cell_id}...", flush=True)
        metrics = await run_cell(
            condition=condition,
            enforcement_level=enforcement_level,
            goal=goal,
            seed=seed,
            tracker=tracker,
            provider=provider,
            model=model,
            data_dir=data_dir,
        )
        cell_calls = tracker.calls[start_idx:]
        real_calls = sum(1 for c in cell_calls if not c["cached"])
        cache_hits = len(cell_calls) - real_calls
        print(
            f"    -> {metrics.outcome}, {len(cell_calls)} LLM calls "
            f"({real_calls} real, {cache_hits} cache hits), "
            f"{metrics.input_tokens + metrics.output_tokens} real tokens",
            flush=True,
        )
        cumulative_tokens += metrics.input_tokens + metrics.output_tokens
        record = {"cell_id": cell_id, **asdict(metrics)}
        sessions_results.append(record)
        _save_checkpoint(
            sessions_results,
            meta={
                "provider": provider,
                "model": model,
                "run_date": datetime.now(UTC).isoformat(),
                "num_goals": args.num_goals,
                "num_seeds": args.num_seeds,
            },
        )
        done += 1

    print(f"\nDone: {done}/{total_cells} sessions completed. Results at {RESULTS_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tier", choices=["A", "B", "C"], default=None, help="Sets --num-goals/--num-seeds defaults."
    )
    parser.add_argument("--num-goals", type=int, default=None)
    parser.add_argument("--num-seeds", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Report planned calls/tokens, spend nothing.")
    parser.add_argument(
        "--max-tokens-budget",
        type=int,
        default=150_000,
        help="Hard cumulative token budget for this invocation; aborts loudly (not silently) once reached.",
    )
    args = parser.parse_args()

    default_goals, default_seeds = TIER_DEFAULTS.get(args.tier, (10, 1))
    num_goals = args.num_goals if args.num_goals is not None else default_goals
    num_seeds = args.num_seeds if args.num_seeds is not None else default_seeds

    if args.dry_run:
        dry_run_report(num_goals, num_seeds)
        return

    asyncio.run(
        main_async(
            SimpleNamespace(
                num_goals=num_goals, num_seeds=num_seeds, max_tokens_budget=args.max_tokens_budget
            )
        )
    )


if __name__ == "__main__":
    main()
