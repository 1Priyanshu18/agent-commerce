"""Role-separation authorization: (actor, tool_name) against a fixed allowlist.

The buyer and merchant tool surfaces live on two separate FastMCP server instances, so an
LLM connected to one structurally cannot even see the other's tools — that's the primary
defense. This module is the defense-in-depth layer behind it: every tool handler on both
servers calls authorize() with its own hardcoded actor identity as its first line, so a
mismatch (e.g. from a future refactor bug, or a client that bypasses the intended server
boundary) is still caught and written to the ledger, never silently allowed or silently
dropped.
"""

from __future__ import annotations

from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore

ROLE_ALLOWED_TOOLS: dict[Actor, frozenset[str]] = {
    Actor.BUYER_AGENT: frozenset(
        {
            "catalog.search",
            "catalog.get_details",
            "cart.add",
            "cart.remove",
            "cart.view",
            "upsell.respond",
            "checkout.confirm",
        }
    ),
    Actor.UPSELL_AGENT: frozenset(
        {
            "cart.read_at_checkout",
            "upsell.make_offer",
            "upsell.no_offer",
        }
    ),
}


class RoleViolationError(PermissionError):
    def __init__(self, actor: Actor, tool_name: str) -> None:
        self.actor = actor
        self.tool_name = tool_name
        super().__init__(f"{actor.value} is not permitted to call '{tool_name}'")


def is_permitted(actor: Actor, tool_name: str) -> bool:
    return tool_name in ROLE_ALLOWED_TOOLS.get(actor, frozenset())


def authorize(
    actor: Actor,
    tool_name: str,
    ledger: LedgerStore,
    *,
    transaction_id: str,
    caused_by: list[str],
) -> None:
    """Raises RoleViolationError and writes a role_violation ledger entry if actor is not
    permitted to call tool_name. No-op (no ledger write) when permitted.
    """
    if is_permitted(actor, tool_name):
        return

    ledger.append(
        transaction_id=transaction_id,
        caused_by=caused_by,
        actor=actor,
        action_type=ActionType.ROLE_VIOLATION,
        input={"attempted_tool": tool_name},
        output={},
        machine_reason="ROLE_NOT_PERMITTED",
        human_reason=(
            f"{actor.value} attempted to call '{tool_name}', which is outside its permitted tool set"
        ),
    )
    raise RoleViolationError(actor, tool_name)
