"""The session state machine: drives the buyer agent's tool-use loop against the real buyer
MCP server, gating cart.add and checkout.confirm through the policy engine before they're
allowed to execute, and calling the payment layer on a successful checkout.

This is the only module in the codebase that calls policy/ and payments/ — the buyer agent
only ever proposes tool calls; this runner decides whether they're allowed to happen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.agents.buyer.constraints import BuyerConstraints
from agent_commerce.core.llm import Message, ToolSpec
from agent_commerce.ledger.models import ActionType, Actor, PolicyVerdict
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments.adapter import PaymentAdapter
from agent_commerce.policy.service import PolicyService

from .session import SessionRegistry

MAX_TOOL_LOOP_TURNS = 8


def _tool_specs_from_fastmcp(fastmcp_tools: list[Any]) -> list[ToolSpec]:
    return [
        ToolSpec(name=t.name, description=t.description or "", input_schema=t.parameters)
        for t in fastmcp_tools
    ]


@dataclass
class SessionResult:
    transaction_id: str
    constraints: BuyerConstraints
    # "order_created" | "policy_denied" | "pending_approval" | "no_purchase" | "turn_limit_reached"
    outcome: str
    cart_view: dict | None
    order: dict | None
    turns_used: int
    denial_reason: str | None = None


class BuyerSessionRunner:
    def __init__(
        self,
        *,
        agent: BuyerAgent,
        buyer_mcp: FastMCP,
        sessions: SessionRegistry,
        ledger: LedgerStore,
        policy: PolicyService,
        payment: PaymentAdapter,
    ) -> None:
        self._agent = agent
        self._buyer_mcp = buyer_mcp
        self._sessions = sessions
        self._ledger = ledger
        self._policy = policy
        self._payment = payment

    def _caused_by(self, transaction_id: str) -> list[str]:
        last = self._ledger.last_entry_id(transaction_id)
        return [last] if last else []

    async def run(self, transaction_id: str, goal_text: str) -> SessionResult:
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

        tools = _tool_specs_from_fastmcp(await self._buyer_mcp.list_tools())
        system = self._agent.system_prompt(constraints)
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
                )
                messages.append(
                    Message(role="tool", content=json.dumps(result), tool_call_id=tc.id, tool_name=tc.name)
                )
                if gate_outcome == "denied":
                    denial_reason = result.get("human_reason")
                    outcome = "policy_denied"
                    stop_loop = True
                elif gate_outcome == "pending_approval":
                    denial_reason = result.get("human_reason")
                    outcome = "pending_approval"
                    stop_loop = True
                elif gate_outcome == "order_created":
                    cart_view = result.get("cart")
                    order_info = result.get("order")
                    outcome = "order_created"
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
        )

    async def _execute_tool_call(
        self, *, transaction_id: str, constraints: BuyerConstraints, tool_name: str, tool_input: dict
    ) -> tuple[dict, str]:
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
            verdict = self._policy.check(
                actor=Actor.BUYER_AGENT,
                tool_name="checkout.confirm",
                arguments={"cart": cart.to_view()},
                state={"session": {"buyer_budget_paise": constraints.budget_ceiling_paise}},
                transaction_id=transaction_id,
                caused_by=self._caused_by(transaction_id),
            )
            if verdict.outcome == PolicyVerdict.DENY:
                return {"error": "policy_denied", "human_reason": verdict.human_reason}, "denied"
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
            order = self._payment.create_order(
                transaction_id=transaction_id,
                amount_paise=amount_paise,
                policy_version=self._policy.policy_version,
            )
            order_output = {
                "order_id": order.order_id,
                "status": order.status.value,
                "receipt": order.receipt,
            }
            self._ledger.append(
                transaction_id=transaction_id,
                caused_by=self._caused_by(transaction_id),
                actor=Actor.PAYMENT_LAYER,
                action_type=ActionType.PAYMENT_CALL,
                input={"amount_paise": amount_paise},
                output=order_output,
                reasoning_summary=f"created order {order.order_id} for {amount_paise} paise",
            )
            output["order"] = order_output
            return output, "order_created"

        return output, "ok"
