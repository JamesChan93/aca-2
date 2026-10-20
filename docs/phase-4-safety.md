# P4-C3 — Local safety evidence and capacity finding

20 October 2026. Application candidate **0.4.12**, schema
`0014_accounting_operations`. Source: the five conditions of
[P4-C3](phase-4-completion.md#p4-c3--close-the-named-local-paper-safety-gaps).

**C3 is not complete.** This increment supplies the integrated T00–T32 mapping,
actual isolated WAL write-failure recovery, populated restore/gap blocking and
controlled notification timing. The existing **fourth condition, capacity**, stays
open. The trial found that replay audit and historical reporting exceed their
20-second measurement budgets at 100,000 records. A 100,000-valuation ceiling also
cannot hold 56 days at one valuation per second. No new work package or unrelated
backup/notification feature is being added to the checklist.

The [machine-readable report](evidence/phase-4-safety.json) records the final release
checks, measurements, source hashes, all 33 T-IDs and later-stage boundaries.
Passing test/CI jobs do not override the recorded capacity failure or approve G2.
The retained paper stack is unchanged. Qualifying days and episodes remain zero;
all research candidates remain rejected and the final holdout stays closed.

## Real storage failure

Run from a checkout with Docker available:

```sh
python3 scripts/storage_fault_drill.py
```

The [helper](../scripts/storage_fault_drill.py) accepts no target, project or
archive arguments. It generates a new project in the existing disposable restore
namespace. Its [override](../compose.storage-fault.yaml) uses a **384 MiB tmpfs
volume**, with no host port. Before starting a database or filling anything, the
helper verifies that resolved configuration uses that bounded RAM filesystem and
that the database mount selects it. Tests reject changed filesystem type, size,
mount and non-generated project identities.

A holder container keeps that RAM volume mounted across PostgreSQL restart.
Both mounts disable Docker's image-directory copy-up. The helper starts the holder
first and verifies actual UID/GID 70 and 384 MiB capacity before starting PostgreSQL,
so database initialization cannot depend on parallel container creation order.
This uses Docker's documented [volume copy-up control](https://docs.docker.com/engine/storage/volumes/).
After committing an account, opening funding and a reserved but unconsumed order,
the fixture fills only its bounded volume. A small explicit WAL message followed
by WAL rotation forces real allocation. PostgreSQL reports `PANIC` and
`No space left on device` for `pg_wal/xlogtemp...`; its subsequent recovery cannot
allocate WAL while space remains exhausted. These are PostgreSQL diagnostics,
not an injected Python capacity exception.

The helper keeps this failed database offline while the original writer attempts
dispatch and the supervisor attempts recovery. Both fail closed. There are zero
venue calls. Removing **only the drill's filler**, then restarting this database,
restores the exact committed portfolio state hash, ledger and reservation. Normal
paper recovery expires the unused order; a durable hard halt blocks further
entries pending review. Reconciliation readiness is distinct from entry permission.

After database recovery, the existing notification worker attempts the critical
halt notice through a controlled HTTPX transport. Host monotonic measurements
bound fault-to-unhealthy detection by ten seconds and fault-to-notification attempt
by 60 seconds. The report also separates the worker's shorter, post-gate duration.
No notification is claimed while its database is unavailable. Total-host outage
notification still needs the separately planned external heartbeat service.

Finally all owned containers and volumes are removed, including on failure.
Private logs/manifests remain under `.secrets/backups/storage-drill-*`, mode
0700/0600. The helper cannot select the retained workstation volume.

## Populated restore and declared missing suffix

```sh
python3 scripts/restore_drill.py
```

The existing exported-snapshot/concurrent-commit drill now contains six synthetic
accounts. The original validated, uncertain, unposted-fill and hard-halt cases are
retained. Two additional accounts contain actual replay cursors/valuations and UTC
boundary evidence, strategy selections, GBP transfer records, a reversal/replacement
correction and its durable invalidation, daily/critical notices, failed and
successful delivery receipts, and exact-event notification reviews.

All application table row hashes, schema/roles/constraints, source anchors,
financial totals and delivery identities must match the exported snapshot after
restore. A deliberately missing ACL role still proves failed restore rolls back
atomically. Two fresh recovery processes submit/cancel zero orders, post the
previously unposted fill once, preserve protection and keep the corrected account
quarantined. Notice/review/replay identities and financial prefixes are unchanged.

Only after those equality checks, a separate declaration from the source reveals
32 post-snapshot GBP contributions and 32 public records that the archive lacks.
The fixture identifies that gap and durably hard-halts all six accounts using the
existing paper halt path. New entries fail, no venue command is issued and continued
qualifying observation is forbidden. The archive does **not** recover the absent
rows; ordinary audit alone cannot discover a completely missing suffix without
independent source evidence. There is no generic gap importer or second backup
architecture in this increment.

This remains a disposable local logical restore. Encrypted off-host/WAL backups,
host loss, writer fencing, actual deployed gap reconciliation and production RPO/RTO
remain later requirements. Full T25 remains open.

## Critical fault and HTTP independence

[Integrated timing tests](../tests/test_integrated_safety.py) start with a funded
paper account. The venue accepts a submission, loses its acknowledgement and
supplies incomplete reconciliation evidence. The command becomes uncertain once,
its reservation remains and supervisor health denies readiness. The notification
owner detects the critical state, persists an attempt and uses a controlled
transport in three cases: successful response, unreachable destination and HTTP
429 with `Retry-After: 65`.

Real monotonic elapsed time must be below ten seconds to detection and below 60
seconds to the first HTTP attempt. While that transport is deliberately blocked,
the financial writer performs another risk observation, preserves units/balances
and reservations, and refuses another entry. Neither network failure nor throttling
retransmits the order, posts a fill or invents another investment journal. Retry
deadlines retain the notice identity and prevent an immediate second request.
This is local transport evidence, not real Discord receipt or sustained reliability.

## Bounded capacity trial — open acceptance condition

```sh
python3 scripts/capacity_trial.py
```

The [trial](../tests/capacity_fixture.py) creates a new disposable database and
bulk-constructs valid production hash/record contracts at the 100,000 event and
valuation report limits. A separate unenrolled maintenance account also has
100,000 financial events, so its one-second loop respects the existing rule that
enrolled accounts require replay supervision. Twenty real scheduled ticks measure
maintenance cost. No invented market health, book touches or fills are used.

The [capacity override](../compose.capacity.yaml) caps the worker at **0.75 CPU and
768 MiB**; PostgreSQL retains the configured 0.75 CPU/1 GiB limit. The report records
CPU time, RSS/cgroup memory, database/table sizes, backlog/source count, audit/report
deadlines and loop durations. The host also runs the retained stack; this is not an
isolated production hardware benchmark. Bulk construction speed does not measure
the actual ingestion or execution writer. The cash-only fixture does not establish
performance with 1,024 retained orders, 10,000 journals or 4,096 fills.

The initial unconstrained diagnostic already exceeded 20 seconds for replay audit
and reporting. The final constrained trial repeated both deadline failures. The
100,000-event financial writer audit completed in 11.93 seconds; the loaded
maintenance account completed 20 healthy ticks, with maximum tick time 0.085
seconds. The synthetic database occupied 500,733,631 bytes. These positive loop
measurements do not repair the failing replay/report operations or prove retention.
Even one valuation per second would require **4,838,400 observations** in 56 days;
100,000 observations hold about **1.16 days**. Real public traffic can be much faster.
The existing 2 GB database stop is a safety ceiling, not a retention solution.

The next task is still **C3 condition 4**: resolve the measured audit/report and
retention limits, then rerun this trial and document either sufficient 56-day
headroom or a reviewed archive path preserving financial history, public source
anchors and permanent order identities. No such path is claimed as implemented
or approved here. Simply increasing a constant, silently dropping source evidence
or starting the eight-week clock would not close that condition.

The command returns success when it completes and cleans up a valid *measurement*,
including measured limit failures. Its JSON explicitly keeps
`56_day_headroom_demonstrated=false` and lists deadline failures. CI prints these
results; it does not convert a measurement into a safety approval.

## Integrated release review

The evidence maps every T-ID to existing test files and states which private,
production or later-stage assertions remain unimplemented. T19 is explicitly
partial because capacity has failed its named condition. The new guard and fault
tests run with the existing integrated suite, including the C1/C2 workflows.

The full Linux/real-PostgreSQL run passed **578 tests in 136.27 seconds**. Ruff
checks/formatting (160 Python files), strict Mypy (138 files), distributable build,
dependency consistency and all three unchanged-lock advisory audits pass. The
packaged image passes accounting/CLI recovery across an actual container restart;
its package source hash matches the checkout and wheel. The populated restore
compared 29 tables for six accounts and passed in 64.385 seconds. Actual WAL fault
detection took at most 0.323 seconds, with a controlled HTTP attempt after recovery
within 6.536 seconds. A subsequent holder-first/copy-up regression run passed with
actual mount ownership/size verified, detection within 0.246 seconds and notification
within 7.098 seconds. The supplemental helper guard suite passed 34 cases, including
rejection of copy-up on the shared volume. None of those results closes the capacity condition.

Critical financial properties retain 1,000 deterministic Hypothesis examples, and
the lifecycle test retains 10,000 generated sequences. `derandomize=True`, locked
Hypothesis/Python versions and source hashes reproduce the generated examples;
`print_blob=True` emits a replay blob and the minimized failing trace if a property
fails. CI retains the pytest failure output. These tests do not represent 10,000
real exchange submissions or 56 elapsed days.

No dependency, database schema, Kraken write capability, paid service, production
deployment or promotion is introduced. C4 follows the still-open C3 capacity
condition and remains responsible for the consolidated runbook and explicit local
G2/Phase 4 closeout decision.
