# P4-C3 — Anchored replay archive reads

21 October 2026. Candidate **0.4.16**, schema `0014_accounting_operations`.
This continues condition 4 of the [fixed checklist](phase-4-completion.md).
**No checklist condition closes in this increment.** C3 still needs a demonstrated
capacity/retention solution for both replay and financial history; C4 follows C3.

## Behaviour and trust boundary

An explicit file/hash pair can now supply the historical replay prefix to paper
performance/daily reports and replay-enabled writer startup. Newer observations
continue from PostgreSQL, using the file's terminal chain hash. Archived public
trades are evidence for the historical audit; they are never replayed as new venue
commands. Default readers continue using PostgreSQL.

The reader does not accept the export's `database_semantics_checked_at_export`
claim as audit authority. Within the same database snapshot it checks:

- The account and current policy; the retained enrolment baseline; the original
  financial event/state hash at export; and the archived terminal valuation/cursor
  hashes against their retained PostgreSQL anchors.
- Every compressed segment, exact file SHA-256, valuation/cursor chain, original
  raw payload and source binding. The actual ordered database sources must cover
  every archived observation. Rolled-back sequence allocations may leave valid
  number gaps; omitted public evidence cannot be skipped.
- The complete retained financial event/journal history and the reconstruction
  of balances, units, investment flows, fills, actual fees and tax-cash decisions.
  Both archived and newer SQL observations use the existing chronology, NAV,
  risk-boundary and midnight-control checks.

Financial results require exhaustion of the complete file and SQL suffix. Existing
accounting-invalidation reports remain explicitly unavailable; a file cannot repair
invalidated units or make those reports complete. Selecting an
older endpoint cannot skip file verification or hide an invalid newer observation.
Performance calculation version **`historical-performance-v3`** also reconstructs
the full recorded financial range for SQL-only past-endpoint reports; period
arithmetic remains limited to the requested range. Existing issued reports keep
their original calculation version/hash. New reports include
`replay_archive_sha256` (null for SQL-only input) in their hashed provenance.

Writer acquisition with a file input performs that full financial reconstruction
before returning the owner to any command caller. Partial iteration, cancellation,
an unposted fill, conflicting anchor or forged units cannot grant ownership.
Acquisition failure invalidates/closes the session and releases its advisory lock.
Ordinary replay startup still audits the retained cursor chain before consuming
new receipts. Existing startup/report deadlines remain unchanged.

The file remains open while its bytes are verified. Its device, inode, size, mtime
and ctime must remain unchanged through EOF; the path must still name that same
regular file. Every subsequent writer transaction checks that identity again.
Deletion, replacement, unreadability or a detected modification latches a fault
for that writer. Replacing the file does not clear the fault. A fresh explicit
writer must repeat the complete audit. These metadata checks detect ordinary local
file changes; they are not a host-compromise defence or a repeated full-file hash
on every one-second tick. Existing durable financial loss latches are preserved.

The parser streams individual observations and caches at most 256 small database
source anchors. It never buffers a page of raw receipt payloads. Codec segment,
decompression, total-file and record ceilings remain unchanged. Async cancellation
points occur at database pages and every 64 file observations; a cancelled iterator
closes its file and has no accepted identity.

The file's version-1 `usable_as_operational_prefix=false` and
`pruning_authorised=false` fields retain their original meaning: offline
verification supplies no independent prefix certificate or deletion authority.
The online reader requires retained database anchors and repeats the financial
audit. No new signing key, trusted audit certificate, migration, deletion role,
archive catalogue or permission to remove PostgreSQL records is introduced.

## Explicit local use

Use the candidate image and supported schema in disposable/reviewed paper state.
The retained 0.4.8 installation has not been upgraded. Export a private file with
the [existing exporter](phase-4-replay-preservation.md), retain its returned hash,
and mount that directory explicitly into a container that needs it.

```sh
aca paper performance --account ACCOUNT_UUID --archive /private/export/prefix.aca --archive-sha256 EXPORTED_SHA256
aca paper daily --account ACCOUNT_UUID --day 2026-10-20 --archive /private/export/prefix.aca --archive-sha256 EXPORTED_SHA256
aca paper supervise --account ACCOUNT_UUID --replay --duration 300 --archive /private/export/prefix.aca --archive-sha256 EXPORTED_SHA256
aca paper run --account ACCOUNT_UUID --duration 300 --archive /private/export/prefix.aca --archive-sha256 EXPORTED_SHA256
```

`consume` accepts the same pair. Supplying only one argument fails before database
access. `supervise` requires `--replay` when a file is supplied. The inputs apply
to that invocation; there is no environment-driven acceptance, persistent archive
activation or automatic fallback to SQL after a required file fails. A later
explicit SQL-only command can audit the complete retained SQL history. Background
notification processes keep their existing SQL-only input; selecting a file for a
runner does not silently change another process's configuration.

Reports remain provisional and operational experiments receive zero qualifying
days/episodes. No command accepts a live key, enables Kraken writes, resets units
or clears a financial halt.

