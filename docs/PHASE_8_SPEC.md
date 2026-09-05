# Phase 8 — Evaluation harness

Captured verbatim from the user's Phase 8 brief (2026-09-05). The original phase-by-phase
brief lived only in chat, which meant it was lost when the conversation was compacted — this
file (and one like it for every future phase) exists so that never happens again. Do not
paraphrase or "improve" this document; it is the source of truth for what Phase 8 must do.

This is the one thing that must never be cut. Everything else in the project is architecture
narrative; this is the defensible number.

## Grid

3 upsell conditions (`none` / `rules` / `llm`) × 2 enforcement levels (`tool_level_only` /
`argument_level`) × 20 goals × 3 seeds = 360 sessions.

Fixed seed, temperature 0, frozen catalog snapshot. Record model name, provider, and run date
in the report. Keep the larger grid (40 goals, 5 seeds) available behind a flag but don't run
it by default.

## Goals — `eval/goals.yaml`

20 goals, each with **ground-truth constraints** so outcomes label automatically: the correct
hard budget ceiling, whether a compliant purchase is possible at all, and which catalog items
would satisfy the goal. Without ground truth there's no false-block rate.

Composition — roughly balanced across:
- easy in-budget (comfortable headroom)
- tight-budget (little headroom, upsell should mostly be denied)
- boundary (a correct cart lands ~₹40 under the ceiling — the small-gap case)
- ambiguous category (constraint extraction is genuinely hard)
- adversarial (routes through a prompt-injected product)
- out-of-stock-prone (likely to trigger reselection)

Goals are **not** exchangeable — a tight-budget goal behaves differently from a loose one.
That matters for the statistics below.

## Metrics

**Primary**
- Margin uplift %, each condition vs the no-upsell baseline
- Policy/budget violation rate — violations that actually reached `payment.create_order`,
  not attempts

**Safety/utility trade-off**
- Violation-prevention rate: blocked ÷ attempted
- **False-block rate**: compliant actions (per ground truth) that were blocked. Report this
  *beside* prevention rate, always. Never present the gate as free.
- Task success rate: goal satisfied within hard constraints

**TRiSM-derived**
- **CSS-style synergy score** — margin delta attributable to the upsell agent, conditioned on
  whether the buyer accepted. Define our formula explicitly in the report and label it
  "adapted from", never "as specified in".
- **TUE** — tool selection correctness, argument validity (from `parse_failure` entries),
  execution success rate.

**Convergence and cost**
- Turns to close, tokens, wall-clock, LLM cost per session
- Small-gap heuristic firing rate
- `turn_limit_reached` rate — this is our own instance of the AgenticPay non-convergence
  finding, report it rather than tuning it away
- **Buyer concession rate**: sessions where the buyer accepted an upsell that pushed the total
  above its soft target but under the hard ceiling

**From Phase 7**
- **Recovery attempt correctness**: given a DENY with a `human_reason`, did the buyer take the
  correct corrective action (remove-then-add), an incorrect one (stack on top), or never
  resolve? Broken down by provider.

**Behavioural**
- Dark-pattern flag rate on `llm` upsell offers

## Statistics — treat this as a real experiment

- Bootstrap confidence intervals over goals (resample goals, not sessions).
- 3 seeds per cell; report between-seed variance so readers see run-to-run noise.
- Paired per-goal comparison for the upsell-condition effect — same goal across conditions,
  not two marginal means side by side.
- Target reporting style: `margin uplift 8.3% [4.1, 12.6], n=20 goals, 3 seeds`.

## Two additional analyses

**1. Safety/utility frontier (the lead figure).**
Sweep `max_discount_pct` and `min_margin_pct` across a grid. For each setting, plot
false-block rate against violation-prevention rate. This gives a curve, not a point. The
question to answer: does argument-level enforcement dominate tool-level across the whole
sweep, or only in part of it? This is the single most persuasive image in the submission.

**2. Prompt-injection robustness as a measured rate.**
Expand from 1 adversarial product to ~20, across injection styles: direct instruction, fake
system message, unicode obfuscation, indirect via review text. Report attack success **at the
agent level** vs **at the policy-gate level** separately. The expected result — agent
sometimes fooled, gate never — is the empirical justification for the entire architecture, in
one number. If the gate ever does fail, that's a more important finding still, so report it
honestly.

## Runner requirements

- Per-session checkpointing to disk; resume skips completed cells. Quota exhaustion mid-run
  must not lose the grid.
- Rate-limit aware: pace against Groq's actual limits, exponential backoff on 429.
- Prompt-hash cache on by default. Never wipe it as a diagnostic.
- No automatic provider fallback mid-run — a silent provider switch would invalidate the
  results. Fail loudly instead.
- `--dry-run` that reports planned call count and estimated quota consumption before spending
  anything.

## Deliverables

- `eval/results.json` — every session's outcome, with `provider`, `model`, `seed`,
  `condition`, `enforcement_level`, `goal_id` as dimensions
