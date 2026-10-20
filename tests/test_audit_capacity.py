"""Batched audits retain coverage/anchor checks and validate report suffixes."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from test_stored_replay import AT, Feed

from aca.observability.notices import daily_summary
from aca.reporting.historical import historical_performance
from aca.simulation.historical import AUDIT_PAGE
from aca.simulation.stored_replay import StoredReplay
from aca.storage.replay import public_events

pytest_plugins = ("test_stored_replay",)
pytestmark = pytest.mark.integration


async def prefix(runner: StoredReplay, feed: Feed, count: int) -> None:
    for index in range(count):
        at = AT + timedelta(microseconds=index)
        await feed.marker("heartbeat", at)
        await runner.batch(at=at)


async def test_audit_source_queries_scale_by_page_not_observation(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await prefix(runner, feed, 2 * AUDIT_PAGE + 12)
    queries: list[str] = []
    financial_heads: list[str] = []

    def capture(c: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FROM aca.public_replay_events" in statement:
            queries.append(statement)
        if "max(aca.paper_events.version)" in statement:
            financial_heads.append(statement)

    event.listen(runner.store.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await runner.audit()
        assert len(queries) <= 6  # Three cursor pages and three valuation pages.
        queries.clear()
        result = await historical_performance(runner.store)
        assert result["period"]["observations"] == 2 * AUDIT_PAGE + 18  # type: ignore[index]
        assert len(queries) <= 4  # Three anchor pages plus the unread-source count.
        queries.clear()
        financial_heads.clear()
        async with runner.store.engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            summary = await daily_summary(c, runner.store, AT.date())
        observed = summary["observed"]
        assert isinstance(observed, dict) and observed["report_hash"] == result["report_hash"]
        assert len(queries) <= 4
        assert len(financial_heads) == 1
    finally:
        event.remove(runner.store.engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("corrupt", ["skip_source", "anchor"])
async def test_report_with_old_endpoint_still_rejects_rehashed_corruption_on_next_page(
    replay: tuple[StoredReplay, Feed], corrupt: str
) -> None:
    runner, feed = replay
    # Baseline and five bootstrap observations fill the remainder of the page.
    await prefix(runner, feed, AUDIT_PAGE - 6)
    async with runner.service.writer.transaction() as c:
        head = await runner.history.latest(c)
        cursor = await runner._latest(c)
        assert head is not None and cursor is not None
        value = runner.history.restore(head)
    old_endpoint = value.number
    at = AT + timedelta(milliseconds=1)
    await feed.marker("heartbeat", at)
    await feed.marker("heartbeat", at + timedelta(microseconds=1))
    async with runner.service.writer.transaction() as c:
        sources = (
            (
                await c.execute(
                    select(public_events)
                    .where(public_events.c.number > old_endpoint)
                    .order_by(public_events.c.number)
                )
            )
            .mappings()
            .all()
        )
        assert len(sources) == 2
        previous = head["fingerprint"]
        for index, source in enumerate(sources):
            if corrupt == "skip_source" and index == 0:
                continue
            changed = value.model_copy(
                update={
                    "number": source["number"],
                    "source_hash": "f" * 64 if index == 0 else source["fingerprint"],
                    "source_received_at": at,
                }
            )
            previous = await runner.history.save(c, changed, previous)
        last = sources[-1]
        context = runner._cursor(cursor).model_copy(
            update={"number": last["number"], "source_hash": last["fingerprint"]}
        )
        await runner._save(c, context, cursor["fingerprint"])
    expected = "coverage gap" if corrupt == "skip_source" else "anchor mismatch"
    with pytest.raises(ValueError, match=expected):
        await runner.audit()
    with pytest.raises(ValueError, match=expected):
        await historical_performance(runner.store, end_source=old_endpoint)
    # Day selection, even a day with no observations, cannot hide corruption.
    for day in (AT.date(), (AT - timedelta(days=1)).date()):
        async with runner.store.engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            with pytest.raises(ValueError, match=expected):
                await daily_summary(c, runner.store, day)


async def test_cursor_anchor_error_cannot_hide_behind_a_later_valid_anchor(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await feed.marker("heartbeat", AT + timedelta(milliseconds=1))
    await feed.marker("heartbeat", AT + timedelta(milliseconds=2))
    async with runner.service.writer.transaction() as c:
        row = await runner._latest(c)
        assert row is not None
        current = runner._cursor(row)
        sources = (
            (
                await c.execute(
                    select(public_events)
                    .where(public_events.c.number > current.number)
                    .order_by(public_events.c.number)
                )
            )
            .mappings()
            .all()
        )
        assert len(sources) == 2
        # A valid later cursor cannot legitimize an intermediate cursor whose
        # hash disagrees with the actual source.
        invalid = current.model_copy(
            update={"number": sources[0]["number"], "source_hash": "f" * 64}
        )
        await runner._save(c, invalid, row["fingerprint"])
        middle = await runner._latest(c)
        assert middle is not None
        valid = current.model_copy(
            update={"number": sources[1]["number"], "source_hash": sources[1]["fingerprint"]}
        )
        await runner._save(c, valid, middle["fingerprint"])
    with pytest.raises(ValueError, match="cursor source anchor mismatch"):
        await runner.audit()


async def test_historical_audit_rejects_missing_suffix_after_exhausting_pages(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await prefix(runner, feed, AUDIT_PAGE - 6)
    current = await runner.audit()
    async with runner.service.writer.transaction() as c:
        with pytest.raises(ValueError, match="baseline/cursor missing"):
            await runner.history.audit(c, current.number + 1)
