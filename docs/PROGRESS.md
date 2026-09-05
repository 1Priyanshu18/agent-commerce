# Progress Log

Notable architecture decisions and pivots, in order. Not a full changelog — see git history
for that; this is for decisions that need their rationale kept alongside the code.

## 2026-09-04 — Provider-agnostic LLM layer, free-tier first

**What changed:** `core/llm.py` (a single Anthropic-specific client with local response
caching) was replaced with `core/llm/`, a provider-agnostic package. Nothing outside
`core/llm/` imports a vendor SDK or touches a vendor-specific response shape — every call
site works against `LLMClient` (a `Protocol`) and normalized types (`Message`, `ToolSpec`,
`ToolChoice`, `ToolCall`, `LLMResponse`).

**Why:** Development was going to run up a real API bill on Anthropic during ordinary
iteration (Phase 4 alone needed dozens of calls just to build and test the buyer agent's
tool-use loop). Free tiers on Gemini and Groq remove that cost during development; Anthropic
is kept as an adapter for one final paid run once the harness works.

**Structure:**
- `types.py` — the normalized `LLMClient` protocol and data types.
- `fake.py` — deterministic, scriptable fake with no network calls. The full test suite
  (209 tests as of this entry) runs against this, never a real provider.
- `anthropic.py`, `gemini.py`, `groq.py` — one adapter per provider, each translating our
  normalized types to/from that provider's actual wire format.
- `cache.py` — `CachingLLMClient`, a decorator wrapping any `LLMClient`. Disk-backed, keyed
  by a hash of `(provider, model, system, messages, tools, tool_choice, temperature,
  max_tokens)`. A cache hit never reaches the wrapped client — no rate-limit slot consumed, no
  retry, no budget spent. (Deviates from the originally-specified key by including
  `max_tokens`: a different `max_tokens` can produce a genuinely different, truncated
  response, so omitting it risked incorrect cache hits.)
- `resilience.py` — `GuardedLLMClient`, another decorator: sliding-window requests-per-minute
  limiting, retry with exponential backoff + jitter on `RetryableError` (never on
  `FatalError`), and a hard per-run call budget (`CallBudgetExceededError`) that aborts
  rather than silently degrading.
- Composition order: `CachingLLMClient(GuardedLLMClient(raw_adapter))` — cache is checked
  first, so a hit skips rate-limiting/retry/budget entirely.

**Provider specifics learned by building against the real APIs** (not just docs — see the
live smoke test below):
- Groq is OpenAI-wire-compatible; forced tool choice is
  `{"type": "function", "function": {"name": ...}}`.
