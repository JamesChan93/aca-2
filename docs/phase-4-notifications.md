# Phase 4 increment — outbound paper alerts

This is the 0.4.5 milestone record. Release 0.4.6 adds
[scheduled notices](phase-4-scheduled-notifications.md); use its upgrade and current
queue-ordering instructions together with the webhook setup below.

Release candidate 0.4.5, 18 October 2026. This delivers the existing transactional
paper alert outbox through an optional Discord webhook. No destination is configured
by installation, migration or default Compose startup. It adds no strategy dispatch,
private Kraken access or exchange writer. Phase 4 and G2–G6 remain incomplete; all
twenty research candidates remain rejected and the final holdout stays unopened.

## Setup

Back up and stop application workers before upgrading the schema from
`0008_historical_risk` to `0009_notifications`. Migrate with the dedicated migration
role, then deploy 0.4.5 and verify runtime privileges and health. Older images refuse
this newer schema; do not roll an application back without a reviewed compatible
database path. Migration adds a notification journal and outbox indexes/uniqueness,
and does not alter financial postings. It refuses downgrade to preserve history.

Use your paper account UUID; the value below is an example. First inspect the
retained backlog without any webhook or network access:

```sh
docker compose exec -T app aca notifications status --account 6e04530b-9382-418d-9a24-331672f4016b
docker compose exec -T app aca paper alerts --account 6e04530b-9382-418d-9a24-331672f4016b
```

If outbound notifications are wanted, create a Discord incoming webhook yourself in
the intended channel. Deliver its URL privately into `.secrets/discord_webhook`;
never put it into a command argument, environment variable, TOML, issue or Git.
Keep `.secrets` mode 0700. For local Compose, use mode 0444 on this secret file inside
that private directory so the non-root container UID can read its secret mount,
as with the existing database secret files. The parent directory prevents other
host users from reading it. A native CLI process can instead use a 0600 file owned
by its user. The loader rejects symlinks, unsafe writable files and oversized input.

Only `https://discord.com/api/webhooks/<id>/<token>` and the explicit `/api/v10/`
form are supported. Queries, fragments, other hosts, user information and alternate
ports are rejected. The override supplies egress only to the notification container;
the default app/database retain their private network. Starting the profile with
`up` shows command help and cannot send. Delivery requires an explicit command:

```sh
docker compose -f compose.yaml -f compose.notifications.yaml --profile notifications run --rm --no-deps notifications aca notifications configure --account 6e04530b-9382-418d-9a24-331672f4016b --webhook-file /run/secrets/discord_webhook
docker compose -f compose.yaml -f compose.notifications.yaml --profile notifications run --rm --no-deps notifications aca notifications run --account 6e04530b-9382-418d-9a24-331672f4016b --webhook-file /run/secrets/discord_webhook --duration 300
```

`configure` records the operator action and a SHA-256 binding to the URL, without
making an HTTP request or storing the secret. It starts at version zero by default:
**all retained alerts are eligible for delivery**. An initial `--after-version N`
can explicitly exclude older account event versions, up to the current account
head. It is rejected once configured; later configuration cannot skip or rewind the
cursor. Changing/rotating a URL requires another explicit `configure`, which also
re-enables a disabled destination and resets its retry count. An existing known
rate-limit deadline is preserved even during reconfiguration.

`run --duration 0` runs until SIGINT/SIGTERM. Other durations are 1–86400 seconds.
An in-progress bounded request can finish after the requested duration. There is
no automatic restart policy or secret provisioning. Stop the optional worker to
stop sends; pending source alerts remain in PostgreSQL. `status` exposes the binding
hash, cursor, deadline, backlog, oldest pending event time, successful deliveries,
last delivery time, retries and retained terminal failure count.

## Delivery and failures

**A40 — independent append-only notification delivery.** One PostgreSQL session lock
per account serialises delivery and configuration. Its namespace differs from the
paper execution writer, so HTTP latency does not hold the execution lock. A process
that loses its database session stops; it cannot silently reacquire ownership.
Runtime retains SELECT/INSERT only and cannot update/delete notification history.

