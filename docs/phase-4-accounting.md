# C2 — Managed-account transfers and corrections

Application 0.4.11, schema `0014_accounting_operations`, 19 October 2026.
This implements all three P4-C2 conditions in the
[fixed Phase 4 checklist](phase-4-completion.md). It authorises no deployment,
research promotion, qualifying observation or exchange access.

## Supported operations

`aca paper transfer` records a signed quantity of GBP, BTC or ETH crossing the
nominated **virtual account** boundary. Positive is inbound; negative is outbound.
The request supplies an external reference, actor UUID, expected state version,
reason, evidence SHA-256, exact event time and an explicit GBP valuation at that
same time. The SHA-256 is an operator evidence reference, not proof that the
application fetched or verified an external document. The local CLI/operator is
trusted; this is not an identity/authentication or signing system.

A transfer issues/redeems investment units at the valid pre-flow unit price.
All other held assets need fresh GBP valuations. The supplied asset valuation is
record evidence and cannot authorise an executable quote. Pre-flow price changes
are observed before issuing units; a deposit cannot hide a loss or clear a latch.
A transfer immediately disarms the account pending reconciliation. Incoming crypto
has no automatically invented protective order: unprotected inventory blocks
entries and needs an explicit position-management decision.

Incoming asset cost in this managed-account book is its event-time GBP value.
An outgoing asset removes proportional managed-account cost. The difference
between that cost and its outbound GBP value is separately attributed as
`transferred_unrealised_gbp`, using `equity:transfer_basis`. A transfer itself
creates no trading gain. For example, a half-unit withdrawal of an asset whose
value rose from £100 to £200 carries £50 of earlier unrealised gain out of the
book. It is not an execution or a realised trading sale.

These costs are **not owner-wide tax basis**. `tax_classification=not_determined`
is retained in the operator evidence. No transfer is presumed to be a tax disposal,
and no claim about its final tax treatment is made. Fees must be represented by
supported authoritative fill/fee records; the transfer path invents no fee.

## Corrections and the chosen C2 boundary

`aca paper correct` supports erroneous contributions, withdrawals and supported
transfers. It cannot rewrite an authoritative trade/fill, fee or manual reserve.
A reserve has its existing separate replacement-decision command.

A correction atomically appends a reversal journal and a replacement journal,
with `reverses`/`replaces` links, the original evidence, the operator reason and
the prior operational report. A versioned `paper_delta_v2` event binds both
postings to one new state. Old events, journals, checkpoints, original-time
valuations, notices and report artefacts are never edited or deleted. A target
can be reversed once; another amendment must target its replacement. Duplicate
request IDs return their original decision; conflicting retries fail.

The checklist explicitly permits **rebuilding or invalidating** affected units
and reports. This release implements **invalidation**:

- Current quantities reflect the corrected movements. Journal/cost totals are
  independently audited against the immutable SQL postings. Intervening trade
  cost allocations are not restated; cost/return projections remain invalid, even
  when these arithmetic totals reconcile.
- The old numerical unit count remains evidence but is marked
  `accounting_invalidated=true`. Unit price, TWR, MWR and P&L reports become
  unavailable. All source-bounded historical performance queries are invalidated,
  including requests for an earlier source range. Raw earlier evidence and
  already-issued report documents/hashes remain intact.
- The account hard-halts and cannot admit entries, restart replay or clear the
  block through funding, recovery or manual policy-epoch review. Prior daily,
  drawdown, policy-epoch and high-water evidence is retained.
- Daily summaries explicitly report unavailable accounting totals/returns rather
  than zeros. The notice renderer can report this condition without disclosing
  request text or fabricating a financial result.

There is no unit-history rebuild/rearm command in this release. A corrected
account remains quarantined. Continuing a separate operational experiment requires
a **new virtual account** with separately declared funding/inventory and its own
selection/baseline; this does not repair or qualify the corrected account. This is
the completed invalidation option under C2, not another unnamed Phase 4 work item.

Corrections require quiescent order state, drained replay and reconciled financial
fills. Transfers also require pending entry orders to be cancelled/reconciled first and
block on uncertain commands or unposted fills. Neither path
may spend reserved cash, the manual tax reserve or inventory reserved by a sell.
A correction that would make current quantity/cost negative is rejected; this
bounded path does not restate intervening authoritative fills or invent inventory.
Backdated replacement facts are accepted within recorded account history; the
correction itself uses the current recording time and cannot regress state time.

## Local operator commands

