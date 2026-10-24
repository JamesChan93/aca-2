"""Reproducible, offline historical gap audit; host Python 3.9+, standard library only.

Consumes the selected official OHLCVT/hourly/quarter/trade files and a retained
public REST receipt. Never runs strategies, opens a holdout, or connects to a DB.
Raw inputs are immutable; reviewed CSVs, complete gap classifications and hashes
are written into a new directory. See docs/research-data-audit.md for acquisition.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal, localcontext
from pathlib import Path
from typing import Any

UTC = timezone.utc  # noqa: UP017 -- the audit also runs with macOS host Python 3.9.
PAIRS = {"XBTGBP": "BTC/GBP", "ETHGBP": "ETH/GBP"}
INTERVALS = (60, 240, 1440)
HEADER = ["timestamp", "price", "volume", "type", "order_type", "misc", "trade_id"]


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    if path.stat().st_size > 16_000_000:
        raise ValueError("JSON size budget exceeded")
    value = json.loads(path.read_bytes(), object_pairs_hook=pairs, parse_float=str)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    canonical(value)
    return value


def write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def at(seconds: int | Decimal) -> str:
    # Decimal arithmetic above determines membership. ISO rendering is reporting only.
    return (
        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(seconds * 1_000_000))
    ).isoformat()


def positive(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or not 0 < result < Decimal("1e20"):
        raise ValueError("non-finite, non-positive or excessive financial value")
    return result


@dataclass
class Bar:
    start: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    count: int

    def add(self, price: Decimal, volume: Decimal) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume
        self.count += 1

    def prices_and_count(self) -> tuple[Decimal, Decimal, Decimal, Decimal, int]:
        return self.open, self.high, self.low, self.close, self.count

    def row(self) -> list[str]:
        return [
            str(self.start),
            *(format(x, "f") for x in (self.open, self.high, self.low, self.close, self.volume)),
            str(self.count),
        ]


def candles(path: Path, interval: int) -> tuple[dict[int, Bar], dict[int, list[str]]]:
    bars: dict[int, Bar] = {}
    originals: dict[int, list[str]] = {}
    if path.stat().st_size > 32_000_000:
        raise ValueError("candle file size budget exceeded")
    previous = -1
    with path.open(newline="") as stream:
        for row in csv.reader(stream):
            if len(row) != 7 or not row[0].isdigit() or not row[6].isdigit():
                raise ValueError("invalid candle row")
            start, count = int(row[0]), int(row[6])
            if start <= previous or start % (interval * 60) or count <= 0:
                raise ValueError("duplicate, out-of-order or unaligned candle")
            prices = [positive(value) for value in row[1:6]]
            bar = Bar(start, prices[0], prices[1], prices[2], prices[3], prices[4], count)
            if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
                raise ValueError("invalid OHLC order")
            bars[start], originals[start] = bar, row
            previous = start
    if not bars:
        raise ValueError("empty candles")
    return bars, originals


def trade_row(row: list[str]) -> tuple[Decimal, Decimal, Decimal, int]:
    if len(row) != 7 or row[3] not in ("b", "s") or row[4] not in ("l", "m"):
        raise ValueError("invalid trade shape or enums")
    timestamp, price, volume = (positive(value) for value in row[:3])
    if not row[6].isdigit() or int(row[6]) <= 0:
        raise ValueError("invalid trade ID")
    return timestamp, price, volume, int(row[6])


def aggregate_trades(
    path: Path, recovered: tuple[list[str], ...] = ()
) -> tuple[dict[int, dict[int, Bar]], dict[str, Any]]:
    """Streaming exact Decimal aggregation; equal times remain distinct trades.

    A repair must be inside an observed ID/time gap. Existing overlapping REST
    rows are corroboration, never a second posting or a replacement of raw data.
    """
    aggregates: dict[int, dict[int, Bar]] = {interval: {} for interval in INTERVALS}
    repairs = sorted(recovered, key=lambda row: int(row[6]))
    pending = list(repairs)
    previous_time, previous_id, first_id = Decimal(0), 0, None
    count, equal_times = 0, 0
    gaps: list[dict[str, Any]] = []

    def consume(row: list[str]) -> None:
        nonlocal previous_time, previous_id, first_id, count, equal_times
        timestamp, price, volume, identity = trade_row(row)
        if timestamp < previous_time or identity <= previous_id:
            raise ValueError("trade order regression or duplicate identity")
        if first_id is None:
            first_id = identity
        elif identity != previous_id + 1:
            gaps.append(
                {
                    "before_id": previous_id,
                    "after_id": identity,
                    "before_time": str(previous_time),
                    "after_time": str(timestamp),
                    "missing_count": identity - previous_id - 1,
                }
            )
        equal_times += int(timestamp == previous_time)
        for interval, bars in aggregates.items():
            start = (
                int(timestamp.to_integral_value(rounding=ROUND_FLOOR))
                // (interval * 60)
                * (interval * 60)
            )
            if start not in bars:
                bars[start] = Bar(start, price, price, price, price, volume, 1)
            else:
                bars[start].add(price, volume)
        previous_time, previous_id = timestamp, identity
        count += 1

    if path.stat().st_size > 500_000_000:
        raise ValueError("trade file size budget exceeded")
    with path.open(newline="") as stream:
        reader = csv.reader(stream)
        if next(reader, None) != HEADER:
            raise ValueError("trade schema/header mismatch")
        for row in reader:
            timestamp, _, _, identity = trade_row(row)
            while pending and int(pending[0][6]) < identity:
                repair = pending.pop(0)
                repaired_time, _, _, repaired_id = trade_row(repair)
                if (
                    not previous_id < repaired_id < identity
                    or not previous_time <= repaired_time <= timestamp
                ):
                    raise ValueError("repair does not belong to an observed ID/time gap")
                consume(repair)
            if pending and int(pending[0][6]) == identity:
                raise ValueError("repair would duplicate an existing archive trade")
            consume(row)
    if pending:
        raise ValueError("unbracketed repair after archive end")
    if not count:
        raise ValueError("empty trade history")
    return aggregates, {
        "rows": count,
        "first_id": first_id,
        "last_id": previous_id,
        "last_timestamp": str(previous_time),
        "equal_timestamp_rows_preserved": equal_times,
        "id_gaps": gaps,
        "repairs_inserted": len(repairs),
    }


def rest_repairs(path: Path, gaps: list[dict[str, Any]]) -> tuple[list[str], ...]:
    document = read_json(path)
    if document.get("error") != [] or set(document.get("result", {})) != {"XXBTZGBP", "last"}:
        raise ValueError("unexpected public REST trade response")
    if sum(gap["after_id"] - gap["before_id"] - 1 for gap in gaps) > 100:
        raise ValueError("repair budget exceeded")
    wanted = {identity for gap in gaps for identity in range(gap["before_id"] + 1, gap["after_id"])}
    if len(wanted) > 100:
        raise ValueError("repair budget exceeded")
    recovered: dict[int, list[str]] = {}
    seen: set[int] = set()
    for row in document["result"]["XXBTZGBP"]:
        if len(row) != 7 or type(row[6]) is not int:
            raise ValueError("invalid REST trade")
        # parse_float=str in the receipt reader below preserves the transmitted time.
        identity = row[6]
        converted = [str(row[2]), row[0], row[1], row[3], row[4], row[5], str(identity)]
        trade_row(converted)
        if identity in seen:
            raise ValueError("duplicate REST trade ID")
        seen.add(identity)
        if identity in wanted:
            recovered[identity] = converted
    if set(recovered) != wanted:
        raise ValueError("public REST response does not resolve every missing ID")
    return tuple(recovered[identity] for identity in sorted(recovered))


def review_receipts(
    directory: Path, gaps: list[dict[str, Any]]
) -> tuple[tuple[list[str], ...], list[dict[str, Any]]]:
    """A historical response alone cannot override conflicting current evidence."""
    names = (
        "btc-id-gap-rest.json",
        "btc-id-gap-rest-verified.json",
        "btc-id-gap-rest-earlier.json",
        "btc-id-gap-rest-overlap.json",
    )
    evidence = []
    candidates = []
    if sum(gap["after_id"] - gap["before_id"] - 1 for gap in gaps) > 100:
        raise ValueError("repair budget exceeded")
    wanted = {identity for gap in gaps for identity in range(gap["before_id"] + 1, gap["after_id"])}
    for name in names:
        path = directory / name
        if not path.exists():
            continue
        document = read_json(path)
        if document.get("error") != [] or set(document.get("result", {})) != {"XXBTZGBP", "last"}:
            raise ValueError("unexpected public REST evidence")
        rows = document["result"]["XXBTZGBP"]
        ids: set[int] = set()
        for row in rows:
            if len(row) != 7 or type(row[6]) is not int or row[6] in ids:
                raise ValueError("invalid or duplicate REST trade")
            trade_row([str(row[2]), row[0], row[1], row[3], row[4], row[5], str(row[6])])
            ids.add(row[6])
        matches = wanted <= ids
        candidate = rest_repairs(path, gaps) if matches else ()
        candidates.append(candidate)
        evidence.append(
            {
                "file": name,
                "sha256": file_sha(path),
                "rows": len(rows),
                "disputed_ids_present": sorted(wanted & ids),
                "disputed_ids_absent": sorted(wanted - ids),
                "candidate_trades": [
                    {key: row[index] for index, key in enumerate(HEADER)} for row in candidate
                ],
            }
        )
    # Three overlapping observations, including the current verified response,
    # are required for an automatic repair. Any absence/economic mismatch vetoes it.
    agreed = (
        bool(candidates)
        and len(candidates) >= 3
        and all(candidate == candidates[0] and bool(candidate) for candidate in candidates)
    )
    return candidates[0] if agreed else (), evidence


def compare(bars: dict[int, Bar], trades: dict[int, Bar]) -> dict[str, Any]:
    common = bars.keys() & trades.keys()
    differences = [abs(bars[key].volume - trades[key].volume) for key in common]
    bad = [
        key
        for key in sorted(common)
        if bars[key].prices_and_count() != trades[key].prices_and_count()
    ]
    return {
        "candles": len(bars),
        "trade_populated_intervals": len(trades),
        "missing_candle_starts": sorted(trades.keys() - bars.keys()),
        "candles_without_trades": sorted(bars.keys() - trades.keys()),
        "price_or_count_conflicts": bad,
        "volume_difference_count": sum(value != 0 for value in differences),
        "maximum_absolute_volume_difference": str(max(differences, default=Decimal(0))),
    }


def classify_gaps(
    bars: dict[int, Bar],
    trades: dict[int, Bar],
    hourly: dict[int, Bar],
    interval: int,
    coverage_end: int,
    id_gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """No gap gets prices or liquidity. ID uncertainty prevents a no-trade claim."""
    step = interval * 60
    starts = sorted(bars)
    if not starts or coverage_end < starts[-1] + step or coverage_end % step:
        raise ValueError("invalid declared coverage")
    result: list[dict[str, Any]] = []
    previous = starts[0]
    for current in [*starts, coverage_end]:
        if current > previous:
            affected = [key for key in trades if previous <= key < current]
            fine = [key for key in hourly if previous <= key < current]
            ambiguous = any(
                Decimal(gap["before_time"]) < current and Decimal(gap["after_time"]) >= previous
                for gap in id_gaps
            )
            classification = (
                "missing_candle_with_trades"
                if affected or fine
                else "unresolved_trade_id_gap"
                if ambiguous
                else "confirmed_no_trade"
            )
            result.append(
                {
                    "start": at(previous),
                    "end": at(current),
                    "intervals": (current - previous) // step,
                    "classification": classification,
                    "trade_rows": sum(trades[key].count for key in affected),
                    "hourly_rows": len(fine),
                    "executable_liquidity": False,
                }
            )
        previous = current + step
    return result


def audit(directory: Path, original: Path, output: Path) -> Path:
    if output.exists():
        raise ValueError("audit output must be a new directory")
    original_document = read_json(original)
    if sha(canonical(original_document)) != original.parent.name:
        raise ValueError("original dataset manifest hash mismatch")
    snapshot = read_json(directory / "snapshot.json")
    if snapshot.get("schema_version") != 1 or set(snapshot.get("archives", {})) != {
        "ohlc",
        "quarter",
        "trades",
    }:
        raise ValueError("missing completed acquisition snapshot")
    for kind, archive in snapshot["archives"].items():
        catalog = read_json(directory / (kind + "-verified-directory.json"))
        if catalog != archive:
            raise ValueError("archive catalog differs from snapshot")
        for member in archive["members"]:
            name = member["name"]
            if Path(name).name != name:
                raise ValueError("member must be a simple filename")
            path = directory / (kind + "-" + name)
            crc = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    crc = zlib.crc32(chunk, crc)
            if (
                path.stat().st_size != member["bytes"]
                or crc != member["crc32"]
                or file_sha(path) != member["sha256"]
            ):
                raise ValueError("archive member size, CRC or SHA256 mismatch")
    receipt = directory / "btc-id-gap-rest-verified.json"
    if file_sha(receipt) != snapshot["rest"]["sha256"]:
        raise ValueError("REST receipt differs from acquisition snapshot")
    coverage = read_json(directory / "ohlc-MANIFEST.json")["coverage"]
    trade_coverage = read_json(directory / "trades-MANIFEST.json")["coverage"]
    quarter_coverage = read_json(directory / "quarter-MANIFEST.json")["coverage"]
    if coverage["end"] != trade_coverage["end"] or coverage["end"] != quarter_coverage["end"]:
        raise ValueError("archive end coverage conflict")
    end = datetime.fromisoformat(coverage["end"]).replace(tzinfo=UTC) + timedelta(days=1)
    end_seconds = int(end.timestamp())
    inventory: dict[str, Any] = {}
    # Bind every consumed raw input before parsing, including archive identity and CRC evidence.
    names = [
        "ohlc-MANIFEST.json",
        "quarter-MANIFEST.json",
        "trades-MANIFEST.json",
        "ohlc-verified-directory.json",
        "quarter-verified-directory.json",
        "trades-verified-directory.json",
        "btc-id-gap-rest-verified.json",
        "snapshot.json",
    ]
    names += [
        f"{kind}-{pair}_{interval}.csv"
        for kind in ("ohlc", "quarter")
        for pair in PAIRS
        for interval in INTERVALS
    ]
    names += [f"trades-{pair}.csv" for pair in PAIRS]
    names += [
        name
        for name in (
            "btc-id-gap-rest.json",
            "btc-id-gap-rest-earlier.json",
            "btc-id-gap-rest-overlap.json",
        )
        if (directory / name).exists()
    ]
    for name in names:
        path = directory / name
        inventory[name] = {"bytes": path.stat().st_size, "sha256": file_sha(path)}
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "kraken_historical_gap_audit",
        "script_sha256": file_sha(Path(__file__)),
        "original_dataset_sha256": original.parent.name,
        "inputs": inventory,
        "coverage_end_exclusive": end.isoformat(),
        "series": [],
        "trade_audits": {},
        "no_strategy_evaluated": True,
        "holdout_performance_opened": False,
        "promotion_permission": False,
        "g3_passed": False,
        "raw_inputs_modified": False,
        "full_exchange_archives_sha256_verified": False,
        "retrieved_at": snapshot["retrieved_at"],
        "evidence_limit": (
            "Official member CRC32/local SHA256/strong ETag ranges; same publisher across "
            "sources, not independent exchange truth."
        ),
    }
    revised_files: dict[str, bytes] = {}
    source_records: list[dict[str, Any]] = []
    repairs_document: list[dict[str, Any]] = []
    with localcontext() as context:
        context.prec = 50
        for pair, symbol in PAIRS.items():
            raw_trades, raw_summary = aggregate_trades(directory / f"trades-{pair}.csv")
            repairs, rest_evidence = (
                review_receipts(directory, raw_summary["id_gaps"]) if pair == "XBTGBP" else ((), [])
            )
            if repairs:
                repaired_trades, repaired_summary = aggregate_trades(
                    directory / f"trades-{pair}.csv", repairs
                )
            else:
                repaired_trades, repaired_summary = raw_trades, raw_summary
            if repaired_summary["first_id"] != 1:
                raise ValueError("trade history does not begin with its first identity")
            report["trade_audits"][symbol] = {
                "original": raw_summary,
                "reviewed": repaired_summary,
                "rest_evidence": rest_evidence,
                "repair_applied": bool(repairs),
                "unresolved_id_ranges": repaired_summary["id_gaps"],
            }
            hourly, _ = candles(directory / f"ohlc-{pair}_60.csv", 60)
            for interval in INTERVALS:
                original_bars, original_rows = candles(
                    directory / f"ohlc-{pair}_{interval}.csv", interval
                )
                baseline = compare(original_bars, raw_trades[interval])
                if any(
                    baseline[key]
                    for key in (
                        "missing_candle_starts",
                        "candles_without_trades",
                        "price_or_count_conflicts",
                    )
                ):
                    raise ValueError(
                        "original candle/trade sources disagree; manual review required"
                    )
                quarter, quarter_rows = candles(
                    directory / f"quarter-{pair}_{interval}.csv", interval
                )
                q_start = int(
                    datetime.fromisoformat(quarter_coverage["start"])
                    .replace(tzinfo=UTC)
                    .timestamp()
                )
                expected_quarter = {
                    key: row for key, row in original_rows.items() if q_start <= key < end_seconds
                }
                if quarter_rows != expected_quarter:
                    raise ValueError("quarterly update differs from full archive")
                corrected = dict(original_bars)
                changes = sorted(
                    {int(Decimal(row[0])) // (interval * 60) * (interval * 60) for row in repairs}
                )
                for start in changes:
                    corrected[start] = repaired_trades[interval][start]
                    repairs_document.append(
                        {
                            "symbol": symbol,
                            "interval_minutes": interval,
                            "start": at(start),
                            "before": original_bars[start].row(),
                            "after": corrected[start].row(),
                            "recovered_trade_ids": [int(row[6]) for row in repairs],
                            "receipt_sha256": inventory["btc-id-gap-rest-verified.json"]["sha256"],
                        }
                    )
                comparison = compare(corrected, repaired_trades[interval])
                if any(
                    comparison[key]
                    for key in (
                        "missing_candle_starts",
                        "candles_without_trades",
                        "price_or_count_conflicts",
                    )
                ):
                    raise ValueError("repaired sources disagree")
                gaps = classify_gaps(
                    corrected,
                    repaired_trades[interval],
                    hourly,
                    interval,
                    end_seconds,
                    repaired_summary["id_gaps"],
                )
                disputed_starts = sorted(
                    {
                        start
                        for gap in repaired_summary["id_gaps"]
                        for start in original_bars
                        if Decimal(gap["before_time"]) < start + interval * 60
                        and Decimal(gap["after_time"]) >= start
                    }
                )
                item = {
                    "symbol": symbol,
                    "interval_minutes": interval,
                    "rows": len(corrected),
                    "first": at(min(corrected)),
                    "last_end": at(max(corrected) + interval * 60),
                    "gap_spans": len(gaps),
                    "empty_intervals": sum(gap["intervals"] for gap in gaps),
                    "gaps": gaps,
                    "original_trade_comparison": baseline,
                    "repaired_trade_comparison": comparison,
                    "quarterly_rows_equal": len(quarter),
                    "corrected_rows": len(changes),
                    "disputed_populated_candles": [at(start) for start in disputed_starts],
                }
                if interval in (240, 1440):
                    source = next(
                        record["source"]
                        for record in original_document["series"]
                        if record["source"]["symbol"] == symbol
                        and record["source"]["interval_minutes"] == interval
                    )
                    source_hash = file_sha(directory / f"ohlc-{pair}_{interval}.csv")
                    if source_hash != source["sha256"]:
                        raise ValueError(
                            "fresh official candle file differs from frozen research source"
                        )
                    object_path = original.parents[2] / "objects" / (source_hash + ".csv")
                    if file_sha(object_path) != source_hash:
                        raise ValueError("original content-addressed CSV changed")
                    # Untouched lines retain original decimal text; only reviewed rows are replaced.
                    text = io.StringIO(newline="")
                    writer = csv.writer(text, lineterminator="\n")
                    for start in sorted(original_rows):
                        writer.writerow(
                            corrected[start].row() if start in changes else original_rows[start]
                        )
                    data = text.getvalue().encode()
                    name = f"{pair}_{interval}.csv"
                    revised_files[name] = data
                    item["revised_csv_sha256"] = sha(data)
                    source_records.append({**source, "path": name, "sha256": sha(data)})
                report["series"].append(item)
    report["candle_corrections"] = repairs_document
    report["unresolved_trade_range_count"] = sum(
        len(item["unresolved_id_ranges"]) for item in report["trade_audits"].values()
    )
    report["decision"] = (
        "GAP_REVIEW_COMPLETE_WITH_DISPUTED_TRADES"
        if report["unresolved_trade_range_count"]
        else "GAP_REVIEW_COMPLETE"
    )
    report["classification_counts"] = dict(
        Counter(
            gap["classification"]
            for item in report["series"]
            if item["interval_minutes"] in (240, 1440)
            for gap in item["gaps"]
        )
    )
    for name, expected in inventory.items():
        if file_sha(directory / name) != expected["sha256"]:
            raise ValueError("raw audit input changed during review")
    report_bytes = canonical(report)
    report_hash = sha(report_bytes)
    # This source bundle is accepted by the existing strict importer. The separate
    # complete gap-review artifact grants no implicit filling/qualification permission.
    source_bundle = {
        "schema_version": 1,
        "source_url": original_document["source_bundle"]["source_url"],
        "retrieved_at": snapshot["retrieved_at"],
        "usage_note": (
            "Reviewed official archive and public REST evidence; see gap audit. "
            "No redistribution licence inferred."
        ),
        "assurance": "range_members_crc",
        "evidence": {
            "audit_sha256": report_hash,
            "original_dataset_sha256": original.parent.name,
            "original_source_retrieved_at": original_document["source_bundle"]["retrieved_at"],
            "gap_review": (
                "confirmed_no_trade; zero executable liquidity; no synthetic prices written"
            ),
            "coverage_end_exclusive": end.isoformat(),
            "holdout_performance_opened": False,
            "unresolved_trade_range_count": report["unresolved_trade_range_count"],
        },
        "files": source_records,
    }
    output.mkdir(parents=True)
    for name, data in revised_files.items():
        write_once(output / name, data)
    write_once(output / "audit.json", report_bytes)
    write_once(output / "source.json", canonical(source_bundle))
    return output / "audit.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--original-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = audit(args.directory, args.original_manifest, args.output)
    print(json.dumps({"audit": str(path), "sha256": file_sha(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
