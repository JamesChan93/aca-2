"""Predeclared experiments with durable start/outcome records and a frozen holdout."""

import platform
import sys
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from aca import __version__
from aca.domain.market import Candle, InstrumentRules, Symbol
from aca.features.indicators import sma
from aca.operations.config import RiskPolicy
from aca.research.artifacts import (
    canonical,
    digest,
    file_hash,
    read_json,
    save_document,
)
from aca.research.datasets import Dataset, load_dataset
from aca.research.economics import fixed_cost_gate
from aca.research.protocol import TRIALS, months, walk_forward
from aca.research.statistics import daily_returns, metrics, paired_bootstrap
from aca.simulation.reference import Backtest, CostModel, Observation, money, simulate
from aca.strategies.slow import Variant

ZERO = Decimal(0)
RULE_DATE = datetime(2026, 10, 17, tzinfo=UTC)


class ResearchDataError(ValueError):
    """Missing evidence; unexpected arithmetic/programming errors must fail the run."""


def reconstructed_rules(symbol: Symbol) -> InstrumentRules:
    """Labelled current-rule reconstruction, not historical or account eligibility."""
    return InstrumentRules(
        venue="kraken",
        symbol=symbol,
        venue_symbol=symbol,
        rest_pair="XXBTZGBP" if symbol == "BTC/GBP" else "XETHZGBP",
        price_increment=Decimal("0.1" if symbol == "BTC/GBP" else "0.01"),
        quantity_increment=Decimal("0.00000001"),
        minimum_quantity=Decimal("0.00006" if symbol == "BTC/GBP" else "0.002"),
        minimum_cost=Decimal("0.43"),
        status="online",
        observed_at=RULE_DATE,
    )


def code_fingerprint() -> str:
    package = Path(__file__).parents[1]
    sources = {
        str(path.relative_to(package)): file_hash(path) for path in sorted(package.rglob("*.py"))
    }
    return digest(canonical(sources))


def event(root: Path, run: str, trial: str, status: str, detail: dict[str, object]) -> Path:
    document: dict[str, object] = {
        "schema_version": 1,
        "id": str(uuid4()),
        "run_id": run,
        "trial": trial,
        "status": status,
        "at": datetime.now(UTC).isoformat(),
        "detail": detail,
    }
    return save_document(root / "events", document)


def _window(bars: tuple[Candle, ...], start: datetime, end: datetime) -> tuple[Candle, ...]:
    preceding = [bar for bar in bars if bar.end <= start]
    # 201 warm-up bars preserve the finalisation/first-executable-open lag.
    offset = max(0, len(preceding) - 201)
    # Keep the originating populated close when a warm-up boundary falls inside
    # a reviewed empty span; never validate a carried price against a future bar.
    while offset and preceding[offset].quality == ("confirmed_no_trade",):
        offset -= 1
    return tuple(
        preceding[offset:] + [bar for bar in bars if start <= bar.start and bar.end <= end]
    )


def _qualified_window(bars: tuple[Candle, ...]) -> None:
    if any(not bar.complete and bar.quality != ("confirmed_no_trade",) for bar in bars):
        raise ResearchDataError("quarantined_or_unqualified_observation")


