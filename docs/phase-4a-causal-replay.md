# Phase 4a Batch B — causal execution, clocks and metrics

25 October 2026. Application **0.4.22**, schema unchanged at
`0016_history_segments`. Base: merged PR #31, `f0fc2b6e406b3bca7ac171b12445ce654326b610`.
This delivers **P4A-01, P4A-04, P4A-05 and P4A-06** from the
[fixed 19-item plan](phase-4a.md), and extends the independent P4A-19 fixtures.
Phase 4a is **6/19**; P4A-19 remains open for Batch C's account/calendar parity.
The [evidence packet](evidence/phase-4a-causal-replay.json) binds the source and checks.

This is execution-model engineering verified with synthetic inputs. **No corrected
S0/S1/S2 study ran, no actual holdout opened, and qualifying paper remains zero
days/episodes.** All 20 original candidates remain rejected. The retained local
stack was not upgraded, migrated or restarted. No Kraken write capability exists.

## P4A-04: three clocks, with legacy interpretation preserved

[A26](architecture.md#a26--research-timing-cost-and-holdout-boundary) was amended
before adopting the new semantics. `domain/timing.py` defines `three-clocks-v1`:

| Clock | Exact rule | Evidence |
| --- | --- | --- |
| Finalisation and slow-signal freshness | Closed candle + 10 seconds; fixed expiry a further 60 seconds later. Late evaluation cannot renew it. | Boundary tests at +10, +69.999 and +70 seconds; consumed-watermark deduplication. |
| Entry risk approval | Fresh state-bound approval for two seconds from admission; equality expires. Approval cannot be renewed by extending the signal. | 1,999 ms dispatch succeeds; 2,000/2,500 ms dispatch fails. Intervening state changes fail the independent veto. |
| Transmission and matching | Five-second transmission deadline; matching latency 250–60,000 ms after dispatch. IOC/FOK matching before the fixed 60-second order deadline. | Successful dispatch then 2/5/59.999-second matching delay keeps the same command ID; 60-second nonexecution is separately reason-coded. |

The durable paper runner creates `slow-v2` signals for new CLI selections.
Admission still reserves exact economics and the independent dispatch check still
checks state, quote/account freshness, rules, clock, funding, exposure and loss.
Once consumed, the command does not regain dispatch permission when its approval
expires. Mutating-command uncertainty/recovery semantics are unchanged.

`OrderIntent.timing_version` is additive and omitted when absent. Existing
`slow-v1` selections, signals and intents retain their original expiry and hash
interpretation. Existing accounts are not silently changed to a new selection.
New entry and strategy-exit intents carry the matching-clock version; this avoids
retroactively cancelling an accepted exit because its pre-send deadline elapsed.
The PostgreSQL fixture admits a fresh slow signal at +3 s, dispatches before its
new approval expires, and matches both its entry and later exit after 2 s latency.

## P4A-01: ordered trade execution

`simulation/events.py` provides incremental `TradeSimulation` and the explicit
`simulate_trades` wrapper. The wrapper merges closed-candle finalisation timers
with receipt-ordered canonical `MarketTrade` inputs. A 200-bar past-only warm-up
is required. Unknown gaps and quarantined observations are rejected. Confirmed
no-trade candles carry the previous close for indicators and supply no fill volume.
Archive retrieval time remains provenance; historical indicator availability is
the disclosed end + 10-second assumption.

An initial signal can dispatch and match **inside the next bar**, without waiting
for its close or using that bar's future OHLC/volume. The synthetic fixture fills
at close + 10.250 s. Arbitrarily changing future bars and their volumes leaves the
entire earlier result unchanged. Appending future prints preserves earlier signals,
approvals, command identities, observations and fills.

The trade-only execution model is `ordered-trade-taker-v1`. Its explicit assumptions:

- A received print supplies a conservative bid/ask **price proxy**, using the
  declared adverse spread/slippage rate and tick rounding. It supplies no book.
- Sizing uses a current/past admissible print. A later print can fill at most 1%
  of **its own** quantity, rounded down. It cannot supply an earlier fill or size.
- IOC entries wait matching latency and cancel unused residuals. An unprotectable
  small entry is not filled. There is no chase, maker award or passive queue fiction.
- Both receipt and exchange-event times must be after the matching boundary.
  Stale/future trades and regressing clocks fail explicitly.
- Duplicate IDs consume no liquidity or money twice; distinct trades with equal
  timestamps remain distinct. Conflicting IDs fail. Financial event IDs and
  research approval IDs are deterministic, with account identity bound to the
  model, clocks, capital, rules, costs and policy.

These are bounded model assumptions, **not calibration evidence**. P4A-13/P4A-14
still own cost/data assurance; Batch C owns intended-account/comparator integration.
The existing research CLI and modular runner continue to label the legacy bar
execution model. They do not silently become corrected S0 or gain holdout authority.

## P4A-06: protection from chronological evidence

The entry retains its approved trigger and minimum sale price. A newly created
protective order waits latency, triggers from an admissible bid proxy, then waits
latency again. Triggering never fills on the same event. Later fills receive the
limit **or better**, respect that event's participation budget and cannot oversell.
Partial residuals retain their original episode and protective bounds. Gaps can
leave the entire position unfilled.

Both PostgreSQL paper and trade research now pass the same literal
[`tests/fixtures/parity/v1.json`](../tests/fixtures/parity/v1.json) oracle:

| Path | Exact outcome |
| --- | --- |
| Recovered bid | Trigger at 500 ms; no fill at 749 ms; sell 0.01 at £960 at 750 ms. Final cash £999.4432; fees £0.1568. |
| Rapid gap | Bid £800 after trigger; no sale below £941.40. Inventory 0.01 remains protected. |
| No executable liquidity | Positive observation quantity rounds to zero after the cap. No invented fill. |
| Partial residual | Sell 0.004 at £960; retain 0.006 through the gap; sell it at £970. Final cash £999.50272; fees £0.15728. |

The old minimum-price convention remains in `legacy-flat-window-bar-taker-v2` as
the named pessimistic sensitivity; its fixture still produces £999.24976 on its
declared path. It is not used as the new event model's realised exit price.

A separate fixture cancels protection, sells part of a confirmed strategy exit,
and restores protection only for the confirmed remaining quantity. In research
this is a deterministic cancellation timeline. Real cancellation uncertainty,
missing protection and recovery remain covered independently by paper fault tests.
Hard-halt fixtures retain protective execution and block further entries.
Soft/daily entry pauses still permit bounded strategy reductions. A hard halt
requires review for strategy exits, while already-held protection continues.

## P4A-05: all observations and reconciled attribution

`research.statistics.metrics` now calculates drawdown from inception and **all
available observations**. The independent 900 → 1,000 intraday recovery fixture
reports 10% drawdown; the original daily-only metric returned zero. Legacy bars
still disclose bar-close resolution; event reports disclose received-trade and
pre/post-fill resolution. Neither claims an unobserved intragap path.

Daily statistics continue to use daily sampling. Event midnight observations
belong to their actual UTC day; legacy interval closes retain their half-open
interval convention. Undefined annualised statistics stay unavailable. The event
report discloses last-available daily sampling and maximum observation gap; this
does not replace the later common-calendar/data-assurance gate.

`event_metrics` includes reason-coded skips, observed/requested horizons, unresolved
open inventory/episodes, completed episodes, actual modelled GBP fees, triggered
but unfilled stop observations and last-known-state duration measures for exposure,
equity and halts. It flags stale terminal inventory marks rather than refreshing
them or liquidating inventory. Duration estimates do not invent price observations
or demonstrate healthy observation coverage.

Two independently checked attribution identities are reported:

```text
NAV = opening NAV + external flows + gross frictionless marked P&L
      - actual modelled fees - spread/slippage/tick costs
      - terminal mark discount + explicit functional-currency rounding

NAV = opening NAV + external flows + journal realised trading P&L
      + remaining inventory's unrealised marked P&L - actual modelled fees
```

Fills use the same GBP journal and balance transitions as paper. Actual modelled
fees are separate from conservative risk fee bounds. Explicit cash flows use the
shared unitisation function and preserve latches; no schedule is assumed. A literal
hard-halt example starts at NAV £990.92 and unit price 0.99092: £99.092 issues 100
units, then a £49.546 withdrawal redeems 50. NAV becomes £1,040.466 with 1,050 units;
the unit price, epoch and hard latch remain unchanged. Flow IDs are idempotent.
Month-end scenarios and comparable benchmark flows remain Batch C's named work.

## Verification and remaining differences

The release regression retains the existing 1,000-example financial properties and
10,000 lifecycle/fault sequences. New generated checks add 1,000 deterministic
examples reconciling journals, attribution, owned inventory and fees across price,
spread and partial-liquidity boundaries. Independent commitment checks are shared
with paper; paper's separate market/metadata validation remains intact.

Run the integrated release checks with:

```sh
python3 scripts/ci_database.py --linux
ruff check src tests scripts
ruff format --check src tests scripts
mypy
```

Final local verification: **803 Linux/PostgreSQL tests pass in 177.69 seconds**;
strict mypy passes 166 files and Ruff check/format passes 193 files. The installed
test image has the source fingerprint recorded in the evidence packet. The exact
PR head must also pass hosted packaging, audits, capacity, restore, container and
image checks; links belong to the PR. Previous release checks are not transferred.

Remaining expected differences under **existing** P4A-19: trade proxies versus
validated paper depth/queues; deterministic research versus durable paper command
uncertainty; reconstructed research rules versus refreshed paper metadata; modelled
GBP fees versus authoritative imported fee assets; explicit control/flow inputs
versus stored operational records. Batch C must carry the complete account across
OOS reporting windows, align comparators and calendar, and extend the same fixtures.
This increment does not close those items or start actual-data selection.
