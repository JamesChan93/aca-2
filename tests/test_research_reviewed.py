"""Reviewed gaps cannot invent liquidity, future prices or usable disputed data."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_research_core import always_long, bars, rules
from test_research_data import bundle

from aca.research.artifacts import canonical, digest, read_json, save_document
from aca.research.datasets import Dataset, import_dataset, load_dataset
from aca.research.runner import ResearchDataError, _curves, code_fingerprint, holdout
from aca.simulation.reference import CostModel, simulate
from aca.strategies.slow import signal


def reviewed_bundle(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    source = bundle(tmp_path)
    original = read_json(source)
    source_file = original["files"][0]  # type: ignore[index]
    gap = {
        "start": "2019-01-02T00:00:00+00:00",
        "end": "2019-01-03T00:00:00+00:00",
        "classification": "confirmed_no_trade",
        "intervals": 1,
        "trade_rows": 0,
        "hourly_rows": 0,
        "executable_liquidity": False,
    }
    audit: dict[str, Any] = {
        "schema_version": 1,
        "kind": "kraken_historical_gap_audit",
        "decision": "GAP_REVIEW_COMPLETE_WITH_DISPUTED_TRADES",
        "raw_inputs_modified": False,
        "candle_corrections": [],
        "holdout_performance_opened": False,
        "original_dataset_sha256": "a" * 64,
        "retrieved_at": original["retrieved_at"],
        "coverage_end_exclusive": source_file["coverage_end"],
        "inputs": {"ohlc.csv": {"sha256": source_file["sha256"]}},
        "series": [
            {
                "symbol": "BTC/GBP",
                "interval_minutes": 1440,
                "revised_csv_sha256": source_file["sha256"],
                "corrected_rows": 0,
                "rows": 2,
                "first": source_file["coverage_start"],
                "last_end": source_file["coverage_end"],
                "gaps": [gap],
                "disputed_populated_candles": [],
            }
        ],
    }
    path = tmp_path / "audit.json"
    bind_review(source, path, audit)
    return source, path, audit


def bind_review(source: Path, audit_path: Path, audit: dict[str, Any]) -> None:
    audit_path.write_bytes(canonical(audit))
    document = read_json(source)
    document["evidence"] = {
        "audit_sha256": digest(audit_path.read_bytes()),
        "original_dataset_sha256": "a" * 64,
    }
    source.write_bytes(canonical(document))


def test_review_is_explicit_immutable_and_past_only(tmp_path: Path) -> None:
    source, audit, _ = reviewed_bundle(tmp_path)
    root = tmp_path / "research"
    raw = load_dataset(import_dataset(source, root))
    target = import_dataset(source, root, gap_audit=audit)
    data = load_dataset(target)
    assert raw.fingerprint != data.fingerprint
    assert len(raw.series[("BTC/GBP", 1440)]) == 2
    expanded = data.series[("BTC/GBP", 1440)]
    assert len(expanded) == 3
    assert expanded[1].open == expanded[1].high == expanded[1].low == expanded[1].close == 101
    assert expanded[2].close == 102  # Never used to fill the earlier interval.
    assert expanded[1].volume == expanded[1].trade_count == 0
    assert not expanded[1].complete and expanded[1].quality == ("confirmed_no_trade",)
    raw_bytes = (tmp_path / "bars.csv").read_bytes()
    assert (root / "objects" / (digest(raw_bytes) + ".csv")).read_bytes() == raw_bytes
    audit_object = root / "objects" / (str(data.manifest["gap_audit_sha256"]) + ".json")
    audit_object.write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash"):
        load_dataset(target)


@pytest.mark.parametrize(
    "fault", ["hash", "gap", "liquidity", "trades", "source", "duplicate", "quarantine"]
)
def test_invalid_or_unbound_reviews_fail_closed(tmp_path: Path, fault: str) -> None:
    source, audit_path, audit = reviewed_bundle(tmp_path)
    item = audit["series"][0]
    if fault == "hash":
        audit_path.write_bytes(b"{}")
    else:
        if fault == "gap":
            item["gaps"][0]["end"] = "2019-01-04T00:00:00+00:00"
        elif fault == "liquidity":
            item["gaps"][0]["executable_liquidity"] = True
        elif fault == "trades":
            item["gaps"][0]["trade_rows"] = 1
        elif fault == "source":
            item["revised_csv_sha256"] = "b" * 64
        elif fault == "duplicate":
            audit["series"].append(item)
        else:
            item["disputed_populated_candles"] = ["2019-01-02T00:00:00+00:00"]
        bind_review(source, audit_path, audit)
    with pytest.raises(ValueError):
        import_dataset(source, tmp_path / "output", gap_audit=audit_path)
    assert not (tmp_path / "output/datasets").exists()


def test_unknown_gaps_and_disputed_observations_remain_unusable(tmp_path: Path) -> None:
    source, path, audit = reviewed_bundle(tmp_path)
    audit["series"][0]["gaps"][0]["classification"] = "unresolved_trade_id_range"
    audit["series"][0]["disputed_populated_candles"] = ["2019-01-03T00:00:00+00:00"]
    bind_review(source, path, audit)
    data = load_dataset(import_dataset(source, tmp_path / "output", gap_audit=path))
    selected = data.series[("BTC/GBP", 1440)]
    assert len(selected) == 2 and selected[-1].quality == ("disputed_trade_evidence",)
    with pytest.raises(ValueError, match="unqualified"):
        simulate(
            selected,
            rules(),
            variant=None,
            benchmark_allocation=Decimal(1),
            start=selected[0].start,
            end=selected[-1].end,
        )


def test_leading_no_trade_cannot_use_a_future_close(tmp_path: Path) -> None:
    source, path, audit = reviewed_bundle(tmp_path)
    document = read_json(source)
    document["files"][0]["coverage_start"] = "2018-12-31T00:00:00+00:00"  # type: ignore[index]
    source.write_bytes(canonical(document))
    gap = dict(audit["series"][0]["gaps"][0])
    gap.update(start="2018-12-31T00:00:00+00:00", end="2019-01-01T00:00:00+00:00")
    audit["series"][0]["gaps"].insert(0, gap)
    bind_review(source, path, audit)
    data = load_dataset(import_dataset(source, tmp_path / "output", gap_audit=path))
    selected = data.series[("BTC/GBP", 1440)]
    assert selected[0].start.isoformat() == "2019-01-01T00:00:00+00:00"


def test_trailing_no_trade_keeps_coverage_and_propagates_disputed_price(tmp_path: Path) -> None:
    source, path, audit = reviewed_bundle(tmp_path)
    document = read_json(source)
    end = "2019-01-06T00:00:00+00:00"
    document["files"][0]["coverage_end"] = end  # type: ignore[index]
    source.write_bytes(canonical(document))
    audit["coverage_end_exclusive"] = end
    item = audit["series"][0]
    gap = dict(item["gaps"][0])
    gap.update(start="2019-01-04T00:00:00+00:00", end=end, intervals=2)
    item["gaps"].append(gap)
    item["disputed_populated_candles"] = ["2019-01-03T00:00:00+00:00"]
    bind_review(source, path, audit)
    data = load_dataset(import_dataset(source, tmp_path / "output", gap_audit=path))
    selected = data.series[("BTC/GBP", 1440)]
    assert len(selected) == 5 and selected[-1].end.isoformat() == end
    assert all(bar.close == 102 and bar.volume == 0 for bar in selected[-2:])
    assert all(bar.quality == ("disputed_trade_evidence",) for bar in selected[-3:])


def no_trade(data: tuple[Any, ...], index: int) -> tuple[Any, ...]:
    last = data[index - 1]
    empty = data[index].model_copy(
        update={
            "open": last.close,
            "high": last.close,
            "low": last.close,
            "close": last.close,
            "volume": Decimal(0),
            "trade_count": 0,
            "complete": False,
            "quality": ("confirmed_no_trade",),
        }
    )
    return (*data[:index], empty, *data[index + 1 :])


def test_no_trade_supports_indicators_but_never_an_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aca.simulation.reference.signal", always_long)
    data = no_trade(bars([100] * 205), 201)
    assert signal(data[:202], "sma200", data[202].start + timedelta(seconds=10)).desired == "flat"
    result = simulate(data, rules(), variant="sma200", start=data[200].start, end=data[-1].end)
    assert result.observations[1].exposure == 0
    assert result.observations[-1].exposure > 0
    assert result.observations[1].nav == 250


def test_no_trade_cannot_fill_a_protective_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aca.simulation.reference.signal", always_long)
    original = bars([100] * 202 + [95] * 6)
    data = no_trade(original, 203)
    result = simulate(data, rules(), variant="sma200", start=data[200].start, end=data[-1].end)
    assert result.episodes and result.episodes[0].closed_at == data[204].start
    assert all(episode.closed_at != data[203].start for episode in result.episodes)


def test_benchmark_waits_for_tradable_interval() -> None:
    data = no_trade(bars([100] * 205), 200)
    result = simulate(
        data,
        rules(),
        variant=None,
        benchmark_allocation=Decimal(1),
        start=data[200].start,
        end=data[-1].end,
    )
    assert result.observations[0].exposure == 0
    assert result.observations[1].exposure > 0


@pytest.mark.parametrize("fault", ["volume", "trade_count", "open", "unfinalised"])
def test_forged_no_trade_values_rejected(fault: str) -> None:
    data = no_trade(bars([100] * 205), 201)
    changes = (
        {fault: Decimal(1)}
        if fault != "unfinalised"
        else {"observed_at": data[201].end - timedelta(seconds=1)}
    )
    data = (*data[:201], data[201].model_copy(update=changes), *data[202:])
    with pytest.raises(ValueError, match="unqualified"):
        simulate(
            data,
            rules(),
            variant=None,
            benchmark_allocation=Decimal(1),
            start=data[200].start,
            end=data[-1].end,
        )


def test_quarantine_blocks_window_and_holdout_without_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = bars([100] * 600)
    disputed = data[-10].model_copy(
        update={
            "complete": False,
            "quality": ("disputed_trade_evidence",),
        }
    )
    data = (*data[:-10], disputed, *data[-9:])
    other = tuple(bar.model_copy(update={"symbol": "ETH/GBP"}) for bar in data)
    with pytest.raises(ResearchDataError, match="quarantined"):
        _curves(data, rules(), "sma200", data[201].start, data[-1].end, CostModel(), other)
    dataset = Dataset("a" * 64, {}, {("BTC/GBP", 1440): data, ("ETH/GBP", 1440): other})
    monkeypatch.setattr("aca.research.runner.load_dataset", lambda _: dataset)
    frozen = save_document(
        tmp_path,
        {
            "kind": "candidate_freeze",
            "dataset": dataset.fingerprint,
            "trial": "BTC/GBP:1440:sma200",
            "declaration": {
                "code_sha256": code_fingerprint(),
                "costs": CostModel().model_dump(mode="json"),
            },
        },
    )
    manifest = tmp_path / "datasets" / dataset.fingerprint / "manifest.json"
    with pytest.raises(ResearchDataError, match="quarantined"):
        holdout(tmp_path, manifest, frozen)
    assert not (tmp_path / "holdout-openings").exists()
