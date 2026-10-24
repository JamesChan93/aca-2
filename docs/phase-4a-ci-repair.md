# Phase 4a CI repair — P4A-17

24 October 2026. Application 0.4.19; schema and application source unchanged.
This increment repairs the failures exposed by [PR #29](https://github.com/JamesChan93/aca-2/pull/29).
It does not complete the other Phase 4a items or grant research/paper/live approval.

| Observed failure | Cause | Repair |
| --- | --- | --- |
| Initial `verify` runs | Development lock pins setuptools 84.0.0 with GHSA-xmj3-9gf7-h52w | Exact 84.1.0 pin/hashes; upstream metadata/licence/release review; all lock audits retained |
| Later database image scans | brotli-libs 1.2.0-r1 has CVE-2026-111497 | Add signed Alpine brotli-libs 1.2.1-r0 to the same digest-pinned PostgreSQL 18.6 base |
| Intermittent bounded-history job | Whole seed/compaction/full-comparison fixture exceeds 600 seconds | 900-second fixture budget inside a 20-minute CI job; unchanged measured-operation deadlines and failure exit |

The first patched-head [push run](https://github.com/JamesChan93/aca-2/actions/runs/40091506242)
passed bounded-history acceptance; its matching
[PR run](https://github.com/JamesChan93/aca-2/actions/runs/40091508963) hit the overall
fixture timeout. Both cleared the dependency audit and reached the previously
blocked database scan. That scan found the Brotli advisory in both runs.

The successful same-head compaction measured financial startup at 2.59 seconds,
replay startup at 6.41, historical reporting at 2.61, post-acquisition replay audit
at 3.64 and daily summary at 2.78. Their respective unchanged deadlines are
30/20/20/10/15 seconds. These results distinguish the full fixture's preparation/
comparison budget from the online operation acceptance budgets; no missed online
deadline is reclassified as a pass. Existing record limits, full-suffix checks,
resource ceilings and lossless economic comparisons remain enforced.

The fixture writes fixed phase/elapsed-time checkpoints into its private diagnostic
log. On completion or timeout, the orchestrator emits only the last allowlisted
phase and finite bounded elapsed value. Raw child errors, database credentials and
arbitrary fields are not copied into the public result. A regression test proves
that timeout still returns failure, performs cleanup and retains a safe checkpoint.
Separate malformed/secret-bearing log cases test the public-output boundary.

The patched database build changes only brotli-libs. Removing the general apk
upgrade preference avoids updating unrelated packages. PostgreSQL remains 18.6,
the original base digest remains pinned, gosu stays removed, and the service still
runs as postgres. The new image is exercised only in disposable tests; retained
workstation services and volumes are untouched. ADRs A16/A19 and their supply-chain
records describe the exact package updates.

**Local verification**

- Hash-checked patched setuptools install, packaging-tool imports, `pip check` and
  all three dependency audits pass.
- The clean Linux musl test image, Ruff/format, strict mypy across 158 files and
  wheel build pass.
- Helper safety tests: 36 passed, including timeout cleanup and private-log filtering.
- Full Linux/PostgreSQL regression using the patched DB image: **738 passed in
  173.00 seconds**; disposable database/network cleanup completed.
- The rebuilt arm64 PostgreSQL image passes the pinned Trivy 0.76.0 scan with
  zero HIGH/CRITICAL vulnerabilities and zero secret findings, including unfixed
  findings. Installed Brotli is 1.2.1-r0. The scanner receives an exported image and
  separate cache, without Docker-socket or credential mounts.

The [local evidence](evidence/phase-4a-ci-local.json) binds these results to exact
file hashes and the scanner report hash. Raw local logs/reports remain under
`/private/tmp/aca-phase4a-*`; they are diagnostics, not a durable backup or
production evidence. Fresh hosted amd64 regression, compaction and both runtime
image scans must establish the final PR-head outcome. PR #29 tracks those checks;
the local packet does not mark them passed in advance.
