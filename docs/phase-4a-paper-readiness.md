# Phase 4a Batch F — approved paper studies and bounded observation capacity

Application **0.4.26**, schema **`0018_paper_studies`**, 29 October 2026.
This closes the existing **P4A-15/P4A-16** engineering items. The fixed Phase 4a
register is **19/19**; Phase 4 remains 17/17. The research decision remains
**no-go**. There is no eligible actual candidate, actual holdout opening, approved
observation start, qualifying day or qualifying episode.

The [source-bound evidence](evidence/phase-4a-paper-readiness.json) records the
final checks and [capacity measurement](evidence/phase-4a-observation-profile.json).
Current hosted checks must pass on the PR head before merge; earlier successes
are not transferred to a new release. No retained service is upgraded, migrated,
compacted or armed by this verification.

## P4A-15 — a separate, evidence-bound study

Existing `PaperSelection`, decisions and operational accounts keep their original
meaning and zero qualifying credit. The new append-only `paper_studies` and
hash-chained `paper_study_events` tables do not relabel those records. Each account
has at most one study; approval IDs cannot be reused. The runtime can insert/read
the new records but cannot update, delete, truncate or perform DDL. Downgrade
refuses to erase study history.

A start requires all of the following together:

- A corrected evaluation that still passes the existing source, statistical,
  economic, strategy, policy and evidence gates. The existing implementation
  rederives eligibility; an environment setting or caller-supplied pass flag does
  not replace it.
- Its matching immutable freeze and a matching, committed PostgreSQL holdout use
  and retained outcome. A consumed/reviewed holdout alone is not a passing result.
  The operator explicitly accepts the exact outcome in the start approval. The
  fixed actual holdout scope remains unique; synthetic campaigns stay separate.
- An approval binding its unique ID/actor, account, evaluation, freeze, holdout,
  release, installed Python source, policy, costs, operating review and start
  window. The window is at most 24 hours; it authorises the start, rather than
  requiring daily paper reapproval. Actual review follows freeze/holdout use.
- A matching operating review and storage configuration. Actual use additionally
  requires retained reviewed evidence that the actual producer fits the declared
  input envelope. A synthetic capacity fixture cannot establish that fact.
- A funded, flat, valid account without prior orders, operational selection or
  risk latches. Start stores its exact initial state hash. All input documents are
  retained in the append-only research registry in the same transaction.

The new study members cover the fixed 26 definitions, including daily S1 and both
S2 portfolios. S2 workers evaluate BTC then ETH against one existing execution
owner, ledger, cash, risk policy and reservation state. Instrument-specific IDs
prevent simultaneous signals from colliding. A strategy cancellation affects its
own entry; a halt retains the existing account-wide entry cancellation and
protective-exit policy. Existing operational selections still admit their original
five variants only.

The existing pure signals, closed-bar evidence, sizing, independent execution veto,
consume-before-action commands, paper venue, reconciliation and financial posting
paths remain in force. New study signal claims are durable before queueing; a
restart cannot retry a consumed decision. Completed episodes are derived from
reconciled cumulative entry/sale fills with a study signal, equal quantities and
terminal remainders. A notification, partial/open episode or historical order
does not create completed-episode credit.

## Time, health, faults and restart accounting

The approved start is the calendar origin. Earlier operational activity earns no
time. Each successful one-second supervisor tick supplies an observation; at most
60 seconds is buffered before a hash-chained health block is committed. A crash
loses the uncommitted credit. Gaps over two seconds, stopped processes and absent
records remain unobserved. Recovery audits existing state before dispatch and
records a new session, without backfilling the interval.

Health blocks partition observed time into healthy, unhealthy and verified
dependency-unavailable seconds. Both endpoints must support interval credit.
Unknown/missing feeds count against availability. An external exclusion requires
a recent, valid, classified exchange status in the current public epoch and a
retained proof packet. Old status messages cannot explain a later outage, and
financial invalidity/uncertain orders cannot become dependency exclusions.
The report denominator is elapsed calendar time less evidenced external outages;
all unobserved time remains in it. Risk suspension is healthy detection when the
application, feeds and reconciliation are otherwise healthy; it never enables an
entry or clears a latch.

Faults and financial discrepancies remain in the evidence history after recovery.
Report values separate elapsed days, observed episodes, availability and the
minimum 56-day/10-episode condition. Synthetic studies always report **zero
qualifying days/episodes**. `g4_pass` remains false: reaching two counters cannot
approve the remaining reliability, recovery and economic evidence or trading.
Study status reads its event chain in bounded pages.

