# Phase 9 — Streamlit demo app

Captured verbatim from the user's Phase 9 brief (2026-09-05). Do not paraphrase or "improve"
this document; it is the source of truth for what Phase 9 must do.

I am rigid on scope here. Single `app.py`, thin view layer, nothing elaborate. Do not
propose React, Vite, Tailwind, a component library, or a richer frontend. If you find
yourself wanting more surface area, the answer is no.

## Structure

Single `app.py` at the project root. It imports from the package
(`from agent_commerce.orchestrator import ...`) and nothing in `src/` may import
Streamlit. The app is a view over existing functionality — it adds no logic.

Caching, or every widget interaction re-runs the script and recompiles the policy
engine:
- `st.cache_resource` — DB connection, compiled policy
- `st.cache_data` — `eval/results.json`

## Four tabs

**1. Live run**
- Text input for the goal, run button, failure-injection dropdown (the four Phase 7
  failures plus "none"), provider/model shown read-only.
- Ledger entries stream in as they're written.
- **Gated**: passphrase via `st.text_input(type="password")` checked against an env
  var (`DEMO_PASSPHRASE`) before the run button enables. Hard per-session call cap.
  A daily budget counter that disables live runs when tripped. This exists because the
  deployed Space is public and spends my quota.
- Today this tab is quota-limited. Build it and verify against `FakeLLMClient` — do
  **not** spend real quota testing it. Live path gets tested after tomorrow's reset.

**2. Session replay**
- Provenance chain as a vertical timeline, read from the committed demo DB.
- Actors colour-coded: teal = AI agent, grey = infra, amber = audit.
- Entries expandable to input / output / reasoning_summary / human_reason.
- Verdict chips: green ALLOW, blue TRANSFORM, amber REQUIRE_APPROVAL, red DENY.
- "Ledger integrity ✓" badge driven by `verify_chain()`, showing the actual result —
  never hardcoded.
- Session picker listing the committed demo sessions.

**3. Eval**
- Reads the committed `eval/results.json`. **Never computes the grid.**
- Shows the completed cells, the margin-uplift plot, the false-block vs
  violation-prevention scatter, and the injection suite results.
- Renders the limitations text from `report.md` prominently — n, single seed, single
  model, trajectory replay, quota-truncated. The limitations must be visible on the
  tab, not buried behind an expander.
- Show which cells completed and which didn't. Partial coverage displayed honestly
  reads better than a table that hides the gaps.

**4. Architecture**
- The system diagram (the agent-boundary one from the original brief).
- The policy DSL rendered from `policies/default.yaml`, live from the file so it can't
  drift.
- Short prose on the three TRiSM pillars: Explainability, Application Security,
  Governance.

## Approvals

Fold the `REQUIRE_APPROVAL` queue into the Session replay tab rather than adding a
fifth tab — approve/deny buttons and the timeout countdown. If that makes the tab
cluttered, a fifth tab is acceptable, but prefer four.

## Demo data

The Space has an ephemeral filesystem, so:
- Commit a pre-seeded read-only `demo_ledger.db` containing the interesting sessions:
  happy path, policy DENY with recovery, stock conflict, role violation. Session
  replay reads it.
- Live runs write to a fresh in-memory DB that dies with the session.
- Build the script that generates `demo_ledger.db` from existing ledger data — no new
  LLM calls. If the Phase 7 sessions are still in the local ledger, extract from
  there.

## Verification

- All existing tests still pass.
- Tests for anything with logic (the passphrase gate, the call cap, the results.json
  loader). Don't test Streamlit rendering itself.
- Launch it locally and show me screenshots of all four tabs before the checkpoint.

## Timebox

**3 hours.** If it isn't done, cut the Live run tab entirely — Session replay, Eval,
and Architecture are the ones that matter for a judge, and a recorded video covers
live execution. Tell me if you hit that point rather than pushing on.

Then the git checkpoint. Run `git status --short` yourself first.
