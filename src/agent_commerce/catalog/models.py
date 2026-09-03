from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    description: str
    category: str
    price_paise: int
    cost_paise: int
    stock: int
    variants: dict[str, list[str]] | None
    tags: list[str] = field(default_factory=list)
    age_range: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Product:
        return cls(
            sku=d["sku"],
            name=d["name"],
            description=d["description"],
            category=d["category"],
            price_paise=d["price_paise"],
            cost_paise=d["cost_paise"],
            stock=d["stock"],
            variants=d.get("variants"),
            tags=d.get("tags", []),
            age_range=d.get("age_range"),
        )

    def to_dict(self) -> dict:
        return asdict(self)
