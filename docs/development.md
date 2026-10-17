# Development

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
contain hashes. Research and network/Discord dependencies are deliberately absent.
CI actions and base images have immutable references. Reverify references, release
notes, licences and advisories before changing them. Native macOS test results do
not substitute for Linux/container CI results.

ADR A18 uses musl-based Alpine 3.24 for the application; ADR A19 uses Alpine for
PostgreSQL. Direct Python package candidates are unchanged. The test image requires
binary wheels for the development lock, and the runtime builder requires them for
the runtime lock. An unavailable wheel fails the build; no silent source-build or
dependency substitution is permitted. Deferred research packages need a separate
compatibility review in their milestone.
