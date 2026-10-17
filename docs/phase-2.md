# Phase 2 — public Kraken market data

This milestone implements credential-free public Spot reads for BTC/GBP and ETH/GBP.
It follows the merged Phase 1 foundation. Account reads are optional in the plan and
remain deferred: no account key has been supplied, requested or mounted. No private
REST/WS transport, signing, nonce generation or exchange write method is present.

## Implemented boundary

- Immutable canonical market records, separate discovery/history/stream protocols,
  explicit REST pair and asset bindings, positive rule constraints and schema v1 DTOs.
- HTTPX public GETs to eight allowlisted endpoints on api.kraken.com. The collector
  uses AssetPairs with country_code=GB, SystemStatus, Time, OHLC and Trades. Ticker,
  Depth and Assets are accessible through the public transport; normalised live books
  come from WebSocket v2. No authenticated eligibility conclusion follows from public data.
- Public instrument, depth-10 book and trade subscriptions. Decimal-aware JSON rejects
  duplicate keys, NaN/Infinity, unsupported precision and malformed safety fields.
  Vendor additive fields are tolerated; unknown critical statuses/types are rejected.
- CRC32 validation after every book snapshot/update, deletion handling and depth
  truncation. Corruption, regressed book timestamps and reconnects discard all books.
  Fresh REST/v2 rules must agree before feed readiness; rules refresh through a new
  recovery epoch before an hour elapses.
- Permanent PostgreSQL public trade identity `(venue, symbol, trade_id)`. Equal prices
  and timestamps do not collapse different trades. Conflicting duplicate economics
  roll back the entire receipt. An observed public REST/WebSocket timestamp rounding
  difference of at most one microsecond is accepted only across those two protocols,
  with exactly equal price/quantity/side and an additional append-only alignment receipt.
  Same-protocol or larger differences fail. This does not establish private fill identity.
  Raw WebSocket receipts and inserted trades commit
  together. REST raw responses precede explicit normalised receipt links by epoch,
  hash and observation time; raw responses remain available for reprocessing.
- Append-only raw/normalised receipts, SHA-256 payload hashes, receive/event times,
  connection epochs, recovery markers and quarantines. Runtime cannot modify history.
- Closed daily/4-hour REST candles, always excluding the API's final unfinished row
  and enforcing a ten-second finalisation delay. Missing intervals are recorded as
  unexplained, never filled with invented executable prices.
- Local event-time OHLC aggregation from newly inserted trades. Healthy continuous
  segments establish coverage; first partial intervals are flagged. Missing intervals
  remain absent; late trades after finalisation interrupt capture rather than silently
  revise a published bar. Equal-time trades use receipt time then ID as a deterministic
  tie-breaker. No strategy or signal consumes these candles yet.
- Bounded overlapping recent trade recovery. Initial recent reads **omit** `since`:
  Kraken interprets `since=0` as old history. After a gap longer than a day, record an
  incomplete bounded recovery instead of trying to reconstruct an archive. At most
  20 pages are fetched per instrument; cursor regression/budget exhaustion interrupts
  capture. No reconnect snapshot is labelled complete history.
- A separate single public-capture owner lock, bounded queues/messages/requests,
  no redirects/proxies, public request spacing of at least five seconds and at most
  three read attempts. Retry-After is honoured; an excessive throttle deadline stops
  that attempt instead of retrying early. Unknown API errors are not retried by the
  transport. The recovery supervisor backs off up to 60 seconds.
- Book freshness (five seconds), connection freshness (ten seconds), database safety
  and REST health checks. Capture health is separate from foundation/trading health.
  REST Time's whole-second precision is represented by an offset interval; this is
  explicitly **not** proof of the future <=500ms live activation requirement.
- Opt-in Compose egress profile; the default app/database remain on the internal
  network. No ports are published. Capture mounts only the runtime database secret.

## Operate locally

```sh
python3 scripts/bootstrap.py
docker compose --profile market-data run --rm --no-deps market-data aca market capture --duration 300
docker compose exec app aca market status
docker compose exec app aca market evidence --channel instrument_rules --limit 1
docker compose exec app aca market evidence --channel trade_recovery --limit 2
```

The finite duration includes REST bootstrap. A very short run may not reach the
WebSocket stage; zero healthy observations produces exit code 1. Duration 0 runs
until SIGINT/SIGTERM. `market status` describes stored evidence, not current network
health, and deliberately never grants readiness from the last stored receipt.
Both approved instruments are required for this capture version.

For a supervised local session:

```sh
docker compose --profile market-data up -d --no-deps market-data
docker compose exec market-data aca health --url http://127.0.0.1:8000/health/market
docker compose logs --tail 50 market-data
docker compose stop market-data
```

