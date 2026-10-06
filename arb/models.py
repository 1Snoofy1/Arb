"""Small, JSON-friendly snapshot schema; monetary values stay decimal strings."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class DataError(ValueError):
    """A response cannot be safely normalized."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DataError(f"{field} must be text or an integer ID")
    result = str(value).strip()
    if not result:
        raise DataError(f"{field} cannot be blank")
    return result


def price(value: Any, field: str, divisor: int = 1) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DataError(f"{field} must be numeric")
    try:
        number = Decimal(str(value)) / divisor
    except (InvalidOperation, ValueError):
        raise DataError(f"{field} must be numeric") from None
    if not number.is_finite() or not 0 <= number <= 1:
        raise DataError(f"{field} must be finite and between 0 and 1")
    return format(number, "f")


@dataclass(frozen=True)
class Outcome:
    label: str
    token_id: str | None = None
    indicative_price: str | None = None
    bid: str | None = None
    ask: str | None = None
    last_trade_price: str | None = None


@dataclass(frozen=True)
class Market:
    source: str
    market_id: str
    title: str
    status: str
    observed_at: str
    closes_at: str | None
    source_updated_at: str | None
    event_id: str | None
    condition_id: str | None
    rules: str
    outcomes: tuple[Outcome, ...]
    raw: dict[str, Any]
    price_unit: str = "payout_fraction"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
