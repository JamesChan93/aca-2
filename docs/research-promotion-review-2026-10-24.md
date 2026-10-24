# Research and candidate-promotion review — 24 October 2026

Reviewed commit: `bdcaed830457ceb38294a79fa4adfe5227f4f208` (merged PR #28,
application 0.4.19). This document proposes changes; it implements no application,
policy, strategy, deployment or promotion change. The fixed Phase 4 closure record
remains 17/17. Current candidates remain unqualified and the holdout remains closed.

The user subsequently requested these recommendations become
[Phase 4a](phase-4a.md). That plan maps recommendations 1–19 directly to its fixed
19 completion items, with dependency batches, study limits and acceptance criteria.
This review remains the source rationale; it is not implementation evidence.

The highest-value work is to improve the fidelity and explanatory power of the
research engine, then test a small, economically motivated strategy revision.
Additional operational features cannot establish an investment advantage. Equally,
the present study cannot establish that an accurately executed version of each
signal has no advantage: some execution assumptions differ materially from the
intended paper implementation. None of these observations supports promoting a
current candidate.

The review covers the complete final research report, all 20 trial summaries and
300 test folds; the research, feature, strategy, sizing and reference-simulation
code; the corresponding paper selection/execution, risk, capture, reporting and
maintenance paths; relevant tests, migrations, CI and release documentation. It is
a focused source/evidence review, not an exhaustive security audit of all 19,011
application-source lines or a new actual-data experiment.

**What the results establish**

The [derived CSV](evidence/research-promotion-diagnostics.csv) and
[review evidence](evidence/research-promotion-diagnostics.json) identify the original
13,841,730-byte report by SHA-256. These statistics use only the existing
pre-holdout outcomes. No new candidate or holdout performance was evaluated.

| Candidate | Annual arithmetic net return | Increment vs passive 10% | Mean exposure | Annual fees / fixed opening equity | Completed episodes |
| --- | ---: | ---: | ---: | ---: | ---: |
| BTC daily SMA100 | 1.56% | -4.78 percentage points | 2.96% | 0.67% | 70 |
| ETH daily SMA100 | 2.78% | -4.44 percentage points | 2.92% | 0.49% | 52 |
| ETH daily Donchian55/20 | 3.19% | -4.02 percentage points | 1.96% | 0.16% | 12 |
| BTC four-hour SMA200 | 0.55% | -6.77 percentage points | 3.35% | 2.42% | 231 |
| BTC four-hour momentum60 | -4.17% | -11.49 percentage points | 2.71% | 5.01% | 462 |
| ETH four-hour momentum60 | -2.56% | -10.48 percentage points | 2.54% | 4.30% | 343 |

Returns are annualised arithmetic daily-return estimates, not CAGR or forecasts.
Exposure is weighted by elapsed fold time. Fees are divided by £250 and elapsed
test years; a continuous contribution-funded account needs its own attribution.
All candidates within one asset/timeframe share the same passive comparator.
Different assets/timeframes have different evaluation dates.

Fourteen of 20 point estimates beat cash; zero beat the required passive comparator.
All ten daily variants have positive cash point estimates, while six of ten
four-hour variants are negative. Changing the multiple-testing correction alone
cannot repair the negative passive-comparison point estimates. All simultaneous
lower bounds remain nonpositive, as recorded in the original review.

There are 146 test folds ending with nonzero holdings, including possible dust,
and 27 folds with soft drawdown latches. The next fold starts flat with fresh £250
and cleared research state. This is disclosed, but it does not represent running
one investment account continuously. Episode-only realised P&L is particularly
misleading where material gains remain in open positions at a fold boundary.

The fixed £480/year hurdle requires a 192% incremental annual lower bound at £250
average equity, or 120% at £400. With £250 opening and £250 month-end contributions,
the first-year average monthly opening equity is approximately £1,625 before
returns: the same hurdle is approximately 29.5%. Capital growth changes the
denominator; it cannot turn a negative incremental return into an advantage.

**Ranked recommendations**

The ordering reflects expected value for a trustworthy candidate-promotion
decision, including information gained and implementation effort. It is not a
requirement to implement every item before making the next research decision.
Items 1–6 should inform the next actual-data study; quick CI repairs can proceed
alongside them. Strategy revisions, portfolio selection and statistical-method
changes need a new predeclared study. Preserve the current failed trials.

