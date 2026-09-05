"""Streamlit demo app. A thin view layer: all logic lives in agent_commerce/ and eval/,
and nothing under src/ imports Streamlit.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

import streamlit as st

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.cart.service import CartService
from agent_commerce.catalog.service import CatalogService
from agent_commerce.catalog.store import CatalogStore
from agent_commerce.core.config import Config, load_config
from agent_commerce.core.ids import generate_id
from agent_commerce.core.llm import build_client
from agent_commerce.demo.budget import DailyBudgetTracker
from agent_commerce.demo.eval_loader import (
    CONDITIONS,
    ENFORCEMENT_LEVELS,
    cell_coverage,
    goals_covered,
    load_eval_results,
    load_injection_results,
)
from agent_commerce.demo.passphrase import check_passphrase
from agent_commerce.demo.usage_tracker import UsageTrackingLLMClient
from agent_commerce.ledger.models import Actor, LedgerEntry, PolicyVerdict
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.mcp.buyer_server import build_buyer_server
from agent_commerce.orchestrator.run_session import KNOWN_INJECTIONS, BuyerSessionRunner
from agent_commerce.orchestrator.session import SessionRegistry
from agent_commerce.payments import build_payment_stack
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

REPO_ROOT = Path(__file__).resolve().parent
DEMO_LEDGER_PATH = REPO_ROOT / "demo_data" / "demo_ledger.db"
BUDGET_STATE_PATH = REPO_ROOT / "demo_data" / "daily_call_budget.json"
POLICY_PATH = REPO_ROOT / "policies" / "default.yaml"
EVAL_RESULTS_PATH = REPO_ROOT / "eval" / "results.json"
INJECTION_RESULTS_PATH = REPO_ROOT / "eval" / "injection_results.json"
REPORT_PATH = REPO_ROOT / "eval" / "report.md"
MARGIN_PLOT_PATH = REPO_ROOT / "eval" / "plot_margin_uplift.png"
SCATTER_PLOT_PATH = REPO_ROOT / "eval" / "plot_false_block_vs_prevention.png"

# Streamlit's :color[] directive only supports a fixed palette; "teal" isn't in it and
# silently breaks. "violet" stands in, kept distinct from VERDICT_COLOR's "blue".
ACTOR_COLOR = {
    Actor.BUYER_AGENT: "violet",
    Actor.UPSELL_AGENT: "violet",
    Actor.POLICY_ENGINE: "orange",
    Actor.PAYMENT_LAYER: "grey",
    Actor.ORCHESTRATOR: "grey",
}
VERDICT_COLOR = {
    PolicyVerdict.ALLOW: "green",
    PolicyVerdict.TRANSFORM: "blue",
    PolicyVerdict.REQUIRE_APPROVAL: "orange",
    PolicyVerdict.DENY: "red",
}

@st.cache_resource
def get_demo_ledger() -> LedgerStore:
    # read_only so this works even if the app's filesystem is read-only.
    return LedgerStore(DEMO_LEDGER_PATH, read_only=True)


@st.cache_data
def get_eval_results() -> dict:
    return load_eval_results(EVAL_RESULTS_PATH)


@st.cache_data
def get_injection_results() -> dict:
    return load_injection_results(INJECTION_RESULTS_PATH)


@st.cache_data
def get_report_sections() -> dict[str, str]:
    if not REPORT_PATH.exists():
        return {}
    text = REPORT_PATH.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current_title = "preamble"
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections[current_title] = "\n".join(current_lines).strip()
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    sections[current_title] = "\n".join(current_lines).strip()
    return sections


def _model_for(config: Config) -> str:
    return {
        "gemini": config.gemini_model,
        "groq": config.groq_model,
        "anthropic": config.anthropic_model,
    }.get(config.llm_provider, "unknown")


def _run_live_session(config: Config, goal_text: str, inject_failure: str | None) -> tuple:
    catalog = CatalogStore()
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
    policy_engine = PolicyEngine(compile_policy(POLICY_PATH))
    policy_service = PolicyService(policy_engine, ledger)

    with tempfile.TemporaryDirectory() as tmp:
        payment_stack = build_payment_stack(config, ledger=ledger, data_dir=Path(tmp))
        # Take the tighter of the demo's per-session cap and the app-wide one.
        capped_config = replace(
            config, llm_max_calls_per_run=min(config.llm_max_calls_per_run, config.demo_max_calls_per_session)
        )
        raw_client = build_client(capped_config)
        tracker = UsageTrackingLLMClient(raw_client)
        agent = BuyerAgent(tracker)
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
        transaction_id = generate_id("txn_live_demo")
        result = asyncio.run(runner.run(transaction_id, goal_text, inject_failure=inject_failure))
        real_calls = tracker.real_call_count()

    return transaction_id, result, ledger, real_calls


def render_live_run_tab(config: Config) -> None:
    st.caption(f"Provider: `{config.llm_provider}`  |  Model: `{_model_for(config)}` (read-only)")

    budget = DailyBudgetTracker(BUDGET_STATE_PATH, daily_budget=config.demo_daily_call_budget)
    budget_tripped = budget.is_tripped()
    st.progress(
        min(1.0, budget.calls_used_today() / max(1, budget.daily_budget())),
        text=f"Daily call budget: {budget.calls_used_today()} / {budget.daily_budget()} used today",
    )
    if budget_tripped:
        st.error("Daily call budget exhausted — live runs are disabled until it resets tomorrow.")

    if not config.demo_passphrase:
        st.warning("DEMO_PASSPHRASE is not configured on this deployment — the Live run tab stays locked.")

    passphrase = st.text_input("Passphrase", type="password", key="live_run_passphrase")
    unlocked = check_passphrase(passphrase, config.demo_passphrase)
    if passphrase and not unlocked:
        st.error("Incorrect passphrase.")

    goal_text = st.text_input(
        "Goal", value="Buy a birthday gift for my 10-year-old nephew, budget Rs 1500", key="live_run_goal"
    )
    inject_choice = st.selectbox("Inject failure", ["none", *sorted(KNOWN_INJECTIONS)], key="live_run_inject")

    can_run = unlocked and not budget_tripped
    if st.button("Run session", disabled=not can_run, type="primary"):
        inject_failure = None if inject_choice == "none" else inject_choice
        with st.spinner("Running session..."):
            transaction_id, result, ledger, real_calls = _run_live_session(config, goal_text, inject_failure)
        budget.record_calls(real_calls)

        st.success(
            f"Outcome: **{result.outcome}**  (transaction `{transaction_id}`, "
            f"{real_calls} real LLM calls)"
        )
        if result.denial_reason:
            st.info(f"Reason: {result.denial_reason}")
        if result.order:
            st.json(result.order)

        st.subheader("Ledger trace")
        placeholder = st.container()
        entries = ledger.entries_for_transaction(transaction_id)
        for entry in entries:
            with placeholder:
                render_ledger_entry(entry)

        verification = ledger.verify_chain()
        if verification.ok:
            st.success(f"Ledger integrity verified ({verification.entries_checked} entries)")
        else:
            st.error(f"Ledger integrity FAILED: {verification.error}")
    elif not can_run:
        st.caption("Enter the correct passphrase to enable the run button.")


def render_ledger_entry(entry: LedgerEntry) -> None:
    color = ACTOR_COLOR.get(entry.actor, "grey")
    header = f":{color}[**{entry.actor.value}**] — {entry.action_type.value}"
    if entry.machine_reason:
        header += f"  `{entry.machine_reason}`"
    if entry.policy_verdict is not None:
        vcolor = VERDICT_COLOR.get(entry.policy_verdict, "grey")
        header += f"  :{vcolor}[{entry.policy_verdict.value}]"
    with st.expander(header, expanded=False):
        if entry.human_reason:
            st.markdown(f"**Human reason:** {entry.human_reason}")
        if entry.reasoning_summary:
            st.markdown(f"**Reasoning:** {entry.reasoning_summary}")
        col1, col2 = st.columns(2)
        with col1:
            st.caption("input")
            st.json(entry.input)
        with col2:
            st.caption("output")
            st.json(entry.output)


def render_session_replay_tab() -> None:
    ledger = get_demo_ledger()
    transaction_ids = ledger.list_transaction_ids()
    if not transaction_ids:
        st.info(
            "No sessions found in the committed demo ledger. Run "
            "`python scripts/build_demo_ledger.py` to generate it."
        )
        return

    selected = st.selectbox("Session", transaction_ids)
    verification = ledger.verify_chain()
    if verification.ok:
        st.success(f"Ledger integrity verified ({verification.entries_checked} entries checked)")
    else:
        st.error(f"Ledger integrity FAILED at entry {verification.entries_checked}: {verification.error}")

    entries = ledger.entries_for_transaction(selected)
    st.caption(f"{len(entries)} entries in this session — vertical timeline, oldest first")
    for entry in entries:
        render_ledger_entry(entry)

    st.divider()
    st.subheader("Pending approvals (REQUIRE_APPROVAL queue)")
    st.caption(
        "None of the four committed demo sessions triggered a REQUIRE_APPROVAL verdict "
        "(that needs a cart total over ₹5,000) — this section is structurally present but "
        "empty for the committed demo data. A live run (Live run tab) that exceeds the "
        "high-value threshold will populate it for that session."
    )


def render_eval_tab() -> None:
    sections = get_report_sections()
    limitations_key = next((k for k in sections if k.lower().startswith("methodology")), None)
    if limitations_key:
        st.warning(sections[limitations_key])
    else:
        st.warning(
            "No eval report found yet — run `python -m eval.report` after the grid runner "
            "(`python -m eval.runner`) to generate one."
        )

    eval_data = get_eval_results()
    sessions = eval_data["sessions"]
    meta = eval_data["meta"]
    if meta:
        st.caption(
            f"Provider/model: {meta.get('provider')} / {meta.get('model')}  |  "
            f"Run date: {meta.get('run_date')}"
        )

    st.subheader("Cell coverage (condition x enforcement level)")
    coverage = cell_coverage(sessions)
    st.table(
        {
            "condition": [c for c in CONDITIONS for _ in ENFORCEMENT_LEVELS],
            "enforcement_level": [e for _ in CONDITIONS for e in ENFORCEMENT_LEVELS],
            "sessions": [coverage[(c, e)] for c in CONDITIONS for e in ENFORCEMENT_LEVELS],
        }
    )

    st.subheader("Goal coverage")
    goals = goals_covered(sessions)
    if goals:
        st.table(
            {
                "goal_id": list(goals.keys()),
                "cells complete (of 6)": list(goals.values()),
            }
        )
    else:
        st.caption("No sessions in eval/results.json yet.")

    col1, col2 = st.columns(2)
    with col1:
        if MARGIN_PLOT_PATH.exists():
            st.image(str(MARGIN_PLOT_PATH), caption="Margin by condition x enforcement level")
        else:
            st.caption("plot_margin_uplift.png not found.")
    with col2:
        if SCATTER_PLOT_PATH.exists():
            st.image(str(SCATTER_PLOT_PATH), caption="False-block vs violation-prevention")
        else:
            st.caption("plot_false_block_vs_prevention.png not found.")

    st.subheader("Prompt-injection robustness suite")
    injection_data = get_injection_results()
    inj_results = injection_data["results"]
    if inj_results:
        st.table(
            {
                "SKU": [r["sku"] for r in inj_results],
                "style": [r["style"] for r in inj_results],
                "outcome": [r["outcome"] for r in inj_results],
                "gate attack success": [r["gate_level_attack_success"] for r in inj_results],
                "agent engaged": [r["agent_level_engaged_with_injection"] for r in inj_results],
            }
        )
        gate_fail = sum(1 for r in inj_results if r["gate_level_attack_success"])
        st.caption(f"Policy-gate attack success: {gate_fail}/{len(inj_results)}")
    else:
        st.caption("No injection suite results found — run `python -m eval.injection_suite`.")


def _bridge_secrets_into_environ() -> None:
    # Streamlit Cloud secrets live in st.secrets, not os.environ, but Config reads
    # os.environ. Streamlit copies secrets into os.environ the first time st.secrets is
    # parsed, so touching it once here is enough. Raises if there's no secrets.toml
    # (plain local dev via .env), which is fine to ignore.
    try:
        len(st.secrets)
    except Exception:
        pass


def main() -> None:
    st.set_page_config(page_title="Agent Commerce", layout="wide")
    st.title("Agent Commerce — constraint-based agentic commerce")
    st.caption("Razorpay AI Buildathon, Track 01")

    _bridge_secrets_into_environ()
    config = load_config()

    tab1, tab2, tab3 = st.tabs(["Live run", "Session replay", "Eval"])
    with tab1:
        render_live_run_tab(config)
    with tab2:
        render_session_replay_tab()
    with tab3:
        render_eval_tab()


main()