def _curves(
    bars: tuple[Candle, ...],
    rules: InstrumentRules,
    variant: Variant,
    start: datetime,
    end: datetime,
    costs: CostModel,
    other: tuple[Candle, ...] | None = None,
) -> tuple[Backtest, dict[str, Backtest]]:
    selected = _window(bars, start, end)
    _qualified_window(selected)
    # Missing boundary candles cannot silently shorten a requested test.
    evaluation = [bar for bar in selected if bar.start >= start]
    if not evaluation or evaluation[0].start != start or evaluation[-1].end != end:
        raise ResearchDataError("incomplete evaluation boundary")
    if len(selected) < len(evaluation) + 200 or any(
        a.end != b.start for a, b in pairwise(selected)
    ):
        raise ResearchDataError("warmup_or_evaluation_gap")
    candidate = simulate(selected, rules, variant=variant, start=start, end=end, costs=costs)
    benchmarks = {
        label: simulate(
            selected,
            rules,
            variant=None,
            start=start,
            end=end,
            benchmark_allocation=allocation,
            costs=costs,
        )
        for label, allocation in (
            ("cash", ZERO),
            ("passive10", Decimal(".1")),
            ("passive30", Decimal(".3")),
            ("buy_hold", Decimal(1)),
        )
    }
    benchmarks["monthly_purchases"] = simulate(
        selected,
        rules,
        variant=None,
        start=start,
        end=end,
        costs=costs,
        monthly=Decimal("250"),
        benchmark_allocation=Decimal(1),
        periodic=True,
    )
    if other is not None:
        alternate = _window(other, start, end)
        _qualified_window(alternate)
        alternate_evaluation = [bar for bar in alternate if bar.start >= start]
        if [bar.start for bar in alternate_evaluation] != [bar.start for bar in evaluation] or any(
            a.end != b.start for a, b in pairwise(alternate)
        ):
            raise ResearchDataError("blend_benchmark_gap")
        halves = [
            simulate(
                series,
                reconstructed_rules(series[-1].symbol),
                variant=None,
                start=start,
                end=end,
                initial=Decimal("125"),
                benchmark_allocation=Decimal(1),
                costs=costs,
            )
            for series in (selected, alternate)
        ]
        observations = tuple(
            Observation(
                left.at,
                left.nav + right.nav,
                (left.nav + right.nav) / 250,
                (left.exposure * left.nav + right.exposure * right.nav) / (left.nav + right.nav),
                ZERO,
            )
            for left, right in zip(halves[0].observations, halves[1].observations, strict=True)
        )
        benchmarks["blend50_50"] = Backtest(
            observations,
            (),
            sum((half.fees for half in halves), ZERO),
            sum((half.turnover for half in halves), ZERO),
            sum(half.skips for half in halves),
            (),
            ZERO,
            0,
            selected[0].interval_minutes,
        )
    return candidate, benchmarks


def _fold_report(
    bars: tuple[Candle, ...],
    rules: InstrumentRules,
    variant: Variant,
    start: datetime,
    end: datetime,
    costs: CostModel,
    other: tuple[Candle, ...] | None = None,
) -> dict[str, Any]:
    candidate, benchmarks = _curves(bars, rules, variant, start, end, costs, other)
    by_end = {bar.end: index for index, bar in enumerate(bars)}
    regimes: dict[str, list[float]] = {"above_sma200": [], "at_or_below_sma200": []}
    previous = Decimal(1)
    for observation in candidate.observations:
        index = by_end[observation.at]
        eligible = bars[max(0, index - 201) : index - 1]
        average = sma(eligible, 200)
        if average is not None:
            regime = "above_sma200" if eligible[-1].close > average else "at_or_below_sma200"
            regimes[regime].append(float(observation.unit_price / previous - 1))
        previous = observation.unit_price
    return {
        "candidate": metrics(candidate),
        "benchmarks": {name: metrics(value) for name, value in benchmarks.items()},
        "daily_candidate": daily_returns(candidate),
        "daily_cash": daily_returns(benchmarks["cash"]),
        "daily_passive10": daily_returns(benchmarks["passive10"]),
        "point_in_time_trend_regimes": {
            name: {"observations": len(values), "sum_simple_returns": sum(values)}
            for name, values in regimes.items()
        },
        "episodes": [
            {
                key: value.isoformat() if isinstance(value, datetime) else str(value)
                for key, value in asdict(episode).items()
            }
            for episode in candidate.episodes
        ],
    }


def _append_daily(target: dict[str, float], incoming: dict[str, float]) -> None:
    """Chain non-overlapping fold returns sharing one partial UTC calendar day."""
    overlap = target.keys() & incoming.keys()
    if overlap and overlap != {next(reversed(target))} & {next(iter(incoming))}:
        raise ValueError("overlapping out-of-sample calendar periods")
    for day, value in incoming.items():
        target[day] = (1 + target[day]) * (1 + value) - 1 if day in target else value


