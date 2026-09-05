"""The session state machine: drives the buyer agent's tool-use loop against the real buyer
MCP server, gating cart.add and checkout.confirm through the policy engine before they're
allowed to execute, and calling the payment layer on a successful checkout.

This is the only module in the codebase that calls policy/ and payments/ — the buyer agent
only ever proposes tool calls; this runner decides whether they're allowed to happen.

Phase 7 adds four reproducible failure paths via inject_failure (explicit .run() parameter,
falling back to the INJECT_FAILURE env var — explicit always wins). Three of them
(stock_conflict, policy_deny_recovery, and the natural DENY case that isn't injected at all)
are recoverable: the gate is not treated as terminal, so the tool_result (with its
human-readable reason) goes back to the agent and the loop keeps going, giving it a real
chance to adapt rather than ending the session outright. payment_failure is different — it's
an orchestrator-level bounded retry, not something the agent needs to react to.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.agents.buyer.constraints import BuyerConstraints
from agent_commerce.cart.models import Cart
from agent_commerce.catalog.store import CatalogStore
from agent_commerce.core.llm import Message, ToolSpec
from agent_commerce.core.money import Money
from agent_commerce.ledger.models import ActionType, Actor, PolicyVerdict
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments.adapter import PaymentAdapter, PaymentFatalError, PaymentRetryableError
from agent_commerce.payments.failure_injection import FailureInjectingPaymentAdapter
from agent_commerce.payments.models import OrderRecord
from agent_commerce.payments.simulated import SimulatedPaymentAdapter
from agent_commerce.policy.service import PolicyService

from .session import SessionRegistry

# 8 was enough for the happy path alone (search, add, checkout ≈ 3-4 turns); recovering from
# an injected failure needs turns on top of that (fail, remove, search/add, retry ≈ 3-4 more),
# so this was raised to 12 to give recovery genuine room. Deliberately NOT raised further:
# repeated live Groq runs showed a model that sometimes never converges within any reasonable
# turn count — searching indefinitely, or stacking a second item on top instead of removing
# the first (see docs/PROGRESS.md, "policy_deny_recovery live convergence" — this is the
# AgenticPay non-convergence finding surfacing in this system, not a bug to engineer around by
# repeatedly enlarging the budget). turn_limit_reached is a legitimate, expected outcome for a
# non-converging agent; Phase 8 measures its rate rather than this constant chasing it away.
MAX_TOOL_LOOP_TURNS = 12
MAX_PAYMENT_RETRIES = 1
_MAX_PAYMENT_ATTEMPTS = MAX_PAYMENT_RETRIES + 1

KNOWN_INJECTIONS = {"stock_conflict", "payment_failure", "missing_webhook", "policy_deny_recovery"}


def _tool_specs_from_fastmcp(fastmcp_tools: list[Any]) -> list[ToolSpec]:
    return [
        ToolSpec(name=t.name, description=t.description or "", input_schema=t.parameters)
        for t in fastmcp_tools
    ]


@dataclass
class SessionResult:
    transaction_id: str
    constraints: BuyerConstraints
    # "order_created" | "policy_denied" | "stock_conflict" | "payment_failed" |
    # "pending_approval" | "no_purchase" | "turn_limit_reached"
    outcome: str
    cart_view: dict | None
    order: dict | None
    turns_used: int
    denial_reason: str | None = None
    injected_failure: str | None = None


class BuyerSessionRunner:
    def __init__(
        self,
        *,
        agent: BuyerAgent,
        buyer_mcp: FastMCP,
        sessions: SessionRegistry,
        catalog: CatalogStore,
        ledger: LedgerStore,
        policy: PolicyService,
        payment: PaymentAdapter,
        simulated_payment_adapter: SimulatedPaymentAdapter | None = None,
    ) -> None:
        self._agent = agent
        self._buyer_mcp = buyer_mcp
        self._sessions = sessions
        self._catalog = catalog
        self._ledger = ledger
        self._policy = policy
        # Wrapping happens here, not in build_payment_stack() — failure injection is a
        # test/demo concern of running a session, never part of the real production stack.
        self._payment = FailureInjectingPaymentAdapter(payment)
        self._simulated_payment_adapter = simulated_payment_adapter

    def _caused_by(self, transaction_id: str) -> list[str]:
        last = self._ledger.last_entry_id(transaction_id)
        return [last] if last else []

    async def run(
        self, transaction_id: str, goal_text: str, inject_failure: str | None = None
    ) -> SessionResult:
        active_injection = inject_failure or os.environ.get("INJECT_FAILURE") or None
        if active_injection is not None and active_injection not in KNOWN_INJECTIONS:
            raise ValueError(
                f"unknown inject_failure {active_injection!r}; expected one of "
                f"{sorted(KNOWN_INJECTIONS)}"
            )
        if active_injection == "missing_webhook" and self._simulated_payment_adapter is None:
            raise ValueError(
                "inject_failure='missing_webhook' requires PAYMENT_MODE=simulated "
                "(no simulated adapter reference was provided to this runner)"
            )

        constraints = self._agent.extract_constraints(goal_text)
        self._ledger.append(
            transaction_id=transaction_id,
            caused_by=[],
            actor=Actor.BUYER_AGENT,
            action_type=ActionType.DECISION,
            input={"goal_text": goal_text},
            output={
                "budget_ceiling_paise": constraints.budget_ceiling_paise,
                "soft_target_paise": constraints.soft_target_paise,
                "category": constraints.category,
                "recipient_context": constraints.recipient_context,
                "must_have": constraints.must_have,
                "deadline": constraints.deadline,
            },
            reasoning_summary="Extracted buyer constraints from stated goal.",
        )

        if active_injection is not None:
            self._ledger.append(
                transaction_id=transaction_id,
                caused_by=self._caused_by(transaction_id),
                actor=Actor.ORCHESTRATOR,
                action_type=ActionType.DECISION,
                input={},
                output={"inject_failure": active_injection},
                machine_reason=f"INJECTED_FAILURE_{active_injection.upper()}",
                human_reason=f"(demo) failure injection active for this session: {active_injection}",
            )
            if active_injection == "payment_failure":
                self._payment.arm_failure(transaction_id)
            elif active_injection == "missing_webhook":
                self._simulated_payment_adapter.suppress_webhook(transaction_id)

        tools = _tool_specs_from_fastmcp(await self._buyer_mcp.list_tools())
        categories = sorted({p.category for p in self._catalog.all()})
        system = self._agent.system_prompt(constraints, categories=categories)
        messages: list[Message] = [
            Message(
                role="user",
                content=(
                    "Find and purchase one suitable item within the stated constraints, then "
                    "confirm checkout."
                ),
            )
        ]

        cart_view: dict | None = None
        order_info: dict | None = None
        denial_reason: str | None = None
        outcome = "no_purchase"
        turns_used = 0
        injection_state: dict[str, Any] = {"stock_conflict_fired": False, "policy_deny_ceiling_paise": None}

        for turn in range(MAX_TOOL_LOOP_TURNS):
            turns_used = turn + 1
            response = self._agent.next_turn(system=system, messages=messages, tools=tools)
            assistant_message = Message(
                role="assistant", content=response.text or None, tool_calls=tuple(response.tool_calls)
            )
            messages.append(assistant_message)

            if not response.tool_calls:
                break

            stop_loop = False
            for tc in response.tool_calls:
                result, gate_outcome = await self._execute_tool_call(
                    transaction_id=transaction_id,
                    constraints=constraints,
                    tool_name=tc.name,
                    tool_input=tc.arguments or {},
                    active_injection=active_injection,
                    injection_state=injection_state,
                )
                messages.append(
                    Message(role="tool", content=json.dumps(result), tool_call_id=tc.id, tool_name=tc.name)
                )
                if gate_outcome in ("denied", "stock_conflict"):
                    # Recoverable: the reason goes back to the agent above, and the loop
                    # keeps going rather than ending the session here.
                    denial_reason = result.get("human_reason")
                    outcome = "policy_denied" if gate_outcome == "denied" else "stock_conflict"
                elif gate_outcome == "pending_approval":
                    denial_reason = result.get("human_reason")
                    outcome = "pending_approval"
                    stop_loop = True
                elif gate_outcome == "payment_failed":
                    denial_reason = result.get("human_reason")
                    outcome = "payment_failed"
                    stop_loop = True
                elif gate_outcome == "order_created":
                    cart_view = result.get("cart")
                    order_info = result.get("order")
                    outcome = "order_created"
                    denial_reason = None
                    stop_loop = True

            if stop_loop:
                break
        else:
            outcome = "turn_limit_reached"

        return SessionResult(
            transaction_id=transaction_id,
            constraints=constraints,
            outcome=outcome,
            cart_view=cart_view,
            order=order_info,
            turns_used=turns_used,
            denial_reason=denial_reason,
            injected_failure=active_injection,
        )

    def _check_stock_conflict(self, cart: Cart) -> tuple[str | None, int]:
        for sku, item in cart.items.items():
            product = self._catalog.get(sku)
            if product is not None and item.quantity > product.stock:
                return sku, product.stock
        return None, 0

    async def _execute_tool_call(
        self,
        *,
        transaction_id: str,
        constraints: BuyerConstraints,
        tool_name: str,
        tool_input: dict,
        active_injection: str | None,
        injection_state: dict[str, Any],
    ) -> tuple[dict, str]:
        # The orchestrator owns which transaction this session is — never the agent's guess.
        # A real LLM has no way to know the session's real transaction_id unless told, and
        # nothing currently tells it (the tool schema requires the field, but the initial
        # prompt never states its value), so it fills in a plausible-looking string of its
        # own. Left unnormalized, the real MCP tool call below would mutate a *different*
        # cart than the one every check in this method reads, silently splitting session
        # state in two. Overriding here makes the mismatch structurally impossible rather
        # than something a better prompt has to prevent.
        tool_input = {**tool_input, "transaction_id": transaction_id}

        if tool_name == "cart.add":
            verdict = self._policy.check(
                actor=Actor.BUYER_AGENT,
                tool_name="cart.add",
                arguments={"product": {"sku": tool_input.get("sku")}},
                state=None,
                transaction_id=transaction_id,
                caused_by=self._caused_by(transaction_id),
            )
            if verdict.outcome == PolicyVerdict.DENY:
                return {"error": "policy_denied", "human_reason": verdict.human_reason}, "denied"

        if tool_name == "checkout.confirm":
            cart = self._sessions.get_or_create(transaction_id)

            if (
                active_injection == "stock_conflict"
                and not injection_state["stock_conflict_fired"]
                and cart.items
            ):
                sku = next(iter(cart.items))
                self._catalog.set_stock(sku, 0)
                injection_state["stock_conflict_fired"] = True
                self._ledger.append(
                    transaction_id=transaction_id,
                    caused_by=self._caused_by(transaction_id),
                    actor=Actor.ORCHESTRATOR,
                    action_type=ActionType.DECISION,
                    input={"sku": sku},
                    output={},
                    machine_reason="INJECTED_STOCK_CONFLICT",
                    human_reason=f"(demo) simulated {sku} selling out between selection and checkout",
                )

            conflict_sku, remaining = self._check_stock_conflict(cart)
            if conflict_sku is not None:
                product = self._catalog.get(conflict_sku)
                requested = cart.items[conflict_sku].quantity
                human_reason = (
                    f"{product.name if product else conflict_sku} ({conflict_sku}) only has "
                    f"{remaining} left in stock; you selected {requested}"
                )
                self._ledger.append(
                    transaction_id=transaction_id,
                    caused_by=self._caused_by(transaction_id),
                    actor=Actor.ORCHESTRATOR,
                    action_type=ActionType.DECISION,
                    input={"sku": conflict_sku, "requested_quantity": requested},
                    output={"remaining_stock": remaining},
                    machine_reason="STOCK_CONFLICT",
                    human_reason=human_reason,
                )
                return {
                    "error": "stock_conflict",
                    "sku": conflict_sku,
                    "remaining_stock": remaining,
                    "requested_quantity": requested,
                    "human_reason": human_reason,
                }, "stock_conflict"

            effective_budget_ceiling_paise = constraints.budget_ceiling_paise
            if active_injection == "policy_deny_recovery":
                if injection_state["policy_deny_ceiling_paise"] is None and cart.total_paise > 0:
                    injected_ceiling = cart.total_paise // 2
                    injection_state["policy_deny_ceiling_paise"] = injected_ceiling
                    self._ledger.append(
                        transaction_id=transaction_id,
                        caused_by=self._caused_by(transaction_id),
                        actor=Actor.ORCHESTRATOR,
                        action_type=ActionType.DECISION,
                        input={},
                        output={"effective_budget_ceiling_paise": injected_ceiling},
                        machine_reason="INJECTED_POLICY_DENY",
                        human_reason=(
                            "(demo) temporarily capping the effective budget ceiling at "
                            f"{Money(injected_ceiling).format_inr()} to force a real policy "
                            "denial — this stays in effect until the cart fits under it"
                        ),
                    )
                if injection_state["policy_deny_ceiling_paise"] is not None:
                    effective_budget_ceiling_paise = injection_state["policy_deny_ceiling_paise"]

            verdict = self._policy.check(
                actor=Actor.BUYER_AGENT,
                tool_name="checkout.confirm",
                arguments={"cart": cart.to_view()},
                state={"session": {"buyer_budget_paise": effective_budget_ceiling_paise}},
                transaction_id=transaction_id,
                caused_by=self._caused_by(transaction_id),
            )
            if verdict.outcome == PolicyVerdict.DENY:
                result: dict = {"error": "policy_denied", "human_reason": verdict.human_reason}
                if verdict.machine_reason == "BUDGET_CEILING_EXCEEDED":
                    # A generic "adapt to errors" instruction in the system prompt proved too
                    # weak in practice (observed live: the agent repeatedly added a second item
                    # on top instead of removing the first, making the total worse) — spelling
                    # out the exact numbers and the required action right where the failure
                    # happens is far more reliably followed than an instruction the model has
                    # to recall from much earlier in a growing conversation.
                    result["cart_total_paise"] = cart.total_paise
                    result["budget_ceiling_paise"] = effective_budget_ceiling_paise
                    result["hint"] = (
                        "Use cart.remove to take the current item(s) out of the cart until the "
                        "total is at or under the budget ceiling, then retry checkout.confirm. "
                        "Do not add another item on top of what's already there."
                    )
                return result, "denied"
            if verdict.outcome == PolicyVerdict.REQUIRE_APPROVAL:
                pending = {"status": "pending_approval", "human_reason": verdict.human_reason}
                return pending, "pending_approval"

        try:
            call_result = await self._buyer_mcp.call_tool(tool_name, tool_input)
        except Exception as e:  # noqa: BLE001 — surfaced to the agent as a tool error, not raised
            return {"error": str(e)}, "error"

        output = dict(call_result.structured_content or {})

        if tool_name == "checkout.confirm":
            cart_view = output.get("cart", {})
            amount_paise = cart_view.get("total_paise", 0)
            order, payment_error = self._create_order_with_bounded_retry(
                transaction_id=transaction_id, amount_paise=amount_paise
            )
            if order is None:
                return {"error": "payment_failed", "human_reason": payment_error}, "payment_failed"
            order_output = {
                "order_id": order.order_id,
                "status": order.status.value,
                "receipt": order.receipt,
            }
            output["order"] = order_output
            return output, "order_created"

        return output, "ok"

    def _create_order_with_bounded_retry(
        self, *, transaction_id: str, amount_paise: int
    ) -> tuple[OrderRecord | None, str | None]:
        last_error: str | None = None
        for attempt in range(1, _MAX_PAYMENT_ATTEMPTS + 1):
            try:
                order = self._payment.create_order(
                    transaction_id=transaction_id,
                    amount_paise=amount_paise,
                    policy_version=self._policy.policy_version,
                )
            except PaymentRetryableError as e:
                last_error = str(e)
                will_retry = attempt < _MAX_PAYMENT_ATTEMPTS
                self._ledger.append(
                    transaction_id=transaction_id,
                    caused_by=self._caused_by(transaction_id),
                    actor=Actor.PAYMENT_LAYER,
                    action_type=ActionType.PAYMENT_CALL,
                    input={"amount_paise": amount_paise, "attempt": attempt},
                    output={},
                    machine_reason="PAYMENT_RETRY" if will_retry else "PAYMENT_ABORTED",
                    human_reason=(
                        f"payment attempt {attempt} failed ({last_error}); retrying with the "
                        "same idempotency key"
                        if will_retry
                        else (
                            f"payment failed after {attempt} attempts and the retry budget is "
                            f"exhausted: {last_error}"
                        )
                    ),
                )
                continue
            except PaymentFatalError as e:
                last_error = str(e)
                self._ledger.append(
                    transaction_id=transaction_id,
                    caused_by=self._caused_by(transaction_id),
                    actor=Actor.PAYMENT_LAYER,
                    action_type=ActionType.PAYMENT_CALL,
                    input={"amount_paise": amount_paise, "attempt": attempt},
                    output={},
                    machine_reason="PAYMENT_FATAL",
                    human_reason=f"payment failed with a non-retryable error: {last_error}",
                )
                return None, last_error
            else:
                self._ledger.append(
                    transaction_id=transaction_id,
                    caused_by=self._caused_by(transaction_id),
                    actor=Actor.PAYMENT_LAYER,
                    action_type=ActionType.PAYMENT_CALL,
                    input={"amount_paise": amount_paise, "attempt": attempt},
                    output={"order_id": order.order_id, "status": order.status.value},
                    reasoning_summary=f"created order {order.order_id} for {amount_paise} paise",
                )
                return order, None
        return None, last_error