1. **Replace the full-bar execution delay with a causal event-time model.**
   `simulation/reference.py:102` explicitly waits until bar i+2 opens, and line 215
   consumes a prefix ending one full interval before the execution bar. A signal
   that the paper runner can evaluate at close plus ten seconds is effectively
   delayed by another four hours or day. This is much larger than the specified
   250 ms latency. Use the already retained trade archives to identify prices and
   traded volume after the actual decision/finalisation/command times. Without
   historical books, use an explicitly bounded taker proxy with uncertainty, not
   invented depth or maker fills. Keep the old bar engine as a named pessimistic
   sensitivity. Verify causal prefixes and event ordering against independent
   fixtures before rerunning candidates. Better fidelity can improve or worsen
   results; do not assume a profitable effect.

2. **Run a continuous account across OOS boundaries.**
   `research/runner.py:348` invokes a fresh simulator per validation/test window;
   `simulation/reference.py:171` resets holdings, cash, units and latches. Preserve
   positions, stops, fees, high-water marks, units, reservations and pending exits
   across nonoverlapping test windows. Training/validation may remain isolated
   experiments. An open episode must cross a reporting boundary without becoming
   a fictitious sale, new acquisition or fresh risk budget. Model an operator
   review/rearm only under a rule specified in advance, preserving inception
   history. Keep independent-fold results for comparison. Acceptance: inserting a
   reporting boundary into an otherwise unchanged run must not change economics.

3. **Model the intended contributions and economic frontier.**
   Candidates currently receive no monthly contributions, while the monthly-buy
   comparator receives £250/month (`research/runner.py:132`). The economic gate
   uses the minimum mean equity across isolated £250 folds (`runner.py:410`) and
   hardcodes £240/£480 (`research/economics.py:55`). Add the approved £100–£400
   contribution scenarios to the same continuous ledger for candidate and
   comparator. Make fixed cost an explicit, versioned evidence input, retain the
   twice-fixed-cost requirement, and distinguish experimental local operation
   from the actual deployment budget. Report when each capital/cost scenario could
   satisfy the hurdle *if* a positive incremental lower bound existed. Keep a
   separate statistical-eligibility result so economic failure is intelligible;
   it must still prevent qualifying promotion under the current mandate. This
   prevents substantial work on an economically infeasible configuration.

4. **Separate signal freshness, risk-approval expiry and command latency.**
   `StrategySignal` itself has a two-second lifetime (`strategies/slow.py:38`),
   operational decisions must complete by watermark plus 12 seconds
   (`app/paper_strategy.py:52`), and the reference disables all new entries at
   latency >=2,000 ms (`reference.py:282`). Every one of the 300 latency-stress
   outcomes is therefore cash. Delays between 250 and 1,999 ms have no price-path
   effect in the bar model. Define three explicit clocks: validity of the slow
   signal, the two-second state-bound risk approval before dispatch, and bounded
   command transmission/matching latency. Recheck risk and quotes immediately
   before dispatch. Preserve uncertain-outcome recovery. An expired approval
   cannot be reused, but network latency after authorised transmission is not a
   newly unapproved economic action. Retain a failure/expired-decision stress and
   add a separate true execution-delay stress. Any longer signal-validity rule
   must be versioned and fixed before results are examined.

5. **Correct risk metrics and add complete performance attribution.**
   `research/statistics.py:60` calculates drawdown from daily closing units, which
   loses four-hour drawdowns. A synthetic path with units 1, .90, 1.02, 1.01, 1.03,
   1.04 within one day reports 0% drawdown despite an observed 10% drawdown. Compute
   drawdown from every available observation plus inception, and label its time
   resolution. Preserve daily data for daily statistics. Decompose gross marked
   P&L, fees, spread/slippage, realised/unrealised P&L, exposure, skipped orders,
   halt time and terminal inventory. Skips need reasons rather than one counter.
   Add per-episode adverse/favourable excursions and cost break-even moves.
   Acceptance: independently reconcile cash, holdings, all fees and NAV for each
   fold and the continuous account; partial/open episodes must remain included.

