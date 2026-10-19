# Phase 4 — Process-kill recovery validation

This increment validates the existing 0.4.8 paper execution code against abrupt
interpreter death. It changes tests and documentation only. There is no application
version bump, migration, deployment, strategy selection or exchange access.

The earlier 10,000 generated lifecycle sequences and `SimulatedCrash` fixtures
exercise control flow and state transitions. These additional tests use real
`SIGKILL` against a separate interpreter with live PostgreSQL connections, followed
by recovery in another interpreter. They do not establish host-loss or database
disaster recovery.

## Reproduce safely

From the repository root with Docker running:

```sh
python3 scripts/ci_database.py --linux tests/test_process_recovery.py -vv
```

The ordinary full-suite command also includes these tests:

```sh
python3 scripts/ci_database.py --linux
```

Use the isolated `aca-foundation-tests` stack only. Do not run two test-stack
commands concurrently. The helper creates a random `aca_test_...` database and
drops it when finished. It does not restart the ordinary paper application or
touch retained virtual accounts. Native POSIX testing is also supported with the
installed application wheel and the documented loopback test database settings.

`tests/process_worker.py` is a test fixture, absent from the runtime image. It
rejects a non-loopback host or a database name outside the disposable prefix before
connecting. Each child uses `exec` into a fresh isolated Python interpreter and
creates its own SQLAlchemy engine. No pooled connection or engine crosses a fork.
The worker receives database settings through stdin and does not inherit the
parent's admin-connection environment. Its database operations use `aca_runtime`.
These restrictions prevent routine accidental misuse; trusted test code running
under the same operating-system user is not a security sandbox.

The parent stops only a subprocess it created, confirms the `-SIGKILL` exit code,
then waits for PostgreSQL to release that account's session lock. It never arms a
replacement while the old process remains alive. A second writer must fail while
the original process is paused with its healthy session. Gate reads are bounded
by 15 seconds, process exit and lock cleanup by five seconds, and each child has
a 60-second alarm. Cleanup kills/reaps any remaining test child on failure.

Each child has a private 0600 JSONL transcript in pytest's temporary directory.
Bounded virtual-account messages also appear in captured failed-test output so
hosted logs retain the failure trace after disposable-container removal. No
password, admin URL or secret-file contents appear in those messages. Download
the failed job's log before its retention expires; this is not a permanent audit
archive. Successful tests retain ordinary local pytest temporary files until
pytest or the test container cleans them up.

## Crash matrix and assertions

Every recovery runs twice in separate interpreters. A recovery-only venue wrapper
records any submit/cancel attempt and makes the test fail if recovery issues one
for these fixtures. The recovered original client ID must also fail a direct
dispatch attempt. Full paper event/ledger audits run before and after recovery;
the original event hashes must remain an unchanged prefix.

| Abrupt stop | Committed state before death | Required recovery |
| --- | --- | --- |
| Inside reservation transaction before commit | No visible intent or reservation | Transaction rolls back; no order reaches the venue |
| Durable intent before consumption | `VALIDATED`, funds reserved | Expire unused intent; release reservation; no submission |
| Consumed command before send | `SUBMITTING`, no venue order | Preserve `UNKNOWN` and reservation; block new entries; never resend |
| Venue accepts before local acknowledgement | `SUBMITTING`, one authoritative order | Discover the order; preserve reservation; never resend |
| Full fill before local acknowledgement | Venue fill and protective child, unposted economics | Discover protection and post exactly one fill/fee journal |
| Partial fill before local acknowledgement | Venue partial fill and protective child | Post once; retain reservation for remaining entry and correct protection |
| Inside fill-posting transaction | Venue fill, uncommitted journal/projection | Roll back the incomplete posting; recover exactly one complete posting |
| Fill posting committed before reconciliation completes | Journal and fill already committed | Preserve posting and finish reconciliation without another posting |
| Cancellation consumed before venue transmission | `PENDING_CANCEL`, authoritative entry still open | Preserve `UNKNOWN`, cancellation uncertainty and reserved remainder; never resend |
| Partial-entry cancellation accepted before local result | `PENDING_CANCEL`, authoritative cancelled remainder | Preserve partial fill/protection; release only the confirmed unused reservation |
| Full fill races entry cancellation | `PENDING_CANCEL`, authoritative filled order | Import the fill once; preserve protective inventory; never resend either command |
| Hard halt committed before safety processing completes | Durable halt and existing protective exit | Preserve the same halt epoch, economics and protection; reject a new entry |

For filled fixtures the independent assertions compare actual fill quantities,
GBP cash, recorded fee assets/amounts, sealed journal lines, remaining reservations
and protective quantities. Repeated recovery must leave all fill IDs, journals,
balances, order economics, halt state and authoritative venue evidence unchanged.

One further test terminates the writer's PostgreSQL backend while its interpreter
is alive **before command consumption**, then resumes it. Dispatch must fail on the
lost connection before a venue call. Only after that interpreter exits does a
replacement recover the unused intent. Two negative fixtures prove that ordinary
retained-database and non-loopback targets are rejected before connecting.

## Coordination and remaining boundaries

The harness follows the existing ADR A27 single-writer rule. SQLAlchemy documents
that [pooled connections must not cross process boundaries](https://docs.sqlalchemy.org/en/20/core/pooling.html#using-connection-pools-with-multiprocessing-or-os-fork).
The test deliberately uses new engines and independently acquired sessions.

A database advisory lock is not exchange-side fencing. These tests do not prove
that an old writer can safely continue after losing its lock, or authorise a
replacement while the old process may still issue an external request. A future
live failover still requires confirmation that the old writer is stopped or its
key revoked. This test only covers connection loss before consumption and fully
fenced process death at the listed boundaries.

These fixtures add evidence for T07, T08, T09, T10, T18, T22, T27 and T30 without
claiming every scenario in those groups. The actual venue remains a local durable
simulator. There is no exchange HTTP request, network partition, container/host
power loss, PostgreSQL crash, disk-full event, WAL restore or measured RPO/RTO.
Strategy-exit/protective-sell races are not an additional process-kill case here.
Protection/fee assumptions still require calibration and all other G2 evidence.

Continuous strategy scheduling, operational accounting/tax workflows, storage and
resource qualification, external notifications/heartbeat and off-host restore
remain unfinished. Phase 4 and G2–G6 are incomplete; the Phase 5 clock has not
started. All 20 research candidates remain rejected and the final holdout stays
closed. No synthetic episode or passing test grants promotion or live authority.

Measured results are recorded in
[the evidence record](evidence/phase-4-process-recovery.json). Hosted results are
bound separately to the pull request's exact commit.