An approved study requires its exact ID on `paper run`. A missing study, changed
release/source/policy/profile or attempt to use the operational supervisor fails.
Health endpoints identify the study; use the durable study report for credit.
Changing the environment cannot create a study or live capability.

## P4A-16 — explicit supported envelope and maintenance

The profile is conditional on the stated workload. It does not assert that an
uncapped Kraken feed fits, that synthetic input proves a trading advantage, or
that 56 real days have elapsed. The default remains **2 GB**. A reviewed
configuration can explicitly select **`paper_storage = "observation-8gb"`**;
no arbitrary size or environment-variable override is admitted. The opted-in
database ceiling is **8 GB**. The 4,096-record suffix, 100,000-block catalogue,
order/fill/journal limits, all financial/risk rules and disk-free guard remain.
The archive-file bound also remains unchanged; compaction is not an off-host
backup or permission to discard original financial evidence.

The measured local profile uses a 768 MiB/0.75 CPU application container and a
1 GiB/0.75 CPU PostgreSQL container. The 56-day model admits at most one financial
observation per second, 0.75 retained public frames per second, 0.1 public trades
per second and an 8,000-byte maximum encoded source row. Actual producer fit must
include all capture receipts, candle/metadata traffic, record sizes and bursts;
no trades or corrupt/stale data may be discarded to fit these assumptions.
The storage projection also requires compacted growth/entropy to fit the measured
growth envelope; frame count and maximum row size alone do not guarantee the
compression ratio. Faster/larger or less-compressible input requires a new reviewed
operating decision, while existing limits continue to fail closed.

Stop producers and the execution owner before 3,600 new financial records/2,700
public records (hourly at the maximum rates). The first compaction fully rebuilds
the prefix. Subsequent explicit `--previous-history` maintenance validates that
exact immutable, source/environment/policy-bound certificate and the complete new
suffix, retains predecessor IDs, copies/verifies original rows, and cuts over in
one transaction. Failure leaves original evidence and protection intact.

An execution owner verifies the complete selected prefix before acquiring
authority. Reuse is restricted to that exact dedicated connection while its
advisory lock prevents concurrent maintenance. Runtime lacks block/certificate
mutation authority. Releasing/losing that owner loses the cache. Independent
reports and complete reconstruction still validate original compressed evidence;
the cached writer path cannot supply an independent rebuild result.

After cutover closes its snapshots, explicit stopped-worker `history-vacuum`
uses ordinary PostgreSQL VACUUM/ANALYZE to make dead-row space reusable. It does
not run VACUUM FULL, remove logical history, grant runtime DDL or relax a bound.
The same writer/venue/public-producer locks apply. Maintain the returned current
certificate for the restarted worker. Stop/start overhead and maintenance are
charged as unavailable time; they are not removed as an external dependency.

The final measurement records three repeated 3,600/2,700 suffix cycles after the
100,000-event/100,000-valuation prefix, equal full/indexed accounting, a complete
independent original-row rebuild and a finite real publication/replay/risk loop.
The model doubles measured maintenance/restart cost, adds two seconds contingency,
doubles measured storage growth, reserves 512 MB for other state and a separate
2 GB for retained trade/raw evidence. It checks the unchanged catalogue ceiling
and measures retained trade-row bytes with an extra row/index allowance. The final local model projects **6,318,708,543 bytes** against 8 GB, **99,131**
catalogue blocks against 100,000 and **99.6371%** availability. Its doubled
maintenance/restart bound is **13.0651 seconds per hour**; the maximum measured
loop tick is **0.1375 seconds**, below the existing three-second deadline. All
timings and byte counts are in the linked packet; this is bounded extrapolation,
not measured long-duration availability. Deployment must still fit its physical
disk, WAL, logs and backup staging outside this database allowance.

At an exhausted suffix, backlog, database/disk boundary, lost writer, corrupt
certificate or missing evidence, entries stop. Keep protective exits and original
history, reconcile and perform the explicit stopped-worker recovery. Do not
increase limits, invent observations or discard history to continue. Full
independent rebuilds can use a separate read-only snapshot; they do not require
disarming a running owner, but their resource impact must fit the target host.
Scheduling/provisioning the production maintenance and off-host/WAL backup path,
host-loss fencing and real restore targets remain deployment/readiness decisions.

## Operator path after a genuine future eligibility decision