6. **Correct and calibrate protective-exit pricing and chronology.**
   The reference triggers from a candle low, waits for a later open, then always
   sells at the protective *minimum*, even when its model bid is much better
   (`reference.py:220`). An isolated probe buys at 100.1 and records a protective
   sale at 94.1 when the next model bid is 99.9. This is a disclosed pessimistic
   convention, not a realistic reconstruction of the trigger-time fill. Use
   subsequent ordered trades/quotes to distinguish trigger, latency, executable
   limit, gap-through and partial fill. Apply available liquidity after the order
   exists, rather than the entire future bar volume. A stop-limit can remain
   unfilled. Keep the current minimum-price convention as a worst-case bound.
   Kraken describes execution at the limit or better, with gap nonexecution risk:
   [official stop-limit explanation](https://support.kraken.com/hc/articles/9616163984020-stop-loss-limit-orders).

7. **Improve benchmark implementation and add exposure-matched diagnostics.**
   Current average candidate exposure is about 1.7%–3.4%; the passive 10% benchmark
   averages about 8.9%–10.9% over the corresponding fold sets. This reflects the
   risk budget, stops, time out of market and returns. Preserve the mandatory
   cash/10% comparisons. Add past-only risk/exposure-matched controls and a simple
   risk-sized always-long control to determine whether timing contributes value.
   Specify passive deployment/rebalancing rules and bounded unfilled-budget
   handling in advance. `reference.py:320` consumes the whole benchmark budget
   after its first populated-bar attempt, even when it only fills partially or
   fails the minimum. Some early BTC four-hour folds invest £0 or a small fraction
   of the intended £25. Report intended versus achieved allocations and residual
   cash. Do not silently award a full fill or change the mandatory benchmark to
   whichever comparator is easiest to beat.

8. **Investigate a small family of slower, lower-turnover signals.**
   After fixing the evaluation, daily SMA100 on BTC and ETH is a more informative
   starting point than more four-hour momentum combinations: both already have
   >=50 completed episodes, positive cash estimates and lower fees. They are
   research priorities, not selected/promoted winners. Predeclare a small number
   of economically distinct modifications, such as an ATR-normalised hysteresis
   band or a fixed confirmation rule, with a stated mechanism for reducing
   whipsaws and an unchanged protective-risk policy. Define re-entry after a stop
   so a persistent long state does not cause repeated churn without fresh
   evidence. Bound the search and register every attempted variant. Check gross
   edge, costs, missed trends, stability and hold times. ETH daily Donchian's 3.19%
   cash point estimate has only 12 completed episodes; it is not the best-qualified
   candidate. Do not raise exposure/risk or tighten stops to manufacture returns.

9. **Evaluate BTC and ETH as one constrained portfolio.**
   Research currently simulates one instrument per £250 account; the operational
   `PaperSelection` also chooses one symbol (`storage/strategy.py:51`). Add a
   deterministic, initially unoptimised two-asset portfolio under the existing
   shared cash, 30% total/15% per-asset exposure, 0.5% per-episode/1% aggregate
   planned risk and single-writer rules. The strategies must not each receive a
   fictitious independent £250. Resolve simultaneous signals deterministically,
   retain correlated losses, and bootstrap the combined portfolio on common
   dates. A combination can use the intended mandate more fully, but the present
   single-asset results do not prove that a combination will pass.

10. **Use a common, complete pre-holdout evaluation calendar.**
    Fold origins depend on each series' first bar plus 201 bars
    (`research/runner.py:333`); complete six-month windows stop before the holdout.
    The unused tails are 112.83 days for BTC four-hour, 128 days for BTC daily,
    21 days for ETH four-hour and 38 days for ETH daily. Thus 300/300 complete
    eligible folds does not mean every available pre-holdout date was tested.
    Choose common calendar origins after valid warm-up, and specify terminal
    partial-window handling before running the next protocol. Show early
    asset-specific history as supplementary evidence where appropriate. Training
    currently verifies continuity; fixed variants are not fitted or chosen by a
    learned training rule. Describe that accurately. Preserve all eligible losses
    and the fixed 1 October 2025 holdout boundary.

11. **Validate statistical calibration and increase tail precision.**
    Bonferroni uses 0.05/80 = 0.000625; 10,000 bootstrap replicates put the bound
    near the seventh order statistic (`research/statistics.py:155`). That is poor
    Monte Carlo resolution for a decision near zero. Add seed/tail-error
    sensitivity and synthetic null/known-effect experiments, including correlated
    assets, clustered returns and the complete selection procedure. Retain the
    current result as a baseline. A studentised joint max-statistic or reviewed
    Romano–Wolf method can account for correlated tests while controlling the
    declared familywise error; it needs a predeclared method and calibration, not
    a post-result switch to obtain significance. Account for successive research
    families and all searched variants. Preserve seven-/30-day sensitivities and
    test appropriate longer blocks for long holdings. The authors discuss the
    power/control trade-off in [Multiple Testing](https://www.econ.uzh.ch/dam/jcr:e2d4e460-63dd-4908-8502-7357c570c5a0/palgrave_2010.pdf).
    Negative passive point estimates still fail regardless of a less conservative
    correction. This is a validity improvement, not a likely rescue of this run.

12. **Implement enforceable research eligibility and holdout lineage.**
    `_trial` initializes every status to rejected and never updates it
    (`research/runner.py:324`). `freeze()` checks report identity, code and family
    membership but does not reject failed confidence/economic criteria (line 444).
    Add typed per-gate outcomes: failed, incomplete, eligible for freeze,
    holdout passed, approved for observation. Freeze should bind successful
    evidence and a concrete reviewer decision; synthetic workflow fixtures need
    an explicit separate test path. Revalidate eligibility when opening holdout.
    Track the holdout's underlying time range/source lineage under a durable
    study identity, since a dataset hash or copied artefact tree is not a global
    once-only boundary. Keep the existing unopened holdout reserved. Add
    rejection tests for failed economics, insufficient episodes, changed data,
    reused holdout lineage and missing evidence.

13. **Calibrate costs against actual public execution evidence.**
    The UK public schedule still lists 0.40% maker/0.80% taker at the entry tier;
    the current 0.80% taker assumption is supported, not an obvious decimal error:
    [Kraken fee schedule](https://www.kraken.com/gb/features/fee-schedule).
    Account-specific fees remain unresolved. The paper default is passive GTD,
    while research assumes taker execution and the paper venue charges its
    conservative fee bound even on maker-labelled fills. Separate risk fee bounds
    from modelled realised fees. Capture spread, depth, post-signal prices,
    queue/trade evidence, fill probability, adverse selection and latency with
    opt-in operational experiments. Only use maker economics with calibrated
    queue/fill evidence. A maker-fee sensitivity alone is not an executable edge.
    Public capture/shadow observations can improve the model while remaining
    nonqualifying and without placing real orders.

14. **Finish the remaining data and historical-rule assurance.**
    Resolve the disputed June 2026 BTC records through an explicit evidence
    decision, including its effect on the ETH blend comparison. Verify full
    archive checksums where practical and preserve immutable source versions.
    Add point-in-time metadata/fee evidence, or bounded assumptions with a clear
    gate decision where history cannot be obtained. Hashes establish byte
    identity, not completeness or truth. The disputed bars are in the closed
    holdout and do not explain the pre-holdout rejection; prioritise resolution
    when a candidate approaches freeze. Continue keeping no-trade indicator bars
    at zero executable liquidity and unexplained gaps blocked.

15. **Add a separate qualifying-paper study model.**
    Current selections, decisions, reports and supervisor status deliberately
    force operational-only/zero qualifying evidence; the database constraint
    reinforces this (`0013_paper_strategy.py:19`, `storage/strategy.py:61`,
    `app/paper_supervisor.py:76`). This was appropriate for the Phase 4 release,
    but merely running it for eight weeks can never produce a qualifying record.
    Add a new versioned, evidence-bound observation approval and migration path,
    retaining existing operational records exactly as such. Bind candidate,
    dataset, code, policy, costs, start time and allowed account; record durable
    healthy/unhealthy intervals, complete position episodes, faults, fills and
    review decisions. Restart must preserve study time and distinguish elapsed
    calendar time from reliability observations. No flag can retroactively
    qualify existing synthetic/operational activity or grant live capability.

16. **Make the observation operating profile sustainable.**
    The reviewed suffix is 4,096 events/observations and currently requires
    stopped-worker maintenance (`docs/phase-4-bounded-history.md:89`). At one
    observation/second that is about 68 minutes. Maintaining that rate for 56
    days produces 4,838,400 observations and roughly 1,181 suffix windows. A 99.5%
    availability budget averages only about 20 seconds downtime per window;
    faster feeds make it harder. Measure actual input and write rates, reduce
    redundant durable observations only with preserved risk/freshness evidence,
    and implement a reviewed incremental maintenance/storage path with realistic
    headroom. Retain durable financial records, safety vetoes and independent
    rebuild checks. Validate full-market-load reliability, disk growth, backup,
    restart and maintenance on the intended resources. Do this before starting
    the qualifying clock, not by silently raising bounds or dropping evidence.

17. **Repair current CI and retain failure diagnostics.**
    The merged-commit [run 40057130464](https://github.com/JamesChan93/aca-2/actions/runs/40057130464)
    has now completed with failure. All 736 Linux/PostgreSQL tests passed in
    390.99 seconds; the bounded-history compaction job timed out, and dependency
    audit failed on setuptools 84.0.0. Version 84.1.0 patches the reviewed
    [Windows legacy-install advisory](https://github.com/advisories/GHSA-xmj3-9gf7-h52w).
    Update the reviewed development constraint/hash lock and verify compatibility.
    Separate fixture construction from measured operation deadlines; preserve
    partial timings, profiles and safe logs when the 600-second harness timeout
    fires. Distinguish genuine deadline failures from setup/runner variation.
    The un-compacted diagnostic mode also measured four missed operation budgets;
    its successful exit is expressly measurement-only. Keep the bounded acceptance
    check strict. Later package/container scans were skipped after the audit
    failure, so they are not current passes. This is release verification work,
    not evidence of investment returns or grounds to fabricate extra Phase 4 tasks.

18. **Make research evidence modular, durable and easy to reproduce.**
    The final report is 13.84 MB while `research/artifacts.py:34` rejects JSON over
    16 MB. Richer attribution or another family can generate a report that the
    same tool cannot read/freeze/register. Use small typed manifests referencing
    immutable per-trial/per-fold curves and episode files by hash. Persist
    declarations before work, individual results as they finish, deterministic
    resumable checkpoints and complete failed-trial history. Back up the full
    source/research artefact tree; the committed 1 MB summary is not the full
    report and ignored local data is not durable storage. Provide one offline
    command for a review packet with returns, costs, gates and source identities.

19. **Concentrate refactoring and tests on research-to-paper parity.**
    Preserve Decimal financial arithmetic, domain boundaries, immutable journals,
    permanent economic IDs, consumed-command recovery, independent dispatch vetoes
    and the paper-only capability boundary. Avoid a framework rewrite or another
    service. Extract pure reusable episode/risk/performance transitions from the
    duplicated reference and operational paths where useful; retain the
    independent final execution veto. Centralise versioned cost/rule assumptions
    without sharing unvalidated mutable configuration. Add small independent
    fixtures that run the same timed decisions through research and paper and
    explain every expected difference. Test drawdown, fees, holds, stop gaps,
    contribution flows, fold continuity and rejected promotions against independently
    calculated economics. Deterministic tests currently encode some model
    limitations, so a higher test count alone is insufficient. Keep current
    status in one generated gate register, preserving historical reports without
    presenting obsolete limitations as new work.

**How I would use the ranking**

First produce an evaluation-correction increment covering event time, signal/risk
clocks, stop chronology, metrics and complete attribution, with small independent
parity fixtures. Then add continuous OOS accounting, intended contributions and
common-calendar benchmarks. Rerun the original family under a clearly versioned
corrected protocol before deciding whether a small low-turnover revision or a
joint BTC/ETH portfolio is justified. Statistical calibration and research-gate
enforcement should be ready before any freeze. Current CI repair is a small
parallel maintenance task. Qualifying-study and operational-capacity work becomes
useful when credible candidates approach promotion.

At the observed daily SMA100 episode rates, ten completed episodes correspond to
about 391 days for BTC and 492 days for ETH. These are simple historical-rate
extrapolations, not forecasts. Eight weeks is the minimum duration; it does not
promise ten slow-strategy episodes. Donchian daily strategies are slower still.
Do not force extra trades, pool correlated variants as independent evidence or
count operational tests to shorten that requirement.

Keep the six original risk limits, the economic mandate, the holdout boundary and
all failed research evidence. A benchmark, confidence or mandate change can be
proposed explicitly with its trade-offs, but is not an implementation correction.
Avoid ML, more exchanges/assets, leverage, a dashboard and infrastructure expansion
while the existing research questions remain unanswered.

The two synthetic probes ran against the installed 0.4.19 research image with
networking disabled, a read-only root and no repository, credential or database
mounts. No actual-data simulation, candidate freeze, holdout opening, paper study,
deployment or financial operation was performed in this review.
