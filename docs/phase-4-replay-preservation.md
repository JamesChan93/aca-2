# P4-C3 — Replay preservation format

21 October 2026. Candidate **0.4.15**, schema `0014_accounting_operations`.
This implements part of the existing capacity condition in the
[fixed checklist](phase-4-completion.md). **It does not close that condition.**
Operational startup and reports still read their PostgreSQL prefixes, and their
existing deadlines and record limits remain in force.

## Implemented behaviour

Two explicit CLI actions export and independently verify a lossless, compressed
replay snapshot. This is a component of the preservation path, not another backup
system. Journals, movements, fills, permanent client IDs, financial events and
operational records remain in PostgreSQL. There is no delete, archive acceptance,
prefix substitution or automatic maintenance command.

`archive-export` opens a database-enforced read-only, repeatable-read snapshot.
The existing performance service audits the financial history and reconstructs
historical balances/units through the recorded head, including source coverage.
The export also validates the entire replay-cursor chain and its public anchors.
An accounting correction that invalidated units/reports prevents export. Missing
market valuations remain recorded as missing; exporting them supplies no
complete-performance or promotion claim.

The resulting file contains, from the historical enrolment baseline to that head:

- Every original valuation document, its account/number and chain hashes.
- Every cursor in that range, including its exact original context and hashes.
  Cursors can be sparse because a replay batch covers several public receipts.
- Each corresponding original public replay envelope, source fingerprint,
  publication time and receipt identity.
- The original market-receipt payload, payload hash, normalised evidence, epoch,
  receipt time, validity and reason. Payload hashes and source bindings are checked.

The header binds the account, policy, financial head/version/state hash, audit
report hash, baseline/head numbers, terminal valuation/cursor hashes and counts.
If enrolment began at a nonzero source, the baseline's public receipt is included.
Its cursor's preceding hash is retained as an opening anchor; earlier cursors are
audited during export and remain in PostgreSQL. This file is baseline-forward
replay evidence, not an archive of the account's complete financial history.

Database pages contain at most eight joined observations because they include raw
payloads. A private temporary file is flushed and fsynced, independently verified,
then published by an exclusive hard link. Existing files and symlinks cannot be
overwritten. The file has mode `0600`. A caught failure removes the temporary file;
a process/host kill may leave a private `.aca-replay-*` temporary file. Such a file
is not a published or accepted archive.

## File contract and validation

The format uses only standard-library JSON, SHA-256 and zlib. It uses no pickle,
tar/ZIP extraction or embedded file paths.

| Element | Version 1 contract |
| --- | --- |
| Magic | Eight bytes: `ACARPL1` followed by a newline |
| Frame header | Unsigned one-byte kind and four-byte big-endian payload length |
| Segment, kind 1 | One zlib stream containing canonical JSON with `segment`, `previous`, `rows` |
| Segment linkage | Zero-based segment number and SHA-256 of the preceding decoded segment; first previous hash is 64 zeroes |
| First row | A single `context` object identifying `replay-preservation-v1` |
| Following rows | Versioned observation objects with `valuation`, optional `cursor`, `source` and original `receipt` |
| Manifest, kind 2 | Canonical uncompressed JSON with format, counts, terminal segment hash and context-header hash |
| File root | SHA-256 of the exact complete file bytes, supplied separately to verification |

Canonical JSON has sorted keys, compact separators, a final newline, no duplicate
keys and no NaN/Infinity. Financial values remain their exact original strings.
Strict domain restoration still rejects financial floats and naive timestamps.
UTC timestamps and UUID columns are serialised explicitly; JSON documents preserve
their original values, including Decimal scale and signed zero.

Segments contain at most 64 rows, normally targeting one megabyte before
compression. A single large row can use a dedicated segment. The hard decoded
segment bound is 16,000,000 bytes; the compressed bound adds 65,536 bytes for stream
overhead. The file bound is 2,000,000,000 bytes, total decoded-segment bound is
8,000,000,000 bytes, manifest bound is 65,536 bytes and segment-count bound is
100,000. Existing export evidence limits, including 100,000 valuations/cursors,
remain. An admitted database row that cannot fit this format fails export rather
than being truncated. These bounds are not a demonstrated 56-day capacity.

The reader checks compressed lengths before allocation and caps decompression
output. It rejects truncated, concatenated or oversized streams, unknown frames,
reordered/missing segments, count/chain mismatches, unexpected suffixes, symlinks
and nonregular input files. A partial iterator has no verified result. Verification
must exhaust every segment and the manifest, then match the independently retained
file root before returning success.

The semantic verifier shares the existing cursor/valuation restoration functions.
It also checks account/policy identity, chronology, original source/payload
bindings, embedded midnight anchors and terminal counts/hashes. It **does not**
re-run journal/control reconstruction offline. The file contains references to
that database audit, not the journal history required to repeat it. Returned fields
explicitly state `database_semantics_rechecked=false`,
`financial_records_included=false`, `usable_as_operational_prefix=false` and
`pruning_authorised=false`.