## Evidence and remaining capacity condition

The [machine-readable report](evidence/phase-4-archive-input.json) binds the final
package identity, Linux/PostgreSQL regression, CLI/restart smoke and capacity
measurements. Tests cover a paginated sparse-cursor prefix, new SQL flows and
fills/fees, exact historical ranges, midnight across the file/SQL boundary,
snapshot isolation, a missing SQL valuation, forged headers with freshly computed
hashes, structurally valid fabricated units, missing/truncated files, per-writer
fault latching, cancelled reads/acquisition and unchanged report record limits.

Reproduce the release regression and two separate resource trials with:

```sh
python3 scripts/ci_database.py --linux -q
python3 scripts/capacity_trial.py
python3 scripts/capacity_trial.py --preservation
```

The original trial still measures the original SQL operations and one-second loop.
The preservation trial additionally measures online archive reports, writer startup,
replay audit and daily summary at the same 20/20/10/15-second budgets. Its original
`operations` map remains explicitly unexercised; the new subset is reported under
`archive_input`. A 60-second diagnostic tail records overruns without turning them
into passes. No child-harness or operational deadline is increased. Green CI means
that the measurements completed, not that their capacity results passed.

The final Linux/PostgreSQL regression passed **663 tests in 120.50 seconds**.
An additional 88 focused native tests passed; Ruff/check/format (168 files), strict
Mypy (146 files), dependency consistency, wheel/sdist build and all three advisory
audits pass. The packaged CLI/restart smoke uses the exact package-source hash in
the evidence report. Its account state survives restart and all owned smoke
containers/volumes are removed. One clean-build attempt lost Docker Desktop near
the end of its suite; the cached retry completed all tests. The interrupted run is
not counted as a success.

The original SQL trial `capacity-trial-a9a84f45427f` measured these local results
at 100,000 financial events and 100,000 historical values:

| Operation | Local duration | Existing deadline | Result |
| --- | ---: | ---: | --- |
| Financial writer startup | 6.73 s | 30 s | Pass locally |
| Combined financial/replay startup | 17.08 s | 20 s | Pass locally |
| Historical report | 15.51 s | 20 s | Pass locally |
| Replay audit after acquisition | 8.82 s | 10 s | Pass locally |
| Scheduled daily summary | 14.68 s | 15 s | Pass locally |

All 20 one-second maintenance ticks were healthy (maximum 0.080 seconds), with
zero venue commands. The database used 500,823,743 bytes, worker CPU time was
92.78 seconds and peak cgroup memory was 104,910,848 bytes under the unchanged
0.75-CPU/768-MiB worker and 0.75-CPU/1-GiB database ceilings. This cash-only bulk
fixture supplies no full loaded-strategy, market-ingestion or elapsed reliability
claim. The summary margin is narrow; hosted exact-head results are recorded in
the PR, and local passes do not override hosted failures.

The separate preservation/input trial `capacity-trial-7e3741577648` independently
verified **100,000 valuations/cursors** in 1,563 segments, using **23,802,435 bytes**
(298,786,135 decoded segment bytes). Export/audit/verification took **187.66 seconds**;
a further offline verification took **16.41 seconds**. Online report economics
matched the original audited SQL snapshot exactly, with the file root added as
provenance. All four online archive-input measurements still exceeded their
existing budgets:

| Archive-input operation | Local duration | Existing deadline | Result |
| --- | ---: | ---: | --- |
| Historical report | 26.25 s | 20 s | **Fail** |
| Combined writer startup | 25.27 s | 20 s | **Fail** |
| Replay audit after acquisition | 20.18 s | 10 s | **Fail** |
| Daily summary | 30.34 s | 15 s | **Fail** |

Every operation completed within its 60-second diagnostic limit. Worker CPU time
for this entire trial was 164.55 seconds and peak cgroup memory was 129,892,352
bytes; the database used 436,074,175 bytes. The original operational map in this
mode is empty and explicitly unexercised; these failures are recorded separately
under `archive_input.measured_boundary_failures`. Both trials removed their owned
containers/volumes and temporary files. No SQL records were pruned. File validation
and financial reconstruction remain proportional to prefix size; compression and
this read path have not solved the named capacity condition.

**Still open under C3 condition 4:** financial events and journals remain fully
audited in PostgreSQL; all replay/source rows are retained; no bounded accepted
financial prefix or archival deletion/read catalogue exists. A file-backed full
scan still performs work proportional to the prefix. The 100,000-value ceiling
covers about 1.16 days at one observation/second, versus 4,838,400 observations for
56 days, before additional public receipts. The database's 2-GB stop threshold,
existing record limits and resource ceilings remain in force. This increment
implements a read path, not a 56-day retention solution or a capacity gate pass.

The fixed result remains **12/17 conditions complete**. Qualifying Phase 5
observation has not started, all 20 research candidates remain rejected, and live
execution remains unavailable.
