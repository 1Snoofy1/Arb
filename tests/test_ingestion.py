import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from arb.cli import main, write_snapshot
from arb.http import ENDPOINTS, FetchError, MAX_BODY_BYTES, NoRedirect, PublicClient
from arb.ingest import snapshot
from arb.models import DataError, price
from arb.sources import kalshi, page, polymarket


FIXTURES = Path(__file__).resolve().parents[1] / "arb" / "fixtures"
POLY = json.loads((FIXTURES / "polymarket.json").read_text())[0]
KALSHI = json.loads((FIXTURES / "kalshi.json").read_text())["markets"][0]
NOW = "2026-10-06T21:00:00+00:00"


def poly(identifier):
    return {**POLY, "id": identifier}


def kal(identifier):
    return {**KALSHI, "ticker": identifier}


class AdapterTests(unittest.TestCase):
    def test_gamma_encoded_arrays_and_price_semantics(self):
        market = polymarket(copy.deepcopy(POLY), NOW)
        self.assertEqual(market.outcomes[0].indicative_price, "0.42")
        self.assertIsNone(market.outcomes[0].ask)
        self.assertIsNone(market.outcomes[0].bid)
        self.assertEqual(market.outcomes[0].token_id, "demo-yes-token")
        self.assertEqual(market.observed_at, NOW)
        self.assertEqual(market.raw, POLY)

    def test_gamma_native_arrays_and_non_binary_outcomes(self):
        raw = {**POLY, "outcomes": ["A", "B", "C"], "outcomePrices": ["0.2", "0.3", "0.5"],
               "clobTokenIds": ["a", "b", "c"]}
        self.assertEqual(len(polymarket(raw, NOW).outcomes), 3)

    def test_gamma_mismatched_or_malformed_arrays_fail(self):
        for changes in ({"outcomePrices": "[\"0.2\"]"}, {"outcomes": "bad"},
                        {"clobTokenIds": ["one"]}, {"outcomes": ["Yes", "Yes"]}):
            with self.subTest(changes=changes), self.assertRaises(DataError):
                polymarket({**POLY, **changes}, NOW)

    def test_closed_inactive_and_archived_are_skipped(self):
        for changes in ({"closed": True}, {"active": False}, {"archived": True}):
            self.assertIsNone(polymarket({**POLY, **changes}, NOW))
        for status in ("closed", "settled", "paused", "initialized"):
            self.assertIsNone(kalshi({**KALSHI, "status": status}, NOW))

    def test_missing_or_string_status_flags_fail(self):
        for changes in ({"active": "true"}, {"closed": None}):
            with self.assertRaises(DataError):
                polymarket({**POLY, **changes}, NOW)
        with self.assertRaises(DataError):
            kalshi({**KALSHI, "status": None}, NOW)

    def test_invalid_prices_rejected_including_boolean_nan_and_infinity(self):
        for value in (True, "NaN", "Infinity", "-0.1", "1.1", "garbage", [], {}):
            with self.subTest(value=value), self.assertRaises(DataError):
                price(value, "price")
        self.assertEqual(price("0", "price"), "0")
        self.assertEqual(price("1", "price"), "1")
        self.assertIsNone(price(None, "price"))

    def test_kalshi_preserves_dollar_precision_and_does_not_invent_no_last_price(self):
        market = kalshi(KALSHI, NOW)
        self.assertEqual(market.outcomes[0].bid, "0.4100")
        self.assertEqual(market.outcomes[0].ask, "0.4400")
        self.assertIsNone(market.outcomes[1].last_trade_price)
        self.assertIsNone(market.outcomes[0].indicative_price)

    def test_kalshi_legacy_cents_and_null_precedence(self):
        raw = {key: value for key, value in KALSHI.items() if not key.endswith("_dollars")}
        raw.update({"yes_bid": 41, "yes_ask": 44, "no_bid": 56, "no_ask": 59})
        self.assertEqual(kalshi(raw, NOW).outcomes[0].ask, "0.44")
        raw["yes_ask_dollars"] = None
        self.assertIsNone(kalshi(raw, NOW).outcomes[0].ask)

    def test_bad_envelopes_are_not_silent_empty_pages(self):
        for source, payload in (("polymarket", {}), ("kalshi", []), ("kalshi", {}),
                                ("kalshi", {"markets": [], "cursor": 4})):
            with self.subTest(source=source, payload=payload), self.assertRaises(DataError):
                page(source, payload)