Use a reviewed candidate image and a disposable local project for this demonstration.
The retained development stack is **not** upgraded by merging this change. Follow
the existing [upgrade procedure](operations.md#upgrade-and-recovery-boundary) before applying its
migration to retained state. The [C1 runbook](phase-4-continuous-paper.md) covers
account creation, funding, collector/runner separation and operational selection.

Stop the account's runner before administration; it owns the same writer lock.
Read its current version with `aca paper status --account UUID`. Store request JSON
in a private local file and mount it read-only for the CLI, or copy it to a
container's bounded `/tmp`. Do not put secrets in a request or its reason.

Example inbound virtual BTC transfer (replace UUIDs, version, timestamp and evidence
reference with the reviewed record):

```json
{
  "schema_version": 1,
  "request_id": "bce4cc95-aa0d-46db-b180-126b0ce3fa26",
  "actor": "2a6f1d1f-d0ef-44e3-9fc0-f187e05b0e43",
  "expected_version": 2,
  "evidence_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "reason": "Record declared owner wallet transfer into virtual account",
  "external_reference": "owner-wallet-transfer-001",
  "flow": {
    "kind": "transfer",
    "asset": "BTC",
    "quantity": "0.001",
    "occurred_at": "2026-10-19T12:00:00Z",
    "valuation": {
      "asset": "BTC",
      "bid_gbp": "60000",
      "observed_at": "2026-10-19T12:00:00Z",
      "source": "reviewed-GBP-valuation-reference"
    }
  }
}
```

```sh
aca paper transfer --account UUID --input /tmp/transfer.json
aca paper accounting --account UUID --after-version 0 --limit 20
aca paper journal --account UUID
aca paper audit --account UUID
```

For an outbound transfer, use a negative quantity, a new request/reference and the
current state version. GBP valuations must be exactly one; missing valuations,
financial floats, duplicate JSON keys and naive timestamps fail. Event time cannot
be in the future or precede current state. References use at most 120 plain
identifier characters. Exact retries reuse **all** original request fields;
a new ID with an existing reference is a conflict.

For a correction, replace `external_reference` and `flow` with `target_journal`
(the original journal UUID from the transfer/fund result or journal CSV) and
`replacement`. The replacement uses the same flow structure; `kind="cash"` is
admitted only for GBP contributions/withdrawals. Its event time is the corrected
historical fact; recording time comes from PostgreSQL, with no CLI override.

```sh
aca paper correct --account UUID --input /tmp/correction.json
aca paper status --account UUID
aca paper performance --account UUID
aca paper daily --account UUID --day 2026-10-19
```

`audit.complete` means the financial event/journal history reconstructs; it is not
an assertion that corrected investment units are valid or that a gate passed.
Read `state.accounting_invalidated` and the explicit invalidation in the reports.
The accounting history query is bounded/cursor-paginated. Runtime has SELECT/INSERT
only; deferred database seals require the event, operator decision and both
correction postings to commit together. UPDATE/DELETE/TRUNCATE remain prohibited.

## Verification and acceptance evidence

The [focused suite](../tests/test_accounting_operations.py) covers all three assets,
both transfer directions, independent GBP/basis/units/P&L/TWR fixtures, stale or
missing valuations, source conflicts, cash and inventory reservations, unposted
fills, uncertain commands, backdated corrections, replacement chains, immutable
prefixes, failed commits, lost-response retries and fresh writer recovery. Its
financial property uses 1,000 deterministic generated examples.

The [packaged CLI worker](../tests/accounting_cli_worker.py) creates a new virtual
account, records £250 and a BTC round trip as owner transfers, verifies zero P&L,
corrects the opening transfer to £240, runs two recoveries, and checks the invalid
reports, immutable history and empty order list. CI runs it against its disposable
Compose stack. No collector, notification destination or exchange key is used.

Reproduce the supported checks:

```sh
.venv/bin/python scripts/ci_database.py -q tests/test_accounting_operations.py
.venv/bin/python scripts/ci_database.py --linux -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/mypy
python3 scripts/restore_drill.py
```

Exact results, source/package hashes and the disposable smoke result are retained
in [C2 evidence](evidence/phase-4-accounting.json). The existing restore regression
still has its documented empty replay/notice/strategy/accounting-operation tables;
it does not close C3's populated restore condition or full T25.

C1 and C2 are complete with their linked evidence. **C3 and C4 remain the only
fixed Phase 4 packages.** Research candidates remain rejected, the holdout stays
closed, and qualifying paper days/episodes remain zero.
