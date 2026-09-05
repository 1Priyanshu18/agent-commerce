# Agent Commerce: technical write-up

This is a companion to README.md, organized around the three TRiSM pillars this project
addresses directly: Explainability, Application Security, and Governance. ModelOps and
Privacy are out of scope, covered briefly at the end.

## Explainability

Every action in the system, a search, a cart mutation, a policy check, a payment call, a
webhook, a reconciliation pass, is written to a single append-only ledger as a structured
entry rather than a free-text log line.

The ledger is a provenance DAG, not a flat log. Every entry carries a `caused_by` field
naming the entry or entries that produced it. A checkout denial links back to the cart
mutation that triggered the policy check, a recovery attempt links back to the denial that
prompted it, a payment call links back to the ALLOW verdict that authorized it.
Reconstructing why something happened is a graph walk backward from any entry, not a search
across a flat table for plausible nearby rows.

Reasoning is mandatory on every entry that represents a decision: a `reasoning_summary`
carrying the agent's own stated reasoning where an LLM was involved, or a structured
description of the deterministic logic that fired otherwise. Policy rejections go further.
`human_reason` is never hand-written at a call site. It is a template on the rule itself in
`policies/default.yaml`, for example `"cart total {cart.total} exceeds buyer budget ceiling
{session.buyer_budget}"`, interpolated against the real values at check time. The explanation
a human reads always matches the values the engine evaluated, because both come from the same
interpolation step.

One gap surfaced during this build and is worth naming directly. A fallback no-offer
decision, the upsell LLM call failing outright, the model not invoking the forced tool, an
invalid SKU, and a genuine no-offer decision wrote the exact same ledger entry shape. Nothing
distinguished a model that looked at the cart and declined from a call that failed and fell
back to decline. A schema bug surfaced through exactly this gap: `_DECIDE_TOOL`'s JSON schema
required two fields even when they were meaningless for a decline, so a genuine decline
naturally omitted them and Groq's server-side validation rejected the call before a response
existed to parse. Because the fallback and a genuine decision looked identical in the ledger,
this went undetected until a cross-check against a second, deterministic strategy sharing the
same candidate pool showed a 12-out-of-12 mismatch rate between conditions that should have
behaved similarly. The fix pairs the schema correction with an explicit `machine_reason` on
every fallback path, `None` for a genuine decision, a distinct label per failure cause,
threaded into the eval's own per-session metrics so it is queryable directly rather than
reconstructed by hand. It proved its value almost immediately: the very next evaluation run
hit a different, unrelated model reliability issue (the model responding without invoking the
forced tool at all), and the taxonomy labeled it correctly on the first try instead of
silently counting it as a decision. Without this taxonomy, a whole class of low-offer-rate
results is ambiguous between a strategy exercising genuine restraint and a strategy that is
simply unreliable at deciding at all. See Findings below for how this shows up as an open
question in the evaluation results rather than a closed one.

## Application Security

The policy engine evaluates real values, cart totals, discount percentages, SKUs, at the
moment a tool is about to execute, rather than only checking which tool was called. The same
engine also supports a `tool_level_only` mode that gates purely on tool identity and ignores
arguments, so the two enforcement levels can be compared instead of the difference being
merely asserted. Tool-level gating cannot distinguish a ₹500 checkout from a ₹50,000 one, both
are just a call to `checkout.confirm`. Argument-level enforcement is designed to catch the
second and let the first through; see the worked example in README.md for all four outcomes.

The evaluation grid did not end up exercising this comparison, and that is stated plainly
rather than left for the numbers to imply otherwise. Every goal in the grid has a compliant
purchase available, and the buyer agent stayed under its own budget ceiling in every session,
so no goal ever attempted a real over-budget checkout and the grid's violation-prevention
figures read 0/0 in both enforcement levels. That is not evidence the two levels are equally
safe. It means the grid supplies no empirical evidence, in either direction, for whether
argument-level enforcement actually prevents more violations than tool-level enforcement. The
comparison mechanism is built and working, it simply was not tested against a real violation
in this evaluation window. The real evidence for the gate's reliability under pressure comes
from a different source, the live-attempt record described in Findings below, where the
policy engine denied a real over-budget checkout correctly in every attempt regardless of
what the buyer agent did next.

