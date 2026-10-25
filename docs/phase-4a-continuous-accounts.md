# Phase 4a Batch C — continuous accounts and coherent comparison

Application **0.4.23**, schema `0016_history_segments`, 25 October 2026.
This implements the existing P4A-02/03/07/10/13/14/19 acceptance items. Together
with Batches A/B, Phase 4a advances from **6/19 to 13/19**. Phase 4 remains 17/17.
The [fixed checklist](phase-4a.md) still has 19 items and the same research budget.

The review concludes **data assurance blocked/no-go for qualification**. Engineering
can finish a bounded audit without making its missing facts verified. This release
does not run corrected S0/S1/S2, open the actual holdout, freeze a candidate, select
a paper strategy, start observation, change the retained deployment or add a live
writer. All 20 original candidates remain rejected; qualifying days/episodes are zero.

## P4A-02: continuous accounts

`research.continuous.ContinuousEvaluation` merges closed-signal timers with an
incremental receipt-ordered trade iterator. `advance(until)` consumes events strictly
before the boundary and returns an immutable cumulative account snapshot. `run`
adds contiguous reporting cuts to the same evaluator. No cut reissues capital,
liquidates inventory, clears fees, replaces protection, creates a new episode or
resets high-water marks/latches. Received-event duplicates retain their permanent IDs.
Pending consumed commands survive a split; deterministic matching deadlines expire
even when no subsequent print arrives, releasing only the unfilled remainder.

The same inputs with one report or cuts during submission, an open position,
stop triggering, partial protection, a hard halt and a contribution produce equal
final account results. A reporting snapshot cannot change prior decisions/fills.
Duration integrands use exact stored Decimal fractions so splitting a duration
cannot introduce arithmetic differences. The 0.4.22 stop paths and clock semantics
remain covered by the original independent fixtures.

Training, validation and OOS runs have distinct account namespaces. They cannot
share an opening ledger or qualify by concatenating separately reset accounts.
This is fixed-rule rolling evaluation: the strategy parameters are not fitted in
the nominal training window. Statistical selection and the study artefact workflow
remain Batch D; the legacy CLI is not silently switched to corrected evaluation.

An epoch review is an explicit input with an ID, timestamp, substantive reason,
evidence hash and expected prior epoch. The engine records the prior state hash
and deterministic new epoch. Conflicting/replayed changes fail; a duplicate exact
review is inert. The inception high-water and daily suspension survive a review,
as in paper. There is no automatic restart decision at a fold, month or deposit.
These offline review inputs confer no operational/live authority.

Missing pre-flow valuations or other invalid inputs fail the whole comparison.
The evaluator then refuses further reports; rerun cleanly from immutable inputs.
Partially applied internal states cannot be reported as completed comparable cases.

## P4A-03: contributions and economics

`scenario_grid()` declares the nine £100/£250/£400 opening-capital × monthly-flow
cases. The opening amount is independent of the contribution amount. These are
capital arithmetic cases, not additional strategy definitions or selection winners.
Zero monthly contributions are also supported as an explicit diagnostic case.

Month-end credit occurs at the next UTC month's 00:00 boundary; its ID names the
closing month. A credit exactly at the exclusive terminal boundary belongs to the
following interval. Every candidate/comparator receives identical IDs, amounts
and dates. A missing held-asset valuation cannot postpone the credit to a more
favourable price. Shared `flow_units`, GBP journals and quantity movements keep
investment flows outside trading P&L and preserve the unit-price high-water state.

The new independent [literal oracle](../tests/fixtures/parity/continuous-v1.json)
starts with £1,000, buys 0.01 at £1,000 and pays £0.08. NAV is £999.92 and unit
price 0.99992. A £99.992 contribution issues exactly 100 units: cash £1,089.912,
NAV £1,099.912, units 1,100, unchanged unit price. Both PostgreSQL paper and the
trade engine check these values independently. The passive comparator's separate
second 0.001 purchase costs £1.008, leaving cash £1,088.904 and NAV £1,099.904.

`economic_assessment` and `economic_frontier` report supplied statistical eligibility
separately from the arithmetic hurdle, with versioned planned/actual fixed-cost
inputs. Default infrastructure is £20/month outside trading NAV; household wealth
subtracts its elapsed-time attribution. Tax is explicitly not computed here. The
two-times annual-fixed-cost hurdle remains unchanged. A supplied 5% lower bound
would require £9,600 average equity at £240 annual fixed costs; this function does
not establish that bound. Missing/failed statistical evidence cannot become economic
eligibility. No arithmetic feeds sizing or changes the fixed risk policy.

