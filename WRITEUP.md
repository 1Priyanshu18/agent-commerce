# Agent Commerce — technical write-up

This is a companion to `README.md`, organized around the three TRiSM pillars this project
addresses directly: Explainability, Application Security, and Governance. ModelOps and Privacy
are out of scope for this build — see the closing section.

## Explainability

Every action in the system — a search, a cart mutation, a policy check, a payment call, a
webhook, a reconciliation pass — is written to a single append-only ledger as a structured
entry, not a free-text log line. Two design choices make that ledger explainable rather than
just complete:

**A provenance DAG, not a flat log.** Every entry carries a `caused_by` field: the entry ID(s)
of whatever produced it. A checkout denial links back to the cart-mutation that triggered the
policy check; a recovery attempt links back to the denial that prompted it; a payment call
links back to the ALLOW verdict that authorized it. Reconstructing "why did this happen" is a
graph walk backward from any entry, not a search across a flat table for plausible nearby rows.

**Reasoning is mandatory, not optional.** Every ledger entry that represents a decision carries
a `reasoning_summary` — the agent's own stated reasoning where an LLM was involved, or a
structured description of the deterministic logic that fired otherwise. Policy rejections go
further: `human_reason` is never hand-written at a call site. It's a template that lives on the
rule itself in `policies/default.yaml` (e.g. `"cart total {cart.total} exceeds buyer budget
ceiling {session.buyer_budget}"`), interpolated against the real values at check time. This
guarantees the explanation a human reads always matches the values the engine actually
evaluated — there's no path for the two to drift apart, because they're generated from the
same interpolation step, not maintained separately.

**A gap we found, and a taxonomy that closed it.** Until recently, a fallback no-offer
decision — the upsell LLM call failing outright, the model not invoking the forced tool, an
invalid SKU — and a *genuine* no-offer decision wrote the exact same ledger entry shape.
Nothing distinguished "the model looked at this cart and declined" from "the call itself
failed and we silently fell back to decline." This is exactly how a real measurement bug
surfaced: a schema requiring two fields even when they were meaningless (a decline naturally
omits them) caused every clean decline to be rejected server-side before a response even
existed — and because the fallback and a genuine decision were indistinguishable, this went
undetected until a cross-check against a second, deterministic strategy sharing the same
candidate pool showed a 12-out-of-12 mismatch rate. The fix pairs a schema correction with an
explicit `machine_reason` on every fallback path (`None` for a genuine decision, a distinct
label per failure cause), threaded into the eval's own per-session metrics so it's queryable
directly rather than reconstructed by hand. It proved its worth almost immediately: the very
next evaluation run hit a *different*, unrelated model reliability issue (the model responding
without invoking the forced tool at all) and the taxonomy labeled it correctly on the first
try — instead of silently miscounting it as a decision, which is exactly the failure mode that
had just been fixed. Without this taxonomy, an entire class of low-offer-rate results is
ambiguous between two very different explanations: a strategy exercising genuine restraint, or
a strategy that's simply unreliable at producing a decision at all. See the Findings section
below for how this shows up as an open question in the evaluation results, not a closed one.

## Application Security

**Argument-level enforcement, not just tool-level.** The policy engine evaluates real values —
cart totals, discount percentages, SKUs — at the moment a tool is about to execute, not merely
which tool name was called. The same engine also supports a `tool_level_only` mode, gating
purely on tool identity and ignoring arguments, specifically so the two enforcement levels can
be compared instead of the difference being merely asserted. Where tool-level gating cannot
distinguish a ₹500 checkout from a ₹50,000 one — both are just "a call to `checkout.confirm`"
— argument-level enforcement is designed to catch the second and let the first through.