- `eval/report.py` → generated markdown table for the README
- The same JSON the Phase 9 Streamlit app reads (it must never compute the grid)
- Plots: margin-uplift bars, the false-block vs violation-prevention scatter with all 6 cells,
  the frontier curve
- An explicit limitations paragraph: state `n`, and state plainly that this is a simulation
  with LLM-generated buyer behaviour, not a user study

## Cross-model check

Groq is primary. Run a **small** Gemini sample (a few cells, not the full grid — 20 req/day)
purely to answer: do the headline findings hold direction across models? Report as a
robustness note, not as a second full result.

---

Before building: run the `--dry-run` math and tell the user whether 360 sessions × ~15 calls
fits inside Groq's daily quota with a cold cache. If it doesn't, decide up front what changes —
fewer seeds, checkpointing across days, or something else — rather than discovering it mid-run.

---

## Amendment (2026-09-05) — tiered plan under a ~24-hour deadline

Dry-run math (measured from 103 real cached Groq calls during Phase 7, avg 2,558 tokens/call):
360 sessions × ~15 calls needs ~13.8M tokens — **69 days** against Groq's free-tier 200K
TPD cap (RPD alone would only need 5.4 days; TPD is ~13x more binding). Total dollar cost at
Groq's published per-token pricing for the whole grid: **~$2.58** — the user is checking
Developer-tier TPD limits directly (not fetchable from public docs) to see if paying removes
the constraint. Given a hard ~24-hour deadline, the user decided on a tiered plan instead of
waiting on that:

**Tiers — each is a complete, independently reportable result. Run and ship Tier A in full
(including report + plots) before starting Tier B; do not start Tier B without checking in.**
- **Tier A (must-have, run first):** 3 conditions × 2 enforcement levels × 10 goals × 1 seed =
  60 sessions. Headline margin uplift + argument-level vs tool-level comparison. Report with
  n=10, no confidence intervals, stated plainly as such.
- **Tier B:** 20 goals × 1 seed = 120 sessions. Enables bootstrap CIs over goals.
- **Tier C:** 20 goals × 3 seeds = 360 sessions (the original default grid). Adds between-seed
  variance.

**Cuts, effective now:**
- **Frontier sweep dropped.** Instead: the two enforcement levels' cells only, plotted on the
  false-block vs violation-prevention scatter (6 cells: 3 conditions × 2 enforcement levels).
  Same axes and argument as the frontier, just points instead of a swept curve.
- **Gemini cross-model check dropped entirely** — 20 req/day can't contribute anything
  meaningful in the time left.
- **Prompt-injection suite kept but shrunk:** 6 adversarial products (not 20) across 3
  injection styles (not 4). Runs as a separate small suite outside the main grid — cheap, and
  the single strongest result in the submission, so it stays.
- **All ledger-derived metrics kept** (TUE, CSS, concession rate, `turn_limit_reached` rate,
  recovery attempt correctness, dark-pattern rate) — these cost zero extra LLM calls, so
  cutting them would save no quota.

**Cost control:** cache on always, never wiped. History trimming only if it's under 30 minutes
of work, otherwise skipped — no time for uncertain optimization. Hard per-run token budget
with a loud abort (not a silent degrade).

**Timeboxes:** Tier A (report + plots included) due 4 hours from the start of this amendment.
If not done by then, stop expanding scope and report status rather than continuing —
Phases 9 and 10 need at least 8 hours of what's left and that time is protected.

**Order of work:** (1) runner with tiering + checkpointing, (2) `--dry-run` for Tier A only,
report the number before spending anything, (3) run Tier A, generate `results.json` + the
markdown table + plots, (4) show the report, then decide with the user whether Tier B is worth
the remaining time.

---

## Amendment 2 (2026-09-05) — final decision: minimal grid, no upgrade, ~20 hours left

Even Tier A alone (measured, not estimated, from the amendment-1 dry-run) needed ~11.5 days
against Groq's free-tier TPD. **Final call: stay on the free tier, no Developer-tier upgrade,
no paid fallback.** Build the smallest eval that still produces honest, defensible numbers,
and protect time for Phases 9 and 10. Tiers A/B/C above are superseded by this plan.

### Step 1 — Trajectory replay

Intent: run the buyer session once per (goal, seed), replay the recorded trajectory across
all 6 cell configs (3 conditions x 2 enforcement levels), only re-invoking the LLM where the
trajectory genuinely diverges (after a DENY, or after an upsell offer needing a fresh buyer
decision).

**Finding: this already happens for free, with zero new code.** `eval/runner.py` shares one
`CachingLLMClient` instance across the entire grid run, and the buyer's main-loop message
history never includes anything upsell-related (`decide_on_offer` is its own standalone call,
by design since before Phase 8) — so the shared portion of the trajectory (constraint
extraction, search, add) is naturally byte-identical, and therefore cache-hit, across
`none`/`rules`/`llm` at a given enforcement level, and across enforcement levels up until a
real DENY makes the two histories diverge. `build_grid()` was reordered (goal, seed outermost)
so the 6 cells sharing a trajectory run back-to-back and the effect is easy to measure.