## P4A-07: comparable benchmarks

All accounts use the same opening time, terminal time, external flows, adverse tick
rounding, taker fee assumptions, 250 ms default latency, 60-second matching/budget
deadline and at-most-1% individual-print participation. Passive orders consume a
bounded budget at the next admissible print, then wait latency and attempt once;
an unfilled residual stays cash. No next print, maker fill, repeated chase or
terminal sale is invented. Each account independently competes against the same
historical market, rather than consuming another hypothetical account's liquidity.

The report preserves cash, same-asset passive 10%/30%, same-asset buy-and-hold,
monthly purchases, BTC buy-and-hold, ETH buy-and-hold and the funded 50/50 blend.
Each passive account spends its stated allocation of opening/contribution cash,
then holds. No periodic sale rebalances market drift. The blend has one cash ledger
and two asset budgets; it does not receive two copies of investor capital. Under
this equal-flow convention full-allocation monthly purchases and buy-and-hold
deliberately have equal economics. Their historic unequal-flow result is preserved
only in the labelled legacy study.

The passive mandate has no trading strategy stops or portfolio-risk halts. Full
exposure and drifting allocations therefore have different risk from the managed
candidate. Reports disclose intended budgets, achieved spend, unspent cash, actual
exposure, inventory by asset, fees and two-source NAV attribution; quantities of
different assets are not added into a displayed position quantity.

Two monthly **diagnostics** use only already observed candidate durations: a
past-exposure target and a past-planned-risk target converted with a conservative
current stop/fee allowance. They start in cash, require fresh evidence and make
bounded buy-only adjustments capped at the existing 15% asset limit. If target
exposure falls, an existing holding may exceed it; no reduction/rebalancing is
pretended. A separate risk-sized always-long diagnostic uses the candidate's ATR,
sizing, protection and risk rules at each new closed-bar decision. It cannot
re-enter within an already consumed decision. None replaces mandatory cash or
passive-10% comparisons, becomes a selected variant, or bypasses a hard halt.

## P4A-10: complete calendar

The [source-bound review](evidence/phase-4a-source-assurance.json) fixes the common
origin at **1 March 2016**, after both assets have 200 daily warm-up intervals.
Twenty-four training months plus six validation months produce the first OOS date
**1 September 2018**. All assets/intervals share OOS through **1 October 2025**,
exclusive: 14 full six-month reporting windows and a final 1 September–1 October
2025 partial window. Previous per-series unused tails are listed explicitly in
the review and included in this complete range. Earlier history is labelled
supplementary; it cannot silently replace the mandatory common comparison.

Declared coverage is not continuity evidence. `qualified_bars` verifies the actual
past-only prefix, exact evaluation boundaries, every adjacent interval, no-trade
carry and quarantine. Confirmed no-trade bars remain indicator-only with zero
liquidity. Unknown gaps and disputed prices fail closed. Later holdout contents
cannot affect the selected ordinary prefix.

The report has every requested UTC daily interval. A held-asset endpoint without
evidence within five seconds is null; neither forward filling nor deleting days
constructs a paired sample. Partial first/last days are labelled. Fresh prior
evidence can supply an exact midnight timer mark without restamping its source;
otherwise the shared missing-midnight daily latch applies. Calendar alignment does
not waive coverage. Batch D must enforce statistical eligibility on this evidence.
The actual holdout remains **1 October 2025–1 October 2026**, end exclusive, unopened.

## P4A-13: public cost evidence

The [immutable packet](evidence/phase-4a-public-costs.json) measures the first 5,000
valid retained public book receipts ordered by receipt time/ID. A read-only database
session exported original payloads, hashes and normalized books; no collector was
started. The offline reader recomputes every payload hash and top-10 checksum,
checks raw/normalized symbol/time/epoch bindings, and rejects conflicting evidence.

| Retained sample | BTC/GBP | ETH/GBP |
| --- | ---: | ---: |
| Books | 1,442 | 3,558 |
| Median spread | 1.13 bps | 2.38 bps |
| 95th-percentile spread | 3.91 bps | 11.10 bps |
| Maximum spread | 7.17 bps | 22.40 bps |
| Median event-to-receipt delay | 28.34 ms | 21.22 ms |

