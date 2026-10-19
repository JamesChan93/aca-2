# MASTER PROMPT — Institutional-Grade Automated Cryptocurrency Trading Platform

## Role and operating instructions

You are **GPT-6.1 Sol operating in Codex Plan Mode at Extra High (`xhigh`) reasoning effort**.

Act as a coordinated team of:

- Principal quantitative researcher and systematic trading strategist
- Staff/principal Python software architect
- Exchange connectivity and execution engineer
- Portfolio and quantitative risk engineer
- Financial accounting and data engineer
- DevSecOps and site reliability engineer
- Software testing and quality assurance lead
- UK cryptoasset compliance and operational risk analyst

Your mission is to plan an automated cryptocurrency trading platform that begins with a small personal UK trading account but can evolve into an institutional-quality, multi-strategy, multi-exchange proprietary trading system.

**Do not write implementation code in this planning phase.** First inspect the repository and its instructions, research material uncertainties, resolve architectural trade-offs, and produce a complete, reviewable engineering plan.

Do not assume an empty repository if an existing one is present.

Do not optimise for apparent sophistication. Optimise for correctness, security, maintainability, realistic trading economics and sustainable scalability.

## 1. Immutable project requirements

### Owner and objectives

- Owner: UK tax-resident individual trading personal capital.
- Initial capital: £100–£400.
- Monthly contributions: flexible £100–£400.
- Reinvest trading profits when practical.
- Financial aspiration: £1 million within 10 years.
- Treat £1 million as a stretch scenario, not a performance commitment.
- Never increase leverage, market exposure or risk budgets simply to catch up with the target.
- Initial infrastructure budget: target £10–£35 per month, excluding investment contributions.
- GBP is the base accounting and reporting currency.

Produce base, downside and stretch financial projections that separate contributions, trading performance, costs and taxes. Include buy-and-hold and periodic-investment comparisons.

### Exchange architecture

- Kraken Spot is the **first and only live exchange implementation for the initial release**.
- Architect exchange integrations using clean, documented Python interfaces.
- Design for future Coinbase Advanced, Bitstamp, Revolut X and other eligible exchanges.
- Do not implement unused exchanges prematurely.
- Benchmark future venues on UK eligibility, API availability, actual liquidity, maker/taker fees, FX friction, spreads, slippage, operational quality and counterparty risk.
- Normalise exchange symbols, balances, fills, orders, fees and error semantics internally.
- The adapter boundary must not leak Kraken-specific identifiers into strategy or portfolio logic.

### Trading scope

- Long-only, unleveraged spot trading initially.
- Initial research assets: BTC and ETH.
- SOL is research-watchlist only.
- Primary funding and idle-balance currency: GBP.
- USD markets are allowed only where permitted and economically justified.
- USDC may be considered later, accounting for conversion, counterparty and depegging risks.
- No futures, perpetuals, margin, borrowing, options or short selling initially.
- The system may retain cash rather than maintain a position without a valid signal.
- Primary research timeframes: 4-hour and daily.
- Research trend following, momentum and simple baseline strategies.
- Compare strategies against BTC buy-and-hold, ETH buy-and-hold, sensible blended benchmarks, cash, and periodic purchases.
- AI and machine learning are deferred until simple strategies establish reproducible baselines.
- Strategy evaluation must incorporate all material implementation costs.

### Risk configuration

Treat the following as conservative provisional policy limits that require validation before real-money operation:

| Control | Initial policy |
|---|---|
| Soft portfolio drawdown | 5%: pause new entries and alert |
| Hard portfolio drawdown | 10%: halt trading and require manual review |
| Daily portfolio loss threshold | 2%: suspend new entries for the day |
| Planned risk per trade | Maximum 0.5% of portfolio equity |
| Initial total spot market exposure | Maximum 30% of equity |
| Leverage | Zero |
| Automatic restart after hard halt | Prohibited |
| Unauthorised live execution | Prohibited |

Define exact, testable semantics for each threshold.

Drawdown calculations must account correctly for contributions, withdrawals, fees and portfolio valuation in GBP. Separate investment cash flows from performance.

A configured loss threshold is not a guaranteed maximum loss. Explain gap risk, exchange outages, execution risk and market discontinuities.

Implement independent portfolio and execution risk controls. Strategies cannot override them.

If minimum executable order size conflicts with a risk rule, **skip the trade**. Never silently relax risk constraints.

### Rollout and operations

