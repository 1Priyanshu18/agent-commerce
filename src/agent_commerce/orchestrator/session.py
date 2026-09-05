"""In-memory transaction_id -> Cart registry shared by both MCP servers, so a cart mutated
via the buyer server is visible to the merchant server's read-only projection at checkout.
"""

from __future__ import annotations

import threading

from agent_commerce.cart.models import Cart


class SessionRegistry:
    def __init__(self) -> None:
        self._carts: dict[str, Cart] = {}
        self._lock = threading.Lock()

    def get_or_create(self, transaction_id: str) -> Cart:
        with self._lock:
            cart = self._carts.get(transaction_id)
            if cart is None:
                cart = Cart(transaction_id=transaction_id)
                self._carts[transaction_id] = cart
            return cart

    def get(self, transaction_id: str) -> Cart | None:
        return self._carts.get(transaction_id)