class IngestionTests(unittest.TestCase):
    def test_offline_run_never_constructs_network_client(self):
        with patch("arb.ingest.PublicClient", side_effect=AssertionError("network forbidden")):
            result = snapshot(["polymarket", "kalshi"], offline=True)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["markets"]), 2)
        self.assertEqual(result["mode"], "offline")
        self.assertFalse(result["execution_enabled"])
        self.assertFalse(result["trading_enabled"])

    def test_gamma_offsets_and_deduplication(self):
        client = Mock()
        client.get.side_effect = [[poly("a"), poly("b")], [poly("b"), poly("c")], []]
        result = snapshot(["polymarket"], client=client, limit=10, page_size=2)
        self.assertEqual([call.args[1]["offset"] for call in client.get.call_args_list], [0, 2, 4])
        self.assertEqual([item["market_id"] for item in result["markets"]], ["a", "b", "c"])
        self.assertEqual(result["sources"]["polymarket"]["duplicates"], 1)
        self.assertEqual(result["sources"]["polymarket"]["stop_reason"], "exhausted")

    def test_kalshi_cursor_pagination_and_remaining_limit(self):
        client = Mock()
        client.get.side_effect = [{"markets": [kal("a"), kal("b")], "cursor": "next"},
                                  {"markets": [kal("c")], "cursor": "more"}]
        result = snapshot(["kalshi"], client=client, limit=3, page_size=2)
        self.assertEqual(client.get.call_args_list[1].args[1], {
            "limit": 1, "status": "open", "mve_filter": "exclude", "cursor": "next"})
        self.assertEqual(len(result["markets"]), 3)
        self.assertEqual(result["sources"]["kalshi"]["stop_reason"], "record_limit")

    def test_page_limit_is_visible(self):
        client = Mock()
        client.get.return_value = {"markets": [kal("a")], "cursor": "next"}
        result = snapshot(["kalshi"], client=client, limit=10, max_pages=1)
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(result["sources"]["kalshi"]["stop_reason"], "page_limit")

    def test_repeated_cursor_fails_without_infinite_loop(self):
        client = Mock()
        client.get.side_effect = [{"markets": [kal("a")], "cursor": "again"},
                                  {"markets": [kal("b")], "cursor": "again"}]
        result = snapshot(["kalshi"], client=client, limit=10, page_size=1)
        self.assertFalse(result["success"])
        self.assertIn("repeated cursor", result["sources"]["kalshi"]["error"])
        self.assertEqual(client.get.call_count, 2)

    def test_partial_source_failure_preserves_other_source(self):
        client = Mock()
        client.get.side_effect = [FetchError("polymarket: HTTP 403"), {"markets": [KALSHI]}]
        result = snapshot(["polymarket", "kalshi"], client=client)
        self.assertFalse(result["success"])
        self.assertEqual(result["sources"]["polymarket"]["status"], "error")
        self.assertEqual(result["sources"]["kalshi"]["status"], "ok")
        self.assertEqual(len(result["markets"]), 1)

    def test_later_page_failure_preserves_earlier_markets(self):
        client = Mock()
        client.get.side_effect = [[POLY], FetchError("connection lost")]
        result = snapshot(["polymarket"], client=client, limit=5, page_size=1)
        self.assertFalse(result["success"])
        self.assertEqual(len(result["markets"]), 1)

    def test_invalid_record_is_quarantined_and_run_fails(self):
        client = Mock()
        client.get.return_value = [POLY, {**POLY, "outcomePrices": "[\"NaN\", \"0.5\"]"}, 42]
        result = snapshot(["polymarket"], client=client)
        report = result["sources"]["polymarket"]
        self.assertFalse(result["success"])
        self.assertEqual(report["accepted"], 1)
        self.assertEqual(len(report["rejected"]), 2)
        self.assertEqual(report["rejected"][1]["raw"], 42)

    def test_empty_response_fails_validation(self):
        client = Mock()
        client.get.return_value = []
        result = snapshot(["polymarket"], client=client)
        self.assertFalse(result["success"])
        self.assertEqual(result["sources"]["polymarket"]["status"], "empty")

    def test_provider_oversized_page_respects_record_limit(self):
        client = Mock()
        client.get.return_value = [poly("a"), poly("b"), poly("c")]
        result = snapshot(["polymarket"], client=client, limit=1)
        self.assertEqual(len(result["markets"]), 1)


