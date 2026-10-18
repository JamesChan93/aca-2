# Phase 4 increment — ordered public replay

Release candidate 0.4.2, 18 October 2026. This increment adds an opt-in consumer
of committed public evidence for an existing virtual account. It does not complete
Phase 4 or G2, qualify a strategy, establish an eight-week paper record, or add
private Kraken access or a real exchange writer.

The subsequent [0.4.3 historical-risk increment](phase-4-historical-risk.md) adds
original-observation NAV and risk evidence. This document describes the 0.4.2 baseline.

## Behaviour

`MarketStore` now publishes one replay envelope per append transaction, alongside
the immutable public receipt and canonical trade inserts. A PostgreSQL transaction
advisory lock serialises publication through commit. The journal's increasing
numbers are suitable for a consumer cursor; random receipt UUIDs and wall-clock
timestamps alone were not. Rolled-back sequence numbers may leave gaps. A later
committed row cannot overtake an earlier uncommitted append through this port.
Auxiliary timestamp-alignment receipts stay associated with their parent append;
they supply no executable liquidity.

Only first-seen WebSocket trade **updates** enter the liquidity envelope, in their
original frame order. Snapshots, duplicate IDs and REST history/recovery trades do
not. Historical candles and raw REST bodies remain in their original receipt;
their large normalised collections are not copied into replay envelopes.
Pre-0006 receipts have no proven commit order and are never silently backfilled.

An account must explicitly enrol while flat, without pending local or venue
orders. Its baseline records the current journal head. A new collector epoch after
enrolment supplies rules, books and health. Repeated enrolment returns the existing
cursor; it cannot reset progress or hide an order. An enrolled account advances
through its durable cursor, rather than the synthetic `advance` entry point.

Each batch consumes at most 64 envelopes. The cursor records the account, source
anchor, epoch, last books and their receipt identities, trade time watermarks,
rules and health. Queue changes, virtual fills, protective child orders and the
next cursor commit in **one transaction**, under the account writer and venue
locks. A crash before commit rolls everything back. A crash after commit resumes
after that cursor; subsequent reconciliation imports each authoritative virtual
fill and fee once. The account is suspended before a non-empty batch starts.
Financial import is a separate idempotent transaction and can safely lag the venue.

Replay requires validated same-epoch books for both pairs, current online rules,
a capture-health observation no older than ten seconds and quotes no older than
five seconds at the observation. A trade must not precede the book's event time
(or receipt time if the snapshot has no event timestamp), or the last applied
trade time for its instrument. Such late trades supply no liquidity and reset
queue credit. Distinct IDs at equal timestamps remain distinct economic events.
Distinct book receipts at equal timestamps have separate observation identities.

A disconnect, coverage gap, unhealthy status or epoch change discards the affected
context and resets passive queues. Corrupt hashes, unknown channels, malformed
financial values, future receipts and regressing book timestamps stop replay
without advancing its cursor. No fallback fabricates depth, fills or protection.
A receipt committed after the decision cutoff defers the entire remaining suffix
until the next tick. `deferred` reports such a suffix separately from `more`, which
reports backlog at the decision cutoff. GBP mark queries use the same cutoff. Its database insertion timestamp distinguishes that normal
concurrency from a receipt more than 500 ms ahead of publication. The timestamp is
an insertion observation, not a commit timestamp; existing 0006 rows receive the
migration's observation. The existing 250 ms minimum latency, 1% participation policy, partial fills,
protective minimum checks and unfilled stop-limit gaps remain in force.

Venue submissions/cancellations check the public backlog while serialised with
publication. A command cannot overtake already committed unread evidence. A delayed
receipt that predates a recorded cancellation is a review fault rather than a
claim that the cancelled order could not have filled. No automatic command retry,
cursor skip or reset is provided.

## Venue read projection

Venue snapshots select the latest immutable event for each permanent client ID
and the authoritative fills, using partial indexes. They no longer materialise
all public book/trade observations on every snapshot. Repeatable-read snapshots
keep the order and fill queries consistent. Last-book checks use a separate index.
The indexed nested JSON expression uses PostgreSQL's subscript syntax, matching
SQLAlchemy 2.1's rendered expression.

Retained venue state has explicit limits: 1,024 permanent orders, 4,096 fills and
2 MB serialised state. Limits reject additional effects atomically; they do not
prune client IDs, reuse old IDs or relax risk. While flat, a batch reads the venue projection once, and the cursor retains
market context without writing duplicate virtual venue observations.

This bounds application result size. It is not proof of fixed database query cost
or a completed resource/retention trial. Immutable records still grow, and the
existing 2 GB database budget stops operation. Future archive/retention work must
preserve the audit and recovery contracts before extended paper validation.

