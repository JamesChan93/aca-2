# Implementation status

Updated 19 October 2026 for release candidate 0.4.6. This describes engineering
capabilities and missing evidence; it is not a gate approval.

**Phase 4 still has coding and safety validation work. The Phase 5 56-day
observation period has not started.** Local demos, historical replay, passing tests
and short maintenance runs do not count as qualifying signal-driven paper trading.

| Area | Implemented increments | Remaining work or evidence |
| --- | --- | --- |
| Foundation and data | Paper-only package, restricted PostgreSQL roles, migrations, containers, public Kraken metadata/books/trades/candles, persisted provenance and replay | Sustained resource/retention qualification and the remaining operational fault trials |
| Research | Shared deterministic indicators and strategies, audited datasets, cost-aware simulation and recorded trials | All 20 evaluated candidates rejected; no qualifying edge, frozen candidate or opened final holdout |
| Accounting and risk | GBP journal/quantities, contributions/withdrawals, unitisation, reservations, Decimal sizing, independent dispatch controls and durable loss latches | Operational transfers/conversions/corrections, tax inventory/reserve workflows and complete safety evidence |
| Paper execution | Durable local venue, partial fills, cancellation races, protective sells, uncertainty/recovery and ordered public replay | Continuous signal scheduling, measured fill/latency calibration and remaining process/storage/recovery trials |
| Reports | Audited original-time valuations, bounded historical performance, flow-neutral TWR and bounded MWR | Complete daily boundary coverage, owner-wide tax reporting and extended-period resource qualification |
| Notifications | Optional Discord transition alerts, durable retries, daily provisional summaries and current-state critical repeats | Real webhook integration, latency/reliability qualification, notification metrics/health and external heartbeat |
| Operations | Local health/metrics, private backups before upgrades and documented procedures | Off-host encrypted backups, WAL recovery, restore drill and demonstrated RPO/RTO |

The Phase 4 exit is **G2: the applicable safety suite, deterministic replay,
reconstructable balances and no enabled Kraken order writer**. Tests implemented so
far are evidence for individual increments. They do not close unimplemented or
undemonstrated scenarios.

Phase 5 starts only after the engineering review accepts a complete paper workflow
and an explicitly selected candidate. It requires at least **56 calendar days** and
**10 completed signal-driven position episodes**, supported lifecycle replay,
reliability/recovery evidence and the research/economic gates. Extend observation
if there are too few trades; do not change a strategy simply to reach the count.

G2–G6 remain incomplete. A failed research or economic gate can leave this system
in paper mode indefinitely. No test count, contribution, elapsed time or restart
promotes a strategy or authorises real trading. Private Kraken reads, live writer
implementation, paid deployment and live activation remain separately authorised.
