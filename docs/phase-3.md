# Phase 3 — historical audit and reproducible research

This milestone adds local research after merged Phase 2. It does not implement the
Phase 4 accounting ledger, independent execution risk, reservations, order lifecycle,
paper exchange, reconciliation or operational recovery. Private account access and
every exchange write remain absent. No paid data or infrastructure is provisioned.

## Local workflow

Use standard GIL-enabled CPython 3.14.8. The documented foundation bootstrap stays
`python3 scripts/bootstrap.py`; research is a separate local process:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/research.lock
.venv/bin/python -m pip install --require-hashes -r requirements/build.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/aca research fetch --directory data/research/source
```

The fetch command prints a source JSON path. Use that returned path for import,
then the returned content-addressed manifest path for the experiment:

```sh
.venv/bin/aca research import --source SOURCE_JSON --root data/research
.venv/bin/aca research run --dataset DATASET_MANIFEST --root data/research
.venv/bin/aca research trials --root data/research
```

`SOURCE_JSON` and `DATASET_MANIFEST` are placeholders for those local paths. The
default bootstrap count is 10,000; deterministic PCG64 seed 1729 and the whole trial
family are recorded before data loading. Development verification installs both
`development.lock` and `research.lock`. The normal runtime image intentionally omits
NumPy; it can publish/query evidence without importing statistical code.

The fetcher reads only the selected BTC/GBP and ETH/GBP 240/1440-minute members and
the archive manifest. It validates HTTP 206, exact Content-Range, unchanged strong
ETag, TLS, CRC32 and member size, with 64MB/200-request limits and no redirects or
proxy configuration. It never extracts arbitrary ZIP paths. No full exchange
archive is downloaded implicitly. `research fetch --archive FULL_ZIP` instead
verifies a locally assembled ZIP against the published full SHA-256 before reading.

Source JSON records retrieval time, URL, usage note, hashes, selected member CRCs,
part sizes/ETags and assurance level. Keep source CSVs, source JSON, archive manifest,
`objects/`, `datasets/`, `events/` and reports together. These ignored local artefacts
are not a substitute for operational backups or seven-year financial records.

Publish a read-only copy to the local PostgreSQL registry after bootstrap/migration:

```sh
docker compose run --rm --no-deps -v "$PWD/data/research:/research:ro" app \
  aca research register --root /research
```

Publication is idempotent by hash/event ID; conflicting IDs fail. Runtime has only
SELECT/INSERT. Migration `0003_research` adds append-only research artefacts and trial
events; UPDATE/DELETE/TRUNCATE are rejected. Each transaction inserts an artefact and
its event together. Previously committed events remain durable if a later batch item
fails. Local start records remain evidence of interrupted/abandoned work; a failed
run never deletes earlier records. Unexpected computation errors fail the run and
append failure evidence rather than being reclassified as missing market data.

## Protocol and simulator

The exact family is five variants × BTC/ETH × daily/4-hour = 20: SMA200/SMA100,
Donchian55/20, momentum90/momentum60. Signals require 200 closed warm-up bars,
exclude the current Donchian channel bar, propose max(2 × simple ATR14, 5% of price),
and carry a data watermark, decision time and two-second expiry. Strategies/features
have tested import boundaries and no venue/network/storage/credential dependency.

The historical reference uses Decimal precision 50. Shared sizing rounds quantities
down and checks fees, free cash, 0.5% episode risk, 15% asset/30% total exposure,
1% aggregate risk and both entry/protection minimums. No pyramiding or minimum-driven
risk relaxation is allowed. It is a reference function, not an independent execution
approval guard. Position limits and risk state apply to one candidate instrument;
multi-strategy/multi-asset operational allocation remains Phase 4.

Signals first execute at a full bar opening after close+10 seconds. At that open,
the strategy is freshly evaluated using only the eligible older prefix. The opening
price plus adverse slippage approximates a post-latency taker fill; it does not
claim an opening print could be filled after latency. This full-bar lag is
deliberately conservative. Entry sizing uses earlier known volume; realised volume
only caps fills at 1%. Exits are participation-limited and preserve residuals.
Two-second stressed latency expires the two-second entry permission, so that stress
can produce cash; it does not demonstrate profitable execution under a two-second delay.

Default costs are 0.8% taker per leg, 0.1% spread/slippage per leg, 0.2% additional
planned-loss allowance and a protective limit 1% below its trigger. Prices and fees
are assumed GBP for this bar study; actual fee assets require Phase 4 reconciliation.
No candle touch earns a maker fill. Stop triggers are observed conservatively; they
cannot fill until a later opening bid permits the limit. A gap can leave protection
unfilled. A strategy exit can reduce confirmed simulated inventory; no real order,
cancel guarantee or partial-fill child-order guarantee follows from this model.
Terminal positions stay marked to an adverse GBP bid instead of receiving a fictitious
sale. Fees, turnover, completed episodes, residuals and unfilled stops are reported.

Reference drawdown latches use flow-neutral units, never raw NAV. Contributions issue
units at a pre-flow mark; they cannot clear a soft/hard latch. UTC daily suspension
resets only its own flag. Bar history observes risk at opens/closes, not once per
second. Every fold starts flat with independent research state; this is not permission
to reset a live policy epoch. Operational durable latches remain Phase 4.

Walk-forward windows have 24 months training, six validation, six test, advancing
six months, with 201 pre-window warm-up bars. Fixed variants are not fitted on test
returns. Final 12 months remain reserved. Every eligible calendar fold is retained;
missing boundary, training, warm-up, test or blend data marks it incomplete. A successful
subset cannot supply a qualifying bootstrap bound. The four-year, two-year OOS and
50-completed-episode requirements are reported separately.

Each valid window reports cash, same-asset buy/hold, 10%/30% crypto, £250 monthly
purchases and a BTC/ETH 50/50 buy/hold blend with two £125 legs. The monthly-purchase
benchmark receives extra external contributions; use TWR/MWR rather than comparing
its terminal NAV directly with an uncontributed candidate. The blend and full crypto
benchmarks carry different risk from the capped candidate.

Metrics include return, TWR, MWR/XIRR, drawdown, volatility, Sharpe/Sortino/Calmar,
profit factor, turnover, exposure, fees, expected shortfall, calendar-year returns
and past-only SMA200 trend regimes. Undefined metrics stay null. XIRR accepts normal
investor cash-flow sign patterns; other/non-unique patterns remain unavailable.
NumPy floats are restricted to statistical outputs, never sizing or cash balances.
Paired circular block bootstrap uses 7/30-day sensitivity, the same dates for both
curves, and a one-sided Bonferroni lower bound across 20 × two mandatory comparisons × two block sensitivities.
Its annualised arithmetic return difference is an estimate under the sampled history,
not a guarantee. Predeclared fee1.5×/spread2× and latency2s stress remain separate.
Neighbouring-parameter and regime stability require review; no automatic selection
or promotion is implemented.

The economic function reproduces the supplied central ten-year scenarios and
separately records infrastructure spending. The hurdle is lower-bound incremental
benefit ≥ £480/year at £240 fixed costs; equality passes its arithmetic only. None
of this arithmetic influences position size or the £1m aspiration.

## Holdout

Only after candidate/cost review, create a freeze then use the explicit holdout action:

```sh
.venv/bin/aca research freeze --report RESEARCH_REPORT --trial BTC/GBP:1440:sma200 \
  --review "Reviewed candidate and fixed assumptions" --root data/research
