"""Passphrase check for the Live run tab. Pure logic, no Streamlit import."""

from __future__ import annotations

import hmac


def check_passphrase(entered: str, expected: str) -> bool:
    # Fails closed: an unset expected passphrase means the gate can never open, not that
    # it's disabled.
    if not expected:
        return False
    return hmac.compare_digest(entered, expected)