These are future review commands, not instructions to start the current rejected
candidate family. Start review runs in an environment with the reviewed optional
research dependencies, because it rechecks research inference; ordinary paper
running/status/health do not require NumPy. Use reviewed configuration and access
to the nominated PostgreSQL account, with private files kept outside the repo.

```sh
aca --config /private/operator/paper.toml paper study-start --account ACCOUNT_UUID --approval START_APPROVAL.json --evaluation EVALUATION.json --freeze FREEZE.json --holdout HOLDOUT_REVIEW.json --operating-review OPERATING_REVIEW.json
aca --config /private/operator/paper.toml paper run --account ACCOUNT_UUID --study STUDY_UUID --history-prefix CURRENT_CERTIFICATE_UUID --duration 0
aca --config /private/operator/paper.toml paper study-status --account ACCOUNT_UUID
```

Before a scheduled boundary, stop capture/runner/notification workers and verify
the exact current financial/source heads. Run the operator commands with the
separate migration credential, then restart with the newly returned certificate:

```sh
aca --config /private/operator/paper.toml paper history-compact --account ACCOUNT_UUID --actor OPERATOR_UUID --reason 'Reviewed hourly stopped-worker suffix maintenance' --expected-version FINANCIAL_HEAD --expected-source SOURCE_HEAD --previous-history CURRENT_CERTIFICATE_UUID
aca --config /private/operator/paper.toml paper history-vacuum --account ACCOUNT_UUID
```

The initial full compaction omits `--previous-history`. Do not omit a certificate
to bypass limits. These commands have been exercised only in disposable fixtures
here; this release does not schedule or invoke them on retained state.

## Integrated Phase 4a decision

The original 20-candidate, 300-fold empirical study remains preserved and rejected.
Batch D's corrected S0 preflight records 20 incomplete/no-run outcomes. Batch E's
four S1/two S2 definitions have six incomplete/no-run outcomes and no empirical
performance claim. Their source-assurance, statistical and economic limitations
remain the named research gate blockers. No actual candidate is freeze eligible;
the actual holdout is unopened and observation is unapproved at **zero days and
episodes**. Batch F runs no new actual-data performance experiment.

The fixed 19 engineering items are complete with the bounded acceptance evidence.
There is no additional Phase 4a coding checklist. Actual research eligibility,
holdout acceptance, observation-start approval, 56-day operational evidence,
production provisioning, private Kraken inspection and any future live writer
retain their original separate gates.


## Release verification

The final source passes 940 Linux/PostgreSQL tests (259.34 seconds), including the
existing 1,000-example financial and 10,000-sequence lifecycle properties, strict
mypy on 185 files and Ruff check/format on 214 source/test/script files. A populated logical restore
compares 35 tables/seven synthetic accounts in 42.585 seconds, rolls back an
injected failed restore and performs two disarmed recoveries with zero venue
commands. The minimal non-root runtime CLI starts without research dependencies
or network/credentials. Fresh hosted CI includes the separate observation-profile
acceptance job, unchanged trading/report deadlines and both image scans.
The final PR body records the hosted run links/results; the source-bound local
packet is retained separately. No safety requirement is waived by a test count.


## Hosted capacity correction within P4A-16

The initial `79d6901` push and PR runs passed the eight existing jobs, including
938 tests and both image scans. Both new capacity jobs failed the unchanged
99.5% model on slower hosted CPUs: measured maintenance was 9.5–9.7 seconds and
its doubled restart-adjusted bound was 21.8–22.3 seconds/hour, above 18. The
[initial measurements](evidence/phase-4a-hosted-capacity-initial.json) are retained.
A local pass was not substituted for this failed hosted acceptance.

Profiling identified duplicate financial/historical audit passes and many small
source reads. Compaction now constructs bounded report blocks during its one
complete mandatory historical report pass. The financial state comes from that
same report's mandatory financial reconstruction; financial and historical
certificates publish together in the cutover transaction. Source/raw-receipt
reads use bounded 32-row batches rather than eight. At the supported 8 kB encoded
source-row bound this is 256 kB of source documents per batch; existing block,
decoded-byte, retention, memory, financial and risk limits are not increased.
Separate reports and full rebuilds still audit independently.

New regressions require exactly one financial and one historical audit pass, equal
full/indexed economics, and rollback of all staged blocks/certificates if a late
corrupt value is discovered. The diagnostic probe retains a CPU profile and can
never satisfy acceptance. The 99.5% target, doubled maintenance multiplier,
18-second hourly allowance, input assumptions and fixture deadlines remain
unchanged. Final hosted results must pass on the corrected commit and are linked
in the PR body.
