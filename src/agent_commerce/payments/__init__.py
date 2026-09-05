"""Payment layer composition. build_payment_stack() is the one place PAYMENT_MODE is read —
everything downstream (the orchestrator, the reconciler, the webhook endpoint) depends only
on the PaymentAdapter protocol and never branches on which concrete adapter is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_commerce.core.config import Config
from agent_commerce.ledger.store import LedgerStore

from .adapter import PaymentAdapter
from .idempotency import IdempotencyStore
from .idempotent_adapter import IdempotentPaymentAdapter
from .order_store import OrderStore
from .reconciler import Reconciler
from .recording_adapter import RecordingPaymentAdapter
from .webhook import WebhookHandler
from .webhook_store import WebhookStore

if TYPE_CHECKING:
    from .simulated import SimulatedPaymentAdapter


@dataclass
class PaymentStack:
    adapter: PaymentAdapter
    webhook_handler: WebhookHandler
    reconciler: Reconciler
    order_store: OrderStore
    # Only set when PAYMENT_MODE=simulated. Exposed as the concrete type (not just the
    # abstract PaymentAdapter) so callers that need to reach past the idempotent/recording
    # wrapping — e.g. the demo script's missing_webhook failure injection — have something to
    # reach for. Reaching for this outside of test/demo failure injection is a smell: normal
    # code should only ever depend on the PaymentAdapter protocol.
    simulated_adapter: SimulatedPaymentAdapter | None = None


def build_payment_stack(
    config: Config, *, ledger: LedgerStore, data_dir: Path | str = "data"
) -> PaymentStack:
    data_dir = Path(data_dir)
    order_store = OrderStore(data_dir / "orders.db")
    webhook_store = WebhookStore(data_dir / "webhooks.db")
    idempotency_store = IdempotencyStore(data_dir / "idempotency.db")

    # on_new_webhook is wired in below, once the reconciler exists — see WebhookHandler's
    # docstring note on the circular dependency this breaks.
    webhook_handler = WebhookHandler(
        webhook_secret=config.razorpay_webhook_secret, store=webhook_store, ledger=ledger
    )

    simulated_adapter: SimulatedPaymentAdapter | None = None
    if config.payment_mode == "live_test":
        from .live_test import RazorpayLiveTestAdapter

        raw_adapter: PaymentAdapter = RazorpayLiveTestAdapter(
            key_id=config.razorpay_key_id, key_secret=config.razorpay_key_secret
        )
    elif config.payment_mode == "simulated":
        from .simulated import SimulatedPaymentAdapter as _SimulatedPaymentAdapter

        simulated_adapter = _SimulatedPaymentAdapter(
            webhook_secret=config.razorpay_webhook_secret,
            webhook_handler=webhook_handler,
            order_store=order_store,
        )
        raw_adapter = simulated_adapter
    else:
        raise ValueError(
            f"unknown PAYMENT_MODE: {config.payment_mode!r} (expected 'live_test' or 'simulated')"
        )

    # The reconciler only ever calls fetch_payments() — never create_order() — so it can take
    # the raw adapter directly, with no idempotency/recording wrapping needed.
    reconciler = Reconciler(
        adapter=raw_adapter,
        order_store=order_store,
        webhook_store=webhook_store,
        ledger=ledger,
        pending_reconciliation_threshold_seconds=config.pending_reconciliation_threshold_seconds,
    )
    webhook_handler.on_new_webhook = reconciler.reconcile

    adapter: PaymentAdapter = RecordingPaymentAdapter(
        IdempotentPaymentAdapter(raw_adapter, idempotency_store), order_store
    )

    return PaymentStack(
        adapter=adapter,
        webhook_handler=webhook_handler,
        reconciler=reconciler,
        order_store=order_store,
        simulated_adapter=simulated_adapter,
    )
