# Phase 4 — Disposable local logical-restore drill

The repeatable PostgreSQL logical-restore drill now covers a captured snapshot
while controlled paper writes commit. It uses application 0.4.9 and schema
`0012_tax_cash_reserves`. This increment changes only the test harness and its
documentation/CI label. No application change, migration, retained-service restart,
strategy selection or exchange access is part of the drill.

## Run it

With Docker running, from a clean checkout:

```sh
python3 scripts/restore_drill.py
```

Only host Python 3.9+ and Docker/Compose are needed. The helper builds the existing
hash-locked Linux test image and starts two **new** PostgreSQL clusters with random
`aca-restore-<12 hex digits>-source` and `...-target` project names. Neither cluster
publishes a port or has a public network. The fixture runs as UID 10001 in the
database network namespace. It is absent from the runtime wheel/image.

There are no arguments for an existing project, database, backup or restore target.
The fixture also restricts database names to its generated `aca_test_restore_...`
namespace and connects only to loopback inside the disposable database namespace.
Ordinary `aca-paper` and `aca-foundation-tests` stacks are not stopped or modified.
The helper does not run the foundation app, collector or notification sender.

Bootstrap creates private local database passwords if needed. Existing files are
reused without rotation; only these explicitly mounted files reach the fixtures.
Administrative creation/restore work is separate from `aca_runtime` financial
operations and permission tests. Database passwords never appear in argv or reports.
This trusted test harness has an administrative credential; it is not an isolation
boundary against malicious test code or an operator with Docker access.

## What it verifies

The source is migrated and seeded with synthetic GBP 1,000 accounts, a configuration
record, an audit record, journals, fills, protective orders, reserved commands,
alerts and checkpoints. Four cases are checked after restoration:

| Backed-up case | Recovery assertion |
| --- | --- |
| Reserved `VALIDATED` entry never consumed | Expires, releases reservation, never submits |
| Consumed entry with no authoritative order | Remains `UNKNOWN`, retains funds, rejects another entry and never resubmits |
| Accepted entry with a venue fill not yet financially posted | Discovers protective child; posts one fill/fee journal; GBP cash and BTC quantity match actual fill economics |
| Posted position with a manual hard halt | Keeps halt/epoch, balances, fill, journal and protective exit; rejects another entry |

The reserved-entry account also receives 64 synthetic GBP 1 contributions and
alternating manual GBP 100/101 tax reserves through the ordinary paper APIs.
Each cycle stores a synthetic public receipt/trade/replay envelope in a separate
transaction. These records populate journal, quantity, unitisation, reserve,
checkpoint, outbox and market history before capture. No Kraken or Discord request
is involved. Test venue methods reject/count any submit or cancellation attempt.

