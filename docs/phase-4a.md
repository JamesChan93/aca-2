# Phase 4a — research fidelity and candidate promotion

Plan established 24 October 2026 at the user's request to turn the
[19 research-review recommendations](research-promotion-review-2026-10-24.md)
into a phase. Baseline: merged PR #28, commit
`bdcaed830457ceb38294a79fa4adfe5227f4f208`, application 0.4.19,
schema `0016_history_segments`.

**Status: in progress; 6/19 completion items.** P4A-17 closes through the
[merged CI repair](phase-4a-ci-repair.md) and its successful hosted checks.
P4A-18 closes through the [modular artefact/restore increment](phase-4a-artifacts.md),
its bounded streaming/lifecycle tests and actual byte-identical baseline restore.
Those increments and version 0.4.21's [independent P4A-19 parity fixtures](phase-4a-parity.md)
are merged with their hosted checks passing. Version 0.4.22 delivers
[Batch B causal execution, three clocks, metrics and protection](phase-4a-causal-replay.md),
closing P4A-01/04/05/06 with source-bound synthetic verification. P4A-19 stays open
for Batch C's continuous-account/comparator checks. The remaining 13 items are
unchecked. No corrected actual-data study has run. Current integrated PR checks must pass before
merge; prior CI success is not transferred to new code.
The checklist below is the authoritative Phase 4a scope and count. The existing
[Phase 4 completion register](phase-4-completion.md) remains closed at 17/17.
The numbered items map directly to recommendations 1–19; the batch and study
tables organise their delivery without adding completion conditions.

**Objective**

Produce a faithful, reproducible evaluation of the intended GBP portfolio, identify
whether any bounded candidate family meets the original promotion requirements,
and implement the evidence path into qualifying local paper observation.
Success means a reliable decision and completed engineering, with either an
eligible candidate or a documented no-go. A profitable candidate is not a promised
deliverable. A no-go must not keep adding work to this phase indefinitely.

Current evidence is preserved in the [reviewed-data study](research-reviewed-validation.md)
and [review diagnostics](evidence/research-promotion-diagnostics.json): 20 candidates,
300 complete eligible folds, zero passing confidence/economic gates, a closed
holdout and zero qualifying paper days/episodes. Fourteen cash point estimates are
positive; all 20 passive-10% increments are negative. The model limitations and
reproduced metric defect motivate this phase; they do not reverse that decision.

**Boundaries retained**

- Python modular monolith; PostgreSQL operational authority; Decimal financial
  arithmetic; GBP; BTC/GBP and ETH/GBP; one nominated account and execution writer.
- Existing 5% soft drawdown, 10% hard drawdown, 2% UTC daily loss, 0.5% episode
  planned risk, 30% total/15% asset exposure, 1% aggregate planned risk, two-position
  maximum, zero leverage and no pyramiding. Deposits and reporting boundaries
  cannot reset latches. Hard halts require an audited manual review.
- Independent portfolio admission and execution vetoes; permanent economic IDs;
  no blind retry of consumed or uncertain commands; protection survives halts.
- Mandatory cash and predeclared 10% passive comparisons, realistic variable costs,
  uncertainty/selection controls and the twice-annual-fixed-cost economic hurdle.
  Matched-exposure comparisons supplement these requirements.
- Final holdout stays **1 October 2025 to 1 October 2026, exclusive at the end**.
  Ordinary research, diagnostic probes and CI cannot open it. An actual opening
  requires an eligible frozen candidate, complete required evidence and recorded
  review; preserve its one-time use across dataset revisions and artefact trees.
- Phase 5 retains at least 56 calendar days, 10 completed signal-driven episodes
  and its reliability, reconciliation, recovery and economic requirements. No
  synthetic or earlier operational activity receives qualifying credit.
- No Kraken order writer, authenticated account access, real orders, paid services,
  production deployment or actual outbound messages are authorised by this plan.
  The user request creates this planning deliverable; it does not start a study,
  capture process, holdout evaluation or deployment. Existing explicit session
  authorisations continue to apply to later work within their stated conditions.

No new assets/venues, USD/USDC execution, ML, dashboard, microservices, distributed
infrastructure, tax filing or general accounting expansion is included. Required
tax/opening-inventory and private-account evidence retain their later-stage gates.

**Implementation batches and decision points**

