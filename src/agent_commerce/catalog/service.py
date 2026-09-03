from __future__ import annotations

from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerEntry, LedgerStore

from .models import Product
from .store import CatalogStore, SearchQuery


class CatalogService:
    """Wraps CatalogStore with ledger writes. Every search is a provenance root or link."""

    def __init__(self, catalog: CatalogStore, ledger: LedgerStore) -> None:
        self._catalog = catalog
        self._ledger = ledger

    def search(
        self,
        query: SearchQuery,
        *,
        transaction_id: str,
        actor: Actor,
        caused_by: list[str],
    ) -> tuple[list[Product], LedgerEntry]:
        results = self._catalog.search(query)
        entry = self._ledger.append(
            transaction_id=transaction_id,
            caused_by=caused_by,
            actor=actor,
            action_type=ActionType.SEARCH,
            input=query.to_dict(),
            output={"skus": [p.sku for p in results], "count": len(results)},
        )
        return results, entry
