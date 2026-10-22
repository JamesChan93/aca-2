"""Protected financial audit authority, bounded suffixes and fail-closed recovery."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_accounting_operations import correction, flow, transfer
from test_paper_execution import AT, filled_position, market, signal
from test_stored_replay import Feed

from aca.domain.trading import Balance, PortfolioSnapshot, fingerprint
from aca.execution.paper import PaperExecution, SimulatedCrash
from aca.operations.cli import _parser
from aca.operations.config import Settings
from aca.portfolio.state import reserved_cash
from aca.reporting.historical import historical_performance
from aca.reporting.replay_archive import export_archive
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage import audit_prefix
from aca.storage.audit_prefix import FinancialAuditPrefix, accept_prefix, prefixes
from aca.storage.database import DatabaseSafetyError, create_engine, verify_runtime
from aca.storage.paper import PaperStore, PaperWriter, checkpoints, events, journals
from aca.storage.paper_projection import delta
from aca.storage.replay import public_events

pytestmark = pytest.mark.integration


async def seed(settings: Settings, count: int = 0) -> tuple[PaperStore, PortfolioSnapshot]:
    store = PaperStore(create_engine(settings), uuid4())
    async with PaperWriter(store) as owner:
        service = PaperExecution(owner, PaperExchange(store))
        await service.initialise(AT)
        state = await service.cashflow("prefix-opening", Decimal(1000), AT)
        for i in range(count):
            state = await service._save(state, state, at=AT, kind=f"observe-{i}")
    return store, state


async def accept(settings: Settings, store: PaperStore, version: int) -> PaperStore:
    result = await accept_prefix(
        settings, store.account_id, uuid4(), "Verified audit fixture", version
    )
    return PaperStore(store.engine, store.account_id, audit_prefix=UUID(str(result["id"])))


async def read(store: PaperStore, *, full: bool = False) -> PortfolioSnapshot | None:
    async with store.engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        return await store.audit(c, full=full)


async def test_only_suffix_restored_and_full_audit_remains_available(
    database_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state = await seed(database_settings, 270)
    try:
        fast = await accept(database_settings, store, state.version)
        async with PaperWriter(store) as owner:
            service = PaperExecution(owner, PaperExchange(store))
            end = await service.cashflow("prefix-tail", Decimal(10), AT)
        calls = []
        original = PaperStore._restore_row

        def restore(self: PaperStore, *args: Any) -> Any:
            calls.append(args[2])
            return original(self, *args)

        monkeypatch.setattr(PaperStore, "_restore_row", restore)
        assert await read(fast) == end
        assert calls == [state.version + 1]
        calls.clear()
        assert await read(fast, full=True) == end
        assert calls == list(range(1, end.version + 1))
        async with PaperWriter(fast) as owner:
            assert (await PaperExecution(owner, PaperExchange(fast))._state()) == end
    finally:
        await store.engine.dispose()


@pytest.mark.parametrize("privilege", ["INSERT", "INSERT (document)"])
async def test_runtime_cannot_accept_and_readiness_rejects_unexpected_grants(
    database_settings: Settings, privilege: str
) -> None:
    store, state = await seed(database_settings)
    authority = create_engine(database_settings, "migration")
    try:
        fast = await accept(database_settings, store, state.version)
        async with store.engine.begin() as c:
            with pytest.raises(DBAPIError):
                await c.execute(prefixes.insert().values(id=uuid4()))
        async with authority.begin() as c:
            await c.execute(text(f"GRANT {privilege} ON aca.paper_financial_audits TO aca_runtime"))
        with pytest.raises(DatabaseSafetyError, match="audit authority"):
            await verify_runtime(store.engine)
        with pytest.raises(ValueError, match="no acceptance authority"):
            await read(fast)
    finally:
        async with authority.begin() as c:
            await c.execute(
                text(f"REVOKE {privilege} ON aca.paper_financial_audits FROM aca_runtime")
            )
        await authority.dispose()
        await store.engine.dispose()


async def test_acceptance_requires_stopped_writer_and_expected_version(
    database_settings: Settings,
) -> None:
    store, state = await seed(database_settings)
    try:
        async with PaperWriter(store):
            with pytest.raises(ValueError, match="stop the paper writer"):
                await accept(database_settings, store, state.version)
        with pytest.raises(ValueError, match="expected version"):
            await accept(database_settings, store, state.version + 1)
        async with store.engine.connect() as c:
            assert (
                await c.scalar(
                    select(func.count())
                    .select_from(prefixes)
                    .where(prefixes.c.account_id == store.account_id)
                )
                == 0
            )
        assert (await accept(database_settings, store, state.version)).audit_prefix is not None
    finally:
        await store.engine.dispose()


async def test_cancelled_acceptance_creates_no_seal_and_releases_lock(
    database_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state = await seed(database_settings)
    started = asyncio.Event()
    original = PaperStore.audit

    async def blocked(self: PaperStore, *args: Any, **kwargs: Any) -> Any:
        assert kwargs["full"] is True
        started.set()
        await asyncio.Event().wait()

    try:
        monkeypatch.setattr(PaperStore, "audit", blocked)
        task = asyncio.create_task(accept(database_settings, store, state.version))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(PaperStore, "audit", original)
        async with PaperWriter(store):
            pass
        async with store.engine.connect() as c:
            assert (
                await c.scalar(
                    select(func.count())
                    .select_from(prefixes)
                    .where(prefixes.c.account_id == store.account_id)
                )
                == 0
            )
    finally:
        await store.engine.dispose()


@pytest.mark.parametrize(
    "kind", ["missing", "account", "code", "policy", "environment", "checkpoint"]
)
async def test_required_prefix_faults_cannot_grant_a_writer(
    database_settings: Settings, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    store, state = await seed(database_settings)
    try:
        fast = await accept(database_settings, store, state.version)
        if kind == "missing":
            fast.audit_prefix = uuid4()
        elif kind == "account":
            fast.account_id = uuid4()
        elif kind == "code":
            monkeypatch.setattr(audit_prefix, "package_fingerprint", lambda: "e" * 64)
        elif kind == "policy":
            monkeypatch.setattr(audit_prefix, "policy_fingerprint", lambda: "e" * 64)
        elif kind == "environment":
            monkeypatch.setattr(audit_prefix, "environment_fingerprint", lambda: "e" * 64)
        else:
            # A runtime INSERT into a previously audited checkpoint set cannot
            # extend that trust. The count changes, even if anchor fields match.
            async with store.engine.begin() as c:
                anchor = (
                    (
                        await c.execute(
                            select(events).where(
                                events.c.account_id == store.account_id,
                                events.c.version == state.version,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                await c.execute(
                    checkpoints.insert().values(
                        account_id=store.account_id,
                        version=state.version,
                        event_hash=anchor["fingerprint"],
                        state_hash=anchor["document"]["state_hash"],
                        document=state.document(),
                    )
                )
        with pytest.raises(ValueError, match="financial audit prefix"):
            async with PaperWriter(fast):
                pytest.fail("a required invalid prefix cannot acquire a writer")
        # A separate explicit full audit grants no trust to the failed prefix.
        assert await read(store, full=True) == state
    finally:
        await store.engine.dispose()


async def test_prefix_rows_are_append_only_even_for_the_migration_role(
    database_settings: Settings,
) -> None:
    store, state = await seed(database_settings)
    authority = create_engine(database_settings, "migration")
    try:
        await accept(database_settings, store, state.version)
        for statement in (
            "UPDATE aca.paper_financial_audits SET version=version",
            "DELETE FROM aca.paper_financial_audits",
            "TRUNCATE aca.paper_financial_audits",
        ):
            async with authority.begin() as c:
                with pytest.raises(DBAPIError):
                    await c.execute(text(statement))
    finally:
        await authority.dispose()
        await store.engine.dispose()


async def test_retained_prefix_gap_is_detected_even_when_the_head_anchor_survives(
    database_settings: Settings,
) -> None:
    store, state = await seed(database_settings, 3)
    authority = create_engine(database_settings, "migration")
    try:
        fast = await accept(database_settings, store, state.version)
        # Isolated restore-fault fixture: privileged DDL simulates an incomplete
        # restore. This is not an application retention/delete operation.
        async with authority.begin() as c:
            await c.execute(text("ALTER TABLE aca.paper_events DISABLE TRIGGER reject_mutation"))
            await c.execute(
                events.delete().where(
                    events.c.account_id == store.account_id,
                    events.c.version == 3,
                )
            )
            await c.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            await c.execute(text("ALTER TABLE aca.paper_events ENABLE TRIGGER reject_mutation"))
        with pytest.raises(ValueError, match="retained event set is incomplete"):
            await read(fast)
        with pytest.raises(ValueError):
            async with PaperWriter(fast):
                pytest.fail("a retained-prefix gap cannot grant a writer")
    finally:
        await authority.dispose()
        await store.engine.dispose()


async def test_rehashed_prefix_state_must_still_match_its_original_event_anchor(
    database_settings: Settings,
) -> None:
    store, state = await seed(database_settings)
    authority = create_engine(database_settings, "migration")
    try:
        fast = await accept(database_settings, store, state.version)
        async with authority.begin() as c:
            document = await c.scalar(
                select(prefixes.c.document).where(prefixes.c.id == fast.audit_prefix)
            )
            assert isinstance(document, dict)
            record = FinancialAuditPrefix.restore(document)
            bad_state = state.model_copy(update={"units": Decimal(9999)})
            bad = record.model_copy(
                update={
                    "id": uuid4(),
                    "state": bad_state,
                    "state_hash": fingerprint(bad_state.document()),
                }
            )
            await c.execute(
                prefixes.insert().values(
                    id=bad.id,
                    account_id=store.account_id,
                    version=bad.version,
                    recorded_at=bad.recorded_at,
                    fingerprint=fingerprint(bad.document()),
                    document=bad.document(),
                )
            )
        wrong = PaperStore(store.engine, store.account_id, audit_prefix=bad.id)
        with pytest.raises(ValueError, match="event anchor mismatch"):
            await read(wrong)
    finally:
        await authority.dispose()
        await store.engine.dispose()


async def forge_balance_round_trip(store: PaperStore, state: PortfolioSnapshot) -> None:
    async with store.engine.begin() as c:
        previous = await c.scalar(
            select(events.c.fingerprint).where(
                events.c.account_id == store.account_id, events.c.version == state.version
            )
        )
        for forged in (True, False):
            after = state.model_copy(
                update={
                    "version": state.version + 1,
                    "balances": tuple(
                        Balance(asset="BTC", quantity=Decimal(1), cost_gbp=Decimal(10))
                        if forged and b.asset == "BTC"
                        else b
                        for b in state.balances
                    ),
                }
            )
            document = delta(state, after, None)
            source = f"forged:{after.version}"
            hashed = fingerprint(
                {
                    "previous": previous,
                    "version": after.version,
                    "kind": "observation",
                    "source": source,
                    "state": document,
                }
            )
            await c.execute(
                events.insert().values(
                    account_id=store.account_id,
                    version=after.version,
                    id=uuid4(),
                    source=source,
                    kind="observation",
                    at=AT,
                    previous_hash=previous,
                    fingerprint=hashed,
                    document=document,
                )
            )
            previous, state = hashed, after


async def test_bad_prefix_cannot_be_accepted_and_bad_suffix_cannot_hide_in_final_balance(
    database_settings: Settings,
) -> None:
    store, state = await seed(database_settings)
    try:
        fast = await accept(database_settings, store, state.version)
        await forge_balance_round_trip(store, state)
        with pytest.raises(ValueError, match="incremental projection differ"):
            await read(fast)
        with pytest.raises(ValueError, match="incremental projection differ"):
            await accept(database_settings, store, state.version + 2)
        with pytest.raises(ValueError):
            async with PaperWriter(fast):
                pytest.fail("a corrupt suffix cannot grant writer ownership")
    finally:
        await store.engine.dispose()


async def test_suffix_budget_requires_explicit_renewal(
    database_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state = await seed(database_settings)
    try:
        fast = await accept(database_settings, store, state.version)
        async with PaperWriter(store) as owner:
            service = PaperExecution(owner, PaperExchange(store))
            end = await service.cashflow("tail-one", Decimal(1), AT)
            end = await service.cashflow("tail-two", Decimal(1), AT)
        monkeypatch.setattr(audit_prefix, "MAX_AUDIT_SUFFIX_EVENTS", 1)
        with pytest.raises(ValueError, match="suffix exceeds budget"):
            await read(fast)
        renewed = await accept(database_settings, store, end.version)
        assert renewed.audit_prefix != fast.audit_prefix and await read(renewed) == end
    finally:
        await store.engine.dispose()


async def test_reconciled_fills_fees_protection_units_and_halts_survive_prefix(
    database_settings: Settings,
) -> None:
    store, _ = await seed(database_settings)
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            assert (await service.recover(AT)).complete
            await filled_position(service, venue)
            await service.halt("Manual fixture halt retains protection", AT + timedelta(seconds=1))
            state = await service._state()
        fast = await accept(database_settings, store, state.version)
        assert state.kill.hard and state.quantity("BTC") > 0
        assert await read(fast) == state and await read(fast, full=True) == state
        async with PaperWriter(fast) as owner:
            service = PaperExecution(owner, PaperExchange(fast))
            result = await service.recover(state.as_of)
            assert result.complete
            recovered = await service._state()
            assert recovered.kill == state.kill and recovered.balances == state.balances
            assert recovered.units == state.units
            assert recovered.orders == state.orders
    finally:
        await store.engine.dispose()


async def test_uncertain_submission_reservation_is_never_replayed_by_prefix(
    database_settings: Settings,
) -> None:
    store, _ = await seed(database_settings)
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            await service.recover(AT)
            order = await service.queue(signal(), market(), AT)

            def crash(where: str) -> None:
                if where == "after_submitting_before_send":
                    raise SimulatedCrash()

            service.fault = crash
            with pytest.raises(SimulatedCrash):
                await service.dispatch(order.intent.client_id, market(), AT)
            state = await service._state()
            assert reserved_cash(state) > 0
        fast = await accept(database_settings, store, state.version)
        assert await read(fast) == state
        async with PaperWriter(fast) as owner:
            service = PaperExecution(owner, PaperExchange(fast))
            await service.recover(AT)
            recovered = await service._state()
            assert reserved_cash(recovered) == reserved_cash(state)
            assert recovered.order(order.intent.client_id).state == "UNKNOWN"
    finally:
        await store.engine.dispose()


async def test_transfer_and_correction_keep_their_existing_financial_semantics(
    database_settings: Settings,
) -> None:
    store, _ = await seed(database_settings)
    try:
        async with PaperWriter(store) as owner:
            service = PaperExecution(owner, PaperExchange(store))
            await transfer(service, flow("GBP", "100"))
            state = await service._state()
        fast = await accept(database_settings, store, state.version)
        assert await read(fast) == state
        async with PaperWriter(fast) as owner:
            service = PaperExecution(owner, PaperExchange(fast))
            async with owner.transaction() as c:
                target = await c.scalar(
                    select(journals.c.id).where(
                        journals.c.account_id == store.account_id,
                        journals.c.source == "prefix-opening",
                    )
                )
            assert isinstance(target, UUID)
            await correction(service, target, flow("GBP", "900", kind="cash"))
            corrected = await service._state()
            assert corrected.accounting_invalidated and corrected.kill.hard
        assert await read(fast) == corrected
        invalid = await historical_performance(fast)
        assert invalid["accounting_valid"] is False and invalid["period"] is None
        renewed = await accept(database_settings, store, corrected.version)
        assert (await read(renewed)) == corrected  # acceptance never repairs units or a halt
    finally:
        await store.engine.dispose()


async def test_cli_exposes_acceptance_and_explicit_prefix_without_supplying_credentials(
    database_settings: Settings,
) -> None:
    from aca.operations.paper import execute

    store, state = await seed(database_settings)
    try:
        args = _parser().parse_args(
            [
                "paper",
                "audit-prefix-accept",
                "--account",
                str(store.account_id),
                "--actor",
                str(uuid4()),
                "--reason",
                "CLI financial prefix fixture",
                "--expected-version",
                str(state.version),
            ]
        )
        result = await execute(args, database_settings)
        assert isinstance(result, dict)
        FinancialAuditPrefix.restore({k: v for k, v in result.items() if k != "fingerprint"})
        status = _parser().parse_args(
            [
                "paper",
                "status",
                "--account",
                str(store.account_id),
                "--audit-prefix",
                str(result["id"]),
            ]
        )
        assert isinstance(await execute(status, database_settings), dict)
        status.audit_prefix = uuid4()
        with pytest.raises(ValueError, match="prefix is missing"):
            await execute(status, database_settings)
        for action in (
            "create",
            "audit",
            "archive-export",
            "archive-verify",
            "audit-prefix-accept",
        ):
            # These actions cannot silently turn a full proof/export into prefix reuse.
            with pytest.raises(SystemExit):
                _parser().parse_args(
                    [
                        "paper",
                        action,
                        "--account",
                        str(store.account_id),
                        "--audit-prefix",
                        str(result["id"]),
                    ]
                )
    finally:
        await store.engine.dispose()


async def test_reports_still_reconstruct_all_historical_economics_after_financial_acceptance(
    database_settings: Settings,
    tmp_path: Path,
) -> None:
    store, _ = await seed(database_settings)
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            runner = StoredReplay(service, venue)
            feed = Feed(runner)
            baseline = await runner.enrol()
            await feed.marker("heartbeat", AT + timedelta(seconds=1))
            await runner.batch(at=AT + timedelta(seconds=1))
            state = await service._state()
        fast = await accept(database_settings, store, state.version)
        with pytest.raises(ValueError, match="full-audit store"):
            await export_archive(fast, tmp_path / "must-not-export.aca")
        assert not (tmp_path / "must-not-export.aca").exists()
        sql = await historical_performance(store)
        result = await historical_performance(fast)
        assert result["financial_audit_prefix"] == str(fast.audit_prefix)
        assert {
            k: v for k, v in result.items() if k not in {"report_hash", "financial_audit_prefix"}
        } == {k: v for k, v in sql.items() if k not in {"report_hash", "financial_audit_prefix"}}
        # A seal covers financial events only. A correctly rehashed future
        # historical value inventing units must still invalidate an older report.
        async with PaperWriter(fast) as owner:
            venue = PaperExchange(fast)
            service = PaperExecution(owner, venue)
            runner = StoredReplay(service, venue)
            feed = Feed(runner)
            at = AT + timedelta(seconds=2)
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
                value_row, cursor_row = await runner.history.latest(c), await runner._latest(c)
                assert value_row is not None and cursor_row is not None
                old = runner.history.restore(value_row)
                forged = old.model_copy(
                    update={
                        "number": source["number"],
                        "source_hash": source["fingerprint"],
                        "source_received_at": at,
                        "state": old.state.model_copy(update={"units": Decimal(9999), "as_of": at}),
                    }
                )
                await runner.history.save(c, forged, value_row["fingerprint"])
                cursor = runner._cursor(cursor_row).model_copy(
                    update={
                        "number": source["number"],
                        "source_hash": source["fingerprint"],
                    }
                )
                await runner._save(c, cursor, cursor_row["fingerprint"])
        for report_store in (store, fast):
            with pytest.raises(ValueError, match="balances/units do not reconstruct"):
                await historical_performance(report_store, end_source=baseline.number)
    finally:
        await store.engine.dispose()