| Batch | Included work | Dependency / reviewable output |
| --- | --- | --- |
| A — Evidence and verification foundation | P4A-17, P4A-18; establish P4A-19 fixtures | Repaired release checks, durable small artefacts, preserved baseline and independent probe fixtures |
| B — Causal execution and metrics | P4A-01, P4A-04, P4A-05, P4A-06; extend P4A-19 | Event-time execution, separate clocks, realistic protection and reconciled metrics; synthetic parity before actual-data evaluation |
| C — Intended account and coherent comparison | P4A-02, P4A-03, P4A-07, P4A-10, P4A-13, P4A-14; finish P4A-19 | Continuous account, contribution/cost scenarios, common calendar, benchmark/cost evidence and explicit data blockers |
| D — Corrected original-family study | P4A-11, P4A-12; rerun S0 | Frozen statistical protocol, enforced eligibility, original 20-candidate report and decision on the mechanisms worth testing next |
| E — Bounded additional research | P4A-08, P4A-09; studies S1/S2 | Registered low-turnover and joint-portfolio evidence, full search accounting and final candidate/no-go review |
| F — Qualifying-paper preparation | P4A-15, P4A-16 | Evidence-bound study workflow and measured sustainable local operating profile; qualifying clock remains unstarted until its start decision |

These are logical batches, not a requirement for one PR per item or batch.
Independent small fixes may proceed together. Corrections discovered by a failing
acceptance check stay under its existing ID. Strategy research must not begin on
an unverified evaluation model. External fact availability can block candidate
eligibility even when the corresponding engineering/reporting work is complete.

**Research budget**

| Study | Maximum candidates | Fixed scope |
| --- | ---: | --- |
| S0 — Corrected baseline | 20 | Original five variants × BTC/ETH × daily/four-hour; corrected execution/accounting/calendar, with original results retained separately |
| S1 — Lower turnover | 4 | At most two additional daily signal definitions applied to both assets; investigate a hysteresis rule and a confirmation rule, with exact parameters and stop re-entry semantics fixed before evaluating them |
| S2 — Shared account | 2 | At most two deterministic BTC/ETH portfolios using eligible definitions from the declared family; shared capital/risk, fixed allocation/priority rule and no weight optimisation |

The ceiling is **26 Phase 4a candidate definitions**, including the original 20.
S0 daily SMA100 is a research reference, not a preselected winner. S1/S2 declarations
must state hypotheses, decision rules, all parameters, exact data ranges and costs
before their first performance evaluation. A joint portfolio counts as a candidate.
Neighbouring parameters and cost/latency stresses are registered sensitivities,
cannot supply an alternative selected winner, and must enter the selection review
if used for selection. Previous research and adaptive choices remain in the trial
registry; the 26-candidate ceiling is not permission to reset the statistical family
or regard reused history as independent evidence.

The S0 review must explain why any proposed S1/S2 mechanism is worth its declared
test. There is no requirement to exhaust the search budget. A decision to stop
research can close the research assessment with a no-go; unimplemented engineering
items remain visibly incomplete. Expanding the family, changing the mandate,
opening another holdout or adding an unrelated feature requires an explicit scope
amendment, not a new unnamed blocker.

**Fixed completion checklist**

Each item closes with a linked implementation/review result, the tested revision,
commands/artefact identities and material limitations. Test counts alone do not
close items. All boxes start unchecked; the source review and existing tests are
baseline evidence rather than Phase 4a implementation credit.

