# Protected financial audit prefixes

Application 0.4.17, schema `0015_financial_audits`, 22 October 2026.
This implements part of **P4-C3 condition 4**. It closes no checklist condition:
integrated replay/report deadlines and reviewed retention remain open.
The fixed checklist remains **12/17**; C4 follows C3.

## A30 — operator-owned financial audit authority

A runtime checkpoint or supplied archive checksum does not certify financial
economics. Keep their existing checks. Add an explicitly selected financial audit
record owned by `aca_migrator`, with **SELECT only** for `aca_runtime`. The normal
application, runner and notification containers need no migration credentials and
cannot create these records. No environment setting accepts one.

The operator command acquires the paper writer's account advisory lock; an active
writer makes acceptance fail. A separate read-only REPEATABLE READ runtime
connection reconstructs **every financial event**, verifies incremental journals,
transfer/correction and reserve decisions, compares total journal balances, and
validates all checkpoints. It never uses an older seal. An exact expected account
version, actor and reason are required. After this audit, the migration connection
inserts the result in `paper_financial_audits`. A trigger binds the insert to the
current head and checkpoint count; a foreign key retains its event anchor.
Cancellation/failure discards the locking session and cannot publish a partial
result. It does not change the financial projection.

The append-only record binds the account, event/state hashes, complete typed
snapshot, checkpoint count, installed Python-source SHA-256, application version,
policy hash, Python/financial-dependency versions, timestamp, actor and reason. It explicitly records
`historical_economics_audited=false`, `pruning_authorised=false` and
`live_execution_available=false`. No SECURITY DEFINER insertion function exists.
Readiness rejects runtime table-level or column-level INSERT grants.

This is a trusted operator/database boundary, not a cryptographic signature or a
defence against a compromised migration role, database administrator or host.
Those principals already control schema/history enforcement. It grants no
strategy, risk, research or live permission.

## Financial reconstruction and failure behaviour

With `--audit-prefix UUID`, financial audit/startup restores the protected record
and verifies its fingerprint, account, code/version, financial runtime and policy. The reader must
have no acceptance authority. It verifies the retained event's envelope/state
hashes and counts prefix events/checkpoints to detect missing records or later
checkpoint insertion. It then reconstructs **every suffix event**, checking
chronology, hashes, journals, operator/reserve decisions and suffix checkpoints.
It independently aggregates **all** journal balances against the resulting state.
A suffix over 100,000 events fails and requires a fresh explicit operator audit.

Python financial-state reconstruction is proportional to the suffix. SQL membership
counts and journal aggregates still scan retained data: total validation work is
not constant. All SQL records must remain. A missing, mismatched or obsolete
required seal fails; no fallback or renewal occurs automatically. Omitting the
option explicitly selects complete reconstruction. `paper audit`, `archive-export`
and seal acceptance always use complete reconstruction.

Status/journal reads validate an explicitly supplied seal. Writers validate it
before ownership is granted; ordinary transactions retain their existing guards.
Reports and supervisor status identify it. Historical reports still validate all
historical observations and their economics, including invalid later records when
an older endpoint is selected. This financial seal does not certify public
receipts, historical valuations or archive contents.

Balances, units, high-water marks, policy epochs, loss latches, reserves, orders,
protection, consumed commands, fees and permanent client IDs remain intact.
Sealing uncertainty or invalidated accounting preserves it and cannot rearm it.
Code/policy or Python/financial-dependency changes require a new full audit and explicitly selected seal.
Restarts may reuse a seal only when all bindings and the new suffix pass.

## Explicit operator workflow

Use a reviewed local paper database at schema 0015. Keep a private operator
configuration referring to separately delivered runtime/migration password files;
do not add migration credentials to runner/research configuration or arguments.
Stop the affected runner, review its full audit/version, then run the installed
CLI in that operator environment:

```sh
aca --config /private/operator/paper.toml paper audit --account ACCOUNT_UUID
aca --config /private/operator/paper.toml paper audit-prefix-accept \
  --account ACCOUNT_UUID --actor OPERATOR_UUID --expected-version FINANCIAL_VERSION \
  --reason 'Reviewed full financial prefix before bounded paper operation'
```

Retain the returned audit UUID/fingerprint with the review. Runtime commands use
that UUID and ordinary runtime configuration:

