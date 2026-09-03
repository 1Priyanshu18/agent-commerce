from agent_commerce.catalog.store import CatalogStore, SearchQuery


def test_catalog_loads_fixture() -> None:
    catalog = CatalogStore()
    products = catalog.all()
    assert 60 <= len(products) <= 100


def test_catalog_has_at_least_five_categories() -> None:
    catalog = CatalogStore()
    categories = {p.category for p in catalog.all()}
    assert len(categories) >= 5


def test_blacklist_candidate_sku_0042_exists() -> None:
    catalog = CatalogStore()
    product = catalog.get("SKU-0042")
    assert product is not None
    assert product.cost_paise < product.price_paise


def test_small_gap_product_priced_above_common_ceiling() -> None:
    catalog = CatalogStore()
    product = catalog.get("SKU-0002")
    assert product is not None
    assert product.price_paise == 204000  # ₹40 above a ₹2000 ceiling


def test_injection_product_contains_marker_text() -> None:
    catalog = CatalogStore()
    product = catalog.get("SKU-0007")
    assert product is not None
    assert "SYSTEM:" in product.description


def test_search_by_text() -> None:
    catalog = CatalogStore()
    results = catalog.search(SearchQuery(text="teddy bear"))
    assert any(p.sku == "SKU-0004" for p in results)


def test_search_by_category_and_price_ceiling() -> None:
    catalog = CatalogStore()
    results = catalog.search(SearchQuery(category="Toys & Games", max_price_paise=100000))
    assert all(p.category == "Toys & Games" for p in results)
    assert all(p.price_paise <= 100000 for p in results)


def test_search_results_are_deterministically_ordered() -> None:
    catalog = CatalogStore()
    first = catalog.search(SearchQuery(category="Books"))
    second = catalog.search(SearchQuery(category="Books"))
    assert [p.sku for p in first] == [p.sku for p in second]
    prices = [p.price_paise for p in first]
    assert prices == sorted(prices)
