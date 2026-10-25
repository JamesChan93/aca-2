"""Float statistics are isolated from Decimal economics. Undefined metrics stay null."""

import math
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np

from aca.simulation.reference import Backtest

if TYPE_CHECKING:
    from aca.simulation.events import EventResult


def daily_units(result: Backtest) -> dict[str, float]:
    return {
        (row.at - timedelta(microseconds=1) if row.interval_close else row.at)
        .date()
        .isoformat(): float(row.unit_price)
        for row in result.observations
    }


def daily_returns(result: Backtest) -> dict[str, float]:
    previous = 1.0
    returns: dict[str, float] = {}
    for day, unit in daily_units(result).items():
        returns[day] = unit / previous - 1
        previous = unit
    return returns


def xirr(flows: Sequence[tuple[datetime, float]]) -> float | None:
    """Normal investor flows only. Other sign patterns may have multiple roots."""
    if len(flows) < 2 or flows[-1][1] <= 0 or any(value >= 0 for _, value in flows[:-1]):
        return None
    origin = flows[0][0]
    years = [(at - origin).total_seconds() / (365.25 * 86400) for at, _ in flows]
    if years[-1] <= 0:
        return None

    def npv(log_rate: float) -> float:
        return sum(
            value * math.exp(-log_rate * year)
            for (_, value), year in zip(flows, years, strict=True)
        )

    low, high = -20.0, 20.0
    try:
        if npv(low) * npv(high) >= 0:
            return None
        for _ in range(200):
            middle = (low + high) / 2
            if npv(middle) > 0:
                low = middle
            else:
                high = middle
        return math.expm1((low + high) / 2)
    except OverflowError:
        return None


def metrics(result: Backtest, initial: float = 250.0) -> dict[str, object]:
    returns = np.array(list(daily_returns(result).values()), dtype=np.float64)
    units = np.array([1.0, *daily_units(result).values()], dtype=np.float64)
    days = len(returns)
    total = units[-1] - 1
    interval = timedelta(minutes=result.interval_minutes)
    origin = result.started_at or result.observations[0].at - interval
    elapsed = (result.observations[-1].at - origin).total_seconds() / 86400
    annual = (
        math.expm1(math.log(units[-1]) * 365.25 / elapsed)
        if elapsed > 0 and units[-1] > 0
        else None
    )
    volatility = float(np.std(returns, ddof=1) * math.sqrt(365.25)) if days > 1 else None
    downside = float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2)) * math.sqrt(365.25))
    mean_annual = float(np.mean(returns) * 365.25)
    observed_units = np.array(
        [1.0, *(float(row.unit_price) for row in result.observations)], dtype=np.float64
    )
    drawdown = float(np.max(1 - observed_units / np.maximum.accumulate(observed_units)))
    wins = sum(float(episode.pnl) for episode in result.episodes if episode.pnl > 0)
    losses = -sum(float(episode.pnl) for episode in result.episodes if episode.pnl < 0)
    flows = [(origin, -initial)]
    flows.extend(
        (row.at, -float(row.contribution)) for row in result.observations if row.contribution
    )
    flows.append((result.observations[-1].at, float(result.observations[-1].nav)))
    return {
        "twr": float(total),
        "annualised_return": annual,
        "mwr_xirr": xirr(flows),
        "max_drawdown": drawdown,
        "drawdown_observation_resolution": result.observation_resolution,
        "drawdown_includes_inception": True,
        "annualised_volatility": volatility,
        "sharpe_zero_cash_yield": mean_annual / volatility if volatility else None,
        "sortino": mean_annual / downside if downside else None,
        "calmar": annual / drawdown if annual is not None and drawdown else None,
        "profit_factor": wins / losses if losses else None,
        "expected_shortfall_95_daily": float(
            np.mean(np.sort(returns)[: max(1, math.ceil(days * 0.05))])
        ),
        "mean_exposure": float(np.mean([float(row.exposure) for row in result.observations])),
        "mean_equity_gbp": str(
            sum(row.nav for row in result.observations) / len(result.observations)
        ),
        "turnover_notional_gbp": str(result.turnover),
        "fees_gbp": str(result.fees),
        "completed_episodes": len(result.episodes),
        "skipped_entries_or_exits": result.skips,
        "open_quantity": str(result.open_quantity),
        "unfilled_stop_bars": result.unfilled_stop_bars,
        "latched_halts": list(result.halts),
        "ending_nav_gbp": str(result.observations[-1].nav),
        "regime_calendar_year": {
            year: float(
                np.prod(
                    [
                        1 + value
                        for day, value in daily_returns(result).items()
                        if day.startswith(year)
                    ]
                )
                - 1
            )
            for year in sorted({day[:4] for day in daily_returns(result)})
        },
    }


