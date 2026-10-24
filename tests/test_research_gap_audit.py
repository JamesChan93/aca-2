"""Evidence integrity, exact trade recovery and conservative gap classifications."""

import ast
import csv
import io
import json
import zlib
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest

from scripts.research_archive_members import REST_URL, Transport
from scripts.research_gap_audit import (
    HEADER,
    INTERVALS,
    PAIRS,
    Bar,
    aggregate_trades,
    audit,
    canonical,
    classify_gaps,
    file_sha,
    read_json,
    rest_repairs,
    sha,
)

D = Decimal
START = 1546300800


def write_trades(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(rows)


def test_exact_trades_equal_times_and_id_gap_repair(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    first = [str(START), "100", "0.000000000000000001", "b", "l", "", "1"]
    last = [str(START + 86400), "101", "1", "s", "m", "", "4"]
    write_trades(path, [first, last])
    repairs = (
        [str(START), "99", "0.000000000000000002", "b", "l", "", "2"],
        [str(START), "102", "0.000000000000000003", "b", "l", "", "3"],
    )
    with localcontext() as context:
        context.prec = 50
        original, before = aggregate_trades(path)
        fixed, after = aggregate_trades(path, repairs)
    assert before["id_gaps"][0]["missing_count"] == 2
    assert original[1440][START].count == 1
    assert after["id_gaps"] == []
    assert after["rows"] == 4
    assert after["equal_timestamp_rows_preserved"] == 2
    assert fixed[1440][START].volume == D("0.000000000000000006")
    assert fixed[1440][START].prices_and_count() == (D(100), D(102), D(99), D(102), 3)


@pytest.mark.parametrize("failure", ["duplicate", "regression", "wrong_repair_time", "outside_end"])
def test_bad_or_duplicate_repair_fails(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "trades.csv"
    first = [str(START), "100", "1", "b", "l", "", "1"]
    last = [str(START + 3600), "101", "1", "b", "l", "", "3"]
    rows = [first, last]
    if failure == "regression":
        rows.reverse()
    write_trades(path, rows)
    repair = [str(START + 1), "99", "1", "b", "l", "", "2"]
    if failure == "duplicate":
        repair[6] = "1"
    elif failure == "wrong_repair_time":
        repair[0] = str(START + 3601)
    elif failure == "outside_end":
        repair[6] = "4"
    with pytest.raises(ValueError):
        aggregate_trades(path, (repair,))


def test_gaps_distinguish_missing_candles_from_no_trades_and_uncertainty() -> None:
    bar = Bar(START, D(100), D(100), D(100), D(100), D(1), 1)
    later = Bar(START + 2 * 86400, D(100), D(100), D(100), D(100), D(1), 1)
    bars = {bar.start: bar, later.start: later}
    result = classify_gaps(bars, bars, bars, 1440, START + 3 * 86400, [])
    assert result[0]["classification"] == "confirmed_no_trade"
    assert result[0]["intervals"] == 1
    assert result[0]["executable_liquidity"] is False
    missing = dict(bars)
    missing[START + 86400] = bar
    assert (
        classify_gaps(bars, missing, bars, 1440, START + 3 * 86400, [])[0]["classification"]
        == "missing_candle_with_trades"
    )
    unknown = [{"before_time": str(START), "after_time": str(START + 2 * 86400)}]
    assert (
        classify_gaps(bars, bars, bars, 1440, START + 3 * 86400, unknown)[0]["classification"]
        == "unresolved_trade_id_gap"
    )


def test_rest_time_never_passes_through_binary_float(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(
        b'{"error":[],"result":{"XXBTZGBP":[["100","0.1",1546300800.123456789,"b","l","",2]],"last":"x"}}'
    )
    gaps = [{"before_id": 1, "after_id": 3}]
    repaired = rest_repairs(path, gaps)
    assert repaired[0][0] == "1546300800.123456789"
    assert isinstance(read_json(path)["result"]["XXBTZGBP"][0][2], str)
    path.write_bytes(b'{"error":[],"result":{"XXBTZGBP":[],"last":"x"}}')
    with pytest.raises(ValueError, match="resolve every"):
        rest_repairs(path, gaps)
    with pytest.raises(ValueError, match="budget"):
        rest_repairs(path, [{"before_id": 1, "after_id": 10**30}])


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "https://example.com/", REST_URL + "&extra=1"]
)
def test_fetch_url_allowlist_precedes_network(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        Transport().request(url)


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    directory = tmp_path / "source"
    directory.mkdir()
    records = []
    objects = tmp_path / "original" / "objects"
    objects.mkdir(parents=True)
    for pair, symbol in PAIRS.items():
        for interval in INTERVALS:
            data = (
                f"{START},100,100,100,100,1,1\n{START + 2 * 86400},101,101,101,101,1,1\n".encode()
            )
            for kind in ("ohlc", "quarter"):
                (directory / f"{kind}-{pair}_{interval}.csv").write_bytes(data)
            if interval != 60:
                records.append(
                    {
                        "source": {
                            "path": f"{pair}_{interval}.csv",
                            "symbol": symbol,
                            "interval_minutes": interval,
                            "sha256": sha(data),
                            "coverage_start": "2019-01-01T00:00:00+00:00",
                            "coverage_end": "2019-01-04T00:00:00+00:00",
                        }
                    }
                )
                (objects / (sha(data) + ".csv")).write_bytes(data)
        ids = (1, 3) if pair == "XBTGBP" else (1, 2)
        write_trades(
            directory / f"trades-{pair}.csv",
            [
                [str(START), "100", "1", "b", "l", "", str(ids[0])],
                [str(START + 2 * 86400), "101", "1", "b", "l", "", str(ids[1])],
            ],
        )
    coverage = {"coverage": {"start": "2019-01-01", "end": "2019-01-03"}}
    for kind in ("ohlc", "quarter", "trades"):
        (directory / (kind + "-MANIFEST.json")).write_bytes(canonical(coverage))
    receipt = {
        "error": [],
        "result": {"XXBTZGBP": [["99", "0.5", str(START + 1), "b", "l", "", 2]], "last": "x"},
    }
    receipt_path = directory / "btc-id-gap-rest-verified.json"
    receipt_path.write_bytes(canonical(receipt))
    for name in (
        "btc-id-gap-rest.json",
        "btc-id-gap-rest-earlier.json",
        "btc-id-gap-rest-overlap.json",
    ):
        (directory / name).write_bytes(canonical(receipt))
    archives: dict[str, Any] = {}
    for kind in ("ohlc", "quarter", "trades"):
        members = []
        for path in [
            *sorted(directory.glob(kind + "-*.csv")),
            directory / (kind + "-MANIFEST.json"),
        ]:
            data = path.read_bytes()
            members.append(
                {
                    "name": path.name.removeprefix(kind + "-"),
                    "bytes": len(data),
                    "crc32": zlib.crc32(data),
                    "sha256": sha(data),
                }
            )
        archive = {"parts": [], "members": members}
        archives[kind] = archive
        (directory / (kind + "-verified-directory.json")).write_bytes(canonical(archive))
    snapshot = {
        "schema_version": 1,
        "retrieved_at": "2026-10-23T00:00:00+00:00",
        "archives": archives,
        "rest": {"sha256": file_sha(receipt_path)},
    }
    (directory / "snapshot.json").write_bytes(canonical(snapshot))
    original = {
        "source_bundle": {
            "source_url": "https://example.invalid/fixture",
            "retrieved_at": "2026-10-17T00:00:00+00:00",
        },
        "series": records,
    }
    data = canonical(original)
    manifest = tmp_path / "original" / "datasets" / sha(data) / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(data)
    return directory, manifest


def test_full_audit_preserves_original_and_does_not_invent_bars(tmp_path: Path) -> None:
    directory, manifest = fixture(tmp_path)
    before = {path: file_sha(path) for path in directory.iterdir()}
    output = tmp_path / "reviewed"
    report = read_json(audit(directory, manifest, output))
    # Four-hour files also have an empty suffix after their final populated bar.
    assert report["classification_counts"] == {"confirmed_no_trade": 6}
    assert report["trade_audits"]["BTC/GBP"]["reviewed"]["id_gaps"] == []
    assert report["promotion_permission"] is False
    assert report["holdout_performance_opened"] is False
    assert len(report["candle_corrections"]) == 3  # hourly evidence + four-hour and daily CSVs
    assert all(file_sha(path) == fingerprint for path, fingerprint in before.items())
    for interval in (240, 1440):
        text = (output / f"XBTGBP_{interval}.csv").read_text()
        rows = list(csv.reader(io.StringIO(text)))
        assert len(rows) == 2
        assert rows[0] == [str(START), "100", "100", "99", "99", "1.5", "2"]
        assert (output / f"ETHGBP_{interval}.csv").read_bytes() == (
            directory / f"ohlc-ETHGBP_{interval}.csv"
        ).read_bytes()
    with pytest.raises(ValueError, match="new directory"):
        audit(directory, manifest, output)


def test_conflicting_current_receipt_preserves_original_and_quarantines(tmp_path: Path) -> None:
    directory, manifest = fixture(tmp_path)
    path = directory / "btc-id-gap-rest-verified.json"
    document = read_json(path)
    document["result"]["XXBTZGBP"] = []
    path.write_bytes(canonical(document))
    snapshot_path = directory / "snapshot.json"
    snapshot = read_json(snapshot_path)
    snapshot["rest"]["sha256"] = file_sha(path)
    snapshot_path.write_bytes(canonical(snapshot))
    output = tmp_path / "reviewed"
    report = read_json(audit(directory, manifest, output))
    assert report["decision"] == "GAP_REVIEW_COMPLETE_WITH_DISPUTED_TRADES"
    assert report["unresolved_trade_range_count"] == 1
    assert report["candle_corrections"] == []
    assert report["trade_audits"]["BTC/GBP"]["repair_applied"] is False
    for interval in (240, 1440):
        assert (output / f"XBTGBP_{interval}.csv").read_bytes() == (
            directory / f"ohlc-XBTGBP_{interval}.csv"
        ).read_bytes()
    # This fixture's uncertain ID spans an empty day. It cannot be called no-trade.
    assert "unresolved_trade_id_gap" in report["classification_counts"]
    source = json.loads((output / "source.json").read_text())
    assert source["evidence"]["unresolved_trade_range_count"] == 1


def test_audit_refuses_changed_source_before_publishing(tmp_path: Path) -> None:
    directory, manifest = fixture(tmp_path)
    with (directory / "ohlc-XBTGBP_240.csv").open("ab") as stream:
        stream.write(b"tampered")
    output = tmp_path / "reviewed"
    with pytest.raises(ValueError, match="CRC or SHA256"):
        audit(directory, manifest, output)
    assert not output.exists()


def test_scripts_remain_host_python_39_compatible() -> None:
    for name in ("research_archive_members.py", "research_gap_audit.py"):
        ast.parse(
            (Path(__file__).parents[1] / "scripts" / name).read_text(), feature_version=(3, 9)
        )
