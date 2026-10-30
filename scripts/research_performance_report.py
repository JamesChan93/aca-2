"""Offline descriptive metrics for a reproduced legacy walk-forward study.

Replay every baseline test fold and verify it against the hash-addressed report
before aggregating. Chained flat-start folds are a research diagnostic, never a
continuous-account backtest or evidence for the corrected promotion workflow.
Run with the locked research dependencies; no database or network is used.
"""

import argparse
import csv
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from aca.domain.market import Symbol
from aca.research.artifacts import canonical, file_hash, read_json, write_once
from aca.research.datasets import load_dataset
from aca.research.runner import _append_daily, _curves, code_fingerprint, reconstructed_rules
from aca.research.statistics import daily_returns, metrics
from aca.simulation.reference import CostModel
from aca.strategies.slow import Variant


def series_metrics(
    daily: dict[str, float], observed_units: list[float], elapsed_days: float
) -> dict[str, Any]:
    """Ratios of the combined series, not averages of per-fold ratios.

    Sortino uses a zero daily target and all-day downside RMS. Sharpe uses sample
    daily volatility. Crypto annualisation is 365.25 days. Drawdown includes the
    initial unit and every available bar observation, including across folds.
    """
    returns = list(daily.values())
    if not returns or not observed_units or elapsed_days <= 0:
        raise ValueError("empty performance series or invalid elapsed time")
    if any(not math.isfinite(r) or r <= -1 for r in returns) or any(
        not math.isfinite(u) or u <= 0 for u in observed_units
    ):
        raise ValueError("invalid performance observations")
    terminal = math.prod(1 + r for r in returns)
    if not math.isclose(terminal, observed_units[-1], rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError("daily and bar curves disagree")
    annual = math.expm1(math.log(terminal) * 365.25 / elapsed_days)
    mean_annual = statistics.mean(returns) * 365.25
    volatility = statistics.stdev(returns) * math.sqrt(365.25) if len(returns) > 1 else None
    downside = math.sqrt(statistics.mean(min(r, 0) ** 2 for r in returns) * 365.25)
    peak, drawdown = 1.0, 0.0
    for unit in observed_units:
        peak = max(peak, unit)
        drawdown = max(drawdown, 1 - unit / peak)
    return {
        "total_return": terminal - 1,
        "annualised_return": annual,
        "max_drawdown": drawdown,
        "sortino": mean_annual / downside if downside else None,
        "calmar": annual / drawdown if drawdown else None,
        "sharpe_zero_cash_yield": mean_annual / volatility if volatility else None,
        "annualised_volatility": volatility,
        "expected_shortfall_95_daily": statistics.mean(
            sorted(returns)[: max(1, math.ceil(len(returns) * 0.05))]
        ),
        "daily_observations": len(returns),
        "elapsed_days": elapsed_days,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def export(report_path: Path, dataset_path: Path, output: Path) -> dict[str, Any]:
    report: dict[str, Any] = read_json(report_path)
    report_sha = file_hash(report_path)
    if report_path.stem != report_sha or report.get("kind") != "research_report":
        raise ValueError("expected a hash-addressed legacy research report")
    if report["declaration"]["code_sha256"] != code_fingerprint():
        raise ValueError("installed source differs from rerun report")
    dataset = load_dataset(dataset_path)
    if dataset.fingerprint != report["dataset"]:
        raise ValueError("dataset differs from rerun report")
    output.mkdir(parents=True, exist_ok=True)
    candidates: list[dict[str, Any]] = []
    benchmark_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    for trial in report["trials"]:
        name = trial["trial"]
        symbol_text, interval_text, variant_text = name.split(":")
        symbol, interval = cast(Symbol, symbol_text), int(interval_text)
        variant = cast(Variant, variant_text)
        other_symbol: Symbol = "ETH/GBP" if symbol == "BTC/GBP" else "BTC/GBP"
        combined: dict[str, dict[str, float]] = {}
        units: dict[str, list[float]] = {}
        totals: dict[str, dict[str, Any]] = {}
        previous_end: datetime | None = None
        origin: datetime | None = None
        wins = losses = 0.0
        winning_episodes = completed_episodes = 0
        for index, fold in enumerate(trial["folds"]):
            if fold["status"] != "complete":
                raise ValueError("incomplete folds cannot produce a pooled performance claim")
            start, end = (
                datetime.fromisoformat(fold[key]) for key in ("validation_end", "test_end")
            )
            if previous_end is not None and previous_end != start:
                raise ValueError("test folds overlap or have missing calendar time")
            origin = origin or start
            previous_end = end
            candidate, benchmarks = _curves(
                dataset.series[(symbol, interval)],
                reconstructed_rules(symbol),
                variant,
                start,
                end,
                CostModel(),
                dataset.series[(other_symbol, interval)],
            )
            for label, result in {"candidate": candidate, **benchmarks}.items():
                actual = metrics(result)
                expected = (
                    fold["test"]["candidate"]
                    if label == "candidate"
                    else fold["test"]["benchmarks"][label]
                )
                if actual != expected:
                    raise ValueError(
                        f"baseline replay disagrees with report: {name}/{index}/{label}"
                    )
                daily = daily_returns(result)
                if label in {"candidate", "cash", "passive10"} and (
                    daily != fold["test"]["daily_" + label]
                ):
                    raise ValueError("daily returns disagree with baseline report")
                _append_daily(combined.setdefault(label, {}), daily)
                curve = units.setdefault(label, [1.0])
                scale = curve[-1]
                curve.extend(scale * float(row.unit_price) for row in result.observations)
                total = totals.setdefault(
                    label,
                    {"fees_gbp": 0.0, "turnover_gbp": 0.0, "exposure_sum": 0.0, "count": 0},
                )
                total["fees_gbp"] += float(result.fees)
                total["turnover_gbp"] += float(result.turnover)
                total["exposure_sum"] += sum(float(row.exposure) for row in result.observations)
                total["count"] += len(result.observations)
                fold_rows.append(
                    {
                        "trial": name,
                        "fold": index + 1,
                        "start": start.isoformat(),
                        "end_exclusive": end.isoformat(),
                        "series": label,
                        **{k: v for k, v in actual.items() if not isinstance(v, (list, dict))},
                    }
                )
            for episode in candidate.episodes:
                pnl = float(episode.pnl)
                wins += max(pnl, 0)
                losses += max(-pnl, 0)
                winning_episodes += pnl > 0
                completed_episodes += 1
        if origin is None or previous_end is None:
            raise ValueError("no completed folds")
        elapsed = (previous_end - origin).total_seconds() / 86400
        summaries: dict[str, Any] = {}
        for label, daily in combined.items():
            total = totals[label]
            summary = {
                "trial": name,
                "series": label,
                "oos_start": origin.isoformat(),
                "oos_end_exclusive": previous_end.isoformat(),
                **series_metrics(daily, units[label], elapsed),
                "mean_bar_exposure": total["exposure_sum"] / total["count"],
                "summed_fold_fees_gbp": total["fees_gbp"],
                "summed_fold_turnover_gbp": total["turnover_gbp"],
            }
            summaries[label] = summary
            if label != "candidate":
                benchmark_rows.append(summary)
        bounds = trial["uncertainty"]
        lower_cash = min(
            bounds[f"cash_{block}d"]["simultaneous_95_lower_arithmetic_increment"]
            for block in (7, 30)
        )
        lower_passive = min(
            bounds[f"passive10_{block}d"]["simultaneous_95_lower_arithmetic_increment"]
            for block in (7, 30)
        )
        candidate_row = {
            **summaries["candidate"],
            "complete_test_folds": len(trial["folds"]),
            "completed_episodes": completed_episodes,
            "winning_episodes": winning_episodes,
            "win_rate": winning_episodes / completed_episodes if completed_episodes else None,
            "profit_factor": wins / losses if losses else None,
            "adjusted_lower_increment_vs_cash": lower_cash,
            "adjusted_lower_increment_vs_passive10": lower_passive,
            "legacy_economic_hurdle_pass": trial["economic_gate"]["passes_arithmetic_hurdle"],
            "legacy_status": trial["status"],
        }
        candidates.append(candidate_row)
        for day, value in combined["candidate"].items():
            daily_rows.append(
                {
                    "trial": name,
                    "utc_day": day,
                    "candidate_return": value,
                    "cash_return": combined["cash"][day],
                    "passive10_return": combined["passive10"][day],
                }
            )
        print(f"Verified and summarised {name}: {len(trial['folds'])} folds", flush=True)
    summary = {
        "schema_version": 1,
        "kind": "legacy-walk-forward-descriptive-performance-v1",
        "report_sha256": report_sha,
        "dataset_sha256": dataset.fingerprint,
        "code_sha256": code_fingerprint(),
        "report_script_sha256": file_hash(Path(__file__)),
        "runtime": report["runtime"],
        "candidates": candidates,
        "benchmark_summaries": benchmark_rows,
        "baseline_replay_matches_report": True,
        "drawdown_resolution": "every_available_bar_close_including_inception_and_fold_boundaries",
        "return_basis": "geometrically_chained_independent_flat_start_test_folds",
        "ratio_basis": (
            "pooled_UTC_daily_returns;365.25;zero_target;sample_vol;all_day_downside_RMS"
        ),
        "fees_basis": "sum_of_independent_250_GBP_fold_accounts;not_compounded_account_fees",
        "holdout_opened": False,
        "promotion_evidence": False,
        "continuous_account_result": False,
    }
    write_once(output / "summary.json", canonical(summary))
    write_csv(output / "candidates.csv", candidates)
    write_csv(output / "benchmarks.csv", benchmark_rows)
    write_csv(output / "folds.csv", fold_rows)
    write_csv(output / "daily-returns.csv", daily_rows)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.report, args.dataset, args.output)