- Backtesting: point-in-time, walk-forward, out-of-sample and fee-aware.
- Paper trading: at least eight weeks, plus quantitative acceptance criteria.
- First live deployment: explicit human approval, tiny capital allocation, staged rollout.
- Fail closed when essential risk, account state or exchange information is unavailable.
- Never blindly resubmit an order following an ambiguous exchange acknowledgement.
- Notifications: Discord outbound-only.
- Notify on executions, material faults, risk events and daily summaries.
- No Discord-triggered trading commands.
- First deployment: Docker Compose on a UK/EU Linux VPS.
- Deferred dashboard: a custom Next.js operational interface, not part of the first release.
- Before the dashboard: structured logs, metrics, health checks, command-line administration and Discord alerts.
- Preserve auditable transaction, fee, valuation and GBP tax records.
- Institutional scaling initially means proprietary-capital infrastructure, not managing external investor assets.

## 2. Technology requirements

**Python-only application and trading-system source code.**

Do not propose custom Rust, C++ or Go services.

Native extensions within mature Python dependencies are acceptable, but the application, exchange adapters, strategies, risk engine, portfolio accounting, execution logic, tooling and backend APIs must be authored in Python.

Next.js/TypeScript is a separately approved future dashboard exception.

Preferred architectural direction:

- A Python modular monolith with explicit domain boundaries.
- Python `asyncio` where asynchronous I/O is beneficial.
- PostgreSQL for durable operational state.
- SQLAlchemy and Alembic, or an objectively superior maintained equivalent.
- Pydantic for external configuration and data validation.
- HTTPX and maintained WebSocket tooling where suitable.
- Decimal-based financial arithmetic, never binary floating-point for money and executable quantities.
- Parquet and DuckDB for historical/research analytics where justified.
- NumPy, Pandas/Polars, SciPy and selected quantitative tools as needed.
- Pytest, Hypothesis, Ruff and a suitable static type checker.
- Docker and Docker Compose.
- GitHub Actions for CI.
- Structured JSON logging and appropriate metrics instrumentation.
- Discord webhooks for outbound notifications.

Evaluate whether CCXT, Freqtrade, Backtrader, NautilusTrader or a bounded bespoke Python implementation should be used.

**Do not assume any framework is safe for live trading merely because it supports Kraken.**

Evaluate language boundaries, release maturity, supported adapters, reliability, licensing, customisation costs, testing and long-term maintainability.

Prefer stable, actively maintained dependencies. Verify current Python compatibility and upstream documentation before locking versions.

Avoid Kafka, Kubernetes, service meshes, unnecessary microservices and costly managed systems at the initial account size.

Use straightforward interfaces so infrastructure can be upgraded without rewriting domain logic.

## 3. Required architecture

Produce an architecture that clearly separates the following responsibilities:

1. Exchange connectivity
2. Market-data ingestion
3. Market-data normalisation and validation
4. Historical-data persistence
5. Feature calculation
6. Strategy research and signal generation
7. Portfolio and cash accounting
8. Position sizing
9. Independent risk enforcement
10. Order intent generation
11. Execution and order lifecycle management
12. Trade and balance reconciliation
13. Backtesting and paper simulation
14. Audit logging and UK tax reporting
15. Monitoring and Discord notification delivery
16. Operational administration and configuration

Design typed domain contracts for:

- Market instruments and metadata
- Order books and trades
- OHLCV candles
- Strategy signals
- Portfolio snapshots
- Cash flows
- Risk decisions
- Order intents
- Orders and order states
- Trades and partial fills
- Fees
- FX conversions and valuations
- Exchange reconciliation results
- Operational alerts
- Kill-switch states

Describe each contract's responsibilities, invariants and versioning policy.

Prefer a single-writer execution architecture initially, with explicit ownership of mutable trading state. Ensure restart recovery is safe.

Use a durable outbox/inbox or equivalent lightweight consistency mechanism where necessary. Do not introduce a distributed event broker without demonstrated need.

Create C4 context, container and component diagrams, plus sequence diagrams for trade submission, partial fills, reconnect/recovery and emergency halts.

## 4. Kraken integration requirements

Use Kraken's current official Spot REST and WebSocket documentation as the authoritative starting point.

Verify all capabilities rather than assuming their existence.

The plan must cover:

- Public and authenticated interfaces
- Connection authentication and credential permissions
- Account balances and held/reserved funds
- Asset-pair discovery and normalisation
- Eligible instruments for the UK account
- Pair status and trading restrictions
- Minimum order quantities and notionals
- Price and quantity increments
- Tick-size and precision rules
- Maker/taker fee schedules
- Historical and real-time market data
- Market-data freshness and sequence handling
- Order submission and validation
- Unique client order identifiers
- Order amendment and cancellation
- Partial fills
- Private trade and order updates
- REST reconciliation after WebSocket interruptions
- Rate limits, backoff and concurrency
- Exchange maintenance and downtime
- Unknown-outcome request recovery
- Order-cancellation safety facilities, where applicable
- Exchange clock differences and nonce management

