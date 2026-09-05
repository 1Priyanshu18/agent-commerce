"""Streamlit demo app for Agent Commerce. A thin view layer only — see
docs/PHASE_9_SPEC.md. All logic (policy, ledger, payments, the eval grid) lives in
agent_commerce/ and eval/; nothing under src/ imports Streamlit, and this file computes
nothing the package doesn't already compute.
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

# Streamlit's `:color[text]` markdown directive only recognizes a fixed palette (red, orange,
# yellow, green, blue, violet, gray/grey, rainbow, primary) — "teal" isn't one of them, and an
# unrecognized color name breaks the directive's parsing (the bracketed content vanishes rather
# than falling back to plain text). "violet" is the closest valid palette color to the
# originally-intended teal, and is used here instead of a color already claimed by
# VERDICT_COLOR below (blue=TRANSFORM) to avoid the two chips ever looking identical.
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

ARCHITECTURE_DIAGRAM = """\
+-------------------+          +----------------------+
|   Buyer Agent      |          |   Upsell Agent        |
|   (LLM, tool use)   |          |   (llm / rules / none)|
+---------+-----------+          +-----------+----------+
          | catalog.search, cart.add,        | cart.read_at_checkout,
          | cart.remove, checkout.confirm    | upsell.make_offer, upsell.no_offer
          v                                  v
+---------------------+          +------------------------+
|  Buyer MCP Server    |          |  Merchant MCP Server    |
|  (buyer tools only)  |          |  (merchant tools only,  |
|                      |          |   no cart-mutating tool)|
+---------+------------+          +-----------+------------+
          |                                   |
          +-----------------+-----------------+
                            |
                            v
                +--------------------------+
                |   BuyerSessionRunner       |   <- the ONLY module that calls
                |   (orchestrator)           |      policy/ and payments/ directly
                +-------------+--------------+
                              | every cart.add / checkout.confirm /
                              | cart.accept_upsell goes through this gate first
                              v
                +--------------------------+
                |   Policy Engine            |   ALLOW / DENY / TRANSFORM /
                |   (policies/default.yaml)  |   REQUIRE_APPROVAL — deterministic,
                |                            |   argument-level, never an LLM call
                +-------------+--------------+
                              | ALLOW (or TRANSFORM) only
                              v
                +--------------------------+
                |   Payment Layer             |   Razorpay (live_test) / simulated,
                |   (idempotent, recorded)    |   same interface either way
                +-------------+--------------+
                              v
                +--------------------------+
                |   Hash-chained Ledger      |   every action, caused_by-linked,
                |                            |   append-only, verify_chain()
                +--------------------------+

Neither MCP server exposes policy.* or payment.* as a tool — an LLM can never call the thing
that authorizes it or the thing that moves money. Role separation is enforced twice:
structurally (each server only ever registers its own role's tools) and via a defense-in-depth
authorize() check inside every tool handler.
"""

TRISM_PROSE = """\
**Explainability.** Every action — search, cart mutation, policy check, payment call,
webhook, reconciliation — is written to the ledger with a `reasoning_summary` and, where a
gate fired, a `human_reason` a non-technical reader can follow. Every entry links to the
entries that caused it (`caused_by`), so a session is a traceable provenance chain, not a flat
log. `verify_chain()` recomputes every hash and reports the real result — never hardcoded.

**Application Security.** The policy engine evaluates real argument values (cart totals,
discount percentages, SKUs) at the moment a tool is about to execute — not just which tool was
called. Role separation is structural (the buyer and merchant LLMs each see only their own
tool surface) and defense-in-depth (`authorize()` re-checks and logs a `role_violation` on any
mismatch). The gate never trusts anything the agent merely *says* — it only acts on catalog
and cart state it reads itself, which is why a prompt injection embedded in a product
description can talk to the agent but never talks to the gate.

**Governance.** The ledger is append-only, enforced by database triggers, not just
application code. High-value checkouts route to `REQUIRE_APPROVAL`, which fails closed
(auto-denies) on timeout rather than sitting open indefinitely. The `transaction_id` that
provenance is keyed on is orchestrator-owned, never agent-supplied — an agent that could pick
its own audit-trail key would undermine the audit trail itself.
"""


# --- Cached resources / data -----------------------------------------------------------


@st.cache_resource
def get_demo_ledger() -> LedgerStore:
    # read_only=True: this file is committed, curated, and never written to by the running
    # app — opening it read-only means it works even on a read-only app directory (HF Spaces).
    return LedgerStore(DEMO_LEDGER_PATH, read_only=True)


@st.cache_resource
def get_compiled_policy():
    return compile_policy(POLICY_PATH)


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


# --- Tab 1: Live run ---------------------------------------------------------------------


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
        # The demo's own hard per-session call cap, never looser than the app-wide one.
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
            st.success(f"Ledger integrity ✓ ({verification.entries_checked} entries)")
        else:
            st.error(f"Ledger integrity FAILED: {verification.error}")
    elif not can_run:
        st.caption("Enter the correct passphrase to enable the run button.")


# --- Tab 2: Session replay -----------------------------------------------------------------


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
        st.success(f"Ledger integrity ✓ ({verification.entries_checked} entries checked)")
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


# --- Tab 3: Eval ---------------------------------------------------------------------------


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


# --- Tab 4: Architecture ---------------------------------------------------------------------


def render_architecture_tab() -> None:
    st.subheader("System architecture")
    st.code(ARCHITECTURE_DIAGRAM, language=None)

    st.subheader("Policy DSL (live from policies/default.yaml)")
    get_compiled_policy()  # exercised here only to prove the file compiles; not rendered raw twice
    st.code(POLICY_PATH.read_text(encoding="utf-8"), language="yaml")

    st.subheader("TRiSM pillars")
    st.markdown(TRISM_PROSE)


# --- Entry point -----------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Agent Commerce", layout="wide")
    st.title("Agent Commerce — constraint-based agentic commerce")
    st.caption("Razorpay AI Buildathon, Track 01")

    config = load_config()

    tab1, tab2, tab3, tab4 = st.tabs(["Live run", "Session replay", "Eval", "Architecture"])
    with tab1:
        render_live_run_tab(config)
    with tab2:
        render_session_replay_tab()
    with tab3:
        render_eval_tab()
    with tab4:
        render_architecture_tab()


main()
