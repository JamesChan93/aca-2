# Phase 4 increment — historical paper valuation and risk

Release candidate 0.4.3, 18 October 2026. Replay now assesses virtual positions at
original public receipt times. A peak, loss and rebound inside one recovered batch
cannot disappear behind the final reconciliation quote. This remains a bounded
paper maintenance increment: Phase 4 and G2–G6 remain incomplete, strategy dispatch
is disabled, every Phase 3 candidate remains rejected, and the final holdout stays
unopened. No private Kraken account access or exchange order writer is added.

## Evidence and chronology

Each enrolled account has a separate append-only `paper_replay_valuations` chain.
Its baseline references an existing flat financial snapshot and public journal
head. Each subsequently consumed envelope records its source number/hash and
receipt time, original-time balances, investment units, GBP bid marks with receipt
provenance, NAV when available, policy hash, high-water marks and risk latches.
The embedded snapshot's `version` identifies the operational baseline; the source
`number` identifies historical progress and `control_version` identifies the last
applied operational investment/operator control. Historical snapshots contain no
order projection and do not replace the operational event or journal authority.

The economic clock is the validated receipt time, when the simulated venue could
observe the data; public trade event time still constrains queue eligibility.
It is never the later batch, restart or reconciliation time. Marks come only from
the replay context at that point, with the existing checksum, epoch, health,
online-rule and freshness checks. A later receipt cannot value an earlier fill.
A delayed heartbeat may retain the last economic state, with NAV omitted and
`late_auxiliary_receipt` recorded. Other regressing market/control times fail closed.
The source receipt time and economic snapshot time remain separate in that case.

Replay observes risk before virtual execution and again after each new fill,
using the shared Decimal journal and unitisation functions. Actual virtual fee
assets and their GBP evidence feed the same accounting arithmetic. This shadow
calculation does not create another financial posting. Authoritative virtual fills
and fees still enter the operational ledger exactly once through reconciliation.

Cash flows and operator halts/reviews are read from immutable operational events
in their original timestamp/version order. Contributions and withdrawals issue or
redeem units using pre-flow marks. A mismatch with the operational unit ledger
stops replay rather than selecting a convenient valuation. A cash flow or epoch
review requires drained public replay and reconciled virtual fills. Duplicate
cash-flow sources retain their existing idempotence behaviour. A first funding
flow establishes its opening daily baseline without claiming an earlier return.

A valid exact UTC-midnight observation establishes the new daily baseline. Missing
midnight evidence suspends the day, rather than inventing its opening price.
Deposits, later quotes and restarts cannot erase soft/hard latches. An explicit
operator epoch review remains the only supported reset, and inception high-water
history is retained. Historical daily baselines propagate to the current account
so a later processing tick cannot replace an observed midnight with a fabricated
one. Existing operator hard-halt reasons survive an unrelated daily reset.

## Safety effects and incomplete periods

On a historical risk breach or unsafe held-asset valuation, replay selectively
cancels remaining **virtual entry** orders before the next executable observation.
Protective sells remain. Trigger latency and stop-limit gap behaviour still apply;
there is no automatic market liquidation or global cancellation timer.

Valuations, virtual fills/protective orders, selective cancellations, operational
risk-latch propagation and the input cursor commit in one PostgreSQL transaction.
A crash before commit rolls them all back. A crash after commit resumes beyond the
same evidence, without resending an order or reposting a fee. Operational events
retain their processing timestamps; they are never rewritten to simulate recovery
having happened earlier. Recovered soft/hard restrictions and high-water marks
propagate into the current policy epoch. A conflicting later epoch requires review.

Held crypto cannot receive a valid NAV from incomplete/stale capture evidence.
Such observations omit NAV and keep the earliest `incomplete_since` permanently in
this history. Subsequent valid quotes allow fresh risk observations, but cannot
repair an unobserved interval or qualify its performance. Missing midnight evidence
also keeps this marker. All valuation query results remain provisional and report
`promotion_evidence=false` and `live_execution_available=false`.

## Upgrade and use

Stop any optional collector/supervisor and back up the local database before
bootstrap. `0008_historical_risk` adds one append-only table with runtime SELECT/INSERT
only; it preserves existing journals, fills, public evidence and replay cursors.
Downgrade is refused. This is a schema-compatible addition, not a backfill of
missing historical risk evidence.

New account enrolment creates both baselines atomically. For a legacy account
already enrolled under 0.4.2, first drain its outstanding source journal using the
0.4.2 image if necessary. It must then be flat, have no outstanding orders, and have
all authoritative virtual fills reconciled. After upgrading, repeat `paper enrol`
to add the historical baseline at the **existing** cursor. It neither resets the
cursor nor reconstructs earlier risk. An account with holdings, outstanding work
or an unread legacy suffix fails closed; preserve its evidence and resolve it
through reviewed recovery rather than rewriting a cursor.

For an existing flat virtual demo account:

```sh
python3 scripts/bootstrap.py
docker compose exec -T app aca paper enrol --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper valuations --account 6e04530b-9382-418d-9a24-331672f4016b --limit 10
```

Continue with a fresh collector epoch and the `supervise --replay` procedure in
[ordered replay](phase-4-replay.md). The default Compose services still schedule
neither worker. A baseline-only account has no new market observation evidence.

`paper valuations --limit 1..100 [--before SOURCE_NUMBER]` is a read-only query.
Results are newest first; pass `next_before` to read an earlier page. Each returned
row's hash, policy, identity and available NAV arithmetic are validated. This page
is not a full chain audit. Replay/supervisor startup audits the complete financial,
cursor and historical chains in bounded pages, including historical source
coverage, hashes and cursor agreement. Empty batches validate their latest
historical anchor without adding records.

## Decisions and remaining work

**A35 — independent historical economics.** Keep original-time risk evidence
separate from the monotonic operational projection. Reuse shared accounting and
risk functions; do not post the same economic event from both projections.

**A36 — historical risk acts within the replay transaction.** Cancel remaining
virtual entries at the observed breach, retain protection and atomically propagate
restrictions to the current account. A final recovered quote cannot override an
intermediate loss. Nothing here grants a Kraken mutation capability.

**A37 — explicit flat upgrade baseline.** Existing ordered evidence cannot prove
that earlier risk was monitored. Require an explicit flat/reconciled boundary
without deleting or replacing the old cursor or asserting retroactive completeness.

Batches still cap at 64 public envelopes, 256 canonical trade updates per frame,
1,024 permanent venue orders, 4,096 retained fills and 2 MB retained venue state.
Historical control reads cap at 256 relevant events; overflow stops processing.
Each consumed receipt adds a compact valuation record, including during flat
maintenance. Storage grows and the existing 2 GB database budget still stops
operation. Full-prefix audits and per-observation SQL work also grow; this increment
is not a throughput, archive/retention or long-duration resource qualification.

Historical tax inventory, transfers/conversions/corrections, a full performance
report rebuild, calibrated fills/latency, strategy scheduling, outbound Discord,
process/disk fault trials and disaster restore remain outstanding. An historical
risk chain does not establish alpha, eight-week reliability, signal-driven episode
counts or economic viability. No strategy is promoted by these changes.

The implementation uses existing SQLAlchemy async connections within one owning
transaction, following the [SQLAlchemy asyncio documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).
See [verification evidence](evidence/phase-4-historical-risk.json) and the pull
request's exact-head hosted checks for measured results and their limits.
