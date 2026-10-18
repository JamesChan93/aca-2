# Phase 4 increment — historical paper performance

Release candidate 0.4.4, 18 October 2026. The new report reconstructs GBP economics
from the explicit historical baseline through a selected original-time source
endpoint. This is paper accounting evidence. Phase 4 and G2–G6 remain incomplete;
no strategy is promoted, all twenty research candidates remain rejected, and the
final holdout remains unopened. Strategy scheduling and live execution remain
disabled.

## Export and boundaries

An account must already have the flat, financially reconciled enrolment described
in [historical paper risk](phase-4-historical-risk.md). There is no schema migration
or automatic backfill in this release. The existing `0008_historical_risk` schema
and dependency/image base pins are retained.

```sh
docker compose exec -T app aca paper performance --account 6e04530b-9382-418d-9a24-331672f4016b > report.json
docker compose exec -T app aca paper valuations --account 6e04530b-9382-418d-9a24-331672f4016b --limit 20
# Substitute exact recorded source numbers from this account's valuation query:
docker compose exec -T app aca paper performance --account 6e04530b-9382-418d-9a24-331672f4016b --start-source 100 --end-source 200 > period.json
```

By default the opening is the historical baseline and the closing is the latest
committed historical valuation. Optional source bounds must identify exact recorded
valuations, including when PostgreSQL sequence allocations have gaps. No nearest
quote, interpolation, wall-clock closing quote or future price is substituted.

The opening is **after** that source's controls/fills; the closing is after its
controls/fills. Period activity therefore lies in `(start source, end source]`.
Sources at the same timestamp remain distinct, ordered boundaries. A baseline-only
or zero-duration report is allowed, but its annualised MWR is unavailable. Historical
source coverage starts at enrolment, even if the account traded beforehand.
Opening inventory/units reflect the prior ledger; prior gains and fees are excluded
from period activity. The report does not label this limited coverage as inception.

The report includes both source receipt and economic timestamps, source/valuation
hashes, the original GBP bid marks, control/fill watermarks, opening/closing balances
and units, policy hash, historical/cursor anchors and the financial head version/hash.
`report_hash` hashes the complete JSON object excluding itself, using the existing
canonical fingerprint function. Repeated reads of the same database snapshot are
deterministic. A later financial head changes the report metadata, even if its
selected historical period remains identical.

`unread_public_sources` and `unapplied_cash_flows` describe the recorded suffix beyond
the historical head. They are excluded from the selected period. The operational
financial snapshot timestamp is reported separately. This command neither drains
replay nor reconciles outstanding fills. `financial_evidence_reconciled=true` refers
only to the reconstructed historical prefix, not current feed or account readiness.

## Financial reconstruction and metrics

The query first audits the operational event/journal projection and the historical
valuation hash chain, source coverage, policy and current cursor agreement. It binds
the opening flat inventory and units to the audited operational baseline. It then
reconstructs every valuation from that baseline through the selected endpoint,
including the prefix before a requested subperiod.

Owner contributions/withdrawals come from the original control-linked GBP journals.
They issue/redeem units using shared pre-flow valuation rules. Virtual fills must
have unique canonical IDs, exact matching reconciled fill documents and sealed
journals. The report independently regenerates each fill journal against the prior
inventory and verifies the stored economics. Unposted or different fills, false
balances/units, corrupt hashes and unsupported transfers/corrections reject the
report. Reporting never posts another journal or invents a missing fee valuation.

Reported period values include contributions, withdrawals, net investment flows,
fill count, actual fee quantities by asset, GBP fees at their original accounting
points, realised P&L and change in unrealised valuation. Where endpoints are valid:

```text
P&L = closing NAV - opening NAV - net investment flows
P&L = realised P&L + change in unrealised value - actual GBP fees
```

Both identities must agree exactly. Infrastructure spending, outside bank balances
and hypothetical tax payments are absent from trading NAV. The report is pre-tax;
it does not calculate household economics, capital gains tax or a tax reserve.

TWR is closing unit price / opening unit price - 1. If the opening is unfunded, its
retained pre-funding unit price is the base; with no investment at all TWR is
unavailable. Planned exposure limits or restart epochs do not reset period returns.
Maximum observed drawdown is calculated from the selected source-level unit prices,
including intermediate loss/rebound observations. It is not a continuous-time loss
bound, and does not reconstruct sub-observation price peaks or liquidity.

Annualised MWR treats opening NAV as an investor outflow, contributions as negative,
withdrawals as positive and closing NAV as a terminal inflow. Amounts at the same
instant are combined. Time uses exact UTC microseconds and actual/365.25 years.
Only a chronological single sign change qualifies for the solver's sufficient
uniqueness test. Withdrawal/reinvestment patterns with more sign changes are
unavailable (`uniqueness_not_established`), even if a numerical solver might find a
root. This deliberately avoids selecting one of several possible returns.
The supported annual rate range is [-99.99%, 100,000%], with 100 bisection steps,
at most 256 input flows including opening NAV, and a 100-year duration budget.
Outside these bounds the field is null with an explicit reason. Very short periods
can exceed the annual rate bounds despite modest monetary changes. No annualised
value is a forecast.

Missing NAV or the historical prefix's sticky `incomplete_since` suppresses TWR,
MWR and maximum drawdown. Selecting a later subperiod cannot erase an earlier
unobserved interval. Known endpoint P&L, fees and flows can still be exported;
this does not repair the missing path. A missing midnight risk observation also
keeps the original incompleteness marker. Warm-up reasons alone do not invalidate
cash-only NAV when the underlying history has no gap.

## Read isolation, bounds and decisions

**A38 — report the sealed original-time prefix.** Keep historical reporting separate
from the current operational status report. Preserve the existing `paper status`
DTO. An audit failure rejects the export rather than producing plausible returns.

**A39 — database-enforced read-only reporting.** One dedicated connection uses
PostgreSQL REPEATABLE READ and READ ONLY, without an execution advisory lock. A
concurrent writer's later commits cannot alter the report's snapshot. The CLI is
a separate process/engine with the existing small connection pool. It does not
change writer ownership, risk latches, orders, cursors or notification work.
This follows the [SQLAlchemy PostgreSQL transaction options](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#setting-read-only-deferrable).

Audits and historical reads use bounded pages; limits are 100,000 historical values,
100,000 operational events, 100,000 venue events, 10,000 journals and 4,096 canonical
fills per account. Exceeding a budget rejects the report. Individual SQL statements
time out after 30 seconds; this is not a total report runtime guarantee. Immutable
history still grows and full-prefix audit cost remains. Long-duration storage,
resource and archival qualification remains outstanding.

The release also fixes exact sign reversal of full-precision financial Decimals
in cash-flow and correction construction. `copy_negate()` preserves every digit
instead of applying the caller's ambient Decimal precision. These helpers still
require the existing exact NUMERIC(38,18) boundary checks.

All reports remain `provisional=true`, `promotion_evidence=false`,
`tax_report_final=false` and `live_execution_available=false`. This adds neither
eligible strategy episodes nor extended paper days. Transfers/conversions/correction
workflows, tax inventory, calibrated fills, strategy dispatch, outbound Discord,
process/disk faults and disaster restore remain outstanding. See the
[verification evidence](evidence/phase-4-performance.json) and the PR checks for the
measured scope of this increment.
