"""Explicit, hash-bound historical gap review. No network or execution authority."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from aca.domain.market import Candle


def validate_review(review: dict[str, Any], bundle: dict[str, Any]) -> None:
    """Accept the audit's unchanged-source path only; repairs need separate review."""
    if (
        type(review.get("schema_version")) is not int
        or review.get("schema_version") != 1
        or review.get("kind") != "kraken_historical_gap_audit"
        or review.get("decision") != "GAP_REVIEW_COMPLETE_WITH_DISPUTED_TRADES"
        or review.get("raw_inputs_modified") is not False
        or review.get("candle_corrections") != []
        or review.get("holdout_performance_opened") is not False
        or review.get("original_dataset_sha256")
        != bundle["evidence"].get("original_dataset_sha256")
        or review.get("retrieved_at") != bundle["retrieved_at"]
        or not isinstance(review.get("series"), list)
    ):
        raise ValueError("unsupported or mismatched gap review")
    keys = [(item["symbol"], item["interval_minutes"]) for item in review["series"]]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate reviewed series")
    for source in bundle["files"]:
        selected = [
            item
            for item in review["series"]
            if (item["symbol"], item["interval_minutes"])
            == (source["symbol"], source["interval_minutes"])
        ]
        if (
            len(selected) != 1
            or selected[0].get("revised_csv_sha256") != source["sha256"]
            or selected[0].get("corrected_rows") != 0
            or source["coverage_end"] != review.get("coverage_end_exclusive")
            or not any(
                item.get("sha256") == source["sha256"] for item in review.get("inputs", {}).values()
            )
        ):
            raise ValueError("gap review source hash or coverage mismatch")


def reviewed_bars(
    bars: tuple[Candle, ...],
    source: dict[str, Any],
    review: dict[str, Any],
    gaps: list[dict[str, str]],
) -> tuple[tuple[Candle, ...], list[dict[str, str]]]:
    """Carry only earlier known closes across individually corroborated empty spans.

    Flagged bars remain incomplete and have zero volume/trades. A leading interval
    cannot borrow a future close. Unknown/missing-with-trades spans remain gaps.
    Disputed populated observations are preserved with a blocking quality flag.
    """
    record = next(
        item
        for item in review["series"]
        if (item["symbol"], item["interval_minutes"])
        == (source["symbol"], source["interval_minutes"])
    )
    declared = record["gaps"]
    if (
        record["rows"] != len(bars)
        or record["first"] != bars[0].start.isoformat()
        or record["last_end"] != bars[-1].end.isoformat()
        or [(gap["start"], gap["end"]) for gap in declared]
        != [(gap["start"], gap["end"]) for gap in gaps]
    ):
        raise ValueError("gap review does not match observed source intervals")
    disputed = record["disputed_populated_candles"]
    if len(disputed) != len(set(disputed)) or not set(disputed) <= {
        bar.start.isoformat() for bar in bars
    }:
        raise ValueError("disputed candle is absent or duplicated")
    step = timedelta(minutes=source["interval_minutes"])
    eligible: dict[datetime, datetime] = {}
    classified: list[dict[str, str]] = []
    for gap in declared:
        start, end = datetime.fromisoformat(gap["start"]), datetime.fromisoformat(gap["end"])
        if gap["classification"] == "confirmed_no_trade":
            if (
                type(gap.get("trade_rows")) is not int
                or gap.get("trade_rows") != 0
                or type(gap.get("hourly_rows")) is not int
                or gap.get("hourly_rows") != 0
                or gap.get("executable_liquidity") is not False
                or type(gap.get("intervals")) is not int
                or gap["intervals"] * step != end - start
            ):
                raise ValueError("no-trade classification lacks zero-liquidity evidence")
            eligible[start] = end
        classified.append(
            {"start": gap["start"], "end": gap["end"], "classification": gap["classification"]}
        )
    result: list[Candle] = []
    previous = datetime.fromisoformat(source["coverage_start"])

    def carry(at: datetime) -> None:
        if at not in eligible or not result:
            return
        stop = eligible[at]
        while at < stop:
            if len(result) >= 200_000:
                raise ValueError("reviewed series exceeds research row budget")
            last = result[-1]
            quality = (
                ("confirmed_no_trade",)
                if last.complete or last.quality == ("confirmed_no_trade",)
                else ("disputed_trade_evidence",)
            )
            result.append(
                Candle(
                    venue=last.venue,
                    symbol=last.symbol,
                    interval_minutes=last.interval_minutes,
                    start=at,
                    end=at + step,
                    open=last.close,
                    high=last.close,
                    low=last.close,
                    close=last.close,
                    volume=Decimal(0),
                    trade_count=0,
                    complete=False,
                    quality=quality,
                    source="reviewed_no_trade_previous_close_v1",
                    observed_at=last.observed_at,
                )
            )
            at += step

    for bar in bars:
        carry(previous)
        if bar.start.isoformat() in disputed:
            bar = bar.model_copy(
                update={"complete": False, "quality": ("disputed_trade_evidence",)}
            )
        result.append(bar)
        previous = bar.end
    # Handle an explicitly corroborated trailing span using past data only.
    carry(previous)
    return tuple(result), classified
