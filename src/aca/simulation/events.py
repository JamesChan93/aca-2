"""Causal trade-taker research economics; no fabricated book or live capability.

Print prices/quantities are a disclosed execution proxy, not quotes/depth. The
legacy bar runner is intentionally separate. This engine does not open datasets,
holdouts or studies. Its incremental interface makes prefix invariance testable.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import NAMESPACE_URL, UUID, uuid5

from aca.accounting.ledger import apply_journals, cash_flow, fill_journal, stored
from aca.domain.market import Candle, InstrumentRules, MarketTrade
from aca.domain.timing import CLOCK_VERSION, FINALISATION, MATCHING_TTL, TRANSMISSION_TTL
from aca.domain.trading import (
    TERMINAL,
    ZERO,
    Asset,
    Fee,
    Fill,
    Journal,
    KillSwitchState,
    Order,
    OrderIntent,
    PortfolioSnapshot,
    Valuation,
    fingerprint,
    replace_order,
)
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal, utc_datetime
from aca.operations.config import RiskPolicy
from aca.portfolio.state import (
    entries_paused,
    episode_risk,
    flow_units,
    free_cash,
    nav,
    observe_risk,
    unit_price,
)
from aca.risk.execution import DispatchDenied, commitment_guard
from aca.risk.portfolio import approve
from aca.simulation.assumptions import TRADE_MODEL
from aca.simulation.passive import proxy_prices
from aca.simulation.reference import (
    DEFAULT_COSTS,
    DEFAULT_POLICY,
    Backtest,
    CostModel,
    Episode,
    Observation,
)
from aca.sizing.long_spot import round_increment, size_long
from aca.strategies.slow import StrategySignal, Variant, signal


@dataclass(frozen=True)
class Command:
    client_id: UUID
    episode_id: UUID
    admitted_at: datetime
    dispatch_due: datetime
    dispatched_at: datetime | None = None
    match_after: datetime | None = None
    triggered_at: datetime | None = None


@dataclass(frozen=True)
class OpenEpisode:
    id: UUID
    opened_at: datetime
    bought: Decimal
    remaining: Decimal
    entry_price: Decimal
    fees: Decimal
    sold: Decimal = ZERO
    proceeds: Decimal = ZERO


@dataclass(frozen=True)
class EpochReview:
    id: str
    at: datetime
    reason: str
    evidence_sha256: str
    previous_epoch: UUID
    previous_state_sha256: str
    new_epoch: UUID


@dataclass(frozen=True)
class EventResult:
    backtest: Backtest
    state: PortfolioSnapshot
    fills: tuple[Fill, ...]
    journals: tuple[Journal, ...]
    commands: tuple[Command, ...]
    open_episode: OpenEpisode | None
    attribution: dict[str, Decimal]
    skip_reasons: dict[str, int]
    halt_seconds: Decimal
    exposure_seconds: Decimal
    stale_terminal_mark: bool
    observed_through: datetime
    requested_through: datetime
    exposure_fraction_seconds: Decimal
    equity_seconds: Decimal
    planned_risk_fraction_seconds: Decimal = ZERO
    epoch_reviews: tuple[EpochReview, ...] = ()
    execution_model: str = TRADE_MODEL
    clock_version: str = CLOCK_VERSION
    promotion_evidence: bool = False


class TradeSimulation:
    """One causal account, incremental closed prefixes and ordered public prints.

    IOC entries use a past/current print for sizing, then wait for a later print
    after latency; unused residuals are cancelled. Every fill is limited by 1% of
    its own print. No passive fills or interpolation across missing events exists.
    Reporting snapshots never reset the account, protection or policy epoch.
    """

    def __init__(
        self,
        rules: InstrumentRules,
        *,
        start: datetime,
        initial: Decimal = Decimal(250),
        costs: CostModel = DEFAULT_COSTS,
        policy: RiskPolicy = DEFAULT_POLICY,
        dispatch_delay_ms: int = 0,
        identity_namespace: str = "",
    ) -> None:
        self.start = self.at = utc_datetime(start)
        parse_decimal(initial)
        costs.checked()
        if (
            initial <= 0
            or type(dispatch_delay_ms) is not int
            or not 0 <= dispatch_delay_ms <= 60_000
        ):
            raise ValueError("invalid trade simulation capital/dispatch delay")
        if rules.status != "online":
            raise ValueError("offline research instrument")
        self.rules, self.costs, self.policy = rules, costs, policy
        self.delay = timedelta(milliseconds=dispatch_delay_ms)
        self.latency = timedelta(milliseconds=costs.latency_ms)
        self.initial = initial
        self.asset: Asset = "BTC" if rules.symbol == "BTC/GBP" else "ETH"
        identity = fingerprint(
            {
                "model": TRADE_MODEL,
                "clock": CLOCK_VERSION,
                "start": start.isoformat(),
                "initial": str(initial),
                "rules": rules.model_dump(mode="json"),
                "costs": costs.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
                "dispatch_delay_ms": dispatch_delay_ms,
                **({"namespace": identity_namespace} if identity_namespace else {}),
            }
        )
        account = uuid5(NAMESPACE_URL, identity)
        opening = cash_flow("research-opening", initial, start).model_copy(
            update={"id": uuid5(account, "opening")}
        )
        self.state = PortfolioSnapshot(
            account_id=account,
            version=1,
            as_of=start,
            balances=apply_journals((), (opening,)),
            units=initial,
            daily_day=start.date(),
            kill=KillSwitchState(policy_epoch=uuid5(account, "initial-epoch")),
            ready=True,
            account_evidence_at=start,
            reconciled_at=start,
        )
        self.journals = [opening]
        self.flows: dict[str, Journal] = {}
        self.external_flows = ZERO
        self.fills: list[Fill] = []
        self.commands: dict[UUID, Command] = {}
        self.observations = [Observation(start, initial, Decimal(1), ZERO, ZERO, False)]
        self.episodes: list[Episode] = []
        self.open_episode: OpenEpisode | None = None
        self.skips: Counter[str] = Counter()
        self.seen: dict[tuple[str, str, str], MarketTrade] = {}
        self.used_signals: set[tuple[str, datetime]] = set()
        self.latest: MarketTrade | None = None
        self.waiting: StrategySignal | None = None
        self.raw_cash = initial
        self.implementation = self.halt_seconds = self.exposure_seconds = ZERO
        self.exposure_fraction_seconds = ZERO
        self.equity_seconds = ZERO
        self.planned_risk_fraction_seconds = ZERO
        self.epoch_reviews: dict[str, EpochReview] = {}
        self.unfilled = 0

    def _clock(self, at: datetime) -> None:
        at = utc_datetime(at)
        if at < self.at:
            raise ValueError("research event clock regressed")
        with localcontext(FINANCIAL_CONTEXT):
            delta = at - self.at
            seconds = (
                Decimal(delta.days * 86400 + delta.seconds)
                + Decimal(delta.microseconds) / 1_000_000
            )
            if entries_paused(self.state, self.at):
                self.halt_seconds += seconds
            if self.state.quantity(self.asset):
                self.exposure_seconds += seconds
            self.exposure_fraction_seconds += seconds * self.observations[-1].exposure
            self.equity_seconds += seconds * self.observations[-1].nav
            if self.observations[-1].nav:
                self.planned_risk_fraction_seconds += seconds * stored(
                    episode_risk(self.state) / self.observations[-1].nav
                )
        self.at = at

    def _quotes(self, trade: MarketTrade) -> tuple[Decimal, Decimal]:
        # Adverse tick rounding is part of implementation cost, not depth evidence.
        with localcontext(FINANCIAL_CONTEXT):
            return self._quoted(trade)

    def _quoted(self, trade: MarketTrade) -> tuple[Decimal, Decimal]:
        return proxy_prices(trade, self.rules, self.costs)

    def _observe(self, at: datetime, trade: MarketTrade) -> None:
        bid, _ = self._quotes(trade)
        self.state = self.state.model_copy(
            update={
                "as_of": at,
                "version": self.state.version + 1,
                "marks": (
                    Valuation(
                        asset=self.asset,
                        bid_gbp=bid,
                        observed_at=trade.received_at,
                        source=TRADE_MODEL,
                    ),
                ),
                "account_evidence_at": at,
                "reconciled_at": at,
            }
        )
        if (at - trade.received_at).total_seconds() > 5 and self.state.quantity(self.asset):
            return  # Never restamp an old mark; result will disclose missing coverage.
        self.state = observe_risk(self.state, at, self.policy)
        if entries_paused(self.state, at):
            for order in self.state.orders:
                if order.intent.purpose == "entry" and order.state not in TERMINAL:
                    self.state = replace_order(
                        self.state, order.model_copy(update={"state": "CANCELLED"})
                    )
        value = nav(self.state, at)
        exposure = stored((value - self.state.quantity("GBP")) / value) if value else ZERO
        self.observations.append(
            Observation(at, value, unit_price(self.state, at), exposure, ZERO, False)
        )

    def publish(self, value: StrategySignal, at: datetime) -> None:
        """Consume a decision once. Its fixed freshness deadline is never renewed."""
        with localcontext(FINANCIAL_CONTEXT):
            self._publish(value, at)

    def _publish(self, value: StrategySignal, at: datetime) -> None:
        at = utc_datetime(at)
        value = StrategySignal.model_validate_json(value.model_dump_json())
        if value.strategy_version != "slow-v2" or value.symbol != self.rules.symbol:
            raise ValueError("corrected research requires a canonical slow-v2 signal")
        self.advance_time(at)
        identity = (value.strategy, value.data_watermark)
        if identity in self.used_signals:
            return
        self.used_signals.add(identity)
        if not value.decision_at <= at < value.expires_at:
            self.skips["signal_expired"] += 1
            return
        if self.waiting is not None:
            self.skips["signal_superseded"] += 1
        self.waiting = value
        self._admit(at)
        self._dispatch(at)

    def _admit(self, at: datetime) -> None:
        value, trade = self.waiting, self.latest
        if value is None:
            return
        if at >= value.expires_at:
            self.skips["signal_expired"] += 1
            self.waiting = None
            return
        if trade is None or any(
            not 0 <= (at - stamp).total_seconds() <= 5
            for stamp in (trade.received_at, trade.event_at)
        ):
            return  # Wait only inside the original slow-signal deadline.
        self.waiting = None
        if (
            self.state.kill.hard
            or not self.state.ready
            or self.state.incident
            or (value.desired == "long" and entries_paused(self.state, at))
        ):
            self.skips["risk_suspended"] += 1
            return
        held = self.state.quantity(self.asset)
        active = any(o.state not in TERMINAL for o in self.state.orders)
        if value.desired == "hold" or (value.desired == "long" and (held or active)):
            self.skips["no_change"] += 1
            return
        if value.desired == "flat":
            if held:
                self._exit(value, at)
            else:
                self.skips["no_inventory"] += 1
            return
        _, ask = self._quotes(trade)
        entry = round_increment(ask * Decimal("1.001"), self.rules.price_increment)
        distance = max(value.stop_distance, entry * Decimal("0.05"))
        if distance >= entry:
            self.skips["invalid_stop"] += 1
            return
        trigger = round_increment(entry - distance, self.rules.price_increment)
        limit = round_increment(
            trigger * (1 - self.costs.protection_limit_offset), self.rules.price_increment, up=True
        )
        if not ZERO < limit < trigger < entry:
            self.skips["invalid_stop"] += 1
            return
        fee_bound = max(Decimal("0.008"), self.costs.taker_fee)
        allowance = stored(entry * max(Decimal("0.002"), self.costs.implementation_allowance))
        sized = size_long(
            equity=nav(self.state, at),
            free_cash=free_cash(self.state),
            entry=entry,
            protective_limit=limit,
            entry_fee=fee_bound,
            exit_fee=fee_bound,
            allowance_per_unit=allowance,
            liquidity_quantity=stored(trade.quantity * self.costs.participation),
            rules=self.rules,
            policy=self.policy,
        )
        if sized.reason:
            self.skips[sized.reason] += 1
            return
        ident = uuid5(self.state.account_id, f"{value.strategy}:{value.data_watermark.isoformat()}")
        intent = OrderIntent(
            id=ident,
            client_id=ident,
            symbol=self.rules.symbol,
            purpose="entry",
            side="buy",
            quantity=sized.quantity,
            limit_price=entry,
            stop_trigger=trigger,
            protective_limit=limit,
            fee_bound=fee_bound,
            allowance_per_unit=allowance,
            created_at=at,
            expires_at=min(value.expires_at, at + TRANSMISSION_TTL),
            strategy_version="slow-v2",
            timing_version=CLOCK_VERSION,
            rules_hash=fingerprint(self.rules.model_dump(mode="json")),
            episode_id=uuid5(ident, "episode"),
            time_in_force="IOC",
        )
        approval = approve(self.state, intent, self.rules, self.policy, at).model_copy(
            update={"id": uuid5(ident, "approval")}
        )
        if not approval.allowed:
            self.skips["risk:" + ",".join(approval.reasons)] += 1
            return
        self.state = replace_order(
            self.state.model_copy(update={"version": self.state.version + 1}),
            Order(intent=intent, approval=approval, state="VALIDATED"),
        )
        self.commands[ident] = Command(ident, intent.episode_id, at, at + self.delay)

    def _dispatch(self, at: datetime) -> None:
        for order in self.state.orders:
            if order.state != "VALIDATED":
                continue
            command = self.commands[order.intent.client_id]
            if command.dispatch_due > at:
                continue
            try:
                commitment_guard(self.state, order, self.policy, at)
                if self.latest is None or any(
                    not 0 <= (at - stamp).total_seconds() <= 5
                    for stamp in (self.latest.received_at, self.latest.event_at)
                ):
                    raise DispatchDenied("stale_trade_proxy")
                _, ask = self._quotes(self.latest)
                if ask > order.intent.limit_price:
                    raise DispatchDenied("price_bound_changed")
            except DispatchDenied as exc:
                self.skips["dispatch:" + str(exc)] += 1
                self.state = replace_order(
                    self.state, order.model_copy(update={"state": "EXPIRED"})
                )
                continue
            self.commands[order.intent.client_id] = Command(
                command.client_id,
                command.episode_id,
                command.admitted_at,
                command.dispatch_due,
                at,
                at + self.latency,
            )
            self.state = replace_order(
                self.state, order.model_copy(update={"state": "OPEN", "submitted_at": at})
            )

    def advance_time(self, at: datetime) -> None:
        """Dispatch timers precede later market input, never using that future print."""
        at = utc_datetime(at)
        if at < self.at:
            raise ValueError("research event clock regressed")
        due = sorted(
            {
                c.dispatch_due
                for c in self.commands.values()
                if c.dispatched_at is None
                and self.at <= c.dispatch_due <= at
                and self.state.order(c.client_id).state == "VALIDATED"
            }
            | {
                o.intent.created_at + MATCHING_TTL
                for o in self.state.orders
                if o.state not in TERMINAL
                and o.intent.purpose != "protection"
                and self.at <= o.intent.created_at + MATCHING_TTL <= at
            }
        )
        for when in due:
            self._clock(when)
            self._dispatch(when)
            for order in self.state.orders:
                if (
                    order.intent.purpose != "protection"
                    and order.state in {"OPEN", "PARTIALLY_FILLED"}
                    and when >= order.intent.created_at + MATCHING_TTL
                ):
                    self.state = replace_order(
                        self.state, order.model_copy(update={"state": "EXPIRED"})
                    )
                    self.skips["matching_deadline"] += 1
                    if order.intent.purpose == "exit":
                        self._protect(order.intent, order.remaining, when)
        self._clock(at)
        if self.waiting is not None and at >= self.waiting.expires_at:
            self.skips["signal_expired"] += 1
            self.waiting = None

    def _exit(self, value: StrategySignal, at: datetime) -> None:
        if self.latest is None or self.open_episode is None:
            return
        bid, _ = self._quotes(self.latest)
        limit = round_increment(bid * Decimal("0.998"), self.rules.price_increment, up=True)
        q = self.state.quantity(self.asset)
        if q < self.rules.minimum_quantity or q * limit < self.rules.minimum_cost:
            self.state = self.state.model_copy(
                update={"ready": False, "incident": "untradeable_residual"}
            )
            self.skips["untradeable_residual"] += 1
            return
        # In this deterministic single timeline cancellation is definitive. There
        # is no claim about transport races; those remain covered by paper faults.
        for order in self.state.orders:
            if order.state not in TERMINAL:
                self.state = replace_order(
                    self.state, order.model_copy(update={"state": "CANCELLED"})
                )
        ident = uuid5(self.open_episode.id, value.data_watermark.isoformat())
        intent = OrderIntent(
            id=ident,
            client_id=ident,
            symbol=self.rules.symbol,
            purpose="exit",
            side="sell",
            quantity=q,
            limit_price=limit,
            stop_trigger=ZERO,
            protective_limit=ZERO,
            fee_bound=max(Decimal("0.008"), self.costs.taker_fee),
            allowance_per_unit=ZERO,
            created_at=at,
            expires_at=min(value.expires_at, at + TRANSMISSION_TTL),
            strategy_version="safety-v1",
            timing_version=CLOCK_VERSION,
            rules_hash=fingerprint(self.rules.model_dump(mode="json")),
            episode_id=self.open_episode.id,
            time_in_force="IOC",
        )
        self.state = replace_order(
            self.state, Order(intent=intent, approval=None, state="OPEN", submitted_at=at)
        )
        self.commands[ident] = Command(ident, intent.episode_id, at, at, at, at + self.latency)

    def trade(self, trade: MarketTrade) -> None:
        """A duplicate is economically inert; a conflicting identity is an error."""
        trade = MarketTrade.model_validate_json(trade.model_dump_json())
        key = (trade.venue, trade.symbol, trade.trade_id)
        previous = self.seen.get(key)
        if previous is not None:
            if previous != trade:
                raise ValueError("conflicting research trade identity")
            return
        if trade.venue != self.rules.venue or trade.symbol != self.rules.symbol:
            raise ValueError("research trade instrument mismatch")
        if not 0 <= (trade.received_at - trade.event_at).total_seconds() <= 5:
            raise ValueError("stale/future research trade")
        with localcontext(FINANCIAL_CONTEXT):
            self.advance_time(trade.received_at)
            self.seen[key] = trade
            self.latest = trade
            self._observe(self.at, trade)
            self._match(trade)
            self._observe(self.at, trade)
            self._admit(self.at)
            self._dispatch(self.at)

    def cashflow(self, source: str, amount: Decimal, at: datetime) -> None:
        """Unitise an explicit external flow; no contribution schedule is inferred."""
        at = utc_datetime(at)
        parse_decimal(amount)
        previous = self.flows.get(source)
        if previous is not None:
            if previous.occurred_at != at or previous.movements[0].quantity != amount:
                raise ValueError("conflicting research cash-flow identity")
            return
        with localcontext(FINANCIAL_CONTEXT):
            self.advance_time(at)
            if amount < 0 and -amount > free_cash(self.state):
                raise ValueError("research withdrawal spends reserved funds")
            units = flow_units(self.state, amount, at)
            journal = cash_flow(source, amount, at).model_copy(
                update={"id": uuid5(self.state.account_id, f"flow:{source}")}
            )
            self.state = self.state.model_copy(
                update={
                    "balances": apply_journals(self.state.balances, (journal,)),
                    "units": units,
                    "as_of": at,
                    "version": self.state.version + 1,
                }
            )
            self.state = observe_risk(self.state, at, self.policy)
            self.flows[source] = journal
            self.journals.append(journal)
            self.raw_cash += amount
            self.external_flows += amount
            value = nav(self.state, at)
            self.observations.append(
                Observation(
                    at,
                    value,
                    unit_price(self.state, at),
                    stored((value - self.state.quantity("GBP")) / value) if value else ZERO,
                    amount,
                    False,
                )
            )

    def mark_time(self, at: datetime) -> None:
        """Observe a timer with existing fresh evidence; never refresh an old mark.

        Used for real UTC midnight baselines. A missing held-asset valuation is
        left missing; the next late observation applies the shared daily latch.
        """
        with localcontext(FINANCIAL_CONTEXT):
            self.advance_time(at)
            if self.latest is not None and 0 <= (at - self.latest.received_at).total_seconds() <= 5:
                self._observe(at, self.latest)
            elif not self.state.quantity(self.asset):
                self.state = observe_risk(self.state, at, self.policy)

    def review_epoch(
        self, source: str, reason: str, evidence_sha256: str, expected_epoch: UUID, at: datetime
    ) -> None:
        """Explicit audited input only; no automatic fold/day/restart rearming."""
        at = utc_datetime(at)
        old = self.epoch_reviews.get(source)
        if old is not None:
            if (old.at, old.reason, old.evidence_sha256, old.previous_epoch) != (
                at,
                reason,
                evidence_sha256,
                expected_epoch,
            ):
                raise ValueError("conflicting epoch review identity")
            return
        if (
            not 1 <= len(source) <= 120
            or not 12 <= len(reason.strip()) <= 240
            or len(evidence_sha256) != 64
            or any(c not in "0123456789abcdef" for c in evidence_sha256)
        ):
            raise ValueError("epoch review requires substantive reason and evidence hash")
        self.advance_time(at)
        before = self.state
        if (
            before.kill.policy_epoch != expected_epoch
            or not before.ready
            or before.incident
            or any(o.state in {"UNKNOWN", "SUBMITTING", "RECONCILING"} for o in before.orders)
            or any(o.intent.purpose == "entry" and o.state not in TERMINAL for o in before.orders)
        ):
            raise ValueError("epoch review requires complete known account state")
        price = unit_price(before, at)
        new = uuid5(before.account_id, f"review:{source}")
        self.epoch_reviews[source] = EpochReview(
            source, at, reason, evidence_sha256, expected_epoch, fingerprint(before.document()), new
        )
        self.state = before.model_copy(
            update={
                "version": before.version + 1,
                "as_of": at,
                "high_water": price,
                "kill": KillSwitchState(
                    policy_epoch=new,
                    daily_day=before.kill.daily_day,
                    reason=reason,
                    latched_at=at,
                    actor="operator",
                ),
            }
        )

    def _match(self, trade: MarketTrade) -> None:
        bid, ask = self._quotes(trade)
        budget = round_increment(
            trade.quantity * self.costs.participation, self.rules.quantity_increment
        )
        for order in self.state.orders:
            i = order.intent
            if order.state not in {"OPEN", "PARTIALLY_FILLED"}:
                continue
            command = self.commands[i.client_id]
            if command.match_after is None or self.at < command.match_after:
                continue
            # Both event and receipt chronology must be after submission/trigger.
            if trade.event_at < command.match_after:
                continue
            if i.purpose == "protection" and not order.triggered:
                if bid <= i.stop_trigger:
                    self.commands[i.client_id] = Command(
                        command.client_id,
                        command.episode_id,
                        command.admitted_at,
                        command.dispatch_due,
                        command.dispatched_at,
                        self.at + self.latency,
                        self.at,
                    )
                    self.state = replace_order(
                        self.state, order.model_copy(update={"triggered": True})
                    )
                continue
            if i.purpose != "protection" and self.at >= i.created_at + MATCHING_TTL:
                self.state = replace_order(
                    self.state, order.model_copy(update={"state": "EXPIRED"})
                )
                self.skips["matching_deadline"] += 1
                if i.purpose == "exit":
                    self._protect(i, order.remaining, self.at)
                continue
            price = ask if i.side == "buy" else bid
            q = min(order.remaining, budget)
            permitted = price <= i.limit_price if i.side == "buy" else price >= i.limit_price
            if i.side == "buy" and (
                q < self.rules.minimum_quantity or q * i.protective_limit < self.rules.minimum_cost
            ):
                q = ZERO  # Never manufacture an unprotectable partial entry.
            if not permitted:
                q = ZERO
            if q:
                self._fill(order, q, price, trade)
                budget -= q
            elif i.purpose == "protection":
                self.unfilled += 1
            if i.purpose != "protection":
                actual = self.state.order(i.client_id)
                if actual.state != "FILLED":
                    self.state = replace_order(
                        self.state, actual.model_copy(update={"state": "CANCELLED"})
                    )
                    self.skips["ioc_unfilled_residual"] += 1
                    if i.purpose == "exit":
                        self._protect(i, actual.remaining, self.at)

    def _protect(self, parent: OrderIntent, quantity: Decimal, at: datetime) -> None:
        if not quantity:
            return
        entry = next(
            o.intent
            for o in self.state.orders
            if o.intent.purpose == "entry" and o.intent.episode_id == parent.episode_id
        )
        if (
            quantity < self.rules.minimum_quantity
            or quantity * entry.protective_limit < self.rules.minimum_cost
        ):
            self.state = self.state.model_copy(
                update={"ready": False, "incident": "untradeable_residual"}
            )
            return
        ident = uuid5(parent.client_id, f"protection:{at.isoformat()}")
        intent = OrderIntent(
            id=ident,
            client_id=ident,
            symbol=parent.symbol,
            purpose="protection",
            side="sell",
            quantity=quantity,
            limit_price=entry.protective_limit,
            stop_trigger=entry.stop_trigger,
            protective_limit=ZERO,
            fee_bound=entry.fee_bound,
            allowance_per_unit=ZERO,
            created_at=at,
            expires_at=at + timedelta(days=3650),
            strategy_version="safety-v1",
            timing_version=CLOCK_VERSION,
            rules_hash=entry.rules_hash,
            episode_id=entry.episode_id,
            parent_id=entry.client_id,
            time_in_force="GTC",
        )
        self.state = replace_order(self.state, Order(intent=intent, approval=None, state="OPEN"))
        self.commands[ident] = Command(ident, intent.episode_id, at, at, at, at + self.latency)

    def _fill(self, order: Order, q: Decimal, price: Decimal, trade: MarketTrade) -> None:
        i = order.intent
        fee = stored(q * price * self.costs.taker_fee)
        fill = Fill(
            id=str(uuid5(i.client_id, f"{trade.trade_id}:{order.filled}")),
            client_id=i.client_id,
            symbol=i.symbol,
            side=i.side,
            quantity=q,
            price=price,
            occurred_at=self.at,
            liquidity="taker",
            fees=(
                Fee(
                    asset="GBP",
                    quantity=fee,
                    rate_gbp=Decimal(1),
                    valued_at=self.at,
                    valuation_source=TRADE_MODEL,
                ),
            ),
        )
        journal = fill_journal(fill, self.state.balances).model_copy(
            update={"id": uuid5(i.client_id, fill.id), "reason": "Modelled ordered research trade"}
        )
        self.journals.append(journal)
        self.fills.append(fill)
        cumulative = order.filled + q
        actual = order.model_copy(
            update={
                "filled": cumulative,
                "state": "FILLED" if cumulative == i.quantity else "PARTIALLY_FILLED",
            }
        )
        self.state = replace_order(
            self.state.model_copy(
                update={"balances": apply_journals(self.state.balances, (journal,))}
            ),
            actual,
        )
        if i.side == "buy":
            self.raw_cash -= q * trade.price
            self.implementation += q * (price - trade.price)
            self.open_episode = OpenEpisode(i.episode_id, self.at, q, q, price, fee)
            self._protect(i, q, self.at)
        else:
            self.raw_cash += q * trade.price
            self.implementation += q * (trade.price - price)
            episode = self.open_episode
            if episode is None:
                raise ValueError("sale without research episode")
            remaining, sold = episode.remaining - q, episode.sold + q
            proceeds, total_fees = episode.proceeds + q * price, episode.fees + fee
            if remaining:
                self.open_episode = OpenEpisode(
                    episode.id,
                    episode.opened_at,
                    episode.bought,
                    remaining,
                    episode.entry_price,
                    total_fees,
                    sold,
                    proceeds,
                )
            else:
                self.episodes.append(
                    Episode(
                        episode.opened_at,
                        self.at,
                        sold,
                        episode.entry_price,
                        proceeds / sold,
                        total_fees,
                        proceeds - sold * episode.entry_price - total_fees,
                        "protective_limit" if i.purpose == "protection" else "strategy",
                    )
                )
                self.open_episode = None

    def result(self, at: datetime | None = None) -> EventResult:
        """Snapshot without fictitious terminal liquidation or refreshed old marks."""
        if at is not None:
            self.advance_time(at)
        with localcontext(FINANCIAL_CONTEXT):
            q = self.state.quantity(self.asset)
            raw = self.latest.price if self.latest else ZERO
            bid = self._quotes(self.latest)[0] if self.latest else ZERO
            terminal = self.state.quantity("GBP") + q * bid
            gross = self.raw_cash + q * raw - self.initial - self.external_flows
            fees = sum((f.fees[0].quantity for f in self.fills), ZERO)
            discount = q * (raw - bid)
            rounding = terminal - (
                self.initial + self.external_flows + gross - fees - self.implementation - discount
            )
            attribution = {
                "opening_nav_gbp": self.initial,
                "external_flows_gbp": self.external_flows,
                "gross_marked_pnl_gbp": gross,
                "fees_gbp": fees,
                "spread_slippage_rounding_gbp": self.implementation,
                "terminal_mark_discount_gbp": discount,
                "functional_currency_rounding_gbp": rounding,
                "terminal_inventory_gbp": q * bid,
                "realised_trading_pnl_gbp": -sum(
                    (
                        line.gbp
                        for journal in self.journals
                        for line in journal.lines
                        if line.account == "pnl:realised"
                    ),
                    ZERO,
                ),
                "unrealised_mark_pnl_gbp": q * bid - self.state.cost(self.asset),
                "ending_nav_gbp": terminal,
            }
            if abs(rounding) > Decimal("1e-15") * max(1, len(self.fills)):
                raise ValueError("research attribution discrepancy")
            halts = tuple(
                name
                for name, hit in (
                    ("hard_drawdown", self.state.kill.hard),
                    ("soft_drawdown", self.state.kill.soft),
                    ("daily_loss_or_missing_midnight", self.state.kill.daily_day is not None),
                )
                if hit
            )
            backtest = Backtest(
                tuple(self.observations),
                tuple(self.episodes),
                fees,
                sum((f.quantity * f.price for f in self.fills), ZERO),
                sum(self.skips.values()),
                halts,
                q,
                self.unfilled,
                1440,
                self.start,
                "every_received_trade_before_and_after_fills; inception; no_gap_interpolation",
            )
            return EventResult(
                backtest,
                self.state,
                tuple(self.fills),
                tuple(self.journals),
                tuple(self.commands.values()),
                self.open_episode,
                attribution,
                dict(self.skips),
                self.halt_seconds,
                self.exposure_seconds,
                bool(q and self.latest and (self.at - self.latest.received_at).total_seconds() > 5),
                self.observations[-1].at,
                self.at,
                self.exposure_fraction_seconds,
                self.equity_seconds,
                self.planned_risk_fraction_seconds,
                tuple(self.epoch_reviews.values()),
            )


def simulate_trades(
    bars: Sequence[Candle],
    trades: Sequence[MarketTrade],
    rules: InstrumentRules,
    *,
    variant: Variant,
    start: datetime,
    end: datetime,
    initial: Decimal = Decimal(250),
    costs: CostModel = DEFAULT_COSTS,
    dispatch_delay_ms: int = 0,
) -> EventResult:
    """Merge finalisation timers with receipt-ordered retained prints.

    Archive `observed_at` is retrieval provenance: the declared historical
    availability assumption is bar end + 10s. Future bar contents are not read by
    decisions, sizing or fills. Unknown gaps/quarantined candles fail closed.
    """
    start, end = utc_datetime(start), utc_datetime(end)
    if start >= end:
        raise ValueError("invalid research evaluation range")
    for index, bar in enumerate(bars):
        no_trade = (
            bar.quality == ("confirmed_no_trade",)
            and bar.volume == 0
            and bar.trade_count == 0
            and index > 0
            and bar.open == bar.high == bar.low == bar.close == bars[index - 1].close
        )
        if (
            bar.symbol != rules.symbol
            or not (bar.complete or no_trade)
            or (
                index
                and (
                    bars[index - 1].end != bar.start
                    or bars[index - 1].interval_minutes != bar.interval_minutes
                )
            )
        ):
            raise ValueError("unqualified/discontinuous research candle")
    if sum(bar.end <= start for bar in bars) < 200:
        raise ValueError("insufficient past-only warm-up")
    engine = TradeSimulation(
        rules, start=start, initial=initial, costs=costs, dispatch_delay_ms=dispatch_delay_ms
    )
    due = [
        (index, bar.end + FINALISATION)
        for index, bar in enumerate(bars)
        if index >= 199 and start <= bar.end + FINALISATION < end
    ]
    next_signal = 0
    for trade in trades:
        if not start <= trade.received_at < end:
            raise ValueError("out-of-range research trade")
        while next_signal < len(due) and due[next_signal][1] <= trade.received_at:
            index, at = due[next_signal]
            engine.publish(
                signal(bars[max(0, index - 200) : index + 1], variant, at, version="slow-v2"), at
            )
            next_signal += 1
        engine.trade(trade)
    while next_signal < len(due):
        index, at = due[next_signal]
        engine.publish(
            signal(bars[max(0, index - 200) : index + 1], variant, at, version="slow-v2"), at
        )
        next_signal += 1
    return engine.result(end)
