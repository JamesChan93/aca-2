# ACA — public market data and paper-only foundation

Phases 1–2 of the approved Kraken Spot platform plan. The application provides a
paper-only foundation and opt-in, credential-free Kraken market capture for BTC/GBP
and ETH/GBP. It validates books, records canonical trades and closed candles, and
preserves gaps and recovery evidence in PostgreSQL. It cannot place an exchange order.

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

See [development](docs/development.md), [operations](docs/operations.md),
[architecture](docs/architecture.md) and [milestone evidence](docs/phase-1.md).
Local Docker functional checks and both runtime image scans pass. The full Linux
suite passes on arm64 and on amd64 under emulation. Hosted GitHub Actions results
are recorded in the milestone report; see the [image security review](docs/image-review.md).

## Safety boundary

Only paper mode exists. Unknown `ACA_` environment variables and unknown TOML keys
are errors. `ACA_CONFIG` selects a TOML file; `ACA_MODE=paper` is the sole mode override.
Risk policy v1 is immutable. The policy is configuration only: its trading enforcement
is a later milestone and is **not implemented** here.

No authenticated account access, financial ledger, risk execution, strategy, simulator,
reconciliation, tax engine, Discord sender or live activation exists in this release.
Subsequent milestones require review; live order-writer implementation and activation
require separate explicit authorisation under the approved plan.
