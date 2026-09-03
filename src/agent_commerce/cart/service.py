from __future__ import annotations

from agent_commerce.catalog.store import CatalogStore
from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore

from .models import Cart, CartItem


class CartService:
    """Wraps Cart mutations with catalog validation and ledger writes."""

    def __init__(self, catalog: CatalogStore, ledger: LedgerStore) -> None:
        self._catalog = catalog
        self._ledger = ledger

    def add_item(
        self,
        cart: Cart,
        sku: str,
        quantity: int,
        *,
        actor: Actor,
        caused_by: list[str],
    ) -> Cart:
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        product = self._catalog.get(sku)
        if product is None:
            raise ValueError(f"Unknown SKU: {sku}")

        cart.add(
            CartItem(
                sku=product.sku,
                name=product.name,
                unit_price_paise=product.price_paise,
                unit_cost_paise=product.cost_paise,
                quantity=quantity,
            )
        )

        self._ledger.append(
            transaction_id=cart.transaction_id,
            caused_by=caused_by,
            actor=actor,
            action_type=ActionType.SELECT,
            input={"op": "add", "sku": sku, "quantity": quantity},
            output=cart.to_view(),
            resulting_state=cart.to_view(),
        )
        return cart

    def remove_item(
        self,
        cart: Cart,
        sku: str,
        quantity: int | None = None,
        *,
        actor: Actor,
        caused_by: list[str],
    ) -> Cart:
        cart.remove(sku, quantity)

        self._ledger.append(
            transaction_id=cart.transaction_id,
            caused_by=caused_by,
            actor=actor,
            action_type=ActionType.SELECT,
            input={"op": "remove", "sku": sku, "quantity": quantity},
            output=cart.to_view(),
            resulting_state=cart.to_view(),
        )
        return cart
