import json
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from agent_commerce.cart.service import CartService
from agent_commerce.catalog.service import CatalogService
from agent_commerce.catalog.store import CatalogStore
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.mcp.buyer_server import build_buyer_server
from agent_commerce.mcp.merchant_server import build_merchant_server
from agent_commerce.orchestrator.session import SessionRegistry


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def mcp_stack(tmp_path) -> SimpleNamespace:
    """Both servers wired to the same shared catalog/ledger/session registry, mirroring how
    they'd be constructed in the real app — a cart mutated via the buyer server must be
    visible to the merchant server's read-only projection.
    """
    catalog = CatalogStore()
    ledger = LedgerStore(tmp_path / "ledger.db")
    sessions = SessionRegistry()
    catalog_service = CatalogService(catalog, ledger)
    cart_service = CartService(catalog, ledger)

    buyer_mcp = build_buyer_server(
        catalog=catalog,
        catalog_service=catalog_service,
        cart_service=cart_service,
        sessions=sessions,
        ledger=ledger,
    )
    merchant_mcp = build_merchant_server(catalog=catalog, sessions=sessions, ledger=ledger)

    return SimpleNamespace(
        catalog=catalog,
        ledger=ledger,
        sessions=sessions,
        buyer_mcp=buyer_mcp,
        merchant_mcp=merchant_mcp,
    )


@pytest.fixture
def make_catalog(tmp_path) -> Callable[[list[dict]], CatalogStore]:
    """Factory for a CatalogStore over a small, hand-picked set of products (with exact,
    known margins) rather than the full generated fixture — useful wherever a test needs
    precise, deterministic assertions about margin math.
    """

    def _make(products: list[dict]) -> CatalogStore:
        path = tmp_path / f"catalog_{len(products)}_{id(products)}.json"
        full_products = [
            {
                "description": p.get("description", p["name"]),
                "variants": None,
                "tags": [],
                "age_range": None,
                **p,
            }
            for p in products
        ]
        path.write_text(json.dumps(full_products), encoding="utf-8")
        return CatalogStore(path)

    return _make
