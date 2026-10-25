"""Causal, funded passive comparators with the same investor flows and taker proxy.

This is an explicitly different mandate: no strategy stops or portfolio risk
halts. Budgets are spent once after latency; residuals stay cash, never chased.
"""

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import NAMESPACE_URL, uuid5

from aca.accounting.ledger import apply_journals, cash_flow, fill_journal, stored
from aca.domain.market import InstrumentRules, MarketTrade, Symbol
from aca.domain.trading import (
    ZERO,
    Asset,
    Fee,
    Fill,
    Journal,
    KillSwitchState,
    PortfolioSnapshot,
    Valuation,
    fingerprint,
)
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal, utc_datetime
from aca.portfolio.state import flow_units, nav, unit_price
from aca.simulation.reference import Backtest, CostModel, Observation
from aca.sizing.long_spot import round_increment

PASSIVE_MODEL = "continuous-passive-trade-taker-v1"


def proxy_prices(
    trade: MarketTrade, rules: InstrumentRules, costs: CostModel
) -> tuple[Decimal, Decimal]:
    with localcontext(FINANCIAL_CONTEXT):
        return (
            round_increment(
                trade.price * (1 - costs.spread_slippage_one_way), rules.price_increment
            ),
            round_increment(
                trade.price * (1 + costs.spread_slippage_one_way), rules.price_increment, up=True
            ),
        )


@dataclass(frozen=True)
class Budget:
    source: str
    symbol: Symbol
    amount: Decimal
    created_at: datetime
    deadline: datetime
    quantity: Decimal = ZERO
    limit: Decimal = ZERO
    match_after: datetime | None = None
    finished: bool = False
    spent: Decimal = ZERO


@dataclass(frozen=True)
class PassiveResult:
    backtest: Backtest
    state: PortfolioSnapshot
    fills: tuple[Fill, ...]
    journals: tuple[Journal, ...]
    budgets: tuple[Budget, ...]
    attribution: dict[str, Decimal]
    skip_reasons: dict[str, int]
    stale_terminal_mark: bool
    mean_exposure: Decimal
    mean_equity: Decimal
    missing_valuation_observations: int
    execution_model: str = PASSIVE_MODEL


