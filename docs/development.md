# Development

The [Phase 4 core report](phase-4.md) documents the new paper contracts, synthetic
replay and limitations. Financial properties use 1,000 generated examples; lifecycle
checks run 10,000 deterministic fault traces. Use the isolated test project only;
do not run two `scripts/ci_database.py` processes concurrently because they share
that project's database/port. Application and test Compose projects remain separate.

The [process-recovery suite](phase-4-process-recovery.md) additionally kills real
paper-writer interpreters at specific durable boundaries and recovers each account
in fresh interpreters. It runs in the ordinary full Linux suite and uses only the
disposable test database. These tests do not kill retained services or demonstrate
database/host disaster recovery.

The [local logical-restore drill](phase-4-local-restore.md) runs separately with
`python3 scripts/restore_drill.py`. It needs only Docker and host Python 3.9+, uses
two random disposable projects without host ports, and restores synthetic accounts
into a fresh cluster. CI runs this real drill after the full Linux suite. Its
archive/target/comparison guard tests are also part of pytest. It does not back up
or restart retained services or prove production RPO/RTO.

The [manual tax-cash reserve workflow](phase-4-tax-cash-reserve.md) adds a sealed
decision/event transaction, read-only history and original-time report controls.
Its integration tests include a populated 0011→0012 upgrade, and the restore drill
includes a nonzero reserve. All validation targets disposable databases; it does
not change the retained paper account's reserve or deploy the new schema there.

## Prerequisites

Application: CPython 3.14.8 (standard GIL build). Database: PostgreSQL 18.6.
Recommended local route: Docker Engine/Desktop with Compose v2.24+ and host Python 3.9+.
The host bootstrap script does not download/install Docker or create cloud resources.

```sh
python3 scripts/bootstrap.py
```

For native development with a separately managed PostgreSQL:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/development.lock
.venv/bin/python -m pip install --require-hashes -r requirements/research.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/aca --config configs/development.toml config check
```

Create database secret files using `scripts/bootstrap.py`'s `prepare_secrets()` helper,
and configure your local **development-only** aca_admin role/database. Never repurpose
a production cluster for these integration tests.

## Checks

```sh
ruff check src tests scripts
ruff format --check src tests scripts
mypy
python -m pytest
python -m pip check
python -m build --no-isolation
pip-audit --disable-pip --require-hashes -r requirements/runtime.lock
pip-audit --disable-pip --require-hashes -r requirements/development.lock
```

Without a test database, PostgreSQL tests skip explicitly. CI requires them and fails
if its database connection is absent. To run all tests using Docker:

```sh
python scripts/ci_database.py
python3 scripts/ci_database.py --linux
```

The first command runs pytest with the host's installed development dependencies
against Docker PostgreSQL. The second builds the hash-locked `test` image stage and
runs the entire suite on Linux, as UID 10001, against a dedicated Docker PostgreSQL
instance. It needs only host Python 3.9+ and Docker. CI uses this Linux command.
Both run against the isolated `aca-foundation-tests` project. Development tools,
test credentials and test source are absent from the runtime image.

Verification installs the built wheel, including its migrations. Native development
uses a regular wheel install; reinstall after source changes. An editable install
requires extra build tooling that is intentionally outside the verified lock.

Alternatively set TEST_DATABASE_URL to a loopback PostgreSQL admin DSN for an isolated
cluster with user aca_admin, and REQUIRE_POSTGRES=1. Tests create/drop only a new,
randomly named `aca_test_...` database. They create cluster-level aca_runtime and
aca_migrator roles, so do not run them on a shared/production cluster. Optional
TEST_RUNTIME_PASSWORD_FILE and TEST_MIGRATION_PASSWORD_FILE preserve existing local
role credentials in an existing disposable cluster. `ci_database.py` uses its own
`aca-foundation-tests` Compose project and does not stop the ordinary paper stack.

Generated database credentials are never printed. TEST_DATABASE_URL is test-only;
the application cannot read database URLs from environment variables. Avoid shell
history containing actual secrets.

The test-only `compose.test.yaml` publishes PostgreSQL on loopback port 55432 and
attaches the test database to an additional bridge so Docker Desktop can forward it.
Ordinary `compose.yaml` publishes no ports. CI then tests the ordinary app container,
including a process restart and persistent audit history.

## Configuration

CLI: `aca --config PATH ...`. Otherwise ACA_CONFIG may select a TOML file; otherwise
safe code defaults apply. ACA_MODE may only be `paper`. Every other ACA_ variable is
rejected, including plausible accidental live switches. `.env` is not loaded.

Unknown TOML fields and non-string financial numbers fail validation. Nested settings
are frozen. Policy v1 values cannot be edited without implementing a reviewed new
policy version. No credentials are accepted in TOML; only secret-file paths.

Migrations are in the installed wheel. Run `aca db provision --admin-password-file PATH`
explicitly with the local admin credential, then `aca db migrate` using the migration
credential. App startup never creates schema or grants itself permissions.

## Dependency changes

See requirements/README.md. Runtime, development and build locks are separate and
contain hashes. HTTPX/websockets support public reads. Optional NumPy research dependencies have a separate lock; Discord remains absent.
CI actions and base images have immutable references. Reverify references, release
notes, licences and advisories before changing them. Native macOS test results do
not substitute for Linux/container CI results.

ADR A18 uses musl-based Alpine 3.24 for the application; ADR A19 uses Alpine for
PostgreSQL. Direct Python package candidates are unchanged. The test image requires
binary wheels for the development lock, and the runtime builder requires them for
the runtime lock. An unavailable wheel fails the build; no silent source-build or
dependency substitution is permitted. Remaining deferred research packages need a separate compatibility review.
