# Phase 4 completion checklist

Register introduced by the scope review of 19 October 2026. Its audited baseline
was merged PR #16, commit `bafb928`, application 0.4.9. Current closure review:
22 October 2026, application 0.4.18. The linked release packet records the applicable
local-paper decision; this register alone grants no deployment or live permission.

## Source and scope

Section and gate references below refer to the user's **Kraken Spot Trading
Platform — Architecture, Feasibility and Engineering Specification**, planning
baseline 17 October 2026. Its Phase 4 row specifies journal/unitisation, sizing,
independent controls, reservations, paper lifecycle, reconciliation and a fault
harness. Its exit is G2 safety evidence, deterministic replay and no enabled Kraken
order writer. The [context summary](context/README.md) distinguishes that later
specification from the archived original planning prompt.

The four work packages below are the implementer's allocation of those
requirements. The user did not prescribe this breakdown or one PR per package.
The earlier status list mixed Phase 4 code, Phase 5 measurements, later operational
readiness and implementation choices. It is superseded by this checklist. Earlier
increment reports retain their historical limits and results; this document and
the current status register govern the remaining-work allocation.

Thirteen merged PRs (#4–#16) delivered the paper core and supporting increments.
Dedicated notification-failure reviews and the controlled concurrent-snapshot
fixture were engineering choices supporting broader requirements. The specification
does not require those exact features or an indefinite series of similar additions.

## Existing implementation to retain

These capabilities have implementation and recorded test evidence. They are not
new projects to repeat before each increment:

- [GBP journals, unitisation, sizing, independent risk and the local venue](phase-4.md),
  including permanent client IDs, uncertainty, partial fills and protective sells.
- [Checkpointed maintenance](phase-4-supervision.md), [ordered public replay](phase-4-replay.md)
  and [original-time risk observations](phase-4-historical-risk.md).
- [Historical performance](phase-4-performance.md), [manual tax-cash reserves](phase-4-tax-cash-reserve.md)
  and [durable outbound notifications](phase-4-notifications.md), including the
  subsequent scheduling, health and review increments.
- [Process-crash recovery](phase-4-process-recovery.md) and the
  [disposable logical-restore drill](phase-4-local-restore.md).

The audited baseline recorded **515 passing tests** in PR #16. Current C1 results
are linked in its completion evidence; neither count measures completed requirements. A named
test below identifies relevant evidence; it does not assert that its entire T-group
or a release gate is complete.

## Fixed remaining Phase 4 work

**C1–C4 are complete in the 0.4.18 local-paper release: 17/17 conditions.**
The [consolidated handoff](phase-4-paper-release.md) and
[current release packet](evidence/phase-4-paper-release.json) record the explicit
applicable local-paper G2 decision and all T00–T32 allocations. C1's five conditions
and C2's three retain their original acceptance evidence and pass the current
regression. C3's final condition uses its reviewed lossless-retention option;
56-day headroom is not claimed. C4 supplies the packaged runbook and review.

The count closes engineering work only. All 20 research candidates remain rejected,
qualifying observation has not started, and G3–G6 remain unapproved. Earlier partial
increments below retain their historical outcomes; they do not describe the current
release. Changes to this fixed register still require the scope rule below.

### P4-C1 — Complete the continuous paper workflow

**Source:** §§8, 11–13, 16, 20 Phase 4; T01–T04, T14–T17, T23, T29, T31.
**Gap at the audited baseline:** the [supervisor](../src/aca/app/paper_supervisor.py) maintains and
replays existing accounts; it explicitly reports `strategy_dispatch_enabled=false`.

- [x] Add explicit selection of an existing versioned strategy, instrument and
  daily/4-hour interval for local paper execution, with its configuration recorded.
  An operational test selection must remain visibly non-qualifying. No rejected
  research candidate is automatically promoted, frozen or granted holdout access.
- [x] Connect persisted closed candles and sufficient past-only warm-up to the
  existing pure strategy functions after the ten-second finalisation delay. Record
  the decision watermark and stable strategy/instrument/decision identity. Gaps,
  unfinished bars, insufficient warm-up and expired decisions cannot cause entries.
- [x] Persist decisions and use the existing sizing, portfolio risk, reservations,
  dispatch guard and local venue for entries and exits. Duplicate ticks, duplicate
  candles and restart cannot create a second economic action. Recovery retains the
  consumed-command rule; backlog cannot create stale catch-up orders.
- [x] Integrate UTC-day boundaries with risk observation and daily reporting. Valid
  point-in-time boundary evidence must be auditable; missing evidence must keep the
  existing daily suspension/provisional-report behaviour. Exact-boundary pure tests
  alone do not establish that the running workflow captures the boundary.
- [x] Demonstrate the complete runner with deterministic input through a long/flat
  signal, bounded entry, fill/fee posting, protection, strategy exit and restart.
  Repeat with a stale feed, data gap and loss latch and prove entries are blocked.
  Produce status and daily-report evidence from that same account. The fixture is
  operational evidence and contributes zero qualifying paper days or episodes.

**Done when:** a documented opt-in local command runs this connected workflow,
the above integration/restart cases pass, and no separate hand-issued demo order
is required to produce the tested signal-driven lifecycle. Default startup remains
paper-only; the runner supplies no research promotion or Kraken write capability.

### P4-C2 — Finish supported-account accounting operations

**Source:** §§9, 12, 20 Phase 4; T09, T11, T14–T15, T17.
**Gap:** purchases, sales, fees, contributions/withdrawals and manual reserves exist;
operational transfer and correction/rebuild paths remain incomplete.

- [x] Support explicit quantity transfers into/out of the nominated virtual account
  for its admitted GBP/BTC/ETH assets, with external reference, event-time GBP
  valuation and investment-flow classification. A transfer across this managed-account
  boundary must not be reported as trading P&L or a tax disposal by assumption.
- [x] Expose audited reversal/replacement of supported erroneous accounting records,
  using immutable links and reasons. Rebuild or explicitly invalidate affected
  projections, units and historical reports; previously issued reports retain their
  original version/evidence. Do not add an UPDATE/DELETE shortcut.
- [x] Test duplicate/conflicting references, an unavailable GBP valuation, out-of-order
  correction, insufficient inventory/reserved funds and restart. Compare balances,
  journal totals and flow-neutral returns with independently calculated fixtures.
  Pending fills and uncertain commands continue to block unsafe mutations/entries.

**Done when:** supported transfers and corrections have an operator path, immutable
evidence and independently checked financial results. Existing cash-flow, fee and
reserve behaviour remains covered. Owner-wide tax pools, tax estimates/payments,
USD/USDC funding and general FX execution are outside this package.

### P4-C3 — Close the named local-paper safety gaps

**Source:** §19; §20 Phase 4; G2. The scenario inventory below fixes the coverage
review to T00–T32 and distinguishes paper evidence from future exchange evidence.

- [x] Map every applicable paper assertion in that inventory to a test/result on the
  integrated release. Retain the required 1,000-example financial properties and
  10,000 generated lifecycle sequences with reproducible seeds/failure traces.
  Existing passing assertions need regression verification, not replacement features.
- [x] Exercise actual storage-write failure in an isolated, disposable environment,
  including the database/WAL storage boundary, rather than only raising the mocked
  capacity exception. Prove zero unaudited submissions, durable reservation/ledger
  consistency, unhealthy entry blocking and disarmed recovery. Record the exact
  failure mechanism and cleanup. Never fill the workstation's retained volume.
- [x] Extend the existing restore fixture with populated account replay/valuation and
  scheduled-notification/review records. Verify their source anchors, financial
  totals and delivery identities survive restore and two recoveries; no order is
  resubmitted or fill booked twice. Use a declared missing-suffix case to prove the
  report identifies the gap and continued qualifying observation is blocked pending
  reconciliation/review. Do not claim the archive recovers absent evidence.
- [x] Run a bounded capacity trial for the configured local paper workload, covering
  the one-second risk loop, startup audit and reports at the documented supported
  record limits. Record CPU/memory/storage growth, backlog and failure thresholds.
  Either demonstrate sufficient headroom for the planned 56-day workload or provide
  a reviewed retention/archive path preserving financial history and permanent IDs.
  This is an engineering capacity check, not 56 days of reliability evidence.
- [x] Prove integrated critical local faults are detected within ten seconds and an
  outbound notification attempt occurs within 60 seconds using a controlled test
  transport. Include unreachable/throttled delivery; finance and order processing
  remain independent. Real Discord receipt and sustained availability are Phase 5
  measurements, with separate destination setup.

**Done when:** the integrated local-paper fault report contains a result or a
reasoned later-stage allocation for every T-ID, no unresolved applicable safety
failure, and passing evidence for the specific storage, restore, capacity and
notification conditions above. Full host-loss recovery, encrypted off-host/WAL
backups, production RPO/RTO and private exchange semantics remain unproven.

**Current result:** all five conditions have current integrated evidence in the
[0.4.18 release packet](evidence/phase-4-paper-release.json). The
[bounded history decision](phase-4-bounded-history.md) preserves original financial
and source records in operator-owned lossless blocks, retains permanent identities,
and measures bounded online work at 100,000 original records plus the complete
4,096-record suffix. All five unchanged operation deadlines pass. Actual WAL ENOSPC,
32-table populated restore/two recoveries, fault-alert assertions and generated
safety suites pass. The applicable local-paper G2 decision is recorded in C4;
full T25 and private/deployed assertions retain their explicit later allocations.

**Historical capacity results:** the following 0.4.12–0.4.17 increments did not close
condition 4 individually. Their failures are preserved, not reclassified as passes.
The original 0.4.12 report/replay calls exceeded 20 seconds; 100,000 one-second
observations represent only about 1.16 days. The current release uses the reviewed
retention-path alternative, not a claim that those rows demonstrate 56 days.

The [0.4.13 capacity increment](phase-4-capacity.md) reduces audit/report overhead
and measures the actual runner/summary deadlines. It preserves the record ceilings
and full validation requirements. Its evidence does not establish 56-day retention
or close this condition.

The [0.4.14 daily-summary capacity increment](phase-4-summary-capacity.md) shares a
single financial/valuation audit for the selected day and its boundaries, batches
checkpoint anchors and reduces repeated validation work. Its measured processing
deadlines and 56-day preservation requirement remain under this same open condition.

The [0.4.15 replay preservation format](phase-4-replay-preservation.md) adds audited,
lossless export and independent verification of replay/source evidence. It retains
all PostgreSQL records and supplies no operational prefix acceptance or pruning.
It is part of condition 4's preservation implementation, not evidence that the
processing deadlines or 56-day headroom have passed.

The [0.4.16 anchored archive reader](phase-4-archive-input.md) supplies that prefix
to online reports/startup only after checking retained database anchors and
reconstructing financial economics. It retains every SQL record and leaves the
financial audit proportional to the full prefix. It closes no checklist condition;
capacity/retention measurements and remaining work stay under condition 4.

The [0.4.17 protected financial audit](phase-4-financial-prefix.md) adds an explicit
operator-owned audit result and suffix reconstruction. Runtime cannot create those
records. It retains SQL history, complete historical economic validation and the
same record/operational ceilings. It supplies no retention/pruning authority or
56-day headroom and closes no checklist condition.

This package does **not** introduce a generic backup-gap importer or a second
backup architecture. Existing paper reconciliation is used where authoritative
evidence exists. Missing evidence remains an explicit block; production recovery
of that gap stays in the deployment/readiness work below.

### P4-C4 — Review the paper release and close the phase explicitly

**Completed:** [consolidated local handoff and explicit gate review](phase-4-paper-release.md),
including its packaged CLI smoke and machine-readable acceptance results.

**Source:** §20 Phase 4 and §21 G2; §§15–16, 22 for the local handoff.

- [x] Supply one local runbook covering account creation/funding, explicit strategy
  selection, collector/runner startup, health, halt, restart, reports and restore.
  Run its smoke path on disposable state with the packaged release and retain the
  result. Include known limits and which workers require opt-in setup.
- [x] Publish the completed T00–T32 coverage review, package versions/source revision,
  reproducible command results, financial reconciliation and unresolved exceptions.
  Record the applicable G2 decision explicitly. A documentation merge or passing
  CI alone is not that decision.
- [x] Mark C1–C3 complete only with links to their acceptance evidence. If any named
  condition fails, identify that ID and failure; do not substitute a generic
  "remaining validation" entry or add unrelated improvements.
- [x] Report engineering completion separately from G3 research, G4 paper/economics
  and G5/G6 readiness/live approval. Record whether a candidate is eligible and
  whether qualifying observation can start. Rejected candidates do not keep an
  otherwise complete engineering checklist open indefinitely.

**Done when:** the review records a clear Phase 4 outcome and local-paper G2
applicability decision. Nothing in this checklist waives full T25, deployment
requirements or another release gate. Any required review that cannot pass remains
named and open; it cannot be relabelled as complete.

## Safety scenario inventory at the audited baseline

C1–C3 gaps in this historical baseline inventory are now covered by their linked
implementation/tests and the current release regression. The completed
[T00–T32 review](evidence/phase-4-paper-release.json) supplies the current allocation;
the table below preserves what the original scope review identified. Later-stage
allocations remain unchanged.

**Evidence** means relevant implementation/tests exist and need review for the
integrated release. **Gap** names a specific remaining paper assertion. **Later**
identifies evidence that cannot be supplied by this local paper application. Full
T-group completion is not claimed by these labels.

| ID | Relevant existing evidence | Remaining local-paper check / later boundary |
| --- | --- | --- |
| T00 | [Research arithmetic](../tests/test_research_core.py), [performance](../tests/test_historical_performance.py) | C2 transfer/correction arithmetic; full household/tax scenarios are later reporting work |
| T01 | [Market storage](../tests/test_market_storage.py), [stored replay](../tests/test_stored_replay.py) | C1 duplicate-candle/decision dispatch integration |
| T02 | [Market processing](../tests/test_market_processor.py), [stored replay](../tests/test_stored_replay.py) | C1 warm-up, late data and decision watermark integration |
| T03 | [Market storage](../tests/test_market_storage.py), [supervisor](../tests/test_paper_supervisor.py) | C1 disconnect/reconnect with strategy dispatch enabled |
| T04 | [Book contracts](../tests/test_market_contracts.py), [dispatch vetoes](../tests/test_paper_execution.py) | C1 stale/corrupt-feed veto through the complete runner |
| T05 | [Paper timeout/recovery](../tests/test_paper_execution.py) | C1/C3 retain one submission; actual mutating Kraken HTTP is later |
| T06 | [Public REST budgets](../tests/test_market_rest.py), [notification throttling](../tests/test_notifications.py) | C3 integrated blocked/backoff behaviour; private matching-engine budgets are later |
| T07 | [Execution](../tests/test_paper_execution.py), [process kills](../tests/test_process_recovery.py) | C1/C3 regression for runner decisions and consumed commands |
| T08 | [Process kills](../tests/test_process_recovery.py) | C1/C3 regression with the continuous runner; real venue recovery is later |
| T09 | [Fill posting](../tests/test_paper_execution.py), [notification delivery](../tests/test_notifications.py) | C2 correction identity and C3 populated restore; REST/private-stream identity is later |
| T10 | [Partial fills/cancellation](../tests/test_paper_execution.py), [process recovery](../tests/test_process_recovery.py) | C1/C3 retain actual filled quantity, residual reservation and protection |
| T11 | [Fee assets](../tests/test_paper_finance.py), [execution](../tests/test_paper_execution.py) | C2 transfer/correction movements and GBP provenance; actual account fee assets are later |
| T12 | [Generated sizing](../tests/test_research_core.py), [financial boundaries](../tests/test_configuration.py) | C3 retain generated limits; C1 exact rounded economics reach dispatch |
| T13 | [Metadata refresh](../tests/test_market_rest.py), [sizing](../tests/test_research_core.py) | C1 changed rules between signal and dispatch skip without risk relaxation |
| T14 | [Financial valuations](../tests/test_paper_finance.py), [historical replay](../tests/test_historical_replay.py) | C1 boundary/warm-up gaps and C2 unavailable transfer valuations |
| T15 | [Unit flows](../tests/test_paper_finance.py), [historical replay](../tests/test_historical_replay.py) | C2 transfers/corrections preserve flow neutrality and drawdown |
| T16 | [Exact thresholds](../tests/test_paper_finance.py), [historical replay](../tests/test_historical_replay.py) | C1 loss crossing precedes another scheduled entry |
| T17 | [Portfolio/dispatch faults](../tests/test_paper_execution.py) | C1/C3 integrated unsafe-state veto; C2 invalidated reports/projections |
| T18 | [Transaction failures](../tests/test_paper_execution.py), [backend loss](../tests/test_process_recovery.py) | C3 actual storage-write failure and recovery with the integrated runner |
| T19 | [Capacity-fault injection](../tests/test_paper_supervisor.py) | C3 actual isolated storage exhaustion/write failure and bounded capacity trial |
| T20 | [Secret redaction](../tests/test_logging_and_secrets.py), [destination failures](../tests/test_notifications.py) | C3 preserve safe errors; invalid Kraken credentials are later because private access is absent |
| T21 | [Status/rule gates](../tests/test_market_processor.py), [replay faults](../tests/test_stored_replay.py) | C1 suspended pair/maintenance cannot admit a scheduled entry |
| T22 | [Paper recovery](../tests/test_paper_execution.py), [restore drill](phase-4-local-restore.md) | C3 populated replay/notice restore; outstanding real account orders are later |
| T23 | [Strict inputs](../tests/test_configuration.py), [strategy/sizing](../tests/test_research_core.py) | C1 reject wrong, stale, short or excessive scheduled requests through the runner |
| T24 | [Delivery/retries](../tests/test_notifications.py), [scheduled notices](../tests/test_scheduled_notifications.py) | C3 integrated timing/fault evidence; real destination and sustained delivery are Phase 5 |
| T25 | [Local restore](phase-4-local-restore.md), [snapshot guards](../tests/test_restore_snapshot.py) | C3 populated restore and declared-gap blocking; off-host/WAL/host-loss recovery and RPO/RTO are later and full T25 stays open |
| T26 | [Venue-independent strategies](../tests/test_research_core.py), [fake execution port](../tests/test_paper_execution.py) | C1 runner uses canonical contracts; actual second-venue implementation is deferred |
| T27 | [Halt/protection/gaps](../tests/test_paper_execution.py), [stored replay](../tests/test_stored_replay.py) | C1/C3 integrated halt preserves exits; actual small-fill conditional stops are later |
| T28 | [Paper-only config/CLI](../tests/test_configuration.py), [container smoke](../.github/workflows/ci.yaml) | C4 confirm no writer/live path; signed permit/account/image/replay tests belong to a separately authorised live implementation |
| T29 | [UTC reset/epochs](../tests/test_paper_finance.py), [historical replay](../tests/test_historical_replay.py) | C1 capture valid daily boundary; C2 corrections cannot clear loss latches |
| T30 | [Closed IDs/cancel races](../tests/test_paper_execution.py), [process recovery](../tests/test_process_recovery.py) | C1/C3 repeated signals/recovery cannot resend an uncertain action or oversell |
| T31 | [Clock faults](../tests/test_paper_supervisor.py), [secrets](../tests/test_logging_and_secrets.py) | C1 deadline/finalisation integration; persisted private-key nonces are later |
| T32 | [Database roles](../tests/test_database.py), [bootstrap](../tests/test_bootstrap.py), [upgrade](../tests/test_tax_reserve_upgrade.py), [CI smoke](../.github/workflows/ci.yaml) | C4 packaged runner/accounting path and upgrade/runbook review; live packaging is later |

The inventory has 33 IDs because numbering starts at T00. It is a coverage review
aid, not a replacement test specification. Within each ID, the original measurable
pass condition still applies to the supported paper behaviour. Any later-stage
allocation must be recorded explicitly in the gate review; an absent private/live
implementation is not evidence that its test passed.

## Work outside Phase 4 closure

These requirements remain in the overall project. Moving them out of the Phase 4
coding list does not remove them or authorise a deployment/promotion.

| Work | Original source / stage | Required outcome |
| --- | --- | --- |
| Research qualification | §11; G3 | Required history/OOS/trial/uncertainty evidence and 50 completed OOS episodes; all 20 current candidates remain rejected and the final holdout stays closed |
| Extended paper observation and fill calibration | §20 Phase 5; G4 | At least 56 calendar days, 10 completed signal-driven episodes, measured latency/fills and supported lifecycle replay; no synthetic/demo credit |
| Reliability and actual external notification | §§16, 21; Phase 5 | 99.5% healthy observations under the stated dependency condition, real destination test, detection/attempt timings and external heartbeat setup |
| Deployment backup/recovery | §§15, 20 Phases 5–6; G4/G5; full T25 | Encrypted off-host recovery points, WAL pipeline, backup-gap reconciliation, host-loss/fencing drills and demonstrated RPO ≤5 minutes / RTO ≤4 hours for the deployed workload |
| Owner-wide tax reporting and household accounting | §§17–18; readiness/reporting milestone before final tax output or live use | Opening declaration, complete cross-account inventory, tax matching/estimates/payments and household costs; paper reports remain provisional until this exists |
| Private account eligibility/fees and security readiness | §20 Phase 6; G5 | Separately authorised account evidence, current fees/permissions, accepted runbooks and security/restore review |
| Kraken writer, permits and real orders | §20 Phase 7; G6 | Separate implementation and activation authorisations, signed limited permit and approved canary/protection evidence |
| USD/USDC, another venue, dashboard, ML or institutional infrastructure | §§1, 23 | Deferred; require a justified new milestone/ADR and applicable authorisation |

Research eligibility and engineering completion are separate decisions. The
continuous runner can be completed and tested without finding profitable signals.
An explicitly labelled operational paper experiment cannot be counted as a
promoted candidate or qualifying Phase 5 record merely because it runs for 56 days.
The applicable local-paper G2 decision is recorded in the release handoff. No
G3–G6 or qualifying observation-start decision is made here.

## Change control and stop condition

**P4-C1–C4 are complete with linked acceptance evidence. No named Phase 4 condition
remains open in this bounded local-paper release.** Before adding a Phase 4 blocker,
record the original requirement, the concrete unsafe/missing behaviour and the
acceptance condition. An optional enhancement or later readiness measurement must
remain in its own stage. A new requirement or scope expansion requires milestone
review; an actual defect in an existing acceptance condition stays under that ID.

Every completion report must state which C-IDs/conditions closed, which remain and
why. Once C1–C4 and the applicable safety review are complete, close Phase 4. A failed
research/economic gate is recorded as that outcome rather than used to keep adding
engineering tasks. Subsequent phases retain their original approval boundaries.
