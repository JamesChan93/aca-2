"""Atomic raw evidence plus canonical public trade insertion; no account state."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from aca.adapters.kraken.wire import MarketDataError
from aca.domain.market import MarketTrade
from aca.domain.trading import fingerprint
from aca.domain.values import utc_datetime
from aca.storage.models import MarketReceipt, PublicTradeRecord
from aca.storage.replay import PUBLIC_REPLAY_LOCK, public_events


class MarketStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        timestamp_compatible: Callable[[datetime, datetime, str, str, str], bool] | None = None,
    ) -> None:
        self.engine = engine
        self.timestamp_compatible = timestamp_compatible
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def append(
        self,
        *,
        epoch: UUID,
        channel: str,
        payload: str,
        received_at: datetime,
        normalized: dict[str, Any] | None = None,
        trades: tuple[MarketTrade, ...] = (),
        valid: bool = True,
        reason: str | None = None,
    ) -> tuple[MarketTrade, ...]:
        receipt_id = uuid4()
        added: list[MarketTrade] = []
        alignments: list[dict[str, str]] = []
        if len(payload.encode()) > 2_000_000:
            raise MarketDataError("market payload exceeds storage bound")
        async with self.sessions.begin() as session:
            # Allocate the sequence only under a transaction lock held through
            # commit. A higher committed number can never hide an earlier
            # uncommitted receipt; rolled-back numbers may leave harmless gaps.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": PUBLIC_REPLAY_LOCK}
            )
            session.add(
                MarketReceipt(
                    id=receipt_id,
                    schema_version=1,
                    epoch=epoch,
                    received_at=utc_datetime(received_at),
                    channel=channel,
                    valid=valid,
                    reason=reason,
                    payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
                    payload=payload,
                    normalized=normalized or {},
                )
            )
            await session.flush()
            for trade in trades:
                inserted = await session.scalar(
                    insert(PublicTradeRecord)
                    .values(
                        venue=trade.venue,
                        symbol=trade.symbol,
                        trade_id=trade.trade_id,
                        receipt_id=receipt_id,
                        event_at=trade.event_at,
                        received_at=trade.received_at,
                        side=trade.side,
                        price=trade.price,
                        quantity=trade.quantity,
                    )
                    .on_conflict_do_nothing()
                    .returning(PublicTradeRecord.trade_id)
                )
                if inserted is not None:
                    added.append(trade)
                else:
                    existing = await session.get(
                        PublicTradeRecord, (trade.venue, trade.symbol, trade.trade_id)
                    )
                    if existing is None or (existing.price, existing.quantity, existing.side) != (
                        trade.price,
                        trade.quantity,
                        trade.side,
                    ):
                        raise MarketDataError("conflicting public trade identity")
                    if existing.event_at != trade.event_at:
                        prior = await session.get(MarketReceipt, existing.receipt_id)
                        if (
                            prior is None
                            or self.timestamp_compatible is None
                            or not self.timestamp_compatible(
                                existing.event_at,
                                trade.event_at,
                                prior.channel,
                                channel,
                                trade.venue,
                            )
                        ):
                            raise MarketDataError("conflicting public trade timestamp")
                        alignments.append(
                            {
                                "trade_id": trade.trade_id,
                                "symbol": trade.symbol,
                                "prior_receipt_id": str(prior.id),
                                "current_receipt_id": str(receipt_id),
                                "prior_event_at": existing.event_at.isoformat(),
                                "current_event_at": trade.event_at.isoformat(),
                            }
                        )
            if alignments:
                session.add(
                    MarketReceipt(
                        id=uuid4(),
                        schema_version=1,
                        epoch=epoch,
                        received_at=utc_datetime(received_at),
                        channel="timestamp_alignment",
                        valid=True,
                        reason=None,
                        payload="{}",
                        payload_hash=hashlib.sha256(b"{}").hexdigest(),
                        normalized={
                            "policy": "kraken-public-cross-protocol-1us-v1",
                            "alignments": alignments,
                        },
                    )
                )
            replay_channels = {
                "epoch_started",
                "instrument_rules",
                "capture_health",
                "status",
                "book",
                "trade",
                "capture_fault",
                "capture_stopped",
                "quarantine",
                "coverage_gap",
            }
            replay_document = {
                "schema_version": 1,
                "receipt_id": str(receipt_id),
                "epoch": str(epoch),
                "received_at": utc_datetime(received_at).isoformat(),
                "channel": channel,
                "valid": valid,
                "payload_hash": hashlib.sha256(payload.encode()).hexdigest(),
                "normalized": (normalized or {}) if channel in replay_channels else {},
                # Only first-seen updates, in original frame order. Historical
                # snapshots/REST recovery never imply executable liquidity.
                "trades": [t.model_dump(mode="json") for t in added]
                if valid and channel == "trade" and (normalized or {}).get("snapshot") is False
                else [],
            }
            if len(json.dumps(replay_document).encode()) > 2_000_000:
                raise MarketDataError("public replay envelope exceeds storage bound")
            await session.execute(
                public_events.insert().values(
                    receipt_id=receipt_id,
                    document=replay_document,
                    fingerprint=fingerprint(replay_document),
                )
            )
        return tuple(added)

    async def check_capacity(self) -> None:
        async with self.engine.connect() as connection:
            size = await connection.scalar(text("SELECT pg_database_size(current_database())"))
            if size is None or size >= 2_000_000_000:
                raise MarketDataError("public capture database budget exhausted")

    async def status(self) -> dict[str, object]:
        async with self.sessions() as session:
            last = await session.scalar(
                select(MarketReceipt)
                .order_by(MarketReceipt.received_at.desc(), MarketReceipt.id.desc())
                .limit(1)
            )
            trades = await session.scalar(select(func.count()).select_from(PublicTradeRecord))
            faults = await session.scalar(
                select(func.count())
                .select_from(MarketReceipt)
                .where(MarketReceipt.valid.is_(False))
            )
        return {
            "schema_version": 1,
            "as_of": datetime.now(UTC).isoformat(),
            "last_receipt_at": last.received_at.isoformat() if last else None,
            "last_channel": last.channel if last else None,
            "last_epoch": str(last.epoch) if last else None,
            "canonical_trade_count": trades,
            "fault_receipt_count": faults,
            "trading_ready": False,
            "live_execution_available": False,
            # A recent historical receipt alone is never evidence of current feed health.
            "market_ready": False,
        }

    async def evidence(self, *, channel: str, limit: int = 5) -> dict[str, object]:
        if (
            channel
            not in {
                "instrument_rules",
                "rest:closed_candles",
                "local:closed_candle",
                "trade_recovery",
                "capture_fault",
                "coverage_gap",
            }
            or not 1 <= limit <= 20
        ):
            raise ValueError("unsupported evidence query")
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(MarketReceipt)
                    .where(MarketReceipt.channel == channel)
                    .order_by(MarketReceipt.received_at.desc(), MarketReceipt.id.desc())
                    .limit(limit)
                )
            ).all()
        return {
            "schema_version": 1,
            "as_of": datetime.now(UTC).isoformat(),
            "items": [
                {
                    "id": str(row.id),
                    "epoch": str(row.epoch),
                    "received_at": row.received_at.isoformat(),
                    "channel": row.channel,
                    "valid": row.valid,
                    "reason": row.reason,
                    "payload_sha256": row.payload_hash,
                    "data": row.normalized,
                }
                for row in rows
            ],
        }