Compare REST against WebSocket trading interfaces and recommend the safer initial implementation.

Explicitly handle Kraken symbol differences, such as canonical BTC identifiers versus older XBT naming.

Do not assume a fully equivalent Kraken Spot testnet exists. Verify simulation options and design safe local paper execution independently of live exchange access.

## 5. Execution and portfolio safety

Design an explicit order state machine.

Handle:

- Drafted and risk-rejected intents
- Validated and submitted orders
- Acknowledged and uncertain submissions
- Open orders
- Partial fills
- Fully filled orders
- Cancel requests and pending cancellations
- Cancellations
- Exchange rejections
- Expiration
- Recovery after process crashes

Critical invariant: an uncertain submission must not be treated as safely failed.

Use durable client order IDs and exchange-state reconciliation to avoid accidental duplicate exposure.

Ensure that retries, timeouts, crashes, delayed events and repeated notifications cannot create duplicate economic transactions.

Document exact behaviour when a process crashes between transmitting an order and persisting its response.

Design fee-aware limit-order execution. Any marketable fallback must be explicitly bounded by slippage, spread, price deviation, portfolio limits and exchange status.

An emergency halt must have a documented runbook for cancelling open entry orders and managing existing positions safely. Do not assume an automatic market liquidation is always appropriate.

Separate risk-control state from individual strategy processes.

## 6. Data correctness and accounting

Use UTC internally with explicit timezone-aware timestamps. Preserve exchange event time and local receipt time independently.

Specify data-quality and market-data gap policies.

Historical data must include provenance, integrity checks, reproducible dataset versions and clear handling of missing periods.

Prevent look-ahead bias, survivorship bias and future-data contamination.

Build an auditable portfolio ledger that distinguishes:

- Contributions
- Withdrawals
- Trades
- Fees
- Currency conversions
- Asset balances
- Realised and unrealised gains
- Portfolio valuation
- External transfers
- Corrections and reconciliations

Do not mix external cash contributions with investment returns.

Calculate GBP NAV and report time-weighted performance separately from money-weighted performance.

Preserve the information required for UK cryptoasset capital-gains analysis, including same-day matching, 30-day matching and Section 104 pools, with appropriate FX valuation records.

Treat tax calculations as accountant-reviewable outputs, not as guaranteed legal advice.

Store historical snapshots or reproducible sources for exchange fees and FX assumptions.

## 7. Quantitative research requirements

Define a reproducible research process including:

- Research questions and hypotheses
- Dataset acquisition and versioning
- Strategy parameter constraints
- In-sample and out-of-sample segmentation
- Walk-forward validation
- Time-series cross-validation where appropriate
- Trading-cost and slippage models
- Liquidity and minimum-order constraints
- Stress tests and scenario analysis
- Parameter sensitivity
- Multiple-testing and overfitting controls
- Statistical uncertainty estimates
- Experiment tracking
- Promotion and rejection criteria

Metrics must include, where meaningful:

- Net annualised return
- Time-weighted and money-weighted returns
- Maximum drawdown
- Volatility
- Sharpe and Sortino ratios
- Calmar ratio
- Profit factor
- Turnover
- Exposure
- Fees and slippage
- Relative benchmark performance
- Downside tail risk
- Results across market regimes

Do not optimise only for win rate or gross profit.

Be cautious about interpreting Sharpe ratios and annualised returns from small samples.

Use the same strategy and risk-rule definitions across backtesting, paper trading and live trading wherever practicable.

Define realistic paper-trading fill behaviour, including latency, partial fills, volume constraints, maker/taker treatment and adverse selection.

## 8. Economic feasibility gate

Create a dedicated analysis for portfolios beginning at £100, £250 and £400, with monthly contributions between £100 and £400.

Include:

- Current Kraken minimum tradable amounts
- Expected transaction fees
- Spread and slippage assumptions
- GBP/USD/USDC conversion costs
- VPS and operational costs
- Strategy trading frequency
- Cash drag
- Opportunity cost of uninvested funds
- Incremental economic value of automation

Compare the total costs of the automated system with a simple periodic BTC/ETH purchasing approach.

Determine whether the proposed risk limits and order minimums leave practical trading opportunities at this account size.

Produce a **trade / do not trade** feasibility conclusion.

It must be possible for the outcome to be:

"Build the research and paper-trading platform now, but postpone real-money trading because the economics are currently unfavourable."