- [x] **P4A-01 — Causal execution timing.** Replace the extra full-bar delay with
  ordered post-finalisation event-time execution using retained trades and
  explicitly bounded price/liquidity assumptions. No future price, whole-bar future
  liquidity or invented depth/maker fill may authorise an earlier fill. Prove that
  future events cannot alter earlier decisions/fills. Retain the named bar-only
  pessimistic sensitivity. Depends on P4A-04/P4A-18 and the P4A-19 timing fixtures.
  Acceptance: [0.4.22 ordered-trade implementation and prefix fixtures](phase-4a-causal-replay.md#p4a-01-ordered-trade-execution),
  [source-bound checks](evidence/phase-4a-causal-replay.json). This closes model
  capability; actual input assurance/integration/study remain P4A-14 and Batches C/D.

- [ ] **P4A-02 — Continuous OOS account.** Carry cash, positions, protection, fees,
  units, high-water marks, risk latches and pending operations across contiguous
  test windows. Isolate training/validation experiments explicitly. A reporting
  split must leave economics unchanged; an open episode retains its identity and
  risk budget across the split. Audited manual epoch changes are explicit inputs,
  never automatic fold resets. Depends on P4A-01/P4A-04/P4A-05/P4A-06.

- [ ] **P4A-03 — Contributions and economic frontier.** Apply the declared opening
  capital and £100/£250/£400 month-end contribution scenarios consistently to each
  continuous candidate/comparator account. Keep flows out of returns. Version
  actual/planned fixed-cost inputs, retain the twice-fixed-cost hurdle and show
  statistical eligibility separately from economic eligibility. Independently
  reconcile scenario cash, units and household infrastructure attribution. Neither
  capital arithmetic nor the £1m aspiration can affect sizing. Depends on P4A-02.

- [x] **P4A-04 — Three explicit clocks.** Version and separate slow-signal
  freshness, two-second state-bound pre-dispatch risk approval, and bounded
  transmission/matching latency. Keep finalisation, fresh quote/account checks,
  expiry, no stale backlog and uncertain-outcome recovery. Demonstrate expired
  approvals cannot dispatch; successful dispatch followed by delay retains its
  command identity. Report actual delay effects separately from deliberately
  expired/no-entry stresses. Amend A26's planned timing semantics before adoption.
  Acceptance: [A26 amendment, versioned clocks and real-PostgreSQL boundary tests](phase-4a-causal-replay.md#p4a-04-three-clocks-with-legacy-interpretation-preserved),
  [0.4.22 source/check evidence](evidence/phase-4a-causal-replay.json). Legacy
  selections/hashes retain their original timing; accepted delay cannot replay a command.

- [x] **P4A-05 — Correct metrics and attribution.** Calculate drawdown from all
  available observations and inception, with its observation resolution disclosed.
  Reproduce the review's 10%-intraday-loss case correctly. Reconcile NAV to gross
  marked P&L, actual modelled fees, spread/slippage, external flows and terminal
  inventory; include partial/open episodes, reason-coded skips, halt time and
  exposure. Keep daily data for daily statistics and undefined outputs unavailable.
  Acceptance: [all-observation metrics, two attribution identities and literal flows](phase-4a-causal-replay.md#p4a-05-all-observations-and-reconciled-attribution),
  [0.4.22 checks](evidence/phase-4a-causal-replay.json). Observation resolution and
  stale marks remain explicit; no continuous-OOS/comparator/statistical approval is inferred.

- [x] **P4A-06 — Protective-exit chronology.** Model stop triggering, subsequent
  latency, limit-or-better execution, partial fills and gap-through nonexecution
  from ordered admissible evidence. No same-event causal shortcut or sale below
  the approved limit. Retain the old minimum-price convention as a named stress.
  Independent fixtures cover recovered bid, rapid gap, no liquidity and residual
  protection. Depends on P4A-01/P4A-04 and P4A-19 fixtures.
  Acceptance: [chronological protection and shared independent stop oracle](phase-4a-causal-replay.md#p4a-06-protection-from-chronological-evidence),
  [0.4.22 source/check evidence](evidence/phase-4a-causal-replay.json). The legacy
  minimum-price stress is retained separately; research does not claim transport certainty.

- [ ] **P4A-07 — Benchmarks and exposure diagnostics.** Preserve mandatory cash/
  passive-10% comparisons and the other original comparators. Predeclare passive
  deployment/rebalancing and bounded handling of unfilled budgets; report intended
  versus achieved allocations. Add past-only exposure/risk-matched and risk-sized
  always-long diagnostics without replacing the mandatory comparator. Equal cash
  flows and execution assumptions must be auditable. Depends on P4A-02/P4A-03.

- [ ] **P4A-08 — Bounded low-turnover research.** Support and preregister S1 within
  its four-candidate ceiling, with unchanged protective/risk limits and explicit
  stop re-entry semantics. Record all attempted/abandoned trials, costs, whipsaws,
  missed trends, parameter stability and completed/open episode evidence. If the
  S0 review stops further actual-data research, retain implemented synthetic
  capability/protocol checks and the explicit no-run decision; do not claim any
  unrun variant passed. Depends on the verified model and the S0 review.

- [ ] **P4A-09 — Shared-account portfolio research.** Support S2's maximum two
  deterministic portfolios on common dates, using one capital ledger, shared
  cash/aggregate risk and explicit simultaneous-signal handling. Show correlated
  losses and joint uncertainty; do not add independent £250 subaccounts or
  optimised weights. Preserve the one-writer boundary. An S0/S1 no-run decision
  requires the same explicit separation of implemented capability and absent
  empirical qualification as P4A-08. Depends on P4A-02/P4A-07/P4A-10/P4A-11.

- [ ] **P4A-10 — Complete common calendar.** Freeze calendar origins, past-only
  warm-up and terminal partial-window handling before reruns. Account explicitly
  for the original unused pre-holdout tails; show common-calendar comparisons and
  labelled supplementary earlier history. Preserve the fixed holdout boundary and
  all eligible losing/outage periods. Describe fixed-rule rolling evaluation
  accurately; no fitted training procedure is implied where none exists.

- [ ] **P4A-11 — Calibrated statistical decision.** Preregister the complete
  comparison family, history reuse, block sensitivities, selection procedure,
  precision target and deterministic seeds. Use sufficient tail resolution for
  the chosen method; test false-positive control and known-effect sensitivity on
  synthetic correlated/clustered returns. A joint/studentised method requires a
  justified recorded method decision, not a post-result switch. Keep baseline
  Bonferroni evidence and disclose Monte Carlo uncertainty. Depends on P4A-05/
  P4A-10/P4A-18; ready before S0 inference or S1/S2 selection.

- [ ] **P4A-12 — Enforced research gates and holdout lineage.** Implement typed
  failed/incomplete/freeze-eligible/holdout-reviewed outcomes with exact data,
  code, strategy, cost, policy and evidence bindings. Reject failed/insufficient
  candidates at freeze and recheck before opening. Make one-time holdout use
  durable across equivalent source lineage and artefact revisions; crash/replay
  cannot reopen it. Keep synthetic workflow tests separate. Demonstrate rejection
  paths and a wholly synthetic positive path without opening the actual holdout.
  Depends on P4A-03/P4A-07/P4A-11/P4A-14/P4A-18.

- [ ] **P4A-13 — Measured execution-cost inputs.** Separate conservative sizing/
  risk fee bounds from modelled realised maker/taker charges. Produce a versioned
  public evidence packet for spreads, latency, queue/fill assumptions and adverse
  selection using retained or expressly started nonqualifying local observations.
  Report conservative taker and stressed assumptions. Maker economics require
  supporting trade/depth calibration; bar touches cannot earn fills. Private
  account fees remain a separately authorised later input. No actual webhook or
  account operation is part of this evidence task.

- [ ] **P4A-14 — Data-assurance decision.** Produce an immutable source/checksum,
  continuity, revision and historical-fee/rule review. Verify available full
  archive checksums where required for the claimed assurance; do not equate a
  local hash with truth/completeness. Resolve the disputed BTC record by evidence
  or retain an explicit quarantine/block, including its blend-comparison effect.
  Unobtainable facts stay identified gate blockers with a bounded attempt/review
  record. A completed audit may conclude blocked/no-go; it cannot mark the missing
  fact verified. Unknown gaps remain blocked and no-trade bars have zero liquidity.

- [ ] **P4A-15 — Qualifying-paper study capability.** Add a versioned study model
  and reviewed migration/CLI path binding an eligible candidate and recorded
  start approval to account, release, policy, costs and evidence. Preserve existing
  operational-only records and zero credit for past/synthetic activity. Record
  durable calendar time, dependency-aware health intervals, financial discrepancies,
  faults and completed signal episodes across restart. Test a synthetic qualifying
  workflow and rejection of missing/replayed/changed approval evidence. This box
  closes capability, not an actual observation start or G4 pass. Depends on P4A-12.

- [ ] **P4A-16 — Sustainable local observation profile.** Measure representative
  input/write rates, resource use, storage growth, backlog and maintenance costs on
  the stated local resource profile. Provide a bounded capacity/retention model
  for the 56-day workload and demonstrate the scheduled maintenance path fits the
  99.5% availability budget under its declared rate. Preserve financial history,
  protection, risk checks and independent rebuilds. Any new record/storage bound
  needs explicit measured justification and an amended supported profile, never a
  silent limit increase. State safe exhaustion/recovery behaviour and remaining
  production backup/host-loss limits. This is engineering capacity evidence, not
  56 elapsed days of reliability. Depends on P4A-15/P4A-17/P4A-19.

- [x] **P4A-17 — Release verification repair.** Address the baseline setuptools
  advisory through the reviewed constraint/hash-lock process and diagnose the
  bounded-history harness timeout. Retain partial timings/profiles and distinguish
  fixture setup from measured operation deadlines. Keep acceptance deadlines and
  security checks enforced; measurement-only jobs cannot supply acceptance.
  Require the relevant regression, packaging and container checks on the integrated
  release, with current hosted outcomes linked. Do not provision retained services
  or represent the failed baseline CI as green.
  Acceptance: [repair packet and final hosted runs](phase-4a-ci-repair.md), tested
  head `eae9a2b825f5d1ce7615cb4dad9e9addb27917ee`, merged in PR #29.

- [x] **P4A-18 — Modular durable research artefacts.** Implement small typed
  manifests referencing immutable hashed trial/fold curves and episodes, complete
  start/outcome/failed-trial records, deterministic resume and an offline review
  packet. Preserve the 0.4.19 report and reproduce its identity. Demonstrate a
  complete study beyond the former single-JSON reader bound without increasing
  unbounded memory or losing failure evidence. Document and verify local archive/
  restore of the complete research bundle; paid/off-host provisioning stays later.
  Acceptance: [0.4.20 implementation and limits](phase-4a-artifacts.md),
  [source-bound actual restore identity](evidence/phase-4a-artifacts.json),
  `tests/test_research_bundles.py` and the PostgreSQL registry regression. The
  original report and its 42 events are preserved; no corrected study ran.

- [ ] **P4A-19 — Research/paper parity and focused refactoring.** Share pure
  economic transitions where useful while retaining the independent execution
  veto. Centralise versioned assumptions and document every expected difference
  between research and paper. Independent fixtures cover timed signals, fills,
  fees, stop gaps, partial/open episodes, flows, latches and reporting boundaries.
  Preserve financial property/lifecycle tests and paper-only capability. Supply
  one integrated status/evidence register so historical limitations do not become
  new unnamed work. No framework rewrite or new service is required.
  Batch A progress: [independent fixtures, versioned simulation bounds and named
  baseline probes](phase-4a-parity.md), application 0.4.21. This establishes the
  verification foundation; Batches B/C must extend it before this box closes.
  Batch B extends that same oracle to corrected ordered research, journals,
  trigger/matching clocks, attribution, explicit flows and duration reports in
  [0.4.22](phase-4a-causal-replay.md). Batch C still owns account/calendar parity.

**Acceptance and phase closure**

Phase 4a closes when all **19 engineering items** have linked acceptance evidence
and an integrated review records their results, original/corrected research
identities, actual study runs, complete trial registry and candidate/no-go decision.
The gate evaluation retains at least four years of qualified history, 200-bar
warm-up, two years aggregate OOS, 50 completed OOS episodes, positive required
adjusted incremental lower bounds, stress/stability evidence and the unchanged
economic test. Failed or missing evidence cannot become a pass through a label.

The final packet must separately state:

- Phase 4: historical closed 17/17; Phase 4a: completed item IDs and count.
- Which S0/S1/S2 studies actually ran; which were stopped/unrun and why.
- Data-assurance, statistical, economic, holdout and observation-start decisions.
- Whether a candidate is eligible for freeze and whether actual holdout use has
  occurred. A successful synthetic gate fixture does not answer either question.
- Whether qualifying observation may start, its exact approval/start record and
  current real days/episodes. They remain zero until a genuine approved study starts.
- Current release/CI outcome, measured operating profile and outstanding external
  deployment/readiness conditions. Historical engineering closure cannot waive a
  current regression or a later gate.

A fully implemented phase with all candidates rejected closes as **engineering
complete / research no-go**. An external fact can remain a named candidate or
deployment blocker when its engineering control/reporting acceptance is complete.
Unimplemented items remain unchecked; no generic "validation remaining" entry or
unrelated feature may extend this fixed list. Amendments must record the user
decision, affected ID/source requirement and changed acceptance before changing
the count or research budget.

Actual Phase 5 observation, host provisioning, off-host/WAL deployment, private
Kraken inspection, live-writer implementation and live activation retain their
original separate decisions. Eight weeks is an operational minimum; slower daily
strategies may take much longer to produce ten episodes. No extra trades or
relaxed evidence threshold may be used to accelerate promotion.
