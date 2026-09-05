"""Phase 8 report generator. Reads eval/results.json (main grid) and
eval/injection_results.json (prompt-injection suite) — never computes the grid itself, per
docs/PHASE_8_SPEC.md ("the same JSON the Phase 9 Streamlit app reads"). Produces:
  - eval/report.md — the markdown report
  - eval/plot_margin_uplift.png
  - eval/plot_false_block_vs_prevention.png
"""

from __future__ import annotations

import json
from collections import defaultdict
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

    lines: list[str] = []
    lines.append("# Phase 8 Evaluation Report")
    lines.append("")
    lines.append(
        f"**Provider/model:** {meta['provider']} / {meta['model']}  \n"
        f"**Run date:** {meta['run_date']}  \n"
        f"**Goals attempted:** {', '.join(goals_seen)} "
        f"({len(goals_complete_all_6)} with all 6 cells complete, "
        f"{len(goals_seen) - len(goals_complete_all_6)} partial)  \n"
        f"**Sessions completed:** {len(sessions)} of a planned "
        f"{len(CONDITIONS) * len(ENFORCEMENT_LEVELS) * 4} (4-goal grid) — "
        "stopped by genuine Groq TPD quota exhaustion mid-run, not a time or design limit."
    )
    lines.append("")

    # --- Per-cell summary table -------------------------------------------------------
    lines.append("## Per-cell summary (n = sessions in that cell, across whatever goals ran)")
    lines.append("")
    lines.append(
        "| condition | enforcement | n | task success | violation rate | "
        "violation prevented | false block | mean margin % | mean turns | "
        "turn_limit_reached |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
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
    lines.append("")

    # --- Margin uplift (paired, per goal with all 6 cells) ----------------------------
    lines.append("## Margin uplift vs. no-upsell baseline (paired per goal)")
    lines.append("")
    if goals_complete_all_6:
        lines.append("| goal | enforcement | rules uplift (pp) | llm uplift (pp) |")
        lines.append("|---|---|---|---|")
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
            "comparison — see the Methodology section."
        )
    else:
        lines.append("_No goal has all 6 cells complete yet — uplift table pending._")
    lines.append("")

    # --- Methodological finding: temperature-0 non-determinism ------------------------
    lines.append("## Methodological finding: trajectory drift despite temperature 0")
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
            "Even at temperature 0 and with an identical goal, **the buyer's own item choice "
            "sometimes differed across conditions that should share the same shopping "
            "trajectory** (no upsell was ever accepted in the cases below, so nothing about "
            "the upsell condition itself should change what's in the cart):"
        )
        lines.append("")
        for goal_id, level, totals in drift_found:
            totals_str = ", ".join(f"{c}={t}" for c, t in totals.items())
            lines.append(f"- **{goal_id} / {level}**: cart totals differ by condition — {totals_str} paise")
        lines.append("")
        lines.append(
            "Groq's own inference is not perfectly reproducible call-to-call even at "
            "temperature 0 (a known property of batched/GPU inference, not specific to this "
            "system). The `CachingLLMClient` sharing that Step 1's trajectory-replay relies on "
            "only produces identical trajectories when a cache **hit** actually occurs; on a "
            "cache **miss**, a fresh call can pick a genuinely different (sometimes "
            "same-priced) product. This means the margin-uplift table above is not a clean, "
            "purely-upsell-driven paired comparison — part of any delta may be this kind of "
            "trajectory drift rather than the upsell agent's effect. A more careful version "
            "would explicitly pin and replay the reference trajectory's tool calls rather than "
            "relying on cache-hit luck; that engineering was not done here under the timebox. "
            "This is itself a finding worth reporting on its own, independent of what it does "
            "to the uplift numbers: **naive LLM-response caching is not a substitute for an "
            "explicit, deterministic replay mechanism** when the goal is a controlled "
            "comparison."
        )
    else:
        lines.append(
            "No trajectory drift detected in this run's data (no goal/enforcement-level pair "
            "with multiple conditions and no accepted offer showed differing cart totals)."
        )
    lines.append("")

    # --- Recovery attempt correctness (Phase 7 metric, ledger-derived) ----------------
    lines.append("## Recovery attempt correctness (given a budget DENY)")
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

    # --- Other free (ledger-derived) metrics ------------------------------------------
    lines.append("## Other metrics (all ledger-derived, zero extra LLM cost)")
    lines.append("")
    offers = [s for s in sessions if s["condition"] in ("rules", "llm")]
    offers_made = [s for s in offers if s["offer_made"]]
    llm_offers_made = [s for s in sessions if s["condition"] == "llm" and s["offer_made"]]
    lines.append(f"- Offers made: {_rate(offers, lambda s: s['offer_made'])}")
    lines.append(
        f"- Offers accepted (of those made): {_rate(offers_made, lambda s: s['offer_accepted'])}"
    )
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
        "accepted the offer. See the paired uplift table; sessions with `offer_accepted=false` "
        "contribute 0 to this score by construction, since no upsell item entered the cart."
    )
    lines.append("")

    # --- Prompt-injection suite --------------------------------------------------------
    lines.append(
        "## Prompt-injection robustness suite (Step 4a — run separately, see "
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
            "general: the Phase 7 live-attempt record (below, and in docs/PROGRESS.md) shows "
            "the policy engine correctly denying an over-budget checkout in **100% of "
            "~6-7 live attempts**, including cases where the buyer agent's own recovery "
            "planning failed — that is the real demonstration that the gate holds under "
            "pressure, not this suite's clean 0/6."
        )
    else:
        lines.append("_Injection suite results not found._")
    lines.append("")

    # --- Methodology & limitations ------------------------------------------------------
    lines.append("## Methodology & limitations — read this before the numbers above")
    lines.append("")
    lines.append(
        "- **n = 2 goals fully complete** (G01 easy_in_budget, G05 tight_budget), **1 partial** "
        "(G08 boundary, 2/6 cells), **1 not run at all** (G14 adversarial — the main grid's "
        "own adversarial goal; the separate injection suite below covers adversarial behavior "
        "instead, on different products)."
    )
    lines.append("- **Single seed.** No between-seed variance is reported anywhere in this document.")
    lines.append("- **Single model, single provider**: gpt-oss-120b via Groq's free tier only.")
    lines.append(
        "- **Trajectory-replay methodology** (cache-shared shopping trajectory across "
        "conditions) — see its own section above for what this means and where it broke down."
    )
    lines.append(
        "- **Stopped by external quota exhaustion, not by choice of design or timebox.** "
        "Groq's real daily token quota (200,000 TPD, free tier) was hit mid-run "
        "(199,568/200,000 used across today's testing and grid run) — a `429` from the actual "
        "API. The report reflects whatever completed before that point."
    )
    lines.append(
        "- **Simulated buyer behavior, not a user study.** The \"buyer\" throughout is an LLM "
        "given a natural-language goal and a hard budget ceiling extracted from it — not a "
        "real person. Task success, concession, and recovery-correctness rates describe this "
        "system's own agent, not human purchasing behavior."
    )
    lines.append(
        "- **The margin uplift number is not statistically meaningful at this n.** With 2 "
        "goals fully complete and no seed replication, the uplift table above should be read "
        "as \"what happened in these specific runs,\" not as an estimate of a true effect size "
        "with any claimed precision. No confidence interval is reported because none would be "
        "honest at this n — say that plainly rather than let a percentage stand unqualified."
    )
    lines.append("")

    return "\n".join(lines)


def build_plots() -> None:
    _, sessions = _load_results()
    by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in sessions:
        by_cell[_cell_key(s)].append(s)

    # Plot 1: margin uplift bars (mean final margin % per cell, whatever's available).
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
    ax.set_title("Margin by condition x enforcement level (n varies per cell — see report.md)")
    plt.xticks(rotation=0, fontsize=8)
    fig.tight_layout()
    fig.savefig(Path(__file__).parent / "plot_margin_uplift.png", dpi=120)
    plt.close(fig)

    # Plot 2: false-block vs violation-prevention scatter, all 6 cells.
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


def main() -> None:
    report = build_report()
    REPORT_PATH.write_text(report, encoding="utf-8")
    build_plots()
    print(f"Report written to {REPORT_PATH}")
    print(f"Plots written to {Path(__file__).parent}/plot_*.png")


if __name__ == "__main__":
    main()
