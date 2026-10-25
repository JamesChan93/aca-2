# Continuous operational paper workflow — P4-C1

This is the historical 0.4.10 handoff. In 0.4.22, new CLI selections use the
[versioned three-clock semantics](phase-4a-causal-replay.md); existing immutable
`slow-v1` selections retain the timing described below. Selection still requires
an explicit operational experiment and earns no qualifying credit.

Application candidate 0.4.10, schema `0013_paper_strategy`. This change implements
all five C1 conditions in the [fixed checklist](phase-4-completion.md). It does not
complete C2–C4 or approve G2–G6. The retained local stack has not been upgraded or
started with strategy dispatch. No private Kraken adapter or order writer exists.

## Connected workflow

`aca paper run` opts an explicitly selected virtual account into the existing
one-second supervisor, ordered public replay and slow strategy functions. It uses
persisted public candles, current validated books/rules and the existing sizing,
portfolio policy, reservations, independent dispatch guard and local venue.

Selection records the account, one existing `slow-v1` variant, canonical BTC/GBP or
ETH/GBP, 240/1440-minute interval, entry policy, application version, policy hash and
UTC selection time. Selection is immutable for that account; a different experiment
uses a fresh account. `--operational-experiment` is required. Every selection, status
and runner result denies promotion evidence and grants **zero qualifying days and
episodes**. All 20 research candidates remain rejected; no research registry,
freeze, holdout or promotion state is changed.

The default entry policy is a post-only GTD limit. An explicitly selected FOK uses
the existing bounded marketable-limit checks; it is not an automatic fallback.
Both paths remain subject to minimum size, fees, planned loss and exposure caps.
The simulation still uses its minimum 250 ms latency, participation ceiling,
partial fills, fees, protective stop limits and authoritative local reconciliation.

For each interval, the decision instant is **candle end + 10 seconds**. Its signal
expires two seconds later. The runner never assigns a fresh expiry to an older
candle. It reads a bounded latest REST bootstrap and local candle receipts known
by the observation time, selects up to 201 contiguous complete bars, and records
the prefix hash plus each candle's receipt ID and full-document hash. It uses only
past bars for the existing 200-bar warm-up. Missing, unfinished, conflicting or
gapped evidence skips the action. A later arrival cannot resurrect that decision.

An append-only decision claim commits **before** reserving or cancelling anything.
The account/selection/watermark determines a permanent UUID. The order uses that
same identity. A repeated tick, duplicate candle or restart cannot claim it again.
A crash after the claim can skip an action; it cannot cause a resend. An absent
result means interrupted processing, never permission to retry. The ordinary
consume-before-transmit, UNKNOWN reservation and recovery rules remain in force.
Current metadata/quotes are read again before dispatch. A veto expires the unused
reservation; rules are never relaxed to fit a trade.

A long signal opens a position only when flat and healthy. It does not pyramid.
A flat signal cancels remaining entries, reconciles races, cancels the linked
protection and sells only confirmed owned inventory through the existing bounded
IOC exit. Exit evidence and expiry are rechecked after cancellation. A failed exit
can leave unprotected inventory; reconciliation blocks entries and raises the
existing critical incident rather than pretending the position was closed. A hard
halt still requires an explicit manual position decision and preserves protection.
Restart recovery cancels outstanding entries and preserves protective sells.

## Capture and UTC boundaries

The former 50-minute planned reconnect prevented complete four-hour/daily local
candles. The collector now refreshes unchanged REST metadata in its maintained
connection. Changed rules still require a new epoch and v2 verification; genuine
faults and health gaps still discard incomplete aggregation. A new account can
need a complete observed interval before it has an actionable local closing bar.
No synthetic tradable candle bridges an unexplained interruption.

Ordered replay captures a UTC midnight value when the last healthy public context
precedes midnight by at most five seconds and a valid same-epoch receipt follows
within five seconds. Both books, metadata and health must satisfy their existing
age/status checks **at midnight**. The value uses only the preceding context's bids,
asset balances and controls strictly before midnight. The following receipt's
prices/trades and investment flows stamped at midnight are processed afterwards.
It creates no liquidity or fill.

The embedded boundary record retains previous public/valuation hashes, the next
source anchor, context hash, marks, balances, units, NAV and unit price. Full audit
reconstructs its economics from the preceding financial/control prefix. Old sealed
valuation documents remain readable without rewriting their hashes.

