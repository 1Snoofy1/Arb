"""Discovery adapters. Gamma indicative prices are never promoted to bid/ask."""

import json
from typing import Any

from .models import DataError, Market, Outcome, price, text


def array(value: Any, field: str) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise DataError(f"{field} is not a JSON array") from None
    if not isinstance(value, list):
        raise DataError(f"{field} must be an array")
    return value


def optional_text(value: Any, field: str) -> str | None:
    return None if value is None or value == "" else text(value, field)


def polymarket(raw: dict, observed_at: str) -> Market | None:
    # Fail closed on absent or incorrectly typed status flags.
    if not isinstance(raw.get("active"), bool) or not isinstance(raw.get("closed"), bool):
        raise DataError("active and closed must be booleans")
    if raw["closed"] or not raw["active"] or raw.get("archived") is True:
        return None
    labels = array(raw.get("outcomes"), "outcomes")
    values = array(raw.get("outcomePrices"), "outcomePrices")
    tokens = array(raw["clobTokenIds"], "clobTokenIds") if raw.get("clobTokenIds") else [None] * len(labels)
    if len(labels) < 2 or len(labels) != len(values) or len(labels) != len(tokens):
        raise DataError("outcome, price and token arrays must align and have at least two items")
    outcomes = tuple(Outcome(
        label=text(label, "outcome"),
        token_id=optional_text(token, "token_id"),
        indicative_price=price(value, "outcomePrices"),
    ) for label, value, token in zip(labels, values, tokens))
    if len({item.label for item in outcomes}) != len(outcomes):
        raise DataError("outcome labels must be unique")
    events = raw.get("events") or []
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise DataError("events must be an array of objects")
    return Market(
        source="polymarket", market_id=text(raw.get("id"), "id"),
        title=text(raw.get("question"), "question"), status="open",
        observed_at=observed_at, closes_at=optional_text(raw.get("endDate"), "endDate"),
        source_updated_at=optional_text(raw.get("updatedAt"), "updatedAt"),
        event_id=optional_text(events[0].get("id"), "event_id") if events else None,
        condition_id=optional_text(raw.get("conditionId"), "conditionId"),
        rules=optional_text(raw.get("description"), "description") or "",
        outcomes=outcomes, raw=raw,
    )


def kalshi_price(raw: dict, name: str) -> str | None:
    # Dollar fields take precedence, even when null; never silently substitute a stale legacy value.
    if name + "_dollars" in raw:
        return price(raw[name + "_dollars"], name + "_dollars")
    return price(raw.get(name), name, divisor=100)


def kalshi(raw: dict, observed_at: str) -> Market | None:
    status = text(raw.get("status"), "status")
    if status not in {"open", "active"}:
        return None
    # Kalshi's query uses status=open; market objects may report status=active.
    if raw.get("market_type", "binary") != "binary":
        return None
    return Market(
        source="kalshi", market_id=text(raw.get("ticker"), "ticker"),
        title=text(raw.get("title"), "title"), status="open",
        observed_at=observed_at, closes_at=optional_text(raw.get("close_time"), "close_time"),
        source_updated_at=optional_text(raw.get("updated_time"), "updated_time"),
        event_id=optional_text(raw.get("event_ticker"), "event_ticker"), condition_id=None,
        rules="\n\n".join(filter(None, (
            optional_text(raw.get("rules_primary"), "rules_primary"),
            optional_text(raw.get("rules_secondary"), "rules_secondary"),
        ))),
        outcomes=(
            Outcome(label="Yes", bid=kalshi_price(raw, "yes_bid"), ask=kalshi_price(raw, "yes_ask"),
                    last_trade_price=kalshi_price(raw, "last_price")),
            Outcome(label="No", bid=kalshi_price(raw, "no_bid"), ask=kalshi_price(raw, "no_ask")),
        ), raw=raw,
    )


ADAPTERS = {"polymarket": polymarket, "kalshi": kalshi}


def page(source: str, payload: Any) -> tuple[list, str | None]:
    if source == "polymarket":
        return array(payload, "markets"), None
    if not isinstance(payload, dict):
        raise DataError("Kalshi response must be an object")
    records = array(payload.get("markets"), "markets")
    cursor = payload.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise DataError("cursor must be text")
    return records, cursor or None