A checksum is an integrity root, not a signature or a new authority to trade.
Retain the hash returned by a trusted export separately before moving the file.
Computing a fresh expected hash from an untrusted file does not authenticate its
claims. Every later preservation/read-path design must define its trust boundary
and verify the required evidence before accepting an archived prefix.

## Commands

Run export in a separate CLI process, outside the one-second risk owner. It can
take longer than a report deadline because it audits, serialises, compresses and
verifies the entire selected prefix. The supplied destination parent must already
exist and be private. In a container, explicitly mount that private export
directory; the read-only application root is not an output destination.
Use a paper maintenance window for large exports. Concurrent writes remain
snapshot-consistent, but simultaneous runner/export resource headroom is unproven.

```sh
aca paper archive-export --account ACCOUNT_UUID --output /private/export/replay.aca
aca paper archive-verify --account ACCOUNT_UUID --input /private/export/replay.aca --sha256 EXPORTED_SHA256
```

The second command needs no database connection or credential files. It checks the
requested account as well as the supplied hash. Neither command can accept an
archive into runtime state, reset an account, clear a loss latch or prune history.
Files contain public market evidence and virtual-account state; retain them under
the project's private data/evidence policy.

## Acceptance evidence and remaining condition

The [machine-readable report](evidence/phase-4-replay-preservation.json) records
the package identity, final regression and bounded capacity/export measurements.
The full capacity trial now exports its 100,000-value synthetic prefix, verifies
the published file again and records sizes, elapsed times and explicit capability
limits under the existing resource ceilings. It does not substitute those numbers
for the startup/report deadlines or label the cold-storage path complete.

Local synthetic trial `capacity-trial-94f48975ffaf` completed with the unchanged
0.75-CPU/768-MiB worker and 0.75-CPU/1-GiB database. Its archive held **100,000
valuations and 100,000 cursors** in 1,563 segments. The file used **23,797,268 bytes**
for 298,786,135 decoded segment bytes. Auditing, exporting and verifying before
publication took **183.33 seconds**; a further independent verification took
**17.40 seconds**. The file was removed with the disposable trial. Its root and
metadata remain in the evidence report; no test archive is treated as retained
operational history.

| Existing operation | Local duration | Unchanged deadline | Result |
| --- | ---: | ---: | --- |
| Financial writer startup | 6.81 s | 30 s | Pass locally |
| Combined financial/replay startup | 15.54 s | 20 s | Pass locally |
| Historical report | 15.01 s | 20 s | Pass locally |
| Replay audit after writer acquisition | 10.09 s | 10 s | **Fail** |
| Scheduled daily summary | 14.83 s | 15 s | Pass locally, narrow margin |

All 20 one-second maintenance ticks were healthy; the maximum tick took 0.056
seconds and there were zero venue commands. The database used 500,545,215 bytes.
Worker peak cgroup memory, including export, was 133,107,712 bytes. All owned
containers/volumes and temporary archive files were removed. This cash-only bulk
fixture does not establish market throughput, a fully loaded strategy workload or
elapsed reliability. Hosted exact-head results are recorded in the PR and take
precedence over narrow local timings. **56-day headroom remains false.**

The final local Linux/PostgreSQL regression passed **639 tests in 103.84 seconds**.
Ruff/formatting (166 files), strict Mypy (144 files), dependency consistency,
wheel/sdist build and all three advisory audits pass. The packaged archive CLI
smoke passed in a disposable non-root image; its account state was unchanged
across container restart. The checkout, wheel, runtime and capacity worker match
the package-source hash in the evidence report. The retained container identities
and application version remain unchanged.

Tests include 1,000 generated lossless JSON round trips, segment/root corruption,
truncation/reordering, decompression limits, duplicate keys, exclusive publication,
interrupted writes, exact database-row round trips, valid sequence gaps, sparse
cursors, concurrent snapshot isolation, corrupt financial units/raw payloads and
bad intermediate cursors behind a valid head. The packaged CLI smoke exports from
the existing disposable demo, verifies with an unreachable database and nonexistent
credential files, and rejects a different account and truncated file.

**Still required under C3 condition 4:** integrate a reviewed bounded preservation
and read path for the source/valuation/cursor and financial prefixes, establish how
its audit authority is retained/verified, and rerun the original workload deadlines
and retention assessment. This format has no pruning or operational acceptance
path. A file export alone cannot reduce PostgreSQL growth, supply 56 days of
headroom or turn failed capacity measurements into passing results.

The fixed checklist remains **12/17**. C4 follows C3. The retained 0.4.8 stack is
unchanged, qualifying paper observation has not started, all 20 research candidates
remain rejected and live execution remains unavailable.
