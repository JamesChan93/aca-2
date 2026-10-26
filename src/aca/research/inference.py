"""Preregistered conservative inference; floats never enter executable economics."""

import math
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from pydantic import Field

from aca.research.artifacts import canonical, digest, file_hash
from aca.research.bundles import Contract


class StatisticalProtocol(Contract):
    kind: Literal["phase4a-statistics-v1"] = "phase4a-statistics-v1"
    method: Literal["paired_circular_percentile_bonferroni"] = (
        "paired_circular_percentile_bonferroni"
    )
    candidate_ceiling: Literal[26] = 26
    comparisons: tuple[Literal["cash"], Literal["passive10"]] = ("cash", "passive10")
    blocks: tuple[Literal[7], Literal[30]] = (7, 30)
    samples: Annotated[int, Field(ge=262_144, le=1_048_576)] = 262_144
    seed: Literal[1729] = 1729
    alpha: Annotated[float, Field(ge=0.05, le=0.05)] = 0.05
    monte_carlo_error: Annotated[float, Field(ge=0.01, le=0.01)] = 0.01
    minimum_expected_tail: Literal[100] = 100
    selection: Literal["any_individually_eligible_recorded_review_no_return_ranking"] = (
        "any_individually_eligible_recorded_review_no_return_ranking"
    )
    history_reuse: Literal[
        "original20_and_adaptive_review_retained; exploratory_not_independent"
    ] = "original20_and_adaptive_review_retained; exploratory_not_independent"
    capital_selection: Literal["250_initial_250_monthly; other_cases_diagnostic_only"] = (
        "250_initial_250_monthly; other_cases_diagnostic_only"
    )

    @property
    def family_size(self) -> int:
        return self.candidate_ceiling * len(self.comparisons) * len(self.blocks)

    def identity(self) -> str:
        return digest(canonical(self.model_dump(mode="json")))


PROTOCOL = StatisticalProtocol()


def implementation_identity() -> str:
    return file_hash(Path(__file__))


def _means(values: np.ndarray, block: int, samples: int, seed: int) -> np.ndarray:
    """O(samples*n/block) memory-bounded block sums, including a truncated last block."""
    n = len(values)
    extended = np.concatenate((values, values[:block]))
    prefix = np.concatenate(([0.0], np.cumsum(extended)))
    whole, remainder = divmod(n, block)
    sums = prefix[block : block + n] - prefix[:n]
    tails = prefix[remainder : remainder + n] - prefix[:n] if remainder else None
    generator = np.random.Generator(np.random.PCG64(seed))
    means = np.empty(samples, dtype=np.float64)
    for offset in range(0, samples, 256):
        count = min(256, samples - offset)
        starts = generator.integers(0, n, size=(count, whole + bool(remainder)))
        totals = np.sum(sums[starts[:, :whole]], axis=1)
        if tails is not None:
            totals += tails[starts[:, -1]]
        means[offset : offset + count] = totals / n
    return means


def block_bound(
    differences: np.ndarray, block: int, protocol: StatisticalProtocol = PROTOCOL
) -> dict[str, object]:
    if differences.ndim != 1 or not np.isfinite(differences).all():
        raise ValueError("invalid finite one-dimensional daily differences")
    if len(differences) < 2 * block or block not in protocol.blocks:
        raise ValueError("insufficient blocks or undeclared sensitivity")
    # Reserve .01 of the .05 family error budget for Monte Carlo rank error.
    p = (protocol.alpha - protocol.monte_carlo_error) / protocol.family_size
    expected = protocol.samples * p
    if expected < protocol.minimum_expected_tail:
        raise ValueError("insufficient Monte Carlo tail resolution")
    # Binomial lower-tail Chernoff bound: across the whole 104-claim family the
    # probability a retained rank exceeds the ideal bootstrap p-quantile <= .01.
    margin = math.sqrt(2 * expected * math.log(protocol.family_size / protocol.monte_carlo_error))
    rank = max(0, math.floor(expected - margin) - 1)
    baseline_rank = max(
        0, math.floor((protocol.samples - 1) * protocol.alpha / protocol.family_size)
    )
    means = _means(differences, block, protocol.samples, protocol.seed + block)
    means.partition((rank, baseline_rank))
    return {
        "block_days": block,
        "samples": protocol.samples,
        "seed": protocol.seed + block,
        "family_size": protocol.family_size,
        "expected_tail_draws": expected,
        "decision_tail_probability": p,
        "nominal_total_family_error_budget": protocol.alpha,
        "conservative_order_rank_zero_based": rank,
        "monte_carlo_family_error_bound": protocol.monte_carlo_error,
        "annualised_arithmetic_increment": float(np.mean(differences) * 365.25),
        "baseline_bonferroni_lower": float(means[baseline_rank] * 365.25),
        "decision_lower": float(means[rank] * 365.25),
        "numpy_version": np.__version__,
        "coverage_limit": "bootstrap_approximation; MC_bound_is_not_true_return_coverage",
    }