def run(root: Path, manifest: Path, *, samples: int = 10_000) -> Path:
    if not 1000 <= samples <= 100_000:
        raise ValueError("bootstrap samples must be 1000..100000")
    run_id = str(uuid4())
    code = code_fingerprint()
    declaration: dict[str, object] = {
        "dataset_manifest_sha256": file_hash(manifest),
        "code_sha256": code,
        "trials": [list(trial) for trial in TRIALS],
        "costs": CostModel().model_dump(mode="json"),
        "policy": RiskPolicy().model_dump(mode="json"),
        "seed": 1729,
        "bootstrap_samples": samples,
        "family_size": 80,
        "holdout_months": 12,
        "folds": "24m_training_6m_validation_6m_test_step6m_flat_start_each_window",
        "daily_fold_aggregation": "chain_shared_partial_utc_day_v2",
    }
    event(root, run_id, "family", "started", declaration)
    try:
        if version("numpy") != "2.5.4":
            raise ValueError("research requires the reviewed NumPy 2.5.4 lock")
        dataset = load_dataset(manifest)
        outcomes = [
            _trial(root, run_id, dataset, symbol, interval, variant, samples)
            for symbol, interval, variant in TRIALS
        ]
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "research_report",
            "run_id": run_id,
            "declaration": declaration,
            "dataset": dataset.fingerprint,
            "runtime": {
                "aca": __version__,
                "python": sys.version,
                "platform": platform.platform(),
                "numpy": version("numpy"),
            },
            "data_review": {
                "transform": dataset.manifest["transform"],
                "gap_audit_sha256": dataset.manifest.get("gap_audit_sha256"),
                "indicator_only_rows": sum(
                    bar.quality == ("confirmed_no_trade",)
                    for bars in dataset.series.values()
                    for bar in bars
                ),
                "quarantined_rows": sum(
                    "disputed_trade_evidence" in bar.quality
                    for bars in dataset.series.values()
                    for bar in bars
                ),
            },
            "trials": outcomes,
            "holdout_opened": False,
            "promotion_allowed": False,
            "decision": "NO_GO_pending_complete_data_holdout_and_all_later_gates",
            "limitations": [
                "Current rules and fee bounds are reconstructed, not historical account facts.",
                "Bar opens plus slippage approximate post-latency taker prices; no maker fills.",
                "Every window starts flat with fresh research state; no live restart inference.",
                "Incomplete windows remain failed; successful subsets cannot establish alpha.",
                "Research results grant no paper selection, live activation or tax approval.",
                "No-trade observations carry past closes for indicators/marks, with no liquidity.",
                "Full archive hashes and point-in-time fee/rule history remain unverified.",
            ],
        }
        output = save_document(root / "reports", report)
        event(root, run_id, "family", "completed", {"report_sha256": file_hash(output)})
        return output
    except Exception as exc:
        event(root, run_id, "family", "failed", {"error_type": type(exc).__name__})
        raise


