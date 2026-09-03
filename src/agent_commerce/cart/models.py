from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CartItem:
    sku: str
    name: str
    unit_price_paise: int
    unit_cost_paise: int
    quantity: int

    @property
    def line_total_paise(self) -> int:
        return self.unit_price_paise * self.quantity

    @property
    def line_cost_paise(self) -> int:
        return self.unit_cost_paise * self.quantity


@dataclass
class Cart:
    """Cart state and totals. Margin computation lives here as the single source of truth
    for both the upsell rules and the eval harness.
    """

    transaction_id: str
    items: dict[str, CartItem] = field(default_factory=dict)
    discount_paise: int = 0

    @property
    def subtotal_paise(self) -> int:
        return sum(item.line_total_paise for item in self.items.values())

    @property
    def total_paise(self) -> int:
        return self.subtotal_paise - self.discount_paise

    @property
    def projected_margin_pct(self) -> float:
        total = self.total_paise
        if total <= 0:
            return 0.0
        profit_paise = (
            sum(item.line_total_paise - item.line_cost_paise for item in self.items.values())
            - self.discount_paise
        )
        return round((profit_paise / total) * 100, 2)

    def add(self, item: CartItem) -> None:
        existing = self.items.get(item.sku)
        if existing is not None:
            existing.quantity += item.quantity
        else:
            self.items[item.sku] = item

    def remove(self, sku: str, quantity: int | None = None) -> None:
        existing = self.items.get(sku)
        if existing is None:
            raise KeyError(f"SKU {sku} not in cart")
        if quantity is None or quantity >= existing.quantity:
            del self.items[sku]
        else:
            existing.quantity -= quantity

    def to_view(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "items": [
                {
                    "sku": item.sku,
                    "name": item.name,
                    "quantity": item.quantity,
                    "unit_price_paise": item.unit_price_paise,
                    "unit_cost_paise": item.unit_cost_paise,
                    "line_total_paise": item.line_total_paise,
                }
                for item in self.items.values()
            ],
            "subtotal_paise": self.subtotal_paise,
            "discount_paise": self.discount_paise,
            "total_paise": self.total_paise,
            "projected_margin_pct": self.projected_margin_pct,
        }
