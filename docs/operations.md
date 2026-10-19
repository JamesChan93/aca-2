# Foundation and public-capture operations

For optional delivery of existing paper alerts, see the
[notification setup and failure procedure](phase-4-notifications.md). It is disabled
in the default stack and has no trading authority.

This is a local paper-only application with virtual accounts and a local venue.
Do not use it to supervise a live account. Future emergency trading procedures in the
approved specification are not implemented by these commands.

## Start, inspect and stop

```sh
python3 scripts/bootstrap.py
docker compose exec app aca config check
docker compose exec app aca db check
docker compose exec app aca audit --limit 20
docker compose exec app aca health
docker compose logs --tail 100 app
docker compose stop
```

`docker compose down` retains the named database volume. Do not add `--volumes` unless
you intentionally want to discard the local development database. No automatic
cleanup or reset command deletes history.

The hardened database uses `postgres_alpine_data`. The old `postgres_data` declaration
is retained for rollback. Existing Debian clusters require logical migration before
ordinary bootstrap; the new volume is a separate cluster and does not discover data
in the old one automatically. Never point an Alpine container at the old glibc data
directory or change ownership to make that directory start.

Provision/migrate containers terminate after success. PostgreSQL and app restart into
paper-only operation. Startup checks the schema revision and restricted runtime role,
persists a configuration snapshot/startup event, then marks foundation readiness.
The app rechecks every two seconds. A failed/stale database check clears readiness.
SIGTERM stops health serving and attempts a bounded shutdown audit write.

## Health

For an enrolled virtual account, export original-time performance with
`docker compose exec -T app aca paper performance --account UUID > report.json`.
The [performance runbook](phase-4-performance.md) explains exact source boundaries,
financial audit requirements and unavailable returns. This query uses a database
read-only snapshot and can run alongside the paper writer.

| Route | Meaning |
| --- | --- |
| GET /health/live | HTTP loop alive; not proof of database or trading safety |
| GET /health/ready | Valid runtime role, expected schema and persisted startup record |
| GET /health/trading | Always 503, with incomplete requirements |
| GET /metrics | Local Prometheus exposition; trading readiness always zero |

There are no command routes. HTTP methods other than GET return 405. Requests and
connection counts are bounded, and idle readers time out. Bind to localhost for native
development. The Compose bind is on its internal bridge only; do not publish it.

## Secrets and logs

`.secrets` must be owned by the operator with mode 0700 and is ignored by Git and Docker.
Generated files are mode 0444 **inside that private directory**, allowing read-only
Compose binds to be read by different non-root container UIDs. Compose file secrets
do not reliably remap file ownership. Do not move these files into a public directory
or weaken the parent directory. Native manually supplied files may use mode 0600.

App gets only the runtime password; migrate gets only the migration password; provision
gets all three explicitly; PostgreSQL gets the admin password. Configuration snapshots
contain file paths but never file contents. Log processing redacts sensitive keys,
registered secret values, URL credentials and Discord webhook URLs; raw exception
messages/tracebacks are suppressed. CLI validation errors never echo untrusted input.

## Failures

- Invalid configuration/secret: CLI exits 2. Correct local settings/permissions; never
  try a live-mode switch. Secrets are not reprinted to explain the error.
- Database failure or schema mismatch: readiness 503; inspect database health, migrations
  and fixed role grants. Liveness may remain 200. No application command can trade.
- Lost secret files with an existing volume: restore the matching files from your
  protected local copy. Bootstrap does not silently rotate existing database roles.
- Disk alarm/exhaustion: stop the app, inspect host/volume usage and recover storage.
  Do not remove audit tables or apply an unreviewed downgrade.
- Suspected host compromise: stop using the environment; rebuild from reviewed images.
  No exchange credential should be present at this milestone.

## Upgrade and recovery boundary

Stop the app, take a PostgreSQL backup using a separate administrative process, review
the migration, deploy the pinned image, migrate and inspect health/audit. The initial
migration deliberately refuses downgrade because it would remove audit history.

## Recorded local Debian-to-Alpine transition

On 17 October 2026, the app was stopped and a PostgreSQL custom-format logical backup
was written under `.secrets/backups/alpine-<UTC timestamp>/aca.dump`. Its directory is
0700, files are 0600, and it is excluded from Git and Docker build contexts. The
adjacent manifest records the source image/volume, dump SHA-256, row counts and
canonical content hashes. The original `aca-paper_postgres_data` volume is retained.

