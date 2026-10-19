# Phase 4 — review of lost paper notifications

Release candidate 0.4.8, schema `0011_notification_reviews`. This increment adds an
explicit operator review of an `abandoned` or `dead_letter` notification. It changes
notification readiness after review; it grants no trading permission and does not
complete Phase 4/G2 or start Phase 5. No research candidate has qualified.

## A43 — preserve loss evidence and review one exact event

A terminal delivery failure remains in the append-only delivery journal. A separate
append-only review records its account, event number, exact event hash, failure kind,
operator UUID, resolution, evidence SHA-256 and database UTC review time. A composite
foreign key binds all four target fields to the journal; only terminal failure kinds
are admitted. Runtime has SELECT/INSERT, without UPDATE/DELETE/TRUNCATE or DDL.

The two resolutions are `manual_notification_confirmed` and `delivery_loss_accepted`.
The operator retains a private incident/review document and supplies its SHA-256.
This is an operator assertion: the application does not fetch that document, verify
its substance, authenticate the UUID as a separate identity or obtain a signature.
Local CLI/database credential access remains the administration boundary. Do not
put a person name, incident prose, webhook or token in a command or review document
stored by the application. The UUID and digest are sufficient references.

A review targets one event, never all events through a cursor or all failures for a
destination. An identical repeat returns the original record without inserting
another row; a different actor, resolution or evidence digest for that event is
rejected. Review time comes from PostgreSQL, cannot precede the target event, and
is not supplied by the operator. A new failure requires its own review.

The review acquires the existing per-account notification owner, so stop the optional
worker before reviewing. Lock contention fails promptly. This serialises reviews
with configuration and delivery; it is not exchange-side fencing or process-kill
recovery evidence. The review never invokes a sender or appends delivery state.

## Inspect and review locally

`failures` needs no webhook or egress. Replace `ACCOUNT_UUID` with the nominated
virtual account. It lists at most 100 terminal events in ascending journal order;
use the returned `next_after_number` as `--after-number` to read the next page.
Reviewed records remain in the list. Responses omit destination hashes, webhook
URLs, raw incident strings and message bodies.

```sh
docker compose exec -T app aca notifications failures --account ACCOUNT_UUID --limit 20
docker compose exec -T app aca notifications failures --account ACCOUNT_UUID --after-number EVENT_NUMBER --limit 20
```

Inspect the identified alert/notice and current paper risk state, resolve the
notification incident through the appropriate manual procedure, and retain the
private review evidence. Stop any optional worker before running the command below.
Copy the exact number/hash from the failure listing. Choose a stable operator UUID
and the resolution that reflects what happened, then substitute all placeholders:

```sh
docker compose exec -T app aca notifications review \
  --account ACCOUNT_UUID \
  --failure-number EVENT_NUMBER \
  --failure-hash EXACT_FAILURE_SHA256 \
  --actor OPERATOR_UUID \
  --resolution delivery_loss_accepted \
  --evidence-sha256 PRIVATE_REVIEW_DOCUMENT_SHA256

docker compose exec -T app aca notifications status --account ACCOUNT_UUID
```

The review changes neither the failed item's terminal status nor its scheduled
receipt. It does not send/requeue that item, advance/rewind a delivery cursor,
change attempts, shorten a persisted rate deadline, enable a disabled destination,
resolve an active critical condition, clear a portfolio halt or rearm execution.
An active critical condition still produces its independently scheduled reminder.
Use the existing explicit configuration procedure to repair a disabled destination;
that procedure also preserves known rate deadlines. Notification review is separate
from the audited portfolio policy-epoch review.

## Readiness and audit bounds

Status now exposes `terminal_failures`, `reviewed_terminal_failures` and
`unreviewed_terminal_failures`. Retained total failures never decrease. A healthy,
configured worker can regain notification readiness when all terminal failures
have valid reviews and the other [worker health checks](phase-4-notification-health.md)
pass. An unreviewed failure yields `unreviewed_terminal_failures`; a disabled
webhook, stale observer, stalled worker or daily-evidence fault still blocks readiness.

The existing `aca_notifications_delivery_events{kind}` gauge retains
`terminal_failures` and adds fixed `reviewed_terminal_failures` and
`unreviewed_terminal_failures` label values. No operator, event or evidence digest
becomes a metric label. Monitoring samples require the three failure counts to
balance. `/health/trading` continues to return 503.

Each status sample validates up to 10,000 retained review records and their hashes
before counting them. Unknown/malformed review contracts or an exceeded audit budget
fail observation and degrade cached health. This is a bounded review-prefix audit,
not a complete audit of the delivery journal's entire hash chain. The failure listing
validates each displayed terminal event and its optional review. Database query
statement limits and the observer's four-second deadline still apply. Extended
history/resource qualification, archival and an operational procedure for exceeding
the review budget remain outstanding; do not delete records to bypass a bound.

## Upgrade and evidence

Stop active workers, back up the database privately, build the reviewed image and
apply the additive migration with the migration credential. Restart application
processes on the matching image after migration. The earlier image rejects the new
schema; do not roll back it blindly or downgrade the append-only tables. Use the
reviewed restore procedure if schema-compatible recovery is unavailable.

Tests use isolated PostgreSQL and HTTPX fixtures only. They cover exact targeting,
repeat/conflicting reviews, account and owner scope, rate-limit/disable preservation,
scheduled dead-letter receipts and continuing critical repeats, rollback and audit
bounds, database restrictions, malformed review rejection, safe CLI/list pagination,
readiness/metrics and unchanged financial counts. See the
[verification record](evidence/phase-4-notification-review.json).

Real Discord integration, delivery/detection latency and uptime objectives, external
heartbeat, extended retention and restore/RPO/RTO remain unproven. The default stack
still has no Discord secret, worker or external messages. See the
[remaining work](status.md).

Database reference: [SQLAlchemy composite foreign keys](https://docs.sqlalchemy.org/en/20/core/constraints.html#defining-foreign-keys).
