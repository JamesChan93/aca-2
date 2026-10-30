# Research rerun and performance metrics — 30 October 2026

**All 300 original walk-forward folds completed again. None of the 20 candidates qualifies for promotion.** Seventeen meet the 50-completed-episode minimum; zero pass the simultaneous confidence-bound or fixed-cost gate. The holdout remains unopened.

This rerun reproduces the original, independently reset, bar-based study requested by the owner. It uses application 0.4.26 from merged PR #36, the same reviewed historical dataset, 10,000 paired bootstrap samples, and unchanged costs, policy, windows and holdout dates. All candidate daily returns and bootstrap results exactly match the [24 October study](research-reviewed-validation.md). No new market history was acquired.

The corrected Phase 4a continuous-account study was also rechecked: all 20 S0 decisions are **incomplete/no-run** because its source, cost and review prerequisites remain unsatisfied. The legacy metrics below are descriptive research diagnostics. They do not replace corrected evaluation, resolve the missing evidence or authorise paper observation.

## Requested steps

| Step | Result |
| --- | --- |
| 1. Integrate reviewed data | Reimported and hash-checked. Same dataset; 6,653 indicator-only zero-liquidity observations and two quarantined BTC candles. Raw CSVs unchanged. |
| 2. Backtest and walk forward | 20 original candidates, 300/300 complete test folds. Independently replayed all 300 baseline folds and all six comparators; every metric and stored candidate/cash/passive10 daily return matched the new report. |
| 3. Review qualification | 17/20 meet 50 episodes; 0/20 pass adjusted confidence or economics. Corrected S0 separately records 20 incomplete/no-run decisions. |
| 4. Freeze and evaluate holdout | Not performed: no eligible candidate and unresolved source/review blockers. The protected interval remains 1 October 2025 to 1 October 2026, end exclusive. |

## Combined out-of-sample metrics

Net returns include modelled trading fees and spread/slippage. They exclude tax and infrastructure, which is funded outside trading NAV. Total return geometrically chains the independent test-fold returns; it is **not the return of a continuously operated account**. Each original fold starts flat with £250 and resets strategy/risk state. No closing sale is invented at its end. Open positions enter marked NAV; profit factor and win rate cover completed episodes only.

| Asset | Bars | Strategy | Total return | CAGR | Max drawdown | Sortino | Calmar | Sharpe | Episodes |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BTC/GBP | 4h | sma200 | +3.97% | +0.49% | 8.50% | 0.24 | 0.06 | 0.15 | 231 |
| BTC/GBP | 4h | sma100 | -21.98% | -3.05% | 26.45% | -1.20 | -0.12 | -0.80 | 393 |
| BTC/GBP | 4h | donchian55-20 | +4.31% | +0.53% | 9.43% | 0.31 | 0.06 | 0.21 | 103 |
| BTC/GBP | 4h | momentum90 | -21.38% | -2.96% | 24.15% | -1.29 | -0.12 | -0.89 | 401 |
| BTC/GBP | 4h | momentum60 | -28.66% | -4.13% | 29.01% | -1.88 | -0.14 | -1.33 | 462 |
| BTC/GBP | Daily | sma200 | +6.73% | +0.87% | 7.33% | 0.44 | 0.12 | 0.30 | 50 |
| BTC/GBP | Daily | sma100 | +12.02% | +1.53% | 7.88% | 0.76 | 0.19 | 0.51 | 70 |
| BTC/GBP | Daily | donchian55-20 | +9.76% | +1.25% | 2.92% | 0.87 | 0.43 | 0.58 | 18 |
| BTC/GBP | Daily | momentum90 | +5.46% | +0.71% | 9.28% | 0.35 | 0.08 | 0.25 | 83 |
| BTC/GBP | Daily | momentum60 | +9.24% | +1.19% | 8.11% | 0.56 | 0.15 | 0.39 | 87 |
| ETH/GBP | 4h | sma200 | +4.25% | +0.56% | 17.38% | 0.22 | 0.03 | 0.15 | 198 |
| ETH/GBP | 4h | sma100 | -9.54% | -1.33% | 19.37% | -0.48 | -0.07 | -0.33 | 301 |
| ETH/GBP | 4h | donchian55-20 | +2.65% | +0.35% | 6.69% | 0.21 | 0.05 | 0.14 | 113 |
| ETH/GBP | 4h | momentum90 | -16.84% | -2.43% | 22.77% | -0.94 | -0.11 | -0.67 | 346 |
| ETH/GBP | 4h | momentum60 | -17.85% | -2.59% | 23.33% | -1.08 | -0.11 | -0.77 | 343 |
| ETH/GBP | Daily | sma200 | +14.33% | +1.93% | 3.76% | 0.97 | 0.51 | 0.65 | 32 |
| ETH/GBP | Daily | sma100 | +20.73% | +2.73% | 5.80% | 0.99 | 0.47 | 0.67 | 52 |
| ETH/GBP | Daily | donchian55-20 | +24.52% | +3.18% | 3.34% | 1.42 | 0.95 | 0.94 | 12 |
| ETH/GBP | Daily | momentum90 | +14.42% | +1.94% | 5.75% | 0.82 | 0.34 | 0.55 | 55 |
| ETH/GBP | Daily | momentum60 | +18.16% | +2.41% | 4.37% | 0.99 | 0.55 | 0.67 | 76 |

