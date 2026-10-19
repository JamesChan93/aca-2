# Phase 4 — Manual paper tax-cash reserves

Release candidate 0.4.9 adds an audited operator workflow for the existing
`tax_reserve_gbp` field. It earmarks owned virtual GBP; it does not estimate tax,
recognise a tax liability, pay HMRC or create a bank/exchange transfer. An earmark
is still an asset in NAV. It reduces cash available for entries and withdrawals.

The amount comes from the operator's own review. Tax inventory, owner-wide
matching, tax estimates, actual payments and household tax reporting remain
unfinished. A zero reserve is not evidence of zero tax. Research candidates,
strategy promotion and the Phase 5 observation clock are unchanged.

## Review and set an amount

Use these commands only with the reviewed 0.4.9 image and schema
`0012_tax_cash_reserves`. A retained 0.4.8 container cannot run the new commands.
This increment's validation uses disposable test stacks, not a retained-account
upgrade. Follow the existing backup/upgrade procedure before deploying it.

Read current state without acquiring the execution writer:

```sh
docker compose exec -T app aca paper tax-reserves --account ACCOUNT_UUID --limit 20
```

The response includes `state_version`, total GBP, order-reserved GBP, the manual
tax cash reserve and free cash, plus an ascending page of immutable decisions.
`--after-version N` reads the next page; `next_after_version` is null at the end.
It uses one read-only repeatable-read snapshot and can run beside a paper writer.

Stop the account's maintenance writer before changing its reserve. For an enrolled
account, stop the public collector, drain recorded public evidence with the existing
consume procedure and reconcile all fills first. An active writer, unread source
or unposted authoritative fill makes the command fail. It never cancels an entry
or protective exit to make room for a reserve.

Keep a private review document describing the chosen amount and its limitations.
Compute its SHA-256 locally and substitute the identifiers, state version and digest:

```sh
docker compose exec -T app aca paper tax-reserve \
  --account ACCOUNT_UUID \
  --request-id NEW_REQUEST_UUID \
  --actor OPERATOR_UUID \
  --expected-version VERSION_FROM_QUERY \
  --amount-gbp "25.00" \
  --evidence-sha256 REVIEW_DOCUMENT_SHA256 \
  --reason "Reviewed manual paper tax cash allocation"
```

The amount is an **absolute target**, not an increment. Decimal text is required;
binary floats, non-finite values, negative amounts and unsupported precision fail.
The target cannot exceed total GBP minus all outstanding buy reservations,
including uncertain commands. If it cannot be funded, record/review the shortfall
outside this workflow; there is no automatic sale, top-up or partial allocation.

The request binds the exact current state version. If state changes, inspect it
again and create a new reviewed request. No force flag or automatic version refresh
exists. A new request for an unchanged amount is rejected. To release a reserve,
use the same workflow with a smaller amount or `"0"` and fresh review evidence.

Retain the complete request for recovery. Repeating **the same** account/request ID,
actor, expected version, amount, digest and reason returns the original decision
with `inserted=false`. This also works after a later reserve decision: the old
amount is not applied again. The response separately states the current reserve
and state version. Changing any request field while reusing its ID is rejected.

The CLI takes decision time from PostgreSQL in UTC. There is no CLI clock override.
The deterministic simulation API accepts an injected UTC clock for offline tests.
Naive or regressing timestamps fail. The reason must be 12–240 printable, trimmed
characters; keep credentials and personal information out of it.

## Financial and safety semantics

One existing account writer serialises the operation. The decision, versioned
portfolio event and local alert append in one transaction. A commit failure rolls
them all back. A lost response after commit is resolved with the exact request ID.
No network command is involved.

Only `tax_reserve_gbp`, the event version and event time change. There is no journal,
asset movement, fee, investment flow or new unit issuance. NAV, unit price, sizing
equity, high-water marks and daily baselines keep their existing meaning. Sizing,
independent portfolio checks, dispatch and withdrawals already use free cash after
both order holds and the tax reserve. Existing queued approvals become stale after
the version change; they are never silently renewed.

A reserve can be raised or released during a halt or unresolved command. It cannot
clear the halt, incident, reservation or reconciliation requirement, and cannot
make a consumed command dispatchable. Risk-reducing exits and exchange-simulated
protective orders remain available under their existing rules. Future fills still
require actual fee/balance reconciliation; an earmark is not a guaranteed loss or
liquidity bound.

Recovery preserves the reserve. Original-time replay applies the decision on its
control timeline. Historical performance reconstructs reserve changes from the
same evidence and reports each endpoint's `manual_tax_cash_reserve_gbp`. It does
not count them as returns, flows or tax costs. Current performance also exposes
the manual amount while `tax_reserve_estimate_gbp` remains null. Daily notification
tax estimates remain unavailable; the manual allocation does not complete them.
Historical endpoint amounts are bounded by their recorded source time. Before
the next public envelope applies a decision, use `tax-reserves` for the current
operational amount rather than treating the older endpoint as current cash.

## Evidence, migration and limits

Migration `0012_tax_cash_reserves` adds `paper_tax_reserves` and deferred pairing
triggers. It does not rewrite previous events, checkpoints or journals. Each
decision has a unique account/request ID and account/event version, links to that
event, and stores the previous/new amounts, operator assertion, reason and review
digest. UPDATE/DELETE/TRUNCATE remain denied or trigger-rejected, including for
ordinary migration-owner operations. A missing/mismatched event or decision fails
the transaction; full-prefix audits verify the typed decision and state transition.
Legacy full-state records remain readable without permitting a reserve change
through that older format.

The operator UUID is a local assertion, not an authenticated identity or signature.
The evidence digest is a reference: the application does not fetch the document,
verify its substance or decide that the amount is legally correct. The local
runtime credential remains the administration boundary. These decisions provide
no future live activation authority.

Each account is limited to 10,000 decisions; exhaustion rejects new requests until
a reviewed archive procedure exists. Query pages are limited to 100 rows. Full
audit/report budgets still apply and have not been qualified for extended operation.
The transaction pattern follows the documented
[SQLAlchemy asyncio connection lifecycle](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).
No dependency or base-image update is included.

Tests include 1,000 deterministic generated financial cases, exact retries,
conflicts, spending limits, outstanding/unknown commands, unposted fills,
approval invalidation, protective exit availability, halt/recovery persistence,
rollback/lost-response boundaries, immutable SQL pairing, CLI/database time,
read-only pagination, original-time returns and a populated 0011→0012 upgrade.
The fresh-cluster restore fixture now includes a manual reserve and its evidence.
These are partial evidence for T00, T09, T14–T18, T25, T29 and T32, not completion
of those groups or tax-law validation.

Measured results are in [the evidence record](evidence/phase-4-tax-cash-reserve.json).
Phase 4, G2–G6 and the research/economic gates remain incomplete. Phase 5 has not
started. No private Kraken request, exchange writer, Discord send or paid deployment
is required by this workflow.
