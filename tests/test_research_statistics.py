"""Seeded uncertainty, undefined metrics and holdout workflow regressions."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_research_core import bars, rules

from aca.research.artifacts import canonical, digest, read_json
from aca.research.datasets import import_dataset
from aca.research.runner import _curves, freeze, holdout, run
from aca.research.statistics import metrics, paired_bootstrap, xirr
from aca.simulation.reference import CostModel, simulate


def test_seeded_paired_blocks_and_family_adjustment() -> None:
    dates = [
        (datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=index)).date().isoformat()
        for index in range(365)
    ]
    strategy = dict(
        zip(dates, [0.001 if index % 3 else -0.002 for index in range(365)], strict=True)
    )
    cash = dict.fromkeys(dates, 0.0)
    first = paired_bootstrap(strategy, cash, block_days=7, samples=1000)
    assert first == paired_bootstrap(strategy, cash, block_days=7, samples=1000)
    one = paired_bootstrap(strategy, cash, block_days=7, samples=1000, family_size=1)
    lower = first["simultaneous_95_lower_arithmetic_increment"]
    unadjusted = one["simultaneous_95_lower_arithmetic_increment"]
    assert isinstance(lower, float) and isinstance(unadjusted, float)
    assert lower <= unadjusted
    assert (
        paired_bootstrap(strategy, strategy, block_days=30, samples=1000)[
            "simultaneous_95_lower_arithmetic_increment"
        ]
        == 0
    )
    assert not paired_bootstrap({"a": 0.0}, {"a": 0.0}, block_days=7, samples=1000)["available"]
    with pytest.raises(ValueError, match="paired"):
        paired_bootstrap(strategy, {"wrong": 0.0}, block_days=7)


def test_cash_metrics_undefined_and_xirr() -> None:
    data = bars([100] * 10)
    result = simulate(
        data,
        rules(),
        variant=None,
        benchmark_allocation=Decimal(0),
        start=data[0].start,
        end=data[-1].end,
    )

    report = metrics(result)
    assert report["twr"] == 0
    assert report["sharpe_zero_cash_yield"] is None
    assert report["sortino"] is None
    assert report["profit_factor"] is None
    rate = xirr([(datetime(2020, 1, 1, tzinfo=UTC), -100), (datetime(2021, 1, 1, tzinfo=UTC), 110)])
    assert rate is not None and abs(rate - 0.1) < 0.001
    assert (
        xirr(
            [
                (datetime(2020, 1, 1, tzinfo=UTC), -100),
                (datetime(2020, 6, 1, tzinfo=UTC), 1000),
                (datetime(2021, 1, 1, tzinfo=UTC), -110),
            ]
        )
        is None
    )


def test_four_hour_blend_annualisation_preserves_interval() -> None:
    data = bars([100] * 220, interval=240)
    other = tuple(bar.model_copy(update={"symbol": "ETH/GBP"}) for bar in data)
    _, benchmarks = _curves(
        data, rules(), "sma200", data[201].start, data[-1].end, CostModel(), other
    )
    blend = benchmarks["blend50_50"]
    assert blend.interval_minutes == 240
    result = metrics(blend)
    duration_days = (data[-1].end - data[201].start).total_seconds() / 86400
    twr = result["twr"]
    annual = result["annualised_return"]
    assert isinstance(twr, float) and isinstance(annual, float)
    assert abs(annual - math.expm1(math.log1p(twr) * 365.25 / duration_days)) < 1e-12


def continuous_dataset(tmp_path: Path) -> Path:
    tmp_path.mkdir(exist_ok=True)
    start, end = datetime(2019, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    data = "".join(
        f"{int((start + timedelta(days=index)).timestamp())},100,101,99,100,10000,100\n"
        for index in range((end - start).days)
    ).encode()
    files: list[dict[str, object]] = []
    for pair, symbol in (("btc", "BTC/GBP"), ("eth", "ETH/GBP")):
        (tmp_path / (pair + ".csv")).write_bytes(data)
        files.append(
            {
                "path": pair + ".csv",
                "sha256": digest(data),
                "symbol": symbol,
                "interval_minutes": 1440,
                "coverage_start": start.isoformat(),
                "coverage_end": end.isoformat(),
            }
        )
    source = tmp_path / "source.json"
    source.write_bytes(
        canonical(
            {
                "schema_version": 1,
                "source_url": "https://example.invalid/synthetic",
                "retrieved_at": "2026-10-17T00:00:00+00:00",
                "usage_note": "synthetic test only",
                "assurance": "local_unverified",
                "evidence": {},
                "files": files,
            }
        )
    )
    return import_dataset(source, tmp_path / "research")


def test_full_family_replay_and_frozen_holdout(tmp_path: Path) -> None:
    manifest = continuous_dataset(tmp_path)
    root = tmp_path / "research"
    first_path = run(root, manifest, samples=1000)
    first = read_json(first_path)
    second = read_json(run(root, manifest, samples=1000))
    assert first["trials"] == second["trials"]
    assert len(first["trials"]) == 20  # type: ignore[arg-type]
    assert first["holdout_opened"] is False
    assert not (root / "holdout-openings").exists()
    trial = "BTC/GBP:1440:sma200"
    frozen = freeze(root, first_path, trial, "Synthetic workflow test; no economic conclusion")
    output = read_json(holdout(root, manifest, frozen))
    assert output["promotion_allowed"] is False
    with pytest.raises(FileExistsError):
        holdout(root, manifest, frozen)
    with pytest.raises(ValueError, match="predeclared"):
        freeze(root, first_path, "unregistered", "test")
    changed = read_json(frozen)
    changed["declaration"]["costs"]["taker_fee"] = "0"  # type: ignore[index]
    tampered = tmp_path / "changed.json"
    tampered.write_bytes(canonical(changed))
    with pytest.raises(ValueError, match="unchanged"):
        holdout(root, manifest, tampered)