A new `aca-paper_postgres_alpine_data` volume was initialised using the hardened
PostgreSQL 18.6 image. Roles were provisioned from the existing secret files, then
the dump was restored with `pg_restore --single-transaction --exit-on-error` before
running migrations or starting the app. Restored records were identical: one Alembic
revision, one configuration record and nine audit records. Every table's hash matched
before app startup appended new events.

For another pre-Alpine local checkout, stop the app and make its protected backup
**before** starting the new database service. Provision roles, restore the complete
dump, compare record counts/content and check runtime privileges before bootstrap.
Do not run the baseline migration first against the restore target. A failed restore
leaves the app stopped; retain the dump and original volume. Rollback needs an explicit
Compose override selecting the original pinned Debian image and `postgres_data`
mount, with `--no-build`; never reuse the Alpine volume with the Debian image. This
local transition does not implement production backup schedules or disaster recovery.

Automated encrypted off-host backups, WAL archiving, restore schedules/RPO/RTO evidence,
external monitoring and Discord delivery are still required before production readiness.
The local persistent volume and connection-restart tests are not disaster recovery.
No paid service or VPS is provisioned by bootstrap.


## Public-market capture

See [Phase 2](phase-2.md) for the opt-in profile, finite capture, health and evidence
queries. The new `0002_public_market` migration adds append-only public receipts and
canonical trades; it does not alter financial history. Stop capture before upgrades.
A Phase 1 binary rejects the new schema revision, so application rollback requires
a compatible reviewed image rather than an automatic Alembic downgrade.

## Phase 3 local research publication

Back up the local database before moving from 0.2.0 to 0.3.0, then run the documented
bootstrap to apply `0003_research`. This migration only adds append-only research
artefact/event tables and runtime SELECT/INSERT grants; market/configuration/audit
history remains intact. A downgrade is deliberately unsupported. Runtime images
contain no NumPy; perform statistical work on the local research environment, then
use the read-only bind-mount publication command in [Phase 3](phase-3.md).

Retain all local run starts and failures, not just the latest report. Archive CSV
objects/manifests/events/reports together. Publication records are append-only
against ordinary roles, not tamper-proof against an administrator. There is no
research-result path to live activation. Historical bar results do not establish
operational risk enforcement, exchange protection behaviour or the paper/live gates.

## Phase 4 offline paper core

Back up before upgrading 0.3.0 to 0.4.0. Bootstrap applies the additive `0004_paper`
revision and preserves existing market/audit/research records. Keep the pre-upgrade
dump in a private directory with mode 0600; this local dump is not a disaster-restore
drill or proof of RPO/RTO. A blind database downgrade remains unsupported.

Follow the [paper runbook](phase-4.md) for virtual demos, replay, status, journal CSV,
local alerts, recovery, selective entry cancellation and audited epoch review.
An uncertain consumed command stays reserved and is never replayed. Halt preserves
protective sells. Do not use these simulated account records as evidence of actual
Kraken balances, completed operational paper observation or a live approval.

The default foundation process does not schedule strategies or supervise an account.
The [checkpoint/supervision runbook](phase-4-supervision.md) covers 0.4.1 maintenance.
Release 0.4.2 adds [ordered public replay](phase-4-replay.md), with flat-account
enrolment, a fresh collector epoch and explicit `supervise --replay`. Back up before
the additive `0006_replay` / `0007_replay_cutoff` migrations. Release 0.4.3 adds [historical paper valuation/risk](phase-4-historical-risk.md).
Back up before `0008_historical_risk`; legacy replay accounts need explicit flat,
reconciled enrolment at their existing drained cursor. No earlier risk is backfilled.
See the [current status](status.md) for remaining Phase 4 engineering. Outbound
alerts are available through the separately configured notification worker, with
[scheduled summaries and critical repeats](phase-4-scheduled-notifications.md) in
0.4.6. Back up before `0010_scheduled_notices`; stop workers, migrate with the
migration role, then deploy the compatible image. The default service does not
prepare or deliver notices. Preview with `aca notifications prepare --account UUID`
and `aca notifications notices --account UUID`. These commands need no Discord
secret or egress; `prepare` appends notification records only.
Live execution, exchange-side fencing and activation remain separately authorised work.
