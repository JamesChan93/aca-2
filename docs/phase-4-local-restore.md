# Phase 4 — Disposable local logical-restore drill

This increment adds a repeatable PostgreSQL logical-restore drill and runs it in
CI. Application code stays at 0.4.8 and schema `0011_notification_reviews`. No
migration, retained-service restart, strategy selection or exchange access is
part of the drill.

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
alerts and checkpoints. Every source writer closes before backup. Four cases are
checked after restoration:

| Backed-up case | Recovery assertion |
| --- | --- |
| Reserved `VALIDATED` entry never consumed | Expires, releases reservation, never submits |
| Consumed entry with no authoritative order | Remains `UNKNOWN`, retains funds, rejects another entry and never resubmits |
| Accepted entry with a venue fill not yet financially posted | Discovers protective child; posts one fill/fee journal; GBP cash and BTC quantity match actual fill economics |
| Posted position with a manual hard halt | Keeps halt/epoch, balances, fill, journal and protective exit; rejects another entry |

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

Before recovery, all 24 application tables are compared using row counts and
length-framed SHA-256 of sorted PostgreSQL JSON rows. Empty tables are included but
do not establish populated-data recovery for their workflows. Sequence values and
selected catalog definitions also match: ownership/ACLs, columns/defaults,
constraints with validation/enforcement/deferral flags, indexes, history triggers,
functions, default privileges, role attributes and database permissions. Runtime
verification and full financial/event reconstruction run on both copies.

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
original consumed or expired client IDs cannot be dispatched. The source snapshot
is checked again to prove the restore/recovery work left it unchanged.

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

Each child command has a 180-second timeout. Cleanup is attempted for both generated
projects after success or failure, removing only their own containers/networks and
disposable volumes. Private files remain for inspection. Incomplete cleanup makes
the drill fail; use the exact generated names in the private manifest to inspect
those projects. Do not run a broad Docker prune. An operating-system kill or host
loss can interrupt cleanup; no background cleanup daemon is provided.

The normal Linux pytest suite includes archive-integrity, target-namespace,
non-overwrite/symlink, atomic-restore option, cleanup and comparison guard tests.
CI separately runs the real two-cluster drill. The measured local result is in
[the evidence record](evidence/phase-4-local-restore.json); hosted checks bind to
the PR's exact commit. Private command logs are not uploaded as CI artifacts; retain
the hosted step result before job logs expire and rerun locally for diagnostics.

## Remaining recovery boundary

This is a small, quiescent synthetic **logical** backup restored between two
clusters on one Docker host. The source remains available for comparison. No real
database/host failure, concurrent-backup load, disk exhaustion, power loss, WAL/PITR,
encrypted off-host transfer, backup-gap reconciliation or old-writer fencing is
demonstrated. Empty market/research/replay/notification tables in this fixture do not
qualify restoration of populated histories in those areas.

The recorded restore/verification and end-to-end helper durations are measurements
for this small fixture, not production RTO or RPO. T25 and G2–G6 remain open. A future
live host recovery must still stop/fence the old writer, reconcile the backup gap
against complete exchange evidence, remain disarmed and require separate activation.
Phase 4 is incomplete; no Phase 5 days/episodes or strategy-promotion evidence are
created. All 20 research candidates remain rejected; the final holdout stays closed.