def run_modular(
    root: Path,
    manifest: Path,
    *,
    samples: int = 10_000,
    retry_failed: bool = False,
) -> Path:
    """Legacy evaluation semantics, one bounded trial at a time; no corrected S0 run.

    A task is one declared trial, not one fold. An interrupted trial recomputes
    deterministically; completed/rejected trials are verified and never recomputed.
    Statistical/execution changes belong to the later Phase 4a items.
    """
    from aca.research.bundles import Result, Store, create, execute, seal

    if not 1000 <= samples <= 100_000:
        raise ValueError("bootstrap samples must be 1000..100000")
    declaration: dict[str, object] = {
        "dataset_manifest_sha256": file_hash(manifest),
        "code_sha256": code_fingerprint(),
        "trials": [list(trial) for trial in TRIALS],
        "costs": CostModel().model_dump(mode="json"),
        "policy": RiskPolicy().model_dump(mode="json"),
        "seed": 1729,
        "bootstrap_samples": samples,
        "family_size": 80,
        "model": "legacy_flat_window_v2_not_corrected_S0",
        "runtime": {"aca": __version__, "python": sys.version, "numpy": version("numpy")},
        "holdout_opened": False,
    }
    tasks: dict[str, dict[str, object]] = {
        f"{symbol}:{interval}:{variant}": {
            "symbol": symbol,
            "interval": interval,
            "variant": variant,
        }
        for symbol, interval, variant in TRIALS
    }
    path = create(root, declaration, tasks)
    run_id = str(uuid5(NAMESPACE_URL, path.parent.name))
    dataset: Dataset | None = None
    for symbol, interval, variant in TRIALS:

        def work(
            store: Store,
            bound_symbol: Symbol = symbol,
            bound_interval: int = interval,
            bound_variant: Variant = variant,
        ) -> Result:
            nonlocal dataset
            if version("numpy") != "2.5.4":
                raise ValueError("research requires the reviewed NumPy 2.5.4 lock")
            if dataset is None:
                dataset = load_dataset(manifest)
            outcome = _trial(
                root, run_id, dataset, bound_symbol, bound_interval, bound_variant, samples
            )
            folds = cast(list[dict[str, Any]], outcome.pop("folds"))

            # Store the original daily values/episodes in independent immutable
            # pages; fold metadata/metrics and trial inference remain small.
            def curve_records() -> Iterator[dict[str, object]]:
                for index, fold in enumerate(folds):
                    for phase in ("validation", "test"):
                        section = fold.get(phase, {})
                        for key in ("daily_candidate", "daily_cash", "daily_passive10"):
                            for day, value in section.pop(key, {}).items():
                                yield {
                                    "fold": index,
                                    "phase": phase,
                                    "series": key,
                                    "day": day,
                                    "return": value,
                                }

            def episode_records() -> Iterator[dict[str, object]]:
                for index, fold in enumerate(folds):
                    for phase in ("validation", "test"):
                        for episode in fold.get(phase, {}).pop("episodes", []):
                            yield {"fold": index, "phase": phase, "episode": episode}

            curves = store.records("curve", curve_records())
            episodes = store.records("episodes", episode_records())
            return Result(
                status="rejected",
                reason="legacy_model_no_promotion",
                artifacts=(
                    store.records("trial", [outcome]),
                    store.records("fold", folds),
                    curves,
                    episodes,
                ),
            )

        result = execute(path, f"{symbol}:{interval}:{variant}", work, retry_failed=retry_failed)
        if result.status == "failed":
            raise ValueError("previous research task failed; explicit --retry-failed required")
    return seal(path)


