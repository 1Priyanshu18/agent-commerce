from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization: sorted keys, fixed separators, no whitespace drift.

    Required for hash chaining — the same logical entry must always serialize to the same
    bytes, regardless of dict insertion order.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
