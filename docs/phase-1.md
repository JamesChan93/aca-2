# Phase 1 milestone and safety register

Status: Phase 1 implemented; native and full Linux tests passed. The image security
blocker is resolved by ADRs A17–A20. The first hosted
[GitHub Actions run passed](https://github.com/JamesChan93/aca-2/actions/runs/39068630968)
for implementation commit `70bd4c0977b79f6c6517b5ea67f94dee8fa3442e` in
[JamesChan93/aca-2](https://github.com/JamesChan93/aca-2).
This file is the milestone review record. Passing local checks does
not authorise the next phase or any real-money operation.

## Delivered scope

- Installable Python 3.14 package and argparse CLI.
- Strict deterministic TOML settings, paper-only mode, immutable provisional policy.
- Exact Decimal/NUMERIC boundary and aware UTC timestamp validation.
- SQLAlchemy/Psycopg async PostgreSQL access; dedicated role provisioning and Alembic migration.
- Append-only configuration/audit tables, safe structured logs, bounded health and metrics.
- Non-root, resource-bounded Docker image/Compose bootstrap and pinned CI workflow.
- Meaningful configuration, property, secret, health, PostgreSQL permission/persistence,
  transaction and process lifecycle tests.

## Acceptance evidence

Observed locally on 17 October 2026:

| Check | Observed result |
| --- | --- |
| Complete pytest suite, REQUIRE_POSTGRES=1 | 76 passed; no skipped integration tests |
| Runtime/database | CPython 3.14.8 and real PostgreSQL 18.6, isolated on loopback |
| Financial property tests | 1,000 exact Decimal cases and 1,000 rejected float cases; deterministic generation and failure blobs |
| Ruff lint and formatting | Passed for application, tests and scripts |
| Strict mypy | Passed, 25 checked source/test files |
| Hash-checked development-lock install / pip check | Passed; no broken requirements |
| Runtime advisory audit | All 16 locked packages; zero known vulnerabilities |
| Development advisory audit | All 64 locked packages; zero known vulnerabilities |
| Linux amd64 CPython 3.14 runtime wheels | All 16 downloaded with mandatory hash verification |
| Wheel and sdist build | Passed |
| Runtime-only wheel installed outside checkout | Packaged migration, restricted role verification and persistent audit passed |
| Compose configuration | Ordinary and test override validated with official Compose v5.6.0 |
| Compose topology inspection | Internal network, no ordinary published ports, non-root/read-only services, resource limits and separate credentials verified |
| Bootstrap without Docker | Clear prerequisite failure; no cloud/system installation attempted |
| Docker Desktop / Compose | 4.95.0 / 5.6.0; Engine 29.8.2 |
| Ordinary Docker bootstrap | Built and started on Linux arm64; provisioning, migration and readiness passed |
| Fresh checkout bootstrap | Passed from a temporary source checkout with new random secrets and empty volumes; restart retained audit history; only temporary test resources removed |
| Tests against Docker PostgreSQL 18.6 | 76 passed, no skipped integration tests; isolated test project |
| Effective container controls | Application UID 10001; app/database read-only roots, all capabilities dropped, no-new-privileges, bounded memory, internal network and no published ports |
| Restart and persistence | App/database restart and rebuilt-image startup preserve audit history; final restart smoke passed |
| Linux amd64 application | Image built; CLI, native extensions and restricted database check passed under emulation |
| Full Linux arm64 suite | 76 passed on Alpine/musl, UID 10001, read-only root, real hardened PostgreSQL; no skips |
| Full Linux amd64 suite | 76 passed under emulation; no skips |
| Fresh amd64 runtime database lifecycle | Provisioning, packaged migration and restricted database checks passed under emulation |
| Logical database transition | All original records restored into a separate Alpine volume with matching content hashes; original Debian volume retained |
| Both runtime image scans, Trivy 0.76.0 | Passed: zero HIGH/CRITICAL findings, no exclusions or ignore-unfixed bypass |
| Hosted GitHub Actions on ubuntu-24.04 amd64 | Passed: hash-locked install, static checks, all 76 Linux tests, dependency audits, package build, container smoke and both runtime image scans |

The PostgreSQL build and verification tools were isolated under `/private/tmp`;
the operating system's Python was not replaced. Native tests cover append-only triggers,
role/schema restrictions, unexpected privilege grants, transaction rollback, concurrent
session isolation, process restart persistence, schema-failure readiness and shutdown.

The initial Docker-backed test run exposed a Docker Desktop forwarding issue for
containers attached only to an internal network. The test override now adds a separate
bridge, and the runner uses the independent `aca-foundation-tests` project. All 76
tests passed after that correction. Ordinary application networking remains internal.

Initial scans found vulnerable Debian components and unused bundled installers.
ADRs A17–A20 move the application and PostgreSQL to official, digest-pinned Alpine
3.24 bases, remove pip/ensurepip from the app and gosu from the database, and test
the packaged wheel. Python, PostgreSQL and direct Python dependency versions are
unchanged. Both runtime scans pass with HIGH/CRITICAL checks, including unfixed
findings. CI scans both images. See [image review](image-review.md).

Clean Linux testing exposed an editable-install helper outside the development lock.
CI and verification now install the wheel, validating packaged migrations and avoiding
an unlocked helper. No dependency pin was substituted. Linux tests use a separate test
stage/project and do not mount test/admin credentials into the ordinary app service.

The full suite ran on native macOS, Linux arm64, emulated Linux amd64 and the hosted
ubuntu-24.04 amd64 runner; each run requires real PostgreSQL. The hosted run also
passed dependency audits, package builds, container bootstrap/restart and both image
scans. Its run URL, tested commit, complete test count and step conclusions are saved
in [the hosted CI evidence](evidence/phase-1-hosted-ci.json). G1 also includes later
read-only connectivity requirements. No Phase 2 work or live execution follows
automatically.

## Incomplete safety requirements

| Required subsystem/evidence | Status | Gate |
| --- | --- | --- |
| Exchange discovery/connectivity, stream checksums, gaps, private reads and fee/eligibility verification | Not implemented | Phase 2 / G1 |
| Historical provenance, strategies, benchmarks, realistic fills, walk-forward and uncertainty | Not implemented | Phase 3 / G3 |
| Financial ledger, unitisation, return calculations and cash-flow reconciliation | Not implemented | Phase 4 / G2 |
| Independent risk enforcement, sizing, reservations and durable kill states | Not implemented; policy values only | Phase 4 / G2 |
| Order state machine, uncertain outcomes, partial fills and protection recovery | Not implemented | Phase 4 / G2 |
| Local paper exchange and deterministic fault replay | Not implemented | Phase 4 / G2 |
| Outbound Discord, daily summaries and external heartbeat | Not implemented | Phase 5 |
| Encrypted off-host backups, WAL archive and actual disaster restore | Not implemented | Phase 5 / G5 |
| UK tax matching, owner inventory and GBP valuation evidence | Not implemented | G5 |
| At least 56 paper days and required completed episodes | Not started | G4 |
| Positive statistical/economic advantage | No evidence | G3 / G4 |
| Live writer, activation, signed permit and real exchange canary | Not implemented or enabled | Separate Phase 7 authorisations |
| Paid VPS/hosting, second exchange, ML and dashboard | Deferred; none provisioned | Separate approval |

The running local stack is a paper-only development foundation with passing local
image checks. It is not a paper trading simulator or a production trading release. Do not describe
the risk limits as enforced trading controls until the later implementation passes G2.

## Next review boundary

The next milestone is Phase 2 read-only Kraken connectivity. It must not implement
order placement or use a trading key. Review this milestone's evidence before
authorising that phase. No automatic progression to Phase 7/8 is authorised.