.venv/bin/aca research holdout --dataset DATASET_MANIFEST --freeze FREEZE_JSON \
  --root data/research
```

These are workflow examples, not a recommendation to open the current holdout.
The freeze binds exact report, dataset, code and cost hashes. Holdout opens once per
dataset artefact tree, across candidates; a failed/crashed opening remains used.
Changing the output root cannot reopen that same dataset tree. This is trusted local
coordination, not protection against an operator deleting/copying its files. Copy
events to PostgreSQL for independent append-only evidence. Freeze/holdout records
always deny execution permission. No current candidate has been frozen or evaluated
on the actual final year.

## Observed evidence and remaining gates

See `docs/evidence/phase-3-*` for the exact source hashes, dataset audit, complete
trial/fold summary and release validation. The official archive's manifest/CSV end
in September 2026 while the fetched support page describes June. Treat that conflict
as unresolved. Selected member CRCs and local SHA-256 are verified; the roughly 9GB
full archive SHA-256 has not been verified. Missing intervals remain unexplained.

The importer found an early volume `0.00033370000000000003`; only volume is rounded
down to 18 decimal places for the bounded domain, with raw bytes and transformation
recorded. It never rounds historical price inputs silently. This conservative volume
transform can reduce modelled liquidity and cannot grant additional size.

All 20 actual candidates are rejected for promotion. Some later windows produce
descriptive results, but incomplete windows are retained and prevent selection
inference. No statistical advantage, positive economic gate or G3 pass is claimed.
G1 authenticated account evidence and all G2–G6 gates remain unresolved/unpassed.
The intended next implementation is Phase 4, subject to milestone review.

Primary sources: [Kraken archive format and downloads](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data),
[published part checksums](https://assets.kraken.com/marketing/institutions/OHLCVT_Full_PARTS_SHA256SUMS.txt),
[NumPy PCG64](https://numpy.org/doc/stable/reference/random/bit_generators/pcg64.html),
[NumPy 2.5.4 package](https://pypi.org/project/numpy/2.5.4/).

## Test mapping

T00 uses the supplied central projection fixtures, fee arithmetic and fixed-cost
hurdle equality. T01–T04 preserve source hashes/order/duplicate/gap/finalisation
checks; Phase 2 feed fault tests remain in the suite. T12–T13 include 1,000 generated
sizing cases, down/up rounding, both minimums and no risk relaxation. T15/T16/T29
cover research unitisation and latches through contributions; operational one-second
risk and durable restart semantics remain Phase 4. T23/T26 cover pure signals,
future prefixes, import boundaries and fake-venue independence. T32 covers the
packaged migration, restricted runtime role, idempotent research publication,
conflicts and restart persistence. Holdout and seeded replay tests exercise the full
predeclared family on explicitly synthetic data. These tests do not claim G2
execution safety, actual fee-currency accounting or exchange-side protection.
