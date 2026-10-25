# ACA — Kraken research and local paper execution

The application provides local paper execution and opt-in, credential-free Kraken market capture for BTC/GBP
and ETH/GBP. It validates books, records canonical trades and closed candles, and
preserves gaps and recovery evidence in PostgreSQL. It cannot place an exchange order.

Version 0.4.18 closes the fixed **17/17 Phase 4 engineering conditions** for the
documented bounded local-paper profile. Use the consolidated
[paper release handoff](docs/phase-4-paper-release.md) for setup, operation,
maintenance and the applicable local-paper safety decision. The
[lossless history path](docs/phase-4-bounded-history.md) preserves original evidence
and bounds online work with an explicit operator certificate. It requires stopped-worker
maintenance before 4,096 new financial events/observations; this is not proof of
56-day storage or uptime. Runtime cannot issue certificates or compact history.

**Qualifying paper observation has not started.** All 20 evaluated research
candidates remain rejected, the holdout is closed and operational experiments
earn zero qualifying days/episodes. Engineering completion does not approve
research, deployment or live trading. The retained workstation stack has not been upgraded.

Version 0.4.19 adds [reviewed-data backtesting and walk-forward validation](docs/research-reviewed-validation.md).
All 300 eligible folds now complete using the audited no-trade convention, but
none of the original 20 candidates passes the confidence or economic gates.
No candidate is frozen and the final holdout remains closed.

The [Phase 4a plan](docs/phase-4a.md) turns the
[research and repository review](docs/research-promotion-review-2026-10-24.md)
into a fixed **19-item** research-fidelity and promotion-preparation phase.
It is in progress at **13/19**, with six implementation batches and a bounded study
budget. P4A-17 closes the release-check repair. Version 0.4.20 adds P4A-18's
[modular research output and offline restore](docs/phase-4a-artifacts.md): durable
attempts, bounded pages, deterministic trial resume and a byte-identical archive/
restore of the original report. Version 0.4.21 establishes
[independent research/paper fixtures](docs/phase-4a-parity.md) under P4A-19:
hand-calculated fill/fee/flow/stop examples and named timing/drawdown discrepancy
probes. Version 0.4.22 delivers [Batch B causal execution, separate clocks,
all-observation drawdown and reconciled attribution](docs/phase-4a-causal-replay.md),
closing P4A-01/04/05/06. Trade research and PostgreSQL paper pass the same independent
stop/partial-fill oracle. Version 0.4.23 delivers [Batch C continuous accounts,
equal contributions, comparators and source/cost assurance](docs/phase-4a-continuous-accounts.md),
closing P4A-02/03/07/10/13/14/19. The data review records a blocked/no-go decision
with missing facts retained as qualification blockers.
No corrected S0/S1/S2 study has run.
Phase 4 remains closed at 17/17; qualifying observation has not started.

The [original project prompt and implementation context](docs/context/README.md)
are preserved in the repository for future contributors.

Optional [outbound paper alerts](docs/phase-4-notifications.md) have a separate,
explicit setup. The default stack neither mounts a Discord secret nor sends messages.
[Scheduled summaries and critical reminders](docs/phase-4-scheduled-notifications.md)
extend that optional worker, with [local health and metrics](docs/phase-4-notification-health.md)
when explicitly run in its own container. [Exact-event operator reviews](docs/phase-4-notification-review.md)
preserve failed-delivery evidence while allowing notification health to recover.
See the [current milestone status](docs/status.md),
[fixed Phase 4 completion checklist](docs/phase-4-completion.md) and
[release evidence](docs/evidence/phase-4-paper-release.json). Earlier increment reports
retain their historical failures and scope; the consolidated handoff records the current result.

## Start locally

Install Docker Engine/Desktop with Docker Compose v2.24 or newer, then run:

```sh
python3 scripts/bootstrap.py
```

This command is for a fresh local stack. For an existing deployment, use the
reviewed backup/upgrade procedure; do not start an older image against schema 0016.
The handoff uses a separate project name for a fresh review stack.

The bootstrap uses the host Python's standard library only (3.9+). Application code
runs on CPython 3.14.8 inside the container. It creates local random database secrets,
builds the image, provisions restricted database roles, migrates and waits for health.
It does not install Docker, provision a VPS, send notifications or use exchange credentials.

On macOS the bootstrap also discovers Docker Desktop's standard application bundle
when its CLI is absent from PATH. For the interactive Docker commands below, add it
to the current shell if needed: `export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"`.

```sh
docker compose exec app aca capabilities
docker compose exec app aca db check
docker compose exec app aca audit --limit 10
docker compose exec app aca health
docker compose logs app
docker compose stop
```

Database and metrics ports are not published. For local HTTP inspection, use the CLI
inside the application container. `/health/ready` means **foundation readiness only**.
`/health/trading` always returns 503; `aca_trading_ready` is always zero.

Read [Phase 2 setup and evidence](docs/phase-2.md) to start a finite public capture:

```sh
docker compose --profile market-data run --rm --no-deps market-data aca market capture --duration 300
docker compose exec app aca market status
docker compose exec app aca market evidence --channel instrument_rules --limit 1
```

Default startup remains offline. The opt-in collector alone receives network egress.
`/health/market` on that container reports public feed readiness. Stored evidence and
public feed readiness never imply reconciled account state or trading permission.

