# Phase 4 increment — scheduled paper notices

Release candidate 0.4.6, 19 October 2026. Extends the optional outbound worker with
daily summaries and repeated current critical conditions. Phase 4/G2 remain
incomplete; this adds no strategy dispatch, qualifying paper days or live capability.
The [status register](status.md) separates engineering from the later 56-day trial.
Release 0.4.7 adds [worker health and metrics](phase-4-notification-health.md); its
run command binds a local health port and should use its own notification container.

## Upgrade and preview

Stop application/notification workers, take a protected database backup, migrate
`0009_notifications` to `0010_scheduled_notices` using the migration role, and deploy
0.4.6. The migration adds an append-only notice queue, terminal receipts, indexes
and runtime SELECT/INSERT grants. It does not rewrite financial events, fills,
journals, alerts or existing delivery history. Older images refuse the new revision;
blind schema downgrade remains unsupported.

Preparing and previewing notices needs no webhook or network access:

```sh
docker compose exec -T app aca notifications prepare --account ACCOUNT_UUID
docker compose exec -T app aca notifications notices --account ACCOUNT_UUID --limit 20
docker compose exec -T app aca notifications status --account ACCOUNT_UUID
```

Replace `ACCOUNT_UUID` with an existing virtual account. `prepare` records at most
one latest eligible daily summary and one current critical reminder per call. It
does not initialise an account, place orders, send HTTP or change balances/latches.
It reports unavailable daily evidence and exits nonzero for that failure. Notice
queries use a database-enforced read-only REPEATABLE READ snapshot and validate
each returned document fingerprint. `status` adds a separate `scheduled_backlog`;
its existing `backlog` remains the source transition count.

Installation/default Compose startup never prepares or sends notices. To deliver,
use the existing explicit [webhook setup](phase-4-notifications.md). The optional
`notifications run` command now checks critical state each tick and checks daily
eligibility once a minute. No actual Discord destination is provisioned by this
increment. There is no inbound command path.

## Daily evidence

At **00:10 UTC** the preceding UTC calendar day becomes eligible. Date arithmetic
uses UTC even during British Summer Time. After downtime, prepare only the latest
eligible day; there is no automatic historical notification flood. Days before
account creation are excluded. An account/day unique key makes preparation durable
across restarts. A prepared summary is a frozen snapshot of the evidence available
then. Later imports do not silently rewrite it; a reviewed correction workflow is
still required.

The daily report uses one read-only database snapshot, the existing financial audit
and report budgets. Contributions, withdrawals and actual GBP-valued fees use journal
economic timestamps in the half-open interval `[00:00, next 00:00)`. Fault counts
are alert transition events carrying incidents/hard halts, not unique incidents or
a measured uptime statistic.

If historical valuations exist within that day, the original-time performance
engine independently reconciles the selected source interval. The message explicitly
shows the interval and its observed NAV, P&L, exposure and drawdown. A daily
contribution at the opening timestamp is still counted as a calendar contribution;
the observed interval's performance begins at its own recorded opening snapshot.
Missing historical data produces unavailable observed figures.

**Full-day P&L/return remains unavailable:** complete midnight boundary coverage has
not been qualified. A partial recorded interval is never labelled a full-day
return. The tax reserve estimate is unavailable, backup health is unverified and
every report remains provisional. These fields do not manufacture evidence for
unfinished tax or backup pipelines. Decimal values stay strings and no new float
financial boundary is introduced.

Daily construction has a 15-second asynchronous deadline, per-statement database
limits and the existing evidence budgets (100,000 financial/valuation/venue events,
10,000 journals, 4,096 fills). Valuations are read in 64-row pages. A data/budget or
deadline failure records no fabricated summary, reports a local fixed-code incident
and retries the daily check later. Critical notices already prepared remain
deliverable. Database/ownership failures stop the worker.

## Critical repeats and delivery

**A41 — scheduled notices share one delivery authority.** Notices and terminal
receipts are separate from financial history. The existing per-account notification
session lock serialises preparation, configuration and HTTP attempts. The execution
writer uses a different lock and process/connection pool. There is no transaction
open over HTTP.

Current hard halt, uncertain orders, inventory/protective-reservation differences,
quarantined residuals and specified reconciliation discrepancies generate a safe
aggregate critical notice. Its episode identity binds policy epoch/latch, order
identities and incident evidence; raw incident strings never appear in the message.
Only fixed reason codes, stable IDs and paper context leave the application.

A pending reminder for the same episode is reused. After its terminal receipt,
wait at least **five minutes** before preparing another. A delayed or rate-limited
delivery therefore does not cause an immediate repeat after eventual success.
Restart and backward clock changes cannot shorten that wait. A newly observed
critical episode can alert immediately. The current state is checked again when
selecting a reminder; a cleared or changed episode is marked `superseded` without
HTTP. This describes the state read at dispatch, not a lock on concurrent execution.

Without an in-flight item, priority is **critical reminder, daily summary, ordinary
transition FIFO**. A retry/in-flight item retains its identity until it finishes
or reaches its bounded attempt count. Neither priority nor reconfiguration bypasses
the destination's known rate-limit deadline. All work shares the existing eight
attempt bound, HTTP deadlines, durable 404 disable, manual reconfiguration policy,
safe rendering and mention suppression. The result plus a terminal notice receipt
commit together; scheduled work never advances the ordinary transition cursor.

An interrupted send can duplicate a Discord message after retry. Confirmed committed
receipts prevent replay. Abandonment/dead letters remain visible in the delivery
journal, preview outcome and terminal failure count. Critical repeats are separate
notices after the five-minute interval, rather than retries beyond eight attempts
for the same notice. Destination disable still stops all sends until explicit review.

## Verification and limits

Real-PostgreSQL/HTTPX fixture tests cover UTC eligibility, immutable preparation,
calendar boundaries, observed source provenance, independent owners, priority,
cursor isolation, recovered-incident suppression, long throttling, restart/crash
retries, terminal receipts, bounded failures, safe rendering and unavailable reports.
No real Discord or exchange request is part of the suite.

The single nominated account/destination remains the operating scope. Shared-webhook
rate coordination across accounts is unimplemented. At this 0.4.6 milestone, notification health/Prometheus,
external heartbeat, complete full-day returns, real destination integration and the
critical 60-second latency objective remain unqualified. Resource/retention and
disaster restore trials remain required. See the [verification record](evidence/phase-4-scheduled-notifications.json).
