"""Webhook receipt: verify X-Razorpay-Signature (HMAC-SHA256 over the RAW body) before
parsing anything, store the raw payload, and record it idempotently on (event, payment_id).
Reconciliation is triggered via an injected callback (not a direct import of Reconciler) to
keep this module able to verify and store a webhook with no dependency on how reconciliation
itself is implemented.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import razorpay
from razorpay.errors import SignatureVerificationError

from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore

from .webhook_store import WebhookRecord, WebhookStore


class UnwiredReconciliationCallbackError(RuntimeError):
    """A new webhook arrived but on_new_webhook was never wired, so reconciliation would
    otherwise silently never fire and the order would sit at "created" forever. A handler
    that genuinely doesn't need reconciliation should pass an explicit no-op callback.
    """


@dataclass(frozen=True)
class WebhookHandlingResult:
    accepted: bool
    is_new: bool
    reason: str | None
    record: WebhookRecord | None


class WebhookHandler:
    def __init__(
        self,
        *,
        webhook_secret: str,
        store: WebhookStore,
        ledger: LedgerStore,
        on_new_webhook: Callable[[str], None] | None = None,
        utility: razorpay.Utility | None = None,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._store = store
        self._ledger = ledger
        # Public and settable after construction: composing the full payment stack has a
        # circular dependency (the simulated adapter needs this handler; this handler's
        # callback needs the reconciler; the reconciler needs the adapter) that's broken by
        # wiring the callback in once everything else exists. See payments/__init__.py.
        self.on_new_webhook = on_new_webhook
        self._utility = utility or razorpay.Utility()

    def handle(self, *, raw_body: bytes, signature: str) -> WebhookHandlingResult:
        raw_text = raw_body.decode("utf-8")
        try:
            self._utility.verify_webhook_signature(raw_text, signature, self._webhook_secret)
        except SignatureVerificationError:
            self._ledger.append(
                transaction_id="unknown",
                caused_by=[],
                actor=Actor.PAYMENT_LAYER,
                action_type=ActionType.WEBHOOK,
                input={"signature_provided": bool(signature)},
                output={},
                machine_reason="INVALID_WEBHOOK_SIGNATURE",
                human_reason="a webhook arrived with a signature that did not verify against the raw body",
            )
            return WebhookHandlingResult(
                accepted=False, is_new=False, reason="invalid signature", record=None
            )

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return WebhookHandlingResult(
                accepted=False, is_new=False, reason="invalid JSON body", record=None
            )

        event = data.get("event", "")
        payment_entity = data.get("payload", {}).get("payment", {}).get("entity", {})
        payment_id = payment_entity.get("id", "")
        order_id = payment_entity.get("order_id")

        if not event or not payment_id:
            return WebhookHandlingResult(
                accepted=False, is_new=False, reason="missing event or payment id", record=None
            )

        record, is_new = self._store.save_if_new(
            event=event, payment_id=payment_id, order_id=order_id, raw_body=raw_text
        )

        transaction_id = payment_entity.get("notes", {}).get("transaction_id") or order_id or "unknown"
        self._ledger.append(
            transaction_id=transaction_id,
            caused_by=[],
            actor=Actor.PAYMENT_LAYER,
            action_type=ActionType.WEBHOOK,
            input={"event": event, "payment_id": payment_id, "order_id": order_id},
            output={"is_new": is_new, "webhook_id": record.webhook_id},
            reasoning_summary=f"received {'new' if is_new else 'duplicate'} webhook: {event}",
        )

        if is_new and order_id:
            if self.on_new_webhook is None:
                raise UnwiredReconciliationCallbackError(
                    f"a new webhook for order {order_id} arrived but no on_new_webhook "
                    "callback is wired — reconciliation was never triggered"
                )
            self.on_new_webhook(order_id)

        return WebhookHandlingResult(accepted=True, is_new=is_new, reason=None, record=record)