These are a short convenience sample, not historical cost calibration. Propagation
delay includes clock error and is not order latency. One-/five-second hypothetical
taker markouts are reported with same-epoch/time-tolerance counts, not actual fills
or maker adverse selection. Queue priority and maker fills remain unqualified.

Conservative sizing uses at least 0.8% fees per leg; modelled realised taker fees
are separate (base 0.8%, stress 1.2%). Planning spread/slippage is 10 bps per leg,
stress 20 bps; default latency is 250 ms, stress two seconds. The sample maximum
ETH spread exceeds the base round-trip allowance: that base is an assumption,
not a uniform empirical upper bound. Historical conditions can exceed the stress
too. The packet retains missing command-latency, queue/maker and private fee
evidence explicitly. No public observation establishes authenticated account fees.

## P4A-14: data-assurance decision

The [source packet](evidence/phase-4a-source-assurance.json) records the actual source,
audit, original/reviewed dataset and every retained input hash. The offline review
rehashed **25 inputs / 628,495,464 bytes**, reloaded the immutable reviewed dataset,
checked source lineage and rebuilt continuity/quarantine projections. Quarterly
agreements and untouched raw-source/correction status remain recorded.

Selected-member CRCs and local SHA-256 establish local integrity. The full exchange
archive hash was not verified; no multi-gigabyte download or replacement receipt
was fabricated. The CLI can rehash a supplied full archive against the original
manifest's bound published hash, but the actual review used none.

The existing conflicting BTC IDs **7098364–7098366** remain unresolved. Both affected
holdout candles stay quarantined, including ETH's required BTC blend comparison.
Historical fee tiers, instrument rules and archived receipt latency remain
reconstructed/unavailable. Same-publisher candles/trades/REST corroboration is not
independent proof of exchange completeness. A bounded retained-evidence review
finishes with **BLOCKED_NO_GO** and named qualification blockers. It does not turn
these missing facts into passes or alter prices to promote a result.

Reproduce both offline reviews without network, exchange secrets or private reads:

```sh
aca research assurance --source SOURCE_JSON --gap-audit AUDIT_JSON \
  --raw-directory RETAINED_AUDIT_INPUTS --dataset REVIEWED_MANIFEST \
  --public-books READ_ONLY_BOOK_EXPORT_JSONL --original-manifest ORIGINAL_MANIFEST \
  --root NEW_RESULTS_DIRECTORY
```

It emits content-addressed review paths. The ignored local book export and inputs
are not a production/off-host backup. Public raw market files are not committed.

## P4A-19: integrated parity

The existing [stop/timing oracle](../tests/fixtures/parity/v1.json) is preserved.
The new independent [flow/calendar oracle](../tests/fixtures/parity/continuous-v1.json)
extends the same verification family. Tests cover equal-flow journals/units,
complete common dates, all nine arithmetic cases, report-split equivalence,
training isolation, pending/partial/open episodes, hard-halt protection, midnight
evidence, manual epochs, residual cash, prefix invariance and unavailable facts.
PostgreSQL paper independently checks the literal contribution economics.

Expected differences remain explicit: retained trade proxies versus validated paper
depth/queues; deterministic research cancellation/timers versus durable paper
uncertainty/recovery; reconstructed versus refreshed metadata; modelled GBP fees
versus imported actual fee assets; passive benchmark mandate versus managed risk;
offline reviewed inputs versus audited operational administration. Shared journals,
unitisation, indicators, sizing and portfolio/commitment vetoes retain their controls.
No framework rewrite, strategy sandbox or new service is introduced.

The [integrated evidence register](evidence/phase-4a-continuous-accounts.json) binds
source/fixture/packet identities and current local verification. Hosted results
must pass on the PR head; prior release success does not approve this release.
The existing 1,000-example critical financial properties and 10,000 lifecycle/fault
sequences remain, plus 1,000 generated passive journal/attribution boundaries.
Current build, full PostgreSQL, format/type/audit/container results belong to the PR.

Final local verification: **843 Linux/PostgreSQL tests pass in 167.00 seconds**;
strict mypy passes 172 files and Ruff check/format passes 199 files. The installed
wheel and repository source fingerprints agree. The exact PR commit still requires
its own hosted packaging, dependency audit, restore, capacity and image checks.
