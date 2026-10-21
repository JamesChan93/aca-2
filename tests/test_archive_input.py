"""Archived replay is read online without substituting a claimed financial audit."""

import argparse
import asyncio
from collections.abc import Generator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from test_historical_performance import reconcile
from test_historical_replay import fok, latest
from test_replay_archive import counts
from test_stored_replay import AT, Feed, evidence

from aca.execution.paper import PaperExecution
from aca.observability.notices import daily_summary
from aca.operations import paper
from aca.operations.config import RiskPolicy, Settings
from aca.reporting import archive_input, historical
from aca.reporting.historical import historical_performance
from aca.reporting.replay_archive import _observations, export_archive, verify_archive
from aca.research.artifacts import file_hash
from aca.simulation.historical import HistoricalReplay
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.archive_format import ArchiveReader, ArchiveReference, ArchiveWriter
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter, WriterUnavailable
from aca.storage.replay import public_events

pytest_plugins = ("test_stored_replay",)
pytestmark = pytest.mark.integration


async def archived(runner: StoredReplay, path: Path) -> tuple[PaperStore, dict[str, Any]]:
    exported = await export_archive(runner.store, path)
    return (
        PaperStore(
            runner.store.engine,
            runner.store.account_id,
            replay_archive=ArchiveReference(path, str(exported["archive_sha256"])),
        ),
        exported,
    )


def economics(report: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in report.items() if k not in {"report_hash", "replay_archive_sha256"}}