def event_metrics(result: EventResult) -> dict[str, object]:
    """Report observed economics separately from unavailable terminal evidence.

    Exposure/halts are durations under the last known state, not invented price
    observations inside gaps. Daily returns sample the last available observation
    per UTC day. A data-assurance/calendar review is required before inference.
    """
    from aca.domain.values import FINANCIAL_CONTEXT

    report = metrics(result.backtest, initial=float(result.attribution["opening_nav_gbp"]))
    origin = result.backtest.started_at or result.backtest.observations[0].at
    duration = Decimal(str((result.requested_through - origin).total_seconds()))
    from decimal import localcontext

    with localcontext(FINANCIAL_CONTEXT):
        exposure = result.exposure_fraction_seconds / duration if duration else None
        equity = result.equity_seconds / duration if duration else None
    report.pop("unfilled_stop_bars")
    report.update(
        execution_model=result.execution_model,
        clock_version=result.clock_version,
        attribution_gbp={key: str(value) for key, value in result.attribution.items()},
        skip_reasons=result.skip_reasons,
        halt_seconds=str(result.halt_seconds),
        position_occupied_seconds=str(result.exposure_seconds),
        mean_exposure=float(exposure) if exposure is not None else None,
        mean_equity_gbp=str(equity) if equity is not None else None,
        exposure_sampling="last_known_state_duration; price_gaps_not_interpolated",
        daily_sampling="last_available_observation_in_each_UTC_day; coverage_requires_review",
        observed_through=result.observed_through.isoformat(),
        requested_through=result.requested_through.isoformat(),
        stale_terminal_mark=result.stale_terminal_mark,
        max_observation_gap_seconds=max(
            (
                (right.at - left.at).total_seconds()
                for left, right in zip(
                    result.backtest.observations, result.backtest.observations[1:], strict=False
                )
            ),
            default=0,
        ),
        open_episode=(
            {key: str(value) for key, value in asdict(result.open_episode).items()}
            if result.open_episode is not None
            else None
        ),
        outstanding_orders=sum(
            o.state not in {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}
            for o in result.state.orders
        ),
        unfilled_stop_observations=result.backtest.unfilled_stop_bars,
        promotion_evidence=False,
        live_execution_available=False,
    )
    return report


def paired_bootstrap(
    strategy: dict[str, float],
    benchmark: dict[str, float],
    *,
    block_days: int,
    seed: int = 1729,
    samples: int = 10_000,
    family_size: int = 80,
) -> dict[str, object]:
    """Circular paired blocks of daily return differences; Bonferroni lower bound.

    All paired days must match. Resample the same dates for both curves. Family size
    covers 20 trials x two comparisons x two block sensitivities.
    Stress results cannot replace the baseline comparisons.
    """
    if list(strategy) != list(benchmark) or block_days < 1 or samples < 1000 or family_size < 1:
        raise ValueError("invalid paired bootstrap")
    n = len(strategy)
    if n < 2 * block_days:
        return {"available": False, "reason": "fewer_than_two_blocks"}
    left, right = np.array(list(strategy.values())), np.array(list(benchmark.values()))
    if np.any(left <= -1) or np.any(right <= -1):
        return {"available": False, "reason": "nonpositive_wealth"}
    differences = left - right
    generator = np.random.Generator(np.random.PCG64(seed))
    means: list[float] = []
    blocks = math.ceil(n / block_days)
    for offset in range(0, samples, 64):
        count = min(64, samples - offset)
        starts = generator.integers(0, n, size=(count, blocks, 1))
        indices = ((starts + np.arange(block_days)) % n).reshape(count, -1)[:, :n]
        means.extend(np.mean(differences[indices], axis=1).tolist())
    lower = float(np.quantile(means, 0.05 / family_size, method="lower"))
    return {
        "available": True,
        "block_days": block_days,
        "samples": samples,
        "seed": seed,
        "family_size": family_size,
        "numpy_version": np.__version__,
        "annualised_arithmetic_increment": float(np.mean(differences) * 365.25),
        "simultaneous_95_lower_arithmetic_increment": lower * 365.25,
        "method": "paired_circular_blocks_bonferroni_one_sided",
    }