def _trial(
    root: Path,
    run_id: str,
    dataset: Dataset,
    symbol: Symbol,
    interval: int,
    variant: Variant,
    samples: int,
) -> dict[str, object]:
    name = f"{symbol}:{interval}:{variant}"
    event(root, run_id, name, "started", {"dataset": dataset.fingerprint})
    outcome: dict[str, Any] = {"trial": name, "status": "rejected", "reasons": [], "folds": []}
    try:
        bars = dataset.series[(symbol, interval)]
        rules = reconstructed_rules(symbol)
        other = dataset.series[("ETH/GBP" if symbol == "BTC/GBP" else "BTC/GBP", interval)]
        holdout = months(bars[-1].end, -12)
        if bars[0].start > months(bars[-1].end, -48) or len(bars) < 401:
            raise ResearchDataError("four_year_history_or_warmup_missing")
        # Calendar origin is fixed by source coverage, never by observed strategy results.
        train_start = bars[0].start + timedelta(minutes=201 * interval)
        # Align daily/4h timestamps; months() preserves their UTC boundary.
        folds = walk_forward(train_start, holdout)
        if not folds:
            raise ResearchDataError("no_eligible_walk_forward_windows")
        all_daily: dict[str, dict[str, float]] = {"candidate": {}, "cash": {}, "passive10": {}}
        episodes = 0
        for fold in folds:
            item: dict[str, Any] = {key: value.isoformat() for key, value in asdict(fold).items()}
            try:
                # Training data must also be contiguous; outages are not deleted from history.
                training = _window(bars, fold.train_start, fold.train_end)
                _qualified_window(training)
                if any(a.end != b.start for a, b in pairwise(training)):
                    raise ResearchDataError("training_gap")
                validation = _fold_report(
                    bars, rules, variant, fold.train_end, fold.validation_end, CostModel(), other
                )
                test = _fold_report(
                    bars, rules, variant, fold.validation_end, fold.test_end, CostModel(), other
                )
                item.update({"status": "complete", "validation": validation, "test": test})
                episodes += int(test["candidate"]["completed_episodes"])
                for key in all_daily:
                    _append_daily(all_daily[key], test["daily_" + key])
                # Stress is predeclared, not an extra searched candidate.
                item["stress"] = {
                    label: metrics(
                        simulate(
                            _window(bars, fold.validation_end, fold.test_end),
                            rules,
                            variant=variant,
                            start=fold.validation_end,
                            end=fold.test_end,
                            costs=cost,
                        )
                    )
                    for label, cost in (
                        (
                            "fees1.5_spread2",
                            CostModel(
                                taker_fee=Decimal(".012"), spread_slippage_one_way=Decimal(".002")
                            ),
                        ),
                        ("latency2s", CostModel(latency_ms=2000)),
                    )
                }
            except ResearchDataError as exc:
                item.update({"status": "incomplete", "reason": str(exc)})
            outcome["folds"].append(item)
        complete = all(item["status"] == "complete" for item in outcome["folds"])
        outcome["holdout_start"] = holdout.isoformat()
        outcome["completed_oos_episodes"] = episodes
        outcome["oos_days"] = len(all_daily["candidate"])
        if not complete:
            outcome["reasons"].append("incomplete_windows_no_inference_from_successful_subset")
        if episodes < 50:
            outcome["reasons"].append("fewer_than_50_completed_oos_episodes")
        if len(all_daily["candidate"]) < 730:
            outcome["reasons"].append("fewer_than_two_years_aggregate_oos")
        if complete:
            outcome["uncertainty"] = {
                f"{benchmark}_{block}d": paired_bootstrap(
                    all_daily["candidate"],
                    all_daily[benchmark],
                    block_days=block,
                    samples=samples,
                )
                for benchmark in ("cash", "passive10")
                for block in (7, 30)
            }
            bounds = [
                value.get("simultaneous_95_lower_arithmetic_increment")
                for value in outcome["uncertainty"].values()
            ]
            if all(isinstance(value, float) for value in bounds):
                lower = money(Decimal(str(min(bounds))))
                average_equity = money(
                    min(
                        Decimal(item["test"]["candidate"]["mean_equity_gbp"])
                        for item in outcome["folds"]
                    )
                )
                outcome["economic_gate"] = fixed_cost_gate(average_equity, lower)
                if lower <= 0:
                    outcome["reasons"].append("nonpositive_adjusted_incremental_bound")
                if not outcome["economic_gate"]["passes_arithmetic_hurdle"]:
                    outcome["reasons"].append("fixed_cost_hurdle_failed")
            else:
                outcome["reasons"].append("insufficient_bootstrap_blocks")
        outcome["reasons"].extend(["unopened_holdout", "historical_rules_and_fees_unverified"])
        source = dataset.manifest["source_bundle"]
        if source["assurance"] != "full_archive_sha256":  # type: ignore[index]
            outcome["reasons"].append("full_archive_sha256_not_verified")
        if source["evidence"].get("coverage_requires_review"):  # type: ignore[index]
            outcome["reasons"].append("archive_coverage_requires_review")
    except (KeyError, ResearchDataError) as exc:
        outcome["reasons"].append(str(exc))
    except Exception as exc:
        event(root, run_id, name, "failed", {"error_type": type(exc).__name__})
        raise
    event(
        root,
        run_id,
        name,
        "completed",
        {"status": outcome["status"], "reasons": outcome["reasons"]},
    )
    return outcome


def freeze(
    root: Path, report_path: Path, trial: str, review: str, *, actor: str | None = None
) -> Path:
    """Historical reports remain readable; they cannot authorise new research use."""
    if read_json(report_path).get("kind") != "corrected-evaluation-v1":
        raise ValueError("legacy report cannot freeze; use corrected evidence-bound research gates")
    if actor is None:
        raise ValueError("freeze requires an explicit review actor")
    from aca.research.gates import freeze_evaluation

    return freeze_evaluation(root, report_path, trial, actor, review)


def holdout(root: Path, manifest: Path, frozen_path: Path) -> Path:
    raise ValueError("legacy holdout disabled; requires corrected gates and durable holdout claim")
