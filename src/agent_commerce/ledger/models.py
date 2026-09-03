from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Actor(StrEnum):
    BUYER_AGENT = "buyer_agent"
    UPSELL_AGENT = "upsell_agent"
    POLICY_ENGINE = "policy_engine"
    PAYMENT_LAYER = "payment_layer"
    ORCHESTRATOR = "orchestrator"


class ActionType(StrEnum):
    SEARCH = "search"
    SELECT = "select"
    OFFER = "offer"
    DECISION = "decision"
    POLICY_CHECK = "policy_check"
    PAYMENT_CALL = "payment_call"
    WEBHOOK = "webhook"
    RECONCILIATION = "reconciliation"
    ROLE_VIOLATION = "role_violation"
    PARSE_FAILURE = "parse_failure"


class PolicyVerdict(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    TRANSFORM = "TRANSFORM"


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    entry_id: str
    transaction_id: str
    timestamp: str
    caused_by: list[str]
    actor: Actor
    action_type: ActionType
    input: dict
    output: dict
    reasoning_summary: str | None
    machine_reason: str | None
    human_reason: str | None
    policy_verdict: PolicyVerdict | None
    policy_version: str | None
    resulting_state: dict | None
    prev_hash: str
    entry_hash: str
