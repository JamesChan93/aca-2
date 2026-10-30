"""Frozen common dates; reporting windows do not define account lifetimes."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from aca.domain.market import Candle, Symbol
from aca.domain.values import utc_datetime
from aca.research.protocol import months

HOLDOUT_START = datetime(2025, 10, 1, tzinfo=UTC)
HOLDOUT_END = datetime(2026, 10, 1, tzinfo=UTC)
ACTUAL_SCOPE = "actual:kraken-spot-gbp:2025-10-01:2026-10-01"
CALENDAR_VERSION = "common-continuous-calendar-v1"


@dataclass(frozen=True)
class TestWindow:
    train_start: datetime
    train_end: datetime
    validation_end: datetime
    test_end: datetime
    partial_tail: bool = False

    @property
    def test_start(self) -> datetime:
        return self.validation_end


@dataclass(frozen=True)
class CommonCalendar:
    origin: datetime
    oos_start: datetime
    oos_end: datetime
    windows: tuple[TestWindow, ...]
    supplementary_before_origin: dict[str, tuple[str, str]]
    version: str = CALENDAR_VERSION
    procedure: str = "fixed_rule_rolling_evaluation; no_fitted_training_claim"


def common_calendar(
    coverage: Mapping[tuple[Symbol, int], tuple[datetime, datetime]],
    *,
    origin: datetime | None = None,
) -> CommonCalendar:
    """Use the same month origin for four series and include the unused tail.

    Coverage here is a declared range, not proof of continuity. ``qualified_bars``
    separately checks actual past-only warm-up and every evaluation interval.
    """
    required = {(s, i) for s in ("BTC/GBP", "ETH/GBP") for i in (240, 1440)}
    if set(coverage) != required:
        raise ValueError("common calendar requires both assets and both intervals")
    earliest: list[datetime] = []
    for (_, interval), (start, end) in coverage.items():
        start, end = utc_datetime(start), utc_datetime(end)
        if start >= end or end < HOLDOUT_START:
            raise ValueError("insufficient declared pre-holdout coverage")
        earliest.append(start + timedelta(minutes=200 * interval))
    available = max(earliest)
    floor = available.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    earliest_origin = floor if floor == available else months(floor, 1)
    origin = utc_datetime(origin) if origin is not None else earliest_origin
    if origin < earliest_origin or origin != origin.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ):
        raise ValueError("origin must follow common warm-up at a UTC month boundary")
    if months(origin, 30) >= HOLDOUT_START:
        raise ValueError("no pre-holdout out-of-sample period")
    windows: list[TestWindow] = []
    anchor = origin
    while months(anchor, 30) < HOLDOUT_START:
        nominal_end = months(anchor, 36)
        windows.append(
            TestWindow(
                anchor,
                months(anchor, 24),
                months(anchor, 30),
                min(nominal_end, HOLDOUT_START),
                nominal_end > HOLDOUT_START,
            )
        )
        anchor = months(anchor, 6)
    return CommonCalendar(
        origin,
        windows[0].test_start,
        HOLDOUT_START,
        tuple(windows),
        {
            f"{symbol}:{interval}": (start.isoformat(), origin.isoformat())
            for (symbol, interval), (start, _) in sorted(coverage.items())
        },
    )


def reporting_windows(
    start: datetime, end: datetime, boundaries: Sequence[datetime]
) -> tuple[tuple[datetime, datetime], ...]:
    start, end = utc_datetime(start), utc_datetime(end)
    cuts = tuple(utc_datetime(at) for at in boundaries)
    if start >= end or end > HOLDOUT_START or cuts != tuple(sorted(set(cuts))):
        raise ValueError("invalid/pre-holdout reporting calendar")
    if any(not start < at < end for at in cuts):
        raise ValueError("reporting boundary outside evaluation")
    edges = (start, *cuts, end)
    return tuple(pairwise(edges))


def qualified_bars(
    bars: Sequence[Candle], symbol: Symbol, start: datetime, end: datetime, *, warmup: int = 200
) -> tuple[Candle, ...]:
    """Select 200 past bars and the entire requested range, without holdout reads.

    A populated predecessor is retained for validating reviewed no-trade carries.
    Quarantine/gaps cannot silently shorten the common calendar.
    """
    start, end = utc_datetime(start), utc_datetime(end)
    if start >= end or end > HOLDOUT_START:
        raise ValueError("ordinary evaluation cannot open the fixed holdout")
    # Ignore later objects without inspecting their price/quality values.
    selected = [bar for bar in bars if bar.start < end]
    preceding = [bar for bar in selected if bar.end <= start]
    if warmup not in {200, 201} or len(preceding) < warmup:
        raise ValueError("insufficient past-only warm-up")
    offset = len(preceding) - warmup
    while offset and preceding[offset].quality == ("confirmed_no_trade",):
        offset -= 1
    result = tuple(preceding[offset:] + [b for b in selected if b.start >= start])
    evaluation = [b for b in result if b.start >= start]
    if not evaluation or evaluation[0].start != start or evaluation[-1].end != end:
        raise ValueError("incomplete common-calendar boundary")
    interval = result[0].interval_minutes
    for index, bar in enumerate(result):
        carried = (
            index > 0
            and bar.quality == ("confirmed_no_trade",)
            and bar.volume == bar.trade_count == 0
            and bar.open == bar.high == bar.low == bar.close == result[index - 1].close
        )
        if (
            bar.symbol != symbol
            or bar.interval_minutes != interval
            or (not bar.complete and not carried)
            or (bar.complete and bar.quality)
            or (index > 0 and result[index - 1].end != bar.start)
        ):
            raise ValueError("unqualified/discontinuous common-calendar data")
    return result
