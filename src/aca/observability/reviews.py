"""Explicit operator assertions about lost notifications; never delivery authority."""

import json
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, Uuid, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aca.domain.trading import fingerprint
from aca.domain.values import UtcDateTime, utc_datetime
from aca.observability.records import DeliveryState, history
from aca.operations.config import StrictModel
from aca.storage.paper import accounts

reviews = Table(
    "paper_notification_reviews",
    MetaData(schema="aca"),
    Column("account_id", Uuid, primary_key=True),
    Column("failure_number", BigInteger, primary_key=True),
    Column("failure_kind", String(32)),
    Column("failure_hash", String(64)),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("document", JSONB),
    Column("fingerprint", String(64)),
)
MAX_REVIEWS = 10_000


class ReviewRequest(StrictModel):
    failure_number: int = Field(ge=1)
    failure_hash: str = Field(pattern="^[a-f0-9]{64}$")
    actor: UUID
    resolution: Literal["manual_notification_confirmed", "delivery_loss_accepted"]
    evidence_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class ReviewRecord(ReviewRequest):
    schema_version: Literal[1] = 1
    account_id: UUID
    failure_kind: Literal["abandoned", "dead_letter"]
    reviewed_at: UtcDateTime


def failure_state(row: Mapping[str, object]) -> DeliveryState:
    state = DeliveryState.model_validate_json(json.dumps(row["document"]))
    if (
        row["kind"] not in {"abandoned", "dead_letter"}
        or state.account_id != row["account_id"]
        or fingerprint(
            {
                "number": row["number"],
                "at": utc_datetime(row["at"]).isoformat(),
                "kind": row["kind"],
                "previous_hash": row["previous_hash"],
                "state": row["document"],
            }
        )
        != row["fingerprint"]
    ):
        raise ValueError("terminal notification evidence is invalid")
    return state


def review_record(row: Mapping[str, object]) -> ReviewRecord:
    record = ReviewRecord.model_validate_json(json.dumps(row["document"]))
    if (
        record.account_id != row["account_id"]
        or record.failure_number != row["failure_number"]
        or record.failure_hash != row["failure_hash"]
        or record.failure_kind != row["failure_kind"]
        or record.reviewed_at != row["reviewed_at"]
        or fingerprint(row["document"]) != row["fingerprint"]
    ):
        raise ValueError("notification review evidence is invalid")
    return record


async def reviewed_count(c: AsyncConnection, account_id: UUID) -> int:
    # Fail closed on corrupt/unknown review contracts instead of trusting a bare
    # COUNT. Explicit budget bounds this full retained review-prefix audit.
    rows = (
        (
            await c.execute(
                select(reviews)
                .where(reviews.c.account_id == account_id)
                .order_by(reviews.c.failure_number)
                .limit(MAX_REVIEWS + 1)
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > MAX_REVIEWS:
        raise ValueError("notification review audit exceeds budget")
    for row in rows:
        review_record(dict(row))
    return len(rows)


async def review_failure(
    c: AsyncConnection,
    account_id: UUID,
    request: ReviewRequest,
) -> dict[str, object]:
    if not await c.scalar(select(accounts.c.id).where(accounts.c.id == account_id)):
        raise ValueError("paper account does not exist")
    row = (
        (
            await c.execute(
                select(history).where(
                    history.c.account_id == account_id,
                    history.c.number == request.failure_number,
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ValueError("terminal notification event does not exist")
    failure_state(dict(row))
    if row["fingerprint"] != request.failure_hash:
        raise ValueError("terminal notification hash does not match review")
    existing = (
        (
            await c.execute(
                select(reviews).where(
                    reviews.c.account_id == account_id,
                    reviews.c.failure_number == request.failure_number,
                )
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        record = review_record(dict(existing))
        if (
            ReviewRequest.model_validate(record.model_dump(include=set(ReviewRequest.model_fields)))
            != request
        ):
            raise ValueError("terminal notification already has a different review")
        inserted = False
    else:
        if await reviewed_count(c, account_id) >= MAX_REVIEWS:
            raise ValueError("notification review audit exceeds budget")
        at = utc_datetime(await c.scalar(select(func.clock_timestamp())))
        if at < utc_datetime(row["at"]):
            raise ValueError("notification review clock precedes failure evidence")
        record = ReviewRecord(
            **request.model_dump(),
            account_id=account_id,
            failure_kind=row["kind"],
            reviewed_at=at,
        )
        doc = record.model_dump(mode="json")
        await c.execute(
            reviews.insert().values(
                account_id=account_id,
                failure_number=request.failure_number,
                failure_kind=record.failure_kind,
                failure_hash=request.failure_hash,
                reviewed_at=at,
                document=doc,
                fingerprint=fingerprint(doc),
            )
        )
        inserted = True
    return {
        "schema_version": 1,
        "mode": "paper",
        "account_id": str(account_id),
        "review": record.model_dump(mode="json"),
        "inserted": inserted,
        "delivery_state_changed": False,
        "message_resent": False,
        "promotion_evidence": False,
        "live_execution_available": False,
    }


async def read_failures(
    engine: AsyncEngine,
    account_id: UUID,
    *,
    after_number: int = 0,
    limit: int = 20,
) -> dict[str, object]:
    if (
        type(after_number) is not int
        or after_number < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("failure query exceeds budget")
    async with engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        if not await c.scalar(select(accounts.c.id).where(accounts.c.id == account_id)):
            raise ValueError("paper account does not exist")
        rows = (
            (
                await c.execute(
                    select(history)
                    .where(
                        history.c.account_id == account_id,
                        history.c.kind.in_(["abandoned", "dead_letter"]),
                        history.c.number > after_number,
                    )
                    .order_by(history.c.number)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        items = []
        for row in rows[:limit]:
            state = failure_state(dict(row))
            review = (
                (
                    await c.execute(
                        select(reviews).where(
                            reviews.c.account_id == account_id,
                            reviews.c.failure_number == row["number"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            items.append(
                {
                    "failure_number": row["number"],
                    "failure_hash": row["fingerprint"],
                    "at": utc_datetime(row["at"]).isoformat(),
                    "kind": row["kind"],
                    "alert_id": str(state.alert_id) if state.alert_id else None,
                    "notice_id": str(state.notice_id) if state.notice_id else None,
                    "http_status": state.http_status,
                    "review": review_record(dict(review)).model_dump(mode="json")
                    if review
                    else None,
                }
            )
        as_of = utc_datetime(await c.scalar(select(func.clock_timestamp())))
    return {
        "schema_version": 1,
        "mode": "paper",
        "account_id": str(account_id),
        "as_of": as_of.isoformat(),
        "items": items,
        "next_after_number": rows[limit - 1]["number"] if len(rows) > limit else None,
        "promotion_evidence": False,
        "live_execution_available": False,
    }
