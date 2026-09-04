from agent_commerce.agents.upsell.strategy import MerchantRules, find_candidate_products
from agent_commerce.cart.models import Cart, CartItem

_PRODUCTS = [
    {
        "sku": "SKU-A001",
        "name": "Cart Item",
        "category": "Toys",
        "price_paise": 100000,
        "cost_paise": 70000,
        "stock": 10,
        "tags": ["building"],
    },
    {
        "sku": "SKU-A002",
        "name": "High Margin Toy",
        "category": "Toys",
        "price_paise": 50000,
        "cost_paise": 20000,
        "stock": 5,
        "tags": ["puzzle"],
    },
    {
        "sku": "SKU-A003",
        "name": "Low Margin Toy",
        "category": "Toys",
        "price_paise": 40000,
        "cost_paise": 32000,
        "stock": 5,
        "tags": ["building"],
    },
    {
        "sku": "SKU-A004",
        "name": "Unrelated Book",
        "category": "Books",
        "price_paise": 30000,
        "cost_paise": 10000,
        "stock": 5,
        "tags": [],
    },
    {
        "sku": "SKU-A005",
        "name": "Out of Stock Toy",
        "category": "Toys",
        "price_paise": 60000,
        "cost_paise": 10000,
        "stock": 0,
        "tags": ["puzzle"],
    },
    {
        "sku": "SKU-A006",
        "name": "Blacklisted Toy",
        "category": "Toys",
        "price_paise": 20000,
        "cost_paise": 5000,
        "stock": 5,
        "tags": [],
    },
]


def _cart_with_a001() -> Cart:
    cart = Cart(transaction_id="txn_1")
    cart.add(
        CartItem(sku="SKU-A001", name="Cart Item", unit_price_paise=100000, unit_cost_paise=70000, quantity=1)
    )
    return cart


def _rules() -> MerchantRules:
    return MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset({"SKU-A006"}))


def test_finds_same_category_complements(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    candidates = find_candidate_products(_cart_with_a001(), catalog, _rules())
    skus = {p.sku for p in candidates}
    assert "SKU-A002" in skus
    assert "SKU-A003" in skus


def test_excludes_different_category_with_no_tag_overlap(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    candidates = find_candidate_products(_cart_with_a001(), catalog, _rules())
    assert "SKU-A004" not in {p.sku for p in candidates}


def test_excludes_out_of_stock(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    candidates = find_candidate_products(_cart_with_a001(), catalog, _rules())
    assert "SKU-A005" not in {p.sku for p in candidates}


def test_excludes_blacklisted(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    candidates = find_candidate_products(_cart_with_a001(), catalog, _rules())
    assert "SKU-A006" not in {p.sku for p in candidates}


def test_excludes_items_already_in_cart(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    candidates = find_candidate_products(_cart_with_a001(), catalog, _rules())
    assert "SKU-A001" not in {p.sku for p in candidates}


def test_empty_cart_has_no_candidates(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    candidates = find_candidate_products(Cart(transaction_id="txn_1"), catalog, _rules())
    assert candidates == []


def test_tag_overlap_alone_is_sufficient_even_across_categories(make_catalog) -> None:
    products = [
        {
            "sku": "SKU-B001",
            "name": "Cart Item",
            "category": "Toys",
            "price_paise": 100000,
            "cost_paise": 70000,
            "stock": 10,
            "tags": ["birthday"],
        },
        {
            "sku": "SKU-B002",
            "name": "Cross-Category Match",
            "category": "Books",
            "price_paise": 30000,
            "cost_paise": 10000,
            "stock": 5,
            "tags": ["birthday"],
        },
    ]
    catalog = make_catalog(products)
    cart = Cart(transaction_id="txn_1")
    cart.add(
        CartItem(sku="SKU-B001", name="Cart Item", unit_price_paise=100000, unit_cost_paise=70000, quantity=1)
    )
    candidates = find_candidate_products(cart, catalog, _rules())
    assert {p.sku for p in candidates} == {"SKU-B002"}
