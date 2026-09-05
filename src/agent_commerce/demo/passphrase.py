"""Passphrase check for the Streamlit demo app's Live run tab (Phase 9,
docs/PHASE_9_SPEC.md). Pure logic, no Streamlit import — kept here so it's testable without
a running app and importable from app.py, which stays a thin view layer.
"""

from __future__ import annotations

import hmac


def check_passphrase(entered: str, expected: str) -> bool:
    """Constant-time comparison. Fails closed: an unset (empty) expected passphrase means the
    gate can never be opened, not that it's disabled — the deployed Space always sets
    DEMO_PASSPHRASE, and a missing env var should not silently turn into "no gate".
    """
    if not expected:
        return False
    return hmac.compare_digest(entered, expected)