All 20 definitions are shown in their declared order. This table is not a new ranking, parameter search or candidate freeze.

| Series group | First OOS start (UTC) | Last OOS end, exclusive (UTC) | Folds per candidate |
| --- | --- | --- | ---: |
| BTC/GBP:240 | 2017-06-10T04:00:00+00:00 | 2025-06-10T04:00:00+00:00 | 16 |
| BTC/GBP:1440 | 2017-11-26T00:00:00+00:00 | 2025-05-26T00:00:00+00:00 | 15 |
| ETH/GBP:240 | 2018-03-10T00:00:00+00:00 | 2025-09-10T00:00:00+00:00 | 15 |
| ETH/GBP:1440 | 2018-08-24T00:00:00+00:00 | 2025-08-24T00:00:00+00:00 | 14 |

The legacy groups have different evaluation calendars and leave different pre-holdout tails unused. Cross-group point estimates are not like-for-like performance rankings. The corrected workflow requires the common calendar and continuous accounts before qualification.

## Benchmark comparison

The following predeclared SMA100 examples show why positive strategy returns alone do not satisfy the passive comparison. Complete benchmark metrics for all 20 definitions are in the linked CSV.

| Candidate | Strategy total return | Passive 10% total return | Strategy CAGR | Passive 10% CAGR | Strategy max drawdown | Passive 10% max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BTC/GBP:240:sma100 | -21.98% | +72.72% | -3.05% | +7.07% | 26.45% | 15.99% |
| BTC/GBP:1440:sma100 | +12.02% | +54.61% | +1.53% | +5.99% | 7.88% | 15.95% |
| ETH/GBP:240:sma100 | -9.54% | +75.14% | -1.33% | +7.75% | 19.37% | 12.48% |
| ETH/GBP:1440:sma100 | +20.73% | +59.36% | +2.73% | +6.88% | 5.80% | 12.56% |

Cash returns 0% under the declared zero-yield assumption. Passive 10% also resets its allocation with each original fold and drifts within it; the figures describe that legacy convention. Passive 30%, full buy-and-hold, 50/50 blend and monthly-purchase metrics are retained as separate comparators with different exposure. The original monthly-purchase comparator receives additional funding; it is not an equal-flow candidate comparison.

ETH daily Donchian has the highest displayed legacy cumulative return (+24.52%), Sortino (1.42) and Calmar (0.95), but only 12 completed episodes. BTC daily Donchian has 18 and ETH daily SMA200 has 32. These fail the minimum independently of their confidence and economic failures. ETH daily SMA100 meets the episode minimum (52), yet its +20.73% cumulative return trails the same-window passive10 result of +59.36%.

## Definitions and interpretation

- **Total return:** product of `(1 + daily return)` minus one, with adjacent partial UTC fold days chained. It includes marked open positions and modelled variable costs.
- **CAGR:** terminal chained unit value raised to `365.25 / elapsed calendar days`, minus one.
- **Maximum drawdown:** largest decline from the running peak, including the initial unit and every available bar-close observation across fold boundaries. There is no claim to capture unobserved within-bar troughs or executable prices.
- **Sortino:** annualised arithmetic mean daily return divided by annualised all-day downside RMS, with a zero minimum acceptable return. Zero-return days remain in the denominator sample.
- **Sharpe:** annualised arithmetic mean daily return divided by annualised sample daily volatility, with zero cash yield.
- **Calmar:** CAGR divided by maximum drawdown. Ratios with zero denominators are undefined, not infinite.
- **Additional CSV fields:** annualised volatility, 95% daily expected shortfall, mean bar exposure, profit factor, win rate, turnover, fees, completed episodes, OOS dates and adjusted incremental-return lower bounds. Fold CSVs retain TWR, MWR/XIRR, ending NAV, open quantity, skips and unfilled stop bars.
- **Costs:** 0.8% taker fee and 0.1% spread/slippage per leg; unchanged sizing fees, protective limits, participation and risk limits. The original run also repeats the declared fee/spread and two-second latency stresses in the full report.
- **Economics:** £240/year infrastructure remains outside NAV; qualification requires at least £480/year in conservative lower-bound incremental benefit. No candidate passes.
- **Fold accounting:** displayed fee/turnover totals add the independent £250 fold accounts; they are not fees or turnover of the geometrically chained diagnostic portfolio. Profit factor uses completed episodes and can be below one while total return includes gains on open positions.
- **Risk thresholds:** latches reset with the original independent fold accounts. Aggregate descriptive drawdown can exceed a per-epoch threshold. Thresholds and stop limits are not guaranteed loss bounds.

