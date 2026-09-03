from __future__ import annotations


class Money:
    """An exact amount in Indian paise. The only representation of money in this codebase.

    Rupee values are display-only and must never be used for arithmetic or storage —
    see format_inr(). Constructing from a float is intentionally not supported.
    """

    __slots__ = ("_paise",)

    def __init__(self, paise: int) -> None:
        if not isinstance(paise, int) or isinstance(paise, bool):
            got = type(paise).__name__
            raise TypeError(f"Money must be constructed from an int (paise), got {got}")
        self._paise = paise

    @classmethod
    def from_rupees(cls, rupees: int) -> Money:
        if not isinstance(rupees, int) or isinstance(rupees, bool):
            raise TypeError(f"from_rupees requires an int, got {type(rupees).__name__}")
        return cls(rupees * 100)

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @property
    def paise(self) -> int:
        return self._paise

    def format_inr(self) -> str:
        """Display-only formatting, e.g. Money(123456).format_inr() -> '₹1,234.56'."""
        sign = "-" if self._paise < 0 else ""
        whole_paise = abs(self._paise)
        rupees, paise = divmod(whole_paise, 100)
        return f"{sign}₹{rupees:,}.{paise:02d}"

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self._paise + other._paise)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self._paise - other._paise)

    def __mul__(self, factor: object) -> Money:
        if not isinstance(factor, int) or isinstance(factor, bool):
            return NotImplemented
        return Money(self._paise * factor)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self._paise)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Money) and self._paise == other._paise

    def __lt__(self, other: Money) -> bool:
        return self._paise < other._paise

    def __le__(self, other: Money) -> bool:
        return self._paise <= other._paise

    def __gt__(self, other: Money) -> bool:
        return self._paise > other._paise

    def __ge__(self, other: Money) -> bool:
        return self._paise >= other._paise

    def __hash__(self) -> int:
        return hash(self._paise)

    def __repr__(self) -> str:
        return f"Money({self._paise})"
