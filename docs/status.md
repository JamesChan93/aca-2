# Implementation status

Updated 19 October 2026 after the scope review of merged PR #16 (`bafb928`).
Application candidate: 0.4.9. This register separates engineering completion from
research eligibility, extended paper observation and later deployment/live gates.

**Phase 4 is incomplete. Its remaining work is fixed in four named packages in the
[completion checklist](phase-4-completion.md). The Phase 5 qualifying observation
period has not started.** The checklist changes scope accounting; it adds no runtime
capability and approves no release gate.

## Existing implementation

| Area | Implemented and recorded evidence |
| --- | --- |
| Foundation/data | Paper-only package, restricted PostgreSQL roles, migrations, containers, public Kraken metadata/books/trades/candles, provenance and ordered replay |
| Research | Shared deterministic indicators/strategies, audited datasets, cost-aware simulation and recorded trials; all 20 evaluated candidates rejected, final holdout closed |
| Accounting/risk | GBP journal and quantity movements, contributions/withdrawals, unitisation, reservations, Decimal sizing, independent dispatch controls, durable loss latches and [manual tax-cash earmarks](phase-4-tax-cash-reserve.md) |
| Paper execution | Durable local venue, partial fills, cancellation races, protective sells, uncertainty/recovery, public replay and [separate-process crash tests](phase-4-process-recovery.md) |
| Reports | Audited original-time valuations, bounded historical performance, flow-neutral TWR and bounded MWR; incomplete evidence is explicitly provisional |
| Notifications | Opt-in Discord worker, durable retries, provisional daily summaries, critical repeats, cached health/metrics and exact-event failure reviews |
| Operations | Local health/metrics, private upgrade backups, runbooks and a [disposable logical restore with controlled concurrent commits](phase-4-local-restore.md) |

The latest recorded full Linux test run is 515 passes from PR #16. This scope review
does not rerun or extend that runtime evidence. Passing tests for these increments
do not prove every original T-group or gate is complete.

## Phase 4 closure register

| ID | Remaining work | Status / observable exit |
| --- | --- | --- |
| P4-C1 | Complete continuous strategy-to-paper workflow, including warm-up, finalised decisions, restart deduplication and UTC-day integration | Open: current supervisor explicitly disables strategy dispatch; close with a packaged continuous runner and deterministic full-lifecycle/fault evidence |
| P4-C2 | Supported-account transfers and audited corrections/rebuilds | Open: operator paths, immutable financial evidence and independent balance/flow/performance fixtures for GBP/BTC/ETH |
| P4-C3 | Named local safety gaps and integrated regression | Open: isolated actual storage-write failure, populated replay/notice restore and declared-gap blocking, bounded capacity trial, integrated fault/notification timing and T00–T32 coverage review |
| P4-C4 | Local handoff and explicit engineering/G2 review | Open: packaged runbook smoke, linked C1–C3 evidence, applicable safety decision and separate research/observation status |

The [checklist](phase-4-completion.md) supplies the source sections, acceptance
conditions, existing test references for all 33 IDs and change-control rule. The
four packages are an engineering allocation of the user's specification, not four
additional requirements or a promise of four PRs. The next implementation is C1.
Standalone backup/notification enhancements do not automatically become blockers.

## Separate later work and gates

| Stage | Work / decision still required |
| --- | --- |
| Research / G3 | Required history and OOS coverage, complete trials, at least 50 completed OOS episodes, uncertainty/stability evidence and a qualifying candidate; current candidates remain rejected |
| Phase 5 / G4 | At least 56 calendar days and 10 completed signal-driven paper episodes, measured fill/latency calibration, 99.5% healthy observations under the specified dependency condition, real notification integration, recovery exercises and positive economics |
| Deployment/readiness / G4–G5 | Authorised hosting/service setup, encrypted off-host/WAL backups, backup-gap reconciliation and host-loss/fencing drills, measured production RPO/RTO, heartbeat, current eligibility/fees and security/runbook review |
| Tax/reporting readiness | Owner-wide opening inventory, matching, estimates/payments and household reporting; manual earmarks are not tax estimates and existing reports remain provisional |
| Optional live implementation / G6 | Separately authorised Kraken writer and release tests, signed limited permit, fresh reconciliation and human-approved canary/protection evidence |

Local paper engineering can be completed without a profitable research result.
Research rejection does not create unlimited additional Phase 4 work. Conversely,
engineering completion does not qualify a strategy, open its holdout or approve
an observation start. Operational test selections and synthetic replay receive
zero qualifying paper-day/episode credit.

G2–G6 remain incomplete. The local restore is partial T25 evidence; moving deployed
disaster recovery into its proper stage does not pass or waive full T25. The gate
review must record applicability and outstanding later evidence explicitly.

No contribution, test count, elapsed time or restart authorises trading. Private
Kraken access, paid deployment, live writer implementation and activation retain
their separate authorisation boundaries. Subsequent milestones require review.
