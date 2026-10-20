# Phase 1 dependency and supply-chain review

**Security update, 20 October 2026 (0.4.13):** the current advisory audit identified
[GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)
in the previously locked `certifi==2026.7.22`. The maintainer initially patched
`2026.10.18`; the [updated advisory database](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)
and current audit recommend `2026.10.19` or later. ADR A16 is amended
to constrain `2026.10.19` in all three affected locks. The pure Python wheel's
published SHA-256 is
`42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b`;
its MPL-2.0 licence and Python >=3.7 requirement are recorded by
[PyPI](https://pypi.org/project/certifi/2026.10.19/).
Other package pins, base-image digests and Actions pins are unchanged. Earlier
dated audit results below describe those releases, not this new advisory.
The retained 0.4.8 stack has not been redeployed by this source update.

Observed 17 October 2026. Direct candidates match the approved plan and their installed
package metadata. Runtime is CPython 3.14.8, standard GIL build. PostgreSQL native tests
use official 18.6 source, verified SHA-256
`555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f`.

| Runtime package | Version | Declared licence |
| --- | --- | --- |
| SQLAlchemy | 2.1.5 | MIT |
| Alembic | 1.20.0 | MIT |
| Psycopg / psycopg-binary | 3.3.6 | LGPL-3.0-only |
| Pydantic | 2.14.1 | MIT |
| pydantic-core | 2.50.1 | MIT |
| pydantic-settings | 2.16.0 | MIT |
| structlog | 26.1.0 | MIT OR Apache-2.0 |
| prometheus-client | 0.26.0 | Apache-2.0 AND BSD-2-Clause |

Development includes Hypothesis (MPL-2.0), Ruff/mypy/Hatchling (MIT), and pip-tools
(BSD-3-Clause). Preserve upstream distribution notices; Psycopg binary distributions
include native components with their own notices. This private repository does not
relicense dependencies. Review redistribution obligations before distributing images
outside the owner's environment.

Primary sources: [PyPI](https://pypi.org/), the packages' installed METADATA/licence
files, [SQLAlchemy docs](https://docs.sqlalchemy.org/en/21/),
[Pydantic settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/),
[Psycopg installation](https://www.psycopg.org/psycopg3/docs/basic/install.html),
[official PostgreSQL image](https://hub.docker.com/_/postgres), and
[Compose secret semantics](https://docs.docker.com/compose/how-tos/use-secrets/).

## Immutable build references

- Python 3.14.8-alpine3.24: index digest
  `sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72`.
- PostgreSQL 18.6-alpine3.24: index digest
  `sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873`.
- Both official manifest digests were resolved through Docker's registry. Both were
  built/exercised on Linux arm64 and Linux amd64 (amd64 under emulation locally).
- Checkout, setup-python and Trivy Actions are pinned to verified upstream commits.
- Trivy is explicitly v0.76.0 in CI. The local scanner image digest, resolved findings
  and passing image gates are recorded in [image review](image-review.md).

The runtime lock contains all 16 transitive packages. Linux amd64 CPython 3.14 wheels
were downloaded with hash verification. A native macOS runtime-only environment is
also checked separately from the Linux wheel installs. The Linux test stage installs
all 64 development pins using mandatory hashes and binary wheels, and runs pip check.

Pip-tools initially selected platformdirs 4.13.4, but the audit's simple-index request
could not retrieve it. ADR A16 constrains 4.13.3, matching the tested environment.
The development lock includes pip/setuptools explicitly rather than leaving them in
pip-tools' default excluded packaging-tool category.

No research, exchange or notification SDK is installed. Application code is Python;
upstream native extensions and third-party build/lint/container tools are dependencies.
Dependency audits report known advisories only; they do not establish live-trading
safety. Container vulnerability scanning is enforced by CI and requires a Docker runner.
Initial Debian image scans failed. The final Alpine-derived application and database
images pass the same HIGH/CRITICAL checks, including unfixed findings. The application
removes pip/ensurepip; the database removes unused gosu and starts as postgres. No
scanner exclusions were introduced. ADRs A17–A20 record these bounded changes.

The earlier Debian digests remain in the dated evidence and rollback records. The
switch changes the OS C library, so the complete Linux suite was required before
adoption. Python 3.14.8, PostgreSQL 18.6 and direct dependency versions are unchanged.
See [official Python image](https://hub.docker.com/_/python),
[official PostgreSQL Alpine build](https://github.com/docker-library/postgres/blob/master/18/alpine3.24/Dockerfile)
and [Docker's glibc/musl guidance](https://docs.docker.com/dhi/explore/security-concepts/glibc-musl/).

Audit uses `--disable-pip` after a separate hash-checked install. This enumerates every
locked pin, including packaging/pip/setuptools/wheel that pip's dry-run resolver can
omit as already installed in its audit environment. Distribution hashes are checked
by installation/download; advisory coverage is checked against all lock entries.

## Phase 2 additions

The planned HTTPX 0.28.1 and websockets 17.2 candidates are added without substitutions.
Both runtime and development locks include exact distribution SHA-256 hashes.
HTTPX requires Python >=3.8 (BSD-3-Clause); websockets requires Python >=3.11
(BSD-3-Clause). New transitive packages: anyio 4.15.1 (Python >=3.10, MIT),
httpcore 1.0.9 (Python >=3.8, BSD-3-Clause), h11 0.16.0 (Python >=3.8, MIT).
Licence/Python declarations were inspected from installed distribution metadata.
Existing certifi, idna and typing-extensions locks remain shared dependencies.
Both complete locks pass the current pip-audit check. Linux musl binary-wheel and
hosted amd64 verification are recorded in the Phase 2 report/evidence; this paragraph
is not a claim of future advisory-free operation.

HTTPX uses explicit timeouts, connection limits, TLS verification, no environment
proxies and no redirects. websockets uses bounded frames/queues, no proxies and an
explicit redirect rejection. HTTPX has no mutable/private endpoint to retry. No
new secret input is introduced. Native websockets speedups are upstream dependencies.

## Phase 3 optional research group

NumPy 2.5.4 is the only added optional direct dependency. PyPI requires Python >=3.12
and publishes standard CPython 3.14 macOS arm64/x86_64, manylinux arm64/amd64 and
musllinux arm64/amd64 wheels. The installed metadata licence expression is
`BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`; its distribution retains
bundled component licence notices. No new transitive Python requirement is added.
`requirements/research.lock` includes the existing runtime plus reviewed NumPy
distribution hashes. Native macOS and non-root Linux test wheels are exercised; CI
checks amd64. The normal runtime image excludes this group.

Pandas 3.0.6, SciPy 1.18.1, PyArrow 26.0.0 and DuckDB 1.5.6 remain deferred under
ADR A24. This four-series CSV study does not need their functionality; there is no
claim to have validated their combined compatibility or licences. Research cannot
run with an unreviewed NumPy version. No direct candidate version is substituted.
