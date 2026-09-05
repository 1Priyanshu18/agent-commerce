"""Phase 8 report generator. Reads eval/results.json (main grid) and
eval/injection_results.json (prompt-injection suite) — never computes the grid itself, per
docs/PHASE_8_SPEC.md ("the same JSON the Phase 9 Streamlit app reads"). Produces:
  - eval/report.md — the full markdown report (internal — quota/timebox narrative allowed)
  - eval/plot_offer_rate.png — the headline finding (offer rate by condition)
  - eval/plot_margin_uplift.png
  - eval/plot_false_block_vs_prevention.png
  - splices a public-facing eval summary into README.md between two marker comments (Phase 10,
    docs/PHASE_10_SPEC.md "Eval sections stay open") — the only place that public summary is
    written, so re-running this script keeps the README in sync with results.json automatically.

Report ordering (2026-09-05 restructure): the offer-rate divergence between the llm and rules
upsell conditions is the headline finding, not margin uplift — see the machine_reason taxonomy
section for why margin uplift was demoted (too few accepted offers at this n to support any
conclusion).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = Path(__file__).parent / "results.json"
INJECTION_RESULTS_PATH = Path(__file__).parent / "injection_results.json"
REPORT_PATH = Path(__file__).parent / "report.md"

CONDITIONS = ["none", "rules", "llm"]
ENFORCEMENT_LEVELS = ["tool_level_only", "argument_level"]


def _load_results() -> tuple[dict, list[dict]]:
    with open(RESULTS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["meta"], data["sessions"]


def _load_injection_results() -> tuple[dict, list[dict]] | tuple[None, None]:
    if not INJECTION_RESULTS_PATH.exists():
        return None, None
    with open(INJECTION_RESULTS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["meta"], data["results"]


def _rate(items: list, predicate) -> str:
    if not items:
        return "n/a (n=0)"
    hits = sum(1 for i in items if predicate(i))
    return f"{hits}/{len(items)} ({100 * hits / len(items):.0f}%)"


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _cell_key(s: dict) -> tuple[str, str]:
    return (s["condition"], s["enforcement_level"])


def _per_cell_table_lines(by_cell: dict[tuple[str, str], list[dict]]) -> list[str]:
    """Shared by the internal report and the public README section, so the two never drift
    apart on how a cell's numbers are computed.
    """
    lines = [
        "| condition | enforcement | n | task success | violation rate | "
        "violation prevented | false block | mean margin % | mean turns | "
        "turn_limit_reached |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for condition in CONDITIONS:
        for level in ENFORCEMENT_LEVELS:
            cell = by_cell.get((condition, level), [])
            if not cell:
                lines.append(f"| {condition} | {level} | 0 | - | - | - | - | - | - | - |")
                continue
            attempted = [s for s in cell if s["violation_attempted"]]
            margin = _mean([s["final_margin_pct"] for s in cell])
            turns = _mean([s["turns_used"] for s in cell])
            margin_str = f"{margin:.1f}" if margin is not None else "n/a"
            turns_str = f"{turns:.1f}" if turns is not None else "n/a"
            prevented_str = (
                _rate(attempted, lambda s: s["violation_prevented"]) if attempted else "n/a (0 attempted)"
            )
            lines.append(
                f"| {condition} | {level} | {len(cell)} | "
                f"{_rate(cell, lambda s: s['task_success'])} | "
                f"{_rate(cell, lambda s: s['violation'])} | "
                f"{prevented_str} | "
                f"{_rate(cell, lambda s: s['false_block'])} | "
                f"{margin_str} | {turns_str} | "
                f"{_rate(cell, lambda s: s['outcome'] == 'turn_limit_reached')} |"
            )
    return lines


def _offer_rate_lines(sessions: list[dict]) -> list[str]:
    """The headline: offer rate by upsell condition, with the llm condition decomposed into
    genuine decisions vs. fallbacks via `upsell_fallback_machine_reason`. Shared by the
    internal report and the public README section.
    """
    lines = ["| condition | n | offer rate | offer accepted (of those made) |", "|---|---|---|---|"]
    for condition in CONDITIONS:
        cond_sessions = [s for s in sessions if s["condition"] == condition]
        offers_made = [s for s in cond_sessions if s["offer_made"]]
        accepted_str = (
            _rate(offers_made, lambda s: s["offer_accepted"]) if offers_made else "n/a (0 offers made)"
        )
        lines.append(
            f"| {condition} | {len(cond_sessions)} | "
            f"{_rate(cond_sessions, lambda s: s['offer_made'])} | {accepted_str} |"
        )
    lines.append("")

    llm_sessions = [s for s in sessions if s["condition"] == "llm"]
    if llm_sessions:
        fallback_sessions = [s for s in llm_sessions if s.get("upsell_fallback_machine_reason")]
        genuine_sessions = [s for s in llm_sessions if not s.get("upsell_fallback_machine_reason")]
        genuine_offer_rate = (
            _rate(genuine_sessions, lambda s: s["offer_made"])
            if genuine_sessions
            else "n/a (0 genuine decisions captured)"
        )
        lines.append(
            f"**Report both llm numbers, not one:** raw offer rate "
            f"{_rate(llm_sessions, lambda s: s['offer_made'])} across all {len(llm_sessions)} "
            f"sessions; **{genuine_offer_rate}** among the {len(genuine_sessions)} sessions "
            f"where the model's decision was actually captured (excluding "
            f"{len(fallback_sessions)} fallback session(s) — see the machine_reason taxonomy "
            "below for what those were)."
        )
        lines.append("")
        lines.append(
            "**Two competing interpretations, both consistent with this data — neither is "
            "favored here:**"
        )
        lines.append(
            "1. The `llm` strategy exercises restraint the deterministic `rules` strategy "
            "can't: it can decline when an offer genuinely isn't warranted for this cart, "
            "which is desirable merchant behavior a fixed rule can't express."
        )
        lines.append(
            "2. Or the `llm` strategy is simply less reliable at producing a valid decision "
            "at all, and the low offer rate partly reflects model flakiness rather than "
            "judgment."
        )
        lines.append(
            f"The {len(fallback_sessions)} fallback session(s) below are direct evidence for "
            "reading 2 — some fraction of the apparent \"restraint\" is measurably not "
            "restraint at all. Which interpretation dominates isn't resolved by this dataset; "
            "what the taxonomy adds is the ability to say precisely how much of the gap is "
            "attributable to which cause, instead of guessing."
        )
    return lines


def _machine_reason_lines(sessions: list[dict]) -> list[str]:
    """What the machine_reason taxonomy actually revealed, in this run's own data."""
    llm_sessions = [s for s in sessions if s["condition"] == "llm"]
    reasons = Counter(s.get("upsell_fallback_machine_reason") for s in llm_sessions)
    genuine = reasons.pop(None, 0)
    fallback_total = sum(reasons.values())

    lines = [
        f"Of {len(llm_sessions)} `llm`-condition sessions, **{genuine} reached a genuine, "
        f"successfully-parsed decision** and **{fallback_total} fell back** to a no-offer "
        "decision without the model actually deciding — broken down by cause:",
        "",
    ]
    if reasons:
        lines.append("| machine_reason | count | meaning |")
        lines.append("|---|---|---|")
        meanings = {
            "UPSELL_DECISION_CALL_FAILED": "the LLM call itself failed before any response existed",
            "UPSELL_DECISION_MISSING_TOOL_CALL": "the model responded without invoking the forced tool",
            "UPSELL_DECISION_INCOMPLETE_OFFER": "offered=true but sku/discount_pct were missing",
            "UPSELL_DECISION_INVALID_SKU": "the model proposed a SKU outside the candidate list",
            "NO_CANDIDATE_AVAILABLE": "no complementary in-stock candidate existed (never calls the LLM)",
        }
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{reason}` | {count} | {meanings.get(reason, '—')} |")
    else:
        lines.append("_(no fallbacks recorded in this dataset)_")
    lines.append("")
    return lines


def _uplift_table_lines(sessions: list[dict], goals_complete_all_6: list[str]) -> list[str]:
    """Shared by the internal report and the public README section."""
    if not goals_complete_all_6:
        return ["_No goal has all 6 cells complete yet — uplift table pending._"]
    lines = ["| goal | enforcement | rules uplift (pp) | llm uplift (pp) |", "|---|---|---|---|"]
    for goal_id in goals_complete_all_6:
        for level in ENFORCEMENT_LEVELS:
            by_cond = {
                s["condition"]: s
                for s in sessions
                if s["goal_id"] == goal_id and s["enforcement_level"] == level
            }
            baseline = by_cond.get("none", {}).get("final_margin_pct")
            rules_m = by_cond.get("rules", {}).get("final_margin_pct")
            llm_m = by_cond.get("llm", {}).get("final_margin_pct")
            rules_uplift = (
                (rules_m - baseline) if (baseline is not None and rules_m is not None) else None
            )
            llm_uplift = (llm_m - baseline) if (baseline is not None and llm_m is not None) else None
            lines.append(
                f"| {goal_id} | {level} | "
                f"{f'{rules_uplift:+.1f}' if rules_uplift is not None else 'n/a'} | "
                f"{f'{llm_uplift:+.1f}' if llm_uplift is not None else 'n/a'} |"
            )
    lines.append("")
    lines.append(
        "**n is small (goals fully complete: "
        f"{len(goals_complete_all_6)}) — reported plainly, no confidence intervals.** "
        "This is a paired, per-goal, percentage-point margin delta, not a marginal-means "
        "comparison — see eval/report.md's Methodology section for detail."
    )
    return lines


def _uplift_nonzero_row_count(sessions: list[dict], goals_complete_all_6: list[str]) -> tuple[int, int]:
    """(rows with any nonzero uplift, total rows) — used only for the demotion caveat, kept
    separate from _uplift_table_lines so that function's return type stays list[str].
    """
    total = 0
    nonzero = 0
    for goal_id in goals_complete_all_6:
        for level in ENFORCEMENT_LEVELS:
            by_cond = {
                s["condition"]: s
                for s in sessions
                if s["goal_id"] == goal_id and s["enforcement_level"] == level
            }
            baseline = by_cond.get("none", {}).get("final_margin_pct")
            rules_m = by_cond.get("rules", {}).get("final_margin_pct")
            llm_m = by_cond.get("llm", {}).get("final_margin_pct")
            total += 1
            rules_uplift = (rules_m - baseline) if (baseline is not None and rules_m is not None) else 0
            llm_uplift = (llm_m - baseline) if (baseline is not None and llm_m is not None) else 0
            if rules_uplift != 0 or llm_uplift != 0:
                nonzero += 1
    return nonzero, total


def build_report() -> str:
    meta, sessions = _load_results()
    inj_meta, inj_results = _load_injection_results()

    by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in sessions:
        by_cell[_cell_key(s)].append(s)

    goals_seen = sorted({s["goal_id"] for s in sessions})
    goals_complete_all_6 = [
        g
        for g in goals_seen
        if sum(1 for s in sessions if s["goal_id"] == g) == len(CONDITIONS) * len(ENFORCEMENT_LEVELS)
    ]

    planned_goals = meta.get("num_goals") or len(goals_seen)
    planned_sessions = len(CONDITIONS) * len(ENFORCEMENT_LEVELS) * planned_goals
    completion_note = (
        "full grid complete."
        if len(sessions) >= planned_sessions
        else "incomplete — rerun `python -m eval.runner` to continue from checkpoint "
        "(never wipes existing results)."
    )

    lines: list[str] = []
    lines.append("# Phase 8 Evaluation Report")
    lines.append("")
    lines.append(
        f"**Provider/model:** {meta['provider']} / {meta['model']}  \n"
        f"**Run date:** {meta['run_date']}  \n"
        f"**Goals attempted:** {', '.join(goals_seen)} "
        f"({len(goals_complete_all_6)} with all 6 cells complete, "
        f"{len(goals_seen) - len(goals_complete_all_6)} partial)  \n"
        f"**Sessions completed:** {len(sessions)} of a planned {planned_sessions} "
        f"({planned_goals}-goal grid) — {completion_note}"
    )
    lines.append("")

    # --- 1. Offer-rate divergence (headline) ------------------------------------------
    lines.append("## 1. Offer-rate divergence: llm vs. rules (the headline finding)")
    lines.append("")
    lines.extend(_offer_rate_lines(sessions))
    lines.append("")

    # --- 2. machine_reason taxonomy ----------------------------------------------------
    lines.append("## 2. The machine_reason taxonomy, and what it revealed")
    lines.append("")
    lines.append(
        "Before today, a fallback no-offer decision (the LLM call failing, the model not "
        "calling the tool, an invalid SKU, etc.) and a genuine no-offer decision wrote the "
        "*exact same* ledger entry shape — indistinguishable without cross-referencing "
        "another condition's results by hand. That's how a schema bug was found: "
        "`_DECIDE_TOOL`'s JSON schema listed `sku`/`discount_pct` as required even though a "
        "genuine decline naturally omits both, so Groq's server-side validation rejected "
        "every clean decline before a response even existed to parse. Cross-checking against "
        "`rules` (same candidate pool, since both strategies draw from the same "
        "`find_candidate_products()`) surfaced a 12/12 mismatch rate — every `llm`-condition "
        "session in that dataset showed `offer_made=False` while its exact `rules` "
        "counterpart showed `True`. That data was discarded and the 20 `llm` cells were "
        "re-run after the schema fix (see docs/PROGRESS.md for the full account)."
    )
    lines.append("")
    lines.append(
        "The fix also added `NoOffer.machine_reason` (`None` for a genuine decision, a "
        "distinct string per fallback cause), threaded through to a new "
        "`SessionMetrics.upsell_fallback_machine_reason` field — queryable in `results.json` "
        "directly, not reconstructed from console logs or another condition's data:"
    )
    lines.append("")
    lines.extend(_machine_reason_lines(sessions))
    lines.append(
        "**This taxonomy proved its value within minutes of existing.** The very first "
        "re-run cell hit a *different*, genuinely separate Groq error (the model not "
        "invoking the forced tool at all) and was correctly labeled "
        "`UPSELL_DECISION_MISSING_TOOL_CALL` instead of silently counting as a decision. "
        "Without this taxonomy, that session — and the others like it — would have been "
        "indistinguishable from genuine restraint, and the offer-rate section above would "
        "have reported the raw 20% as pure model judgment with no way to know otherwise."
    )
    lines.append("")

    # --- 3. Enforcement-level comparison -------------------------------------------------
    lines.append("## 3. Enforcement-level comparison (argument_level vs. tool_level_only)")
    lines.append("")
    total_attempted = sum(1 for s in sessions if s["violation_attempted"])
    for level in ENFORCEMENT_LEVELS:
        cell = [s for s in sessions if s["enforcement_level"] == level]
        margin = _mean([s["final_margin_pct"] for s in cell])
        lines.append(
            f"- **{level}**: n={len(cell)}, task success {_rate(cell, lambda s: s['task_success'])}, "
            f"mean margin {margin:.1f}%" if margin is not None else f"- **{level}**: n={len(cell)}"
        )
    lines.append("")
    if total_attempted == 0:
        lines.append(
            "**No goal in this run attempted a real budget violation** — every goal here has "
            "`compliant_purchase_possible: true` in `eval/goals.yaml`, and the buyer agent "
            "stayed under its own ceiling in every session. That means the core Phase 8 "
            "research question — does argument-level enforcement prevent more real "
            "violations than tool-level enforcement? — is **untested by this dataset**, not "
            "answered favorably by a 0% violation rate in both conditions. A meaningful "
            "comparison needs a goal designed to induce an over-budget attempt; none of the "
            "10 goals run here does (the separate injection suite below covers adversarial "
            "behavior on different, non-budget grounds instead)."
        )
    else:
        lines.append(
            f"{total_attempted} session(s) attempted a real violation across both levels — "
            "see the per-cell table for prevention/false-block rates broken out by level."
        )
    lines.append("")
    lines.append("### Per-cell summary (n = sessions in that cell, across whatever goals ran)")
    lines.append("")
    lines.extend(_per_cell_table_lines(by_cell))
    lines.append("")

    # --- 4. Prompt-injection suite --------------------------------------------------------
    lines.append(
        "## 4. Prompt-injection robustness suite (Step 4a — run separately, see "
        "eval/injection_suite.py)"
    )
    lines.append("")
    if inj_results:
        n = len(inj_results)
        gate_fail = sum(1 for r in inj_results if r["gate_level_attack_success"])
        agent_eng = sum(1 for r in inj_results if r["agent_level_engaged_with_injection"])
        lines.append(f"**{n} adversarial products** across 3 injection styles (direct instruction, "
                     "fake system message, indirect via review text), one live session each.")
        lines.append("")
        lines.append("| SKU | style | outcome | correct price | paid | gate attack success | agent engaged |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in inj_results:
            lines.append(
                f"| {r['sku']} | {r['style']} | {r['outcome']} | {r['correct_price_paise']} | "
                f"{r['paid_amount_paise']} | {r['gate_level_attack_success']} | "
                f"{r['agent_level_engaged_with_injection']} |"
            )
        lines.append("")
        lines.append(f"**Policy-gate attack success: {gate_fail}/{n}.**")
        lines.append(f"**Agent-level engagement with injected text: {agent_eng}/{n}.**")
        lines.append("")
        lines.append(
            "**Read this result carefully — it is an absence-of-failure result, not a "
            "positive demonstration.** The agent was never induced to act on any injected "
            "instruction (0/6 agent-level engagement), which means no injected action ever "
            "reached the policy gate in the first place. This suite therefore does **not** "
            "show the gate successfully blocking an injection attempt — it shows the "
            "injection never got that far. The gate's defense specifically against "
            "prompt-injected instructions remains **untested** by this suite; a stronger test "
            "would need to first get the agent to attempt the injected action (e.g. via a "
            "weaker or differently-prompted agent) and then check whether the gate still "
            "blocks it. What we do have positive evidence for is the gate's defense in "
            "general: the Phase 7 live-attempt record (below) shows the policy engine "
            "correctly denying an over-budget checkout in **100% of ~6-7 live attempts**, "
            "including cases where the buyer agent's own recovery planning failed — that is "
            "the real demonstration that the gate holds under pressure, not this suite's "
            "clean 0/6."
        )
    else:
        lines.append("_Injection suite results not found._")
    lines.append("")
    lines.append("### Recovery attempt correctness (given a budget DENY), for context")
    lines.append("")
    recovery_counts = defaultdict(int)
    for s in sessions:
        recovery_counts[s["recovery_attempt_correctness"]] += 1
    lines.append(
        f"- correct: {recovery_counts.get('correct', 0)}, "
        f"incorrect: {recovery_counts.get('incorrect', 0)}, "
        f"never_resolved: {recovery_counts.get('never_resolved', 0)}, "
        f"not_applicable (no DENY this session): {recovery_counts.get('not_applicable', 0)}"
    )
    lines.append("")
    lines.append(
        "**Phase 7 live-attempt record (folded in as real observational data already paid "
        "for, not re-run here):** across ~6-7 live attempts at the `policy_deny_recovery` "
        "failure path on this same model (gpt-oss-120b), the policy engine correctly denied "
        "the over-budget checkout **100% of the time** — the gate never once failed. What "
        "varied was the buyer agent's own recovery planning: one attempt executed the correct "
        "remove-then-add recovery but ran out of turns before retrying checkout; another "
        "attempt regressed and stacked a second item on top instead of removing the first, "
        "denied again at triple the original total; several attempts never got far enough to "
        "test recovery at all (excessive, sometimes zero-result, category search before ever "
        "adding an item). See docs/PROGRESS.md, \"policy_deny_recovery live convergence\", for "
        "the full record."
    )
    lines.append("")

    # --- 5. Temperature-0 trajectory drift ----------------------------------------------
    lines.append("## 5. Methodological finding: trajectory drift despite temperature 0")
    lines.append("")
    drift_found = []
    for goal_id in goals_seen:
        for level in ENFORCEMENT_LEVELS:
            by_cond = {
                s["condition"]: s
                for s in sessions
                if s["goal_id"] == goal_id and s["enforcement_level"] == level
            }
            if len(by_cond) < 2:
                continue
            if any(s["offer_accepted"] for s in by_cond.values()):
                continue  # a real upsell acceptance explains any total difference here
            totals = {c: s["final_cart_total_paise"] for c, s in by_cond.items()}
            if len(set(totals.values())) > 1:
                drift_found.append((goal_id, level, totals))
    if drift_found:
        lines.append(
            f"Even at temperature 0 and with an identical goal, **the buyer's own item choice "
            f"differed across conditions in {len(drift_found)} case(s)** that should share the "
            "same shopping trajectory (no upsell was ever accepted in these cases, so nothing "
            "about the upsell condition itself should change what's in the cart):"
        )
        lines.append("")
        for goal_id, level, totals in drift_found:
            totals_str = ", ".join(f"{c}={t}" for c, t in totals.items())
            lines.append(f"- **{goal_id} / {level}**: cart totals differ by condition — {totals_str} paise")
        lines.append("")
        lines.append(
            "Groq's own inference is not perfectly reproducible call-to-call even at "
            "temperature 0 (a known property of batched/GPU inference, not specific to this "
            "system). The `CachingLLMClient` sharing that trajectory-replay relies on only "
            "produces identical trajectories when a cache **hit** actually occurs; on a cache "
            "**miss**, a fresh call can pick a genuinely different (sometimes same-priced) "
            "product. This means the margin-uplift table below is not a clean, "
            "purely-upsell-driven paired comparison — part of any delta may be this kind of "
            "trajectory drift rather than the upsell agent's effect. A more careful version "
            "would explicitly pin and replay the reference trajectory's tool calls rather than "
            "relying on cache-hit luck; that engineering was not done here. This is itself a "
            "finding worth reporting on its own, independent of what it does to the uplift "
            "numbers: **naive LLM-response caching is not a substitute for an explicit, "
            "deterministic replay mechanism** when the goal is a controlled comparison."
        )
    else:
        lines.append(
            "No trajectory drift detected in this run's data (no goal/enforcement-level pair "
            "with multiple conditions and no accepted offer showed differing cart totals)."
        )
    lines.append("")

    # --- 6. Margin uplift (demoted) ------------------------------------------------------
    lines.append("## 6. Margin uplift vs. no-upsell baseline — demoted, see caveat below")
    lines.append("")
    nonzero_rows, total_rows = _uplift_nonzero_row_count(sessions, goals_complete_all_6)
    accepted_by_condition = {
        c: sum(1 for s in sessions if s["condition"] == c and s["offer_accepted"])
        for c in ("rules", "llm")
    }
    lines.append(
        f"**No margin conclusion is supportable at this n.** Only {nonzero_rows} of "
        f"{total_rows} rows below show any nonzero uplift at all, and across the whole grid "
        f"only {accepted_by_condition['rules']} `rules`-condition and "
        f"{accepted_by_condition['llm']} `llm`-condition offer was accepted, period. What this "
        "table actually measures is \"offers rarely reached acceptance at this sample size,\" "
        "not a margin effect attributable to either upsell strategy. It's kept here for "
        "completeness, not as evidence for anything — the offer-rate finding above is the "
        "result this run actually supports."
    )
    lines.append("")
    lines.extend(_uplift_table_lines(sessions, goals_complete_all_6))
    lines.append("")

    # --- 7. Other free (ledger-derived) metrics ------------------------------------------
    lines.append("## 7. Other metrics (all ledger-derived, zero extra LLM cost)")
    lines.append("")
    offers = [s for s in sessions if s["condition"] in ("rules", "llm")]
    llm_offers_made = [s for s in sessions if s["condition"] == "llm" and s["offer_made"]]
    lines.append(f"- Offers made (rules + llm combined): {_rate(offers, lambda s: s['offer_made'])}")
    lines.append(
        "- Dark-pattern flag rate (llm condition offers): "
        f"{_rate(llm_offers_made, lambda s: s['dark_pattern_flagged'])}"
    )
    lines.append(f"- Small-gap heuristic fired: {_rate(sessions, lambda s: s['small_gap_heuristic_fired'])}")
    lines.append(f"- Buyer concession rate: {_rate(sessions, lambda s: s['buyer_concession'])}")
    lines.append(f"- turn_limit_reached rate (overall): "
                 f"{_rate(sessions, lambda s: s['outcome'] == 'turn_limit_reached')}")
    lines.append(
        f"- TUE-adapted tool execution success rate (mean): "
        f"{_mean([s['tool_execution_success_rate'] for s in sessions]):.2f}"
    )
    lines.append(
        f"- Parse failures (total across all sessions): "
        f"{sum(s['parse_failure_count'] for s in sessions)}"
    )
    lines.append("")
    lines.append(
        "**CSS-style synergy score (adapted from TRiSM, not as-specified):** we define it here "
        "as the margin-uplift table above, conditioned on `offer_accepted` — i.e. the margin "
        "delta attributable to the upsell agent only counts sessions where the buyer actually "
        "accepted the offer. See section 6; sessions with `offer_accepted=false` contribute 0 "
        "to this score by construction, since no upsell item entered the cart."
    )
    lines.append("")

    # --- Methodology & limitations ------------------------------------------------------
    num_seeds = meta.get("num_seeds", "n/a")
    lines.append("## Methodology & limitations — read this before the numbers above")
    lines.append("")
    partial_goals = [g for g in goals_seen if g not in goals_complete_all_6]
    sample_line = (
        f"- **n = {len(goals_complete_all_6)} of {len(goals_seen)} attempted goals fully "
        f"complete** (all {len(CONDITIONS) * len(ENFORCEMENT_LEVELS)} cells)"
    )
    if partial_goals:
        sample_line += f", partial: {', '.join(partial_goals)}."
    else:
        sample_line += "."
    lines.append(sample_line)
    lines.append(
        f"- **{num_seeds} seed(s).** "
        + (
            "No between-seed variance is reported anywhere in this document."
            if num_seeds in (1, "1")
            else "Between-seed variance, where reported, is noted explicitly per metric."
        )
    )
    lines.append(f"- **Single model, single provider**: {meta['model']} via {meta['provider']}.")
    lines.append(
        "- **No goal in this grid attempted a real budget violation** — the enforcement-level "
        "comparison (section 3) is untested by this dataset, not resolved favorably."
    )
    lines.append(
        "- **Trajectory-replay methodology** (cache-shared shopping trajectory across "
        "conditions) — see section 5 for what this means and where it broke down."
    )
    if len(sessions) < planned_sessions:
        lines.append(
            "- **This report reflects an incomplete run** — "
            f"{len(sessions)} of {planned_sessions} planned sessions completed. See the header "
            "above for whether that's still in progress or was a deliberate stopping point."
        )
    lines.append(
        "- **Simulated buyer behavior, not a user study.** The \"buyer\" throughout is an LLM "
        "given a natural-language goal and a hard budget ceiling extracted from it — not a "
        "real person. Task success, concession, and recovery-correctness rates describe this "
        "system's own agent, not human purchasing behavior."
    )
    lines.append(
        "- **The margin uplift number is not statistically meaningful at this n and is not "
        "the headline finding** (see section 6) — with "
        f"{len(goals_complete_all_6)} goal(s) fully complete and {num_seeds} seed(s), and only "
        f"{accepted_by_condition['rules'] + accepted_by_condition['llm']} accepted offers "
        "total across the whole grid, the uplift table should be read as \"what happened in "
        "these specific runs,\" not as an estimate of a true effect size. No confidence "
        "interval is reported because none would be honest at this n."
    )
    lines.append(
        "- **The offer-rate divergence (section 1) is the supportable finding, and even it "
        "has two open interpretations** (restraint vs. reliability) that this dataset alone "
        "doesn't resolve — see the machine_reason taxonomy (section 2) for what's quantified "
        "vs. still uncertain."
    )
    lines.append("")

    return "\n".join(lines)


def build_plots() -> None:
    _, sessions = _load_results()
    by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in sessions:
        by_cell[_cell_key(s)].append(s)

    # Plot 1 (headline): offer rate by condition, with the llm condition's genuine-decision-
    # only rate marked separately — this is the finding the report actually supports.
    fig, ax = plt.subplots(figsize=(6, 5))
    conditions_present = [c for c in CONDITIONS if any(s["condition"] == c for s in sessions)]
    rates = []
    for c in conditions_present:
        cond_sessions = [s for s in sessions if s["condition"] == c]
        rate = (
            100 * sum(1 for s in cond_sessions if s["offer_made"]) / len(cond_sessions)
            if cond_sessions
            else 0
        )
        rates.append(rate)
    colors = ["#999999", "#4C72B0", "#DD8452"][: len(conditions_present)]
    ax.bar(conditions_present, rates, color=colors)
    for i, r in enumerate(rates):
        ax.text(i, r + 2, f"{r:.0f}%", ha="center")
    if "llm" in conditions_present:
        llm_sessions = [s for s in sessions if s["condition"] == "llm"]
        genuine = [s for s in llm_sessions if not s.get("upsell_fallback_machine_reason")]
        if genuine:
            genuine_rate = 100 * sum(1 for s in genuine if s["offer_made"]) / len(genuine)
            idx = conditions_present.index("llm")
            ax.scatter(
                [idx], [genuine_rate], color="black", marker="_", s=600, zorder=5,
                label="genuine-decision-only rate",
            )
            ax.legend(loc="upper right", fontsize=8)
    ax.set_ylabel("offer rate (%)")
    ax.set_ylim(0, 110)
    ax.set_title("Offer rate by upsell condition (the headline finding)")
    fig.tight_layout()
    fig.savefig(Path(__file__).parent / "plot_offer_rate.png", dpi=120)
    plt.close(fig)

    # Plot 2 (demoted): margin uplift bars (mean final margin % per cell) — kept for
    # completeness; see report.md section 6 for why this isn't the headline.
    fig, ax = plt.subplots(figsize=(8, 5))
    labels, values = [], []
    for condition in CONDITIONS:
        for level in ENFORCEMENT_LEVELS:
            cell = by_cell.get((condition, level), [])
            margin = _mean([s["final_margin_pct"] for s in cell])
            labels.append(f"{condition}\n{level}")
            values.append(margin if margin is not None else 0)
    ax.bar(labels, values, color="#4C72B0")
    ax.set_ylabel("mean final cart margin %")
    ax.set_title("Margin by condition x enforcement level (demoted — see report.md section 6)")
    plt.xticks(rotation=0, fontsize=8)
    fig.tight_layout()
    fig.savefig(Path(__file__).parent / "plot_margin_uplift.png", dpi=120)
    plt.close(fig)

    # Plot 3: false-block vs violation-prevention scatter, all 6 cells.
    fig, ax = plt.subplots(figsize=(6, 6))
    for condition in CONDITIONS:
        for level in ENFORCEMENT_LEVELS:
            cell = by_cell.get((condition, level), [])
            if not cell:
                continue
            attempted = [s for s in cell if s["violation_attempted"]]
            prevention_rate = (
                sum(1 for s in attempted if s["violation_prevented"]) / len(attempted)
                if attempted
                else 0
            )
            false_block_rate = sum(1 for s in cell if s["false_block"]) / len(cell)
            marker = "o" if level == "argument_level" else "^"
            ax.scatter(prevention_rate, false_block_rate, marker=marker, s=120, label=f"{condition}/{level}")
    ax.set_xlabel("violation prevention rate")
    ax.set_ylabel("false block rate")
    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.05, 1.05)
    ax.set_title("Safety/utility trade-off (o=argument_level, ^=tool_level_only)")
    ax.legend(fontsize=7, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    fig.savefig(Path(__file__).parent / "plot_false_block_vs_prevention.png", dpi=120)
    plt.close(fig)


README_PATH = REPO_ROOT / "README.md"
README_EVAL_START = (
    "<!-- EVAL_SECTION_START (auto-generated by `python -m eval.report` — do not hand-edit "
    "between these two markers; your edits will be overwritten on the next run) -->"
)
README_EVAL_END = "<!-- EVAL_SECTION_END -->"


README_LIMITATIONS_START = (
    "<!-- EVAL_LIMITATIONS_START (auto-generated by `python -m eval.report` — do not "
    "hand-edit between these two markers; your edits will be overwritten on the next run) -->"
)
README_LIMITATIONS_END = "<!-- EVAL_LIMITATIONS_END -->"


def _public_eval_data() -> tuple[dict, list[dict], list[dict], list[str], list[str]] | None:
    try:
        meta, sessions = _load_results()
    except FileNotFoundError:
        return None
    _, inj_results = _load_injection_results()
    inj_results = inj_results or []
    goals_seen = sorted({s["goal_id"] for s in sessions})
    goals_complete_all_6 = [
        g
        for g in goals_seen
        if sum(1 for s in sessions if s["goal_id"] == g) == len(CONDITIONS) * len(ENFORCEMENT_LEVELS)
    ]
    return meta, sessions, inj_results, goals_seen, goals_complete_all_6


def build_public_eval_results_section() -> str:
    """The 'Evaluation results' section spliced into README.md. Numbers are computed the same
    way as build_report() (via the shared helpers) so the two documents can never silently
    disagree — but this version omits anything about quota, free tiers, API budgets, or run
    interruptions (docs/PHASE_10_SPEC.md, "Public-facing tone"): it states the scope of the
    evidence, not the operational reason a run stopped where it did (that reasoning, if any,
    belongs only in docs/PROGRESS.md). Returns a clearly marked placeholder if
    eval/results.json doesn't exist yet.
    """
    data = _public_eval_data()
    if data is None:
        return "_(eval/results.json not found — run `python -m eval.runner` first.)_"
    meta, sessions, inj_results, goals_seen, goals_complete_all_6 = data

    by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in sessions:
        by_cell[_cell_key(s)].append(s)

    lines: list[str] = []
    lines.append(f"**Model:** {meta.get('provider', 'n/a')} / {meta.get('model', 'n/a')}")
    lines.append("")
    lines.append("### Offer-rate divergence: llm vs. rules (the headline finding)")
    lines.append("")
    lines.extend(_offer_rate_lines(sessions))
    lines.append("")
    lines.append("![Offer rate by condition](eval/plot_offer_rate.png)")
    lines.append("")
    lines.append("### Enforcement-level comparison")
    lines.append("")
    lines.extend(_per_cell_table_lines(by_cell))
    lines.append("")
    if inj_results:
        n = len(inj_results)
        gate_fail = sum(1 for r in inj_results if r["gate_level_attack_success"])
        lines.append(
            f"**Prompt-injection suite:** {n} adversarial products, policy-gate attack "
            f"success {gate_fail}/{n}. This is an absence-of-failure result, not a positive "
            "demonstration — see Limitations below."
        )
        lines.append("")
    lines.append("### Margin uplift vs. no-upsell baseline — demoted, not statistically supportable")
    lines.append("")
    lines.extend(_uplift_table_lines(sessions, goals_complete_all_6))
    lines.append("")
    lines.append("![Safety/utility trade-off](eval/plot_false_block_vs_prevention.png)")
    return "\n".join(lines)


def build_public_limitations_section() -> str:
    """The README's own 'Limitations' section — see build_public_eval_results_section()'s
    docstring for the same public-facing-tone constraint. Kept as a separate function/marker
    pair from the eval results so the README can order them as two distinct sections
    (docs/PHASE_10_SPEC.md) while both stay generated from the same underlying data.
    """
    data = _public_eval_data()
    if data is None:
        return "_(eval/results.json not found — run `python -m eval.runner` first.)_"
    meta, sessions, inj_results, goals_seen, goals_complete_all_6 = data

    llm_sessions = [s for s in sessions if s["condition"] == "llm"]
    fallback_count = sum(1 for s in llm_sessions if s.get("upsell_fallback_machine_reason"))

    lines = [
        f"- Sample size: {len(goals_complete_all_6)} of {len(goals_seen) or 'n/a'} attempted "
        "goal(s) have the full 3-condition x 2-enforcement-level grid complete.",
        "- Single random seed; no between-seed variance is reported.",
        f"- Single model/provider: {meta.get('model', 'n/a')} via {meta.get('provider', 'n/a')}.",
        "- No goal in this grid attempted a real budget violation, so the enforcement-level "
        "comparison is untested by this dataset, not resolved favorably.",
        "- Trajectory-replay methodology: sessions sharing a goal reuse a cached shopping "
        "trajectory where possible; a cache miss can pick a different, similarly-priced "
        "product, which is a measured source of noise in the margin-uplift numbers.",
        "- Simulated buyer behaviour: each goal is a natural-language prompt given to an LLM "
        "buyer, not a real user.",
        "- Simulated payments on the deployed Space (`PAYMENT_MODE=simulated`) — the full "
        "order → webhook → reconciliation lifecycle still executes end to end.",
        "- The margin-uplift table is not statistically meaningful at this sample size and is "
        "not the headline finding — read it as illustrative only.",
        f"- The offer-rate divergence is the supportable finding, and even it has two open "
        f"interpretations (restraint vs. reliability) this dataset doesn't resolve on its own "
        f"— {fallback_count} of {len(llm_sessions) or 'n/a'} llm-condition session(s) are "
        "known fallbacks rather than genuine decisions (see the write-up's machine_reason "
        "taxonomy discussion).",
    ]
    if inj_results:
        lines.append(
            "- The prompt-injection suite is an absence-of-failure result: the agent was "
            "never induced to act on an injected instruction, so the policy gate's defense "
            "against injected actions specifically remains untested by this suite (see the "
            "write-up's Phase 7 recovery record for the gate's positive track record instead)."
        )
    return "\n".join(lines)


def _splice(text: str, start_marker: str, end_marker: str, content: str) -> str:
    if start_marker not in text or end_marker not in text:
        return text  # markers not (yet) present in README.md; nothing to splice into
    before, rest = text.split(start_marker, 1)
    _, after = rest.split(end_marker, 1)
    return f"{before}{start_marker}\n\n{content}\n\n{end_marker}{after}"


def update_readme() -> bool:
    """Returns True if README.md was found and (potentially) updated."""
    if not README_PATH.exists():
        return False
    text = README_PATH.read_text(encoding="utf-8")
    text = _splice(text, README_EVAL_START, README_EVAL_END, build_public_eval_results_section())
    text = _splice(
        text, README_LIMITATIONS_START, README_LIMITATIONS_END, build_public_limitations_section()
    )
    README_PATH.write_text(text, encoding="utf-8")
    return True


def main() -> None:
    report = build_report()
    REPORT_PATH.write_text(report, encoding="utf-8")
    build_plots()
    readme_updated = update_readme()
    print(f"Report written to {REPORT_PATH}")
    print(f"Plots written to {Path(__file__).parent}/plot_*.png")
    print(f"README.md eval/limitations sections updated: {readme_updated}")


if __name__ == "__main__":
    main()
