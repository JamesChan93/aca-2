"""One independent notification owner per account; append before every HTTP attempt."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from time import monotonic
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aca.domain.trading import fingerprint
from aca.domain.values import utc_datetime
from aca.observability.discord import DiscordSender, Webhook
from aca.observability.notices import (
    critical,
    daily_summary,
    notice_payload,
    notices,
    receipts,
    summary_day,
)
from aca.observability.records import MAX_ATTEMPTS, DeliveryState
from aca.observability.records import history as history
from aca.observability.reviews import ReviewRequest, review_failure, reviewed_count
from aca.storage.paper import PaperStore, accounts, events, outbox


def payload(row: dict[str, object], version: int) -> dict[str, object]:
    document = row["document"]
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
    ):
        raise ValueError("unknown alert schema")
    if document.get("mode") != "paper" or document.get("live_execution_available") is not False:
        raise ValueError("unsupported alert mode")
    if (
        document.get("account_id") != str(row["account_id"])
        or type(document.get("version")) is not int
        or document.get("version") != version
        or document.get("category") != row["category"]
        or type(document.get("hard_halt")) is not bool
    ):
        raise ValueError("alert identity does not match its event")
    # Render only reviewed fields. Incident descriptions and other raw strings never leave storage.
    category = str(row["category"])
    categories = {
        "cash_flow",
        "tax_reserve_set",
        "valuation",
        "risk_rejected",
        "dispatch_denied",
        "definitive_rejection",
        "unknown_submission",
        "confirmed_fill",
        "reconciliation_incomplete",
        "reconciliation_failed",
        "recovering",
        "unknown_cancellation",
        "hard_halt",
        "soft_halt",
        "manual_epoch_review",
        "residual_quarantined",
        "closing_reserved",
        "historical_risk",
        "paper_supervisor_stopped",
        "paper_feed_unavailable",
        "paper_clock_invalid",
        "paper_state_unavailable",
    }
    safe_category = category if category in categories else "paper_state_changed"
    severity = (
        "CRITICAL"
        if document.get("hard_halt") is True
        or category
        in {
            "unknown_submission",
            "unknown_cancellation",
            "residual_quarantined",
            "reconciliation_failed",
        }
        else "INFO"
    )
    if severity != "CRITICAL" and (
        document.get("incident") is not None
        or category
        in {"soft_halt", "risk_rejected", "dispatch_denied", "reconciliation_incomplete"}
    ):
        severity = "WARNING"
    content = (
        f"ACA PAPER | {severity} | {safe_category}\n"
        f"Account: {UUID(str(row['account_id']))}\n"
        f"Alert: {UUID(str(row['id']))}\nEvent: {UUID(str(row['event_id']))}\n"
        f"Paper state version: {version}\nLive trading: disabled"
    )
    return {"content": content, "allowed_mentions": {"parse": []}}


class NotificationOwner:
    def __init__(self, engine: AsyncEngine, account_id: UUID) -> None:
        self.engine = engine
        self.account_id = account_id
        self.connection: AsyncConnection | None = None
        self.pid: int | None = None
        self.mutex = asyncio.Lock()
        self.operation = asyncio.Lock()

    async def __aenter__(self) -> Self:
        c = await self.engine.connect()
        try:
            await c.execution_options(isolation_level="REPEATABLE READ")
            if not await c.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:scope, 0))"),
                {"scope": f"aca:notifications:{self.account_id}"},
            ):
                raise RuntimeError("another notification owner is active")
            self.pid = await c.scalar(text("SELECT pg_backend_pid()"))
            await c.commit()
            self.connection = c
            return self
        except BaseException:
            await c.invalidate()
            await c.close()
            raise

    async def __aexit__(self, *args: object) -> None:
        if self.connection is not None:
            await self.connection.invalidate()
            await self.connection.close()
        self.connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.mutex:
            c = self.connection
            if c is None or c.closed or c.invalidated:
                raise RuntimeError("notification owner connection unavailable")
            async with c.begin():
                if await c.scalar(text("SELECT pg_backend_pid()")) != self.pid:
                    raise RuntimeError("notification owner connection changed")
                yield c

    async def _head(self, c: AsyncConnection) -> tuple[int, str, DeliveryState | None]:
        row = (
            (
                await c.execute(
                    select(history)
                    .where(history.c.account_id == self.account_id)
                    .order_by(history.c.number.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return 0, "0" * 64, None
        state = DeliveryState.model_validate_json(json.dumps(row["document"]))
        if (
            state.account_id != self.account_id
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
            raise ValueError("notification head evidence is invalid")
        return row["number"], row["fingerprint"], state

    async def _append(
        self, c: AsyncConnection, state: DeliveryState, at: datetime, kind: str
    ) -> None:
        number, previous, _ = await self._head(c)
        doc = state.model_dump(mode="json")
        hashed = fingerprint(
            {
                "number": number + 1,
                "at": at.isoformat(),
                "kind": kind,
                "previous_hash": previous,
                "state": doc,
            }
        )
        await c.execute(
            history.insert().values(
                account_id=self.account_id,
                number=number + 1,
                at=at,
                kind=kind,
                previous_hash=previous,
                fingerprint=hashed,
                document=doc,
            )
        )

    async def configure(
        self, webhook: Webhook, at: datetime, *, after_version: int | None = None
    ) -> None:
        async with self.operation:
            await self._configure(webhook, at, after_version=after_version)

    async def _configure(
        self, webhook: Webhook, at: datetime, *, after_version: int | None = None
    ) -> None:
        at = utc_datetime(at)
        async with self.transaction() as c:
            if not await c.scalar(select(accounts.c.id).where(accounts.c.id == self.account_id)):
                raise ValueError("paper account does not exist")
            _, _, old = await self._head(c)
            if old is not None and after_version is not None:
                raise ValueError("a configured notification cursor cannot be skipped or rewound")
            head = (
                await c.scalar(
                    select(func.max(events.c.version)).where(events.c.account_id == self.account_id)
                )
                or 0
            )
            cursor = old.cursor if old else (0 if after_version is None else after_version)
            if type(cursor) is not int or not 0 <= cursor <= head:
                raise ValueError("notification starting version is outside account history")
            state = DeliveryState(
                account_id=self.account_id,
                destination_hash=webhook.fingerprint,
                cursor=cursor,
                not_before=max(at, old.not_before) if old else at,
                notice_id=old.notice_id if old and old.attempts else None,
                alert_id=old.alert_id if old and old.attempts else None,
            )
            await self._append(c, state, at, "operator_configured")

    async def review(self, request: ReviewRequest) -> dict[str, object]:
        async with self.operation, self.transaction() as c:
            # Same per-account owner as delivery. A review never appends delivery
            # state or invokes HTTP, and cannot overlap an in-flight local tick.
            await self._head(c)
            return await review_failure(c, self.account_id, request)

    async def _notice(
        self, c: AsyncConnection, kind: str, key: str, document: dict[str, object], at: datetime
    ) -> None:
        doc = {
            "schema_version": 1,
            "account_id": str(self.account_id),
            "kind": kind,
            "live_execution_available": False,
            **document,
        }
        await c.execute(
            notices.insert().values(
                id=uuid4(),
                account_id=self.account_id,
                kind=kind,
                key=key,
                created_at=at,
                document=doc,
                fingerprint=fingerprint(doc),
            )
        )

    async def prepare(self, at: datetime, *, include_daily: bool = True) -> dict[str, object]:
        """No HTTP. Schedule the latest eligible day and current critical episode only."""
        at = utc_datetime(at)
        async with self.operation:
            async with self.transaction() as c:
                state = await PaperStore(self.engine, self.account_id).load(c)
                if state is None:
                    raise ValueError("paper account does not exist")
                active = critical(state)
                if active:
                    episode, reasons = active
                    latest = (
                        (
                            await c.execute(
                                select(notices)
                                .where(
                                    notices.c.account_id == self.account_id,
                                    notices.c.kind == "critical",
                                    notices.c.document["episode"].as_string() == episode,
                                )
                                .order_by(notices.c.created_at.desc())
                                .limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    finished_at = (
                        await c.scalar(
                            select(receipts.c.at).where(receipts.c.notice_id == latest["id"])
                        )
                        if latest is not None
                        else None
                    )
                    pending = latest is not None and finished_at is None
                    if not pending and (
                        latest is None
                        or (finished_at is not None and at >= finished_at + timedelta(minutes=5))
                    ):
                        await self._notice(
                            c,
                            "critical",
                            f"critical:{episode}:{at.isoformat()}",
                            {
                                "episode": episode,
                                "reasons": list(reasons),
                                "source_version": state.version,
                            },
                            at,
                        )
                day = summary_day(at)
                key = f"daily:{day.isoformat()}"
                exists = await c.scalar(
                    select(notices.c.id).where(
                        notices.c.account_id == self.account_id, notices.c.key == key
                    )
                )
                created = await c.scalar(
                    select(accounts.c.created_at).where(accounts.c.id == self.account_id)
                )
            scheduled = False
            unavailable = False
            if include_daily and exists is None and created is not None and day >= created.date():
                # Separate read-only snapshot. Never hold the owner transaction during a report.
                async with self.engine.connect() as c:
                    await c.execution_options(
                        isolation_level="REPEATABLE READ", postgresql_readonly=True
                    )
                    try:
                        async with asyncio.timeout(15):
                            summary = await daily_summary(
                                c, PaperStore(self.engine, self.account_id), day
                            )
                    except ValueError, TimeoutError:
                        unavailable = True
                if not unavailable:
                    async with self.transaction() as c:
                        await self._notice(c, "daily", key, {"summary": summary}, at)
                    scheduled = True
            return {
                "daily_scheduled": scheduled,
                "eligible_day": day.isoformat(),
                "daily_evidence_unavailable": unavailable,
                "critical_active": active is not None,
                "live_execution_available": False,
            }

    async def tick(self, sender: DiscordSender, at: datetime) -> str:
        async with self.operation:
            return await self._tick(sender, at)

    async def _tick(self, sender: DiscordSender, at: datetime) -> str:
        at = utc_datetime(at)
        async with self.transaction() as c:
            _, _, state = await self._head(c)
            if state is None or state.destination_hash != sender.webhook.fingerprint:
                raise ValueError("notification destination is not configured for this secret")
            if not state.enabled:
                return "disabled"
            if at < state.not_before:
                return "waiting"
            notice = None
            if state.notice_id is not None and state.attempts:
                notice = (
                    (
                        await c.execute(
                            select(notices).where(
                                notices.c.account_id == self.account_id,
                                notices.c.id == state.notice_id,
                                ~select(receipts.c.notice_id)
                                .where(receipts.c.notice_id == notices.c.id)
                                .exists(),
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            elif not state.attempts:
                notice = (
                    (
                        await c.execute(
                            select(notices)
                            .where(
                                notices.c.account_id == self.account_id,
                                ~select(receipts.c.notice_id)
                                .where(receipts.c.notice_id == notices.c.id)
                                .exists(),
                            )
                            .order_by(
                                (notices.c.kind == "critical").desc(),
                                notices.c.created_at,
                                notices.c.id,
                            )
                            .limit(1)
                        )
                    )
                    .mappings()
                    .first()
                )
            row = (
                (
                    await c.execute(
                        select(outbox, events.c.version)
                        .join(events, events.c.id == outbox.c.event_id)
                        .where(
                            outbox.c.account_id == self.account_id,
                            events.c.account_id == self.account_id,
                            events.c.version > state.cursor,
                        )
                        .order_by(events.c.version)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if notice is not None:
                ident, version = notice["id"], state.cursor
            elif row is not None:
                ident, version = row["id"], row["version"]
                if state.attempts and state.alert_id != ident:
                    raise ValueError("in-flight alert identity changed")
            else:
                return "idle"
            if notice is not None and fingerprint(notice["document"]) != notice["fingerprint"]:
                raise ValueError("scheduled notice evidence is invalid")
            if notice is not None and notice["kind"] == "critical":
                current = await PaperStore(self.engine, self.account_id).load(c)
                active = critical(current) if current is not None else None
                if active is None or active[0] != notice["document"].get("episode"):
                    await c.execute(
                        receipts.insert().values(
                            notice_id=ident, account_id=self.account_id, at=at, outcome="superseded"
                        )
                    )
                    cleared = state.model_copy(
                        update={"attempts": 0, "notice_id": ident, "outcome": "superseded"}
                    )
                    await self._append(c, cleared, at, "superseded")
                    return "superseded"
            try:
                message = (
                    notice_payload(notice["document"], ident, self.account_id)
                    if notice is not None
                    else payload(dict(row) if row is not None else {}, version)
                )
            except ValueError, KeyError, TypeError:
                message = None
            if state.attempts >= MAX_ATTEMPTS or message is None:
                state = state.model_copy(
                    update={
                        "cursor": version,
                        "attempts": 0,
                        "outcome": "dead_letter",
                        "alert_id": None if notice is not None else ident,
                        "notice_id": ident if notice is not None else None,
                        "http_status": None,
                        "message_id": None,
                        "not_before": at + timedelta(seconds=1),
                    }
                )
                await self._append(c, state, at, "dead_letter")
                if notice is not None:
                    await c.execute(
                        receipts.insert().values(
                            notice_id=ident,
                            account_id=self.account_id,
                            at=at,
                            outcome="dead_letter",
                        )
                    )
                return "dead_letter"
            attempt = state.attempts + 1
            backoff = timedelta(
                seconds=min(300, 5 * 2 ** (attempt - 1)), milliseconds=ident.int % 1000
            )
            started = state.model_copy(
                update={
                    "attempts": attempt,
                    "not_before": at + max(timedelta(seconds=30), backoff),
                    "outcome": "in_flight",
                    "alert_id": None if notice is not None else ident,
                    "notice_id": ident if notice is not None else None,
                    "http_status": None,
                    "message_id": None,
                }
            )
            await self._append(c, started, at, "attempt_started")
        # The attempt is committed before HTTP; never hold a transaction over the request.
        began = monotonic()
        result = await sender.send(message, at)
        completed_at = at + timedelta(seconds=max(0, monotonic() - began))
        async with self.transaction() as c:
            _, _, latest = await self._head(c)
            if latest != started:
                raise RuntimeError("notification attempt ownership changed")
            completed = started.model_copy(
                update={
                    "enabled": result.outcome != "disabled",
                    "cursor": version
                    if result.outcome in {"delivered", "abandoned"}
                    else state.cursor,
                    "attempts": 0 if result.outcome in {"delivered", "abandoned"} else attempt,
                    "not_before": completed_at
                    + max(
                        result.delay, backoff if result.outcome == "retry" else timedelta(seconds=1)
                    ),
                    "outcome": result.reason,
                    "http_status": result.status,
                    "message_id": result.message_id,
                }
            )
            await self._append(c, completed, completed_at, result.outcome)
            if notice is not None and result.outcome in {"delivered", "abandoned"}:
                await c.execute(
                    receipts.insert().values(
                        notice_id=ident,
                        account_id=self.account_id,
                        at=completed_at,
                        outcome=result.outcome,
                    )
                )
        return result.outcome


async def status(engine: AsyncEngine, account_id: UUID) -> dict[str, object]:
    owner = NotificationOwner(engine, account_id)
    async with engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        if not await c.scalar(select(accounts.c.id).where(accounts.c.id == account_id)):
            raise ValueError("paper account does not exist")
        _, _, state = await owner._head(c)
        pending = (
            select(outbox.c.at)
            .join(events, events.c.id == outbox.c.event_id)
            .where(
                outbox.c.account_id == account_id, events.c.version > (state.cursor if state else 0)
            )
            .subquery()
        )
        backlog, oldest = (await c.execute(select(func.count(), func.min(pending.c.at)))).one()
        delivered, retries, failures, last_delivery = (
            await c.execute(
                select(
                    func.count().filter(history.c.kind == "delivered"),
                    func.count().filter(history.c.kind == "retry"),
                    func.count().filter(history.c.kind.in_(["dead_letter", "abandoned"])),
                    func.max(history.c.at).filter(history.c.kind == "delivered"),
                ).where(history.c.account_id == account_id)
            )
        ).one()
        reviewed = await reviewed_count(c, account_id)
        if reviewed > failures:
            raise ValueError("notification review counts are invalid")
        scheduled = (
            await c.execute(
                select(notices.c.kind, func.count(), func.min(notices.c.created_at))
                .where(
                    notices.c.account_id == account_id,
                    ~select(receipts.c.notice_id)
                    .where(receipts.c.notice_id == notices.c.id)
                    .exists(),
                )
                .group_by(notices.c.kind)
            )
        ).all()
        queues = {
            kind: {"count": count, "oldest_at": utc_datetime(at).isoformat()}
            for kind, count, at in scheduled
        }
        # Database wall time is evidence time; local monotonic time ages cached samples.
        as_of = utc_datetime(await c.scalar(select(func.clock_timestamp())))
    return {
        "schema_version": 1,
        "mode": "paper",
        "account_id": str(account_id),
        "as_of": as_of.isoformat(),
        "configured": state is not None,
        "state": state.model_dump(mode="json") if state else None,
        "backlog": backlog,
        "oldest_pending_at": utc_datetime(oldest).isoformat() if oldest else None,
        "terminal_failures": failures,
        "reviewed_terminal_failures": reviewed,
        "unreviewed_terminal_failures": failures - reviewed,
        "delivered": delivered,
        "retry_events": retries,
        "last_delivery_at": utc_datetime(last_delivery).isoformat() if last_delivery else None,
        "scheduled_backlog": sum(item["count"] for item in queues.values()),
        "scheduled_queues": queues,
        "live_execution_available": False,
    }


async def read_notices(engine: AsyncEngine, account_id: UUID, limit: int = 20) -> dict[str, object]:
    if not 1 <= limit <= 100:
        raise ValueError("notice query exceeds budget")
    async with engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        if not await c.scalar(select(accounts.c.id).where(accounts.c.id == account_id)):
            raise ValueError("paper account does not exist")
        rows = (
            (
                await c.execute(
                    select(notices, receipts.c.outcome)
                    .outerjoin(
                        receipts,
                        receipts.c.notice_id == notices.c.id,
                    )
                    .where(notices.c.account_id == account_id)
                    .order_by(
                        notices.c.created_at.desc(),
                        notices.c.id,
                    )
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        items = []
        for row in rows:
            if fingerprint(row["document"]) != row["fingerprint"]:
                raise ValueError("scheduled notice evidence is invalid")
            items.append(
                {
                    "id": str(row["id"]),
                    "created_at": utc_datetime(row["created_at"]).isoformat(),
                    "kind": row["kind"],
                    "outcome": row["outcome"],
                    "document": row["document"],
                    "preview": notice_payload(row["document"], row["id"], account_id),
                }
            )
    return {
        "schema_version": 1,
        "mode": "paper",
        "account_id": str(account_id),
        "items": items,
        "live_execution_available": False,
    }
