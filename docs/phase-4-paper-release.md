# Local paper release handoff — P4-C4

Candidate 0.4.18 / schema `0016_history_segments`, 22 October 2026.
Final acceptance results are recorded in
[the release evidence](evidence/phase-4-paper-release.json).
This is the consolidated handoff for the fixed C1–C4 checklist. It adds no new
work package. The exact-head hosted results are also recorded in the release PR.

## Engineering and gate decision

**Phase 4 engineering is complete: C1–C4, 17/17 fixed conditions.** C1/C2 retain
their accepted evidence and pass the integrated regression. C3 condition 4 closes
through its reviewed lossless-retention alternative and passing bounded-operation
measurements. C4 is this packaged runbook, full coverage review, linked acceptance
and explicit separation of later gates. Earlier 12/17 reports describe earlier releases.

**Applicable local-paper G2 decision: pass for the documented bounded profile.**
The review is limited to applicable paper assertions in T00–T32.
The release evidence reproduces that full inventory, current test commands,
financial reconciliation, capacity/restore results and later-stage allocations.
Private Kraken mutation/authentication semantics, host-loss/WAL recovery and full
T25 are outside this local release; they are not marked passed by paper tests.
No G3/G4/G5/G6 approval, research promotion or live permission follows from
engineering completion or green CI.

The frozen Python source contains 118 files, SHA-256
`4a8e92759b83c38ad1183c41bd829e159c285c2e00b7a4164d61cd85ef8f4fb9`.
The capacity and installed-CLI workers independently reported that same hash.
Python 3.14.8/PostgreSQL 18.6 and all dependency locks/base-image digests are unchanged.
No applicable local-paper safety failure remains unresolved in this packet.

| Release check | Observed result |
| --- | --- |
| Full Linux/PostgreSQL regression | 702 passed in 171.23 seconds, including 1,000-example financial properties and 10,000 generated lifecycle sequences |
| Static/dependency checks | Ruff passes for 180 files; strict Mypy passes for 155 files; runtime/research/development advisory audits pass |
| Capacity input | 100,000 original financial events and observations, lossless compaction, then 4,096 new events/observations; indexed/full economics agree |
| Writer startup | 0.700 s against unchanged 30 s budget |
| Combined replay startup | 1.941 s against unchanged 20 s budget |
| Replay audit after owner acquisition | 1.092 s against unchanged 10 s budget |
| Historical report | 0.764 s against unchanged 20 s budget |
| Scheduled daily summary | 0.846 s against unchanged 15 s budget |
| One-second diagnostic maintenance loop | 20/20 healthy ticks, maximum 0.090 s, zero venue commands; separate account without market/replay workload |
| Resource measurement | 0.75 CPU / 768 MiB worker cap; 130,232,320-byte cgroup memory peak; 564,672,191-byte total fixture database including its separate diagnostic account |
| Offline maintenance | Full audit/compaction took 78.535 s; workers must remain stopped |
| Populated logical restore | 32 tables, six synthetic accounts, actual compacted history, atomic failed-restore rollback, permissions/economics equal, two recoveries with zero venue commands; 35.680 s end-to-end |
| Actual WAL ENOSPC | Verified disposable 384 MiB RAM volume; zero venue commands, reservation retained, disarmed recovery; unhealthy upper bound 0.293 s and controlled notification-attempt upper bound 6.187 s |
| Installed CLI handoff | Five runner ticks, actual health HTTP read, halt/compaction/recovery/report checks and invalid-authority rejection; zero orders/days/episodes |

All disposable helpers completed cleanup. The table reports local measurements;
hosted timing may differ and its acceptance job must pass the same budgets. The
capacity seed is not ingestion-throughput evidence, does not cover simultaneous
maximum order/journal/fill traffic, and does not establish 56-day storage/uptime.
Old SQL/file-prefix measurement jobs remain diagnostic; their green job status is
not a timing pass. The new bounded-history job fails when its measured budgets fail.

All 20 research candidates remain rejected and the holdout remains closed.
Qualifying Phase 5 observation **cannot start** from an operational test selection;
qualifying days and episodes remain zero. This is separate from finishing Phase 4
engineering. A rejected candidate does not create more Phase 4 coding requirements.

## Fresh local setup

The retained workstation stack is not upgraded by this release. Use the existing
backup/upgrade procedure in [operations](operations.md) for a separately reviewed
retained deployment. For a new development stack, use a distinct project name:

```sh
COMPOSE_PROJECT_NAME=aca-paper-review python3 scripts/bootstrap.py
docker compose -p aca-paper-review exec -T app aca capabilities
docker compose -p aca-paper-review exec -T app aca db check
docker compose -p aca-paper-review exec -T app aca health
```

The bootstrap needs Docker and host Python 3.9+, uses the locked Python 3.14 image,
creates/provisions its local database and waits for foundation readiness. No
exchange or Discord credentials are needed. Never point an older image at schema
0016. `/health/trading` remains unavailable; foundation readiness is distinct from
paper feed/reconciliation readiness.

## Create, select and capture

Use a fresh virtual account UUID; the following GBP contribution is virtual.

```sh
paper_account=$(python3 -c 'import uuid; print(uuid.uuid4())')
flow_time=$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')
docker compose -p aca-paper-review exec -T app aca paper create --account "$paper_account"
docker compose -p aca-paper-review exec -T app aca paper fund --account "$paper_account" --reference opening --amount-gbp 250 --occurred-at "$flow_time"
docker compose -p aca-paper-review exec -T app aca paper select --account "$paper_account" --strategy sma200 --symbol BTC/GBP --interval 240 --operational-experiment
docker compose -p aca-paper-review exec -T app aca paper enrol --account "$paper_account"
```