The buyer agent and the merchant-side upsell agent are each backed by their own FastMCP
server, and neither server registers `policy.*` or `payment.*` as a callable tool. Those stay
orchestrator-only, invoked from code that no LLM output ever reaches. This closes the
agent-collusion path structurally: even if both agents' reasoning somehow converged on a
mutually convenient outcome, neither has a tool that lets it approve its own transaction or
move money directly. Role separation is enforced twice, structurally, since a server simply
has no such tool to call, and again through a defense-in-depth `authorize()` check inside
every tool handler that verifies the calling actor matches the tool's intended role before
doing anything else. This second layer was exercised for real during this build: the demo
data includes a captured `role_violation` entry, produced by deliberately calling a
cart-mutating tool as the upsell actor. `authorize()` rejected it and logged the attempt
before any state changed.

A dedicated adversarial suite ran real sessions against products whose descriptions or
reviews carry injected instructions, a direct instruction, a fake system message, and an
indirect instruction via review text. Across every session, the buyer agent was never induced
to act on the injected text, so no injected instruction ever reached the policy gate. Read
that result carefully. It is evidence of absence, not evidence the gate defeats injection. The
suite shows the injection never got far enough to test the gate's defense specifically, it
does not show the gate successfully blocking one. The stronger positive evidence for the
gate's robustness comes from the live-attempt record in Findings below.

Razorpay webhooks are verified via `X-Razorpay-Signature`, HMAC-SHA256 over the raw request
body, before any parsing happens, so a malformed or unsigned payload is rejected before it can
influence anything. Every payment operation is idempotent on `(transaction_id, attempt_no)`,
so a retried request from a network blip or a duplicate webhook delivery can never
double-charge or double-record an order.

## Governance

Append-only is enforced at the database layer through triggers that reject UPDATE and DELETE
outright, not just a convention the application code happens to follow. Each entry's hash
incorporates the previous entry's hash, so `verify_chain()` can walk the entire ledger and
detect any insertion, edit, or reordering anywhere in it, not just within one transaction.
This check is never hardcoded to report success. It recomputes every hash from the stored
data each time it runs, and the Session replay tab's ledger-integrity badge reflects a live
call to it.

`policies/default.yaml` is parsed and compiled into callables once, at boot, and the compiled
result carries a `policy_version`, a SHA-256 hash of the canonicalized rule set truncated to
16 hex characters. Every ledger entry that records a policy decision carries that version, and
every Razorpay order created through this system embeds it in the order's `notes` field. Any
completed order can be traced back to the exact rule set that authorized it. If the policy
changes tomorrow, an order approved today still shows which version approved it, rather than
an ambiguous reference to "current policy" that means something different after the next
deploy.

A `REQUIRE_APPROVAL` verdict queues the checkout for human review rather than deciding it
automatically. If that review does not happen within the configured timeout, the queue entry
resolves to denied rather than to an implicit approval, so an unattended high-value checkout
can never complete simply because nobody looked at it in time.

Every payment is checked against three independent sources: this system's own order record, a
fresh read from Razorpay's own API, and whatever webhooks actually arrived, reported as
matched, pending, or mismatch. This runs both on webhook receipt and on a timer, so a dropped
webhook does not strand an order in an unreconciled state indefinitely.

## Findings worth surfacing on their own

These came out of actually building and running the system, not from designing it on paper.
Each one changed something in the implementation, not just the write-up.

### The llm-vs-rules offer-rate divergence

The clearest result from the evaluation grid isn't a margin number. With only one or two
offers ever accepted across the whole grid, no margin conclusion is supportable at this
sample size, see `eval/report.md` for the full caveat. It's how often each upsell strategy
makes an offer at all. The deterministic `rules` strategy offered in every session where a
valid candidate existed, while the LLM-driven strategy offered in a small minority of
comparable sessions, and even that minority splits into two numbers depending on whether
sessions where the model never produced a valid decision are counted as declines or excluded.
Both numbers are reported side by side in `eval/report.md`, never one silently substituted
for the other.

Two readings are consistent with this data, and this evaluation does not resolve which one
dominates. The LLM strategy may be exercising real restraint a fixed rule can't express,
declining when an offer genuinely isn't warranted. Or it may simply be less reliable at
producing a valid decision, with the low offer rate partly an artifact of that unreliability.
The sessions that hit a distinct, known failure mode, the model not invoking the forced tool,
are evidence for the second reading. What the `machine_reason` taxonomy contributes isn't an
answer to which reading is correct. It's the ability to say how much of the gap is
attributable to a known failure mode versus genuinely ambiguous, instead of reporting one
unqualified percentage and calling it settled.

### The provider-agnostic LLM boundary earning its keep

