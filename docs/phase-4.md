# Phase 4 — durable offline paper core

This review delivers the accounting, portfolio, risk and local execution core after
merged Phase 3. It is an independently reviewable part of Phase 4. It does **not**
declare the whole phase or G2 complete. Continuous public-feed strategy dispatch,
one-second operational supervision, resource/fault trials, notification delivery
and disaster recovery still need implementation and review before extended paper
operation. The existing foundation health endpoint continues to deny trading readiness.

All new order mutations go to an in-process **local simulator** with its own durable
PostgreSQL evidence. There is no Kraken order writer, private authentication, key
mount, live profile, approval permit, paid service or production deployment.
The Phase 3 research rejection and unopened actual holdout remain unchanged.

## Review the workflow

From a clean checkout, `python3 scripts/bootstrap.py` builds the paper-only image,
provisions roles and upgrades through `0004_paper`. Use a fresh UUID for each demo
or replay. These commands operate on **virtual** funds:

```sh
docker compose exec -T app aca paper demo \
  --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper status \
  --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper journal \
  --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper alerts \
  --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper recover \
  --account 6e04530b-9382-418d-9a24-331672f4016b
```

The demo uses £250 virtual capital, a disclosed synthetic rising 201-day history,
the shared SMA200 signal, a bounded FOK entry, attached simulated protection and
a bounded IOC exit. It loses money to the assumed fees and spread. Its report
explicitly excludes it from strategy promotion and paper-episode gates. It is not
a market study, observed exchange behaviour or evidence of an advantage.

`aca paper replay --account NEW_UUID --input PLAN_JSON` accepts the bounded
`ReplayPlan` contract in `aca.simulation.replay`: schema version 1,
`kind=synthetic_paper_replay_v1`, initial capital as Decimal text, one or two rules
and strictly ordered `steps`. Each step has a validated book, UTC receipt time,
optional public trades and optional closed candles for the shared strategy.
Maximums are 16MB input, 1,000 steps, 1,000 candles and 100 trades per step.
No arbitrary strategy module or network endpoint can be supplied. A replay refuses
an already used account. Preserve the input file alongside its report.

Offline input time is the injected logical clock. The five-second async command
timeout uses monotonic asyncio deadlines. This does not implement a wall-clock
market supervisor or demonstrate the live clock bound. Multi-asset replays need
fresh GBP marks for every held asset; a missing mark blocks entries and makes
performance provisional.

Status returns versioned Decimal-string/UTC JSON. Journal returns CSV signed GBP
debits, source references and correction links. Alerts returns the durable local
notification outbox. There is no Discord sender in this change; reading an alert
does not acknowledge/deliver it. Nothing is sent externally by the demo/bootstrap.

## Accounting and performance

`accounting.ledger` constructs balanced GBP double-entry journals and separate
asset-quantity movements. Contributions/withdrawals use an equity-flow account,
trades use owned inventory and cash, fees use their **actual** assets and explicit
GBP valuation at the event time. Sales remove weighted-average internal carrying
cost and recognise internal realised P&L. This carrying convention is **not** UK
tax matching or an owner-wide Section 104 pool.

Financial inputs reject binary floats, non-finite values and unsupported storage
precision. Calculations use precision 50; GBP functional-currency products and
derived stored values round explicitly to 18 places using half-even. Quantities
and price limits use the shared directional increment rounding. The database
never supplies the application's rounding policy.

Journal sources and canonical fill IDs are unique per account. Repeated evidence
does not book another fill, fee or alert. Conflicting evidence blocks reconciliation.
Actual GBP, base-asset and third-owned-asset fees have fixtures. An unfunded fee
or oversell cannot fabricate an owned balance. `reverse` constructs a linked
opposite journal; applying corrections and rebuilding historical unit/performance
observations is not exposed as an operator shortcut in this release.

Units issue/redeem at a valid pre-flow unit price. External flows do not become
trading returns, and latches survive deposits, withdrawals and recovery. Complete
withdrawal preserves the last unit price instead of inventing a return to one.
Unit rounding has an explicit 18-place storage quantum; generated flow properties
check numerical neutrality within that quantum's propagated error. TWR, normal-flow
MWR, fees, NAV, exposure and drawdown are exported. MWR returns unavailable when
the supported unique-root conditions or numerical bracket do not hold. Infrastructure
costs and the £1m scenario target do not enter these records or risk sizing.

