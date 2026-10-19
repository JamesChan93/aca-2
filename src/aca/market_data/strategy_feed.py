"""Bounded point-in-time candle prefixes from existing immutable public receipts."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from uuid import UUID

from sqlalchemy import select

from aca.domain.market import Candle
from aca.domain.trading import fingerprint
from aca.market_data.paper_feed import FeedUnavailable
from aca.storage.models import MarketReceipt
from aca.storage.paper import PaperStore
from aca.storage.strategy import PaperSelection


@dataclass(frozen=True)
class CandlePrefix:
    bars: tuple[Candle, ...]
    sources: tuple[tuple[UUID, str], ...]

    @property
    def fingerprint(self) -> str:
        return fingerprint([bar.model_dump(mode="json") for bar in self.bars])


class StoredStrategyFeed:
    def __init__(self, store: PaperStore) -> None:
        self.store = store

    async def prefix(self, chosen: PaperSelection, end: datetime, at: datetime) -> CandlePrefix:
        """Use one bootstrap archive and at most 202 local receipts, all known by at.

        A fresh collector bootstrap supplies up to 720 bars per pair/interval.
        Local bars extend it. No historical archive or research holdout is opened.
        """
        table = MarketReceipt.__table__
        async with self.store.engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ")
            base = select(table).where(table.c.valid.is_(True), table.c.received_at <= at)
            rest = (
                (
                    await c.execute(
                        base.where(
                            table.c.channel == "rest:closed_candles",
                            table.c.normalized["candles"][0]["symbol"].as_string() == chosen.symbol,
                            table.c.normalized["candles"][0]["interval_minutes"].as_integer()
                            == chosen.interval_minutes,
                        )
                        .order_by(table.c.received_at.desc(), table.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .all()
            )
            local = (
                (
                    await c.execute(
                        base.where(
                            table.c.channel == "local:closed_candle",
                            table.c.normalized["candle"]["symbol"].as_string() == chosen.symbol,
                            table.c.normalized["candle"]["interval_minutes"].as_integer()
                            == chosen.interval_minutes,
                            table.c.received_at
                            >= end - timedelta(minutes=chosen.interval_minutes * 202),
                        )
                        .order_by(table.c.received_at.desc(), table.c.id.desc())
                        .limit(203)
                    )
                )
                .mappings()
                .all()
            )
        if len(local) > 202:
            raise FeedUnavailable("strategy candle receipt budget exceeded")
        candidates: dict[datetime, tuple[Candle, UUID]] = {}
        lower = end - timedelta(minutes=chosen.interval_minutes * 201)
        for row in (*rest, *local):
            raw = (
                row["normalized"].get("candles")
                if row["channel"] == "rest:closed_candles"
                else [row["normalized"].get("candle")]
            )
            if not isinstance(raw, list) or len(raw) > 720:
                raise FeedUnavailable("invalid stored candle collection")
            for document in raw:
                bar = Candle.model_validate_json(json.dumps(document))
                if (
                    bar.symbol != chosen.symbol
                    or bar.interval_minutes != chosen.interval_minutes
                    or bar.venue != "kraken"
                ):
                    raise FeedUnavailable("stored candle binding mismatch")
                if not lower <= bar.start < end or bar.end > end:
                    continue
                if (
                    bar.observed_at > at
                    or bar.observed_at > row["received_at"]
                    or row["received_at"] < bar.end + timedelta(seconds=10)
                ):
                    raise FeedUnavailable("stored candle was unavailable at decision time")
                if not bar.complete or bar.quality:
                    continue
                previous = candidates.get(bar.end)
                if previous is not None:
                    # Provenance may differ, economics must agree. Do not silently
                    # choose between two conflicting complete observations.
                    fields = {"observed_at", "source"}
                    if previous[0].model_dump(exclude=fields) != bar.model_dump(exclude=fields):
                        raise FeedUnavailable("conflicting stored candle economics")
                else:
                    candidates[bar.end] = bar, row["id"]
        ordered = sorted(candidates.values(), key=lambda pair: pair[0].end)
        bars = tuple(pair[0] for pair in ordered)
        if (
            len(bars) < 200
            or bars[-1].end != end
            or any(a.end != b.start for a, b in pairwise(bars))
        ):
            raise FeedUnavailable("strategy warm-up missing, gapped or unfinished")
        return CandlePrefix(
            bars,
            tuple((source, fingerprint(bar.model_dump(mode="json"))) for bar, source in ordered),
        )