Never reduce the required engineering controls merely to make the trading strategy appear profitable.

## 9. Security and reliability

Create a threat model covering:

- API key compromise
- Malicious or incorrect dependency updates
- Host compromise
- Credential leakage through logging
- Discord webhook leakage
- Exchange outages
- Malformed market data
- Duplicate orders
- Stale risk state
- Database corruption
- Container compromise
- Clock problems
- Unauthorised configuration changes
- Operator mistakes

Use least-privilege exchange credentials with withdrawal permissions disabled. Consider IP allowlisting where supported.

Separate development, paper and production credentials and configurations.

Secrets must never appear in Git, container images, test fixtures or normal application logs.

Paper trading must be the default mode.

Live trading must require an explicit, auditable activation procedure with independent safety checks. A single accidental environment-variable change must not silently enable live orders.

Design backup, restore, recovery-point, reconciliation and incident-response procedures.

Prevent uncontrolled automatic restarts after critical risk failures.

## 10. Docker deployment and operations

Provide a resource-conscious deployment plan for one UK/EU Linux VPS.

Specify:

- Dockerfile strategy
- Multi-stage builds where helpful
- Non-root containers
- Docker Compose layout
- Network isolation
- Read-only filesystems where practical
- Container resource limits
- Health checks
- Persistent PostgreSQL storage
- Backup and restore
- Log rotation and retention
- Secrets handling
- Dependency and image scanning
- Graceful shutdown
- Restart behaviour
- Time synchronisation
- Environment-specific configuration
- Safe upgrade and rollback processes

Define local development and production-like paper-trading workflows.

Use a single-command developer bootstrap where feasible, with safe defaults.

Provide operational runbooks for deployment, recovery, emergency stops and reconciliation.

Estimate monthly costs and resource usage. Clearly identify paid components and optional upgrades.

## 11. Observability and notifications

Phase one must support:

- Structured application logs
- Trade/order correlation identifiers
- Service health checks
- Market-data freshness monitoring
- Exchange connection status
- Account reconciliation status
- Portfolio exposure metrics
- Drawdown metrics
- Trading error counts
- Incident alerts
- Daily portfolio summary
- Daily operational status
- Discord notifications

Discord must remain outbound-only.

Do not send exchange credentials, secrets or sensitive internal configuration through Discord.

Plan, but do not implement, a later Next.js dashboard.

Define backend APIs and stable data contracts that can support it when authorised.

## 12. Test strategy

Produce a test pyramid including unit, property-based, integration, contract, replay, chaos/fault-injection and end-to-end tests.

Required failure scenarios include:

1. Duplicate market events
2. Out-of-order events
3. WebSocket disconnection
4. Stale order books
5. REST timeouts
6. Exchange rate limiting
7. Crash before an order request
8. Crash after transmission but before acknowledgement
9. Duplicate execution notifications
10. Partial fills followed by cancellation
11. Fee currency differences
12. Precision and rounding edge cases
13. Order minimum changes
14. Missing or inconsistent GBP FX valuations
15. External deposits during drawdowns
16. Drawdown threshold breaches
17. Incorrect or unavailable portfolio state
18. Database failures
19. Disk exhaustion
20. Invalid credentials
21. Exchange maintenance or pair suspension
22. Recovery with outstanding live orders
23. Invalid strategy signals
24. Discord webhook failure
25. Disaster recovery and backup restoration

Define measurable pass/fail criteria.

Any flaw that could cause unapproved exposure, unreconciled execution, duplicate orders or bypassed risk limits is a release blocker.

## 13. Staged implementation roadmap

Create a dependency-aware roadmap with these broad milestones.

### Phase 0 — Architecture and feasibility

Requirements, research, technology decisions, diagrams, cost analysis, risk model and engineering plan.

### Phase 1 — Reproducible development foundation

Python packaging, quality gates, Docker, CI, configuration management, PostgreSQL, logging and test infrastructure.

### Phase 2 — Kraken read-only connectivity

Market data, exchange metadata, balances where approved, secure authentication and data-quality checks. No order placement.

### Phase 3 — Research platform

Historical datasets, benchmark strategies, backtesting, walk-forward analysis and reproducible experiment reporting.

### Phase 4 — Independent risk and paper execution

Portfolio accounting, risk enforcement, execution state machine, paper exchange and deterministic fault testing.

### Phase 5 — Extended paper-trading validation

At least eight weeks of monitored paper trading, with objective performance, safety and reliability gates.

### Phase 6 — Production-readiness review

Exchange eligibility, security, reliability, tax records, economics, rollback and live-trading approval checks.

