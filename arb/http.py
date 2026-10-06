"""GET-only, endpoint-allowlisted transport without credentials or redirects."""

import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINTS = {
    "polymarket": "https://gamma-api.polymarket.com/markets",
    "kalshi": "https://external-api.kalshi.com/trade-api/v2/markets",
}
MAX_BODY_BYTES = 8 * 1024 * 1024
RETRY_STATUSES = {429, 500, 502, 503, 504}


def invalid_json_constant(value: str):
    raise ValueError(f"Non-standard JSON constant: {value}")


class FetchError(RuntimeError):
    """Public market data could not be fetched."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    def __init__(self, timeout: float = 10, retries: int = 2):
        if not math.isfinite(timeout) or timeout <= 0 or not 0 <= retries <= 3:
            raise ValueError("timeout must be finite and positive; retries must be 0..3")
        self.timeout = timeout
        self.retries = retries
        self.opener = build_opener(NoRedirect())

    def get(self, source: str, params: dict):
        if source not in ENDPOINTS:
            raise FetchError(f"Unsupported public data source: {source}")
        url = ENDPOINTS[source] + "?" + urlencode(params)
        request = Request(url, headers={
            "Accept": "application/json", "User-Agent": "Arb-ReadOnlyScanner/0.1"
        }, method="GET")
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise FetchError(f"{source}: response exceeds 8 MiB")
                return json.loads(body, parse_constant=invalid_json_constant)
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status not in RETRY_STATUSES or attempt == self.retries:
                    raise FetchError(f"{source}: HTTP {status}") from None
                delay = 0.5 * 2 ** attempt
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            date = parsedate_to_datetime(retry_after)
                            if date.tzinfo is None:
                                date = date.replace(tzinfo=timezone.utc)
                            delay = max(0, (date - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                if not math.isfinite(delay) or delay > 5:
                    raise FetchError(f"{source}: HTTP {status}; Retry-After exceeds retry budget") from None
                time.sleep(max(0, delay))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise FetchError(f"{source}: network failure ({type(exc).__name__})") from None
                time.sleep(0.5 * 2 ** attempt)
            except (ValueError, UnicodeError, RecursionError):
                raise FetchError(f"{source}: response is not valid JSON") from None
