from __future__ import annotations

from agent_commerce.ledger.models import ActionType, Actor, PolicyVerdict
from agent_commerce.ledger.store import LedgerStore

from .approvals import ApprovalStore
from .engine import PolicyEngine, Verdict


class PolicyService:
    """Wraps the pure PolicyEngine with the ledger write every check requires, and routes
    REQUIRE_APPROVAL verdicts to their destination (the approvals table).
    """

    def __init__(
        self,
        engine: PolicyEngine,
        ledger: LedgerStore,
        approvals: ApprovalStore | None = None,
    ) -> None:
        self._engine = engine
        self._ledger = ledger
        self._approvals = approvals

    def check(
        self,
        *,
        actor: Actor,
        tool_name: str,
        arguments: dict,
        state: dict | None,
        transaction_id: str,
        caused_by: list[str],
    ) -> Verdict:
        verdict = self._engine.evaluate(actor, tool_name, arguments, state)

        entry = self._ledger.append(
            transaction_id=transaction_id,
            caused_by=caused_by,
            actor=Actor.POLICY_ENGINE,
            action_type=ActionType.POLICY_CHECK,
            input={"tool_name": tool_name, "arguments": arguments},
            output={
                "outcome": verdict.outcome.value,
                "matched_rule_ids": verdict.matched_rule_ids,
                "adjustments": [a.to_dict() for a in verdict.adjustments],
            },
            reasoning_summary=verdict.reasoning_summary,
            machine_reason=verdict.machine_reason,
            human_reason=verdict.human_reason,
            policy_verdict=verdict.outcome,
            policy_version=verdict.policy_version,
        )

        if self._approvals is not None and verdict.outcome == PolicyVerdict.REQUIRE_APPROVAL:
            self._approvals.create(
                transaction_id=transaction_id,
                ledger_entry_id=entry.entry_id,
                tool_name=tool_name,
                arguments=arguments,
                timeout_seconds=self._engine.approval_timeout_seconds,
            )

        return verdict
