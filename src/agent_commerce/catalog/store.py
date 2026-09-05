from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .models import Product

_DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "products.json"


@dataclass(frozen=True)
class SearchQuery:
    text: str | None = None
    category: str | None = None
    max_price_paise: int | None = None
    tags: list[str] = field(default_factory=list)
    age_range: str | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "category": self.category,
            "max_price_paise": self.max_price_paise,
            "tags": self.tags,
            "age_range": self.age_range,
        }


class CatalogStore:
    """In-memory product catalog, seeded from a static fixture file for reproducibility.

    Stock is mutable only via set_stock() — a narrow escape hatch for the checkout-time stock
    check (orchestrator/run_session.py) and for reproducing the stock_conflict failure path on
    demand. Every other field stays effectively immutable: nothing else about a product
    changes after boot.
    """

    def __init__(self, fixture_path: Path | str = _DEFAULT_FIXTURE) -> None:
        with open(fixture_path, encoding="utf-8") as f:
            raw = json.load(f)
        self._products: dict[str, Product] = {p["sku"]: Product.from_dict(p) for p in raw}

    def get(self, sku: str) -> Product | None:
        return self._products.get(sku)

    def set_stock(self, sku: str, stock: int) -> None:
        product = self._products.get(sku)
        if product is None:
            raise ValueError(f"unknown SKU: {sku}")
        self._products[sku] = replace(product, stock=stock)

    def all(self) -> list[Product]:
        return list(self._products.values())

    def search(self, query: SearchQuery) -> list[Product]:
        results = []
        for product in self._products.values():
            if query.text:
                haystack = f"{product.name} {product.description} {' '.join(product.tags)}".lower()
                if query.text.lower() not in haystack:
                    continue
            if query.category and product.category != query.category:
                continue
            if query.max_price_paise is not None and product.price_paise > query.max_price_paise:
                continue
            if query.tags:
                wanted = {t.lower() for t in query.tags}
                have = {t.lower() for t in product.tags}
                if not wanted & have:
                    continue
            if query.age_range and product.age_range and query.age_range != product.age_range:
                continue
            results.append(product)
        results.sort(key=lambda p: (p.price_paise, p.sku))
        return results
