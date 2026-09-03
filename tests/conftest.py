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
