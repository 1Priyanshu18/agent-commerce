# Agent Commerce

Constraint-based agentic commerce system for the Razorpay AI Buildathon (Track 01). A buyer
agent and a merchant decision layer transact against Razorpay's test-mode payment lifecycle,
gated by a deterministic policy engine and recorded to a hash-chained audit ledger.

## Setup

Requires Python 3.12.

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
pip install -e .
```

## Running

```bash
uvicorn agent_commerce.api.main:app --reload
```

`GET /health` returns `{"status": "ok", "env": "..."}`.

## Tests

```bash
pytest
ruff check .
```

## Components

- **`core/`** — `Money` (integer paise only), ID generation, canonical JSON, a clock
  abstraction, env-backed config.
- **`ledger/`** — append-only, hash-chained audit ledger (SQLite). Every entry links to the
  entries that caused it (`caused_by`), forming a provenance chain rather than a flat log.
  Append-only is enforced by DB triggers, not just application code. `verify_chain()` walks
  the whole ledger and recomputes every hash.
- **`catalog/`** — 72 products across 6 categories, seeded from a static fixture
  (`catalog/fixtures/products.json`) so every eval run sees the same data. Includes two
  adversarial products: a prompt-injection attempt embedded in a product description
  (`SKU-0007`), and an item priced ₹40 above a ₹2,000 budget ceiling to exercise small-gap
  negotiation logic later (`SKU-0002`).
- **`cart/`** — cart state and totals. `projected_margin_pct` here is the single source of
  truth used later by both the upsell rules and the eval harness.
- **`policy/`** — the deterministic authorization gate (`policies/default.yaml`). Two-phase:
  rules are compiled once at boot into callables (`policy/compiler.py`, `policy/expr.py` — a
  restricted expression language, not `eval()`), then checked cheaply at runtime
  (`policy/engine.py`). Four outcomes: `ALLOW` / `DENY` / `REQUIRE_APPROVAL` / `TRANSFORM`
  (e.g. capping an over-limit discount rather than rejecting the whole checkout). Enforcement
  is argument-level — the engine sees real values like `cart.total_paise`, not just which
  tool was called — and a `tool_level_only` mode exists solely to reproduce the "tool-level
  gating gives ~0% violation prevention" baseline for the eval. Every check is written to the
  ledger via `policy/service.py`; `REQUIRE_APPROVAL` verdicts are queued in `policy/approvals.py`,
  which fails closed (auto-denies) on timeout rather than leaving them open indefinitely. A
  malformed `policies/default.yaml` fails hard at boot — it never degrades into a permissive
  runtime.
- **`mcp/`** — two separate FastMCP servers, not one, each exposing only its own role's tools:
  - **Buyer server**: `catalog.search`, `catalog.get_details`, `cart.add`, `cart.remove`,
    `cart.view`, `upsell.respond`, `checkout.confirm`
  - **Merchant server**: `cart.read_at_checkout` (read-only projection — this server has no
    cart-mutating tool at all), `upsell.make_offer`, `upsell.no_offer`

  **Neither server exposes `policy.*` or `payment.*`.** An LLM can never call the thing that
  authorizes it or the thing that moves money — those stay orchestrator-only. This is the
  answer to the agent-collusion / mode-collapse risk class: even if the buyer and upsell
  agents' reasoning somehow converged on a mutually convenient outcome, neither has a tool
  that lets it approve its own transaction or touch payment directly, so there's no path for
  two LLMs to informally "agree" past the policy gate.

  Role separation is enforced twice: structurally (each server only ever registers its own
  role's tools — the other role's tools don't exist to be called), and via a defense-in-depth
  `authorize()` check (`mcp/authz.py`) that every tool handler calls first, which writes a
  `role_violation` ledger entry on any mismatch rather than just erroring silently.
- **`orchestrator/session.py`** — `SessionRegistry`, an in-memory `transaction_id -> Cart`
  map shared by both servers (so a cart item added via the buyer server is visible to the
  merchant server's checkout-time read). Not the orchestrator's full session state machine
  yet — that arrives once there's a buyer agent decision loop to drive it.
