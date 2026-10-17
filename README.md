# ACA — paper-only foundation

Phase 1 of the approved Kraken Spot platform plan. The current application provides
configuration validation, PostgreSQL configuration/audit persistence, local health
and metrics, and operational tooling. It cannot connect to an exchange or place an order.

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

No exchange connectivity, financial ledger, risk execution, strategy, simulator,
reconciliation, tax engine, Discord sender or live activation exists in this milestone.
Subsequent milestones require review; live order-writer implementation and activation
require separate explicit authorisation under the approved plan.