class PassiveAccount:
    def __init__(
        self,
        name: str,
        rules: dict[Symbol, InstrumentRules],
        weights: dict[Symbol, Decimal],
        *,
        start: datetime,
        initial: Decimal,
        costs: CostModel,
    ) -> None:
        self.start = self.at = utc_datetime(start)
        parse_decimal(initial)
        for weight in weights.values():
            parse_decimal(weight)
        costs.checked()
        if (
            initial <= 0
            or not rules
            or not set(weights) <= rules.keys()
            or any(not 0 <= weight <= 1 for weight in weights.values())
            or sum(weights.values(), ZERO) > 1
            or any(
                symbol != rule.symbol or rule.status != "online" for symbol, rule in rules.items()
            )
        ):
            raise ValueError("invalid passive mandate")
        self.rules, self.weights, self.costs, self.initial = rules, weights, costs, initial
        account = uuid5(
            NAMESPACE_URL,
            fingerprint(
                {
                    "model": PASSIVE_MODEL,
                    "name": name,
                    "rules": {s: r.model_dump(mode="json") for s, r in sorted(rules.items())},
                    "weights": {s: str(w) for s, w in sorted(weights.items())},
                    "costs": costs.model_dump(mode="json"),
                    "start": start.isoformat(),
                    "initial": str(initial),
                }
            ),
        )
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
            kill=KillSwitchState(policy_epoch=uuid5(account, "passive-mandate")),
            ready=True,
            account_evidence_at=start,
            reconciled_at=start,
        )
        self.journals = [opening]
        self.flows: dict[str, Journal] = {}
        self.fills: list[Fill] = []
        self.budgets: list[Budget] = []
        self.latest: dict[Symbol, MarketTrade] = {}
        self.seen: dict[tuple[str, str, str], MarketTrade] = {}
        self.observations = [Observation(start, initial, Decimal(1), ZERO, ZERO, False)]
        self.skips: Counter[str] = Counter()
        self.external_flows = self.implementation = ZERO
        self.raw_cash = initial
        self.equity_seconds = self.exposure_seconds = ZERO
        self.missing = 0
        self.allocate("opening", initial, start)

    def advance_time(self, at: datetime) -> None:
        at = utc_datetime(at)
        if at < self.at:
            raise ValueError("passive clock regressed")
        with localcontext(FINANCIAL_CONTEXT):
            seconds = Decimal(str((at - self.at).total_seconds()))
            self.equity_seconds += seconds * self.observations[-1].nav
            self.exposure_seconds += seconds * self.observations[-1].exposure
        self.at = at
        for index, budget in enumerate(self.budgets):
            if not budget.finished and at >= budget.deadline:
                self.budgets[index] = replace(budget, finished=True)
                self.skips["budget_deadline_residual_cash"] += 1

    def _record(self, at: datetime, amount: Decimal = ZERO) -> None:
        try:
            value = nav(self.state, at)
            price = unit_price(self.state, at)
        except ValueError:
            self.missing += 1
            return
        with localcontext(FINANCIAL_CONTEXT):
            self.observations.append(
                Observation(
                    at,
                    value,
                    price,
                    stored((value - self.state.quantity("GBP")) / value) if value else ZERO,
                    amount,
                    False,
                )
            )

    def allocate(self, source: str, amount: Decimal, at: datetime) -> None:
        with localcontext(FINANCIAL_CONTEXT):
            for symbol, weight in sorted(self.weights.items()):
                if weight:
                    self.budget(source, symbol, stored(amount * weight), at)

    def budget(self, source: str, symbol: Symbol, amount: Decimal, at: datetime) -> None:
        """One bounded buy attempt; at most available uncommitted cash."""
        parse_decimal(amount)
        self.advance_time(at)
        if symbol not in self.rules or amount < 0:
            raise ValueError("invalid passive deployment budget")
        if any(b.source == source and b.symbol == symbol for b in self.budgets):
            raise ValueError("duplicate passive deployment identity")
        committed = sum((b.amount for b in self.budgets if not b.finished), ZERO)
        if amount > self.state.quantity("GBP") - committed:
            raise ValueError("passive budget exceeds free cash")
        if amount:
            self.budgets.append(Budget(source, symbol, amount, at, at + timedelta(seconds=60)))

    def cashflow(self, source: str, amount: Decimal, at: datetime) -> None:
        parse_decimal(amount)
        old = self.flows.get(source)
        if old is not None:
            if old.occurred_at != at or old.movements[0].quantity != amount:
                raise ValueError("conflicting passive cash-flow identity")
            return
        if amount <= 0:
            raise ValueError("passive scenario contributions must be positive")
        with localcontext(FINANCIAL_CONTEXT):
            self.advance_time(at)
            units = flow_units(self.state, amount, at)  # Missing pre-flow value fails explicitly.
            journal = cash_flow(source, amount, at).model_copy(
                update={"id": uuid5(self.state.account_id, f"flow:{source}")}
            )
            self.state = self.state.model_copy(
                update={
                    "version": self.state.version + 1,
                    "as_of": at,
                    "units": units,
                    "balances": apply_journals(self.state.balances, (journal,)),
                }
            )
            self.flows[source] = journal
            self.journals.append(journal)
            self.raw_cash += amount
            self.external_flows += amount
            self._record(at, amount)
            self.allocate(source, amount, at)

    def trade(self, trade: MarketTrade) -> None:
        trade = MarketTrade.model_validate_json(trade.model_dump_json())
        key = (trade.venue, trade.symbol, trade.trade_id)
        old = self.seen.get(key)
        if old is not None:
            if old != trade:
                raise ValueError("conflicting passive trade identity")
            return
        rules = self.rules.get(trade.symbol)
        if rules is None or trade.venue != rules.venue:
            raise ValueError("passive trade instrument mismatch")
        if not 0 <= (trade.received_at - trade.event_at).total_seconds() <= 5:
            raise ValueError("stale/future passive trade")
        with localcontext(FINANCIAL_CONTEXT):
            self.advance_time(trade.received_at)
            self.seen[key] = trade
            self.latest[trade.symbol] = trade
            bid, ask = proxy_prices(trade, rules, self.costs)
            asset: Asset = "BTC" if trade.symbol == "BTC/GBP" else "ETH"
            mark = Valuation(
                asset=asset, bid_gbp=bid, observed_at=trade.received_at, source=PASSIVE_MODEL
            )
            self.state = self.state.model_copy(
                update={
                    "version": self.state.version + 1,
                    "as_of": self.at,
                    "marks": (*(m for m in self.state.marks if m.asset != asset), mark),
                }
            )
            self._record(self.at)
            available = round_increment(
                trade.quantity * self.costs.participation, rules.quantity_increment
            )
            for index, budget in enumerate(self.budgets):
                if budget.finished or budget.symbol != trade.symbol:
                    continue
                if budget.match_after is None:
                    limit = round_increment(ask * Decimal("1.001"), rules.price_increment)
                    q = round_increment(
                        min(budget.amount / (limit * (1 + self.costs.taker_fee)), available),
                        rules.quantity_increment,
                    )
                    if q < rules.minimum_quantity or q * limit < rules.minimum_cost:
                        self.budgets[index] = replace(budget, finished=True)
                        self.skips["minimum_or_liquidity_residual_cash"] += 1
                    else:
                        self.budgets[index] = replace(
                            budget,
                            quantity=q,
                            limit=limit,
                            match_after=self.at + timedelta(milliseconds=self.costs.latency_ms),
                        )
                    continue
                if self.at < budget.match_after or trade.event_at < budget.match_after:
                    continue
                q = min(available, budget.quantity) if ask <= budget.limit else ZERO
                if q < rules.minimum_quantity or q * ask < rules.minimum_cost:
                    q = ZERO
                spent = ZERO
                if q:
                    ident = uuid5(self.state.account_id, f"{budget.source}:{budget.symbol}")
                    fee = stored(q * ask * self.costs.taker_fee)
                    fill = Fill(
                        id=str(ident),
                        client_id=ident,
                        symbol=trade.symbol,
                        side="buy",
                        quantity=q,
                        price=ask,
                        occurred_at=self.at,
                        liquidity="taker",
                        fees=(
                            Fee(
                                asset="GBP",
                                quantity=fee,
                                rate_gbp=Decimal(1),
                                valued_at=self.at,
                                valuation_source=PASSIVE_MODEL,
                            ),
                        ),
                    )
                    journal = fill_journal(fill, self.state.balances).model_copy(
                        update={
                            "id": uuid5(ident, "journal"),
                            "reason": "Modelled passive taker purchase",
                        }
                    )
                    self.state = self.state.model_copy(
                        update={"balances": apply_journals(self.state.balances, (journal,))}
                    )
                    self.fills.append(fill)
                    self.journals.append(journal)
                    self.raw_cash -= q * trade.price
                    self.implementation += q * (ask - trade.price)
                    available -= q
                    spent = q * ask + fee
                if spent < budget.amount:
                    self.skips["ioc_residual_cash_no_chase"] += 1
                self.budgets[index] = replace(budget, spent=spent, finished=True)
            self._record(self.at)

    def result(self, at: datetime) -> PassiveResult:
        self.advance_time(at)
        with localcontext(FINANCIAL_CONTEXT):
            marks = {m.asset: m.bid_gbp for m in self.state.marks}
            inventory = sum(
                (
                    b.quantity * marks[b.asset]
                    for b in self.state.balances
                    if b.asset != "GBP" and b.quantity
                ),
                ZERO,
            )
            raw_inventory = sum(
                (
                    self.state.quantity("BTC" if s == "BTC/GBP" else "ETH") * t.price
                    for s, t in self.latest.items()
                ),
                ZERO,
            )
            terminal = self.state.quantity("GBP") + inventory
            fees = sum((f.fees[0].quantity for f in self.fills), ZERO)
            gross = self.raw_cash + raw_inventory - self.initial - self.external_flows
            discount = raw_inventory - inventory
            rounding = terminal - (
                self.initial + self.external_flows + gross - fees - self.implementation - discount
            )
            if abs(rounding) > Decimal("1e-15") * max(1, len(self.fills)):
                raise ValueError("passive attribution discrepancy")
            seconds = Decimal(str((at - self.start).total_seconds()))
            return PassiveResult(
                Backtest(
                    tuple(self.observations),
                    (),
                    fees,
                    sum((f.price * f.quantity for f in self.fills), ZERO),
                    sum(self.skips.values()),
                    (),
                    sum((b.quantity for b in self.state.balances if b.asset != "GBP"), ZERO),
                    0,
                    1440,
                    self.start,
                    "received_trades; missing_cross_asset_marks_disclosed",
                ),
                self.state,
                tuple(self.fills),
                tuple(self.journals),
                tuple(self.budgets),
                {
                    "opening_nav_gbp": self.initial,
                    "external_flows_gbp": self.external_flows,
                    "gross_marked_pnl_gbp": gross,
                    "fees_gbp": fees,
                    "spread_slippage_rounding_gbp": self.implementation,
                    "terminal_mark_discount_gbp": discount,
                    "functional_currency_rounding_gbp": rounding,
                    "terminal_inventory_gbp": inventory,
                    "realised_trading_pnl_gbp": ZERO,
                    "unrealised_mark_pnl_gbp": inventory
                    - sum((b.cost_gbp for b in self.state.balances if b.asset != "GBP"), ZERO),
                    "ending_nav_gbp": terminal,
                },
                dict(self.skips),
                any(
                    b.quantity
                    and b.asset != "GBP"
                    and (
                        at - next(m.observed_at for m in self.state.marks if m.asset == b.asset)
                    ).total_seconds()
                    > 5
                    for b in self.state.balances
                ),
                self.exposure_seconds / seconds if seconds else ZERO,
                self.equity_seconds / seconds if seconds else self.initial,
                self.missing,
            )