async def test_archived_prefix_and_live_tail_match_sql_for_full_and_exact_ranges(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, feed = replay
    for i in range(130):
        await feed.marker("heartbeat", AT + timedelta(microseconds=i))
    for _ in range(3):
        await runner.batch(at=AT + timedelta(seconds=1), limit=64)
    store, exported = await archived(runner, tmp_path / "prefix.aca")
    boundary = (await latest(runner)).number
    # A legitimate allocation gap at the archive/SQL join is not missing evidence.
    async with runner.service.writer.transaction() as c:
        await c.execute(text("SELECT nextval('aca.public_replay_events_number_seq')"))
    at = AT + timedelta(seconds=2)
    await runner.service.cashflow("after-archive", Decimal(250), at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    before = await counts(runner)
    consumed = 0
    original = ArchiveReader.rows

    def observed(reader: ArchiveReader) -> Generator[dict[str, object]]:
        nonlocal consumed
        for item in original(reader):
            consumed += 1
            yield item

    monkeypatch.setattr(ArchiveReader, "rows", observed)
    for bounds in ({}, {"end_source": boundary}, {"start_source": boundary}):
        sql = await historical_performance(runner.store, **bounds)
        result = await historical_performance(store, **bounds)
        assert economics(result) == economics(sql)
        assert result["replay_archive_sha256"] == exported["archive_sha256"]
    assert consumed == 3 * 137  # Context plus 136 archived observations on each read.
    assert store.archive_identity is not None
    assert await counts(runner) == before


async def test_fill_fee_entry_in_archive_exit_and_investment_flow_in_sql_tail(
    replay: tuple[StoredReplay, Feed], tmp_path: Path
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    at = AT + timedelta(milliseconds=300)
    await reconcile(runner, at)
    store, _ = await archived(runner, tmp_path / "holding.aca")
    at += timedelta(milliseconds=10)
    assert (
        await runner.service.exit_position("BTC/GBP", evidence(at, feed.epoch, depth="10"), at)
        is not None
    )
    at += timedelta(milliseconds=300)
    await feed.book(at, depth="10")
    await runner.batch(at=at)
    await reconcile(runner, at)
    at += timedelta(milliseconds=1)
    await runner.service.cashflow("tail-withdrawal", Decimal(-50), at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    sql = await historical_performance(runner.store)
    result = await historical_performance(store)
    assert economics(result) == economics(sql)
    period = result["period"]
    assert isinstance(period, dict)
    assert period["trade_fill_count"] == 2 and Decimal(period["withdrawals_gbp"]) == 50
    assert Decimal(period["realised_pnl_gbp"]) - Decimal(period["fees_gbp"]) == Decimal(
        period["pnl_gbp"]
    )


async def test_midnight_anchor_and_daily_report_cross_archive_sql_join(
    replay: tuple[StoredReplay, Feed], tmp_path: Path
) -> None:
    runner, feed = replay
    before = AT + timedelta(days=1, seconds=-2)
    await feed.marker(
        "instrument_rules",
        before,
        {
            "rules": [
                evidence(before, feed.epoch, symbol)
                .rules.model_copy(update={"observed_at": before})
                .model_dump(mode="json")
                for symbol in ("BTC/GBP", "ETH/GBP")
            ]
        },
    )
    await feed.book(before)
    await feed.book(before, "ETH/GBP")
    await feed.marker("capture_health", before, {"market_ready": True})
    await runner.batch(at=before)
    store, _ = await archived(runner, tmp_path / "before-midnight.aca")
    after = AT + timedelta(days=1, seconds=1)
    await feed.marker("heartbeat", after)
    await runner.batch(at=after)
    assert (await latest(runner)).day_boundary is not None
    async with runner.store.engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        sql = await daily_summary(c, runner.store, AT.date())
        result = await daily_summary(c, store, AT.date())
    for item in (sql, result):
        item.pop("report_hash")
        observed = item["observed"]
        assert isinstance(observed, dict)
        observed.pop("report_hash")
    assert result == sql
    assert economics(await historical_performance(store)) == economics(
        await historical_performance(runner.store)
    )


async def test_missing_valuation_in_newer_sql_tail_blocks_even_an_older_archived_report(
    replay: tuple[StoredReplay, Feed], tmp_path: Path
) -> None:
    runner, feed = replay
    store, exported = await archived(runner, tmp_path / "gap.aca")
    for seconds in (1, 2):
        await feed.marker("heartbeat", AT + timedelta(seconds=seconds))
    async with runner.service.writer.transaction() as c:
        source = (
            (
                await c.execute(
                    select(public_events).order_by(public_events.c.number.desc()).limit(1)
                )
            )
            .mappings()
            .one()
        )
        value_row, cursor_row = await runner.history.latest(c), await runner._latest(c)
        assert value_row is not None and cursor_row is not None
        value = runner.history.restore(value_row).model_copy(
            update={
                "number": source["number"],
                "source_hash": source["fingerprint"],
                "source_received_at": AT + timedelta(seconds=2),
            }
        )
        await runner.history.save(c, value, value_row["fingerprint"])
        cursor = runner._cursor(cursor_row).model_copy(
            update={
                "number": source["number"],
                "source_hash": source["fingerprint"],
            }
        )
        await runner._save(c, cursor, cursor_row["fingerprint"])
    before = await counts(runner)
    with pytest.raises(ValueError, match="source coverage gap"):
        await historical_performance(store, end_source=exported["context"]["head_number"])
    await runner.service.writer.__aexit__()
    with pytest.raises(ValueError, match="source coverage gap"):
        async with PaperWriter(store):
            pytest.fail("writer ignored the source missing from the SQL suffix")
    assert await counts(runner) == before


@pytest.mark.parametrize("fault", ["missing", "wrong_root", "truncated", "suffix"])
async def test_bad_required_file_never_falls_back_even_for_report_ending_at_baseline(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, fault: str
) -> None:
    runner, _ = replay
    path = tmp_path / "required.aca"
    store, exported = await archived(runner, path)
    if fault == "missing":
        path.unlink()
    elif fault == "wrong_root":
        store.replay_archive = ArchiveReference(path, "0" * 64)
    else:
        content = path.read_bytes()
        path.write_bytes(content[:-1] if fault == "truncated" else content + b"x")
    before = await counts(runner)
    with pytest.raises((ValueError, OSError)):
        await historical_performance(store, end_source=exported["context"]["baseline_number"])
    assert store.archive_identity is None
    assert await counts(runner) == before
    assert (await historical_performance(runner.store))["financial_evidence_reconciled"] is True


@pytest.mark.parametrize(
    "field",
    [
        "account_id",
        "policy_hash",
        "financial_hash",
        "financial_state_hash",
        "head_valuation_hash",
        "head_cursor_hash",
        "baseline_number",
    ],
)
async def test_repacked_header_with_new_expected_root_cannot_replace_database_authority(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, field: str
) -> None:
    runner, _ = replay
    original = tmp_path / "original.aca"
    _, exported = await archived(runner, original)
    rows = list(ArchiveReader(original, file_hash(original)).rows())
    context = dict(exported["context"])
    context[field] = (
        str(uuid4())
        if field == "account_id"
        else (context["baseline_number"] + 1 if field == "baseline_number" else "f" * 64)
    )
    rows[0] = {"context": context}
    forged = tmp_path / "repacked.aca"
    from aca.domain.trading import fingerprint

    with ArchiveWriter(forged) as writer:
        for row in rows:
            writer.append(row)
        hashed = writer.finish({"header_hash": fingerprint(rows[0])})
        writer.publish()
    store = PaperStore(
        runner.store.engine,
        runner.store.account_id,
        replay_archive=ArchiveReference(forged, hashed),
    )
    with pytest.raises(ValueError, match="archive input"):
        await historical_performance(store)
    assert store.archive_identity is None


@pytest.mark.parametrize("fault", ["remove", "replace", "rewrite"])
async def test_writer_revalidates_file_identity_before_each_transaction_and_never_rearms(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, fault: str
) -> None:
    runner, _ = replay
    path = tmp_path / "required.aca"
    store, _ = await archived(runner, path)
    before = await counts(runner)
    await runner.service.writer.__aexit__()
    async with PaperWriter(store) as owner:
        assert owner.archive_checked and store.archive_identity is not None
        if fault == "remove":
            path.unlink()
        elif fault == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        else:
            content = path.read_bytes()
            path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        with pytest.raises((ValueError, OSError)):
            await PaperExecution(owner, PaperExchange(store)).cashflow(
                "must-not-post", Decimal(100), AT + timedelta(seconds=1)
            )
        assert owner.archive_faulted
        with pytest.raises(WriterUnavailable, match="fault is latched"):
            async with owner.transaction():
                pytest.fail("writer silently rearmed after losing a required archive")
    assert await counts(runner) == before


async def test_cancelled_partial_read_closes_file_without_accepting_it(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = replay
    store, _ = await archived(runner, tmp_path / "cancel.aca")
    closed = False
    original = ArchiveReader.rows

    def tracking(reader: ArchiveReader) -> Generator[dict[str, object]]:
        nonlocal closed
        try:
            yield from original(reader)
        finally:
            closed = True

    monkeypatch.setattr(ArchiveReader, "rows", tracking)
    async with store.engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        head = await runner._latest(c)
        assert head is not None
        iterator = HistoricalReplay(store, PaperExchange(store), RiskPolicy()).audited_values(
            c, head["number"]
        )
        assert (await anext(iterator))[1].reason == "baseline"
        await iterator.aclose()
    assert closed and store.archive_identity is None


async def test_cancelled_writer_acquisition_releases_lock_and_emits_no_command(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = replay
    store, _ = await archived(runner, tmp_path / "deadline.aca")
    before = await counts(runner)
    entered = asyncio.Event()

    async def slow_bind(*args: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(archive_input, "_bind", slow_bind)
    await runner.service.writer.__aexit__()
    task = asyncio.create_task(PaperWriter(store).__aenter__())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.archive_identity is None
    async with PaperWriter(runner.store):
        pass
    assert await counts(runner) == before


async def test_archive_does_not_bypass_existing_report_evidence_budget(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = replay
    store, _ = await archived(runner, tmp_path / "budget.aca")
    monkeypatch.setattr(historical, "MAX_REPORT_VALUES", 3)
    with pytest.raises(ValueError, match="evidence budget exceeded"):
        await historical_performance(store)
    assert store.archive_identity is None


async def test_cli_archive_arguments_are_explicit_pairs_before_database_access(
    replay: tuple[StoredReplay, Feed],
    database_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = replay
    path = tmp_path / "cli.aca"
    _, exported = await archived(runner, path)
    result = await paper.execute(
        argparse.Namespace(
            action="performance",
            account=runner.store.account_id,
            start_source=None,
            end_source=None,
            archive=path,
            archive_sha256=exported["archive_sha256"],
        ),
        database_settings,
    )
    assert (
        isinstance(result, dict) and result["replay_archive_sha256"] == exported["archive_sha256"]
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid archive arguments accessed the database")

    monkeypatch.setattr(paper, "create_engine", forbidden)
    for path_arg, hash_arg in ((path, None), (None, exported["archive_sha256"]), (path, "bad")):
        with pytest.raises(ValueError):
            await paper.execute(
                argparse.Namespace(
                    action="performance",
                    archive=path_arg,
                    archive_sha256=hash_arg,
                ),
                database_settings,
            )


async def test_archive_read_snapshot_excludes_concurrently_committed_flow_and_source(
    replay: tuple[StoredReplay, Feed],
    database_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, feed = replay
    path = tmp_path / "snapshot.aca"
    store, _ = await archived(runner, path)
    expected = await historical_performance(store)
    original = archive_input._bind

    async def appending(*args: Any) -> None:
        await original(*args)
        at = AT + timedelta(seconds=1)
        await runner.service.cashflow("concurrent-archive-flow", Decimal(50), at)
        await feed.marker("heartbeat", at)
        await runner.batch(at=at)

    monkeypatch.setattr(archive_input, "_bind", appending)
    # The independent report process must not occupy both execution-pool slots.
    engine = create_engine(database_settings)
    try:
        independent = PaperStore(engine, store.account_id, replay_archive=store.replay_archive)
        assert await historical_performance(independent) == expected
    finally:
        await engine.dispose()
    monkeypatch.setattr(archive_input, "_bind", original)
    actual = await historical_performance(store)
    assert cast(int, actual["financial_head_version"]) > cast(
        int, expected["financial_head_version"]
    )
    assert economics(actual) == economics(await historical_performance(runner.store))


async def test_structurally_valid_rehashed_archive_still_requires_financial_reconstruction(
    replay: tuple[StoredReplay, Feed], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aca.domain.trading import fingerprint

    runner, feed = replay
    original = tmp_path / "valid.aca"
    _, exported = await archived(runner, original)
    at = AT + timedelta(seconds=1)
    await feed.marker("heartbeat", at)
    async with runner.service.writer.transaction() as c:
        source = (
            (
                await c.execute(
                    select(public_events).order_by(public_events.c.number.desc()).limit(1)
                )
            )
            .mappings()
            .one()
        )
        value_row, cursor_row = await runner.history.latest(c), await runner._latest(c)
        assert value_row is not None and cursor_row is not None
        value = runner.history.restore(value_row).model_copy(
            update={
                "number": source["number"],
                "source_hash": source["fingerprint"],
                "source_received_at": at,
                "state": runner.history.restore(value_row).state.model_copy(
                    update={"units": Decimal(2000), "as_of": at}
                ),
            }
        )
        value_hash = await runner.history.save(c, value, value_row["fingerprint"])
        cursor = runner._cursor(cursor_row).model_copy(
            update={
                "number": source["number"],
                "source_hash": source["fingerprint"],
            }
        )
        await runner._save(c, cursor, cursor_row["fingerprint"])
        head = await runner._latest(c)
        assert head is not None
        context = dict(exported["context"])
        context.update(
            head_number=value.number,
            head_valuation_hash=value_hash,
            head_cursor_hash=head["fingerprint"],
            valuation_count=context["valuation_count"] + 1,
            cursor_count=context["cursor_count"] + 1,
        )
        forged = tmp_path / "forged-economics.aca"
        with ArchiveWriter(forged) as writer:
            header: dict[str, object] = {"context": context}
            writer.append(header)
            async for observation in _observations(
                c, runner.store, context["baseline_number"], value.number
            ):
                writer.append(observation.document())
            hashed = writer.finish({"header_hash": fingerprint(header)})
            writer.publish()
    assert verify_archive(forged, hashed)["integrity_verified"] is True
    store = PaperStore(
        runner.store.engine,
        runner.store.account_id,
        replay_archive=ArchiveReference(forged, hashed),
    )
    closed = 0
    original_rows = ArchiveReader.rows

    def closing(reader: ArchiveReader) -> Generator[dict[str, object]]:
        nonlocal closed
        try:
            yield from original_rows(reader)
        finally:
            closed += 1

    monkeypatch.setattr(ArchiveReader, "rows", closing)
    before = await counts(runner)
    with pytest.raises(ValueError, match="balances/units do not reconstruct"):
        await historical_performance(store)
    assert closed == 1
    for report_store in (store, runner.store):
        with pytest.raises(ValueError, match="balances/units do not reconstruct"):
            await historical_performance(report_store, end_source=context["baseline_number"])
    await runner.service.writer.__aexit__()
    with pytest.raises(ValueError, match="balances/units do not reconstruct"):
        async with PaperWriter(store):
            pytest.fail("writer accepted an unearned unit balance from an export claim")
    assert closed == 3
    assert await counts(runner) == before
    async with PaperWriter(runner.store):
        pass  # Rejection released the writer lock; no order/financial event was emitted.