The [Phase 3 research workflow](docs/phase-3.md) adds audited historical CSVs,
shared Decimal indicators/signals/sizing, conservative bar simulation and a trial
registry. Research runs locally with the optional hash-locked NumPy environment.
The final year stays reserved until an explicit candidate freeze and holdout command.
No candidate has qualified for promotion.

The [23 October historical gap review](docs/research-data-audit.md) distinguishes
2,833 no-trade spans from one disputed BTC trade-ID range. Original prices and
trial outcomes remain unchanged; no fresh backtest or holdout evaluation is claimed.

The [Phase 4 offline paper core](docs/phase-4.md) adds append-only GBP journals,
investment units, reservations, independent portfolio/dispatch risk checks, a
durable local venue and recovery without command replay. A synthetic demonstration
uses virtual funds and cannot qualify a strategy or count toward extended paper:

```sh
docker compose exec -T app aca paper demo --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper status --account 6e04530b-9382-418d-9a24-331672f4016b
```

Use a fresh account UUID for each replay. [Checkpointed maintenance](docs/phase-4-supervision.md)
adds incremental journal/state projections and an opt-in one-second supervisor for
an **existing virtual account**:

```sh
docker compose exec -T app aca paper audit --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose run --rm --no-deps app aca paper supervise --account 6e04530b-9382-418d-9a24-331672f4016b --duration 60
```

The supervisor needs the separate public collector for healthy GBP marks. Without
it, the account stays suspended. It observes risk, cancels entries and reconciles
the local venue. [Ordered public replay](docs/phase-4-replay.md) adds explicit flat-account
enrolment, atomic venue effects and durable cursors. After enrolment, use
`paper supervise --replay` and a new collector epoch; the runbook gives the full
sequence. The `supervise` command keeps strategy dispatch disabled. The opt-in
[continuous runner](docs/phase-4-continuous-paper.md), `paper run`, connects an
explicit operational strategy selection to this same replay/risk/execution core.
It includes finalised candle warm-up, permanent decision identities, entries, exits
and UTC boundary accounting. It grants zero qualifying paper days or episodes.
C1–C4 are complete for the bounded local-paper release; future exchange and deployed
recovery assertions retain their separate gate allocations in the handoff.
[Historical paper risk](docs/phase-4-historical-risk.md) now preserves original-time
NAV, loss latches and investment flows during replay, with an explicit flat-account
upgrade baseline. `aca paper valuations --account UUID --limit 20` reads its
provisional evidence. [Historical performance](docs/phase-4-performance.md) adds a
read-only, source-bounded GBP report with journal-reconciled contributions, withdrawals,
fees and P&L, flow-neutral TWR and bounded annualised MWR. Use
`aca paper performance --account UUID`; missing valuation evidence leaves returns
unavailable. The report begins at the explicit historical baseline. [Managed-account transfers and corrections](docs/phase-4-accounting.md) add explicit
GBP/BTC/ETH owner flows and immutable reversal/replacement. Corrections invalidate
units/returns and quarantine the account; no unit-history rebuild/rearm is supplied.
The named local safety checks and closeout are linked in the completion checklist. Deployment recovery and sustained reliability
have separate later-stage acceptance. Outbound delivery and scheduled notices are
opt-in capabilities;
real destination integration and alert latency qualification remain outstanding.

[Manual tax-cash reserves](docs/phase-4-tax-cash-reserve.md) add an audited, version-bound
operator earmark of virtual GBP. `aca paper tax-reserves --account UUID` reads the
current reserve and decision history. It reduces spendable cash while preserving
NAV and return history; it is not a tax estimate or a completed tax workflow.

[Process-kill recovery validation](docs/phase-4-process-recovery.md) exercises
abrupt writer death and recovery in separate interpreters using a disposable
PostgreSQL database. A [separate local restore drill](docs/phase-4-local-restore.md)
captures one exported snapshot while controlled synthetic paper commits advance
the source, then restores that prefix into a fresh disposable cluster. It checks
data/permissions, safe sequence counters and recovery without command replay.
Off-host backups and WAL/disaster recovery remain deployment/readiness work. The completion checklist
records the completed local-paper restore checks and the remaining deployed recovery boundary.

See [development](docs/development.md), [operations](docs/operations.md),
[architecture](docs/architecture.md) and [milestone evidence](docs/phase-1.md).
The [replay preservation format](docs/phase-4-replay-preservation.md) also supports
lossless file export and offline verification within its documented limits.
Local Docker functional checks and both runtime image scans pass. The full Linux
suite passes on arm64 and on amd64 under emulation. Hosted GitHub Actions results
are recorded in the milestone report; see the [image security review](docs/image-review.md).

## Safety boundary

Only paper mode exists. Unknown `ACA_` environment variables and unknown TOML keys
are errors. `ACA_CONFIG` selects a TOML file; `ACA_MODE=paper` is the sole mode override.
Risk policy v1 is immutable. Its limits constrain historical research and the
offline operational paper core. Durable hard/soft halts require audited review.

No authenticated account access, final tax engine or live activation exists.
The continuous operational runner uses only the durable local paper venue. Paper reconciliation uses only the local
simulator's records; it is not evidence of Kraken account reconciliation.
Subsequent milestones require review; live order-writer implementation and activation
require separate explicit authorisation under the approved plan.
