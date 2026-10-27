# Phase 4a Batch D — statistical protocol and enforced research gates

Application **0.4.24**, schema **0017_research_holdout**, 26 October 2026.
Implements the existing **P4A-11/P4A-12** requirements. Phase 4a advances from
13/19 to **15/19**; Phase 4 remains 17/17. Four original items remain: bounded
S1, shared-account S2, qualifying-paper workflow, and sustainable operation.

The actual corrected S0 outcome is **incomplete / no run**. Its source review is
still blocked. The new workflow declares all 20 original definitions and records
one incomplete decision for each before any performance evaluation. It does not
replace the original study's 20 rejected candidates with new return estimates.
There is no candidate freeze, actual holdout opening, paper selection, deployment,
private-account access, outbound notification, or qualifying observation credit.

## P4A-11: preregistered statistical decision

`research.inference.StatisticalProtocol` fixes paired circular-block percentile
Bonferroni inference; there is no result-dependent method switch. The family
reserves **26 definitions × cash/passive10 × 7-/30-day blocks = 104 claims**.
S1/S2 slots remain reserved even if unrun. The original 20 trials and adaptive
review/history reuse remain visible; this correction does not create independent
confirmation or replenish the search budget. A substantive reuse review and
stability review must be bound before a candidate can freeze.

Only the £250 opening / £250 monthly account can select a candidate. Other capital
cases, matched-exposure diagnostics and stress/sensitivity cases cannot nominate
an alternative winner. Every mandatory candidate bound must be positive, followed
by the unchanged twice-annual-fixed-cost hurdle. An operator may review any individually eligible candidate under this
simultaneous correction; observed return ranking supplies no additional evidence.
The workflow records candidate eligibility;
it does not automatically choose a winner or grant paper/live permission.

The production protocol fixes **262,144 draws**, PCG64 seed **1729 + block days**,
and at least **100 expected decision-tail draws**. It retains the plain
one-sided 0.05/104 Bonferroni percentile as baseline evidence. Decision inference
allocates .04 family error to the bootstrap tail and .01 to Monte Carlo rank error:

- Decision tail probability: .04/104; about 100.825 expected tail draws.
- Use a lower-tail binomial Chernoff margin of
  `sqrt(2*N*p*log(104/.01))` and the conservative order statistic, rank 56 at the
  default draw count. This bounds simultaneous Monte Carlo rank error by .01
  conditional on the empirical bootstrap distributions.
- The total nominal error budget remains .05. The rank calculation does not prove
  the bootstrap approximation has correct coverage for arbitrary return processes.

Block sums include the truncated last block and use bounded 256-draw batches.
An independent explicit-index fixture checks the optimisation, including wrap and
truncation. Common deterministic indices preserve paired dates and dependence;
Bonferroni does not assume independent candidates. Missing/reordered dates, null
required observations, partial days, nonfinite returns and nonpositive wealth
fail. Required comparisons cannot qualify by removing unsuccessful days.

The calibration is preregistered synthetic evidence: 64 independent worlds of
252 days; four correlated AR(1) processes (.5), shared shocks and twelve-day
volatility clusters; these processes repeat across all 26 definitions. Both
mandatory comparator differences share each path. A false positive from **any**
candidate counts as a family error. The known effect is a fixed +.002 daily
increment, assessed on all four processes. The acceptance rule requires a
one-sided 95% null-family upper bound below .05 and at least 90% known-effect
sensitivity. This deliberately large effect tests detection capability; it is
not a realistic strategy forecast or a minimum-detectable-alpha estimate.

The committed calibration packet binds both the protocol and inference-source
SHA-256. Changes invalidate that evidence. Finite calibration worlds and these
particular dependency patterns do not establish universal false-positive control,
fix structural breaks, or cure historical/adaptive reuse. No actual strategy
returns or the actual holdout are used for calibration.

## P4A-12: rechecked decisions and durable holdout use

