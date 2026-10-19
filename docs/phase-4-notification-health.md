# Phase 4 increment — local notification monitoring

Release candidate 0.4.7, 19 October 2026. Adds a health/Prometheus surface to the
explicitly started paper notification worker. No new database revision or dependency
is required; schema remains `0010_scheduled_notices`. Phase 4/G2 remain incomplete
and the Phase 5 observation period has not started.

## Start and inspect

Use the existing [webhook setup](phase-4-notifications.md) and
[scheduled-notice procedure](phase-4-scheduled-notifications.md). `notifications run`
now starts a local health server at the configured health host/port, default 8000.
Run it in its own optional notification container, rather than through `exec app`:
the foundation application already owns that port in its own container. Failure to
bind stops startup before a send. The default Compose stack does not start this
worker, mount a webhook or deliver messages. The optional profile's default command
still only displays help.

For an already configured destination, replace `ACCOUNT_UUID` and start explicitly:

```sh
docker compose -f compose.yaml -f compose.notifications.yaml --profile notifications run --name aca-paper-notifier --rm --no-deps notifications aca notifications run --account ACCOUNT_UUID --webhook-file /run/secrets/discord_webhook --duration 300
```

While that named container is running, from another terminal:

```sh
docker exec aca-paper-notifier aca health --url http://127.0.0.1:8000/health/notifications
docker exec aca-paper-notifier aca notifications status --account ACCOUNT_UUID
docker exec aca-paper-notifier python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/metrics").read().decode())'
```

No ports are published. `aca health` only accepts loopback HTTP health routes;
HTTP routes accept GET only and cannot configure a destination or issue commands.
The notification override enables a container health check, with a 30-second start
period. It keeps `restart: no`. Unhealthy status does not restart delivery, re-enable
a destination or affect a paper execution decision.

`status` remains available without a secret or network egress. Its schema-1 response
adds `as_of` from PostgreSQL and `scheduled_queues`, with count/oldest timestamp for
each pending scheduled kind. Existing `backlog` remains the transition count and
`scheduled_backlog` remains the sum of daily/critical pending notices.

## Readiness and failure semantics

**A42 — cached notification monitoring has no command authority.** A separate
connection pool performs runtime/schema checks and a read-only REPEATABLE READ
notification snapshot. It takes neither delivery nor execution ownership. The
observer uses a separate pool from the delivery writer and daily report builder.
An observation has a four-second asynchronous deadline and the existing five-second
per-statement database timeout. Polls wait five seconds after completion, without
overlap or catch-up bursts. Full-history aggregate query costs still require
extended-period resource qualification.

HTTP handlers only read the in-memory sample. Scraping never queries PostgreSQL,
prepares work or sends HTTP. The existing bounded HTTP parser, connection limit and
shutdown rules apply. The observer stores no heartbeat rows or financial records.
Its failures produce fixed-code local logs containing exception type only; failed
observations cannot recursively create an outbound alert.

The worker records an in-memory monotonic heartbeat after each completed
prepare/delivery tick, including idle/rate-limited ticks. This is separate from the
last successful Discord message. A quiet but operating worker can be healthy.
After a restart, readiness requires fresh observations and a completed tick.

On this worker `/health/ready` and `/health/notifications` return 200 only when:

- A runtime/schema snapshot succeeded within ten seconds.
- A worker tick completed within 30 seconds, without shutdown or worker fault.
- The destination is configured/enabled and the last daily check has no evidence error.
- No retained abandoned/dead-letter delivery events exist for the account.

Otherwise they return 503 with a fixed reason. The response includes a safe sample
of counts, UTC timestamps, attempt count and retry deadline, never a webhook,
destination hash, incident text, message body or Discord message ID. Retained terminal
failures conservatively keep readiness degraded even after reconfiguration. There
is no acknowledgement/clearing workflow in this increment; inspect the journal and
record review separately. Delivery's existing retry/cursor policy continues.

A rate-limit wait is a functioning worker state, so it does not itself force 503.
Use the wait/backlog metrics to detect delayed delivery. Readiness is not a claim
that an alert met its 60-second delivery objective or that the network is reachable
at the next attempt. `/health/live` means the HTTP loop responds. `/health/trading`
always returns 503 and `aca_trading_ready` remains zero.

On observer failure, freshness/readiness clear immediately. Cached counts stay
visible with freshness zero; unknown timestamp ages/waits are -1. During an observer
stall, cached evidence becomes stale after ten seconds. A stalled delivery loop is
independently stale after 30 seconds while the observer/server can still respond.
The server is in the same Python event loop: a total event-loop or host failure
requires an external monitor, which is still unimplemented.

## Metrics and interpretation

The private registry has no account, order, incident or destination labels. Only
fixed queue/event/result labels are used.

| Metric | Meaning |
| --- | --- |
| `aca_notifications_ready` | Local notification readiness, 0 or 1 |
| `aca_notifications_worker_fresh` | Completed worker tick within 30 seconds |
| `aca_notifications_monitoring_fresh` | Valid runtime/schema/queue evidence within ten seconds |
| `aca_notifications_destination_enabled` | Configured/enabled in the last sample |
| `aca_notifications_pending{kind}` | Last observed transition/daily/critical backlog; -1 before any sample |
| `aca_notifications_oldest_age_seconds{kind}` | Oldest timestamp age, or -1 if absent/future/unavailable |
| `aca_notifications_delivery_events{kind}` | Retained delivered/retry/terminal-failure journal counts |
| `aca_notifications_current_attempts` | Current item's durable attempt count; -1 before a sample |
| `aca_notifications_retry_wait_seconds` | Known deadline remaining, or -1 if unavailable |
| `aca_notifications_last_delivery_age_seconds` | Age of last confirmed message, or -1 if absent/future/unavailable |
| `aca_notifications_monitor_samples_total{result}` | Process-local successful/failed monitoring observations |

**Transition event age is not queue waiting time.** The transition timestamp is the
source event's economic time and can be historical or move backwards during replay.
Scheduled ages use notice creation time. These measures do not establish detection
or delivery latency. Retained journal counts are gauges because they are database
totals, rather than counters initialised from a process's own observations. Sample
counters reset on process restart and are not application uptime statistics.

Ages/deadline waits advance from database sample time using local monotonic elapsed
time. Wall-clock reads in a scrape cannot shorten them. No financial float boundary
is introduced; floating-point values here are durations and monitoring values only.
Always evaluate cached queue metrics together with `monitoring_fresh`.

## Verification and limits

Tests cover independent observer/worker freshness, deadline/cancellation, read-only
snapshot isolation, owner/pool independence, queue kinds, retained failures,
two-hour throttle and durable disable visibility, fixed labels, redaction, HTTP/CLI
routes, no scrape-triggered I/O and optional worker cleanup. The run integration
uses HTTPX fixtures and isolated PostgreSQL; no real Discord or exchange request.
See the [verification record](evidence/phase-4-notification-health.json).

This does not provision Prometheus, a real webhook, an external heartbeat, paid
hosting or a qualifying strategy. Real destination integration, critical
detection/delivery objectives, 99.5% availability, process/disk fault trials,
resource/retention qualification and backup/restore targets remain unproven.
The [status register](status.md) records the remaining Phase 4 work.

API references: [SQLAlchemy read-only transaction options](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#setting-read-only-deferrable),
[Prometheus Python client registry](https://prometheus.github.io/client_python/registry/).
