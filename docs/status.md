# Implementation status

Updated 23 October 2026. Application **0.4.18**, schema `0016_history_segments`.

**The fixed Phase 4 engineering checklist is complete: 17/17 conditions.**
C1–C4 are linked in the [completion register](phase-4-completion.md). The
[consolidated paper release handoff](phase-4-paper-release.md) records the applicable
local-paper G2 decision, supported operating profile, commands and exceptions.
Its [machine-readable evidence](evidence/phase-4-paper-release.json) contains all
33 T-ID allocations and the frozen-source release checks. This supersedes earlier
12/17 status reports without changing their historical results.

The capacity condition closes through its **reviewed lossless retention path**,
not a claim of 56-day headroom. Operator-only compaction preserves original rows,
financial postings and permanent order identities. Online startup/reports require
an explicit code/policy-bound certificate and validate the complete new suffix.
At 100,000 original events/observations plus 4,096 new records, all five existing
operation budgets passed; complete and indexed report economics agreed. The
[A31 decision](phase-4-bounded-history.md) documents authority, rollback, limits and
what is retained. Uncertified accounts cannot bypass entry-capacity checks.

This is a bounded local-paper release: one enrolled account, stopped-worker
maintenance before 4,096 new financial events/observations, existing 2 GB/disk and
order/journal/fill ceilings. At one observation per second the suffix lasts about
68 minutes, less at higher input rates. Unselected capture receipts, normalized
trades and venue evidence still consume storage. Actual feed growth, maintenance
availability and eight-week reliability remain unproven deployment/Phase 5 inputs.

The release passed **702 Linux/PostgreSQL tests**, including 1,000-example financial
properties and 10,000 generated lifecycle sequences, strict typing, formatting and
all three dependency audits. The populated restore compares 32 tables including
compacted history, preserves permissions and completes two recoveries without venue
commands. A real bounded PostgreSQL WAL ENOSPC drill preserves reservations and
recovers disarmed. The installed CLI handoff exercises setup, runner health, halt,
compaction, required-authority rejection, recovery and equal reports. All are
disposable synthetic tests with zero qualifying days/episodes.

**The qualifying Phase 5 observation clock has not started.** All 20 research
candidates remain rejected and the holdout is closed. Local-paper engineering can
finish without a profitable research result. Research rejection does not add new
Phase 4 coding requirements. Engineering completion does not qualify a candidate,
open its holdout or approve observation, hosting or live operation.

The [historical data review](research-data-audit.md) classifies the 2,833 original
research gap spans as no-trade intervals using hourly and public trade archives.
One populated BTC candle on 23 June 2026 has disputed trade-ID evidence; no repair
is applied. The reviewed data sidecar has not been consumed by a new backtest or
walk-forward run. Research qualification remains outstanding; Phase 4 stays complete.

The retained workstation stack has not been upgraded, migrated or compacted by
this release. Its existing image/schema is not this release's deployed evidence.
Use the reviewed upgrade procedure before changing retained state.

## Phase 4 closure register

| ID | Evidence | Result |
| --- | --- | --- |
| P4-C1 | [Continuous strategy-to-paper workflow](phase-4-continuous-paper.md), reverified in the current regression | Complete, 5/5; operational experiments only |
| P4-C2 | [Transfers, immutable corrections and invalidation](phase-4-accounting.md), reverified in the current regression | Complete, 3/3; corrected historical accounting stays quarantined |
| P4-C3 | [Original safety review](phase-4-safety.md), [bounded history](phase-4-bounded-history.md) and [current integrated results](evidence/phase-4-paper-release.json) | Complete, 5/5 for applicable local-paper assertions; retention-path option used |
| P4-C4 | [Packaged runbook, coverage review and gate decision](phase-4-paper-release.md) | Complete, 4/4; later gates explicitly separate |

No applicable local-paper safety failure remains unresolved in this release packet.
The G2 decision covers the stated local-paper assertions only. Full T25, private
Kraken semantics and production recovery are not marked passed or waived. Earlier
capacity runs failed their budgets and remain preserved in their increment reports;
only the integrated 0.4.18 bounded-history measurements close C3 condition 4.

## Existing implementation

| Area | Implemented and recorded evidence |
| --- | --- |
| Foundation/data | Paper-only package, restricted PostgreSQL roles, migrations, containers, public Kraken metadata/books/trades/candles, provenance and ordered replay |
| Research | Shared deterministic indicators/strategies, audited datasets, cost-aware simulation and recorded trials; 20 rejected candidates, final holdout closed |
| Accounting/risk | GBP journal/movements, owner flows, supported transfers/corrections, unitisation, reservations, Decimal sizing, independent dispatch controls, durable loss latches and manual tax-cash earmarks |
| Paper execution | Explicit versioned strategy selection, past-only finalised candles, durable decisions, local entry/exit lifecycle, protection, uncertainty/recovery and UTC boundaries |
| Reports/history | Original-time GBP valuations, reconciled TWR and bounded MWR, lossless blocks, protected summaries and full suffix validation; incomplete evidence stays provisional |
| Notifications | Opt-in outbound worker, durable bounded retries, daily summaries, critical repeats, health and exact-event failure review |
| Operations | CLI, private health/metrics, packaged handoff, bounded storage-fault and populated logical-restore drills |

## Separate later work and gates

| Stage | Work / decision still required |
| --- | --- |
| Research / G3 | Required history and OOS coverage, at least 50 completed OOS episodes, uncertainty/stability evidence and a qualifying candidate; all current candidates rejected |
| Phase 5 / G4 | Authorised observation start; at least 56 calendar days and 10 completed signal-driven episodes; measured fills/latency, 99.5% healthy observations under the specified dependency condition, real destination integration, recovery exercises and positive economics |
| Deployment/readiness / G4–G5 | Authorised hosting/service setup, actual capture growth and maintenance planning, encrypted off-host/WAL backups, gap reconciliation, host-loss/fencing drills, production RPO/RTO, heartbeat, current eligibility/fees and security/runbook review |
| Tax/reporting readiness | Owner-wide opening inventory, matching, estimates/payments and household reporting; manual earmarks are not tax estimates and reports remain provisional |
| Optional live implementation / G6 | Separately authorised Kraken writer and release tests, signed limited permit, fresh reconciliation and human-approved canary/protection evidence |

These later requirements do not reopen the fixed Phase 4 checklist. No contribution,
test count, elapsed time, restart or configuration flag grants trading permission.
Private Kraken access, paid deployment, writer implementation and every live order
retain their separate authorisation boundaries.