Each attempt commits an `attempt_started` event before HTTP. No transaction stays
open across the request. The result and cursor advance are committed together.
Original alert and financial rows are never edited. Ordering follows account event
version, including when original event timestamps are equal or go backwards during
historical processing. Only one alert is attempted per tick.

The sender uses `wait=true`, requires HTTP 200 plus a validated Discord message ID,
and stores only fixed reason codes, status and ID. It has no transport retry,
redirect following, environment proxy or response-body logging. TLS verification
remains enabled. HTTP timeouts are five seconds per operation with a ten-second
overall request deadline. Raw response data is bounded to 16 KiB. Oversized data
disables the destination; authentication failures, 404 and redirects also disable
it durably. A 404 disables even when its body is oversized. No response content,
headers, exception message or webhook URL is persisted in the journal.

Rate limits use the maximum supported `Retry-After`, JSON `retry_after` and exhausted
bucket `X-RateLimit-Reset-After` deadline. Numeric seconds and HTTP-date Retry-After
are supported. Fractional seconds round upwards to a microsecond. Deadlines persist
across restarts, and a successful last bucket slot delays the next alert too. A
missing 429 deadline falls back to 60 seconds. Invalid, negative, non-finite or
greater-than-seven-day deadlines disable delivery for operator review instead of
shortening an unrepresentable wait.

Timeouts, 408, server errors and unconfirmed success responses retry with exponential
backoff (5–300 seconds plus stable subsecond per-alert jitter), respecting larger
server deadlines. Every ambiguous attempt counts. After eight attempts the alert
is recorded as a dead letter and the cursor can advance; other permanent message
4xx errors are abandoned immediately. These failures remain queryable in the
append-only journal and terminal count. They never replay an order, reconcile a
fill, change a kill latch or release funds.

A crash after the send but before result persistence leaves `in_flight`, with a
precommitted retry deadline of at least 30 seconds. The next owner may retry that
notification. **Duplicate Discord messages are possible**: the webhook API provides
no exactly-once guarantee here. A confirmed, committed delivery is not resent.
Each message includes stable account, alert, event and paper state version IDs.
Raw incident text is excluded, categories use a safe display allowlist, mentions
are disabled, and URL/token values are registered with log redaction.

On a disabled destination, inspect `status` and local logs, verify/rotate the webhook
outside the application, then explicitly configure it again and run a short delivery
session. Do not erase source rows or notification history. Inspect dead-letter events
before deciding whether any manual notification is necessary. Historical terminal
failures remain visible; `run` exits nonzero when disabled or terminal failures exist.
Notification incidents also produce a local structured log event, without adding
another outbound alert that could recursively fail.

## Evidence and remaining work

Tests use HTTPX MockTransport and real isolated PostgreSQL. They exercise commit
visibility before transmission, no financial mutations, duplicate suppression after
restart, ambiguous retry, interrupted delivery, destination binding/cutoffs, durable
404 shutdown, two-hour throttling, bucket exhaustion, bounded attempts, malformed
input, redaction, owner conflicts and database failure before transmission. No real
Discord or exchange request is part of these tests. The hosted container smoke reads
an unconfigured destination without mounting a webhook.

This increment delivers existing transition alerts only. Five-minute unresolved
critical repeats, daily portfolio summaries, a notification Prometheus/health server,
external heartbeat setup, real destination integration, end-to-end alert-latency
qualification and a bulk dead-letter recovery workflow remain unimplemented. FIFO
backlogs can delay critical messages; the 60-second objective is not established.
There are no new qualifying paper episodes, research evidence or extended paper
days. Resource/retention, process/disk fault trials, backup/WAL disaster restore and
remaining accounting/tax workflows also remain outstanding.

Transport references: [Discord execute webhook](https://docs.discord.com/developers/resources/webhook#execute-webhook),
[Discord rate limits](https://docs.discord.com/developers/topics/rate-limits),
[HTTPX 0.28.1 async usage](https://github.com/encode/httpx/blob/0.28.1/docs/async.md).
See the [verification record](evidence/phase-4-notifications.json) for measured results.