Immutable strict contracts distinguish **failed**, **incomplete**,
**freeze_eligible**, and **holdout_reviewed**. Bindings include purpose, exact
candidate definition, dataset, stable source lineage, installed code, costs,
policy, calendar, protocol and review evidence. `freeze` rederives the decision;
opening rederives it again and must match the exact frozen decision hash.
A caller-supplied pass flag or a registered historical report is insufficient.

Admission requires four years of reviewed usable history, two years of complete
OOS days, at least 50 completed episodes, fresh terminal evidence, no outstanding
orders, positive adjusted cash/passive10 bounds, the fixed-cost hurdle, a passing
source-bound calibration and retained matching source/cost/parity/reuse/stability
review documents. Financial text remains Decimal. A duration-average equity can
have repeating decimals: its conversion to the economic check explicitly rounds
down to 18 places, outside the journal. There is no balance rounding or risk change.

Source reviews cannot substitute a local hash for verified history. Approved
review references must resolve to their exact bytes, purpose and code/data/cost/
policy/protocol context. Incomplete source/cost/parity/reuse/stability facts stop
corrected S0 before strategy performance is evaluated. The current retained
public-cost and parity packets are historical evidence, not new approvals for
this source revision. A typed review bundle supports later reviewed inputs;
changing booleans while retaining an unqualified or mismatched document fails.

The old `research freeze` call now accepts only a corrected evaluation envelope
and an explicit review actor/reason. Legacy freezes and the legacy `research
holdout` command are refused. Existing reports remain readable and preserved.
A versioned generic holdout gate is an internal API requiring a reviewed, bound
freeze and a separately supplied evaluator; it is not an automatic CLI opening.
A successful callback records **reviewed/consumed**, never a promotion pass.

PostgreSQL's new append-only `research_holdout_uses` table owns the claim. The
actual unique scope is fixed to Kraken Spot GBP, **2025-10-01 to 2026-10-01**,
independent of candidate, dataset hash, code, source-lineage label or artefact
folder. Rehashing or copying evidence cannot buy another use. A claim commits
before the evaluator can read the holdout; failures and uncertain completion
leave it consumed. Conflicting concurrent requests cannot both invoke evaluation.
No transaction stays open during the callback. Synthetic campaigns have a UUID
namespace and cannot create actual promotion permission.

The migration is additive; runtime retains SELECT/INSERT only, and mutation
triggers forbid erasing claims. Existing latches, commands, accounting and paper
schemas retain their semantics. Restore must retain/reconcile the claim history;
an old database backup is not permission to reopen research. No retained local
service is migrated or restarted by these tests.

## Corrected S0 and current decision

`research.study.run_corrected` writes the protocol, all 20 definitions, common
calendar, source/code/review/input identities, cost model and risk policy before
work. Successful inputs invoke the same continuous account and equal-flow
comparators from Batch C, with separate immutable evaluation/decision artefacts
in the bounded resumable bundles. Trade input hashes are rechecked per task;
reads are incremental, receipt ordered and bounded per record. Ordinary
performance input excludes the actual holdout.

The integrated synthetic test actually runs all 20 original definitions through
that continuous model and retains their short-sample rejection. It catches a
four-hour/daily label mismatch by binding the real input interval explicitly.
A separate source-blocked test prohibits even constructing the evaluator, records
20 incomplete outcomes, resumes identically and verifies archive/restore.

The actual retained dataset is unchanged:
`689acbcc0199e3c0a72b4a0d37f535e7ed95e9c2e15342ecb8aff5ced826ad71`.
Its source review still reports missing full-archive verification, disputed BTC
IDs and the required blend comparison, historical fee/rule verification and
historical receipt timing. The new gate also refuses absent current
cost/parity/reuse/stability approvals. All 20 preregistered tasks therefore finish
with `incomplete_evidence_no_run`; none receives a fabricated performance report.
This completes the bounded engineering/decision work, not empirical G3.

The original negative passive10 comparison is a reason to investigate transaction
costs and participation, not evidence that hysteresis or confirmation will help.
There is no corrected empirical basis to rank S1/S2 mechanisms. The remaining
Batch E may implement and preregister its already approved bounded mechanisms
synthetically; actual evaluation stays no-go until the named facts/reviews qualify.
There is no requirement to exhaust the budget or keep adding strategies.