`tax_reserve_gbp` is represented and excluded from free cash, but there is no tax
estimator or owner declaration workflow here; a zero default is not a tax estimate.
Transfers, conversions, fee tax classification and final tax reports remain later
accounting/reporting work. All outputs identify tax reporting as provisional.

## Independent entry controls

The sizer proposes downward-rounded quantities. A separate portfolio policy checks
the exact intent. A separate dispatch guard recomputes cash, pending exposure,
episode/aggregate risk and loss thresholds against the committed state. It binds
the immutable policy, exact intent, rules and state version to a two-second approval.
Any intervening account event invalidates an unused approval. Denial expires that
queued entry; it never silently refreshes permission or chases the price.

Both checks preserve the provisional six limits and conservative defaults. Pending
or uncertain buys consume cash and exposure; unfilled sells grant no credit.
There is no pyramiding. The stop distance is at least the signal proposal and 5%
of maximum entry price. Costs include 0.8% per leg and the 0.2% implementation
allowance. Entry and protective-exit minimums are checked without increasing size.
Metadata/hash/status, fresh checksum-valid quotes, account/reconciliation age,
clock offset and marketable-limit spread/slippage/deviation checks can veto dispatch.
The account evidence here is **simulated**, not authenticated Kraken evidence.

Loss equality triggers the specified latches. Soft/hard flags cannot clear within
an epoch. A substantive operator `paper review --reason TEXT --account UUID`
requires complete reconciliation and creates an audited new epoch while preserving
inception high water and old history. It cannot clear a same-day daily suspension.
An actual 00:00 UTC mark is required for the new day's loss baseline; missing it
suspends that day instead of guessing midnight NAV.

## Commands, protection and recovery

The writer commits `SUBMITTING` and permanent consumption before calling the port,
then submits once. A timeout or unknown failure becomes `UNKNOWN`, retains reserves
and blocks entries. Positive, classified rejection evidence can produce `REJECTED`.
A missing open order never proves failure. Recovery searches the complete separate
paper venue stream, including closed orders and fills. An unresolved consumed
command is never replayed, including a crash after consumption but before sending.

Financial fills and their journals commit atomically with account state, fill
identity and alert work. Projections reconstruct owned quantities/costs from the
journal and verify the versioned hash chain. A changed/missing canonical fill or
an unknown order is a reconciliation failure. Terminal remainders cannot reopen;
late fills can update cumulative economics without another submission.

Passive orders start behind displayed queue volume and require qualifying aggressive
trades to consume that queue. Reconnect epochs reset the queue assumption. Depth
and trade observations are deduplicated; marketable liquidity is shared across
orders at a 1% participation ceiling. Latency is at least 250ms. GTD entries expire
after 60 seconds; IOC/FOK cannot wait beyond the bounded logical permit for a more
favourable future observation. There is no maker fill from candle prices.

Simulated partial entries create independent protective children only when they
meet the current exit minimums. Tiny unprotectable fills block entries and cancel
the residual entry. They do not trigger a risk-relaxing top-up. A stop-limit gap
can remain unfilled. A strategy exit cancels identified protection, reconciles
racing fills, reserves only confirmed owned inventory and uses a bounded IOC sell.
Untradeable residuals are quarantined. A hard halt keeps protection and requires
an explicit position decision for a voluntary exit. No global cancellation timer
or automatic unbounded liquidation is implemented.

`paper halt --account UUID --reason TEXT` latches a hard halt, selectively cancels
entries and preserves protective sells. `paper recover` first disarms/expires
unused work, then reconciles. Simulator recovery can restore paper readiness only;
it cannot grant a live capability. Existing live restart/fencing/approval design
remains unimplemented and mandatory before any future real execution.

## PostgreSQL and ADRs

**A27 — Append-only paper projections.** Retain the restricted runtime role rather
than grant UPDATE for mutable projections/row locks. One dedicated session-level
advisory lock owns each paper account. Immutable sequential events, permanent
client identities, journals, canonical fills and outbox work commit together.
Uniqueness constraints reject stale versions, repeated economic sources and client
reuse. A lost/replaced writer connection cannot silently reconnect and rearm.
The separate simulated venue uses a separate transaction lock. No SQL transaction
spans a venue request. This is local coordination, not exchange-side fencing.

