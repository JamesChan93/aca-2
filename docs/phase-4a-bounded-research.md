# Phase 4a Batch E — bounded low-turnover and shared-account research

Application **0.4.25**, schema **0017_research_holdout**, 29 October 2026.
Implements the existing **P4A-08/P4A-09** requirements, advancing Phase 4a from
15/19 to **17/19**. Exactly P4A-15/16 remain in Batch F. Phase 4 stays 17/17.

The actual-data outcome is **six incomplete/no-run decisions**. Source evidence
still blocks corrected evaluation. This release supports and verifies the bounded
family; it establishes no new actual return, candidate ranking or promotion.
The original 20 empirical rejections and Batch D's 20 S0 no-run decisions remain
preserved. No candidate is frozen, the actual holdout is unopened, and qualifying
paper days/episodes remain **zero**. Retained workstation services are unchanged.

## P4A-08: four fixed daily candidates

The [28 October preregistration](phase-4a-bounded-protocol.md) fixes two additional
signal definitions on each of BTC/GBP and ETH/GBP:

- **SMA200 with a 1% band:** enter strictly above 1.01 times the average, exit
  strictly below .99 times it, hold otherwise, including exact equality.
- **SMA200 with two-close confirmation:** enter after two closes above their own
  200-day averages, exit after two below, hold otherwise. Requires 201 past bars.

These pure functions share closed-bar validation, Decimal indicators, ATR/5%
protective distance and slow-v2 timing with S0. A hold preserves an existing
position and does not enter from cash. A stop does not reset the indicator; only
a new daily decision satisfying the full entry rule can re-enter, subject to the
same latches, limits and independent risk checks. No stop is tightened to fit a
minimum order. Existing operational paper selections still admit only their
original definitions; research support does not select or enrol these candidates.

Completed and open episodes retain symbol, quantities, actual modelled fees and
entry/exit evidence. Whipsaws are disclosed as completed losing round trips of
at most seven days. Missed participation is a descriptive flat/non-entry decision
followed by a positive next daily close. This later diagnostic never feeds a
decision or inference. Costs, skips, inventory and partial/open episodes are
reported alongside complete account statistics, rather than silently removed.

The band .5%/1.5%, confirmation 1/3 days, 1.5-times fees, twice spread/slippage,
two-second latency and legacy bar-only stress are **registered, unrun review
sensitivities**. They cannot nominate another winner. No actual stability evidence
is claimed; missing stability/reuse approval still prevents freeze. The bounded
no-run acceptance in P4A-08 permits engineering closure without inventing results.

## P4A-09: two deterministic portfolios, one economic account each

`joint-hysteresis` pairs the band definition on BTC/ETH;
`joint-confirmation` pairs the confirmation definition. Their definitions are
fixed before evaluation and do not depend on a return ranking. They remain
unqualified family members, not eligible/promoted strategies.

Each portfolio uses **one opening balance, one monthly cash flow, one portfolio
state, one unit register and one GBP/asset journal**. Per-instrument price/signal/
episode contexts share that state; they are not separately funded accounts.
The same sizer, portfolio admission and independent execution veto inspect held
and pending exposure, cash reservations, tax reserve and aggregate planned risk.
All six provisional limits and conservative defaults retain their values.

Daily timer ties use BTC then ETH; trade receipt ties preserve source order.
Available evidence and shared reservations determine admission. Signals retain
their original deadline; rejected signals are not chased. Shared-account economic
IDs include the instrument, so simultaneous equal-rule signals cannot overwrite
one another. Existing single-instrument IDs retain their original construction.

Matching consumes only that instrument's print participation. Strategy exits
cancel only their own protective order; partial residuals regain only their own
protection. Account risk latches cancel exposure-increasing entries across both
assets while preserving both protective sells. Missing fresh marks for either
held asset block new commitments and pre-flow unitisation. No mark is restamped.

Whole-account NAV, units, high-water marks, reservations and latches survive
report boundaries. Joint uncertainty uses the shared account's paired daily
returns under the unchanged 7-/30-day, 104-claim protocol. It never averages
independent strategy returns or adds standalone drawdowns. Reports retain joint
drawdown, actual account economics, asset-close co-loss dates and episode detail.

Cash and passive10 remain mandatory. Joint passive10 deploys 5% BTC and 5% ETH
from each equally dated funding flow; passive30 uses 15% each. Full buy-and-hold
and monthly-purchase blends use 50% each, with separate BTC/ETH comparators and
the original buy-only bounded passive policy. Past-exposure/risk diagnostics split
their historical aggregate target equally, using current per-asset evidence and
monthly buy-only budgets. These do not replace mandatory comparisons or change
the strategy's shared risk allocation. No weight optimisation or pyramiding exists.

