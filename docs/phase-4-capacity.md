# P4-C3 capacity work — audit cost and remaining retention limit

20 October 2026. Candidate **0.4.13**, schema `0014_accounting_operations`.
This addresses the existing fourth condition in the
[fixed completion checklist](phase-4-completion.md#p4-c3--close-the-named-local-paper-safety-gaps).
**That condition remains open.** Faster audits do not establish 56-day retention.
The [original safety report](phase-4-safety.md) and its immutable evidence describe
0.4.12; the [capacity evidence](evidence/phase-4-capacity.json) records this candidate.

## Changes

Historical valuation audits now fetch source numbers and fingerprints once per
64-row page. They compare the actual ordered source sequence, including at page
boundaries. PostgreSQL sequence-allocation holes remain allowed; a missing
observation or source anchor remains an error. Cursor audits likewise fetch their
page's anchors together, while checking each cursor's identity and hash chain.

The historical report calculates from the same audited observation stream instead
of restoring the entire valuation history a second time. It still exhausts the
whole recorded prefix before returning a report, including when the requested
report endpoint is in the past. A bad later record or missing suffix cannot be
hidden by requesting an earlier endpoint. The database transaction remains
read-only and repeatable-read. Every record still undergoes typed validation,
hash, NAV, policy, ordering and applicable UTC-boundary checks.

Profiling also identified repeated context creation and quantization in financial
input validation. The equivalent check now inspects a Decimal's coefficient and
exponent: magnitude below `1e20`, with no nonzero digits below `1e-18`. It preserves
the original value, including insignificant trailing zeroes. Financial floats and
non-finite or unrepresentable values remain rejected. A 1,000-example independent
quantization comparison covers both signs, up to 200 coefficient digits and
exponents from -200 to 200, under a deliberately restricted ambient context.
The existing 1,000-example exact financial properties also remain in the suite.
Financial arithmetic continues to use precision 50.

The advisory check also found a newly published critical issue in the previously
locked certificate bundle. The [dependency review](dependency-review.md) and ADR
A16 amendment record the targeted `certifi==2026.10.19` update. All three affected
locks are regenerated with hashes; no other dependency or base image changes.

## Reproduction and interpretation

```sh
python3 scripts/capacity_trial.py
```

This creates and removes only a new disposable database/project. It retains the
original 100,000-row limits and 30/20/20-second financial-startup, combined
replay-startup and historical-report budgets. It now also measures the existing
ten-second replay-audit budget **after** financial writer acquisition, and the
notification worker's existing 15-second daily-summary budget. These are actual
callers of the report/audit code, within the same C3 capacity condition.

An over-budget operation may continue diagnostically for at most 60 seconds to
record its actual duration. `operation_finished=true` does not imply a pass:
`outcome` remains `deadline_exceeded` when the original acceptance budget is
exceeded. A timed-out operation remains a failure as well. No runtime timeout,
record ceiling, database-size ceiling or container resource limit is increased.

The worker is limited to 0.75 CPU and 768 MiB, PostgreSQL to 0.75 CPU and 1 GiB.
The seeded histories are cash-only, contain no fabricated liquidity or exchange
commands, and are bulk construction rather than ingestion-throughput evidence.
The separate maintenance account has 100,000 financial events and completes 20
real one-second ticks. This is neither a fully loaded market/strategy trial nor
56 elapsed days of reliability. Local workstation and hosted CI measurements must
be distinguished; a faster local CPU cannot waive a hosted deadline failure.

The [machine-readable evidence](evidence/phase-4-capacity.json) contains final local
measurements and verification results. Exact-head hosted results are attached to
the pull request after both workflows finish. A successful measurement job only
means that the bounded experiment completed and cleaned up; its explicit failure
list still governs capacity acceptance.

Final local run (`capacity-trial-11511946af95`), with `certifi==2026.10.19`:

| Operation | Measured duration | Unchanged budget | Result |
| --- | ---: | ---: | --- |
| Financial writer startup | 9.99 s | 30 s | Pass locally |
| Combined financial/replay startup audit | 19.50 s | 20 s | Pass locally, little margin |
| Historical performance | 19.02 s | 20 s | Pass locally, little margin |
| Replay audit after writer acquisition | 9.41 s | 10 s | Pass locally, little margin |
| Scheduled daily summary | 35.08 s | 15 s | **Fail** |

Twenty maintenance ticks were healthy, with maximum duration 0.169 seconds and zero
venue commands. The synthetic database used 500,782,783 bytes; peak worker cgroup
memory was 101,560,320 bytes. Owned resources were removed. A preceding repeat of
the same runtime code took 21.88 seconds for combined startup and 34.86 seconds for
the summary, illustrating timing variability rather than assured headroom.

The patched full Linux/PostgreSQL suite passed **596 tests in 98.70 seconds**.
Ruff checks/formatting (161 files), strict Mypy (139 files), wheel/sdist build,
dependency consistency and all three advisory audits pass. A disposable packaged
CLI/accounting smoke check survives an application container restart, preserves
the exact account state and verifies the installed source hash against the
checkout and wheel. The retained container identities remain unchanged.

## Remaining C3 condition 4

One observation per second requires **4,838,400 observations** over 56 days.
The unchanged 100,000-observation report ceiling holds approximately **1.1574 days**.
The ordered replay actually records one historical observation per public receipt,
which can arrive more frequently than once a second. There is no demonstrated
56-day headroom and no implemented preservation/archive reader for these histories.
The database's 2 GB stop and the projection/order/journal/fill limits also remain.

Increasing those constants alone cannot resolve this condition. Nor can taking a
backup followed by dropping operational source rows: readers currently need those
rows to verify financial history, source coverage, risk latches and original-time
performance. The existing logical restore reconstructs the saved database; it is
not a hot-history archive reader or a reconciliation of evidence absent from a
backup. A new account or policy epoch must not be presented as continuous history
or used to reset drawdown, units or permanent order identities.

The remaining engineering work under **the same condition** is a preservation path
that keeps the authoritative ledger, quantity movements, fills, permanent client
IDs, units and latches intact, verifies any archived source/valuation/cursor prefix
before accepting it, and blocks if a required archive is missing or corrupt. It
must preserve the specified acquisition/tax and financial retention periods and
pass a bounded trial with a declared 56-day workload, including CPU, storage,
backlog, startup, reports and the one-second loop. This paragraph records the
requirements a design must meet; it does **not** claim that a design or archive
implementation has already passed review.

No additional C-package, backup system, paid infrastructure or production migration
is introduced. C3 remains partial and C4 follows its resolution. The retained
0.4.8 paper stack is unchanged; there is no Kraken writer, qualifying observation
start, research promotion or G2/live approval.