An administrative READ ONLY, REPEATABLE READ transaction exports a snapshot. Table
hashes and the independent runtime financial audit import that exact snapshot in
separate read-only transactions. The exporting transaction remains open until
`pg_dump --snapshot=...` completes. The rules follow PostgreSQL 18's
[snapshot synchronization](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)
and [snapshot import requirements](https://www.postgresql.org/docs/18/sql-set-transaction.html).

To make overlap deterministic, a test-only administrative transaction holds an
exclusive lock on the configuration table. The fixture must observe its actual
`pg_dump` backend blocked on that lock before committing another 32 cycles (96
transactions). It checks the dump is still waiting afterwards, releases the lock,
then lets the dump finish. Re-importing the exported snapshot must reproduce the
same transactional data and economics despite those commits. A current source
read must show the newer records. This establishes consistency during a controlled
overlap; it does not measure continuous write/copy throughput or recommend that
production backups lock this table.

The host writes a **custom-format logical dump** to a private directory and records
its byte count and SHA-256. Each restore checks the custom archive header and full
checksum before starting PostgreSQL's restore tool. Restore retains ownership and
ACLs and uses `--single-transaction --exit-on-error`; there is no `--clean`,
`--create`, partial-table selection, `--no-owner` or `--no-acl` workaround.
These choices follow the [PostgreSQL 18 pg_restore documentation](https://www.postgresql.org/docs/18/app-pgrestore.html).
The [pg_dump documentation](https://www.postgresql.org/docs/18/app-pgdump.html)
describes consistent database snapshots; cluster roles require separate setup.

A non-login test role has a SELECT grant on one source table. That role is initially
absent on the fresh target, so the first restore must reach an explicit missing-role
ACL failure. The target's application tables, sequence evidence and selected catalog
hashes must remain identical to their empty pre-restore snapshot. The fixture then
creates the missing non-login role and restores the same verified archive into that
still-empty target. The application migration is **not** run on the target first.

Before recovery, all 25 application tables are compared using row counts and
length-framed SHA-256 of sorted PostgreSQL JSON rows. Empty tables are included but
do not establish populated-data recovery for their workflows. Selected catalog
definitions also match: ownership/ACLs, columns/defaults,
constraints with validation/enforcement/deferral flags, indexes, history triggers,
functions, sequence definitions, default privileges, role attributes and database permissions. Runtime
verification and full financial/event reconstruction run on both copies.

Sequence counters are checked separately because
[sequence changes are outside ordinary MVCC rollback](https://www.postgresql.org/docs/18/functions-sequence.html).
For the current positive, non-cycling, cache-one owned sequences, the restored
counter must lie between the counter at capture and the counter after all source
writers close. It must also be called and at least as high as every backed-up row
ID if any exist. Other sequence definitions stop the drill. No table, financial
field, ownership, sequence definition or maximum backed-up row ID is excluded from
the exact snapshot comparison. Ahead counters may leave harmless ID gaps; they
do not authorise importing post-snapshot rows.

PostgreSQL reparses seven current enum-style checks from an array cast, such as
`ARRAY['buy'::character varying]::text[]`, to equivalent casts on each literal,
`ARRAY['buy'::character varying::text]`. The comparison normalises **only those two
complete CHECK forms** with unbounded varchar string literals. It preserves the
column and exact literals; tests distinguish changed values, columns, bounded
varchar and other types. All other definitions compare as returned. This is a
limited schema comparison for the present migrations, not a general SQL-equivalence
engine or database integrity/security attestation.

Recovery runs in two successive fresh fixture interpreters. A venue wrapper counts
and rejects every submit/cancel attempt, including exceptions the execution core
could otherwise handle. Recovery must make zero such calls. Repeated recovery must
leave economic evidence unchanged; recovery audit events may be appended. The
original consumed or expired client IDs cannot be dispatched. The current source
is checked against its post-capture state to prove restore/recovery left that state
unchanged. The newer source suffix is deliberately excluded from the restored
copy and is not automatically imported. The dump alone cannot recover that gap.

Finally, SQL probes prove that the restored runtime cannot create schema, temporary
tables or mutate financial history, and that a history trigger still rejects an
owner's UPDATE. No runtime activation mechanism or automatic failover is added.

## Evidence and cleanup

The helper prints its result and private directory under
`.secrets/backups/restore-drill-<id>/`. The directory is 0700; the dump, per-command
diagnostics, comparison snapshots and final `manifest.json` are 0600. They are
ignored by Git and Docker. They contain synthetic records and are **not encrypted**.
Never treat them as off-host backups of the retained portfolio. The manifest records
success/failure, timings, checksum, comparisons, recovery and cleanup status.

Ordinary child commands have a 180-second timeout. The interactive snapshot helper
has bounded messages (4 MiB), 90-second host response/dump waits, a 30-second dump
barrier wait, 45-second controller waits and a 120-second idle-transaction timeout.
Its processes are reaped on error, and cleanup explicitly removes only its exact
generated snapshot container if still present. Cleanup is attempted for both generated
projects after success or failure, removing only their own containers/networks and
disposable volumes. Private files remain for inspection. Incomplete cleanup makes
the drill fail; use the exact generated names in the private manifest to inspect
those projects. Do not run a broad Docker prune. An operating-system kill or host
loss can interrupt cleanup; no background cleanup daemon is provided.

The normal Linux pytest suite includes archive-integrity, target-namespace,
non-overwrite/symlink, atomic-restore option, cleanup and comparison guard tests.
CI separately runs the real two-cluster drill. The original quiescent result is in
[its evidence record](evidence/phase-4-local-restore.json); the extended result is in
[the concurrent snapshot evidence](evidence/phase-4-concurrent-restore.json). Hosted checks bind to
the PR's exact commit. Private command logs are not uploaded as CI artifacts; retain
the hosted step result before job logs expire and rerun locally for diagnostics.

## Remaining recovery boundary

This is a small synthetic **logical** backup with controlled concurrent commits, restored between two
clusters on one Docker host. The source remains available for comparison. No real
database/host failure, free-running load/throughput trial, disk exhaustion, power loss, WAL/PITR,
encrypted off-host transfer, backup-gap reconciliation or old-writer fencing is
demonstrated. Synthetic market rows are populated; research, account replay valuation/cursor
and scheduled notification/review tables remain empty. Their populated recovery and
extended-history resource usage remain unqualified. An intact older snapshot is
partial T25 evidence, not evidence that backup-gap reconciliation is implemented.

The recorded restore/verification and end-to-end helper durations are measurements
for this small fixture, not production RTO or RPO. T25 and G2–G6 remain open. A future
live host recovery must still stop/fence the old writer, reconcile the backup gap
against complete exchange evidence, remain disarmed and require separate activation.
Phase 4 is incomplete; no Phase 5 days/episodes or strategy-promotion evidence are
created. All 20 research candidates remain rejected; the final holdout stays closed.
