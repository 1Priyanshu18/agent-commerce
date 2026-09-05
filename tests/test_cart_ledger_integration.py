"""Drive a cart to a total from Python, with the ledger recording every mutation and
correct caused_by provenance links, and the chain verifying.
"""

from agent_commerce.cart.models import Cart
from agent_commerce.cart.service import CartService
from agent_commerce.catalog.service import CatalogService
from agent_commerce.catalog.store import CatalogStore, SearchQuery
from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore


def test_drive_cart_to_total_with_provenance(tmp_path) -> None:
    catalog_store = CatalogStore()
    ledger = LedgerStore(tmp_path / "ledger.db")
    catalog_service = CatalogService(catalog_store, ledger)
    cart_service = CartService(catalog_store, ledger)

    transaction_id = "txn_demo_1"
    cart = Cart(transaction_id=transaction_id)

    # 1. Buyer searches for a birthday gift under budget.
    results, search_entry = catalog_service.search(
        SearchQuery(category="Toys & Games", max_price_paise=200000),
        transaction_id=transaction_id,
        actor=Actor.BUYER_AGENT,
        caused_by=[],
    )
    assert len(results) > 0
    chosen = results[0]

    # 2. Buyer adds the chosen product, caused by the search that surfaced it.
    cart = cart_service.add_item(
        cart,
        chosen.sku,
        2,
        actor=Actor.BUYER_AGENT,
        caused_by=[search_entry.entry_id],
    )
    add_entries = [
        e
        for e in ledger.entries_for_transaction(transaction_id)
        if e.action_type == ActionType.SELECT
    ]
    assert add_entries[-1].caused_by == [search_entry.entry_id]

    # 3. Buyer removes one unit, caused by the add.
    cart = cart_service.remove_item(
        cart,
        chosen.sku,
        1,
        actor=Actor.BUYER_AGENT,
        caused_by=[add_entries[-1].entry_id],
    )

    assert cart.items[chosen.sku].quantity == 1
    assert cart.total_paise == chosen.price_paise
    assert cart.projected_margin_pct > 0

    entries = ledger.entries_for_transaction(transaction_id)
    assert [e.action_type for e in entries] == [
        ActionType.SEARCH,
        ActionType.SELECT,
        ActionType.SELECT,
    ]
    assert entries[1].caused_by == [entries[0].entry_id]
    assert entries[2].caused_by == [entries[1].entry_id]

    verification = ledger.verify_chain()
    assert verification.ok is True
    assert verification.entries_checked == 3
