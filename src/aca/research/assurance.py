"""Bounded offline cost/source reviews. Missing facts remain explicit blockers."""

import math
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from itertools import pairwise
from pathlib import Path
from typing import Any

from aca.adapters.kraken.book import checksum
from aca.adapters.kraken.wire import BookData, decode
from aca.domain.market import OrderBook
from aca.domain.values import FINANCIAL_CONTEXT, utc_datetime
from aca.research.artifacts import canonical, digest, file_hash, read_json, save_document
from aca.research.calendar import HOLDOUT_END, HOLDOUT_START, common_calendar
from aca.research.datasets import load_dataset, source_bundle
from aca.research.protocol import walk_forward
from aca.research.reviewed import validate_review


def _distribution(values: list[Decimal]) -> dict[str, object]:
    values = sorted(values)
    return {
        "count": len(values),
        "minimum": str(values[0]) if values else None,
        "median": str(values[(len(values) - 1) // 2]) if values else None,
        "p95_nearest_rank": str(values[math.ceil(len(values) * 0.95) - 1]) if values else None,
        "maximum": str(values[-1]) if values else None,
    }


def cost_review(receipts: Path) -> dict[str, Any]:
    """Recheck raw hashes and CRCs in a bounded read-only public book export.

    Propagation delay includes exchange/local clock error. Hypothetical taker
    markouts are not observed fills, order latency or passive queue calibration.
    """
    if not 0 < receipts.stat().st_size <= 64_000_000:
        raise ValueError("public cost input exceeds 64MB budget")
    books: dict[str, list[OrderBook]] = defaultdict(list)
    seen: set[str] = set()
    previous: datetime | None = None
    with receipts.open("rb") as stream:
        for index, line in enumerate(stream):
            if index >= 20_000 or len(line) > 2_000_000:
                raise ValueError("public cost receipt count/line budget exceeded")
            record = decode(line)
            payload, ident = record.get("payload"), record.get("id")
            normalized = record.get("normalized")
            if not isinstance(payload, str) or not isinstance(ident, str) or ident in seen:
                raise ValueError("invalid/duplicate public receipt identity")
            if digest(payload.encode()) != record.get("payload_hash"):
                raise ValueError("public receipt raw payload hash mismatch")
            seen.add(ident)
            raw = decode(payload)
            data = raw.get("data")
            if raw.get("channel") != "book" or not isinstance(data, list):
                raise ValueError("public cost evidence must contain raw book messages")
            if not isinstance(normalized, dict) or not isinstance(normalized.get("books"), list):
                raise ValueError("public cost evidence lacks normalized books")
            wire = [BookData.model_validate(item) for item in data]
            if len(wire) != len(normalized["books"]):
                raise ValueError("raw/normalized book count differs")
            for document, original in zip(normalized["books"], wire, strict=True):
                book = OrderBook.model_validate_json(canonical(document))
                at = record.get("received_at")
                if not isinstance(at, str):
                    raise ValueError("missing public receipt time")
                if (
                    book.symbol != original.symbol
                    or book.event_at != original.timestamp
                    or str(book.epoch) != record.get("epoch")
                    or book.received_at != utc_datetime(datetime.fromisoformat(at))
                    or (previous is not None and book.received_at < previous)
                    or checksum(
                        {v.price: v.quantity for v in book.bids},
                        {v.price: v.quantity for v in book.asks},
                    )
                    != original.checksum
                ):
                    raise ValueError("public cost receipt binding/checksum mismatch")
                previous = book.received_at
                books[book.symbol].append(book)
    if not books:
        raise ValueError("empty public cost sample")
    reports: dict[str, Any] = {}
    with localcontext(FINANCIAL_CONTEXT):
        for symbol, records in sorted(books.items()):
            spreads: list[Decimal] = []
            delays: list[Decimal] = []
            clock_inconsistent = 0
            for book in records:
                bid, ask = book.bids[0].price, book.asks[0].price
                spreads.append((ask - bid) / ((ask + bid) / 2) * 10_000)
                if book.event_at is not None:
                    delay = Decimal(str((book.received_at - book.event_at).total_seconds())) * 1000
                    delays.append(delay)
                    clock_inconsistent += int(delay < 0)
            times = [b.received_at for b in records]
            markouts: dict[str, object] = {}
            for seconds in (1, 5):
                buy: list[Decimal] = []
                sell: list[Decimal] = []
                for book in records:
                    target = book.received_at + timedelta(seconds=seconds)
                    offset = bisect_left(times, target)
                    if offset == len(records):
                        continue
                    later = records[offset]
                    if later.epoch != book.epoch or later.received_at > target + timedelta(
                        seconds=1
                    ):
                        continue
                    buy.append((later.bids[0].price / book.asks[0].price - 1) * 10_000)
                    sell.append((1 - later.asks[0].price / book.bids[0].price) * 10_000)
                markouts[f"{seconds}s"] = {
                    "buy_at_ask_to_later_bid_bps": _distribution(buy),
                    "sell_at_bid_to_later_ask_bps": _distribution(sell),
                }
            reports[symbol] = {
                "books": len(records),
                "epochs": len({b.epoch for b in records}),
                "first": times[0].isoformat(),
                "last": times[-1].isoformat(),
                "spread_bps": _distribution(spreads),
                "event_to_receipt_ms_including_clock_error": _distribution(delays),
                "negative_delay_observations": clock_inconsistent,
                "hypothetical_taker_markouts_no_fills": markouts,
            }
    return {
        "schema_version": 1,
        "kind": "public-execution-cost-review-v1",
        "source_sha256": file_hash(receipts),
        "source_bytes": receipts.stat().st_size,
        "receipts": len(seen),
        "series": reports,
        "sampling": (
            "bounded_valid_retained_books_by_received_at_then_id; short_convenience_sample"
        ),
        "raw_hashes_and_top10_checksums_rechecked": True,
        "fees": {
            "risk_entry_exit_floor": "0.008",
            "modelled_taker_base": "0.008",
            "modelled_taker_stress": "0.012",
            "private_fee_verified": False,
        },
        "assumptions": {
            "spread_slippage_one_way": "0.001",
            "stress_one_way": "0.002",
            "command_latency_ms": 250,
            "latency_stress_ms": 2000,
            "maximum_print_participation": "0.01",
        },
        "blockers": [
            "short_nonrepresentative_public_sample",
            "command_latency_not_measured",
            "queue_and_maker_fill_calibration_absent",
            "maker_adverse_selection_not_established",
            "private_account_fees_unverified",
        ],
        "decision": "CONSERVATIVE_TAKER_ASSUMPTIONS_ONLY; MAKER_UNQUALIFIED",
        "promotion_evidence": False,
        "live_execution_available": False,
    }


def data_review(
    source: Path,
    audit: Path,
    raw_directory: Path,
    dataset: Path,
    *,
    full_archive: Path | None = None,
    original_manifest: Path | None = None,
) -> dict[str, Any]:
    """Revalidate retained evidence without strategy returns or archive downloads.

    Full source truth, historical metadata/fees and the existing BTC dispute are
    separate from revalidated local integrity. A complete review can say no-go.
    """
    bundle = source_bundle(source)
    document: dict[str, Any] = read_json(audit)
    if file_hash(audit) != bundle.evidence.get("audit_sha256"):
        raise ValueError("data assurance audit hash mismatch")
    validate_review(document, bundle.model_dump(mode="json"))
    inputs = document.get("inputs")
    if not isinstance(inputs, dict) or not 1 <= len(inputs) <= 64:
        raise ValueError("invalid assurance input inventory")
    verified: dict[str, object] = {}
    total = 0
    for name, expected in sorted(inputs.items()):
        if Path(name).name != name or not isinstance(expected, dict):
            raise ValueError("invalid assurance input path")
        path = raw_directory / name
        size = path.stat().st_size
        total += size
        if (
            total > 1_000_000_000
            or size != expected.get("bytes")
            or file_hash(path) != expected.get("sha256")
        ):
            raise ValueError("assurance source bytes/checksum mismatch")
        verified[name] = {"bytes": size, "sha256": expected["sha256"]}
    loaded = load_dataset(dataset)
    if loaded.manifest.get("gap_audit_sha256") != file_hash(audit):
        raise ValueError("data assurance dataset lineage mismatch")
    if loaded.manifest.get("source_bundle") != bundle.model_dump(mode="json"):
        raise ValueError("data assurance source binding mismatch")
    # Header/range inspection only; no return/strategy calculations in holdout.
    calendar = common_calendar(
        {
            (s.symbol, s.interval_minutes): (
                datetime.fromisoformat(s.coverage_start),
                datetime.fromisoformat(s.coverage_end),
            )
            for s in bundle.files
        }
    )
    series: list[dict[str, object]] = []
    for key, bars in sorted(loaded.series.items()):
        unknown = sum(a.end != b.start for a, b in pairwise(bars))
        quarantined = [b.start.isoformat() for b in bars if "disputed_trade_evidence" in b.quality]
        legacy = walk_forward(bars[0].start + timedelta(minutes=201 * key[1]), HOLDOUT_START)
        series.append(
            {
                "symbol": key[0],
                "interval_minutes": key[1],
                "bars": len(bars),
                "indicator_only": sum(b.quality == ("confirmed_no_trade",) for b in bars),
                "unknown_discontinuities": unknown,
                "quarantined_starts": quarantined,
                "legacy_unused_pre_holdout_tail": {
                    "start": legacy[-1].test_end.isoformat() if legacy else None,
                    "end_exclusive": HOLDOUT_START.isoformat(),
                    "now_included_in_common_calendar": True,
                },
            }
        )
    original_sha: str | None = None
    published: object = bundle.evidence.get("published_full_sha256")
    if original_manifest is not None:
        original = read_json(original_manifest)
        original_sha = digest(canonical(original))
        if original_sha != document.get("original_dataset_sha256"):
            raise ValueError("original source manifest lineage mismatch")
        original_bundle = original.get("source_bundle")
        if not isinstance(original_bundle, dict) or not isinstance(
            original_bundle.get("evidence"), dict
        ):
            raise ValueError("missing original archive evidence")
        published = original_bundle["evidence"].get("published_full_sha256")
    full_verified = False
    if full_archive is not None:
        if (
            not isinstance(published, str)
            or full_archive.stat().st_size > 12_000_000_000
            or file_hash(full_archive) != published
        ):
            raise ValueError("full source archive hash mismatch/budget exceeded")
        full_verified = True
    blockers: list[str] = []
    if not full_verified:
        blockers.append("full_archive_sha256_not_verified")
    if document.get("unresolved_trade_range_count") != 0:
        blockers.extend(
            [
                "btc_trade_ids_7098364_7098366_disputed",
                "btc_holdout_and_eth_blend_comparison_quarantined",
            ]
        )
    if any(s["unknown_discontinuities"] for s in series):
        blockers.append("unexplained_calendar_discontinuity")
    blockers.extend(
        [
            "historical_fees_reconstructed_not_verified",
            "historical_rules_reconstructed_not_verified",
            "historical_trade_receipt_latency_unavailable",
        ]
    )
    return {
        "schema_version": 1,
        "kind": "historical-source-assurance-review-v1",
        "decision": "BLOCKED_NO_GO" if blockers else "REVIEW_COMPLETE",
        "source_sha256": file_hash(source),
        "source_url": bundle.source_url,
        "retrieved_at": bundle.retrieved_at,
        "usage_note": bundle.usage_note,
        "declared_source_assurance": bundle.assurance,
        "audit_sha256": file_hash(audit),
        "dataset_sha256": loaded.fingerprint,
        "verified_inputs": verified,
        "rehashed_bytes": total,
        "series": series,
        "full_archive_sha256_verified": full_verified,
        "published_archive_sha256": published,
        "original_manifest_sha256": original_sha,
        "same_publisher_corroboration_is_not_independent_completeness": True,
        "revisions": {
            "raw_inputs_modified": document.get("raw_inputs_modified"),
            "candle_corrections": document.get("candle_corrections"),
            "quarterly_equal_rows": [
                {
                    "symbol": item["symbol"],
                    "interval_minutes": item["interval_minutes"],
                    "rows": item.get("quarterly_rows_equal"),
                }
                for item in document["series"]
            ],
        },
        "common_calendar": {
            "origin": calendar.origin.isoformat(),
            "oos_start": calendar.oos_start.isoformat(),
            "oos_end": calendar.oos_end.isoformat(),
            "windows": len(calendar.windows),
            "last_test_start": calendar.windows[-1].test_start.isoformat(),
            "partial_tail": calendar.windows[-1].partial_tail,
        },
        "holdout": {
            "start": HOLDOUT_START.isoformat(),
            "end_exclusive": HOLDOUT_END.isoformat(),
            "performance_opened": False,
        },
        "bounded_review": {
            "network_requests": 0,
            "full_archive_downloaded": False,
            "scope": "rehash_retained_inputs_rebuild_reviewed_projection_retain_dispute",
            "resolution": ("missing_archive_or_authoritative_conflict_resolution_remain_blockers"),
        },
        "blockers": blockers,
        "promotion_evidence": False,
        "live_execution_available": False,
    }


def save_reviews(root: Path, cost: dict[str, Any], data: dict[str, Any]) -> dict[str, str]:
    return {
        "cost_review": str(save_document(root / "assurance", cost)),
        "data_review": str(save_document(root / "assurance", data)),
    }