## Reproduction and evidence

In the research-enabled installed image, with public inputs mounted read-only:

```sh
aca research calibrate --root NEW_RESULTS
aca research corrected --dataset REVIEWED_MANIFEST \
  --source-review docs/evidence/phase-4a-source-assurance.json \
  --cost-review docs/evidence/phase-4a-public-costs.json \
  --parity-review docs/evidence/phase-4a-continuous-accounts.json \
  --calibration CALIBRATION_JSON --root NEW_RESULTS
```

This invocation performs retained-evidence preflight and emits a sealed no-run
packet. It needs no network, database, exchange credentials or notifications.
The generic actual study API/CLI can receive a later reviewed bundle and hashed
trade stream; unqualified facts cannot pass merely by supplying the stream.

A corrected eligible envelope can be reviewed with:

```sh
aca research freeze --report CORRECTED_EVALUATION_JSON --trial REGISTERED_TRIAL \
  --actor REVIEWER --review 'Substantive evidence-bound review reason' --root RESULTS
```

The current actual results cannot satisfy this command. Synthetic positive tests
exercise it separately and receive no actual selection or observation credit.
The [source-bound evidence register](evidence/phase-4a-statistical-gates.json)
records the final package fingerprint, calibration, no-run packet and validation.
Full hosted results belong to the exact PR head and are linked in its description.

The installed offline run has source fingerprint
`52cef2b9c996e292de266482b425372d670eab9e7f3cf8141367682744c59a75`
(132 application Python files, equal to the worktree). Its
[calibration](evidence/phase-4a-statistical-calibration.json) has canonical identity
`e0c57cbc694eca25e6e603b8687534f1062aa55858916335a06813a634182fb3`: zero
false positives in 64 null worlds, one-sided upper limit about 4.573%, and 64/64
known-effect detections. These remain synthetic bounded results.

The [actual preflight packet summary](evidence/phase-4a-s0-preflight.json) records
sealed packet `b73c112c3fd18bdea3ada3114700e03a745db039c351b11d8d80904bef938a72`,
83 objects / 103,146 bytes and 20 incomplete decisions. The 174,080-byte offline
archive has SHA-256
`f76128b67984bf427fcf389d07d0194fb41cfd885576bab529bc53ab71611d6a`;
restoration verifies the same complete object closure and task statuses. This is
a local evidence archive, not an off-host backup or production restore claim.

Final local frozen-source verification: **885 Linux/PostgreSQL tests pass in
223.45 seconds**, including the existing 1,000-example financial properties and
10,000 generated lifecycle sequences. Strict mypy passes 177 files; Ruff check
and format pass 205 files. The installed wheel and worktree fingerprints agree.
Hosted dependency, image, capacity, restore and packaging checks still belong to
the exact PR commit; previous-release success does not approve this source.

## Database-image security follow-up, 27 October 2026

Both initial hosted workflows passed all 885 tests, typed/format checks, restore
(including all 33 application tables), capacity, dependency audits, packaging and
application-image scanning. Their database-image scan found
[CVE-2026-92392](https://curl.se/docs/CVE-2026-92392.html) in libcurl 8.22.0-r0.
A19 records the exact signed Alpine `libcurl=8.23.0-r0` fix, preserving the base
digest, PostgreSQL version and existing Brotli pin. Comparing the previous
committed image with the patched image shows only this package changes (53
packages in each). No finding is suppressed and no scan is weakened.

The patched local image passes the full **885-test PostgreSQL suite in 256.44
seconds** and Trivy 0.76.0 finds **zero high/critical vulnerabilities and zero
secrets**. The first local vulnerability-database mirror transfer ended early;
a retry from Trivy's official registry succeeded. Local image verification is
arm64; the new hosted runs verify amd64. The
[security receipt](evidence/phase-4a-image-security.json) preserves the initial
failed run IDs, exact package/inventory hashes, scan identity and limits. Python
source, protocol/calibration/preflight identities, financial policy and dependency
locks remain unchanged. Retained services remain on their original images.
