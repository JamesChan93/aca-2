# Phase 4 — checkpointed projections and paper maintenance

This increment follows the merged offline core (PR #4). It removes full-history
reads from normal state changes and adds an opt-in one-second **maintenance** loop
for an existing virtual account. It is a reviewable part of Phase 4, not completion
of the phase, G2, or authorisation to start the eight-week qualifying paper period.

Strategy dispatch and continuous fill replay remain disabled. The runner does not
generate entries, turn a candle touch into a fill, replay a latest-book snapshot as
a complete trade stream, or select a rejected candidate. It marks holdings using
validated public GBP bids, checks loss limits, selectively cancels entry orders and
reconciles the already existing local venue. Kraken private access/order writes,
live activation, Discord delivery and paid provisioning remain absent.

## Upgrade and use

Stop any collector, replay and maintenance process before upgrading. Take a private
PostgreSQL dump with the same administrative process used for earlier milestones.
Then `python3 scripts/bootstrap.py` builds 0.4.1 and migrates through
`0005_checkpoints`. The migration adds checkpoints and indexes without rewriting
old events, journals, fills, market receipts or research records. No dependency or
base-image version changes are introduced. Downgrades remain deliberately refused.

Use an existing **virtual** account UUID. A fresh `paper demo` creates virtual funds
and a synthetic closed episode, as in the original runbook; it has no promotion
weight. Never enter an exchange account identifier or credentials here.

```sh
docker compose exec -T app aca paper audit --account ACCOUNT_UUID
docker compose run --rm --no-deps app aca paper supervise --account ACCOUNT_UUID --duration 60
docker compose exec -T app aca paper status --account ACCOUNT_UUID
```

`--duration` is 0 (until SIGINT/SIGTERM) or 1–86400 seconds. The maintenance command
uses a separate one-off container with the internal network, runtime database
password and existing non-root/read-only/resource restrictions. It has no public
network, published port or automatic restart. Default bootstrap starts neither it
nor the collector. Run the public collector separately using the Phase 2 profile
if fresh public valuations are desired:

```sh
docker compose --profile market-data run --rm --no-deps market-data aca market capture --duration 300
```

The supervisor opens its health server inside its own container; do not use
`compose exec app ... supervise`, which would conflict with the foundation's port.
`GET /health/paper` reports maintenance readiness; `/health/trading` stays 503 and
`aca_trading_ready` stays zero. Health includes account ID, reason, observation
counts and tick duration; `/metrics` adds maintenance readiness, tick duration and
healthy/unhealthy observation counters. Neither endpoint accepts commands.

Starting maintenance acquires the existing per-account writer lock, audits history,
recovers without resubmitting consumed commands, and cancels outstanding entries.
It preserves protective sells. Another replay, review, halt or writer cannot run
concurrently; stop maintenance before an administrative mutation. Status, audit,
journal and alert queries remain read-only. An external termination that prevents
shutdown is recovered on the next owner acquisition; no process restart grants a
live capability.

SIGTERM/SIGINT interrupt the one-second wait immediately. Shutdown attempts a
bounded durable suspension and selective entry cancellation. A failed database
write is logged locally, and no new unaudited mutation can be transmitted. This
does not implement an emergency exchange UI, exchange fencing or disaster recovery.

## A30 — field deltas and sealed checkpoints

New `paper_events.document` values use `paper_delta_v1`: changed top-level fields,
a canonical resulting-state SHA-256, and an optional journal UUID. The event's
existing chain hash binds that envelope, source, kind, version and previous hash.
Order lists are included when they change; there is no order-history deletion.
Legacy full-state events remain readable with their original hashes.

The first new event for an account, and every version divisible by 128, stores a
full typed state in `paper_checkpoints` in the **same transaction** as its event,
journal, canonical fill and alert work. Checkpoints are append-only; the runtime
still has SELECT/INSERT only. PostgreSQL binds the account/version/event hash and
resulting-state hash to the corresponding event. Application loading recomputes
the state hash and anchor hash. The unique event journal index prevents reusing a
journal in another delta. Failed transactions cannot leave a committed checkpoint.

The normal reader validates the newest checkpoint and at most 127 subsequent
events. It checks every financial delta against its linked sealed journal.
Incremental balances apply that journal to the previously verified balances at
Decimal precision 50. They do not reread all earlier journals for a mark or fill.
The explicit `paper audit` and every writer acquisition stream the complete hash
chain in 128-row pages, verify all checkpoint anchors, replay financial deltas and
compare final balances to independent SQL aggregates of sealed journal lines and
asset movements. Read-only audits/CLI queries use repeatable-read transactions;
writer transactions retain the dedicated connection and verify its backend PID.

This relies on PostgreSQL immutability, the startup audit and ordinary trusted
application code. Hashes are not an external tamper-proof archive. An administrator
with control of the host/database remains outside that guarantee. No state cache
survives connection loss or bypasses writer ownership. SQLAlchemy's
[async connection rules](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
and [isolation-level guidance](https://docs.sqlalchemy.org/en/20/core/connections.html)
inform the transaction handling.

Current state is capped at 1,024 retained orders and 2MB JSON. Crossing a cap rejects
the next state before dispatch; it never drops terminal identities or relaxes a
risk rule. A reviewed archive/checkpoint evolution is required beyond those bounds.
Explicit historical journal export and performance reporting still read financial
history. The independent simulated venue still reconstructs its bounded operational
history; it does not receive a new public book every second from this supervisor.
Continuous fill replay needs its own durable cursor, complete ordered trades, venue
projection and restart/queue/liquidity validation.

## A31 — maintenance is not strategy promotion

The public collector publishes a small `capture_health` receipt every five seconds
and on a health transition. Maintenance requires a healthy receipt no older than
ten seconds, current same-epoch rules/status, and both checksum-validated GBP books
no older than five seconds. A later fault, stop, quarantine or coverage-gap receipt
blocks use; starting a new epoch invalidates the earlier epoch's evidence. Rules
must remain online and no older than one hour. The latest 20 book receipts are the
bounded query budget; missing a pair in that window suspends rather than widening
the query or inventing a quote. Original receipt and event times are retained.

These receipts establish public valuation evidence only. They do not verify the
500ms live clock bound, account eligibility, private balances or gap-free historical
coverage. No public observation can promote a strategy or arm an execution writer.
All research rejection/holdout decisions remain unchanged.

Each maintenance tick observes risk with the real UTC clock. Purely unchanged marks
avoid duplicate events. Reconciliation runs at least every 15 seconds while evidence
is healthy, and immediately after feed/valuation recovery. Repeated unresolved
differences keep that bounded cadence and alert on transitions. Existing durable loss
latches and missing-midnight policy survive. Feed loss records a deduplicated
suspension, cancels entries and waits for refreshed evidence; it preserves protection.
Unknown commands and financial differences remain unresolved until reconciled.

Scheduling/deadlines use monotonic time. A wall/monotonic discontinuity exceeding
500ms, capacity failure, lost writer or other unclassified fault stops the loop.
Ticks have a three-second deadline, initial maintenance recovery ten seconds, and
shutdown suspension five seconds. The full prefix audit runs disarmed before the
loop. Health expires after two seconds without a completed healthy tick. There is
no burst of catch-up ticks after a delay and no automatic writer reacquisition.

The capacity check rejects a database at/above 2GB and app-filesystem free space
at/below 15%. It is an initial bound, not a measured production retention budget or
proof that the database/WAL/off-host-backup filesystem is healthy. Long-duration
CPU/memory/disk trials, retention/archiving and actual disk-exhaustion/recovery
testing remain necessary. A small passing synthetic test does not satisfy T19.

## Evidence and remaining work

Tests cover checkpoint rollover, bounded normal reads, legacy-to-delta compatibility,
full audit, corrupt anchors, checkpoint transaction rollback and restricted runtime
mutation. Maintenance tests cover stale/missing/future/corrupt books, metadata,
epoch changes, stopped/failed capture, deduplicated suspension, healthy recovery,
capacity-fault injection, wall-clock jumps, tick timeout, immediate shutdown and
health/metrics boundaries. Existing financial properties and lifecycle/fault traces
remain part of the complete suite. CI adds packaged audit and finite no-feed
maintenance checks after replay/restart.

The applicable evidence extends T04, T14, T17–T18, T21–T22, T29, T31–T32; it does not
claim all those groups complete. Remaining Phase 4 work includes continuous ordered
fill replay/strategy supervision, venue projections, transfers/conversions/correction
rebuild and tax-reserve workflows, outbound delivery, resource/disk trials, and
disaster restore. G2–G6 remain unpassed; no paper observation, episode, alpha or
economic-promotion claim is made by maintenance.

## Local verification — 18 October 2026

The final installed wheel passes **245 tests** on macOS arm64 (37.36 seconds) and
non-root Linux arm64 musl (42.55 seconds), both with PostgreSQL 18.6. The existing
1,000-example financial properties and 10,000 generated lifecycle traces remain
in the suite. Ruff, formatting, strict mypy, wheel/sdist builds, dependency consistency
and the runtime/research/development advisory audits pass. These are local results;
hosted checks remain bound independently to the pull request's exact Git commit.

Source fingerprint
`0a1aa4c20c727de6dc5c96302c82b6ce877f15ac812e2444efbddfa6389e5bb9`
matches checkout, installed native wheel and running application. Both image scans
report zero HIGH/CRITICAL findings and embedded secrets, with no finding exclusions.
The exact scanned rootfs layers and image configuration match the running app and
database. Both containers are healthy, non-root, read-only, capability-dropped,
on the private network with no published ports.

A protected 69,241,049-byte pre-migration dump preceded the additive upgrade. After
app/database restart, the virtual account passes a complete audit at version 56.
The database's six paper journals, four authoritative fills and prior research records
are unchanged. The local finite check recorded 86 maintenance ticks: the startup
period correctly waited for public snapshots, 16 ticks were healthy, and stopping
the collector suspended maintenance. The account remained flat with virtual NAV
£249.7246607824 and no new order or fill. These observations do not establish an
availability percentage, a qualifying paper episode, alpha or restore targets.
Both one-off containers were stopped; default foundation operation remains offline.

Two earlier native fault-suite runs exposed the streaming-cursor cleanup problem;
bounded keyset pagination resolved it without suppressing warnings. The local
evidence collector also initially compared textual zero representations incorrectly;
Decimal comparison corrected the harness. Failed checks were not counted as passes.
Full results, hashes and non-secret runtime evidence are in
[the verification record](evidence/phase-4-supervision.json).