The buyer agent's tool-use loop worked in every scripted test and in an initial single-call
smoke test against a thinking-capable Gemini model. The first time a real, multi-turn Gemini
session ran the actual loop, the second turn failed outright. Gemini attaches an opaque
`thought_signature` to each function-call part in its response, living on the response part
itself rather than the SDK's high-level convenience accessor, and requires that exact
signature to be echoed back when the call is replayed as history on a later turn. Anthropic
and Groq have no equivalent concept, a tool call is just a tool call for both. Fixing this
meant adding an optional, provider-opaque `provider_metadata` field to the system's internal
`ToolCall` type, populated and consumed only by the Gemini adapter. Every call site above the
LLM boundary, the buyer agent, the orchestrator, needed zero changes and stayed unaware the
fix happened. This is the concrete argument for keeping a real adapter boundary instead of
coding directly against one vendor's SDK: a requirement invisible in a provider's convenience
API surface can still be mandatory on the wire, and a proper boundary contains that surprise
to one file instead of leaking it through the whole call stack.

### Temperature-0 trajectory drift

The evaluation grid compares upsell strategies by giving each condition the same goal and
relying on a shared response cache so, in principle, all three conditions share an identical
shopping trajectory up to the point the strategies diverge. In practice a cache miss can
still occur even at temperature 0, since inference isn't perfectly reproducible call-to-call
under batched serving, and when it does the buyer can land on a genuinely different product,
sometimes at an identical price point. This shows up directly in the collected data: for one
goal, two conditions landed on the same SKU while a third landed on a different one at the
same price, with no upsell offer accepted anywhere in that comparison, meaning part of the
measured margin difference in that cell is trajectory noise rather than the upsell agent's
effect. The lesson generalizes past this project. An LLM-response cache is not a substitute
for an explicit, deterministic trajectory-replay mechanism when the actual goal is a
controlled comparison between conditions. A more careful version would pin and replay the
reference trajectory's literal tool calls rather than depending on a cache hit; that
engineering is future work, flagged here rather than left silent.

### The live-attempt record

One of the four reproducible failure paths this system models is a buyer going over budget,
getting denied, and needing to recover: remove the over-budget item, choose something
cheaper, retry checkout. Producing a live trace of this path, rather than the scripted,
deterministic test-double version, took a number of real attempts, and the result is more
informative than a single clean trace would have been. Across every attempt, the policy
engine denied the over-budget checkout correctly, with the right machine-readable and
human-readable reason attached. The gate did not fail once, regardless of what the buyer
agent did next. What varied attempt to attempt was the buyer agent's own recovery planning.
Some attempts executed the correct remove-then-add sequence and completed successfully. One
attempt instead made the cart worse, stacking a second item on top of the over-budget one
rather than removing it, and was denied again, correctly, at an even higher total. Other
attempts never got far enough into the session to test recovery at all. This is the clearest
evidence in this project for why constrained-purchase enforcement belongs in a separate,
deterministic layer rather than delegated to agent judgment: the gate's correctness held
perfectly across every variation in the agent's own behavior, while the agent's behavior
varied substantially. It also confirms, directly and in this system's own data rather than by
citation, a finding reported elsewhere in the agentic-commerce literature: that frontier
models don't reliably converge on constrained multi-step recovery on their own.

### The transaction_id hallucination

Nothing in the buyer agent's system prompt or initial message states the actual
`transaction_id` of its own session, yet every one of its tools requires that ID as a
parameter. A real model, with no way to know the true value, invents a plausible-looking one
of its own. Before this was caught and fixed, the agent's real tool calls mutated a different
session's cart than the one the orchestrator's own policy and stock checks were reading,
silently splitting one session's state into two, with the provenance chain rooted in an
identifier the agent chose rather than one the system assigned. This is a governance risk
independent of the functional bug it also caused: an audit trail built on a `transaction_id`
is only trustworthy if that ID is authoritative, and an agent that can cause its own actions
to be recorded under an ID it selected undermines the audit trail's basic premise. The fix is
structural rather than prompt-based. The orchestrator overwrites the `transaction_id` on
every tool call with its own authoritative value before that call executes, so correctness
depends on nothing else being able to supply a different one, not on the model using the
right one.

## Out of scope

ModelOps was not addressed: model versioning, deployment rollout, drift monitoring, and
automated retraining are all absent. The provider-agnostic LLM boundary in `core/llm/` makes
swapping a model or provider a contained, one-adapter change, but operating that swap as a
managed rollout process was not built.

Privacy was not addressed either: no PII handling, redaction, retention policy, or
data-subject request tooling exists. Catalog and cart data are synthetic, and the ledger
records buyer goals and reasoning text verbatim with no anonymization layer.
