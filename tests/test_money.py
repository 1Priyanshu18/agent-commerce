import pytest

from agent_commerce.core.money import Money


def test_from_rupees() -> None:
    assert Money.from_rupees(20).paise == 2000


def test_arithmetic() -> None:
    assert (Money(500) + Money(250)) == Money(750)
    assert (Money(500) - Money(250)) == Money(250)
    assert (Money(500) * 3) == Money(1500)


def test_format_inr() -> None:
    assert Money(123456).format_inr() == "₹1,234.56"
    assert Money(0).format_inr() == "₹0.00"
    assert Money(-50).format_inr() == "-₹0.50"


def test_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money(19.99)  # type: ignore[arg-type]


def test_rejects_bool_as_int() -> None:
    with pytest.raises(TypeError):
        Money(True)  # type: ignore[arg-type]


def test_ordering() -> None:
    assert Money(100) < Money(200)
    assert Money(200) >= Money(200)
