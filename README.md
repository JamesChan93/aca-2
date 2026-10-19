# ACA — Kraken research and local paper execution

Phases 1–3 and the Phase 4 paper core, maintenance, ordered replay and historical risk increments. The application provides a
paper-only foundation and opt-in, credential-free Kraken market capture for BTC/GBP
and ETH/GBP. It validates books, records canonical trades and closed candles, and
preserves gaps and recovery evidence in PostgreSQL. It cannot place an exchange order.

The [original project prompt and implementation context](docs/context/README.md)
are preserved in the repository for future contributors.

Optional [outbound paper alerts](docs/phase-4-notifications.md) have a separate,
explicit setup. The default stack neither mounts a Discord secret nor sends messages.
[Scheduled summaries and critical reminders](docs/phase-4-scheduled-notifications.md)
extend that optional worker, with [local health and metrics](docs/phase-4-notification-health.md)
when explicitly run in its own container. [Exact-event operator reviews](docs/phase-4-notification-review.md)
preserve failed-delivery evidence while allowing notification health to recover. See the [current milestone status](docs/status.md) for
the remaining Phase 4 work and the separate Phase 5 observation requirements.

## Start locally

Install Docker Engine/Desktop with Docker Compose v2.24 or newer, then run:

```sh
python3 scripts/bootstrap.py
```

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
sequence. Strategy dispatch remains disabled. This does not complete Phase 4 or G2.
[Historical paper risk](docs/phase-4-historical-risk.md) now preserves original-time
NAV, loss latches and investment flows during replay, with an explicit flat-account
upgrade baseline. `aca paper valuations --account UUID --limit 20` reads its
provisional evidence. [Historical performance](docs/phase-4-performance.md) adds a
read-only, source-bounded GBP report with journal-reconciled contributions, withdrawals,
fees and P&L, flow-neutral TWR and bounded annualised MWR. Use
`aca paper performance --account UUID`; missing valuation evidence leaves returns
unavailable. The report begins at the explicit historical baseline. Remaining
accounting workflows, strategy scheduling and resource/disaster trials precede
extended paper. Outbound delivery and scheduled notices are opt-in capabilities;
real destination integration and alert latency qualification remain outstanding.

See [development](docs/development.md), [operations](docs/operations.md),
[architecture](docs/architecture.md) and [milestone evidence](docs/phase-1.md).
Local Docker functional checks and both runtime image scans pass. The full Linux
suite passes on arm64 and on amd64 under emulation. Hosted GitHub Actions results
are recorded in the milestone report; see the [image security review](docs/image-review.md).

## Safety boundary

Only paper mode exists. Unknown `ACA_` environment variables and unknown TOML keys
are errors. `ACA_CONFIG` selects a TOML file; `ACA_MODE=paper` is the sole mode override.
Risk policy v1 is immutable. Its limits constrain historical research and the
offline operational paper core. Durable hard/soft halts require audited review.

No authenticated account access, continuous strategy trading service, final tax
engine or live activation exists. Paper reconciliation uses only the local
simulator's records; it is not evidence of Kraken account reconciliation.
Subsequent milestones require review; live order-writer implementation and activation
require separate explicit authorisation under the approved plan.
