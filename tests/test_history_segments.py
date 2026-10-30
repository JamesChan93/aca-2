"""Exact report/reconstruction equivalence after lossless operator compaction."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from test_stored_replay import AT, Feed

from aca.domain.trading import fingerprint
from aca.execution.paper import PaperExecution
from aca.observability.notices import daily_summary
from aca.operations.config import Settings
from aca.operations.history import compact_history
from aca.operations.provision import provision_roles
from aca.reporting.historical import historical_performance
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.database import create_engine
from aca.storage.history_blocks import blocks, certificates
from aca.storage.migrate import migrate
from aca.storage.paper import PaperStore, PaperWriter
from aca.storage.replay import cursors, public_events, valuations


def economic_summary(value: dict[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in value.items() if k != "report_hash"}
    if isinstance(result.get("observed"), dict):
        result["observed"] = {k: v for k, v in result["observed"].items() if k != "report_hash"}
    return result


pytestmark = pytest.mark.integration


@pytest.fixture
async def isolated_history_settings(database_settings: Settings) -> AsyncIterator[Settings]:
    """Compaction is intentionally limited to one nominated enrolled account."""
    name = "aca_history_" + uuid4().hex[:12]
    settings = database_settings.model_copy(
        update={"database": database_settings.database.model_copy(update={"name": name})}
    )
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        from pathlib import Path

        provision_roles(settings, Path(settings.database.runtime_password_file).parent / "admin")
        await migrate(settings)
        yield settings
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


async def seed(
    settings: Settings, count: int = 270
) -> tuple[PaperStore, dict[str, Any], dict[str, Any]]:
    store = PaperStore(create_engine(settings), uuid4())
    async with PaperWriter(store) as owner:
        venue = PaperExchange(store)
        service = PaperExecution(owner, venue)
        await service.initialise(AT)
        await service.cashflow("history-opening", Decimal(1000), AT)
        runner = StoredReplay(service, venue)
        await runner.enrol()
        feed = Feed(runner)
        await feed.bootstrap()
        for i in range(count):
            when = AT + timedelta(seconds=i + 1)
            if i in (100, 260):
                await service.cashflow(f"flow-{i}", Decimal(10 if i == 100 else -5), when)
            await service.mark((), when)
            state = await service._state()
            await service._save(state, state, at=when, kind="valuation")
            await feed.marker("heartbeat", when)
            await runner.batch(at=when)
    before = await historical_performance(store)
    async with store.engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        summary = await daily_summary(c, store, AT.date())
    return store, before, summary


async def compact(
    settings: Settings, store: PaperStore, report: dict[str, Any]
) -> tuple[PaperStore, dict[str, Any]]:
    result = await compact_history(
        settings,
        store.account_id,
        uuid4(),
        "Reviewed complete history and original evidence",
        report["financial_head_version"],
        report["historical_head_source"],
        previous_history=store.history_prefix,
    )
    return PaperStore(
        store.engine, store.account_id, history_prefix=UUID(result["certificate"]["id"])
    ), result


async def test_compaction_preserves_full_audit_and_report_arithmetic(
    isolated_history_settings: Settings,
) -> None:
    store, before, summary = await seed(isolated_history_settings)
    try:
        fast, result = await compact(isolated_history_settings, store, before)
        assert result["moved_rows"]["financial"] > 100
        assert result["moved_rows"]["valuation"] > 250
        assert result["journals_or_ids_removed"] == 0
        after = await historical_performance(fast)
        assert after["period"] == before["period"]
        assert (await historical_performance(store))["period"] == before["period"]
        async with store.engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            assert await fast.audit(c) == await store.audit(c, full=True)
            indexed_summary = await daily_summary(c, fast, AT.date())
            assert {
                k: v for k, v in indexed_summary.items() if k not in {"observed", "report_hash"}
            } == {k: v for k, v in summary.items() if k not in {"observed", "report_hash"}}
            assert {
                k: v
                for k, v in cast(dict[str, Any], indexed_summary["observed"]).items()
                if k != "report_hash"
            } == {k: v for k, v in summary["observed"].items() if k != "report_hash"}
        async with PaperWriter(fast) as owner:
            runner = StoredReplay(PaperExecution(owner, PaperExchange(fast)), PaperExchange(fast))
            assert (await runner.audit()).number == before["historical_head_source"]
        async with store.engine.connect() as c:
            for table in (cursors, valuations):
                assert await c.scalar(select(func.count()).select_from(table)) == 2
        for lower, upper in ((0, 1), (2, 100), (5, 259), (255, 264)):
            original = await historical_performance(store, start_source=lower, end_source=upper)
            indexed = await historical_performance(fast, start_source=lower, end_source=upper)
            assert original["period"] == indexed["period"]
    finally:
        await store.engine.dispose()


async def test_compaction_consumes_one_complete_audited_value_pass(
    isolated_history_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aca.simulation.historical import HistoricalReplay

    store, before, _ = await seed(isolated_history_settings, 20)
    original = HistoricalReplay.audited_values
    original_financial = PaperStore.audit
    passes, rows, financial_passes = 0, 0, 0

    async def counted(self: HistoricalReplay, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal passes, rows
        passes += 1
        async for item in original(self, *args, **kwargs):
            rows += 1
            yield item

    async def financial(self: PaperStore, *args: Any, **kwargs: Any) -> Any:
        nonlocal financial_passes
        financial_passes += 1
        return await original_financial(self, *args, **kwargs)

    try:
        monkeypatch.setattr(PaperStore, "audit", financial)
        monkeypatch.setattr(HistoricalReplay, "audited_values", counted)
        fast, _ = await compact(isolated_history_settings, store, before)
        monkeypatch.setattr(HistoricalReplay, "audited_values", original)
        monkeypatch.setattr(PaperStore, "audit", original_financial)
        assert passes == financial_passes == 1
        assert rows == before["period"]["observations"]
        assert (await historical_performance(fast))["period"] == before["period"]
        assert (await historical_performance(store))["period"] == before["period"]
    finally:
        await store.engine.dispose()


async def test_late_invalid_value_rolls_back_streamed_blocks_and_certificate(
    isolated_history_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, before, _ = await seed(isolated_history_settings)
    authority = create_engine(isolated_history_settings, "migration")
    original = AsyncConnection.execute
    wrote_block = False

    async def observe(self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal wrote_block
        if str(statement).startswith("INSERT INTO aca.paper_history_blocks"):
            wrote_block = True
        return await original(self, statement, *args, **kwargs)

    try:
        # Corrupt a late row, leaving the directly-read final head intact. The
        # complete pass must fail after its first bounded block has been staged.
        async with authority.begin() as c:
            number = await c.scalar(
                select(valuations.c.number)
                .where(valuations.c.account_id == store.account_id)
                .order_by(valuations.c.number)
                .offset(260)
                .limit(1)
            )
            assert number is not None
            await c.execute(
                text("ALTER TABLE aca.paper_replay_valuations DISABLE TRIGGER reject_mutation")
            )
            await c.execute(
                valuations.update()
                .where(valuations.c.account_id == store.account_id, valuations.c.number == number)
                .values(fingerprint="f" * 64)
            )
            await c.execute(
                text("ALTER TABLE aca.paper_replay_valuations ENABLE TRIGGER reject_mutation")
            )
        monkeypatch.setattr(AsyncConnection, "execute", observe)
        with pytest.raises(ValueError):
            await compact(isolated_history_settings, store, before)
        assert wrote_block
        from aca.storage.audit_prefix import prefixes

        async with store.engine.connect() as c:
            assert await c.scalar(select(func.count()).select_from(prefixes)) == 0
            assert await c.scalar(select(func.count()).select_from(blocks)) == 0
            assert await c.scalar(select(func.count()).select_from(certificates)) == 0
            assert (
                await c.scalar(
                    select(valuations.c.fingerprint).where(
                        valuations.c.account_id == store.account_id, valuations.c.number == number
                    )
                )
                == "f" * 64
            )  # Original evidence was neither repaired nor deleted.
    finally:
        await authority.dispose()
        await store.engine.dispose()


async def test_runtime_cannot_compact_or_issue_history_authority(
    isolated_history_settings: Settings,
) -> None:
    store, before, _ = await seed(isolated_history_settings, 2)
    try:
        fast, _ = await compact(isolated_history_settings, store, before)
        async with store.engine.begin() as c:
            with pytest.raises(DBAPIError):
                await c.execute(certificates.insert().values(id=uuid4()))
        async with store.engine.begin() as c:
            with pytest.raises(DBAPIError):
                await c.execute(blocks.delete())
        async with store.engine.begin() as c:
            with pytest.raises(DBAPIError):
                await c.execute(
                    text("ALTER TABLE aca.paper_events DISABLE TRIGGER reject_mutation")
                )
        assert (await historical_performance(fast))["period"] == before["period"]
    finally:
        await store.engine.dispose()


async def test_vacuum_has_a_bounded_offline_budget_without_changing_runtime_timeouts(
    isolated_history_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aca.operations.history import vacuum_history

    store, before, _ = await seed(isolated_history_settings, 2)
    original = AsyncConnection.execute
    vacuum_seen = False

    async def observe(self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal vacuum_seen
        if str(statement).startswith("VACUUM"):
            vacuum_seen = True
            assert await self.scalar(text("SHOW statement_timeout")) == "30s"
            assert await self.scalar(text("SELECT current_user")) == "aca_migrator"
        return await original(self, statement, *args, **kwargs)

    authority = create_engine(isolated_history_settings, "migration")
    try:
        async with PaperWriter(store):
            with pytest.raises(ValueError, match="stop collector"):
                await vacuum_history(isolated_history_settings, store.account_id)
        monkeypatch.setattr(AsyncConnection, "execute", observe)
        await vacuum_history(isolated_history_settings, store.account_id)
        assert vacuum_seen
        for engine in (store.engine, authority):
            async with engine.connect() as c:
                assert await c.scalar(text("SHOW statement_timeout")) == "5s"
        assert (await historical_performance(store))["period"] == before["period"]
    finally:
        await authority.dispose()
        await store.engine.dispose()


async def test_suffix_and_second_compaction_preserve_flows_and_old_ranges(
    isolated_history_settings: Settings,
) -> None:
    store, before, _ = await seed(isolated_history_settings)
    try:
        fast, first = await compact(isolated_history_settings, store, before)
        async with PaperWriter(fast) as owner:
            venue = PaperExchange(fast)
            service = PaperExecution(owner, venue)
            replay = StoredReplay(service, venue)
            feed = Feed(replay)
            at = AT + timedelta(seconds=271)
            await service.cashflow("new-suffix-flow", Decimal(25), at)
            await feed.marker("heartbeat", at)
            await replay.batch(at=at)
        complete = await historical_performance(store)
        indexed = await historical_performance(fast)
        assert complete["period"] == indexed["period"]
        assert Decimal(cast(dict[str, Any], indexed["period"])["contributions_gbp"]) == 35
        # The full pass and indexed suffix must also agree for a past endpoint.
        assert (await historical_performance(fast, end_source=260))["period"] == (
            await historical_performance(store, end_source=260)
        )["period"]
        new, second = await compact(isolated_history_settings, fast, complete)
        assert second["certificate"]["predecessor"] == first["certificate"]["id"]
        assert second["certificate"]["id"] != first["certificate"]["id"]
        assert (await historical_performance(new))["period"] == complete["period"]
        with pytest.raises(ValueError, match="catalogue"):
            await historical_performance(fast)
        async with PaperWriter(new) as owner:
            service = PaperExecution(owner, PaperExchange(new))
            await service.halt("Stop after lossless history restart", at)
        async with store.engine.connect() as c:
            audited = await store.audit(c, full=True)
            assert audited is not None and audited.kill.hard
    finally:
        await store.engine.dispose()


async def test_failed_cutover_rolls_back_blocks_rows_and_triggers(
    isolated_history_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, before, _ = await seed(isolated_history_settings)
    original = AsyncConnection.execute
    reached = False

    async def fail(self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal reached
        if str(statement).startswith("DELETE FROM aca.paper_replay_valuations"):
            reached = True
            raise RuntimeError("Injected failure after financial cutover")
        return await original(self, statement, *args, **kwargs)

    try:
        monkeypatch.setattr(AsyncConnection, "execute", fail)
        with pytest.raises(RuntimeError, match="Injected"):
            await compact(isolated_history_settings, store, before)
        monkeypatch.setattr(AsyncConnection, "execute", original)
        assert reached
        assert (await historical_performance(store))["period"] == before["period"]
        async with store.engine.connect() as c:
            assert await c.scalar(select(func.count()).select_from(blocks)) == 0
            assert await c.scalar(select(func.count()).select_from(certificates)) == 0
            assert not await c.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='reject_mutation' "
                    "AND tgenabled <> 'O')"
                )
            )
        async with PaperWriter(store):
            pass
    finally:
        await store.engine.dispose()


@pytest.mark.parametrize("fault", ["missing", "code", "payload", "summary"])
async def test_damaged_or_obsolete_prefix_never_grants_writer(
    isolated_history_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from aca.reporting import history_prefix

    store, before, _ = await seed(isolated_history_settings, 2)
    admin_engine = create_engine(isolated_history_settings, "migration")
    try:
        fast, _ = await compact(isolated_history_settings, store, before)
        if fault == "code":
            monkeypatch.setattr(history_prefix, "package_fingerprint", lambda: "a" * 64)
        elif fault == "missing":
            fast.history_prefix = uuid4()
        else:
            # Isolated operator corruption fixture; no application mutation path.
            async with admin_engine.begin() as c:
                await c.execute(
                    text("ALTER TABLE aca.paper_history_blocks DISABLE TRIGGER reject_mutation")
                )
                if fault == "payload":
                    await c.execute(
                        blocks.update().where(blocks.c.kind == "cursor").values(payload=b"broken")
                    )
                else:
                    await c.execute(
                        blocks.update().where(blocks.c.kind.like("report:%")).values(document={})
                    )
                await c.execute(
                    text("ALTER TABLE aca.paper_history_blocks ENABLE TRIGGER reject_mutation")
                )
        with pytest.raises(ValueError, match="historical"):
            async with PaperWriter(fast):
                pytest.fail("damaged historical prefix acquired writer")
    finally:
        await admin_engine.dispose()
        await store.engine.dispose()


async def test_history_operator_refuses_active_writer_and_stale_head(
    isolated_history_settings: Settings,
) -> None:
    store, before, _ = await seed(isolated_history_settings, 2)
    try:
        async with PaperWriter(store):
            with pytest.raises(ValueError, match="stop the paper writer"):
                await compact(isolated_history_settings, store, before)
        with pytest.raises(ValueError, match="expected head"):
            await compact_history(
                isolated_history_settings,
                store.account_id,
                uuid4(),
                "Review stale compaction expected head",
                before["financial_head_version"],
                before["historical_head_source"] + 1,
            )
        assert (await historical_performance(store))["period"] == before["period"]
    finally:
        await store.engine.dispose()


async def test_cancelled_cutover_is_atomic_and_releases_writer(
    isolated_history_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, before, _ = await seed(isolated_history_settings, 2)
    original = AsyncConnection.execute
    reached = asyncio.Event()

    async def block(self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if str(statement).startswith("DELETE FROM aca.paper_replay_valuations"):
            reached.set()
            await asyncio.Event().wait()
        return await original(self, statement, *args, **kwargs)

    try:
        monkeypatch.setattr(AsyncConnection, "execute", block)
        task = asyncio.create_task(compact(isolated_history_settings, store, before))
        await asyncio.wait_for(reached.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(AsyncConnection, "execute", original)
        assert (await historical_performance(store))["period"] == before["period"]
        async with PaperWriter(store):
            pass
    finally:
        await store.engine.dispose()


async def test_fills_protection_and_exact_drawdown_survive_compaction(
    isolated_history_settings: Settings,
) -> None:
    from test_historical_performance import reconcile
    from test_historical_replay import fok

    from aca.domain.trading import Valuation

    store, _, _ = await seed(isolated_history_settings, 0)
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            replay = StoredReplay(service, venue)
            feed = Feed(replay)
            await feed.bootstrap()
            await service.mark(
                (Valuation(asset="BTC", bid_gbp=Decimal(999), observed_at=AT, source="fixture"),),
                AT,
            )
            await service.recover(AT)
            await fok(replay, feed)
            await reconcile(replay, AT + timedelta(milliseconds=300))
            for millis, bid, ask in ((600, "9999", "10000"), (700, "1", "2"), (900, "999", "1000")):
                await feed.book(AT + timedelta(milliseconds=millis), bid=bid, ask=ask)
            await replay.batch(at=AT + timedelta(milliseconds=1100))
        before = await historical_performance(store)
        assert Decimal(cast(dict[str, Any], before["period"])["max_observed_drawdown"]) > Decimal(
            "0.1"
        )
        fast, _ = await compact(isolated_history_settings, store, before)
        assert (await historical_performance(fast))["period"] == before["period"]
        async with store.engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            financial = await fast.audit(c)
            assert financial == await store.audit(c, full=True)
            assert financial is not None and financial.quantity("BTC") > 0
            assert any(o.intent.purpose == "protection" for o in financial.orders)
    finally:
        await store.engine.dispose()


async def test_oversized_suffix_blocks_rearm_but_remains_fully_auditable(
    isolated_history_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aca.reporting import history_prefix

    store, before, _ = await seed(isolated_history_settings, 0)
    try:
        fast, _ = await compact(isolated_history_settings, store, before)
        monkeypatch.setattr(history_prefix, "MAX_ONLINE_SUFFIX", 2)
        async with PaperWriter(fast) as owner:
            service = PaperExecution(owner, PaperExchange(fast))
            for _ in range(3):
                state = await service._state()
                await service._save(state, state, at=AT, kind="valuation")
        with pytest.raises(ValueError, match="suffix budget"):
            async with PaperWriter(fast):
                pytest.fail("unbounded suffix granted writer")
        assert (await historical_performance(store))["period"] == before["period"]
        async with store.engine.connect() as c:
            assert (await store.audit(c, full=True)) is not None
    finally:
        await store.engine.dispose()


async def test_export_reads_originals_and_never_substitutes_prefix_authority(
    isolated_history_settings: Settings,
    tmp_path: Path,
) -> None:
    from aca.reporting.replay_archive import export_archive, verify_archive
    from aca.storage.archive_format import ArchiveReference

    store, before, _ = await seed(isolated_history_settings)
    try:
        fast, _ = await compact(isolated_history_settings, store, before)
        result = await export_archive(store, tmp_path / "originals.aca")
        verified = verify_archive(tmp_path / "originals.aca", str(result["archive_sha256"]))
        assert (
            cast(dict[str, Any], verified["context"])["valuation_count"]
            == before["period"]["observations"]
        )
        fresh = PaperStore(store.engine, store.account_id, history_prefix=fast.history_prefix)
        with pytest.raises(ValueError, match="full-audit"):
            await export_archive(fresh, tmp_path / "not-full.aca")
        file_store = PaperStore(
            store.engine,
            store.account_id,
            replay_archive=ArchiveReference(tmp_path / "missing.aca", "a" * 64),
        )
        with pytest.raises(ValueError, match="compacted history"):
            await historical_performance(file_store)
    finally:
        await store.engine.dispose()


async def test_midnight_bridge_in_next_report_block_is_not_lost(
    isolated_history_settings: Settings,
) -> None:
    from aca.reporting.historical import read_performance

    store = PaperStore(create_engine(isolated_history_settings), uuid4())
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            previous_day = AT - timedelta(days=1)
            await service.initialise(previous_day)
            await service.cashflow("midnight-opening", Decimal(1000), previous_day)
            replay = StoredReplay(service, venue)
            await replay.enrol()
            feed = Feed(replay)
            await feed.bootstrap(previous_day)
            for i in range(245):
                when = AT - timedelta(seconds=3) + timedelta(microseconds=i)
                await feed.marker("heartbeat", when)
                await replay.batch(at=when)
            await feed.bootstrap(AT - timedelta(seconds=1))
            await feed.marker("capture_health", AT + timedelta(seconds=1), {"market_ready": True})
            await replay.batch(at=AT + timedelta(seconds=1))
        before = await historical_performance(store)
        fast, _ = await compact(isolated_history_settings, store, before)
        async with store.engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            original = await read_performance(c, store, day=previous_day.date())
            indexed = await read_performance(c, fast, day=previous_day.date())
            assert original.calendar is not None and indexed.calendar is not None
            assert original.calendar.closing is not None
            assert indexed.calendar.closing == original.calendar.closing
            assert indexed.report is not None and original.report is not None
            assert indexed.report["period"] == original.report["period"]
    finally:
        await store.engine.dispose()


async def test_dispatch_capacity_veto_expires_reservation_without_sending(
    isolated_history_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from restore_fixture import NoCommands
    from test_paper_execution import signal
    from test_stored_replay import evidence

    from aca.reporting import history_prefix
    from aca.risk.execution import DispatchDenied

    store, _, _ = await seed(isolated_history_settings, 0)
    try:
        async with PaperWriter(store) as owner:
            await PaperExecution(owner, PaperExchange(store)).recover(AT)
        before = await historical_performance(store)
        fast, _ = await compact(isolated_history_settings, store, before)
        monkeypatch.setattr(history_prefix, "MAX_ONLINE_SUFFIX", 32)
        async with PaperWriter(fast) as owner:
            venue = NoCommands(fast)
            service = PaperExecution(owner, venue)
            market = evidence(AT, uuid4(), depth="10")
            order = await service.queue(signal(), market, AT)
            with pytest.raises(DispatchDenied, match="history maintenance"):
                await service.dispatch(order.intent.client_id, market, AT)
            assert venue.attempts == 0
            assert (await service._state()).order(order.intent.client_id).state == "EXPIRED"
    finally:
        await store.engine.dispose()


async def test_legacy_historical_defaults_keep_original_raw_hashes(
    isolated_history_settings: Settings,
) -> None:
    store, _, _ = await seed(isolated_history_settings, 0)
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            replay = StoredReplay(PaperExecution(owner, venue), venue)
            at = AT + timedelta(milliseconds=1)
            feed = Feed(replay)
            async with owner.transaction() as c:
                current = await replay._latest(c)
                assert current is not None
                epoch = replay._cursor(current).epoch
                assert epoch is not None
                feed.epoch = epoch
            await feed.marker("heartbeat", at)
            async with owner.transaction() as c:
                source = (
                    (
                        await c.execute(
                            select(public_events).order_by(public_events.c.number.desc()).limit(1)
                        )
                    )
                    .mappings()
                    .one()
                )
                previous = await replay.history.latest(c)
                cursor = await replay._latest(c)
                assert previous is not None and cursor is not None
                value = replay.history.restore(previous).model_copy(
                    update={
                        "number": source["number"],
                        "source_hash": source["fingerprint"],
                        "source_received_at": at,
                        "state": replay.history.restore(previous).state.model_copy(
                            update={"as_of": at}
                        ),
                    }
                )
                old_value = value.document()
                old_value.pop("day_boundary")
                value_hash = fingerprint(
                    {"previous": previous["fingerprint"], "valuation": old_value}
                )
                await c.execute(
                    valuations.insert().values(
                        account_id=store.account_id,
                        number=source["number"],
                        previous_hash=previous["fingerprint"],
                        fingerprint=value_hash,
                        document=old_value,
                    )
                )
                old_cursor = (
                    replay._cursor(cursor)
                    .model_copy(
                        update={"number": source["number"], "source_hash": source["fingerprint"]}
                    )
                    .document()
                )
                old_cursor.pop("trade_watermarks")
                cursor_hash = fingerprint({"previous": cursor["fingerprint"], "cursor": old_cursor})
                await c.execute(
                    cursors.insert().values(
                        account_id=store.account_id,
                        number=source["number"],
                        previous_hash=cursor["fingerprint"],
                        fingerprint=cursor_hash,
                        document=old_cursor,
                    )
                )
        before = await historical_performance(store)
        fast, _ = await compact(isolated_history_settings, store, before)
        assert (await historical_performance(fast))["period"] == before["period"]
        async with PaperWriter(fast):
            pass
        async with store.engine.connect() as c:
            assert (
                await c.scalar(
                    select(valuations.c.document).where(valuations.c.number == source["number"])
                )
                == old_value
            )
            assert (
                await c.scalar(
                    select(cursors.c.document).where(cursors.c.number == source["number"])
                )
                == old_cursor
            )
            assert (
                await c.scalar(
                    select(valuations.c.fingerprint).where(valuations.c.number == source["number"])
                )
                == value_hash
            )
    finally:
        await store.engine.dispose()


async def test_omitting_certificate_does_not_bypass_entry_capacity(
    isolated_history_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from restore_fixture import NoCommands
    from test_paper_execution import signal
    from test_stored_replay import evidence

    from aca.reporting import history_prefix
    from aca.risk.execution import DispatchDenied

    store, _, _ = await seed(isolated_history_settings, 0)
    try:
        async with PaperWriter(store) as owner:
            venue = NoCommands(store)
            service = PaperExecution(owner, venue)
            await service.recover(AT)
            market = evidence(AT, uuid4(), depth="10")
            order = await service.queue(signal(), market, AT)
            monkeypatch.setattr(history_prefix, "MAX_ONLINE_SUFFIX", 32)
            with pytest.raises(DispatchDenied, match="history maintenance"):
                await service.dispatch(order.intent.client_id, market, AT)
            assert venue.attempts == 0
            assert (await service._state()).order(order.intent.client_id).state == "EXPIRED"
    finally:
        await store.engine.dispose()
