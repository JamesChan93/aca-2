# Bounded paper history — A31 / P4-C3 condition 4

Candidate 0.4.18, schema `0016_history_segments`, 22 October 2026.
The release evidence and acceptance decision are in
[the paper handoff](phase-4-paper-release.md) and its
[machine-readable results](evidence/phase-4-paper-release.json).

## Decision and authority

Store already audited history in lossless compressed PostgreSQL blocks and retain
operator-owned economic summaries. Online startup/reporting checks the protected
catalogue, its retained anchors and **every new suffix event/observation**. It no
longer reconstructs every historical observation for each report. Complete audits
still reconstruct original records from blocks plus retained SQL anchors.

This extends A30 with a separate explicit representation-change operation. An A30
financial seal still says `pruning_authorised=false`; it alone cannot admit a
compacted writer. `paper history-compact` requires separately supplied migration
credentials, actor, reason, exact financial version and exact public source head.
The new certificate explicitly authorises lossless storage compaction and records
that original financial history and permanent order IDs are preserved. It grants
no strategy, restart-policy or live-trading permission.

The operator first obtains a full financial audit. Compaction then takes the
account writer lock, local venue lock and public-publication lock. It refuses an
active owner and currently requires **one enrolled account**, matching the initial
nominated-account scope. Stop the runner, collector and notification worker first.
The separate runtime connection uses database-enforced read-only REPEATABLE READ
to reconstruct financial and historical economics, verify cursor/source chains,
and check original raw-receipt bindings. No old summary substitutes for this audit.

Before moving any row, the operator compresses and independently decodes its exact
original fields. One transaction inserts blocks, versioned report summaries and
the certificate, then removes eligible duplicate active rows. Operator-only trigger
changes are transactional; interruption, constraint failure or cancellation rolls
back the entire cutover and releases the locks. Runtime has SELECT only on both new
tables, and has no DDL, compaction or certificate-insertion authority. Unexpected
runtime INSERT grants fail readiness and certificate reading.

This trusts the operator/migration role and PostgreSQL's protected immutable
storage. It is not a defence against an administrator or host compromise, a signed
external archive, or an off-host backup system.

## What is preserved

Blocks contain exact original event/cursor/valuation/source rows, UUIDs, timestamps,
hashes and original public receipt bodies. Financial movement is limited to older
non-journal `valuation`/`reconciled` observations. Journal headers/lines, asset
movements, fills, order evidence, permanent order identities, investment flows,
controls, tax-reserve/correction records, alerts, checkpoint/audit anchors and the
financial load tail remain in their operational tables. No acquisition basis or
economic posting is deleted. Raw receipts referenced by normalized public trades
remain in SQL too. Unselected capture receipts, normalized trades and venue records
are not pruned by this operation.

Compression changes representation without shortening financial retention. Keep
the blocks, certificates, journals, identities and their backups for the existing
financial retention period; retain open acquisition/basis evidence longer as
required. Losing a required block or anchor blocks entry ownership. Do not delete
blocks after an ordinary backup or treat a database-size measurement as authority
to discard history.

Complete `paper audit` uses the original financial rows across both representations,
including per-event journal checks and every checkpoint. Complete historical reads
also reconstruct original observations and economics. The existing V1 file export
can read through compacted history within its unchanged 100,000-observation bound;
its standalone verifier remains an integrity check. Full database backups preserve
all blocks beyond that file-format bound. V1 file input cannot silently replace a
compacted account's required historical certificate.

## Indexed reports and suffix limits

The certificate binds account, full financial seal, baseline/head values, cursor,
catalogue root/count, complete-period statistics, actor/reason, installed source,
application, financial dependency/Python versions and policy. PostgreSQL checks
compressed-byte and summary digests without expanding old rows. Calendar indexes
select relevant report blocks. Full-period reads reuse exact cumulative statistics;
subperiods merge complete blocks and reconstruct only their partial boundary blocks.
Cross-block drawdown preserves intermediate losses and rebounds, and external flows
retain their exact dates. Report ratios retain 50 calculation digits in JSON;
money/quantities retain the existing NUMERIC(38,18) boundary. Midnight selection also
reads the allowed five-second following-source bridge across a block boundary.

New observations after the certificate are always validated, even when requesting
an older endpoint or day. Unsupported/corrected accounting remains invalid; an
incomplete prefix stays incomplete. Sealing never resets units, inception or policy
high-water marks, loss latches, protection, reservations or consumed commands.

The reviewed **online suffix bound is 4,096 financial events and 4,096 observations**.
New entries leave an additional 32-event financial allowance for state transitions;
the independent dispatch check vetoes exhaustion before transmission. Safety
cancellation/position handling remains available to an existing owner. A suffix over
the bound blocks startup/reports until stopped-worker maintenance. This is an
additional conservative bound, not an increase to the existing report/order/fill/
journal, database or memory limits. At one observation per second, 4,096 observations
last about 68 minutes; faster input reaches the bound sooner. Plan maintenance
before that point and retain evidence of any capture interruption.
An account without a certificate has the same entry-capacity check, measured from
its opening history. Omitting the certificate cannot bypass maintenance limits.

The catalogue is limited to 100,000 blocks; each has at most 256 rows, a 16 MB decoded
hard bound and a bounded compressed size. Source receipt reads use small pages and
byte-aware batching. The original 2 GB database and 15% free-disk stop remain. No
56-day storage or market-throughput guarantee follows: raw input rate, normalized
trades, unselected receipts and retained venue evidence still consume space. The C3
choice is a **reviewed lossless retention path with measured bounded operations**;
actual deployment growth, maintenance interruptions and availability must be measured
before G4. Exhaustion remains a stop, never permission to discard records or relax
risk. Provisioning/scheduling a maintenance service is deployment work.

Code/policy/financial-environment changes require a full operator re-audit. Report
summaries are namespaced by exact source hash so older summary semantics cannot be
reused under new code. Later compaction can make an older catalogue certificate
obsolete; restart workers with the explicitly returned current UUID. There is no
automatic latest-certificate selection or runtime self-renewal.

## Reproduce the engineering evidence

```sh
python3 scripts/ci_database.py --linux
python3 scripts/capacity_trial.py --compaction
python3 scripts/restore_drill.py
python3 scripts/paper_release_smoke.py
```

Every helper owns new disposable resources and accepts no retained target. The new
capacity mode starts at the original 100,000-event/observation limits, compacts,
adds the complete 4,096-event/observation suffix, compares indexed and independent
full report economics, and measures startup, replay, reporting and daily summary
against the unchanged 30/20/10/20/15-second budgets. It also retains the twenty real
one-second maintenance ticks. Bulk construction is not an ingestion-rate test;
the loop fixture has no replay/market workload or qualified episode. A failed
compaction-mode timing makes that CI job fail. Older SQL/V1 measurements remain
separate historical evidence and cannot be represented as passes.

The populated restore includes a real compacted account, protected blocks and
certificates, compares all 32 tables/permissions, and checks two recoveries and the
declared missing suffix. It remains local logical-restore evidence. Off-host/WAL
recovery, production RPO/RTO and real exchange protection remain later gates.
