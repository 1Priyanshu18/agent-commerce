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
