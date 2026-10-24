# Reviewed-data backtesting and walk-forward validation — 24 October 2026

This is the post-Phase-4 research review requested after PR #27. Phase 4 remains
complete at 17/17. This study cannot start qualifying paper observation or grant
execution permission. The [gap audit](research-data-audit.md) is its data input;
the original Phase 3 results remain preserved as historical evidence.

## Decision and requested steps

**No candidate qualifies for a freeze or holdout evaluation.** All 20 candidates
fail the adjusted incremental-return requirement and the fixed-cost hurdle even
after the gap classifications allow all 300 eligible walk-forward folds to run.
The result is a substantive research rejection, not an inference from a successful
subset of data. G3 does not pass; qualifying paper days/episodes remain zero.

| Requested step | Result |
| --- | --- |
| 1 — Consume reviewed data | Complete; hash-bound import, zero-liquidity carry and quarantine implemented/tested |
| 2 — Backtest and walk forward | Complete; 20 candidates, 300/300 complete folds, 10,000 bootstrap samples |
| 3 — Review qualification | Complete; 17/20 meet 50 episodes, 20/20 have over two years OOS, 0/20 pass confidence/economics |
| 4 — Freeze and evaluate holdout | Not performed: no eligible candidate; BTC data dispute is an additional holdout blocker |

Negative lower bounds mean this study does not establish the required advantage;
they are not forecasts that a strategy will lose those percentages. The table
reports the worse of seven-/30-day **simultaneous 95% lower bounds on annualised
arithmetic incremental return**, after variable costs. The required comparison is
against both cash and a 10% passive allocation. Full-exposure benchmarks have
different risk and do not substitute for either required comparison.

| Candidate | Complete test folds | Completed OOS episodes | Bound vs cash | Bound vs passive 10% |
| --- | ---: | ---: | ---: | ---: |
| BTC/GBP 4h SMA200 | 16 | 231 | −3.75% | −15.78% |
| BTC/GBP 4h SMA100 | 16 | 393 | −7.49% | −18.88% |
| BTC/GBP 4h Donchian55/20 | 16 | 103 | −2.68% | −16.35% |
| BTC/GBP 4h momentum90 | 16 | 401 | −7.26% | −19.90% |
| BTC/GBP 4h momentum60 | 16 | 462 | −8.56% | −20.90% |
| BTC/GBP daily SMA200 | 15 | 50 | −2.98% | −13.62% |
| BTC/GBP daily SMA100 | 15 | 70 | −2.45% | −12.86% |
| BTC/GBP daily Donchian55/20 | 15 | 18 | −1.40% | −14.76% |
| BTC/GBP daily momentum90 | 15 | 83 | −3.01% | −13.77% |
| BTC/GBP daily momentum60 | 15 | 87 | −2.77% | −13.35% |
| ETH/GBP 4h SMA200 | 15 | 198 | −4.87% | −16.02% |
| ETH/GBP 4h SMA100 | 15 | 301 | −6.01% | −18.56% |
| ETH/GBP 4h Donchian55/20 | 15 | 113 | −3.16% | −18.27% |
| ETH/GBP 4h momentum90 | 15 | 346 | −7.16% | −20.20% |
| ETH/GBP 4h momentum60 | 15 | 343 | −7.62% | −20.57% |
| ETH/GBP daily SMA200 | 14 | 32 | −1.63% | −15.42% |
| ETH/GBP daily SMA100 | 14 | 52 | −2.22% | −13.11% |
| ETH/GBP daily Donchian55/20 | 14 | 12 | −0.54% | −13.70% |
| ETH/GBP daily momentum90 | 14 | 55 | −2.12% | −14.32% |
| ETH/GBP daily momentum60 | 14 | 76 | −2.04% | −14.03% |

The OOS samples contain 2,557–2,923 calendar days per candidate. Some descriptive
mean returns beat cash, but every required adjusted lower bound is nonpositive.
The fixed SMA/momentum neighbours likewise fail; no candidate has a qualifying
baseline for stress/regime evidence to support. Both predeclared stresses and
past-only trend-regime breakdowns are retained for every test fold in the evidence.
Higher costs or latency-induced cash cannot be used to select a different winner.

The accounting assumption is £250 at each independent research fold start,
without candidate monthly contributions. The monthly-purchase benchmark receives
its declared separate contributions. Infrastructure is £240/year outside NAV;
the economic gate requires at least £480/year in lower-bound incremental benefit.
No candidate passes that arithmetic, irrespective of the unopened holdout or the
additional source-assurance limitations. No freeze/holdout artifact was created.

## Scope and data handling

Application 0.4.19 adds an explicit `research import --gap-audit AUDIT_JSON` path.
Without that option, raw gaps retain their original unexplained classification.
The option checks the source's audit hash, source-file hashes, coverage, observed
gap boundaries, zero-trade/hourly evidence and populated-candle quarantine. The
audit is copied into content-addressed objects and reverified on every load.
The supported path applies no numerical trade repairs.

The reviewed transformation reconstructs **6,653 indicator-only observations**
across the four series, from the **2,833 individually reviewed gap spans**. Each
uses only an earlier known close for its OHLC, zero volume and zero trade count,
`complete=False`, and the `confirmed_no_trade` flag. A leading interval without
an earlier price stays absent. Unknown gaps are not filled. A disputed price
propagates its quarantine through any subsequent carried observations.

The reference simulator validates those observations separately from populated
candles. They supply no fill capacity for entries or exits. Initial and monthly
benchmark purchase budgets falling in a no-trade interval wait for a populated
interval; the existing participation/minimum rules still apply, and unfilled
residuals are not chased. Raw CSV objects remain byte-for-byte unchanged.
These are historical indicator/valuation conventions, not fresh executable quotes.

