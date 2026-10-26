"""One OOS account and equally funded comparators, independent of report splits.

No inference, candidate selection, dataset acquisition or holdout opening occurs
here. Training/validation must create separate evaluators (``scope`` is recorded).
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from heapq import merge
from itertools import pairwise
from typing import Literal
from uuid import UUID

from aca.accounting.ledger import stored
from aca.domain.market import Candle, InstrumentRules, MarketTrade, Symbol
from aca.domain.timing import FINALISATION
from aca.domain.trading import ZERO, fingerprint
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal, utc_datetime
from aca.operations.config import RiskPolicy
from aca.portfolio.state import nav
from aca.research.calendar import HOLDOUT_START, qualified_bars, reporting_windows
from aca.research.economics import fixed_cost_gate
from aca.research.protocol import months
from aca.simulation.events import EventResult, TradeSimulation
from aca.simulation.passive import PassiveAccount, PassiveResult, proxy_prices
from aca.simulation.reference import CostModel
from aca.strategies.slow import StrategySignal, Variant, signal

ACCOUNT_MODEL = "continuous-oos-equal-flows-v1"


@dataclass(frozen=True)
class Scenario:
    initial: Decimal = Decimal(250)
    monthly: Decimal = Decimal(250)
    infrastructure_monthly: Decimal = Decimal(20)
    fixed_cost_version: str = "planned-household-infrastructure-2026-10-v1"
    fixed_cost_actual: bool = False

    def __post_init__(self) -> None:
        for value in (self.initial, self.monthly, self.infrastructure_monthly):
            parse_decimal(value)
        if (
            self.initial <= 0
            or self.monthly not in {ZERO, Decimal(100), Decimal(250), Decimal(400)}
            or not 0 <= self.infrastructure_monthly <= 35
            or not 1 <= len(self.fixed_cost_version) <= 120
            or type(self.fixed_cost_actual) is not bool
        ):
            raise ValueError("invalid declared capital/contribution/fixed-cost scenario")


@dataclass(frozen=True)
class ScheduledFlow:
    id: str
    at: datetime
    amount: Decimal
    closing_month: str


DEFAULT_SCENARIO = Scenario()
DEFAULT_COSTS = CostModel()


def scenario_grid() -> tuple[Scenario, ...]:
    """Nine capital arithmetic cases, not nine newly selected strategies."""
    return tuple(
        Scenario(initial=opening, monthly=monthly)
        for opening in (Decimal(100), Decimal(250), Decimal(400))
        for monthly in (Decimal(100), Decimal(250), Decimal(400))
    )


def contributions(start: datetime, end: datetime, scenario: Scenario) -> tuple[ScheduledFlow, ...]:
    """Month-end credit at next UTC month 00:00, within [start,end).

    A credit exactly at the terminal boundary belongs to the next interval. All
    accounts receive the same tuples; missing valuation never moves the date.
    """
    start, end = utc_datetime(start), utc_datetime(end)
    if start >= end:
        raise ValueError("invalid contribution interval")
    at = months(start.replace(day=1, hour=0, minute=0, second=0, microsecond=0), 1)
    result: list[ScheduledFlow] = []
    while scenario.monthly and at < end:
        label = (at - timedelta(microseconds=1)).strftime("%Y-%m")
        result.append(ScheduledFlow(f"month-end:{label}", at, scenario.monthly, label))
        at = months(at, 1)
    return tuple(result)


@dataclass(frozen=True)
class ReviewInput:
    id: str
    at: datetime
    reason: str
    evidence_sha256: str
    expected_epoch: UUID


@dataclass(frozen=True)
class AccountSnapshot:
    at: datetime
    candidate: EventResult
    comparators: dict[str, PassiveResult]
    always_long: EventResult


@dataclass(frozen=True)
class Comparison:
    scope: Literal["out_of_sample", "training", "validation"]
    scenario: Scenario
    flows: tuple[ScheduledFlow, ...]
    final: AccountSnapshot
    reports: tuple[AccountSnapshot, ...]
    common_start: datetime
    common_end: datetime
    symbol: Symbol
    variant: Variant
    costs: CostModel
    rules: dict[Symbol, InstrumentRules]
    policy: RiskPolicy
    interval_minutes: int = 1440
    version: str = ACCOUNT_MODEL
    promotion_evidence: bool = False


class ContinuousEvaluation:
    """Incremental, deterministic prefix replay; every window uses these accounts.

    Ties: closed-signal timers, received prints in source order, midnight marks,
    explicit reviews, contributions, then diagnostic deployments. Reporting is
    a snapshot after all events strictly before its cut; it has no trading rule.
    """

    def __init__(
        self,
        bars: Mapping[Symbol, Sequence[Candle]],
        trades: Iterable[MarketTrade],
        rules: dict[Symbol, InstrumentRules],
        *,
        symbol: Symbol,
        variant: Variant,
        start: datetime,
        end: datetime,
        scenario: Scenario = DEFAULT_SCENARIO,
        costs: CostModel = DEFAULT_COSTS,
        scope: Literal["out_of_sample", "training", "validation"] = "out_of_sample",
        reviews: Sequence[ReviewInput] = (),
    ) -> None:
        self.start, self.end = utc_datetime(start), utc_datetime(end)
        start, end = self.start, self.end
        if start >= end or end > HOLDOUT_START or set(bars) != {"BTC/GBP", "ETH/GBP"}:
            raise ValueError("invalid common pre-holdout evaluation")
        if set(rules) != set(bars) or scope not in {"out_of_sample", "training", "validation"}:
            raise ValueError("incomplete comparison mandate/scope")
        self.bars = {s: qualified_bars(v, s, start, end) for s, v in bars.items()}
        if len({v[-1].interval_minutes for v in self.bars.values()}) != 1:
            raise ValueError("comparators require one common bar interval")
        self.symbol, self.variant, self.scope = symbol, variant, scope
        self.scenario, self.costs = scenario, costs.checked()
        namespace = f"{ACCOUNT_MODEL}:{scope}:{symbol}:{variant}:{scenario.monthly}"
        self.candidate = TradeSimulation(
            rules[symbol],
            start=start,
            initial=scenario.initial,
            costs=costs,
            identity_namespace=f"{namespace}:candidate",
        )
        self.always_long = TradeSimulation(
            rules[symbol],
            start=start,
            initial=scenario.initial,
            costs=costs,
            identity_namespace=f"{namespace}:risk-sized-always-long",
        )
        weights: dict[str, dict[Symbol, Decimal]] = {
            "cash": {},
            "passive10": {symbol: Decimal(".1")},
            "passive30": {symbol: Decimal(".3")},
            "buy_hold": {symbol: Decimal(1)},
            "monthly_purchases": {symbol: Decimal(1)},
            "btc_buy_hold": {"BTC/GBP": Decimal(1)},
            "eth_buy_hold": {"ETH/GBP": Decimal(1)},
            "blend50_50": {"BTC/GBP": Decimal(".5"), "ETH/GBP": Decimal(".5")},
            "past_exposure_matched": {},
            "past_risk_matched": {},
        }
        self.comparators = {
            name: PassiveAccount(
                f"{namespace}:{name}", rules, w, start=start, initial=scenario.initial, costs=costs
            )
            for name, w in weights.items()
        }
        self.flows = contributions(start, end, scenario)
        self.events: list[tuple[datetime, int, int, str, object]] = []
        for index, bar in enumerate(self.bars[symbol]):
            at = bar.end + FINALISATION
            if index >= 199 and start <= at < end:
                self.events.append((at, 0, index, "signal", index))
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if midnight < start:
            midnight += timedelta(days=1)
        while midnight < end:
            self.events.append((midnight, 2, 0, "midnight", None))
            midnight += timedelta(days=1)
        for index, review in enumerate(reviews):
            if not start <= utc_datetime(review.at) < end:
                raise ValueError("review outside evaluation")
            self.events.append((review.at, 3, index, "review", review))
        for index, flow in enumerate(self.flows):
            self.events.append((flow.at, 4, index, "flow", flow))
        month = months(start.replace(day=1, hour=0, minute=0, second=0, microsecond=0), 1)
        while month < end:
            self.events.append((month, 5, 0, "diagnostics", None))
            month = months(month, 1)
        self.events.sort(key=lambda e: e[:3])
        self.stream = merge(self.events, self._trade_events(trades), key=lambda e: e[:3])
        self.next_event = next(self.stream, None)
        self.cursor = 0
        self.at = start
        self.last_signal: StrategySignal | None = None
        self.diagnostic_targets: list[dict[str, str]] = []
        self.failed = False

    def _trade_events(
        self, trades: Iterable[MarketTrade]
    ) -> Iterator[tuple[datetime, int, int, str, object]]:
        prior = self.start
        for index, trade in enumerate(trades):
            if not self.start <= trade.received_at < self.end or trade.received_at < prior:
                raise ValueError("out-of-range/out-of-order common trade evidence")
            prior = trade.received_at
            yield trade.received_at, 1, index, "trade", trade

    def _diagnostics(self, at: datetime) -> None:
        elapsed = Decimal(str((at - self.start).total_seconds()))
        if not elapsed or self.candidate.latest is None:
            return
        if any(
            not 0 <= (at - stamp).total_seconds() <= 5
            for stamp in (self.candidate.latest.received_at, self.candidate.latest.event_at)
        ):
            for name in ("past_exposure_matched", "past_risk_matched"):
                self.comparators[name].skips["diagnostic_missing_fresh_mark"] += 1
            return
        with localcontext(FINANCIAL_CONTEXT):
            candidate = self.candidate.result(at)
            exposure = candidate.exposure_fraction_seconds / elapsed
            risk = candidate.planned_risk_fraction_seconds / elapsed
            _, ask = proxy_prices(self.candidate.latest, self.candidate.rules, self.costs)
            distance = max(
                self.last_signal.stop_distance if self.last_signal else ZERO, ask * Decimal(".05")
            )
            loss_fraction = (
                distance / ask
                + Decimal(".01")
                + 2 * max(Decimal(".008"), self.costs.taker_fee)
                + Decimal(".002")
            )
            targets = {"past_exposure_matched": exposure, "past_risk_matched": risk / loss_fraction}
            for name, target in targets.items():
                account = self.comparators[name]
                target = min(self.candidate.policy.asset_exposure, max(ZERO, target))
                try:
                    value = nav(account.state, at)
                except ValueError:
                    account.skips["diagnostic_missing_fresh_mark"] += 1
                    continue
                held = value - account.state.quantity("GBP")
                budget = stored(
                    max(ZERO, min(value * target - held, account.state.quantity("GBP")))
                )
                account.budget(f"monthly-diagnostic:{at.isoformat()}", self.symbol, budget, at)
                self.diagnostic_targets.append(
                    {
                        "at": at.isoformat(),
                        "name": name,
                        "prior_mean_exposure": str(exposure),
                        "prior_mean_planned_risk": str(risk),
                        "target_allocation": str(target),
                        "buy_budget_gbp": str(budget),
                        "existing_inventory_gbp": str(held),
                    }
                )

    def advance(self, until: datetime) -> AccountSnapshot:
        until = utc_datetime(until)
        if self.failed:
            raise ValueError("failed continuous evaluation requires a fresh deterministic replay")
        if not self.at <= until <= self.end:
            raise ValueError("continuous evaluation clock regressed/outside range")
        # If any account/input fails, no partially applied cross-account state
        # can subsequently be returned as a completed comparable result.
        self.failed = True
        while self.next_event is not None and self.next_event[0] < until:
            at, _, _, kind, value = self.next_event
            if kind == "signal":
                if not isinstance(value, int):
                    raise TypeError("invalid signal timer")
                prefix = self.bars[self.symbol][max(0, value - 200) : value + 1]
                decision = signal(prefix, self.variant, at, version="slow-v2")
                self.last_signal = decision
                self.candidate.publish(decision, at)
                # Same ATR/risk/sizing policy; long every new closed-bar decision,
                # with no intra-decision re-entry after a stop.
                self.always_long.publish(decision.model_copy(update={"desired": "long"}), at)
            elif kind == "trade":
                if not isinstance(value, MarketTrade):
                    raise TypeError("invalid trade input")
                if value.symbol == self.symbol:
                    self.candidate.trade(value)
                    self.always_long.trade(value)
                for account in self.comparators.values():
                    account.trade(value)
            elif kind == "midnight":
                self.candidate.mark_time(at)
                self.always_long.mark_time(at)
            elif kind == "review":
                if not isinstance(value, ReviewInput):
                    raise TypeError("invalid review input")
                self.candidate.review_epoch(
                    value.id, value.reason, value.evidence_sha256, value.expected_epoch, at
                )
                # A candidate review is not authority to reset a different account.
            elif kind == "flow":
                if not isinstance(value, ScheduledFlow):
                    raise TypeError("invalid flow input")
                self.candidate.cashflow(value.id, value.amount, at)
                self.always_long.cashflow(value.id, value.amount, at)
                for account in self.comparators.values():
                    account.cashflow(value.id, value.amount, at)
            else:
                self._diagnostics(at)
            self.cursor += 1
            self.next_event = next(self.stream, None)
        self.at = until
        snapshot = AccountSnapshot(
            until,
            self.candidate.result(until),
            {name: a.result(until) for name, a in self.comparators.items()},
            self.always_long.result(until),
        )
        self.failed = False
        return snapshot

    def run(self, boundaries: Sequence[datetime] = ()) -> Comparison:
        windows = reporting_windows(self.start, self.end, boundaries)
        snapshots = tuple(self.advance(end) for _, end in windows)
        return Comparison(
            self.scope,
            self.scenario,
            self.flows,
            snapshots[-1],
            snapshots,
            self.start,
            self.end,
            self.symbol,
            self.variant,
            self.costs,
            dict(self.comparators["cash"].rules),
            self.candidate.policy,
            interval_minutes=self.bars[self.symbol][0].interval_minutes,
        )


def economic_assessment(
    result: EventResult,
    scenario: Scenario,
    *,
    incremental_lower_bound: Decimal | None,
    statistical_eligible: bool | None,
) -> dict[str, object]:
    """Report scenario economics without manufacturing a confidence decision."""
    with localcontext(FINANCIAL_CONTEXT):
        start = result.backtest.started_at
        if start is None:
            raise ValueError("continuous account needs an inception")
        seconds = Decimal(str((result.requested_through - start).total_seconds()))
        if seconds <= 0:
            raise ValueError("economic assessment needs observed elapsed time")
        average = stored(result.equity_seconds / seconds)
        annual_cost = scenario.infrastructure_monthly * 12
        infrastructure = stored(annual_cost * seconds / Decimal("31557600"))
        gate = (
            None
            if incremental_lower_bound is None
            else fixed_cost_gate(average, incremental_lower_bound, annual_fixed_cost=annual_cost)
        )
        return {
            "average_equity_gbp": str(average),
            "statistical_eligible": statistical_eligible,
            "economic_arithmetic": gate,
            "economic_eligible": bool(
                statistical_eligible is True
                and gate is not None
                and gate["passes_arithmetic_hurdle"]
            ),
            "required_average_equity_at_supplied_bound_gbp": (
                str(2 * annual_cost / incremental_lower_bound)
                if incremental_lower_bound is not None and incremental_lower_bound > 0
                else None
            ),
            "fixed_cost_version": scenario.fixed_cost_version,
            "fixed_cost_actual": scenario.fixed_cost_actual,
            "separate_infrastructure_gbp": str(infrastructure),
            "trading_nav_gbp": str(result.attribution["ending_nav_gbp"]),
            "household_wealth_before_tax_gbp": str(
                stored(result.attribution["ending_nav_gbp"] - infrastructure)
            ),
            "tax_assessment": "not_computed",
            "execution_permission": False,
        }


def economic_frontier(
    cases: Sequence[Comparison],
    *,
    evidence: Mapping[str, tuple[Decimal | None, bool | None]] | None = None,
) -> dict[str, object]:
    """Compare a declared scenario grid without choosing a winner or changing risk.

    A supplied bound must be justified by the later statistical review. The
    arithmetic surface is not a forecast and does not establish that bound.
    """
    if not cases:
        raise ValueError("empty economic frontier")
    first = cases[0]
    evidence = evidence or {}
    allowed = {snapshot_identity(case.final) for case in cases}
    if not set(evidence) <= allowed:
        raise ValueError("economic frontier evidence belongs to a different account result")
    identities: set[tuple[Decimal, Decimal]] = set()
    rows = []
    for case in cases:
        identity = (case.scenario.initial, case.scenario.monthly)
        if identity in identities or (
            case.common_start,
            case.common_end,
            case.symbol,
            case.variant,
            case.costs,
            case.rules,
            case.scope,
        ) != (
            first.common_start,
            first.common_end,
            first.symbol,
            first.variant,
            first.costs,
            first.rules,
            first.scope,
        ):
            raise ValueError("duplicate/noncomparable economic frontier cases")
        identities.add(identity)
        account_identity = snapshot_identity(case.final)
        bound, eligible = evidence.get(account_identity, (None, None))
        rows.append(
            {
                "account_identity": account_identity,
                "initial_gbp": str(case.scenario.initial),
                "monthly_gbp": str(case.scenario.monthly),
                **economic_assessment(
                    case.final.candidate,
                    case.scenario,
                    incremental_lower_bound=bound,
                    statistical_eligible=eligible,
                ),
            }
        )
    return {
        "kind": "continuous-scenario-economic-frontier-v1",
        "cases": rows,
        "bound_is_supplied_evidence_not_established_here": True,
        "no_sizing_or_policy_change": True,
        "promotion_evidence": False,
    }


def snapshot_identity(value: AccountSnapshot) -> str:
    """Bind cumulative account evidence for independent split-equivalence probes."""
    return fingerprint(
        {
            "at": value.at.isoformat(),
            "candidate": value.candidate.state.document(),
            "candidate_fills": [f.document() for f in value.candidate.fills],
            "comparators": {
                name: account.state.document()
                for name, account in sorted(value.comparators.items())
            },
            "always_long": value.always_long.state.document(),
        }
    )


def comparison_report(value: Comparison) -> dict[str, object]:
    """Whole-account economics plus a complete daily availability calendar.

    Missing daily boundaries remain null, never removed or forward filled into a
    paired sample. A later statistical gate must reject incomplete required data.
    """
    from aca.research.statistics import event_metrics, metrics

    final = value.final
    curves = {
        "candidate": final.candidate.backtest,
        "risk_sized_always_long": final.always_long.backtest,
        **{name: account.backtest for name, account in final.comparators.items()},
    }
    edges = [value.common_start]
    midnight = value.common_start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    while midnight < value.common_end:
        edges.append(midnight)
        midnight += timedelta(days=1)
    edges.append(value.common_end)
    dates = [at.date().isoformat() for at in edges[:-1]]
    daily: dict[str, list[str | None]] = {}
    missing: dict[str, list[str]] = {}
    for name, curve in curves.items():
        cursor = 0
        endpoints: list[Decimal | None] = []
        for at in edges:
            while cursor + 1 < len(curve.observations) and curve.observations[cursor + 1].at <= at:
                cursor += 1
            row = curve.observations[cursor]
            valid = row.at <= at and (row.exposure == 0 or (at - row.at).total_seconds() <= 5)
            endpoints.append(row.unit_price if valid else None)
        returns: list[str | None] = []
        for prior, current in pairwise(endpoints):
            with localcontext(FINANCIAL_CONTEXT):
                returns.append(
                    str(current / prior - 1) if prior is not None and current is not None else None
                )
        daily[name] = returns
        missing[name] = [day for day, result in zip(dates, returns, strict=True) if result is None]
    reports: dict[str, object] = {}
    for name, account in final.comparators.items():
        report = metrics(account.backtest, initial=float(value.scenario.initial))
        report.pop("open_quantity")  # BTC + ETH is not a meaningful quantity unit.
        report.update(
            {
                "inventory": {
                    b.asset: str(b.quantity) for b in account.state.balances if b.asset != "GBP"
                },
                "attribution_gbp": {k: str(v) for k, v in account.attribution.items()},
                "intended_budget_gbp": str(sum((b.amount for b in account.budgets), ZERO)),
                "achieved_spend_gbp": str(sum((b.spent for b in account.budgets), ZERO)),
                "unspent_budget_gbp": str(sum((b.amount - b.spent for b in account.budgets), ZERO)),
                "mean_exposure": str(account.mean_exposure),
                "mean_equity_gbp": str(account.mean_equity),
                "stale_terminal_mark": account.stale_terminal_mark,
                "missing_cross_asset_valuation_observations": (
                    account.missing_valuation_observations
                ),
                "skip_reasons": account.skip_reasons,
                "execution_model": account.execution_model,
                "mandate": (
                    "buy_only_funded_passive; no_strategy_stops_or_trading_halts; no_residual_chase"
                ),
            }
        )
        reports[name] = report
    return {
        "schema_version": 1,
        "kind": ACCOUNT_MODEL,
        "scope": value.scope,
        "symbol": value.symbol,
        "variant": value.variant,
        "interval_minutes": value.interval_minutes,
        "costs": value.costs.model_dump(mode="json"),
        "instrument_rules": {s: r.model_dump(mode="json") for s, r in sorted(value.rules.items())},
        "risk_policy": value.policy.model_dump(mode="json"),
        "start": value.common_start.isoformat(),
        "end_exclusive": value.common_end.isoformat(),
        "scenario": {
            "initial_gbp": str(value.scenario.initial),
            "monthly_gbp": str(value.scenario.monthly),
            "infrastructure_monthly_gbp": str(value.scenario.infrastructure_monthly),
            "fixed_cost_version": value.scenario.fixed_cost_version,
            "fixed_cost_actual": value.scenario.fixed_cost_actual,
        },
        "flows": [
            {
                "id": flow.id,
                "at": flow.at.isoformat(),
                "amount_gbp": str(flow.amount),
                "closing_month": flow.closing_month,
            }
            for flow in value.flows
        ],
        "candidate": event_metrics(final.candidate),
        "comparators": reports,
        "risk_sized_always_long": event_metrics(final.always_long),
        "report_boundaries": [s.at.isoformat() for s in value.reports],
        "final_account_identity": snapshot_identity(final),
        "calendar": {
            "dates": dates,
            "returns_decimal": daily,
            "missing_days": missing,
            "first_day_partial": value.common_start
            != datetime.combine(value.common_start.date(), datetime.min.time(), UTC),
            "last_day_partial": value.common_end.time() != datetime.min.time(),
            "statistical_sample_complete": not any(missing.values()),
            "sampling": "UTC_close_with_five_second_evidence; no_missing_day_deletion",
        },
        "economics": economic_assessment(
            final.candidate, value.scenario, incremental_lower_bound=None, statistical_eligible=None
        ),
        "promotion_evidence": False,
        "holdout_opened": False,
        "live_execution_available": False,
    }