Start the opt-in collector in its own terminal/container. Enrol first, then begin a
new collector epoch. Stop any previous collector before starting another.

```sh
docker compose -p aca-paper-review --profile market-data run --rm --no-deps market-data aca market capture --duration 0
docker compose -p aca-paper-review exec -T app aca market status
```

The collector checks book CRC/freshness and records gaps, trades and closed candles.
Strategies require the existing 200-bar past-only warm-up, contiguous finalised
bars and a fresh decision; no order is promised merely because a process starts.
The deterministic lifecycle regression supplies entry/fill/fee/protection/exit and
restart coverage separately from the handoff smoke's deliberately blocked runner.

## Run and inspect

For an initial small un-compacted account:

```sh
docker compose -p aca-paper-review run --rm --no-deps app aca paper run --account "$paper_account" --duration 300
docker compose -p aca-paper-review exec -T app aca paper status --account "$paper_account"
docker compose -p aca-paper-review exec -T app aca paper decisions --account "$paper_account"
docker compose -p aca-paper-review exec -T app aca paper performance --account "$paper_account"
docker compose -p aca-paper-review exec -T app aca paper daily --account "$paper_account" --day YYYY-MM-DD
```

`/health/paper` on the runner container reports its cached supervision state;
no host port is published. Read it through the container/private network rather
than exposing an inbound administration API. The process observes risk every
second. Missing/stale evidence, backlog, uncertainty, loss latches, missing
protection, storage pressure and expired decisions block entries. Default `serve`
does not start capture, a strategy or notifications.

## Halt, maintain history and restart

Stop the runner through SIGTERM/Ctrl-C and wait for bounded shutdown. For a hard
halt while it is stopped:

```sh
docker compose -p aca-paper-review exec -T app aca paper halt --account "$paper_account" --reason 'Manual paper review'
docker compose -p aca-paper-review exec -T app aca paper audit --account "$paper_account"
```

A hard halt cancels entries and preserves protection. `recover` reconciles; it
does not clear a hard/soft latch, erase inception losses or approve a restart.
The audited `review` action is a separate explicit operator decision under the
existing policy. Contributions and container restarts cannot supply that decision.

Before long operation, follow [A31's bounded profile](phase-4-bounded-history.md).
Stop the runner, collector and optional notifier. Keep ordinary runtime credentials
separate from a private operator configuration containing the migration secret.
Review current status/performance and record their exact version/source head.
In that operator environment run:

```sh
aca --config /private/operator/paper.toml paper history-compact --account ACCOUNT_UUID --actor OPERATOR_UUID --reason 'Reviewed stopped-worker lossless paper history maintenance' --expected-version FINANCIAL_VERSION --expected-source PUBLIC_SOURCE
```

Retain the returned certificate UUID/fingerprint with the maintenance evidence.
Restart with the explicitly returned UUID:

```sh
docker compose -p aca-paper-review run --rm --no-deps app aca paper run --account ACCOUNT_UUID --history-prefix CERTIFICATE_UUID --duration 300
docker compose -p aca-paper-review exec -T app aca paper status --account ACCOUNT_UUID --history-prefix CERTIFICATE_UUID
docker compose -p aca-paper-review exec -T app aca paper performance --account ACCOUNT_UUID --history-prefix CERTIFICATE_UUID
docker compose -p aca-paper-review exec -T app aca paper daily --account ACCOUNT_UUID --history-prefix CERTIFICATE_UUID --day YYYY-MM-DD
docker compose -p aca-paper-review exec -T app aca paper recover --account ACCOUNT_UUID --history-prefix CERTIFICATE_UUID
```

Run collector recovery as a new epoch; record interrupted coverage and never
invent candles/healthy observations across the gap. Reconcile before further
decisions. Maintain before 4,096 new observations/financial events accumulate;
new entries reserve additional financial headroom. This is about 68 minutes at
one observation per second and less at higher input rates. Current compaction
supports one enrolled account. Keep the 2 GB/disk, order, fill, journal and archive
limits; exhaustion blocks operation. Scheduling/provisioning maintenance and
measuring actual capture throughput are deployment work. The bounded engineering
trial does not establish eight-week uptime or storage headroom for a real feed.

Optional notification delivery needs an explicitly authorised destination/secret
and its separate worker. After compaction pass the same `--history-prefix` to
`notifications prepare`/`notifications run` for bounded daily reporting. Runtime
notification processes need no migration secret. Discord failure cannot replay
an order; a delivery failure review never changes portfolio/risk state.

## Restore, faults and verification

Use the existing [incident runbooks](operations.md) for lost acknowledgements,
missing protection, database/disk failure and manual exchange intervention in a
future live release. UNKNOWN retains its reservation and is never retried as failed.
For this local paper release, reproduce on disposable resources:

```sh
python3 scripts/ci_database.py --linux
python3 scripts/storage_fault_drill.py
python3 scripts/restore_drill.py
python3 scripts/capacity_trial.py --compaction
python3 scripts/paper_release_smoke.py
```

The restore drill includes compacted history and compares data/roles/constraints,
then performs two recoveries with zero venue commands. A declared missing suffix
blocks observation rather than pretending absent evidence was recovered. Off-host
WAL/host-loss recovery, fencing and production RPO/RTO remain G4/G5 work.

The installed-CLI smoke creates/funds/selects/enrols a new account, supplies
deterministic public fixtures, runs the actual strategy process, reads its health,
halts, compacts, rejects missing authority/certificates, recovers and checks reports.
It places zero orders and earns zero qualifying days/episodes. Its collector input
is synthetic; it does not claim fresh public Kraken connectivity or alpha.
The full signal-driven lifecycle is covered by the same installed package's C1
integration tests. The machine-readable packet records exact source fingerprints,
test output, measurements, restore and packaged handoff results.