```sh
aca paper status --account ACCOUNT_UUID --audit-prefix AUDIT_UUID
aca paper performance --account ACCOUNT_UUID --audit-prefix AUDIT_UUID
aca paper run --account ACCOUNT_UUID --audit-prefix AUDIT_UUID --duration 300
```

The runner still requires selection, enrolment and healthy evidence. Its existing
operational-only status/zero qualifying-day credit remain. This option may be
combined with `--archive`/`--archive-sha256`; the archive retains its full integrity
and historical economic checks. Background notification processes are not silently
reconfigured. Upgrade/restore must preserve the new records and permissions;
schema downgrade is rejected. An old application cannot run against schema 0015.

## Verification and measured scope

New PostgreSQL tests cover suffix/full equivalence; active writers/expected
versions; runtime and column-grant faults; immutable records; missing/account/code/
policy mismatches; checkpoint insertions and incomplete retained prefixes;
rehashed state forgeries; cancellation/lock cleanup; intermediate balance
invention; suffix budget/renewal; fills, fees, protection, units and hard halts;
uncertain submission reservations; transfer/correction invalidation; CLI selection;
and historical economic rejection after an older selected endpoint.

The populated restore drill seals all six synthetic accounts. It compares complete
and protected audits before/after restore, preserves seal rows/permissions, and
uses explicit seals through two recoveries. Existing financial, uncertainty,
protection, notification and declared-suffix-gap assertions remain. It is local
logical-restore evidence, not full host-loss/WAL/RPO/RTO evidence.

Reproduce on disposable state:

```sh
python3 scripts/ci_database.py --linux
python3 scripts/restore_drill.py
python3 scripts/capacity_trial.py --financial-prefix
python3 scripts/capacity_trial.py
python3 scripts/capacity_trial.py --preservation
```

The new financial-only measurement exercises installed CLI acceptance of 99,984
events, then 16 real suffix writes ending at the original 100,000-event ceiling.
Protected and complete reconstruction must agree on that same final state. The
two investment flows cancel without changing unit price. Worker limits remain
0.75 CPU/768 MiB, database 0.75 CPU/1 GiB; the financial startup deadline remains
30 seconds. Other operational deadlines, record limits and disk budget are
unchanged. This subset has no replay workload, populated order/fill load or 56-day
claim. Original combined replay/startup, historical-report and daily-summary
measurements remain separate. A green measurement job cannot approve C3/G2.

The final local run passed **684 tests in 123.85 seconds**, Ruff checks/formatting,
strict Mypy on 149 source files, and all three dependency advisory audits. The
installed wheel and all final workers use Python-source SHA-256
`79db6dcef858ae0c23bcfe3819c3329af5ed1b19539c48739127f4328ea5ee2e`.
The six-account restore compared 30 tables and passed in 58.371 seconds, with six
protected audit records, unchanged permissions and zero recovery venue commands.

| Financial-only operation | Local seconds | Existing budget |
| --- | ---: | ---: |
| Full financial writer startup | 6.938 | 30 s |
| Protected-prefix writer startup | 0.030 | 30 s |
| Protected startup after 16 new events | 0.024 | 30 s |

The separate full audit/acceptance CLI took 7.351 seconds. Database size was
70,563,519 bytes; worker peak cgroup memory was 183,640,064 bytes under its
805,306,368-byte limit. All 100,000 events remain. Full/protected states match,
the cancelling flows preserve the independently expected £1,000 cash/units,
and owned-project cleanup passed. These are Linux aarch64/Docker Desktop subset
measurements; hosted amd64 measurements are recorded separately in the PR.

[Exact-source evidence](evidence/phase-4-financial-prefix.json) records these
results, provenance and remaining limits. No retained
service is upgraded, migrated, sealed, archived or pruned by this increment.

## Remaining C3 work

This supplies protected financial audit results and suffix reconstruction. It
does not implement financial-event archival, indexed historical aggregate
certificates, an archive catalogue, retention/deletion authority, SQL prefix
removal or 56-day storage headroom. Full historical economic checks and all
report/order/journal/fill ceilings remain. Existing integrated capacity failures
remain until superseding evidence passes. All 20 research candidates are rejected,
the holdout stays closed, G2–G6 remain unapproved, and qualifying Phase 5 observation
has not started.
