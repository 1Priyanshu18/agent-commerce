import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from agent_commerce.api.main import app
from agent_commerce.core.config import Config
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments import build_payment_stack

SECRET = "test_webhook_secret"


def _config(tmp_path) -> Config:
    return Config(
        app_env="test",
        log_level="INFO",
        llm_provider="gemini",
        gemini_api_key="",
        gemini_model="gemini-3.6-flash",
        groq_api_key="",
        groq_model="llama-3.3-70b-versatile",
        anthropic_api_key="",
        anthropic_model="claude-haiku-4-5-20251001",
        llm_max_calls_per_run=200,
        payment_mode="simulated",
        razorpay_key_id="",
        razorpay_key_secret="",
        razorpay_webhook_secret=SECRET,
        reconcile_poll_interval_seconds=30,
        pending_reconciliation_threshold_seconds=30,
        data_dir=str(tmp_path),
    )


def _client(tmp_path) -> TestClient:
    # TestClient(app) without `with` never runs lifespan (matches test_health.py), so setting
    # app.state directly here is what the running app would otherwise do at startup.
    config = _config(tmp_path)
    ledger = LedgerStore(tmp_path / "ledger.db")
    app.state.ledger = ledger
    app.state.payment_stack = build_payment_stack(config, ledger=ledger, data_dir=tmp_path)
    return TestClient(app)


def _signed_body(*, event: str = "payment.captured", payment_id: str = "pay_1", order_id: str = "order_1"):
    payload = {
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 100000,
                    "status": "captured",
                    "notes": {"transaction_id": "txn_1", "policy_version": "v1"},
                }
            }
        },
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, signature


def test_webhook_endpoint_accepts_valid_signature(tmp_path) -> None:
    client = _client(tmp_path)
    body, signature = _signed_body()

    response = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": signature}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "is_new": True}


def test_webhook_endpoint_rejects_invalid_signature(tmp_path) -> None:
    client = _client(tmp_path)
    body, _ = _signed_body()

    response = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": "not-valid"}
    )

    assert response.status_code == 400


def test_webhook_endpoint_duplicate_delivery_returns_is_new_false(tmp_path) -> None:
    client = _client(tmp_path)
    body, signature = _signed_body()
    headers = {"X-Razorpay-Signature": signature}

    client.post("/webhooks/razorpay", content=body, headers=headers)
    response = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.json() == {"status": "ok", "is_new": False}


def test_webhook_endpoint_reaches_the_ledger(tmp_path) -> None:
    client = _client(tmp_path)
    body, signature = _signed_body()

    client.post("/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": signature})

    entries = app.state.ledger.entries_for_transaction("txn_1")
    assert len(entries) >= 1