**The evaluation grid did not end up exercising this comparison, and we say so plainly rather
than let the numbers imply otherwise.** Every goal run in the eval grid has a compliant
purchase available, and the buyer agent stayed under its own budget ceiling in every session —
no goal ever attempted a real over-budget checkout, so the grid's violation-prevention figures
read 0/0 in both enforcement levels (see `eval/report.md`'s Limitations section). That is not
evidence the two levels are equally safe; it means the grid supplies no empirical evidence,
in either direction, for whether argument-level enforcement actually prevents more violations
than tool-level enforcement. The comparison mechanism is built and working — it simply wasn't
tested against a real violation in this evaluation window.

The real evidence for the gate's reliability under pressure comes from a different source: the
Phase 7 live-attempt record (see "The policy-deny-recovery record" in Findings, below). Across
roughly six to seven live attempts at a genuine over-budget recovery scenario, the policy
engine — running in its normal, argument-level mode — denied the over-budget checkout correctly
in 100% of attempts, including several where the buyer agent's own recovery planning failed
outright. That is what this system's claim about the gate holding under pressure actually rests
on, not the grid's enforcement-level comparison.

**Role separation across two MCP servers.** The buyer agent and the merchant-side upsell agent
are each backed by their own FastMCP server, and neither server registers `policy.*` or
`payment.*` as a callable tool at all — those stay orchestrator-only, invoked from code that no
LLM output ever reaches. This closes the agent-collusion path structurally rather than by
policy: even if both agents' reasoning somehow converged on a mutually convenient outcome,
neither has a tool that lets it approve its own transaction or move money directly.

Role separation is enforced twice, not once: structurally (a server simply has no such tool to
call), and again via a defense-in-depth `authorize()` check inside every tool handler, which
verifies the calling actor matches the tool's intended role before doing anything else. This
second layer isn't redundant paranoia — it was exercised for real during this build: the demo
data includes a captured `role_violation` entry, produced by deliberately calling a
cart-mutating tool as the upsell actor. `authorize()` rejected it and logged the attempt before
any state changed, which is exactly the intended failure mode for a defense-in-depth check to
demonstrate — not a hypothetical, but a recorded one.

**Prompt-injection results.** A dedicated adversarial suite ran real sessions against products
whose descriptions or reviews carry injected instructions (a direct instruction, a fake system
message, and an indirect instruction via review text). Across every one of those sessions, the
buyer agent was never induced to act on the injected text — no injected instruction ever
reached the policy gate, because the agent never attempted the action it would have gated. Read
that result carefully: it is evidence of absence, not evidence the gate defeats injection. The
suite shows the injection never got far enough to test the gate's defense specifically — it
does not show the gate successfully blocking one. The stronger positive evidence for the gate's
robustness under pressure comes from a different part of this project's evaluation: see "The
policy-deny-recovery record" below.

**Payment-layer hardening.** Razorpay webhooks are verified via `X-Razorpay-Signature`
(HMAC-SHA256 over the *raw* request body) before any parsing happens — a malformed or
unsigned payload is rejected before it can influence anything. Every payment operation is
idempotent on `(transaction_id, attempt_no)`, so a retried request (a network blip, a duplicate
webhook delivery) can never double-charge or double-record an order.

## Governance

**A hash-chained, append-only ledger with a real integrity check.** Append-only is enforced at
the database layer via triggers that reject UPDATE and DELETE outright — not just a convention
the application code happens to follow. Each entry's hash incorporates the previous entry's
hash, so `verify_chain()` can walk the entire ledger and detect any insertion, edit, or
reordering anywhere in it, not just within one transaction. This check is never hardcoded to
report success — it recomputes every hash from the stored data each time it runs, and the
Session replay tab's "Ledger integrity ✓" badge reflects a live call to it.

**Policy is compiled offline and hash-versioned.** `policies/default.yaml` is parsed and
compiled into callables once, at boot — not interpreted per-request — and the compiled result
carries a `policy_version`: a SHA-256 hash of the canonicalized rule set, truncated to 16 hex
characters. Every ledger entry that records a policy decision carries that version, and every
Razorpay order created through this system embeds it in the order's `notes` field
(`{"transaction_id": ..., "policy_version": ...}`). That means any completed order can be
traced back to the *exact* rule set that authorized it — if the policy changes tomorrow, an
order approved today still shows which version approved it, rather than an ambiguous "current
policy" reference that silently means something different after the next deploy.

**Approvals fail closed.** A `REQUIRE_APPROVAL` verdict queues the checkout for human review
rather than deciding it automatically. If that review doesn't happen within the configured
timeout, the queue entry resolves to *denied*, not to an implicit approval — an unattended
high-value checkout can never complete just because nobody looked at it in time.

**Three-way reconciliation.** Every payment is checked against three independent sources: this
system's own order record, a fresh read from Razorpay's own API, and whatever webhook(s)
actually arrived — reported as `matched`, `pending`, or `mismatch`. This runs both on webhook
receipt and on a timer, so a dropped webhook doesn't strand an order in an unreconciled state
indefinitely.

## Findings worth surfacing on their own

These came out of actually building and running the system, not from designing it on paper —
each one changed something in the implementation, not just the write-up.

### The llm-vs-rules offer-rate divergence — a finding with two open interpretations

The clearest result from the evaluation grid isn't a margin number — with only one or two
offers ever accepted across the whole grid, no margin conclusion is supportable at this sample
size (see `eval/report.md` for the full caveat). It's how often each upsell strategy makes an
offer at all: the deterministic `rules` strategy offered in every session where a valid
candidate existed, while the LLM-driven strategy offered in a small minority of comparable
sessions — and even that minority splits into two different numbers depending on whether
sessions where the model never produced a valid decision at all (see the Explainability
section's `machine_reason` taxonomy above) are counted as declines or excluded. Both numbers
are reported side by side in `eval/report.md`, never one silently substituted for the other.

This admits two competing readings, and this evaluation does not resolve which one dominates:
the LLM strategy may be exercising real restraint a fixed rule can't express — declining when
an offer genuinely isn't warranted for that cart — or it may simply be less reliable at
producing a valid decision at all, with the low offer rate partly an artifact of that
unreliability rather than judgment. The sessions that hit a distinct, known failure mode (the
model not invoking the forced tool) are direct evidence for the second reading. What the
`machine_reason` taxonomy contributes is not an answer to which reading is correct — it's the
ability to say precisely how much of the gap is attributable to a known failure mode versus
genuinely ambiguous, instead of reporting one unqualified percentage and calling it settled.

### A concrete case for the provider-agnostic LLM boundary

The buyer agent's tool-use loop worked in every scripted test and in an initial single-call
smoke test against a "thinking" Gemini model. The first time a real, *multi-turn* Gemini
session ran the actual loop, the second turn failed outright: Gemini attaches an opaque
`thought_signature` to each function-call part in its response — living on the response part
itself, not exposed through the SDK's high-level convenience accessor — and requires that exact
signature to be echoed back when the call is replayed as history on a later turn. Anthropic and
Groq have no equivalent concept; a tool call is just a tool call for both. Fixing this meant
adding an optional, provider-opaque `provider_metadata` field to the system's own internal
`ToolCall` type, populated and consumed only by the Gemini adapter — every call site above the
LLM boundary (the buyer agent, the orchestrator) needed zero changes and remained unaware the
fix happened at all. This is the concrete argument for keeping a real adapter boundary rather
than coding directly against one vendor's SDK: a requirement invisible in a provider's
convenience API surface can still be mandatory on the wire, and a proper boundary contains that
surprise to one file instead of leaking it through the whole call stack.

### Temperature-0 trajectory drift, and what it means for caching

The evaluation grid compares upsell strategies (none / rules / llm) by giving each condition
the same goal and relying on a shared response cache so, in principle, all three conditions
share an identical shopping trajectory up to the point the strategies diverge. In practice, a
cache *miss* can still occur even at temperature 0 — inference isn't perfectly reproducible
call-to-call under batched serving — and when it does, the buyer can land on a genuinely
different product, sometimes at an identical price point. This shows up directly in the
collected data: for one goal, two conditions landed on the same SKU while a third landed on a
different one at the same price, with no upsell offer accepted anywhere in that comparison —
meaning part of the measured margin difference in that cell is trajectory noise, not the
upsell agent's effect. The lesson generalizes past this one project: an LLM-response cache is
not a substitute for an explicit, deterministic trajectory-replay mechanism when the actual goal
is a controlled comparison between conditions. A more careful version would pin and replay the
reference trajectory's literal tool calls rather than depending on a cache hit; that engineering
is future work, flagged here rather than left silent.

### The policy-deny-recovery record — the strongest evidence for the deterministic-gate thesis

One of the four reproducible failure paths this system models is a buyer going over budget,
getting denied, and needing to recover: remove the over-budget item, choose something cheaper,
retry checkout. Producing a *live* trace of this path (rather than the scripted, deterministic
test-double version) took a number of real attempts, and the result is more informative than a
single clean trace would have been. Across every one of those attempts, the policy engine
denied the over-budget checkout correctly, with the exact right machine-readable and
human-readable reason attached — the gate did not fail once, in any attempt, regardless of what
the buyer agent did next. What varied, attempt to attempt, was the buyer agent's own recovery
*planning*: some attempts executed the correct remove-then-add sequence and completed
successfully; one attempt instead made the cart worse — stacking a second item on top of the
over-budget one rather than removing it — and was denied again, correctly, at an even higher
total; other attempts never got far enough into the session to test recovery at all. This is
the clearest evidence in this project for why constrained-purchase enforcement belongs in a
separate, deterministic layer rather than being delegated to agent judgment: the gate's
correctness held perfectly across every variation in the agent's own behavior, while the
agent's behavior varied substantially. It is also a direct, hands-on confirmation of a finding
reported elsewhere in the agentic-commerce literature — that frontier models don't reliably
converge on constrained multi-step recovery on their own — showing up in this system's own
data, not asserted from a citation.

### The `transaction_id` hallucination — an audit-integrity risk, not just a bug

Nothing in the buyer agent's system prompt or initial message ever states the actual
`transaction_id` of its own session, yet every one of its tools requires that ID as a
parameter. A real model, with no way to know the true value, invents a plausible-looking one of
its own. Before this was caught and fixed, that meant the agent's real tool calls were mutating
a *different* session's cart than the one the orchestrator's own policy and stock checks were
reading — silently splitting one session's state into two, with the provenance chain rooted in
an identifier the agent chose rather than one the system assigned. This is worth naming
specifically as a governance risk, not just a functional defect: an audit trail built on a
`transaction_id` is only trustworthy if that ID is authoritative. An agent that can cause its
own actions to be recorded under an ID *it selected* undermines the audit trail's basic premise,
independent of whatever functional bug it also happens to cause. The fix is structural rather
than prompt-based: the orchestrator overwrites the `transaction_id` on every tool call with its
own authoritative value before that call executes, so correctness doesn't depend on the model
using the right one — it depends on nothing else being able to.

## Out of scope

**ModelOps** — this project does not address model versioning, deployment rollout, drift
monitoring, or automated retraining. The provider-agnostic LLM boundary (`core/llm/`) makes
swapping a model or provider a contained, one-adapter change, but operating that swap as a
managed rollout process was not built.

**Privacy** — no PII handling, redaction, retention policy, or data-subject request tooling was
built. Catalog and cart data are synthetic; the ledger records buyer goals and reasoning text
verbatim, with no anonymization layer.
