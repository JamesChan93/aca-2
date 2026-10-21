"""Online replay input: database anchors, complete file validation, then SQL tail.

No exported claim is used as a financial audit certificate. The report/writer
must still reconstruct economics against the complete retained financial history.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.domain.trading import fingerprint
from aca.reporting.replay_archive import Context, EvidenceValidator, Observation
from aca.simulation.historical import AUDIT_PAGE, HistoricalReplay, HistoricalValuation
from aca.storage.archive_format import ArchiveReader
from aca.storage.paper import events
from aca.storage.replay import cursors, public_events, valuations


async def _bind(c: AsyncConnection, history: HistoricalReplay, context: Context) -> None:
    store = history.store
    if context.account_id != store.account_id or context.policy_hash != history.policy_hash:
        raise ValueError("archive input account/policy mismatch")
    first = (
        await c.execute(
            select(valuations.c.number, valuations.c.fingerprint)
            .where(valuations.c.account_id == store.account_id)
            .order_by(valuations.c.number)
            .limit(1)
        )
    ).one_or_none()
    head = (
        await c.execute(
            select(valuations.c.fingerprint, cursors.c.fingerprint.label("cursor_hash"))
            .join(
                cursors,
                (cursors.c.account_id == valuations.c.account_id)
                & (cursors.c.number == valuations.c.number),
            )
            .where(
                valuations.c.account_id == store.account_id,
                valuations.c.number == context.head_number,
            )
        )
    ).one_or_none()
    financial = (
        (
            await c.execute(
                select(events.c.fingerprint, events.c.document).where(
                    events.c.account_id == store.account_id,
                    events.c.version == context.financial_version,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    state_hash = None
    if financial is not None:
        document = financial["document"]
        state_hash = document.get("state_hash") if "format" in document else fingerprint(document)
    if (
        first is None
        or first.number != context.baseline_number
        or head is None
        or head.fingerprint != context.head_valuation_hash
        or head.cursor_hash != context.head_cursor_hash
        or financial is None
        or financial["fingerprint"] != context.financial_hash
        or state_hash != context.financial_state_hash
    ):
        raise ValueError("archive input does not match retained database anchors")


async def archived_values(
    c: AsyncConnection, history: HistoricalReplay
) -> AsyncGenerator[tuple[Mapping[Any, Any], HistoricalValuation]]:
    """Exhaust before acceptance; no SQL suffix or writer command can precede EOF."""
    store, reference = history.store, history.store.replay_archive
    if reference is None:
        raise ValueError("explicit archive input required")
    reader = ArchiveReader(reference.path, reference.sha256)
    iterator = reader.rows()
    try:
        header = next(iterator, None)
        if header is None or set(header) != {"context"} or not isinstance(header["context"], dict):
            raise ValueError("archive input context missing")
        context = Context.restore(header["context"])
        await _bind(c, history, context)
        validator = EvidenceValidator(context)
        number = context.baseline_number - 1
        # Cache only small source anchors; never buffer a page of raw receipts.
        # Individual decoded segments retain the codec's 16 MB hard bound.
        sources: deque[tuple[int, str]] = deque()
        baseline_hash = await c.scalar(
            select(valuations.c.fingerprint).where(
                valuations.c.account_id == store.account_id,
                valuations.c.number == context.baseline_number,
            )
        )
        for document in iterator:
            observation = Observation.restore(document)
            value = validator.observe(observation)
            if value.number:
                if not sources:
                    sources.extend(
                        (int(row.number), str(row.fingerprint))
                        for row in (
                            await c.execute(
                                select(public_events.c.number, public_events.c.fingerprint)
                                .where(
                                    public_events.c.number >= max(1, number + 1),
                                    public_events.c.number <= context.head_number,
                                )
                                .order_by(public_events.c.number)
                                .limit(AUDIT_PAGE)
                            )
                        ).all()
                    )
                if not sources or sources.popleft() != (value.number, value.source_hash):
                    raise ValueError("archive input retained source coverage/anchor mismatch")
            if (
                number < context.baseline_number
                and observation.valuation.fingerprint != baseline_hash
            ):
                raise ValueError("archive input baseline anchor mismatch")
            number = value.number
            yield observation.valuation.model_dump(), value
            if validator.count % 64 == 0:
                await asyncio.sleep(0)  # Honour startup/report cancellation during file work.
        validator.finish()
        if (
            not reader.verified
            or reader.manifest is None
            or reader.manifest["context"] != {"header_hash": fingerprint(header)}
            or reader.identity is None
        ):
            raise ValueError("archive input not completely verified")
        reference.check_identity(reader.identity)
        store.archive_identity = reader.identity
    finally:
        iterator.close()