**Measured** (goal G01, easy_in_budget, all 6 cells, gpt-oss-120b): 27 total LLM calls, 16 real
(the rest cache hits) — a ~1.7x reduction in real calls for this simple goal, where only 2 of
6 cells ever reach a genuine upsell decision and no enforcement-level divergence occurs at all
(the goal fits budget either way). Reduction will be larger for goals where an offer is made
in more cells, smaller for goals where a DENY diverges the trajectory early. ~26,252 real
tokens for this one goal across its 6 cells — well under what the Phase-7-measured average
would have predicted, because Phase 7's average was measured on failure-recovery-heavy demo
sessions, not typical shopping sessions.

**Methodology note for the report's limitations paragraph:** results are counterfactual
replays over a shared buyer trajectory, not 6 fully independent sessions. This removes
buyer-side variance across the 3 upsell conditions and 2 enforcement levels for the same
(goal, seed) — arguably a strength (a paired design, cleaner than comparing marginal means) —
but it means the buyer's own shopping choices (which item, how many searches) are identical
across cells by construction; only the upsell negotiation and any DENY-triggered recovery are
independently observed per cell. State this plainly, not buried.

**Two real bugs found and fixed while measuring this** (neither ever exercised by
`FakeLLMClient`-only tests): Groq's server-side tool-call validation can reject a response
that omits a nullable field entirely (e.g. `counter_price_paise`, `sku`/`discount_pct`)
instead of sending it as JSON `null` — raising a `BadRequestError` *before* any response object
exists to parse. This hit both `BuyerAgent.decide_on_offer` (schema: `respond_to_offer`) and
`LLMStrategy.decide` (schema: `upsell_decision`), and would have crashed the whole session each
time. Both call sites now catch the exception and fail closed exactly like an unparseable
response would (DECLINE / NoOffer respectively) — matching each class's own pre-existing
fail-closed philosophy, just extended to cover a failure that happens before parsing gets a
chance to run at all.

### Step 2 — History trimming

Implemented: `catalog.search`'s list view (`mcp/buyer_server.py`) trimmed from top-10 full
product dicts to top-5 minimal dicts (`sku`, `name`, `price_paise`, `stock` only — no
description/tags/category/variants/cost). `catalog.get_details` is unchanged and still returns
everything for the one item the agent is actually about to commit to.

Not implemented (skipped under the timebox): "tool results older than 2 turns replaced by a
one-line summary" and system-prompt trimming. Measured Phase 8 sessions (simple shopping
goals, not Phase 7's failure-recovery stress tests) are short enough — 4-10 calls observed —
that the search-result trim alone was judged sufficient; the more invasive history-rewriting
change was not worth the risk under a 2-hour budget for steps 1-3.

### Step 3 — gpt-oss-20b

Tested live, one goal (G01) across all 6 cells. **Result: does not hold the output contract
reliably** — 2 of 6 cells (`llm` condition, both enforcement levels) failed the forced
tool-choice call entirely ("Tool choice is required, but model did not call a tool"), and a
third cell needed 8 real calls where gpt-oss-120b needed 3. **Decision: stay on gpt-oss-120b**
for the main grid.

### Step 4 — the minimal grid actually run

Superseding the goal list above: **4 goals** picked for maximum coverage (one easy in-budget,
one tight-budget, one boundary, one adversarial) x 3 conditions x 2 enforcement levels x 1 seed
= **24 cells**. Reported as n=4, plainly, no confidence intervals. Expansion to 6 then 8 goals
only if quota and the 4-hour timebox both allow, stopping the moment either runs out.

**4a (run first): prompt-injection suite.** 6 adversarial products across 3 injection styles
(direct instruction, fake system message, indirect via review text — unicode obfuscation
dropped from the original 4 styles under the timebox), run as a separate small suite outside
the main grid. Reports attack success at the agent level vs the policy-gate level separately.

**Cuts, final:** frontier sweep (the 6-cell scatter carries the same argument); Gemini
cross-model check; multiple seeds (single seed, stated plainly); Tier B/C (cut unless quota
and time both allow after Step 4 completes).

**Free metrics (zero extra LLM cost, computed entirely from the ledger):** TUE, CSS-style
synergy score, buyer concession rate, `turn_limit_reached` rate, recovery attempt correctness,
dark-pattern flag rate, turns-to-close, tokens/cost per session — plus the Phase 7 live-attempt
record (7 attempts on gpt-oss-120b for `policy_deny_recovery`, folded in as real observational
data already paid for: the policy engine denied correctly every time; only the agent's
recovery planning varied).

**Timebox: 4 hours for all of Step 4**, from the start of this amendment. At 4 hours, stop
wherever things are, generate the report from whatever completed, and report status — Phases 9
and 10 need the remaining time more than a larger n does.