Bootstrap must run first. Capture has restart disabled; after a database failure or
process exit, inspect evidence before starting it again. Transient public-feed faults
recover in-process with fresh snapshots and explicit gaps. A collector cannot start
while another holds the same database's public owner lock. This lock provides no
exchange-side execution fencing and is not the future execution writer lock.

Database size is capped at 2GB for this local capture. Capacity is checked at startup
and with periodic REST health. This is a bounded development capture, not a promise
of 30 days' tick retention. Stop/archive before the cap; automatic archival/pruning
and disk/RPO/restore engineering remain later work. History cannot be deleted by runtime.

## Validation and remaining gates

See the dated evidence files under `docs/evidence/phase-2-*` for actual results.
The offline suite includes the official CRC32 golden example, 1,000 generated book
precision cases, malformed/future/stale events, rate limits, bounded timeout retries,
changed metadata, partial capture intervals, duplicate/conflicting trades, migration,
restart persistence, append-only enforcement and storage/disconnect invalidation.
No ordinary test makes a network request or uses an exchange key.

| Plan test | Phase 2 evidence / limit |
| --- | --- |
| T01 | Duplicate receipts/trade IDs insert one canonical trade; equal timestamps remain distinct |
| T02 | Event-time OHLC, late-finalised rejection, regressed book time rejection |
| T03–T04 | Disconnect invalidation, fresh snapshot requirement, stale/checksum rejection |
| T05–T06 | Public read timeouts have bounded retries; server throttle/spacing tested; mutating commands do not exist |
| T13 | Refreshed rules and REST/v2 minimum conflicts tested; executable sizing remains Phase 4 |
| T14/T17 | No valuation/account state is invented; all trading remains unavailable |
| T18–T20 | Failed storage blocks capture; no private credential path; full disk/auth fault suite remains future work |
| T21 | Maintenance/unknown status blocks public readiness |
| T26 | Venue-neutral records and read ports introduced; full shared-strategy adapter acceptance deferred |
| T31 | Exact JSON/UTC and conservative time evidence; private nonce/signing remain absent |
| T32 | Clean baseline/upgrade tests, restricted runtime role, wheel/container checks |

G1 is demonstrated for the **public-data portion**, with dated local/CI evidence.
Authenticated account eligibility, permissions, fees and balances are unresolved.
There is no claim that a short public capture demonstrates 99.5% paper reliability,
56-day observation, historical continuity or an economic advantage. All G2–G6 remain
unpassed. Research, accounting, risk enforcement, reservations, paper fills, order
recovery, tax reporting, notifications and live activation remain unimplemented.

## Primary contracts checked

- [Kraken AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
- [Book checksum v2](https://docs.kraken.com/exchange/guides/websockets/book-checksum-v2)
- [Book channel](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book)
- [Trade channel](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade)
- [Instrument channel](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/instrument)
- [REST OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
- [Recent trades](https://docs.kraken.com/api-reference/market-data/get-recent-trades)
- [HTTPX 0.28.1](https://github.com/encode/httpx/tree/0.28.1)
- [websockets client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)

## Recorded release validation — 17 October 2026

- Native macOS arm64 + PostgreSQL: **129 passed** (8.35s).
- Non-root Linux arm64 musl test image + PostgreSQL: **129 passed** (8.85s).
- Hosted Linux amd64 musl + PostgreSQL: **129 passed** (10.40s), with formatting,
  lint, strict typing, both dependency audits, distributable build, clean bootstrap,
  restart/persistence smoke and both image scans successful. The first source run is
  [39070975258](https://github.com/JamesChan93/aca-2/actions/runs/39070975258);
  machine-readable step results are in
  [phase-2-hosted-ci.json](evidence/phase-2-hosted-ci.json).
- The release image's installed Python sources exactly match commit `30887a0`.
  The source hash and clean local image scans are in
  [phase-2-images.json](evidence/phase-2-images.json). Dependency declarations/audits
  are in [phase-2-dependencies.json](evidence/phase-2-dependencies.json).
- Default app restart preserved audit history and remained unable to trade. The
  collector alone had egress and only the runtime database secret. Inspection is in
  [phase-2-containers.json](evidence/phase-2-containers.json).

The credential-free public trial includes an intentional local egress interruption.
Market health returned 503 while books were invalidated; restoring egress created a
new epoch and returned to healthy status only after fresh snapshots. The trial and
its exact frame/trade/fault counts are recorded in
[phase-2-public-capture.json](evidence/phase-2-public-capture.json). The 300-second
trial received 12,073 WebSocket frames, recorded zero checksum failures, deduplicated
197 stream trades and inserted nine new stream trades. Its one fault/reconnect was
the intentional network interruption. Active-loop observations exclude bootstrap and
backoff; they are not an availability denominator.
This controlled short trial is operational evidence, not a paper-promotion or
research-performance result. Earlier diagnostic cursor/timestamp failures remain
preserved locally; their corrections are documented above and covered by regression
tests. No exchange order or account read was part of validation.
