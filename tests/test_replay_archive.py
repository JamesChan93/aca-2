"""Read-only exports bind audited finance, ordered replay and original payloads."""

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, text
from test_historical_performance import reconcile
from test_historical_replay import fok
from test_stored_replay import AT, Feed

from aca.domain.trading import fingerprint
from aca.operations.cli import main
from aca.reporting.replay_archive import _observations, export_archive, verify_archive
from aca.research.artifacts import file_hash
from aca.simulation.stored_replay import ReplayReceipt, StoredReplay
from aca.storage.archive_format import ArchiveReader, ArchiveWriter
from aca.storage.models import MarketReceipt
from aca.storage.paper import events, fills, journals, order_identities
from aca.storage.replay import cursors, public_events, valuations

pytest_plugins = ("test_stored_replay",)
pytestmark = pytest.mark.integration


async def counts(runner: StoredReplay) -> list[int | None]:
    async with runner.store.engine.connect() as c:
        return [
            await c.scalar(select(func.count()).select_from(table))
            for table in (
                events,
                fills,
                journals,
                order_identities,
                cursors,
                valuations,
                public_events,
                MarketReceipt.__table__,
            )
        ]


async def test_lossless_sparse_cursor_export_and_offline_cli(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner, feed = replay
    for index in range(130):
        await feed.marker("heartbeat", AT + timedelta(microseconds=index))
    for _ in range(3):
        await runner.batch(limit=64, at=AT + timedelta(seconds=1))
    before = await counts(runner)
    async with runner.service.writer.transaction() as c:
        original = await runner.store.load(c)
        first = (
            await c.execute(
                select(valuations.c.number)
                .where(valuations.c.account_id == runner.store.account_id)
                .order_by(valuations.c.number)
                .limit(1)
            )
        ).scalar_one()
        last = await runner.history.latest(c)
        assert last is not None
        expected = [
            o.document() async for o in _observations(c, runner.store, first, last["number"])
        ]
    path = tmp_path / "replay.aca"
    result = await export_archive(runner.store, path)
    assert result["integrity_verified"] is True
    assert result["database_semantics_checked_at_export"] is True
    assert result["database_semantics_rechecked"] is False
    assert result["usable_as_operational_prefix"] is result["pruning_authorised"] is False
    assert result["financial_records_included"] is False
    assert result["segments"] == 3
    assert result["archive_sha256"] == file_hash(path)
    raw = list(ArchiveReader(path, str(result["archive_sha256"])).rows())
    assert raw[1:] == expected
    assert len(expected) == 136 and sum(o["cursor"] is not None for o in expected) < 10
    assert await counts(runner) == before
    async with runner.service.writer.transaction() as c:
        assert await runner.store.load(c) == original

    def no_database(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("offline archive verification attempted a database connection")

    monkeypatch.setattr("aca.operations.paper.create_engine", no_database)
    args = [
        "paper",
        "archive-verify",
        "--account",
        str(runner.store.account_id),
        "--input",
        str(path),
        "--sha256",
        str(result["archive_sha256"]),
    ]
    assert await asyncio.to_thread(main, args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["context"]["account_id"] == str(runner.store.account_id)
    assert output["database_semantics_rechecked"] is False
    args[3] = str(uuid4())
    assert await asyncio.to_thread(main, args) == 1


async def test_export_snapshot_excludes_later_committed_receipts(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, feed = replay
    from aca.reporting import replay_archive

    original = replay_archive._observations
    later = AT + timedelta(seconds=1)

    async def appending(*args: Any, **kwargs: Any) -> Any:
        # Reuse the other already-held writer connection; the application pool
        # deliberately permits only two connections, one now pins the export.
        receipt = ReplayReceipt(
            receipt_id=uuid4(),
            epoch=feed.epoch,
            received_at=later,
            channel="heartbeat",
            valid=True,
            payload_hash=hashlib.sha256(b"{}").hexdigest(),
            normalized={},
            trades=(),
        )
        async with runner.service.writer.transaction() as c:
            await c.execute(
                insert(MarketReceipt).values(
                    id=receipt.receipt_id,
                    schema_version=1,
                    epoch=feed.epoch,
                    received_at=later,
                    channel="heartbeat",
                    valid=True,
                    reason=None,
                    payload_hash=receipt.payload_hash,
                    payload="{}",
                    normalized={},
                )
            )
            await c.execute(
                public_events.insert().values(
                    receipt_id=receipt.receipt_id,
                    document=receipt.document(),
                    fingerprint=fingerprint(receipt.document()),
                    published_at=later,
                )
            )
        await runner.batch(at=later)
        async for observation in original(*args, **kwargs):
            yield observation

    monkeypatch.setattr(replay_archive, "_observations", appending)
    path = tmp_path / "snapshot.aca"
    result = await export_archive(runner.store, path)
    context = result["context"]
    assert isinstance(context, dict) and context["valuation_count"] == 6
    async with runner.service.writer.transaction() as c:
        current = await runner.history.latest(c)
        assert current is not None and current["number"] > context["head_number"]
    assert verify_archive(path, file_hash(path))["integrity_verified"] is True


@pytest.mark.parametrize("corrupt", ["financial_units", "cursor", "raw_payload"])
async def test_export_rejects_incomplete_or_forged_evidence_without_publishing(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
    corrupt: str,
) -> None:
    runner, feed = replay
    at = AT + timedelta(seconds=1)
    if corrupt == "raw_payload":
        receipt = ReplayReceipt(
            receipt_id=uuid4(),
            epoch=feed.epoch,
            received_at=at,
            channel="heartbeat",
            valid=True,
            payload_hash=hashlib.sha256(b"{}").hexdigest(),
            normalized={},
            trades=(),
        )
        async with runner.service.writer.transaction() as c:
            await c.execute(
                insert(MarketReceipt).values(
                    id=receipt.receipt_id,
                    schema_version=1,
                    epoch=feed.epoch,
                    received_at=at,
                    channel="heartbeat",
                    valid=True,
                    reason=None,
                    payload_hash=receipt.payload_hash,
                    payload="changed raw evidence",
                    normalized={},
                )
            )
            await c.execute(
                public_events.insert().values(
                    receipt_id=receipt.receipt_id,
                    document=receipt.document(),
                    fingerprint=fingerprint(receipt.document()),
                    published_at=at,
                )
            )
        await runner.batch(at=at)
        expected = "receipt binding"
    else:
        await feed.marker("heartbeat", at)
        async with runner.service.writer.transaction() as c:
            value_row, cursor_row = await runner.history.latest(c), await runner._latest(c)
            assert value_row is not None and cursor_row is not None
            source = (
                (
                    await c.execute(
                        select(public_events).order_by(public_events.c.number.desc()).limit(1)
                    )
                )
                .mappings()
                .one()
            )
            value = runner.history.restore(value_row).model_copy(
                update={
                    "number": source["number"],
                    "source_hash": source["fingerprint"],
                    "source_received_at": at,
                }
            )
            if corrupt == "financial_units":
                value = value.model_copy(
                    update={
                        "state": value.state.model_copy(update={"units": value.state.units + 1})
                    }
                )
            await runner.history.save(c, value, value_row["fingerprint"])
            context = runner._cursor(cursor_row).model_copy(
                update={
                    "number": source["number"],
                    "source_hash": "f" * 64 if corrupt == "cursor" else source["fingerprint"],
                }
            )
            await runner._save(c, context, cursor_row["fingerprint"])
        expected = "units do not reconstruct" if corrupt == "financial_units" else "cursor identity"
    before = await counts(runner)
    with pytest.raises(ValueError, match=expected):
        await export_archive(runner.store, tmp_path / "rejected.aca")
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
    assert await counts(runner) == before
    if corrupt == "raw_payload":
        # The session database's public log is shared by test accounts. Leave a
        # valid global tail for the next account's explicit enrolment baseline.
        await feed.marker("heartbeat", at + timedelta(microseconds=1))


@pytest.mark.parametrize("corrupt", ["account", "units_float", "source", "raw", "terminal"])
async def test_offline_verifier_rejects_repacked_invalid_archive(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
    corrupt: str,
) -> None:
    runner, _ = replay
    path = tmp_path / "original.aca"
    result = await export_archive(runner.store, path)
    reader = ArchiveReader(path, str(result["archive_sha256"]))
    rows = list(reader.rows())
    assert reader.manifest is not None
    if corrupt == "account":
        rows[-1]["valuation"]["account_id"] = str(uuid4())  # type: ignore[index]
    elif corrupt == "units_float":
        rows[-1]["valuation"]["document"]["state"]["units"] = 1000.0  # type: ignore[index]
        value = rows[-1]["valuation"]
        assert isinstance(value, dict)
        value["fingerprint"] = fingerprint(
            {"previous": value["previous_hash"], "valuation": value["document"]}
        )
    elif corrupt == "source":
        rows[-1]["source"]["fingerprint"] = "f" * 64  # type: ignore[index]
    elif corrupt == "raw":
        rows[-1]["receipt"]["payload"] = "changed"  # type: ignore[index]
    else:
        rows.pop()
    damaged = tmp_path / "repacked.aca"
    with ArchiveWriter(damaged) as writer:
        for row in rows:
            writer.append(row)
        hashed = writer.finish({"header_hash": fingerprint(rows[0])})
        writer.publish()
    with pytest.raises(ValueError):
        verify_archive(damaged, hashed)


async def test_database_export_is_read_only(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
) -> None:
    runner, _ = replay
    statements = []

    def capture(c: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(runner.store.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await export_archive(runner.store, tmp_path / "readonly.aca")
    finally:
        event.remove(runner.store.engine.sync_engine, "before_cursor_execute", capture)
    assert statements and all(s.lstrip().startswith("SELECT") for s in statements)


async def test_real_source_sequence_gaps_are_preserved(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
) -> None:
    runner, feed = replay
    async with runner.service.writer.transaction() as c:
        skipped = await c.scalar(text("SELECT nextval('aca.public_replay_events_number_seq')"))
    await feed.marker("heartbeat", AT + timedelta(seconds=1))
    await runner.batch(at=AT + timedelta(seconds=1))
    result = await export_archive(runner.store, tmp_path / "gap.aca")
    context = result["context"]
    assert isinstance(context, dict) and context["head_number"] == skipped + 1
    rows = list(ArchiveReader(tmp_path / "gap.aca", str(result["archive_sha256"])).rows())
    assert skipped not in [row["valuation"]["number"] for row in rows[1:]]  # type: ignore[index]
    assert len(rows) == 8  # Context, baseline, five bootstrap values and heartbeat.


async def test_valid_head_cannot_conceal_bad_intermediate_cursor(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
) -> None:
    runner, feed = replay
    for index in range(2):
        await feed.marker("heartbeat", AT + timedelta(seconds=index + 1))
    async with runner.service.writer.transaction() as c:
        first, cursor_row = await runner.history.latest(c), await runner._latest(c)
        assert first is not None and cursor_row is not None
        value, previous = runner.history.restore(first), first["fingerprint"]
        sources = (
            (
                await c.execute(
                    select(public_events)
                    .where(public_events.c.number > value.number)
                    .order_by(public_events.c.number)
                )
            )
            .mappings()
            .all()
        )
        for index, source in enumerate(sources):
            receipt = ReplayReceipt.restore(source["document"])
            value = runner.history.observe(value, receipt.received_at, (), None).model_copy(
                update={
                    "number": source["number"],
                    "source_hash": source["fingerprint"],
                    "source_received_at": receipt.received_at,
                }
            )
            previous = await runner.history.save(c, value, previous)
            cursor = runner._cursor(cursor_row).model_copy(
                update={
                    "number": source["number"],
                    "source_hash": "f" * 64 if index == 0 else source["fingerprint"],
                }
            )
            await runner._save(c, cursor, cursor_row["fingerprint"])
            cursor_row = await runner._latest(c)
            assert cursor_row is not None
    with pytest.raises(ValueError, match="cursor source anchor mismatch"):
        await export_archive(runner.store, tmp_path / "bad-cursor.aca")
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_failed_independent_verification_cannot_publish(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = replay

    def rejected(*args: Any) -> Any:
        raise ValueError("independent verification failed")

    monkeypatch.setattr("aca.reporting.replay_archive.verify_archive", rejected)
    before = await counts(runner)
    with pytest.raises(ValueError, match="independent verification failed"):
        await export_archive(runner.store, tmp_path / "rejected.aca")
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
    assert await counts(runner) == before


async def test_held_position_and_fee_evidence_require_reconciliation_before_export(
    replay: tuple[StoredReplay, Feed],
    tmp_path: Path,
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    with pytest.raises(ValueError, match="reconciled historical fills"):
        await export_archive(runner.store, tmp_path / "unposted.aca")
    await reconcile(runner, AT + timedelta(milliseconds=300))
    before = await counts(runner)
    async with runner.service.writer.transaction() as c:
        financial = await runner.store.load(c)
        assert financial is not None and financial.quantity("BTC") > 0
    path = tmp_path / "held-position.aca"
    result = await export_archive(runner.store, path)
    context = result["context"]
    assert isinstance(context, dict)
    assert context["financial_state_hash"] == fingerprint(financial.document())
    assert result["financial_records_included"] is False
    assert await counts(runner) == before
    rows = list(ArchiveReader(path, str(result["archive_sha256"])).rows())
    value = rows[-1]["valuation"]
    assert isinstance(value, dict)
    assert value["document"]["state"]["balances"][1]["quantity"] != "0"
    assert verify_archive(path, str(result["archive_sha256"]))["integrity_verified"] is True
