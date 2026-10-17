# Local image security evidence

Observed 17 October 2026. Both hardened runtime images pass the HIGH/CRITICAL
vulnerability and secret checks on arm64 and amd64. No advisory exceptions, ignore
rules or ignore-unfixed bypasses were added. These are dated scanner results, not a
guarantee that the images contain no vulnerabilities.

## Final results

| Runtime image | Architecture | HIGH vulnerabilities | CRITICAL vulnerabilities | HIGH/CRITICAL secrets | Exit |
| --- | --- | --- | --- | --- | --- |
| aca-foundation:0.1.0 | arm64 | 0 | 0 | 0 | 0 |
| aca-postgres:18.6 | arm64 | 0 | 0 | 0 | 0 |
| aca-foundation:verify-amd64 | amd64 | 0 | 0 | 0 | 0 |
| aca-postgres:verify-amd64 | amd64 | 0 | 0 | 0 | 0 |

Scans completed at 20:47–20:48 UTC. Image IDs, OS versions, exact timestamps, scanner
settings and counts are preserved in
[the machine-readable evidence](evidence/phase-1-image-scans.json). Full local reports
are retained under ignored `artifacts/`: `trivy-image.json`, `trivy-postgres.json`,
`trivy-image-amd64.json` and `trivy-postgres-amd64.json`.

Trivy 0.76.0 used the official `ghcr.io/aquasecurity/trivy-db:2` database. The scanner
image is pinned to
`aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222`.
It received exported image archives and a disposable cache. No credentials, source
secrets or Docker socket were mounted into it. CI explicitly runs the same scanners
and severity gate against both runtime images, including unfixed findings.

## Resolution and compatibility

ADR A17 removes pip and ensurepip after wheel installation. ADR A18 selects the
official Python 3.14.8 Alpine 3.24 base. ADR A19 derives PostgreSQL from the official
18.6 Alpine 3.24 base and removes unused gosu; the service starts as postgres and
does not need a root step-down helper. ADR A20 installs the application wheel during
verification, avoiding an unlocked editable-install helper. Python, PostgreSQL and
the direct Python package versions are unchanged.

Both bases use musl. The runtime lock installs using hash-verified binary wheels;
the separate test stage also installs the development lock using hashes and binary
wheels. The full suite passed with real PostgreSQL, unprivileged UID 10001 and a
read-only root filesystem: 76 tests on Linux arm64 and 76 on Linux amd64 under
emulation. Fresh amd64 role provisioning, packaged migrations and restricted runtime
checks also pass. Research packages remain outside this compatibility claim.

The clean-checkout bootstrap test used new secrets and empty volumes, then verified
restart persistence. The existing local database was moved by a protected logical
backup/restore into a separate Alpine volume, with identical record counts/content
hashes. Its original volume remains available for rollback. See
[operations](operations.md#recorded-local-debian-to-alpine-transition).

## Initial findings retained for review

The initial Debian application image had 44 HIGH package findings across eight
distinct advisories, plus four earlier findings in pip's vendored dependencies. The
Debian PostgreSQL image had 62 OS package findings and 25 in gosu's bundled Go
runtime. Repeated binary-package findings can refer to the same source advisory.
The Alpine PostgreSQL candidate cleared the OS findings but still contained gosu;
removing that unused binary cleared its remaining findings.

Affected Debian source families included util-linux, acl, ncurses, systemd and perl.
For example, the dated [Debian CVE-2026-76642 tracker](https://security-tracker.debian.org/tracker/CVE-2026-76642)
identified the installed trixie version as vulnerable. The original application
base digest was `sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170`;
the original PostgreSQL base was
`sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336`.
The resolution changes supported base images and removes unneeded code; it does not
import unstable Debian packages or hide findings from the scanner.

## Repeat the application scan

After building the current source, with Docker available on PATH:

```sh
mkdir -p artifacts/trivy-cache
docker save aca-foundation:0.1.0 -o artifacts/foundation-image.tar
docker run --rm --cap-drop ALL --security-opt no-new-privileges \
  --mount type=bind,src="$PWD/artifacts",dst=/scan,readonly \
  --mount type=bind,src="$PWD/artifacts/trivy-cache",dst=/root/.cache \
  aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222 \
  image --input /scan/foundation-image.tar \
  --db-repository ghcr.io/aquasecurity/trivy-db:2 \
  --scanners vuln,secret --severity HIGH,CRITICAL --ignore-unfixed=false --exit-code 1
```

Export `aca-postgres:18.6` to a different archive and repeat for the database. Fetch
fresh vulnerability data before release; dated results do not establish future
advisory status. The first hosted GitHub Actions result is recorded in the milestone
report when verified.
The local stack remains a Phase 1 development foundation without exchange access,
live activation or production deployment.
