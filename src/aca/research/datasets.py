"""Audit headerless Kraken CSVs; unexplained gaps are never tradable bars."""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from aca.domain.market import Candle, Symbol
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal, utc_datetime
from aca.operations.config import StrictModel
from aca.research.artifacts import canonical, digest, file_hash, read_json, write_once
from aca.research.protocol import months
from aca.research.reviewed import reviewed_bars, validate_review


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expected ISO UTC timestamp")
    return utc_datetime(datetime.fromisoformat(value))


class SourceFile(StrictModel):
    path: str
    symbol: Symbol
    interval_minutes: Literal[240, 1440]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_start: str
    coverage_end: str

    @field_validator("coverage_start", "coverage_end")
    @classmethod
    def dates(cls, value: str) -> str:
        return instant(value).isoformat()


class SourceBundle(StrictModel):
    schema_version: Literal[1] = 1
    source_url: str = Field(pattern=r"^https://")
    retrieved_at: str
    usage_note: str = Field(min_length=1)
    assurance: Literal["local_unverified", "range_members_crc", "full_archive_sha256"]
    evidence: dict[str, object]
    files: tuple[SourceFile, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version requires an integer")
        return value

    @field_validator("retrieved_at")
    @classmethod
    def date(cls, value: str) -> str:
        return instant(value).isoformat()

    @model_validator(mode="after")
    def bounded(self) -> SourceBundle:
        keys = [(item.symbol, item.interval_minutes) for item in self.files]
        if not 1 <= len(keys) <= 16 or len(keys) != len(set(keys)):
            raise ValueError("duplicate or excessive source files")
        return self


def source_bundle(path: Path) -> SourceBundle:
    return source_bundle_from_document(read_json(path))


def source_bundle_from_document(document: dict[str, object]) -> SourceBundle:
    document = dict(document)
    files = document.get("files")
    if isinstance(files, list):
        document["files"] = tuple(files)
    return SourceBundle.model_validate(document)


def parse_csv(data: bytes, source: SourceFile, observed: datetime) -> tuple[Candle, ...]:
    if len(data) > 32_000_000:
        raise ValueError("CSV exceeds 32MB research budget")
    bars: list[Candle] = []
    start, end = instant(source.coverage_start), instant(source.coverage_end)
    step = timedelta(minutes=source.interval_minutes)
    if (
        start >= end
        or start.timestamp() % step.total_seconds()
        or end.timestamp() % step.total_seconds()
    ):
        raise ValueError("unaligned or empty coverage")
    for row in csv.reader(io.StringIO(data.decode("utf-8-sig"))):
        if len(row) != 7 or not row[0].isdigit() or not row[6].isdigit():
            raise ValueError("invalid headerless OHLCVT row")
        at = datetime.fromtimestamp(int(row[0]), UTC)
        if not start <= at < end or (bars and at <= bars[-1].start):
            raise ValueError("out-of-order, duplicate or out-of-coverage candle")
        with localcontext(FINANCIAL_CONTEXT):
            volume = Decimal(row[5])
            if not volume.is_finite() or not 0 < volume < Decimal("1e20"):
                raise ValueError("invalid archive volume")
            volume = parse_decimal(volume.quantize(Decimal("1e-18"), rounding=ROUND_DOWN))
        if int(row[6]) <= 0 or volume <= 0:
            raise ValueError("archive populated candles require trades and volume")
        bars.append(
            Candle(
                venue="kraken",
                symbol=source.symbol,
                interval_minutes=source.interval_minutes,
                start=at,
                end=at + step,
                open=parse_decimal(row[1]),
                high=parse_decimal(row[2]),
                low=parse_decimal(row[3]),
                close=parse_decimal(row[4]),
                volume=volume,
                trade_count=int(row[6]),
                complete=True,
                quality=(),
                source="audited_kraken_schema_csv",
                observed_at=observed,
            )
        )
    if not bars:
        raise ValueError("empty source")
    return tuple(bars)


@dataclass(frozen=True)
class Dataset:
    fingerprint: str
    manifest: dict[str, object]
    series: dict[tuple[Symbol, int], tuple[Candle, ...]]


def _gaps(bars: tuple[Candle, ...], source: SourceFile) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    previous = instant(source.coverage_start)
    for bar in bars:
        if bar.start != previous:
            gaps.append(
                {
                    "start": previous.isoformat(),
                    "end": bar.start.isoformat(),
                    "classification": "unexplained",
                }
            )
        previous = bar.end
    if previous != instant(source.coverage_end):
        gaps.append(
            {
                "start": previous.isoformat(),
                "end": source.coverage_end,
                "classification": "unexplained",
            }
        )
    return gaps


def _review(path: Path, bundle: SourceBundle) -> dict[str, object]:
    expected = bundle.evidence.get("audit_sha256")
    if not isinstance(expected, str) or file_hash(path) != expected:
        raise ValueError("gap audit hash does not match source provenance")
    document = read_json(path)
    validate_review(document, bundle.model_dump(mode="json"))
    return document


def import_dataset(source_path: Path, root: Path, *, gap_audit: Path | None = None) -> Path:
    bundle = source_bundle(source_path)
    observed = instant(bundle.retrieved_at)
    review = _review(gap_audit, bundle) if gap_audit else None
    if review is not None and gap_audit is not None:
        write_once(
            root / "objects" / (str(bundle.evidence["audit_sha256"]) + ".json"),
            gap_audit.read_bytes(),
        )
    reports: list[dict[str, object]] = []
    for source in bundle.files:
        path = (source_path.parent / source.path).resolve()
        if not path.is_relative_to(source_path.parent.resolve()):
            raise ValueError("source path must remain inside the bundle directory")
        if path.stat().st_size > 32_000_000 or file_hash(path) != source.sha256:
            raise ValueError("source hash or size mismatch")
        data = path.read_bytes()
        bars = parse_csv(data, source, observed)
        gaps = _gaps(bars, source)
        raw_rows = len(bars)
        if review is not None:
            bars, gaps = reviewed_bars(bars, source.model_dump(mode="json"), review, gaps)
        name = source.sha256 + ".csv"
        write_once(root / "objects" / name, data)
        reports.append(
            {
                "source": source.model_dump(mode="json"),
                "object": name,
                "rows": len(bars),
                **(
                    {
                        "raw_rows": raw_rows,
                        "indicator_only_rows": sum(
                            bar.quality == ("confirmed_no_trade",) for bar in bars
                        ),
                        "quarantined_rows": sum(
                            "disputed_trade_evidence" in bar.quality for bar in bars
                        ),
                    }
                    if review is not None
                    else {}
                ),
                "first": bars[0].start.isoformat(),
                "last_end": bars[-1].end.isoformat(),
                "gaps": gaps,
            }
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "transform": "kraken-reviewed-no-trade-v2" if review else "kraken-ohlcvt-audit-v1",
        "source_bundle": bundle.model_dump(mode="json"),
        "series": reports,
        "missing_intervals": (
            "individually_reviewed_zero_liquidity_other_gaps_block"
            if review
            else "unexplained_until_individually_reviewed"
        ),
        "historical_metadata": "reconstructed_current_rules_not_point_in_time",
        "historical_fees": "conservative_planning_bounds_not_account_history",
        "volume_transform": "round_down_18_decimal_places_raw_bytes_retained",
        **(
            {
                "gap_audit_sha256": bundle.evidence["audit_sha256"],
                "indicator_convention": "past_close_only_zero_volume_no_executable_liquidity",
                "holdout_start": months(instant(bundle.files[0].coverage_end), -12).isoformat(),
                "holdout_end": bundle.files[0].coverage_end,
            }
            if review
            else {}
        ),
    }
    data = canonical(manifest)
    target = root / "datasets" / digest(data) / "manifest.json"
    write_once(target, data)
    return target


def load_dataset(path: Path) -> Dataset:
    manifest = read_json(path)
    fingerprint = digest(canonical(manifest))
    if path.parent.name != fingerprint:
        raise ValueError("manifest content hash mismatch")
    if manifest.get("transform") not in {"kraken-ohlcvt-audit-v1", "kraken-reviewed-no-trade-v2"}:
        raise ValueError("unsupported dataset transform")
    bundle = manifest["source_bundle"]
    if not isinstance(bundle, dict) or not isinstance(bundle.get("retrieved_at"), str):
        raise ValueError("missing provenance")
    observed = instant(bundle["retrieved_at"])
    review = None
    if manifest["transform"] == "kraken-reviewed-no-trade-v2":
        reviewed_bundle = source_bundle_from_document(bundle)
        expected = reviewed_bundle.evidence.get("audit_sha256")
        if manifest.get("gap_audit_sha256") != expected:
            raise ValueError("dataset gap audit binding mismatch")
        review = _review(path.parents[2] / "objects" / (str(expected) + ".json"), reviewed_bundle)
    series: dict[tuple[Symbol, int], tuple[Candle, ...]] = {}
    records = manifest["series"]
    if not isinstance(records, list):
        raise ValueError("invalid series list")
    for record in records:
        source = SourceFile.model_validate(record["source"])
        # Reconstruct the location from the hash, never a manifest-controlled path.
        object_path = path.parents[2] / "objects" / (source.sha256 + ".csv")
        if object_path.stat().st_size > 32_000_000 or file_hash(object_path) != source.sha256:
            raise ValueError("dataset object hash mismatch")
        bars = parse_csv(object_path.read_bytes(), source, observed)
        if review is not None:
            bars, gaps = reviewed_bars(
                bars, source.model_dump(mode="json"), review, _gaps(bars, source)
            )
            if gaps != record["gaps"] or len(bars) != record["rows"]:
                raise ValueError("reviewed dataset projection mismatch")
        series[(source.symbol, source.interval_minutes)] = bars
    return Dataset(fingerprint, manifest, series)