A missing/stale/reconnected bridge keeps the existing daily suspension. An earlier
incomplete historical prefix stays incomplete. Daily summaries supply full-day P&L
and flow-neutral TWR only when both boundary records and the financial/source audit
succeed without incomplete coverage; otherwise they retain unavailable/provisional
fields. This is accounting completeness, not a reliability or strategy gate.

## Local opt-in commands

Use an already reviewed/migrated **fresh development stack**. The ordinary
`python3 scripts/bootstrap.py` remains the fresh-checkout foundation command.
An existing retained database needs the backup/upgrade procedure in
[operations](operations.md); do not point an older running image at the new schema.
This increment did not upgrade the retained stack.

With a new UUID, create and fund the virtual account, then record its selection:

```sh
paper_account=$(python3 -c 'import uuid; print(uuid.uuid4())')
docker compose exec -T app aca paper create --account "$paper_account"
flow_time=$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')
docker compose exec -T app aca paper fund --account "$paper_account" --reference opening --amount-gbp 250 --occurred-at "$flow_time"
docker compose exec -T app aca paper select --account "$paper_account" --strategy sma200 --symbol BTC/GBP --interval 240 --operational-experiment
docker compose exec -T app aca paper enrol --account "$paper_account"
```

Amounts are Decimal strings; funding timestamps must be timezone aware and cannot
precede account state. Reusing a cash-flow reference requires identical original
economics and time. Funding changes investment units, not trading P&L.

Start a **new collector epoch after enrolment**, in a separate terminal/container:

```sh
docker compose --profile market-data run --rm --no-deps market-data aca market capture --duration 0
```

Start the strategy worker in another terminal, using the recorded account UUID:

```sh
docker compose run --rm --no-deps app aca paper run --account ACCOUNT_UUID --duration 0
```

Separate containers avoid health-port collisions with the foundation server. An
already running collector must be stopped first; the database permits only one.
`--duration 0` runs until interrupted; a positive duration is bounded to one day.
The runner requires explicit flat-account replay enrolment and a selection. Stale
capture, replay backlog, unsafe state, loss latches and incomplete candles block
entries. Ctrl-C/SIGTERM follows the existing bounded shutdown and cancels entries.

Read the evidence without acquiring a writer:

```sh
docker compose exec -T app aca paper status --account ACCOUNT_UUID
docker compose exec -T app aca paper decisions --account ACCOUNT_UUID
docker compose exec -T app aca paper performance --account ACCOUNT_UUID
docker compose exec -T app aca paper daily --account ACCOUNT_UUID --day YYYY-MM-DD
```

`decisions` returns the latest 20 claims/results; full records remain in PostgreSQL.
`supervise` retains maintenance-only behaviour and never enables strategy dispatch.
`serve` also remains foundation-only. Discord delivery remains a separately opted-in
worker; this command neither creates a destination nor sends a test message.

## Validation and limits

The integration tests in [test_paper_strategy.py](../tests/test_paper_strategy.py)
use the installed package and disposable real PostgreSQL. They generate public
receipt/candle fixtures; no hand-issued demo order creates the tested position.
They cover signal-driven entry, rounded reservation, latency-delayed fill/fee,
protection, flat exit, writer-session restart/deduplication, four interruption
points, unsafe prefixes, stale feed, loss latches, rules changing before dispatch,
actual daily-loss blocking, and captured/missing midnight accounting. The same
lifecycle account supplies decision status, audited performance and a provisional
daily summary. A separate two-boundary account proves a midday contribution has
zero P&L/TWR impact. These fixtures contribute no qualifying observation time.

[Capture refresh tests](../tests/test_capture_refresh.py) verify unchanged metadata
preserves its epoch while changed rules require fresh verification. Hosted container
smoke creates/funds/selects/enrols a fresh account through the packaged CLI and runs
the opted-in worker without a feed; it must stay blocked. The full connected
lifecycle runs in the packaged Linux test image.

The release result is recorded in [the evidence file](evidence/phase-4-continuous-paper.json).
C3 retains the integrated safety/capacity/timing review and C4 retains the final
packaged handoff and applicable G2 decision. No 56-day reliability, profitable edge,
real account protection semantics or production disaster recovery is established.
