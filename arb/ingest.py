"""Bounded pagination and explicit health reports for each source."""

import json
from importlib.resources import files

from .http import FetchError, PublicClient
from .models import DataError, utc_now
from .sources import ADAPTERS, page


def collect(source: str, client: PublicClient | None, *, limit: int, page_size: int,
            max_pages: int, offline: bool = False) -> tuple[list[dict], dict]:
    markets = []
    report = {
        "status": "ok", "pages_fetched": 0, "records_received": 0,
        "accepted": 0, "skipped": 0, "duplicates": 0,
        "rejected": [], "error": None, "stop_reason": None,
    }
    seen_ids = set()
    seen_cursors = set()
    cursor = None
    offset = 0
    try:
        for _ in range(max_pages):
            requested = min(page_size, limit - report["records_received"])
            if offline:
                fixture = files("arb").joinpath("fixtures", source + ".json")
                payload = json.loads(fixture.read_text(encoding="utf-8"))
            else:
                params = {"limit": requested}
                if source == "polymarket":
                    params.update({"closed": "false", "active": "true", "offset": offset})
                else:
                    params["status"] = "open"
                    params["mve_filter"] = "exclude"
                    if cursor:
                        params["cursor"] = cursor
                payload = client.get(source, params)
            observed_at = utc_now()
            records, next_cursor = page(source, payload)
            report["pages_fetched"] += 1
            bounded = records[:limit - report["records_received"]]
            report["records_received"] += len(bounded)
            for index, raw in enumerate(bounded):
                try:
                    if not isinstance(raw, dict):
                        raise DataError("market must be an object")
                    market = ADAPTERS[source](raw, observed_at)
                    if market is None:
                        report["skipped"] += 1
                    elif market.market_id in seen_ids:
                        report["duplicates"] += 1
                    else:
                        seen_ids.add(market.market_id)
                        markets.append(market.to_dict())
                except DataError as exc:
                    report["rejected"].append({
                        "page": report["pages_fetched"], "index": index,
                        "reason": str(exc), "raw": raw,
                    })
            if offline:
                report["stop_reason"] = "offline_fixture"
                break
            if report["records_received"] >= limit:
                report["stop_reason"] = "record_limit"
                break
            if not records or (source == "polymarket" and len(records) < requested) or (
                    source == "kalshi" and not next_cursor):
                report["stop_reason"] = "exhausted"
                break
            if source == "kalshi":
                if next_cursor in seen_cursors:
                    raise DataError("pagination returned a repeated cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            offset += len(records)
        else:
            report["stop_reason"] = "page_limit"
    except (FetchError, DataError, OSError, ValueError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        report["stop_reason"] = "error"
    report["accepted"] = len(markets)
    if report["rejected"]:
        report["status"] = "error"
        report["error"] = report["error"] or "One or more records failed schema validation"
    if not markets and report["status"] == "ok":
        report["status"] = "empty"
        report["error"] = "No valid open markets were ingested"
    return markets, report


def snapshot(sources: list[str], *, limit: int = 100, page_size: int = 100,
             max_pages: int = 5, offline: bool = False,
             client: PublicClient | None = None) -> dict:
    if not sources or any(source not in ADAPTERS for source in sources):
        raise ValueError("At least one supported source is required")
    if not 1 <= limit <= 10000 or not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
        raise ValueError("limit must be 1..10000, page_size 1..100, max_pages 1..100")
    result = {
        "schema_version": 1, "mode": "offline" if offline else "live",
        "started_at": utc_now(), "finished_at": None,
        "execution_enabled": False, "trading_enabled": False,
        "success": False, "sources": {}, "markets": [],
        "bounds": {"limit_per_source": limit, "page_size": page_size, "max_pages": max_pages},
    }
    if not offline:
        client = client or PublicClient()
    for source in dict.fromkeys(sources):
        markets, report = collect(source, client, limit=limit, page_size=page_size,
                                  max_pages=max_pages, offline=offline)
        result["markets"].extend(markets)
        result["sources"][source] = report
    result["finished_at"] = utc_now()
    result["success"] = all(report["status"] == "ok" for report in result["sources"].values())
    return result