## Family, gates and retained actual decision

The registry contains **20 S0 + 4 S1 + 2 S2 = 26 definitions**. New declarations
record all reserved names, exact applicable definitions, code, common calendar,
costs, policy, evidence, original report identity and prior S0 no-run packet.
Original S0 definition semantics/hashes and historical trial artefacts are kept.
The original study and adaptive choices remain reused exploratory history; a new
study directory does not create independent confirmation or reset the family.

Both new studies use the corrected continuous account/comparator engine and the
same evidence preflight, inference, economic test and rechecked freeze boundary.
Joint report bindings additionally require the exact shared-account definition.
Incomplete evidence stops work before performance. Bounded transactional bundle
attempts preserve rejected, failed, completed and abandoned outcomes, resume, and
archive/restore under the existing budgets. A `+` is admitted in bounded task
labels for the joint instrument label; storage paths remain hash-derived.

The installed offline run uses the unchanged actual dataset
`689acbcc0199e3c0a72b4a0d37f535e7ed95e9c2e15342ecb8aff5ced826ad71`.
All six decisions retain the full-archive verification gap, disputed BTC IDs
7098364–7098366 and quarantine/blend effect, reconstructed historical fees/rules,
unavailable receipt latency and absent qualified source/cost/parity/reuse/stability
reviews. Zero reviewed qualified history days means **no qualification credit**;
it does not assert that no historical observations exist.

The [actual receipt](evidence/phase-4a-bounded-preflight.json) binds installed code
`909a0b93c3711d97cb943ed09b34ac62a593253d6f33eba1fd2e62c95efef1f0`,
equal to all 134 worktree Python files. Its declaration is
`0655207a0740be0e3309e2743a58d1df861de4124855445e99f165209ab31da1`;
sealed packet
`678199122bef5d8edbd119522200eb5aa69e6b636712ebb4adf4be1fc2fcbad2`
contains 27 objects / 62,403 bytes. The 92,160-byte offline archive has SHA-256
`0f9e59e3ae08fa8b063c3931272f257805054f606bd354457658ecead1e01ad7`;
restore verifies identical complete object closure and task status.

This run had no network, database, account, webhook or deployment access. The
calibration implementation/protocol are unchanged and the retained source-bound
calibration remains valid for its original bounded synthetic claim; it is not a
new alpha result. No actual performance evaluator, freeze or holdout was invoked.

## Verification and reproduction

The synthetic checks cover all six new definitions through the continuous study
workflow, retain their short-sample rejection, and cover all 26 definitions across
the integrated S0/S1/S2 tests. Separate positive statistical workflow fixtures
remain wholly synthetic. Literal shared-account fixtures buy .01 BTC and .01 ETH
at £1,000 with .08 GBP fee each: one £1,000 account ends at £979.84 cash and
£999.84 NAV, not two £1,000 accounts. The same fixtures cover duplicate prints,
instrument-scoped exits/partial protection, stale other-asset marks, shared tax/
pending reservations and correlated-loss hard halts. Split/prefix tests preserve
joint economics and exclude future observations from prior decisions. A new
1,000-example shared-account property verifies financial reconstruction,
participation, cash/exposure/planned-risk admission and GBP attribution alongside
all existing financial/lifecycle properties.

Final installed Linux/PostgreSQL verification passes **915 tests in 229.00
seconds**, including the existing critical financial/lifecycle properties and the
new shared-account property. Strict mypy passes 180 files; Ruff check and format
pass 256 files. The installed wheel matches the 134-file source fingerprint above.

Full regression/type/format results and exact-head hosted CI belong in the
[source-bound evidence register](evidence/phase-4a-bounded-research.json) and PR.
No earlier successful release substitutes for this commit's checks.

In the research-enabled installed image, with inputs read-only and a new results
directory:

```sh
aca research bounded --dataset REVIEWED_MANIFEST \
  --source-review docs/evidence/phase-4a-source-assurance.json \
  --cost-review docs/evidence/phase-4a-public-costs.json \
  --parity-review docs/evidence/phase-4a-continuous-accounts.json \
  --calibration docs/evidence/phase-4a-statistical-calibration.json --root NEW_RESULTS
aca research bundle-review --packet SEALED_PACKET
aca research bundle-archive --packet SEALED_PACKET --output NEW_ARCHIVE
aca research bundle-restore --archive NEW_ARCHIVE --directory NEW_RESTORE
```

A later reviewed bundle/trade stream uses the same `--review-bundle` / `--trades`
inputs as corrected S0. It cannot override the required facts with a label. The
current evidence produces six no-run outcomes, no empirical qualification.
