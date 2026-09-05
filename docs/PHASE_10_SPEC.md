# PHASE 10 — HF Spaces deployment + submission artifacts

Same scope rigidity as Phase 9. One Space, Streamlit SDK, free tier.

## Space setup

`README.md` frontmatter, both versions pinned (a silent upstream bump would break the
Space during judging):

```yaml
---
title: Agent Commerce
sdk: streamlit
sdk_version: <the version we developed against>
app_file: app.py
python_version: "3.11"
---
```

Pin `requirements.txt` to exact versions too.

Four HF-specific things:
- **Ephemeral disk** — `demo_data/demo_ledger.db` is committed and read-only; live runs
  use an in-memory DB. Verify this works with a read-only working directory.
- **Public Space spends my quota** — the Phase 9 passphrase gate, call cap, and daily
  budget must work off Space secrets. If the passphrase env var is unset, Live run
  fails closed (disabled, not open).
- **Secrets** — Space settings, read via `os.environ`. Runtime only, not build time.
  Ship no Razorpay credentials.
- **No webhook route** — Space runs `PAYMENT_MODE=simulated`. Full lifecycle still
  executes and appears in the ledger. Say so plainly in the README.

Tell me the exact env vars to set, required vs optional.

## Demo script

One command: seed catalog, run three sessions (happy path, policy DENY with recovery,
stock conflict), print ledger summary, launch Streamlit. Must work with
`LLM_PROVIDER=fake` — zero quota, can't fail mid-demo.

## README

Overview + Space link; architecture diagram + agent-boundary table; quickstart; the
policy DSL with a worked example of each of the four outcomes; eval results; then a
Limitations section of its own.

## Write-up, by TRiSM pillar

- **Explainability** — `caused_by` provenance DAG, mandatory `reasoning_summary`,
  human-readable rejections interpolated from rule templates
- **Application Security** — argument-level enforcement (cite our own
  `tool_level_only` baseline, not just the paper), role separation across two MCP
  servers with neither exposing `policy.*` or `payment.*`, the captured
  `role_violation`, injection results, webhook signature verification over raw body,
  idempotency keys
- **Governance** — hash-chained append-only ledger with `verify_chain()`, policy
  compiled offline and hash-versioned, `policy_version` in Razorpay order notes,
  approval queue failing closed, three-way reconciliation

State ModelOps and Privacy as out of scope.

Include these findings:
- Gemini `thought_signature` divergence — the concrete case for the provider abstraction
- Temperature-0 trajectory drift inflating apparent uplift with a non-upsell effect
- **The Phase 7 live-attempt record** — the agent flailed at recovery across 7
  attempts and the gate correctly DENIED every non-converging one. Strongest evidence
  in the project for the deterministic-gate thesis. Give it real space.
- The injection result as absence-of-failure, not proof
- The `transaction_id` hallucination bug as an audit-integrity risk we caught

Don't cite Lean-Agent Protocol's specific claims (microsecond latency, "impervious to
regulatory violations"). Two-phase compile/check only.

## Public-facing tone

Nothing about quota limits, free tiers, API budgets, or run interruptions appears
anywhere public — not the README, write-up, Space, or code comments. Those are internal
notes; keep them in `docs/PROGRESS.md` only.

Limitations state the facts without the backstory: sample size, single seed, single
model, trajectory-replay methodology, that the uplift figure isn't statistically
meaningful at that n, simulated buyer behaviour rather than a user study, simulated
payments on the Space, and that the gate's injection defence is untested since no
injected action reached it. Just the scope of the evidence, no explanation of why.

## Eval sections stay open

I'm adding a fresh API key and resuming the remaining grid cells (G08, G14) right
after this phase. So:
- Generate all eval numbers and the limitations sample-size line from
  `eval/report.md` — never hand-written — so re-running `report.py` updates the README
  automatically. Where generation isn't possible, leave a clearly marked placeholder.
- Confirm the resume path works end to end: new key in `.env` → `python -m eval.runner
  --tier A` picks up from the checkpoint, runs only incomplete cells, then `report.py`
  regenerates `results.json` stats, `report.md`, both plots, and the README's eval
  section. Tell me the exact commands.
- Confirm every cell in `results.json` records `provider` and `model`.

## Razorpay one-off

`scripts/live_test_checkout.py` has never run. Walk me through doing it once locally —
dashboard settings, test card, webhook URL, `cloudflared` command, what output means
success vs silent half-success — and how to capture that session into
`demo_ledger.db`. Don't block on it.

## Checkpoint + video

Print the commands for creating the Space, adding the remote, and pushing — I run them.
Then tell me what to configure in Space settings, and what to record for a 3-minute
video, in order. Lead with stock-conflict recovery; it runs reliably.

## Timebox

3 hours. If short: README and write-up first, then the Space, then Razorpay.