**A28 — Database-sealed journals.** Deferred PostgreSQL triggers require balanced
lines and exact agreement with immutable JSON evidence, including quantity movements.
An existing journal cannot acquire more lines or movements through INSERT. Runtime
retains SELECT/INSERT only; UPDATE/DELETE/TRUNCATE remain denied/trigger-rejected.
Migration `0004_paper` contains its own frozen table definitions and refuses a blind
downgrade. Full state snapshots and full-history verification are deliberately
bounded offline mechanisms; incremental projections/checkpoints and resource trials
are required before a continuously running eight-week paper service.

**A29 — No promotion through synthetic operations.** Offline operational replay
shares signals, sizing and risk policy but is not the historical reference engine
or a source of statistical/trading evidence. No currently rejected candidate is
automatically selected or frozen. Extended paper scheduling and promotion reports
remain disabled until separately reviewed implementation/evidence supports them.

Connection/transaction handling follows the
[SQLAlchemy asyncio documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
and [connection failure guidance](https://docs.sqlalchemy.org/en/20/faq/connections.html).
No package version or container base changes were needed for this increment.

## Validation and outstanding release gates

Tests cover exact loss thresholds, unit flows, empty-portfolio continuity, actual
fee assets, journal arithmetic/rebuild, sealed history, positive rejection versus
unknown outcome, crash-before/after-send, consumed-command non-replay, closed client
IDs, cancel/fill races, protection gaps/dust, writer exclusion/connection termination,
failed durable consumption, stale approvals/metadata/quotes/accounts, incomplete
financial history and full synthetic CLI replay. Critical financial properties use
1,000 deterministic generated examples; lifecycle validation uses 10,000 generated
fault/state traces with retained Hypothesis failure blobs. These are not 10,000
independent process-kill trials.

Applicable evidence maps to T00, T05, T07–T18, T22–T23, T26–T30 and T32; existing
public-data/configuration tests remain. This does **not** claim every scenario in
those test groups, G2, actual private exchange behaviour, a restore drill, RPO/RTO,
eight weeks of availability, ten real-time paper episodes, protection calibration
or positive economics. T19 resource/disk trials, T24 outbound delivery and T25
disaster restore remain outstanding alongside continuous supervision and the
unresolved Phase 3 data/strategy gates. The next increment remains within Phase 4.

Release results and local runtime evidence are recorded in `docs/evidence/phase-4-*`
when verified. Earlier milestone evidence remains historical, bound to its source.

## Local verification — 18 October 2026

The installed wheel passed **221 tests** on native macOS arm64 (28.13s) and non-root
Linux arm64 musl (33.01s), each with PostgreSQL 18.6. Ruff, formatting, strict mypy,
wheel/sdist builds, dependency consistency and all three hash-lock advisory audits
passed. The exact Python source fingerprint is
`754ab05f29f6926fa6e1924ee95ab7ffafd03eb3acd641ea946472cd3065de63`;
it matches the checkout, installed native package and running application image.

Both pinned runtime image scans found zero HIGH/CRITICAL vulnerabilities and
embedded secrets, without finding exclusions. Scanned rootfs layers and image
configuration fields match the running containers. App and database are healthy,
non-root, read-only, capability-dropped, on the internal network without published
ports. The default application still reports `trading_ready=false`.

A protected 69,018,902-byte pre-migration dump was taken. The additive migration,
application restart and database-container recreation preserved prior market,
research and audit history plus the new journals and canonical fills. The saved
synthetic replay ended flat at £249.7246607824 from £250, including £0.2591351676
fees. It is a functional example with **no promotion weight**. Two completed virtual
demo accounts are retained; the first verification harness compared different
textual representations of zero before being corrected to Decimal comparison.
An initial Docker build also exposed old 0.3.0 wheel paths, which were corrected
before the successful packaged runs. Neither failed check was counted as a pass.

See [verification](evidence/phase-4-verification.json) and
[runtime/replay evidence](evidence/phase-4-runtime.json). Hosted workflow results
remain independently bound to the pull request's exact Git head. This local
evidence does not assert a hosted pass, G2 completion or a disaster restore.
