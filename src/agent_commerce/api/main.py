from __future__ import annotations

from fastapi import FastAPI

from agent_commerce.core.config import load_config

app = FastAPI(title="Agent Commerce")


@app.get("/health")
def health() -> dict:
    config = load_config()
    return {"status": "ok", "env": config.app_env}