- Gemini's forced tool choice is a `ToolConfig` with `FunctionCallingConfig(mode=ANY,
  allowed_function_names=[...])`; JSON schemas pass through directly via
  `FunctionDeclaration.parameters_json_schema`, no translation needed.
- Gemini's `finish_reason` is `"STOP"` even when the turn produced a function call — unlike
  Anthropic/Groq, which have a distinct tool-use finish reason. The adapter must check
  `tool_calls` presence before consulting the finish-reason table, or every tool call gets
  misreported as `end_turn`. (Found and fixed via the adapter normalization tests, before the
  live call — the live call then confirmed the fix was right.)
- Anthropic and Gemini both require multiple tool-call results within one turn to be grouped
  into a single API-native message (Anthropic: one `user` message with multiple
  `tool_result` blocks; Gemini: one `Content` with multiple `function_response` parts).
  Splitting them across separate messages is documented (Anthropic) to train the model away
  from parallel tool use. Groq/OpenAI-shaped APIs don't have this requirement — each tool
  result is its own message there.
- `gemini-2.5-flash` (the model this was originally built against, based on third-party
  aggregator data) returned a 404 on live testing — no longer available to new API keys. The
  API's own error message named the replacement: `gemini-3.6-flash`, now the default.

**Live validation:** a real call to Gemini (`gemini-3.6-flash`) ran the actual constraint
extraction flow against the goal "Buy a birthday gift under Rs 2000 for my 10-year-old
nephew" and correctly extracted `budget_ceiling_paise: 200000` and
`recipient_context: "10-year-old nephew"`, both via the raw SDK call and through
`GeminiLLMClient.complete()`, confirming the normalization matches reality, not just the
adapter's own unit tests.

**Eval plan (Phase 8, not yet built) updated accordingly:** default grid drops from 40
goals/5 seeds to 20 goals/3 seeds (larger grid stays available behind a flag); the runner
must checkpoint per-session results and resume after a quota exhaustion; `provider` and
`model` are recorded dimensions in `eval/results.json`; a cross-model robustness check
(do the headline findings hold across two providers?) is planned as an additional analysis.

**Explicit non-goals:** no local inference (no GPU on the dev machine; CPU inference would
make `parse_failure` a measurement of the model, not the system). No automatic fallback
between providers mid-run — a silent provider switch would invalidate eval results.

## 2026-09-04 — Groq becomes the primary provider; Gemini's free tier can't support Phase 8

**What changed:** `LLM_PROVIDER` default flipped from `gemini` to `groq` in `Config` and
`.env.example`. `GROQ_MODEL` default corrected from `llama-3.3-70b-versatile` (now
enterprise-only on Groq, not available on the free tier at all) to `openai/gpt-oss-120b`
(confirmed via Groq's live docs: supports tool/function calling, JSON schema mode, 131K
context). Gemini drops to secondary, used only for the Phase 8 cross-model robustness check
against a small sample — not the main eval grid.

**Why:** Building and testing Phase 7 (`scripts/run_demo_session.py`) against live Gemini
exhausted its free-tier daily quota: `gemini-3.6-flash` is capped at **20 requests/day**
(confirmed live via a 429 `RESOURCE_EXHAUSTED` response naming the exact limit). That's
unworkable for iterative development, let alone Phase 8's eval grid. Groq's free tier is
**1,000 requests/day**, a ~50x improvement.

**Groq free-tier limits for `openai/gpt-oss-120b`** (fetched live from
console.groq.com/docs/rate-limits, not recalled from training data — the model previously
defaulted here, `llama-3.3-70b-versatile`, no longer appears in the free-tier table at all):

| | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| Free tier | 30 | 1,000 | 8,000 | 200,000 |

A Developer (paid) tier exists with higher limits, but the exact figures live behind the
account billing page, not the public docs — not fetchable without the user's account access.

**Phase 8 feasibility at the reduced grid (3 upsell conditions × 2 enforcement levels × 20
goals × 3 seeds = 360 sessions, ~15 calls/session ≈ 5,400 calls total), cold cache, Groq free
tier only:**

- **By RPD (1,000/day):** 5,400 ÷ 1,000 ≈ **5.4 days minimum**, assuming every single request
  that day goes to the eval and none to development/debugging.
- **By TPD (200,000/day):** this is the binding constraint, not RPD. Estimating ~1,500 tokens/
  call on average (system prompt + 7 tool schemas resent every call, ~500 tokens, plus growing
  message history across up to 8 turns; GPT-OSS output is lean — no Gemini-style thinking-token
  bloat) gives roughly 5,400 × 1,500 ≈ 8.1M tokens total. 8.1M ÷ 200,000 ≈ **~40 days
  minimum**. This is an estimate, not measured — the real number depends on how much
  conversation history each turn actually carries — but the order of magnitude (weeks, not
  days) is the important finding regardless of the exact multiplier.

**Conclusion: the reduced grid does not fit in one day, or even one week, on Groq's free tier.**
TPD is ~7x more binding than RPD, so grid-size cuts alone help less than reducing tokens per
call would. Splitting cells across Groq and Gemini does not meaningfully help — Gemini's 20
req/day is under 1% of the gap.

**What the Phase 8 runner needs, given this — flagged now, not discovered mid-run, decision
pending user input:**
1. **Per-session checkpointing and resume** regardless of which other lever is chosen — a
   multi-day run (free tier) or even a same-day paid-tier run should survive a crash or an
   intentional pause without redoing completed sessions. Building this in from the start of
   Phase 8, not bolted on after a run dies.
2. Requests a decision from the user among: (a) accept a multi-week free-tier run via
   checkpointing, (b) upgrade to Groq's paid Developer tier for the eval window (Groq's
   published per-token pricing for comparable models is roughly $0.15/$0.60 per 1M in/out
   tokens — at ~8.1M total tokens that's on the order of a few dollars, not a large spend, but
   the user's call to make and to confirm against actual Developer-tier pricing), (c) cut the
   grid further (a much smaller grid than currently planned — rough math suggests something
   like 30-40 sessions fits one free-tier day), or (d) reduce tokens/call (trim tool schema
   verbosity, cap how much history each turn resends) as an engineering lever independent of
   grid size. Not decided unilaterally; needs the user's answer before the Phase 8 runner is
   built around a specific assumption.
3. The `GuardedLLMClient` rate limiter currently only enforces **requests**-per-minute, not
   tokens-per-minute — for Groq specifically, TPM (8,000) is tight enough that a single
   large-history turn could exceed it even while comfortably under the RPM cap. Worth adding
   token-aware limiting before Phase 8's grid runs, not just relying on request counting.

## 2026-09-04 — Working rules from a bug hunt that burned live quota it didn't need to

**What happened:** Diagnosing the Gemini `thought_signature` bug below took several live
Gemini calls to isolate (constraint extraction alone, then through turn 1, then through turn
2) before the actual fix was written — and a stray `rm -rf .cache/llm` diagnostic step wiped
cached responses that then had to be regenerated live. Combined, this used a meaningful chunk
of a 20-requests/day budget on a bug that was fully diagnosable without any live calls.

**Working rules going forward (for this project and its later phases):**
- **Diagnose against `core/llm/fake.py` first.** A hang, a wrong-shape response, or a parsing
  bug is almost always reproducible by scripting `FakeLLMClient` with a response shaped like
  the suspected bad case — no network, no quota, no rate limit. Reserve live calls for
  *confirming* a fix once the fake-based repro is understood, not for the exploration itself.
- **Never delete `.cache/llm/` (or any provider's response cache) as a diagnostic step.** The
  cache is the primary defense against quota exhaustion, especially once Phase 8 re-runs the
  eval grid repeatedly while debugging the harness — a cold cache there re-spends real quota
  on every subsequent run, not just the one being debugged. To isolate a cache-invalidation
  problem specifically, use a bypass flag for that one call instead (`build_client(...,
  bypass_cache=True)`, exposed as `--no-cache` on `scripts/run_demo_session.py`) — never touch
  the store itself.

## 2026-09-04 — Gemini's `thought_signature` requirement: a genuine multi-turn tool-use bug

**What happened:** The buyer agent's tool-use loop worked in every test (all scripted against
`FakeLLMClient`) and in the original Phase 4 live smoke test (a single, non-multi-turn call).
The first time a *real*, multi-turn Gemini session ran the actual loop (Phase 7's demo
script), turn 2 failed with `400 INVALID_ARGUMENT: Function call is missing a
thought_signature in functionCall parts`.

**Root cause:** Gemini's `gemini-3.6-flash` (a "thinking" model) attaches a `thought_signature`
— opaque bytes — to each `function_call` part in its response, living on the same `Part`
object as the function call itself (not on `FunctionCall`, and not exposed via the SDK's
`response.function_calls` convenience property — only visible by iterating
`response.candidates[0].content.parts` directly). Gemini's API requires that signature to be
echoed back verbatim when that tool call is replayed as conversation history on a later turn.
The adapter (`core/llm/gemini.py`) was reconstructing `FunctionCall` parts from history without
it, so any second real (non-cached) turn in a tool-use loop was guaranteed to fail — not a rare
edge case, but the *normal* path for any session with more than one tool call.

**Fix:**
- `ToolCall` (`core/llm/types.py`) gained an optional `provider_metadata: dict | None` field —
  opaque, provider-specific round-trip data that only the originating adapter reads back;
  every other adapter and all orchestrator code ignores it.
- `gemini.py`'s response parsing now iterates `response.candidates[0].content.parts` directly
  (not `response.function_calls`) so it can pair each function call with its sibling
  `thought_signature`, stored as `{"thought_signature": <bytes>}` in `provider_metadata`.
- `gemini.py`'s history reconstruction (`_contents_from_messages`) now passes
  `thought_signature=` back onto the rebuilt `Part` when present.
- `CachingLLMClient` needed a matching fix: `provider_metadata` can now hold raw `bytes`
  (Gemini's signature), which plain `json.dumps` cannot serialize. Cache read/write now
  recursively base64-encodes/decodes `bytes` values via a small `_encode_bytes`/`_decode_bytes`
  helper, so a cached tool call round-trips its signature exactly rather than crashing on write
  or silently dropping the field on read.
- Caught via new unit tests first (`test_gemini_carries_thought_signature_into_provider_metadata`,
  `test_gemini_echoes_thought_signature_back_on_the_next_turn`,
  `test_tool_call_provider_metadata_bytes_round_trip_through_cache`), confirmed against the
  real API second.

**Why this matters for the write-up:** this is a concrete, non-hypothetical case for the
provider-agnostic abstraction layer's value. It's a requirement specific to one provider
(Anthropic and Groq have no equivalent concept — a tool call is just a tool call) that would
have silently broken the buyer agent's multi-turn loop in production if `core/llm/` didn't
exist as a boundary: every call site above it (`agents/buyer/agent.py`,
`orchestrator/run_session.py`) is unaware this fix happened at all, because `ToolCall` stayed
the same shape from their point of view — only the Gemini adapter and the cache needed to
change. Also a caution for future adapter work: a feature invisible in a provider's
"convenience" response fields (here, `response.function_calls`) can still be required by the
API on the next call — worth checking the raw response shape, not just the high-level
accessor, especially for anything resembling reasoning/thinking state.

## 2026-09-05 — Bugs found producing the Phase 7 demo traces

Getting one real, live rendered ledger trace for each of the two demo-lead failure paths
(`stock_conflict`, `policy_deny_recovery`) via `scripts/run_demo_session.py` surfaced three
real bugs, none of which any test caught beforehand — all of them exist because the full test
suite runs against `FakeLLMClient`, which always supplies a correct, hand-authored
`transaction_id` and a well-formed message history. A live model doesn't guarantee either.

1. **Buyer agent transaction_id hallucination (governance-relevant).** Nothing in the system
   prompt or the initial user message ever states the session's actual `transaction_id`, yet
   every buyer tool's schema requires it as a parameter. A real LLM has no way to know the
   real value, so it invents a plausible-looking one of its own (observed live, e.g.
   `tx_gift_10yo`). Before the fix, this meant the real MCP tool calls mutated a *different*
   session's cart than the one `BuyerSessionRunner`'s own policy/stock checks were reading —
   silently splitting session state in two. This is worth naming specifically in the
   Governance section of the write-up: an agent that can cause its own actions to be recorded
   under an ID *it chose* is a real audit-trail integrity risk, not just a functional bug —
   provenance built on `transaction_id` is only trustworthy if that ID is authoritative, not
   agent-supplied. Fixed by having the orchestrator overwrite `tool_input["transaction_id"]`
   with its own real value before every tool call (`run_session.py:_execute_tool_call`),
   structurally, rather than trying to prompt the model into using the right one.
2. **Unbounded search results could overflow a provider's per-request token limit.**
   `catalog.search` returned every matching product with no cap. An agent that repeatedly
   guesses an invalid category (see #3) and eventually searches with no filter at all gets
   back all 72 products — enough tokens, once sitting in conversation history, to make the
   *next* request exceed Groq's free-tier 8,000 TPM limit outright (a 413, not a retryable
   429 — retrying doesn't help an oversized request). Fixed by capping what the tool hands
   back to the agent to 10 results plus a hint to narrow the query when truncated
   (`mcp/buyer_server.py`); the ledger's own SEARCH entry still records the true count.
3. **Gemini's `thought_signature` requirement** — see the dedicated entry above.

## 2026-09-05 — policy_deny_recovery live convergence: not solved, measured instead

**The attempt record.** Producing a live `policy_deny_recovery` trace against Groq's free-tier
`openai/gpt-oss-120b` took roughly six live attempts, iterating a real bug fix or a prompt
change between most of them (the transaction_id and search-cap bugs above were found *during*
this process, not before it; category-hint and remove-vs-add prompt wording were both tuned in
response to specific observed failures). Across those attempts: several ended in
`turn_limit_reached` before the agent ever added anything to the cart (excessive, sometimes
zero-result, category search); one genuinely executed the correct recovery — `cart.remove` the
over-budget item, then `cart.add` a cheaper one — but ran out of turns two calls short of
retrying `checkout.confirm`; and, after the turn budget was raised from 12 to 16 to give that
attempt more room, the *next* attempt regressed and stacked two more items on top of the
original instead of removing it, denied a second time at triple the original total. A Gemini
attempt (in case a different model converged more reliably) hit Gemini's exhausted 20/day
quota before producing a result at all.

**What this means, and the decision made about it.** In every single attempt, the policy
engine denied the over-budget `checkout.confirm` correctly — 100% of the time, with the exact
right number attached. The gate never failed. What varied was the buyer agent's own recovery
*planning* after receiving that denial — sometimes correct, sometimes not, independent of how
much turn budget or how explicit a hint it was given. This is the AgenticPay paper's own
finding (frontier models don't reliably converge on constrained multi-step negotiation without
being handed the mechanism, not just told to do it) showing up in this system's own recovery
path, not a bug local to this codebase. Chasing it by repeatedly enlarging
`MAX_TOOL_LOOP_TURNS` doesn't fix that — it just delays the point where a non-converging agent
gives up, and would keep costing more of a very tight free-tier quota per attempt. Decision:
`MAX_TOOL_LOOP_TURNS` is reverted to and capped at 12 (see the comment at its definition in
`orchestrator/run_session.py`); `turn_limit_reached` is treated as a legitimate, expected
outcome, not a defect to engineer away.

**New Phase 8 metric (not yet built): recovery attempt correctness.** Given a `DENY` with a
`human_reason`, does the buyer agent's next action taken:
- **correctly recover** (remove/replace to bring the cart under the ceiling, then retry),
- **incorrectly recover** (e.g. add another item on top, making it worse), or
- **never resolve** (`turn_limit_reached` before either)?

This is directly measurable from the ledger alone (the `caused_by` link from the DENY entry to
whatever `select` entry follows it, plus the resulting cart total), needs no extra
instrumentation, and — broken down by provider — is a substantially more informative Phase 8
result than a single clean demo trace would have been: it turns "did the demo work" into "how
often, and for which providers, does recovery actually work."

**The two demo-lead traces, as actually produced:** `stock_conflict` succeeded live (Groq,
`openai/gpt-oss-120b`) on the first attempt after the three bugs above were fixed — genuine
injection, genuine recovery, `verify_chain().ok == True`. `policy_deny_recovery` did not
converge live within the attempts above; the trace shown for it is the deterministic
`FakeLLMClient`-scripted one from `test_policy_deny_recovery_injection_and_recovery`, labeled
explicitly as scripted rather than presented as a live run. The mechanism it exercises
(injection, a real budget-ceiling `DENY`, the structured recovery hint, the `caused_by` link)
is identical to what a converging live run would show — only the model's own planning is
absent.
