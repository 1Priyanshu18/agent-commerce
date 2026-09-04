"""Normalization tests for the live_test adapter against a fake razorpay.Client — recorded,
fixture-shaped payloads (matching the real API responses fetched from Razorpay's docs), no
live network calls. This file must never construct a real razorpay.Client with real
credentials or make a real HTTP request.
"""

from __future__ import annotations

import pytest
import razorpay.errors

from agent_commerce.payments.adapter import PaymentFatalError, PaymentRetryableError
from agent_commerce.payments.live_test import RazorpayLiveTestAdapter
from agent_commerce.payments.models import OrderStatus, PaymentStatus


class _FakeOrderResource:
    def __init__(self, create_response=None, payments_response=None) -> None:
        self._create_response = create_response
        self._payments_response = payments_response
        self.create_calls: list[dict] = []
        self.payments_calls: list[str] = []

    def create(self, data=None, **kwargs):
        self.create_calls.append(data)
        if isinstance(self._create_response, Exception):
            raise self._create_response
        return self._create_response

    def payments(self, order_id, data=None, **kwargs):
        self.payments_calls.append(order_id)
        if isinstance(self._payments_response, Exception):
            raise self._payments_response
        return self._payments_response


class _FakeRazorpayClient:
    def __init__(self, order_resource: _FakeOrderResource) -> None:
        self.order = order_resource


# Real example response shape confirmed from Razorpay's official docs.
_ORDER_CREATE_RESPONSE = {
    "id": "order_RB58MiP5SPFYyM",
    "entity": "order",
    "amount": 100000,
    "amount_paid": 0,
    "amount_due": 100000,
    "currency": "INR",
    "receipt": "txn_abc:1",
    "status": "created",
    "attempts": 0,
    "notes": {"transaction_id": "txn_abc", "policy_version": "v1"},
    "created_at": 1756455561,
    "offer_id": None,
}

# Real example response shape confirmed from Razorpay's official docs (fetch payments).
_FETCH_PAYMENTS_RESPONSE = {
    "entity": "collection",
    "count": 2,
    "items": [
        {
            "id": "pay_N8FUmetkCE2hZP",
            "entity": "payment",
            "amount": 100000,
            "currency": "INR",
            "status": "failed",
            "order_id": "order_RB58MiP5SPFYyM",
            "method": "upi",
            "captured": False,
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Payment was unsuccessful due to a temporary issue.",
        },
        {
            "id": "pay_N8FVRD1DzYzBh1",
            "entity": "payment",
            "amount": 100000,
            "currency": "INR",
            "status": "captured",
            "order_id": "order_RB58MiP5SPFYyM",
            "method": "upi",
            "captured": True,
            "error_code": None,
            "error_description": None,
        },
    ],
}


def test_create_order_normalizes_response() -> None:
    order_resource = _FakeOrderResource(create_response=_ORDER_CREATE_RESPONSE)
    adapter = RazorpayLiveTestAdapter(key_id="x", key_secret="y", client=_FakeRazorpayClient(order_resource))

    order = adapter.create_order(transaction_id="txn_abc", amount_paise=100000, policy_version="v1")

    assert order.order_id == "order_RB58MiP5SPFYyM"
    assert order.amount_paise == 100000
    assert order.currency == "INR"
    assert order.status == OrderStatus.CREATED
    assert order.notes == {"transaction_id": "txn_abc", "policy_version": "v1"}


def test_create_order_sends_receipt_as_idempotency_key_and_notes() -> None:
    order_resource = _FakeOrderResource(create_response=_ORDER_CREATE_RESPONSE)
    adapter = RazorpayLiveTestAdapter(key_id="x", key_secret="y", client=_FakeRazorpayClient(order_resource))

    adapter.create_order(transaction_id="txn_abc", amount_paise=100000, policy_version="v1", attempt_no=2)

    sent = order_resource.create_calls[0]
    assert sent["receipt"] == "txn_abc:2"
    assert sent["currency"] == "INR"
    assert sent["notes"] == {"transaction_id": "txn_abc", "policy_version": "v1"}


def test_fetch_payments_normalizes_response() -> None:
    order_resource = _FakeOrderResource(payments_response=_FETCH_PAYMENTS_RESPONSE)
    adapter = RazorpayLiveTestAdapter(key_id="x", key_secret="y", client=_FakeRazorpayClient(order_resource))

    payments = adapter.fetch_payments("order_RB58MiP5SPFYyM")

    assert len(payments) == 2
    assert payments[0].status == PaymentStatus.FAILED
    assert payments[0].error_code == "BAD_REQUEST_ERROR"
    assert payments[1].status == PaymentStatus.CAPTURED
    assert payments[1].captured is True


def test_bad_request_error_is_fatal() -> None:
    import httpx

    req = httpx.Request("POST", "https://api.razorpay.com/v1/orders")
    resp = httpx.Response(400, request=req)
    error = razorpay.errors.BadRequestError("duplicate receipt", response=resp, body=None)
    order_resource = _FakeOrderResource(create_response=error)
    adapter = RazorpayLiveTestAdapter(key_id="x", key_secret="y", client=_FakeRazorpayClient(order_resource))

    with pytest.raises(PaymentFatalError):
        adapter.create_order(transaction_id="txn_abc", amount_paise=100000, policy_version="v1")


def test_server_error_is_retryable() -> None:
    import httpx

    req = httpx.Request("POST", "https://api.razorpay.com/v1/orders")
    resp = httpx.Response(500, request=req)
    error = razorpay.errors.ServerError("internal error", response=resp, body=None)
    order_resource = _FakeOrderResource(create_response=error)
    adapter = RazorpayLiveTestAdapter(key_id="x", key_secret="y", client=_FakeRazorpayClient(order_resource))

    with pytest.raises(PaymentRetryableError):
        adapter.create_order(transaction_id="txn_abc", amount_paise=100000, policy_version="v1")


def test_fetch_payments_server_error_is_retryable() -> None:
    import httpx

    req = httpx.Request("GET", "https://api.razorpay.com/v1/orders/order_1/payments")
    resp = httpx.Response(502, request=req)
    error = razorpay.errors.GatewayError("bad gateway", response=resp, body=None)
    order_resource = _FakeOrderResource(payments_response=error)
    adapter = RazorpayLiveTestAdapter(key_id="x", key_secret="y", client=_FakeRazorpayClient(order_resource))

    with pytest.raises(PaymentRetryableError):
        adapter.fetch_payments("order_1")