The one disputed June 2026 BTC trade range remains represented by two quarantined
research candles (daily and four-hour). A holdout preflight checks both the selected
asset and the blend-comparison asset before writing the one-time opening marker.
No disputed observation is silently dropped, repaired or made eligible.

Review also corrected a pre-existing return-aggregation defect: when adjacent
four-hour folds share a partial UTC day, their subperiod returns are now chained
as `(1+r1)*(1+r2)-1`. Previously the later fold overwrote that day's earlier
return. A regression fixture checks the arithmetic and rejects overlapping
multi-day periods. This does not change signals, folds or trial selection.

## Fixed protocol

- Original 20 candidates: five declared variants, BTC/ETH, daily/four-hour bars.
- Original source coverage, calendar fold origins, 200-bar indicator warm-up and
  201-bar execution prefix. An earlier populated bar is retained when needed to
  verify a carried price at the prefix boundary.
- 24 months training, six validation, six test, advancing six months; flat starts
  remain research assumptions rather than permission to reset operational latches.
- Final holdout remains **1 October 2025–1 October 2026, exclusive at the end**.
  No holdout strategy returns enter this study.
- Unchanged cost/risk policy, GBP initial equity £250, 0.8% taker cost per leg,
  0.1% spread/slippage per leg, 1% participation and protective-limit gap handling.
- Cash, 10%/30% passive, same-asset buy-and-hold, monthly purchases and the
  BTC/ETH blend remain separate comparators with their existing risk differences.
- PCG64 seed 1729, 10,000 paired bootstrap samples, seven-/30-day blocks and
  the existing simultaneous adjustment over 80 comparisons.
- Fee 1.5×/spread 2× and two-second latency stress; fixed SMA and momentum
  neighbours remain sensitivities, not newly searched candidates.

The study retains every eligible fold, including losses, empty intervals and
unfilled protective exits. Fixed costs remain outside trading NAV but enter the
£480 annual incremental-benefit hurdle. No threshold was relaxed for a result.

## Reproduction and evidence

Build the test/research image from this branch with the existing reviewed locks:

```sh
docker build --target test -t aca-foundation-tests:0.4.19 .
```

Run the installed package offline, mounting the retained reviewed bundle read-only
at `/source` and a new writable result directory at `/results`:

```sh
aca research import --source /source/source.json --gap-audit /source/audit.json --root /results
aca research run --dataset /results/datasets/DATASET_HASH/manifest.json --root /results --bootstrap-samples 10000
```

`DATASET_HASH` is returned by import. The research dependencies are intentionally
in the test/research image; the ordinary operational image omits NumPy. No DB,
exchange credential, network access or retained paper account is needed.

Full local reports contain all fold metrics, daily curves, episodes, costs,
stress and bootstrap results. The [committed review evidence](evidence/reviewed-walk-forward.json)
contains every trial/fold, selected metrics, bounds and decisions; the
[trial CSV](evidence/reviewed-walk-forward-trials.csv) is a compact comparison.
Bulky daily curves, episodes and complete metrics remain in the full local report.
Do not treat the ignored local directory as a production backup.

| Evidence identity | SHA-256 |
| --- | --- |
| Gap audit | `46b05154db4743806acde69d5e1d238335e4d7e382d5dfcaac539c99b4f69318` |
| Reviewed dataset manifest | `689acbcc0199e3c0a72b4a0d37f535e7ed95e9c2e15342ecb8aff5ced826ad71` |
| Python source (119 files) | `c167cb66381324ba876b90c0d505a7aaa83e0b67a312db3dee0e4f5df9212f06` |
| Full final report, 13,841,730 bytes | `67a8f9630be6d4f55ad52f2132ba70468f097fca859dbf3d8babc970f8f20790` |

The earlier `data/research/reviewed-study-2026-10-24/` run is retained as preliminary
investigation evidence. Its four-hour partial-day aggregation predates the fix;
it is not the final report or a candidate-selection source. The final run uses
`data/research/reviewed-study-final-2026-10-24/` and the installed 0.4.19 package.

## Limits and later decisions

The audit corroborates publisher-recorded no-trade intervals, not independent
proof of complete exchange history. Selected member CRCs/local SHA-256 are
verified; full multi-gigabyte archive hashes are not. Historical fee tiers,
instrument rules and account eligibility remain reconstructed/unverified.
Bar-only taker fills and fold resets remain labelled model assumptions.

Two-second latency exhausts the existing entry permission and can produce cash;
it is not evidence that a strategy earns returns during a two-second delay.
Stress, parameter and regime descriptions cannot override a failed baseline
confidence bound or economic gate. An unopened holdout is not a missing reason
to promote a rejected candidate.

No new strategy family, parameter search, candidate freeze, paper selection,
observation start, deployment, notification destination or live capability is
introduced by this review. Any later research proposal must preserve the failed
trials and reserve the existing holdout until the required prerequisites hold.

## Verification

The final package builds from the existing hash-locked dependencies. **736 tests
pass in 180.04 seconds** in the disposable Linux/PostgreSQL stack, including the
existing financial/lifecycle properties and 20 new cases. A targeted research run
passes 74 tests; strict mypy passes across 158 files, and Ruff/format checks pass.
Tests cover audit/source tampering, exact gap correspondence, leading/trailing
carry, quarantine propagation, zero entry/exit liquidity, deferred benchmark
purchase, unsafe no-trade payloads, holdout preflight and partial-day chaining.

The final actual-data run uses the installed 0.4.19 package, CPython 3.14.8,
NumPy 2.5.4, disabled networking, a read-only root and source mount, and a separate
writable research directory. It is not a production deployment or live execution
test. The retained app/database container identities and existing images remain
unchanged; no retained schema migration is performed.