## Local commands

Back up the existing database before running `python3 scripts/bootstrap.py`.
Migrations `0006_replay` and `0007_replay_cutoff` are additive, grant runtime only SELECT/INSERT and sequence
USAGE, and retains all financial, research and public records. Downgrade is refused.
See [operations](operations.md) for protected local dumps and recovery limits.

The following example uses an already completed, flat synthetic demo account.
Choose the UUID of the intended existing **virtual** account. Stop any collector
before enrolment so its next start creates the required fresh epoch.

```sh
docker compose exec -T app aca paper audit --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper enrol --account 6e04530b-9382-418d-9a24-331672f4016b
```

Start the following in separate terminals. Each one-off container has its own
internal health port. Use `run`, since `exec` into the foundation container would
compete for its already bound health listener.

```sh
docker compose run --rm --no-deps app aca paper supervise --account 6e04530b-9382-418d-9a24-331672f4016b --replay --duration 300
docker compose --profile market-data run --rm --no-deps market-data aca market capture --duration 240
```

A finite administrative batch is also available:

```sh
docker compose exec -T app aca paper consume --account 6e04530b-9382-418d-9a24-331672f4016b --limit 32
```

Only one execution owner can operate an account. Do not run `consume` beside its
supervisor. Startup audits the complete cursor chain in 64-row pages and checks
source anchors. Recovery drains at most eight 64-frame batches within the existing
ten-second startup deadline before ordinary recovery/cancellation. Tick processing
retains the three-second deadline; a backlog blocks readiness. An enrolled account
requires `--replay`; restarting maintenance without it fails closed.

If backlog exceeds the startup budget, use finite `consume` batches with the
supervisor stopped, investigate why it accumulated, then restart. An invalid
source/cursor or cancellation chronology fault needs review; repeatedly restarting
or rewriting a cursor is not recovery. Protective orders remain virtual venue
records. Capture failure stops simulated liquidity; it does not liquidate a
position or infer that an unobserved exchange price filled a stop.

`/health/paper` reports maintenance/replay readiness. The status adds the committed
cursor, processed-frame count, skipped-update count and `fill_replay_enabled`.
Metrics expose those three counters/gauges with the `aca_paper_replay_` prefix.
`/health/trading` remains 503; `aca_trading_ready` stays zero. All reports retain
`strategy_dispatch_enabled=false`, `promotion_evidence=false` and
`live_execution_available=false`. Default Compose startup schedules neither worker.

## Decisions and limits

**A32 — commit-ordered public journal.** Own the small PostgreSQL journal and
append-only account cursor; do not infer arrival order from timestamps, UUIDs or a
sequence allocated outside a serialised commit boundary. Capture writes use the
`MarketStore` port; arbitrary SQL insertion is trusted internal/administrative code,
not a supported ingestion or strategy extension mechanism.

**A33 — replay virtual venue effects before financial recovery.** Separate durable
venue effects from idempotent financial import, and commit venue effects with the
input cursor. Never replay an uncertain order command as part of data recovery.

**A34 — bounded immutable venue projection.** Query latest order events and fills
using indexes, retain identities and cap state. A mutable cache would add another
repair path. Database and long-duration resource evidence remain required.

No strategy dispatch is added. All Phase 3 candidates remain rejected and the final
year remains unopened. A maintenance session on a flat demo account cannot produce
signal-driven position episodes or count as extended paper validation.

Backlog replay posts original fill timestamps but observes financial risk at
reconciliation time. A historical, per-observation NAV/risk reconstruction and
calibrated latency/liquidity model are still required for qualifying performance
and paper evidence. Large trade frames above 256 canonical updates stop replay;
no frame is silently truncated. These bounds need operational calibration before
promotion. This release is not evidence that the paper model matches Kraken
conditional protection, especially for small partial fills.

Remaining Phase 4 work includes shared strategy scheduling without promoting a
rejected candidate, complete accounting workflows and historical performance
rebuilds, outbound notification delivery, resource/fault trials and disaster restore.
G2–G6 are not passed by this increment. Live implementation, paid provisioning and
activation remain separately authorised work.

The transaction and isolation implementation follows the
[SQLAlchemy asyncio documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
and [PostgreSQL dialect isolation guidance](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html).

## Verification evidence

See [the verification record](evidence/phase-4-replay.json) and the PR's exact-head
hosted checks. The suite covers crash rollback/commit boundaries, financial
idempotence, permanent IDs, first-seen updates, queue resets, stale/future/corrupt
sources, equal-time identities, cancellation chronology, stop-limit gaps, bounded
batches and recovery by a new consumer. Existing financial properties and 10,000
lifecycle fault traces remain required; these are not 10,000 real process kills.
