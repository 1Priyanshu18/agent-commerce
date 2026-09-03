from __future__ import annotations

import uuid


def generate_id(prefix: str) -> str:
    """A prefixed, globally-unique identifier, e.g. generate_id("txn") -> 'txn_3f9c...'."""
    return f"{prefix}_{uuid.uuid4().hex}"
