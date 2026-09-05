from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    app_env: str
    log_level: str
    llm_provider: str
    gemini_api_key: str
    gemini_model: str
    groq_api_key: str
    groq_model: str
    anthropic_api_key: str
    anthropic_model: str
    llm_max_calls_per_run: int
    payment_mode: str
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    reconcile_poll_interval_seconds: int
    pending_reconciliation_threshold_seconds: int
    data_dir: str
    demo_passphrase: str
    demo_max_calls_per_session: int
    demo_daily_call_budget: int


@lru_cache
def load_config() -> Config:
    return Config(
        app_env=os.environ.get("APP_ENV", "development"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        llm_provider=os.environ.get("LLM_PROVIDER", "groq"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        groq_api_key=os.environ.get("GROQ_API_KEY", ""),
        groq_model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        llm_max_calls_per_run=int(os.environ.get("LLM_MAX_CALLS_PER_RUN", "200")),
        payment_mode=os.environ.get("PAYMENT_MODE", "simulated"),
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", ""),
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET", ""),
        razorpay_webhook_secret=os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""),
        reconcile_poll_interval_seconds=int(os.environ.get("RECONCILE_POLL_INTERVAL_SECONDS", "30")),
        pending_reconciliation_threshold_seconds=int(
            os.environ.get("PENDING_RECONCILIATION_THRESHOLD_SECONDS", "30")
        ),
        data_dir=os.environ.get("DATA_DIR", "data"),
        demo_passphrase=os.environ.get("DEMO_PASSPHRASE", ""),
        demo_max_calls_per_session=int(os.environ.get("DEMO_MAX_CALLS_PER_SESSION", "20")),
        demo_daily_call_budget=int(os.environ.get("DEMO_DAILY_CALL_BUDGET", "50")),
    )
