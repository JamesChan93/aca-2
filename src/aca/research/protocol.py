"""Predeclared windows and costs. Holdout is never part of ordinary research runs."""

import calendar
from dataclasses import dataclass
from datetime import datetime

from aca.domain.market import Symbol
from aca.strategies.slow import VARIANTS, Variant

TRIALS: tuple[tuple[Symbol, int, Variant], ...] = tuple(
    (symbol, interval, variant)
    for symbol in ("BTC/GBP", "ETH/GBP")
    for interval in (240, 1440)
    for variant in VARIANTS
)


def months(at: datetime, count: int) -> datetime:
    year, month = divmod(at.year * 12 + at.month - 1 + count, 12)
    month += 1
    return at.replace(year=year, month=month, day=min(at.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class Fold:
    train_start: datetime
    train_end: datetime
    validation_end: datetime
    test_end: datetime


def walk_forward(start: datetime, end: datetime) -> tuple[Fold, ...]:
    """24 training months, six validation, six test; advance six. End excludes holdout."""
    folds: list[Fold] = []
    while months(start, 36) <= end:
        folds.append(Fold(start, months(start, 24), months(start, 30), months(start, 36)))
        start = months(start, 6)
    return tuple(folds)
