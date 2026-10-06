# Arb — read-only prediction-market scanner foundation

Small Python prototype that discovers open markets from **Polymarket Gamma** and
**Kalshi**, validates their responses, and writes one normalized JSON snapshot.
Start here to validate the data flow before building market matching or arbitrage
signals. Requires **Python 3.11+**, with **no third-party runtime dependencies**.

**Execution and trading are disabled by construction.** There are no order,
wallet, signing, transfer, account, or execution APIs in this project. The only
network operations are unauthenticated `GET` requests to two fixed market-list
endpoints. There is no switch to enable trading. Do not add API keys or private
keys; this prototype does not need them.

## Run it

From the repository root (Windows users can use `py -3` in place of `python`):

```bash
# Deterministic demo: synthetic fixtures, no network or API keys
python -m arb --offline --output data/demo.json

# Live snapshot: both sources, up to 100 raw records per source
python -m arb --output data/live.json

# Small live Kalshi run exercising cursor pagination
python -m arb --source kalshi --limit 6 --page-size 2 --output data/kalshi.json

# A larger bounded discovery run; still a sample, not a full-universe guarantee
python -m arb --limit 300 --page-size 100 --max-pages 3 --output data/sample.json

# Run checks without contacting the markets
python -m unittest discover -s tests -v
```

Omit `--output` to write JSON to stdout. Health summaries go to stderr, so the
JSON can be piped into another process. Repeat `--source` to select both sources;
the default is both. `--help` lists all options.

Optional installation provides the same CLI as `arb-scan`:

```bash
python -m pip install -e .
arb-scan --offline --output data/demo.json
```

Generated snapshots belong in `data/`, which is ignored by Git. Output files are
replaced atomically. An existing path is overwritten, so choose a timestamped
filename when preserving history.

## First supported data sources

| Source | Public endpoint | Ingested data | Pagination |
| --- | --- | --- | --- |
| Polymarket Gamma | `https://gamma-api.polymarket.com/markets` | IDs, question, outcome/token arrays, indicative outcome prices, end date, description, raw record | `limit` / `offset`; requests `active=true&closed=false` and checks returned flags |
| Kalshi | `https://external-api.kalshi.com/trade-api/v2/markets` | Ticker, title, YES/NO bid/ask, YES last trade, close time, settlement rules, raw record | `limit` / response `cursor`; requests `status=open&mve_filter=exclude` and accepts open/active binary records |

Sources checked against official documentation on **October 6, 2026**:

- [Polymarket market-list API](https://docs.polymarket.com/api-reference/markets/list-markets)
- [Kalshi public market-data quick start](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Kalshi market-list API](https://docs.kalshi.com/api-reference/market/get-markets)

Polymarket CLOB order books are a logical next data source, after Gamma discovery
is validated. Wealthsimple, Novig, and other venues are **not implemented**;
add them only after verifying a documented, permitted data API and its response
contract. There is no browser scraping or reuse of private account endpoints.

## What the snapshot means

Each file contains:

- `schema_version`, `mode` (`live` or `offline`), UTC run timestamps and limits.
- `execution_enabled: false` and `trading_enabled: false`.
- `sources`: per-source health, pages, counts, stop reason, rejected raw records
  and errors. One source failing does not discard the other source's data.
- `markets`: normalized identity, title, status, observation time, source update
  time if supplied, closing time, event/condition identifiers, resolution text,
  outcomes and the original `raw` record.

Prices are **decimal strings from 0 to 1**, labelled
`price_unit: "payout_fraction"`. Null means unavailable. Kalshi's current dollar
fields take precedence; legacy integer cents are converted only when the dollar
field is absent. Zero is preserved as reported, and may be a venue sentinel for
an empty book; raw fields are retained for inspection. Quote sizes and executable
depth are not validated.

Gamma `outcomePrices` are kept as `indicative_price`, **never bid/ask**. Kalshi
bid/ask and YES last-trade fields stay separate. There is no inferred NO last
price or normalization of different instruments into one common market. An
observation timestamp records when a page was received; it does not establish
when an individual quote last changed. Source metadata update times also do not
prove quote freshness.

`success: true` means every selected source returned at least one valid open
market without ingestion errors. It does **not** mean all markets were covered,
that quotes are executable, or that arbitrage exists. Inspect `stop_reason`:
`exhausted`, `record_limit`, `page_limit`, `offline_fixture`, or `error`.
Limits count raw records examined, including duplicates and skipped records.
Closed/inactive records are skipped; duplicate IDs are kept once per source.
Malformed records are quarantined and make the overall run unsuccessful.

Exit codes: **0** for a successful snapshot, **1** for failed/empty ingestion or
an output error, **2** for invalid arguments. A failed run still writes its JSON
health report and any valid partial results. Offline fixtures are explicitly
synthetic and exercise parsing; they are not evidence of live API availability.
Offline mode reads one bundled fixture page per source, respects `--limit`, and
does not apply live pagination options.

## Validation and failure handling

Local checks on October 6, 2026:

- Deterministic adapter, pagination, HTTP, safety and CLI tests pass.
- Offline CLI ingests one synthetic open market from each venue.
- Live Kalshi CLI ingested **6 unique markets across 3 pages**, with no rejected
  records, using `--limit 6 --page-size 2 --retries 0`.
- Live Polymarket Gamma CLI ingested **6 unique markets across 3 pages**, with no
  rejected records, using `--limit 6 --page-size 2 --retries 0`.
- A live run selecting both sources accepted **2 markets from each** and returned
  `success: true`. These small runs validate ingestion, not trading readiness.

CI runs deterministic tests and the offline demo on Python 3.11 and 3.12; it
does not depend on live venue availability.

The transport uses a 10-second per-request timeout and up to two retries by
default. It retries network failures, HTTP 429 and selected 5xx errors with a
bounded backoff. A `Retry-After` longer than five seconds fails the source instead
of retrying earlier than requested. Redirects are rejected. Response bodies are
limited to 8 MiB, pagination is bounded, and repeated Kalshi cursors fail visibly.
HTTP 403 is reported directly; it never causes fallback to synthetic data.

If a source fails, inspect `sources.<name>.error`, then test that source alone:

```bash
python -m arb --source polymarket --limit 2 --retries 0 --output data/poly-check.json
```

Access can vary by network and venue policy. Keep failures visible and resolve
access through supported channels; do not treat a blocked source as validated.

## Project structure

```text
arb/
  __main__.py       Module entry point
  cli.py            Arguments, exit codes and atomic snapshot output
  http.py           GET-only public endpoint transport
  models.py         Normalized market/outcome schema and price validation
  sources.py        Polymarket and Kalshi response adapters
  ingest.py         Bounded pagination, deduplication and health reporting
  fixtures/         Clearly synthetic source-response examples
tests/
  test_ingestion.py Deterministic unit and CLI checks
.github/workflows/
  ci.yml            Python 3.11/3.12 tests and offline smoke check
```

## Next implementation steps

1. Validate live Gamma ingestion and compare both normalized snapshots against
   venue pages and raw responses over repeated runs.
2. Add read-only order-book data and validate depth, timestamp freshness, fees,
   quote conventions and venue-specific missing-quote markers.
3. Match events using settlement rules, dates, thresholds and outcome definitions
   with explicit review. Similar titles alone do not establish equivalence.
4. Add fee-aware candidate calculations and historical paper checks after those
   data requirements are met. This foundation does not calculate arbitrage,
   execute orders, or make return projections.

Trading requires a separate, explicitly authorized implementation after data
validation; no execution module is scaffolded into this prototype.