## Data assurance and holdout decision

The independent assurance rerun rehashed 25 retained public inputs / 628,495,464 bytes and all 5,000 retained public-book receipts. It reproduced the same source and cost review identities. Four reviewed source CSV hashes were checked again after evaluation. No raw source changed and no numerical candle repair was applied.

The full archive hash remains unverified; BTC IDs 7098364–7098366 remain disputed, affecting the BTC holdout and ETH blend comparator. Historical fees/rules are reconstructed, receipt latency is unavailable, and qualified source/cost/parity/history-reuse/stability reviews are absent. These are recorded missing evidence, not zero historical data. No evidence status was overridden to run the corrected model.

The legacy multiple-comparison protocol remains its original 80 comparisons; the current corrected 26-definition family/104-claim protocol remains unchanged and unevaluated. Reusing the same market history is not independent confirmation. No strategy, paper-study approval, holdout opening, deployment, retained database migration, notification or live command was performed. Qualifying paper days and episodes remain zero.

## Evidence and reproduction

- [Machine-readable evidence and all candidate/benchmark summaries](evidence/research-rerun-2026-10-30.json).
- [20-candidate CSV](evidence/research-rerun-2026-10-30-trials.csv).
- [120 benchmark summaries](evidence/research-rerun-2026-10-30-benchmarks.csv).
- [300 candidate test-fold metrics](evidence/research-rerun-2026-10-30-folds.csv).
- [Reporting script](../scripts/research_performance_report.py) and [independent arithmetic tests](../tests/test_research_performance_report.py).

The full 14,254,686-byte report, all daily returns, all baseline benchmark folds and corrected preflight packet remain in the ignored local directory `data/research/reviewed-metrics-rerun-2026-10-30/`. Bulky historical prices and daily records are not redistributed through Git. This directory is not a production backup.

| Identity | SHA-256 |
| --- | --- |
| Reviewed dataset | `689acbcc0199e3c0a72b4a0d37f535e7ed95e9c2e15342ecb8aff5ced826ad71` |
| Application source (138 files) | `ebd9e71a057cfc53dc0bbaa03837c3eb7600740ad183f3de4f516c8987d8a62a` |
| Full rerun report | `5a3dc51e7ad783d8bb0a603ab4c0aac5401ef44bd5bc36dfa770f0223d3e5069` |
| Reporting script | `d0c85a7831afad4718073f689cf3fcd1183ee8ca5be00ea15d6c20221c768b14` |
| Corrected no-run packet | `80054123498c3343baec683507e2d48b0648d22266e097041f486dacc508a67a` |

Run the installed locked research image offline. Mount the retained reviewed bundle read-only at `/source`, this repository read-only at `/repo`, and a new results directory at `/results`:

```sh
aca research import --source /source/source.json --gap-audit /source/audit.json --root /results
aca research run --dataset /results/datasets/DATASET_HASH/manifest.json --root /results --bootstrap-samples 10000
python /repo/scripts/research_performance_report.py \
  --report /results/reports/REPORT_HASH.json \
  --dataset /results/datasets/DATASET_HASH/manifest.json \
  --output /results/performance
```

The source-bound report hash changes with run identity/timestamps; deterministic economics and bootstrap results must agree. The exporter refuses a changed source fingerprint, inconsistent dataset, incomplete or non-contiguous folds, differing replay metrics or inconsistent daily/bar curves. It never opens a holdout.

Verification passes **33 targeted research/report tests**, strict project typing across **186 files**, and Ruff check/format across **216 files**. Nine new literal arithmetic/error fixtures cover pooled ratios, intra-day observation drawdown, inception/cross-fold high-water marks, undefined cash ratios, and invalid/inconsistent curves. All 300 baseline folds and 1,800 benchmark folds replay exactly; the full application source fingerprint remains unchanged.