class HttpTests(unittest.TestCase):
    def client(self):
        client = PublicClient(timeout=2, retries=1)
        client.opener = Mock()
        return client

    def response(self, body=b"[]"):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = body
        return response

    def error(self, code, headers=None):
        return HTTPError(ENDPOINTS["kalshi"], code, "error", headers or {}, io.BytesIO())

    def test_transport_only_emits_allowlisted_get_without_credentials(self):
        client = self.client()
        client.opener.open.return_value = self.response()
        client.get("polymarket", {"limit": 2})
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(urlsplit(request.full_url).path, "/markets")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"limit": ["2"]})
        self.assertFalse(any(key.lower() == "authorization" for key in request.headers))
        with self.assertRaises(FetchError):
            client.get("https://example.com/orders", {})
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_retries_rate_limit_with_retry_after(self):
        client = self.client()
        client.opener.open.side_effect = [self.error(429, {"Retry-After": "1"}), self.response()]
        with patch("arb.http.time.sleep") as sleep:
            self.assertEqual(client.get("kalshi", {}), [])
        sleep.assert_called_once_with(1)

    def test_long_retry_after_fails_without_sleeping(self):
        client = self.client()
        client.opener.open.side_effect = self.error(429, {"Retry-After": "120"})
        with patch("arb.http.time.sleep") as sleep, self.assertRaises(FetchError):
            client.get("kalshi", {})
        sleep.assert_not_called()

    def test_redirects_and_forbidden_are_not_retried(self):
        for status in (302, 403):
            client = self.client()
            client.opener.open.side_effect = self.error(status)
            with self.assertRaises(FetchError):
                client.get("kalshi", {})
            self.assertEqual(client.opener.open.call_count, 1)

    def test_network_retry_budget_is_finite(self):
        client = self.client()
        client.opener.open.side_effect = URLError("blocked")
        with patch("arb.http.time.sleep"), self.assertRaises(FetchError):
            client.get("kalshi", {})
        self.assertEqual(client.opener.open.call_count, 2)

    def test_invalid_json_and_nonstandard_constants_fail(self):
        for body in (b"not json", b"[NaN]", b"[Infinity]"):
            client = self.client()
            client.opener.open.return_value = self.response(body)
            with self.assertRaises(FetchError):
                client.get("kalshi", {})

    def test_oversized_body_fails(self):
        client = self.client()
        client.opener.open.return_value = self.response(b" " * (MAX_BODY_BYTES + 1))
        with self.assertRaises(FetchError):
            client.get("kalshi", {})


class CliTests(unittest.TestCase):
    def test_offline_cli_writes_json_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stderr", new_callable=io.StringIO):
            output = Path(directory) / "nested" / "snapshot.json"
            self.assertEqual(main(["--offline", "--output", str(output)]), 0)
            result = json.loads(output.read_text())
            self.assertTrue(result["success"])
            self.assertEqual(list(output.parent.glob(".arb-*")), [])

    def test_partial_failure_returns_nonzero_with_json_output(self):
        client = Mock()
        client.get.side_effect = FetchError("HTTP 403")
        with patch("arb.cli.PublicClient", return_value=client), patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["--source", "polymarket"]), 1)
            self.assertFalse(json.loads(output.getvalue())["success"])

    def test_invalid_bounds_and_execution_flags_rejected(self):
        for args in (["--limit", "0"], ["--max-pages", "0"], ["--timeout", "nan"],
                     ["--timeout", "inf"], ["--retries", "4"], ["--execute"], ["--trade"]):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as exc:
                main(args)
            self.assertEqual(exc.exception.code, 2)

    def test_atomic_write_failure_keeps_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            output.write_text("old")
            with patch("arb.cli.os.replace", side_effect=OSError("write failed")), self.assertRaises(OSError):
                write_snapshot(output, "new")
            self.assertEqual(output.read_text(), "old")
            self.assertEqual(list(output.parent.glob(".arb-*")), [])


if __name__ == "__main__":
    unittest.main()
