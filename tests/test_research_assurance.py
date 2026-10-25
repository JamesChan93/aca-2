"""Public evidence integrity and a bounded audit's explicit no-go outcomes."""

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from parity_fixture import AT, market

from aca.adapters.kraken.book import checksum
from aca.domain.market import Level
from aca.research.artifacts import canonical, digest, read_json
from aca.research.assurance import cost_review, data_review, save_reviews
from aca.research.datasets import import_dataset

D = Decimal


def cost_input(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    epoch = uuid4()
    for seconds, bid, ask in ((0, "100", "102"), (1, "101.5", "102.5"), (5, "101", "103")):
        at = AT + timedelta(seconds=seconds)
        crc = checksum({D(bid): D(1)}, {D(ask): D(1)})
        payload = canonical(
            {
                "channel": "book",
                "type": "snapshot",
                "data": [
                    {
                        "symbol": "BTC/GBP",
                        "bids": [{"price": bid, "qty": "1"}],
                        "asks": [{"price": ask, "qty": "1"}],
                        "checksum": crc,
                        "timestamp": at.isoformat(),
                    }
                ],
            }
        ).decode()
        received = at + timedelta(milliseconds=20)
        book = market(received).book.model_copy(
            update={
                "venue": "kraken",
                "event_at": at,
                "received_at": received,
                "epoch": epoch,
                "bids": (Level(price=D(bid), quantity=D(1)),),
                "asks": (Level(price=D(ask), quantity=D(1)),),
            }
        )
        result.append(
            {
                "id": str(uuid4()),
                "epoch": str(epoch),
                "received_at": received.isoformat(),
                "payload": payload,
                "payload_hash": digest(payload.encode()),
                "normalized": {"books": [book.model_dump(mode="json")]},
            }
        )
    path.write_bytes(b"".join(canonical(row) for row in result))
    return result


def test_public_spread_delay_and_markout_are_measurements_not_fill_evidence(tmp_path: Path) -> None:
    path = tmp_path / "books.jsonl"
    cost_input(path)
    report = cost_review(path)
    assert report["receipts"] == 3
    series = report["series"]["BTC/GBP"]
    assert series["books"] == 3
    assert D(series["event_to_receipt_ms_including_clock_error"]["median"]) == 20
    assert (
        series["hypothetical_taker_markouts_no_fills"]["1s"]["buy_at_ask_to_later_bid_bps"]["count"]
        == 1
    )
    assert report["fees"]["private_fee_verified"] is False
    assert report["raw_hashes_and_top10_checksums_rechecked"]
    assert "command_latency_not_measured" in report["blockers"]
    assert "queue_and_maker_fill_calibration_absent" in report["blockers"]
    assert not report["promotion_evidence"]


@pytest.mark.parametrize("fault", ["raw", "checksum", "time", "epoch", "duplicate", "order"])
def test_changed_cost_evidence_cannot_become_calibration(tmp_path: Path, fault: str) -> None:
    path = tmp_path / "books.jsonl"
    rows = cost_input(path)
    if fault == "raw":
        rows[0]["payload"] += " "
    elif fault == "checksum":
        rows[0]["normalized"]["books"][0]["bids"][0]["quantity"] = "2"
    elif fault == "time":
        rows[0]["received_at"] = AT.isoformat()
    elif fault == "epoch":
        rows[0]["epoch"] = str(uuid4())
    elif fault == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows.reverse()
    path.write_bytes(b"".join(canonical(row) for row in rows))
    with pytest.raises(ValueError):
        cost_review(path)


def assurance_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    start = datetime(2015, 1, 1, tzinfo=UTC)
    end = datetime(2026, 10, 1, tzinfo=UTC)
    files = []
    reviews = []
    inputs = {}
    for symbol, label in (("BTC/GBP", "BTC"), ("ETH/GBP", "ETH")):
        for interval in (240, 1440):
            step = timedelta(minutes=interval)
            raw = (
                f"{int(start.timestamp())},100,110,90,101,1,1\n"
                f"{int((end - step).timestamp())},101,110,90,102,1,1\n"
            ).encode()
            name = f"{label}-{interval}.csv"
            (tmp_path / name).write_bytes(raw)
            sha = digest(raw)
            files.append(
                {
                    "path": name,
                    "symbol": symbol,
                    "interval_minutes": interval,
                    "sha256": sha,
                    "coverage_start": start.isoformat(),
                    "coverage_end": end.isoformat(),
                }
            )
            inputs[name] = {"bytes": len(raw), "sha256": sha}
            reviews.append(
                {
                    "symbol": symbol,
                    "interval_minutes": interval,
                    "revised_csv_sha256": sha,
                    "corrected_rows": 0,
                    "rows": 2,
                    "first": start.isoformat(),
                    "last_end": end.isoformat(),
                    "gaps": [
                        {
                            "start": (start + step).isoformat(),
                            "end": (end - step).isoformat(),
                            "classification": "unexplained",
                        }
                    ],
                    "disputed_populated_candles": [(end - step).isoformat()]
                    if symbol == "BTC/GBP"
                    else [],
                }
            )
    original = {
        "source_bundle": {"evidence": {"published_full_sha256": digest(b"synthetic full archive")}}
    }
    original_path = tmp_path / "original.json"
    original_path.write_bytes(canonical(original))
    original_sha = digest(canonical(original))
    audit = {
        "schema_version": 1,
        "kind": "kraken_historical_gap_audit",
        "decision": "GAP_REVIEW_COMPLETE_WITH_DISPUTED_TRADES",
        "raw_inputs_modified": False,
        "candle_corrections": [],
        "holdout_performance_opened": False,
        "original_dataset_sha256": original_sha,
        "retrieved_at": "2026-10-23T00:00:00+00:00",
        "coverage_end_exclusive": end.isoformat(),
        "inputs": inputs,
        "series": reviews,
        "unresolved_trade_range_count": 1,
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_bytes(canonical(audit))
    source = {
        "schema_version": 1,
        "source_url": "https://example.invalid/synthetic-source",
        "retrieved_at": audit["retrieved_at"],
        "usage_note": "Synthetic evidence only",
        "assurance": "local_unverified",
        "evidence": {
            "audit_sha256": digest(canonical(audit)),
            "original_dataset_sha256": original_sha,
        },
        "files": files,
    }
    source_path = tmp_path / "source.json"
    source_path.write_bytes(canonical(source))
    manifest = import_dataset(source_path, tmp_path / "research", gap_audit=audit_path)
    return source_path, audit_path, manifest, original_path


def test_integrity_and_review_completion_do_not_remove_fact_blockers(tmp_path: Path) -> None:
    source, audit, manifest, original = assurance_inputs(tmp_path)
    report = data_review(source, audit, tmp_path, manifest, original_manifest=original)
    assert report["decision"] == "BLOCKED_NO_GO"
    assert not report["full_archive_sha256_verified"]
    assert "btc_holdout_and_eth_blend_comparison_quarantined" in report["blockers"]
    assert "unexplained_calendar_discontinuity" in report["blockers"]
    assert "historical_rules_reconstructed_not_verified" in report["blockers"]
    assert "historical_fees_reconstructed_not_verified" in report["blockers"]
    assert all(
        item["quarantined_starts"] for item in report["series"] if item["symbol"] == "BTC/GBP"
    )
    assert report["holdout"]["performance_opened"] is False
    assert report["bounded_review"]["network_requests"] == 0
    archive = tmp_path / "full.zip"
    archive.write_bytes(b"synthetic full archive")
    full = data_review(
        source, audit, tmp_path, manifest, original_manifest=original, full_archive=archive
    )
    assert full["full_archive_sha256_verified"]
    assert "full_archive_sha256_not_verified" not in full["blockers"]
    assert "btc_trade_ids_7098364_7098366_disputed" in full["blockers"]
    books = tmp_path / "books.jsonl"
    cost_input(books)
    paths = save_reviews(tmp_path / "results", cost_review(books), report)
    assert read_json(Path(paths["data_review"])) == report
    assert save_reviews(tmp_path / "results", cost_review(books), report) == paths


@pytest.mark.parametrize("fault", ["raw", "audit", "original", "full"])
def test_source_assurance_rejects_changed_bound_evidence(tmp_path: Path, fault: str) -> None:
    source, audit, manifest, original = assurance_inputs(tmp_path)
    archive: Path | None = None
    if fault == "raw":
        (tmp_path / "BTC-240.csv").write_bytes(b"changed")
    elif fault == "audit":
        audit.write_bytes(b"{}")
    elif fault == "original":
        original.write_bytes(b"{}")
    else:
        archive = tmp_path / "full.zip"
        archive.write_bytes(b"unverified archive")
    with pytest.raises(ValueError, match=r"checksum|hash|lineage"):
        data_review(
            source, audit, tmp_path, manifest, original_manifest=original, full_archive=archive
        )