def infer(
    calendar: dict[str, object], protocol: StatisticalProtocol = PROTOCOL
) -> dict[str, object]:
    dates = calendar.get("dates")
    series = calendar.get("returns_decimal")
    if (
        not isinstance(dates, list)
        or not dates
        or len(dates) > 20_000
        or not all(isinstance(day, str) for day in dates)
        or not isinstance(series, dict)
        or calendar.get("first_day_partial") is not False
        or calendar.get("last_day_partial") is not False
    ):
        raise ValueError("full common UTC daily calendar required")
    parsed = [date.fromisoformat(day) for day in dates]
    if any(right - left != timedelta(days=1) for left, right in pairwise(parsed)):
        raise ValueError("missing, duplicated or reordered calendar day")
    arrays: dict[str, np.ndarray] = {}
    for name in ("candidate", *protocol.comparisons):
        rows = series.get(name)
        if (
            not isinstance(rows, list)
            or len(rows) != len(dates)
            or any(not isinstance(value, str) for value in rows)
        ):
            raise ValueError("missing daily evidence; no successful-subset inference")
        values = np.array(rows, dtype=np.float64)
        if not np.isfinite(values).all() or np.any(values <= -1):
            raise ValueError("nonfinite returns or nonpositive wealth")
        arrays[name] = values
    bounds = {
        f"{name}_{block}d": block_bound(arrays["candidate"] - arrays[name], block, protocol)
        for name in protocol.comparisons
        for block in protocol.blocks
    }
    lower = min(float(row["decision_lower"]) for row in bounds.values())  # type: ignore[arg-type]
    return {
        "protocol_sha256": protocol.identity(),
        "calendar_sha256": digest(canonical(calendar)),
        "days": len(dates),
        "bounds": bounds,
        "minimum_lower": lower,
        "statistical_pass": lower > 0,
        "history_independent": False,
        "promotion_allowed": False,
    }


def calibration(*, worlds: int = 64) -> dict[str, object]:
    """Synthetic stress: four correlated processes replicated over 26 definitions.

    Every independent world shares shocks and 12-day volatility clusters across
    four AR(1) processes (.5). Definitions repeat these four paths; both required
    benchmark differences share each path. Count ANY candidate's false positive
    as a family error. This bounded probe cannot establish universal coverage.
    """
    if not 64 <= worlds <= 256:
        raise ValueError("calibration requires at least 64 deterministic worlds")
    generator = np.random.Generator(np.random.PCG64(804_011))
    false_positives = sensitivity = 0
    for _ in range(worlds):
        common = generator.normal(0, 0.0008, 252)
        scales = np.repeat(generator.choice([0.5, 2.0], 21), 12)
        lowers = []
        for _process in range(4):
            shocks = 0.8 * common + 0.6 * generator.normal(0, 0.0008, 252)
            values = np.zeros(252)
            for i in range(1, 252):
                values[i] = 0.5 * values[i - 1] + shocks[i] * scales[i]
            bounds = [block_bound(values, block) for block in PROTOCOL.blocks]
            lowers.append(min(float(row["decision_lower"]) for row in bounds))  # type: ignore[arg-type]
        false_positives += max(lowers) > 0
        # A preregistered large known daily effect, not fitted to observed worlds.
        sensitivity += min(lowers) + 0.002 * 365.25 > 0
    # Exact one-sided 95% binomial upper bound when zero failures are observed.
    upper = 1 - 0.05 ** (1 / worlds) if false_positives == 0 else None
    return {
        "kind": "synthetic_statistical_calibration",
        "implementation_sha256": implementation_identity(),
        "protocol_sha256": PROTOCOL.identity(),
        "worlds": worlds,
        "days": 252,
        "correlated_processes_per_family": 4,
        "definitions_per_family": 26,
        "seed": 804_011,
        "null_false_positives": false_positives,
        "null_fwer_one_sided_95_upper_if_zero": upper,
        "known_daily_effect": 0.002,
        "known_effect_detections": sensitivity,
        "passes_declared_probe": upper is not None and upper < 0.05 and sensitivity >= worlds * 0.9,
        "limitations": [
            "bounded_four_process_family_repeated_over_26_definitions",
            "not_universal_bootstrap_coverage",
            "history_reuse_not_cured",
            "no_actual_strategy_results",
        ],
        "promotion_allowed": False,
    }
