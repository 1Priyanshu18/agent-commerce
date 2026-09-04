from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from agent_commerce.core.config import load_config
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments import build_payment_stack


async def _reconciliation_poll_loop(app: FastAPI) -> None:
    config = load_config()
    while True:
        await asyncio.sleep(config.reconcile_poll_interval_seconds)
        app.state.payment_stack.reconciler.reconcile_all_pending()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.ledger = LedgerStore(f"{config.data_dir}/ledger.db")
    app.state.payment_stack = build_payment_stack(config, ledger=app.state.ledger, data_dir=config.data_dir)

    poll_task = asyncio.create_task(_reconciliation_poll_loop(app))
    try:
        yield
    finally:
        poll_task.cancel()


app = FastAPI(title="Agent Commerce", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    config = load_config()
    return {"status": "ok", "env": config.app_env}


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> dict:
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    result = app.state.payment_stack.webhook_handler.handle(raw_body=raw_body, signature=signature)
    if not result.accepted:
        raise HTTPException(status_code=400, detail=result.reason)
    return {"status": "ok", "is_new": result.is_new}
