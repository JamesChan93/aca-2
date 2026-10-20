# P4-C3 — Daily-summary and audit capacity

20 October 2026. Candidate **0.4.14**, schema `0014_accounting_operations`.
Scope: the existing fourth condition in the
[fixed Phase 4 checklist](phase-4-completion.md#p4-c3--close-the-named-local-paper-safety-gaps).
C1/C2 and the other four C3 conditions retain their linked evidence. This increment
introduces no additional completion condition. **C3 condition 4 remains open**;
processing speed alone cannot supply the missing 56-day preservation capacity.

## One audited snapshot for daily reporting

The daily summary previously audited the financial history, restored the complete
valuation history to discover the requested day, and then invoked the historical
report, repeating the financial audit and valuation pass. With both midnight
boundaries present, it invoked another historical report.

The shared `read_performance` service now audits the financial history once and
exhausts one validated valuation stream in the same read-only, repeatable-read
transaction. It calculates the selected observed period and collects both UTC
boundary anchors during that pass. Daily accounting totals still use the half-open
UTC calendar interval. Contributions and withdrawals remain external flows;
missing midnight coverage still yields an incomplete daily return.

Calendar selection never limits validation to that day. Every recorded source,
chain link, NAV, policy, chronology and embedded midnight anchor is checked. Every
historical balance/unit transition through the head is reconstructed from the
financial evidence, including evidence after the selected day. A corrupt later
record also rejects a summary for a day without any observations. Unenrolled
accounts retain their audited accounting-only summaries; accounting corrections
retain explicit unit/report invalidation.

## Bounded audit cost

Valuation and replay-cursor audit pages contain at most 256 rows, and financial
full-audit pages at most 512 rows. The financial checkpoint interval and fast-load
tail remain 128. Checkpoint audits fetch the anchors for each bounded group of 16
checkpoints together, then validate every checkpoint, including older ones.
Financial delta restoration renders and size-checks its restored state once before
hashing that same document.

A journal-free event cannot change the balances. After both adjacent snapshots
have passed typed restoration, the audit directly compares the already validated
canonical GBP/BTC/ETH balances when GBP quantity equals its GBP cost. Events with
journals still reconstruct balances from those journals; noncanonical legacy
prefixes use the original reconstruction. All operation/reserve checks and the
independent final SQL ledger comparison remain. A regression inserts correctly
hashed, journal-free inventory creation followed by its reversal: even though the
final projection matches the ledger, the intermediate creation is rejected.

Exact decimal text validation caches at most 1,024 successful strings of at most
128 characters. Cache keys retain exact text, including scale and signed zero.
The type boundary is checked before cache access; binary floats, integers and
booleans still fail. Decimal objects and longer strings use the exact range check
directly. Invalid results are never cached. The validation is independent of the
ambient Decimal context, and the existing independent 1,000-example quantization
comparison remains. Financial arithmetic continues at precision 50.

Record ceilings, report/startup/summary deadlines, database-size limits, resource
caps and the admitted risk policy are unchanged. No prefix is trusted on the
strength of a cached order/risk decision, skipped source check or deleted record.

## Reproduction and evidence

```sh
.venv/bin/python scripts/ci_database.py --linux -q
python3 scripts/capacity_trial.py
```

The [capacity-trial interpretation](phase-4-capacity.md#reproduction-and-interpretation)
still applies: an operation finishing after its original deadline fails that
budget. A successful measurement job records and cleans up an experiment; it
cannot approve C3 or G2. Cash-only bulk construction is not market ingestion,
fully loaded strategy execution or elapsed reliability evidence.

The [machine-readable evidence](evidence/phase-4-summary-capacity.json) records
verification, exact package-source identity and the local measurement. The PR
records the exact-head push and pull-request workflow results after completion;
hosted measurements take precedence over a narrowly passing local duration.

Final local trial, `capacity-trial-6a64cf1b7272`, using the unchanged
0.75-CPU/768-MiB worker and 0.75-CPU/1-GiB database:

| Operation | Measured duration | Existing budget | Result |
| --- | ---: | ---: | --- |
| Financial writer startup | 9.36 s | 30 s | Pass locally |
| Combined financial/replay startup audit | 30.94 s | 20 s | **Fail** |
| Historical performance | 21.48 s | 20 s | **Fail** |
| Replay audit after writer acquisition | 16.10 s | 10 s | **Fail** |
| Scheduled daily summary | 21.16 s | 15 s | **Fail** |

All 20 one-second maintenance ticks were healthy. The maximum tick took 0.078
seconds and there were zero venue commands. The synthetic database used
500,717,247 bytes, worker peak cgroup memory was 105,201,664 bytes, and measured
worker CPU use was 152.60 seconds for the complete experiment, including bulk
construction and maintenance. All owned resources were removed. These numbers
remain measurements of the declared cash-only workload.

The final full Linux/PostgreSQL regression passed **598 tests in 104.59 seconds**.
Ruff, formatting (161 files), strict Mypy (139 files), dependency consistency,
wheel/sdist build and all three vulnerability audits pass. The packaged CLI and
accounting smoke check preserves the exact account state across an application
container restart, with matching source hashes in the checkout, wheel, runtime
image and capacity worker. The retained container identities are unchanged.

## Remaining condition and handoff

At one observation per second, 56 days require **4,838,400 observations**. The
unchanged 100,000-observation ceiling covers about **1.1574 days**. Actual ordered
public receipts can exceed that rate. There is no implemented archive reader or
reviewed preservation path for the required history. Existing financial/order,
projection and storage bounds also remain applicable.

The next work remains this same C3 condition: preserve the source/valuation/cursor
and financial prefixes so bounded operational reads can continue across the
planned workload, without resetting units, high-water marks, loss latches or
permanent client IDs. Required archived evidence must be verified before use;
missing/corrupt evidence must block acceptance. Increasing row limits alone or
backing up and deleting rows cannot establish that behaviour.

The fixed checklist remains **12 of 17 conditions complete**. C4 follows C3's
resolution. The retained 0.4.8 stack is unchanged; qualifying paper observation
has not started, all 20 research candidates remain rejected, and live execution
is unavailable. No deployment, migration of retained state or external
notification is part of this increment.
