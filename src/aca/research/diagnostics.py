"""Descriptive episode and missed-participation evidence, never selection inputs."""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from aca.domain.market import Candle, Symbol
from aca.domain.trading import ZERO
from aca.domain.values import FINANCIAL_CONTEXT
from aca.simulation.events import EventResult
from aca.strategies.slow import StrategySignal


def describe(
    result: EventResult,
    signals: Sequence[tuple[StrategySignal, Decimal]],
    bars: Mapping[Symbol, Sequence[Candle]],
    end: datetime,
) -> dict[str, object]:
    episodes: list[dict[str, object]] = []
    with localcontext(FINANCIAL_CONTEXT):
        for entry in (o for o in result.state.orders if o.intent.purpose == "entry"):
            orders = {
                o.intent.client_id
                for o in result.state.orders
                if o.intent.episode_id == entry.intent.episode_id
            }
            fills = [f for f in result.fills if f.client_id in orders]
            buys = [f for f in fills if f.side == "buy"]
            sells = [f for f in fills if f.side == "sell"]
            if not buys:
                continue
            bought = sum((f.quantity for f in buys), ZERO)
            sold = sum((f.quantity for f in sells), ZERO)
            fees = sum((fee.quantity for f in fills for fee in f.fees), ZERO)
            complete = bought == sold
            pnl = (
                (
                    sum((f.quantity * f.price for f in sells), ZERO)
                    - sum((f.quantity * f.price for f in buys), ZERO)
                    - fees
                )
                if complete
                else None
            )
            episodes.append(
                {
                    "episode": str(entry.intent.episode_id),
                    "symbol": entry.intent.symbol,
                    "opened_at": buys[0].occurred_at.isoformat(),
                    "closed_at": sells[-1].occurred_at.isoformat() if complete else None,
                    "bought": str(bought),
                    "sold": str(sold),
                    "remaining": str(bought - sold),
                    "fees_gbp": str(fees),
                    "completed_pnl_gbp": str(pnl) if pnl is not None else None,
                    "whipsaw": bool(
                        complete
                        and pnl is not None
                        and pnl < 0
                        and sells[-1].occurred_at - buys[0].occurred_at <= timedelta(days=7)
                    ),
                }
            )
        by_end = {
            s: {b.end: b for b in rows if b.end <= end and b.interval_minutes == 1440}
            for s, rows in bars.items()
        }
        missed: list[dict[str, object]] = []
        for decision, held in signals:
            if held or decision.desired == "long":
                continue
            current = by_end[decision.symbol].get(decision.data_watermark)
            following = by_end[decision.symbol].get(decision.data_watermark + timedelta(days=1))
            if current is not None and following is not None and following.close > current.close:
                missed.append(
                    {
                        "at": decision.decision_at.isoformat(),
                        "symbol": decision.symbol,
                        "desired": decision.desired,
                        "subsequent_close_return": str(following.close / current.close - 1),
                    }
                )
        losses: dict[str, set[Symbol]] = {}
        for symbol, rows in bars.items():
            prior: Candle | None = None
            for bar in rows:
                if bar.interval_minutes != 1440:
                    continue
                if bar.end > end:
                    break
                if (
                    prior is not None
                    and bar.end > result.backtest.observations[0].at
                    and bar.close < prior.close
                ):
                    losses.setdefault(bar.end.date().isoformat(), set()).add(symbol)
                prior = bar
    return {
        "episodes": episodes,
        "whipsaw_definition": "completed_negative_pnl_with_duration_at_most_7_days",
        "missed_trend_definition": (
            "flat_nonentry_decision_then_positive_next_daily_close; descriptive_only"
        ),
        "missed_trend_observations": missed,
        "both_asset_close_loss_days": sorted(
            day for day, members in losses.items() if len(members) == 2
        ),
        "joint_uncertainty": (
            "whole_account_unit_returns; paired_7_and_30_day_blocks; fixed_104_claims"
        ),
        "parameter_and_cost_sensitivities": "registered_unrun; separate_bound_review_required",
        "selection_input": False,
    }
