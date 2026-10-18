"""Float statistics are isolated from Decimal economics. Undefined metrics stay null."""

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np

from aca.simulation.reference import Backtest


def daily_units(result: Backtest) -> dict[str, float]:
    return {
        (row.at - timedelta(microseconds=1)).date().isoformat(): float(row.unit_price)
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
    origin = result.observations[0].at - interval
    elapsed = (result.observations[-1].at - origin).total_seconds() / 86400
    annual = (
        math.expm1(math.log(units[-1]) * 365.25 / elapsed)
        if elapsed > 0 and units[-1] > 0
        else None
    )
    volatility = float(np.std(returns, ddof=1) * math.sqrt(365.25)) if days > 1 else None
    downside = float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2)) * math.sqrt(365.25))
    mean_annual = float(np.mean(returns) * 365.25)
    drawdown = float(np.max(1 - units / np.maximum.accumulate(units)))
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