### Phase 7 — Optional minimal live trading

Only after separate explicit authorisation, successful gates and an economically viable assessment.

### Phase 8 — Expansion

Additional strategies, second exchange, advanced analytics, Next.js dashboard and larger-scale infrastructure when justified.

Do not implement Phase 7 or Phase 8 merely because earlier phases have completed.

Each phase must have independent acceptance criteria, dependencies, estimated resource requirements, test evidence and go/no-go gates.

## 14. Institutional evolution

Explain how the initial design can evolve to support:

- Multiple strategies and strategy versions
- Multiple exchanges
- Multiple portfolios
- Stronger portfolio optimisation
- Independent real-time risk services
- Distributed research jobs
- Advanced market-data infrastructure
- More complex execution algorithms
- Expanded monitoring and incident management
- Institutional-grade audit trails
- More sophisticated security and governance
- A future proprietary trading company

Show trigger conditions for introducing Kafka/Redpanda/NATS, ClickHouse, object storage, distributed computing or Kubernetes.

Do not deploy these technologies simply to demonstrate scalability.

If the business ever progresses towards external investor capital, identify the regulatory, legal, custody, fiduciary, accounting and governance questions requiring a separate professional assessment.

Do not assume that managing personal capital is legally equivalent to operating a fund.

## 15. Required Plan Mode deliverables

Return a complete planning package in this order:

1. **Executive architecture decision** — proposed solution and why.
2. **Requirement traceability matrix** — every requirement mapped to components and tests.
3. **Assumptions and verified facts** — separate confirmed documentation from unresolved uncertainties.
4. **Architecture Decision Records (ADRs)** — important alternatives and the rationale for each selection.
5. **Technology stack** — exact dependency candidates, compatibility and supported versions to verify.
6. **Architecture diagrams** — system context, containers, components and critical data flows.
7. **Repository structure** — proposed Python package layout, files and clear module responsibilities.
8. **Domain model and interface contracts** — data structures, typing conventions and invariants.
9. **Database model** — tables, relationships, constraints, migrations and event retention.
10. **Kraken API integration design** — documented endpoints, authentication, subscriptions and error handling.
11. **Strategy research and backtesting specification**.
12. **Portfolio accounting and risk specification**.
13. **Execution state machine and fault-recovery design**.
14. **Security architecture and threat model**.
15. **Docker and deployment design**.
16. **Monitoring and Discord alert specification**.
17. **UK tax and audit-data specification**.
18. **Economic feasibility assessment**.
19. **Testing strategy and fault-injection matrix**.
20. **Phased implementation plan** — small, independently reviewable development tasks.
21. **Acceptance criteria and go/no-go gates**.
22. **Operational runbooks and failure procedures**.
23. **Institutional scaling roadmap**.
24. **Top risks, open issues and decision log**.
25. **First implementation milestone** — the exact scope that should be coded after plan approval.

Provide realistic estimates of complexity and dependencies, not fictitious delivery commitments.

Clearly distinguish mandatory initial functionality from deferred functionality.

Cite authoritative sources or official documentation for important external claims.

## 16. Engineering acceptance standards

The plan must demonstrate that:

- No leverage can enter the initial execution path.
- An exchange outage cannot silently become a duplicate order.
- A strategy cannot circumvent independent risk controls.
- Deposits cannot be mistaken for trading profit.
- UK GBP accounting can be reconstructed from stored records.
- Backtests do not assume impossible fills or ignore fees.
- Minimum order constraints are enforced.
- Trading does not depend on Discord availability.
- A container restart cannot erase unreconciled execution state.
- Incomplete market data blocks unsafe trading.
- Live trading cannot be enabled accidentally.
- The codebase supports an additional exchange without rewriting strategy logic.
- Initial operations can run within a realistic small-VPS resource budget.
- The system can remain safely in paper mode indefinitely.
- A lack of statistical trading advantage is reported transparently rather than hidden.

## 17. Final instruction

Before finalising your plan, challenge it as an independent principal engineer, quantitative risk manager and security reviewer.

Identify the ten most serious ways the proposed implementation could lose money through software or operational failure.

For each, describe the failure mechanism, preventive controls, detection, recovery and required tests.

Then review architectural complexity and remove anything not justified for the initial capital size.

Present your recommendation clearly.

**Stop after delivering the complete Plan Mode specification and request implementation approval. Do not begin implementation, create live trading orders, provision paid infrastructure or enable production execution.**

When implementation is subsequently approved, proceed milestone by milestone, demonstrating passing tests and explicitly recording incomplete safety requirements before continuing.