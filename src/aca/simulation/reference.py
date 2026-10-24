"""Decimal long/cash bar reference engine. Bar-only history never awards maker fills."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from itertools import pairwise

from aca.domain.market import Candle, InstrumentRules
from aca.domain.values import FINANCIAL_CONTEXT, FinancialDecimal, parse_decimal, utc_datetime
from aca.operations.config import RiskPolicy, StrictModel
from aca.simulation.assumptions import BOUNDS
from aca.sizing.long_spot import round_increment, size_long
from aca.strategies.slow import Variant, signal

D = Decimal
ZERO = Decimal(0)
INITIAL = Decimal("250")


class CostModel(StrictModel):
    version: str = "bar-taker-v1"
    taker_fee: FinancialDecimal = D("0.008")
    spread_slippage_one_way: FinancialDecimal = D("0.001")
    implementation_allowance: FinancialDecimal = D("0.002")
    protection_limit_offset: FinancialDecimal = D("0.01")
    participation: FinancialDecimal = BOUNDS.maximum_participation
    latency_ms: int = BOUNDS.minimum_latency_ms

    def checked(self) -> CostModel:
        if (
            any(
                not D(0) <= value < D("0.1")
                for value in (
                    self.taker_fee,
                    self.spread_slippage_one_way,
                    self.implementation_allowance,
                    self.protection_limit_offset,
                )
            )
            or not D(0) < self.participation <= BOUNDS.maximum_participation
            or not BOUNDS.minimum_latency_ms <= self.latency_ms <= BOUNDS.maximum_latency_ms
        ):
            raise ValueError("invalid cost model")
        return self


DEFAULT_COSTS = CostModel()
DEFAULT_POLICY = RiskPolicy()


@dataclass(frozen=True)
class Observation:
    at: datetime
    nav: Decimal
    unit_price: Decimal
    exposure: Decimal
    contribution: Decimal


@dataclass(frozen=True)
class Episode:
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    fees: Decimal
    pnl: Decimal
    reason: str


@dataclass(frozen=True)
class Backtest:
    observations: tuple[Observation, ...]
    episodes: tuple[Episode, ...]
    fees: Decimal
    turnover: Decimal
    skips: int
    halts: tuple[str, ...]
    open_quantity: Decimal
    unfilled_stop_bars: int
    interval_minutes: int = 1440


def money(value: Decimal) -> Decimal:
    return value.quantize(D("1e-18"), rounding=ROUND_DOWN)


def simulate(
    bars: tuple[Candle, ...],
    rules: InstrumentRules,
    *,
    variant: Variant | None,
    start: datetime,
    end: datetime,
    initial: Decimal = INITIAL,
    monthly: Decimal = ZERO,
    costs: CostModel = DEFAULT_COSTS,
    benchmark_allocation: Decimal | None = None,
    periodic: bool = False,
    policy: RiskPolicy = DEFAULT_POLICY,
) -> Backtest:
    """No parameter selection. OOS windows begin flat and never use future warm-up.

    A signal at bar i close+10s executes no earlier than bar i+2 open: the first
    *observable full bar* after the decision, rather than leaking bar i+1's opening
    print. This intentionally overstates bar-only latency. Stops trigger on lows but
    cannot fill until a subsequent opening print is at/above their minimum limit.
    Signals are freshly evaluated at that later open using the eligible prefix.
    Opening prices plus slippage approximate post-latency prices; this is a labelled
    historical assumption, not evidence of a fill at the opening timestamp.
    Terminal holdings are marked at a cost-adjusted bid, never silently liquidated.
    """
    costs.checked()
    parse_decimal(initial)
    parse_decimal(monthly)
    start, end = utc_datetime(start), utc_datetime(end)
    if benchmark_allocation is not None:
        parse_decimal(benchmark_allocation)
    if not bars or start >= end or initial <= 0 or monthly < 0:
        raise ValueError("invalid simulation interval or capital")
    if (variant is None) == (benchmark_allocation is None):
        raise ValueError("choose one strategy or benchmark")
    if benchmark_allocation is not None and not 0 <= benchmark_allocation <= 1:
        raise ValueError("invalid benchmark allocation")
    for index, bar in enumerate(bars):
        indicator_only = (
            not bar.complete
            and bar.quality == ("confirmed_no_trade",)
            and bar.volume == 0
            and bar.trade_count == 0
            and bar.open == bar.high == bar.low == bar.close
            and index > 0
            and bar.close == bars[index - 1].close
            and bar.observed_at >= bar.end + timedelta(seconds=10)
        )
        if bar.symbol != rules.symbol or not (bar.complete or indicator_only):
            raise ValueError("unqualified bars")
    if any(previous.end != current.start for previous, current in pairwise(bars)):
        raise ValueError("unexplained gaps cannot be simulated")
    if variant is not None and sum(bar.end <= start for bar in bars) < 200:
        raise ValueError("insufficient pre-OOS warm-up")
    with localcontext(FINANCIAL_CONTEXT):
        return _simulate(
            bars,
            rules,
            variant,
            start,
            end,
            initial,
            monthly,
            costs,
            benchmark_allocation,
            periodic,
            policy,
        )


def _simulate(
    bars: tuple[Candle, ...],
    rules: InstrumentRules,
    variant: Variant | None,
    start: datetime,
    end: datetime,
    initial: Decimal,
    monthly: Decimal,
    costs: CostModel,
    allocation: Decimal | None,
    periodic: bool,
    policy: RiskPolicy,
) -> Backtest:
    cash, units, quantity = initial, initial, D(0)
    fees, turnover = D(0), D(0)
    entry_price, entry_fees, stop, limit = D(0), D(0), D(0), D(0)
    opened = start
    pending: tuple[datetime, str, Decimal] | None = None
    stop_triggered = False
    episode_sold = episode_proceeds = episode_fees = D(0)
    unfilled = skips = 0
    high_water = daily_baseline = D(1)
    day = start.date()
    month = (start.year, start.month)
    daily_pause = False
    latches: set[str] = set()
    observations: list[Observation] = []
    episodes: list[Episode] = []
    first = True
    deferred_benchmark_budget = D(0)
    for index, bar in enumerate(bars):
        if bar.start < start:
            continue
        if bar.end > end:
            break
        bid = bar.open * (1 - costs.spread_slippage_one_way)
        nav = cash + quantity * bid
        unit = nav / units
        if bar.start.date() != day:
            day, daily_baseline, daily_pause = bar.start.date(), unit, False
        contribution = D(0)
        new_month = (bar.start.year, bar.start.month) != month
        if new_month:
            month = (bar.start.year, bar.start.month)
            contribution = monthly
            units += monthly / unit
            cash += monthly
            nav += monthly
        # Evaluate before a queued entry as well as after the close.
        drawdown = 1 - unit / high_water
        if variant is not None:
            if drawdown >= policy.soft_drawdown:
                latches.add("soft_drawdown")
            if drawdown >= policy.hard_drawdown:
                latches.add("hard_drawdown")
            daily_pause |= unit / daily_baseline - 1 <= -policy.daily_loss
        if variant is not None and index >= 201 and "hard_drawdown" not in latches:
            decision = signal(bars[index - 201 : index - 1], variant, bar.start)
            pending = (bar.start, decision.desired, decision.stop_distance)
        due = pending is not None
        exit_reason: str | None = None
        exit_price = D(0)
        if quantity and stop_triggered:
            if bid >= limit:
                exit_reason, exit_price = "protective_limit", limit
            else:
                unfilled += 1
        if (
            quantity
            and exit_reason is None
            and due
            and pending is not None
            and pending[1] == "flat"
        ):
            exit_reason, exit_price = "strategy", bid
        # Historical exit liquidity is also bounded. Residuals keep their protection.
        if exit_reason:
            sold = min(
                quantity,
                round_increment(bar.volume * costs.participation, rules.quantity_increment),
            )
            if sold >= rules.minimum_quantity and sold * exit_price >= rules.minimum_cost:
                fee = sold * exit_price * costs.taker_fee
                cash += sold * exit_price - fee
                allocated_entry_fee = entry_fees * sold / quantity
                entry_fees -= allocated_entry_fee
                fees += fee
                turnover += sold * exit_price
                episode_sold += sold
                episode_proceeds += sold * exit_price
                episode_fees += fee + allocated_entry_fee
                quantity -= sold
                if not quantity:
                    episodes.append(
                        Episode(
                            opened,
                            bar.start,
                            episode_sold,
                            entry_price,
                            episode_proceeds / episode_sold,
                            episode_fees,
                            episode_proceeds - episode_sold * entry_price - episode_fees,
                            exit_reason,
                        )
                    )
                    episode_sold = episode_proceeds = episode_fees = D(0)
                    stop_triggered = False
            else:
                skips += 1
        # A benchmark buys once; periodic purchases use the new external contribution.
        if allocation is not None and allocation > 0:
            if first:
                deferred_benchmark_budget += initial * allocation
            if periodic and new_month:
                deferred_benchmark_budget += contribution * allocation
        benchmark_buy = deferred_benchmark_budget > 0 and bar.complete
        strategy_buy = (
            variant is not None
            and due
            and pending is not None
            and pending[1] == "long"
            and not quantity
            and not latches
            and not daily_pause
            and costs.latency_ms < 2000
            and exit_reason is None
        )
        if strategy_buy or benchmark_buy:
            price = round_increment(
                bar.open * (1 + costs.spread_slippage_one_way), rules.price_increment, up=True
            )
            if strategy_buy:
                if pending is None:
                    raise ValueError("missing decision")
                trigger = round_increment(
                    max(D(0), price - max(pending[2], price * D("0.05"))), rules.price_increment
                )
                proposed_limit = round_increment(
                    trigger * (1 - costs.protection_limit_offset), rules.price_increment, up=True
                )
                if not 0 < proposed_limit < trigger < price:
                    skips += 1
                    proposed = D(0)
                else:
                    sized = size_long(
                        equity=money(nav),
                        free_cash=money(cash),
                        entry=price,
                        protective_limit=proposed_limit,
                        entry_fee=costs.taker_fee,
                        exit_fee=costs.taker_fee,
                        allowance_per_unit=money(price * costs.implementation_allowance),
                        liquidity_quantity=money(bars[index - 2].volume * costs.participation),
                        rules=rules,
                        policy=policy,
                    )
                    proposed = sized.quantity
                    skips += int(sized.reason is not None)
                stop, limit = trigger, proposed_limit
            else:
                if allocation is None:
                    raise ValueError("missing benchmark allocation")
                budget = deferred_benchmark_budget
                deferred_benchmark_budget = D(0)
                proposed = round_increment(
                    min(cash, budget) / (price * (1 + costs.taker_fee)), rules.quantity_increment
                )
                proposed = min(
                    proposed,
                    round_increment(bar.volume * costs.participation, rules.quantity_increment),
                )
                if proposed < rules.minimum_quantity or proposed * price < rules.minimum_cost:
                    proposed = D(0)
                    skips += 1
            # Realised bar volume bounds fills, rather than informing the earlier
            # strategy/sizing decision. A partially filled entry gets no second budget.
            proposed = min(
                proposed,
                round_increment(bar.volume * costs.participation, rules.quantity_increment),
            )
            if (
                proposed < rules.minimum_quantity
                or proposed * price < rules.minimum_cost
                or (strategy_buy and proposed * limit < rules.minimum_cost)
            ):
                proposed = D(0)
            if proposed:
                fee = proposed * price * costs.taker_fee
                entry_price = (quantity * entry_price + proposed * price) / (quantity + proposed)
                quantity += proposed
                entry_fees += fee
                cash -= proposed * price + fee
                fees += fee
                turnover += proposed * price
                opened = bar.start
        pending = None
        if variant is not None and quantity and bar.low <= stop:
            stop_triggered = True
        nav = cash + quantity * bar.close * (1 - costs.spread_slippage_one_way)
        unit = nav / units
        high_water = max(high_water, unit)
        if variant is not None:
            if 1 - unit / high_water >= policy.soft_drawdown:
                latches.add("soft_drawdown")
            if 1 - unit / high_water >= policy.hard_drawdown:
                latches.add("hard_drawdown")
            daily_pause |= unit / daily_baseline - 1 <= -policy.daily_loss
        exposure = quantity * bar.close / nav if nav else D(0)
        observations.append(Observation(bar.end, nav, unit, exposure, contribution))
        first = False
    if not observations:
        raise ValueError("no evaluation bars")
    return Backtest(
        tuple(observations),
        tuple(episodes),
        fees,
        turnover,
        skips,
        tuple(sorted(latches)),
        quantity,
        unfilled,
        bars[0].interval_minutes,
    )
