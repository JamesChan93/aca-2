"""Checkpoint rollover, immutable prefixes, rollback and legacy event migration."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from aca.domain.trading import Balance, KillSwitchState, PortfolioSnapshot, fingerprint
from aca.execution.paper import PaperExecution
from aca.operations.config import Settings
from aca.simulation.paper import PaperExchange
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter, accounts, checkpoints, events
from aca.storage.paper_projection import CHECKPOINT_INTERVAL, delta, restore_event

AT = datetime(2026, 10, 18, tzinfo=UTC)
D = Decimal


def opening() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id=uuid4(),
        version=1,
        as_of=AT,
        balances=tuple(
            Balance(asset=a, quantity=D(0), cost_gbp=D(0)) for a in ("GBP", "BTC", "ETH")
        ),
        kill=KillSwitchState(policy_epoch=uuid4()),
    )


def test_delta_rejects_changed_meaning_and_result_hash() -> None:
    state = opening()
    first = delta(None, state, None)
    assert restore_event(None, first) == state
    next_state = state.model_copy(update={"version": 2, "as_of": AT + timedelta(seconds=1)})
    patch = delta(state, next_state, None)
    changes = patch["changes"]
    assert isinstance(changes, dict) and set(changes) == {"version", "as_of"}
    assert restore_event(state, patch) == next_state
    assert restore_event(None, state.document()) == state  # legacy format
    for changed in (
        {**patch, "format": "future"},
        {**patch, "state_hash": "0" * 64},
        {**patch, "extra": True},
        {**patch, "changes": {"units": 0.1}},
        {**patch, "journal_id": "invalid"},
    ):
        with pytest.raises(ValueError):
            restore_event(state, changed)


@pytest.mark.integration
async def test_checkpoint_rollover_fast_load_audit_and_atomic_rollback(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            state = await service.initialise(AT)
            state = await service.cashflow("opening", D(1000), AT)
            for number in range(2 * CHECKPOINT_INTERVAL + 3):
                state = await service._save(
                    state, state, at=AT + timedelta(milliseconds=number), kind="observation"
                )
            async with writer.transaction() as c:
                assert await store.audit(c) == await store.load(c) == state
                assert (
                    await c.scalar(
                        select(func.count())
                        .select_from(checkpoints)
                        .where(checkpoints.c.account_id == store.account_id)
                    )
                    == 3
                )
                row = (
                    await c.execute(
                        select(events.c.document).where(
                            events.c.account_id == store.account_id,
                            events.c.version == state.version,
                        )
                    )
                ).scalar_one()
                assert set(row["changes"]) == {"version", "as_of"}
            queries: list[str] = []

            def capture_sql(*args: object) -> None:
                queries.append(str(args[2]))

            event.listen(engine.sync_engine, "before_cursor_execute", capture_sql)
            try:
                assert await service._state() == state
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture_sql)
            assert not any("paper_journal" in query or "SUM(" in query for query in queries)
            assert all("LIMIT" in q or "version =" in q for q in queries if "paper_events" in q)
            # Rollback at a checkpoint boundary cannot leave an ahead-of-state anchor.
            with pytest.raises(RuntimeError, match="crash"):
                async with writer.transaction() as c:
                    staged = state
                    while staged.version % CHECKPOINT_INTERVAL:
                        new = staged.model_copy(update={"version": staged.version + 1})
                        await store.append(
                            c, staged, new, source=f"rollback:{new.version}", kind="observation"
                        )
                        staged = new
                    raise RuntimeError("crash")
            assert await service._state() == state
        for command in (
            "DELETE FROM aca.paper_checkpoints",
            "TRUNCATE aca.paper_checkpoints",
            "UPDATE aca.paper_checkpoints SET version=version",
        ):
            with pytest.raises(DBAPIError):
                async with engine.begin() as c:
                    await c.execute(text(command))
        # Reacquiring ownership performs a full prefix audit before any command.
        async with PaperWriter(store) as second:
            async with second.transaction() as c:
                assert await store.load(c) == state
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_corrupt_checkpoint_never_grants_writer_ownership(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as owner:
            service = PaperExecution(owner, PaperExchange(store))
            await service.initialise(AT)
            state = await service.cashflow("opening", D(250), AT)
            async with owner.transaction() as c:
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
                bad = state.document()
                bad["units"] = "9999"
                # A raw INSERT can reference an existing hash, but cannot forge
                # the bytes that hash describes. The loader checks both.
                await c.execute(
                    checkpoints.insert().values(
                        account_id=store.account_id,
                        version=state.version,
                        event_hash=anchor["fingerprint"],
                        state_hash=anchor["document"]["state_hash"],
                        document=bad,
                    )
                )
            with pytest.raises(ValueError, match="corrupt paper checkpoint"):
                await service._state()
        with pytest.raises(ValueError, match="corrupt paper checkpoint"):
            async with PaperWriter(store):
                pytest.fail("corrupt checkpoint cannot acquire a writer")
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_journal_free_forgery_cannot_hide_behind_a_restored_final_balance(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as owner:
            service = PaperExecution(owner, PaperExchange(store))
            original = await service.initialise(AT)
            original = await service.cashflow("opening", D(250), AT)
            async with owner.transaction() as c:
                previous = await c.scalar(
                    select(events.c.fingerprint).where(
                        events.c.account_id == store.account_id,
                        events.c.version == original.version,
                    )
                )
                state = original
                # Correctly hashed state changes invent then remove inventory.
                # The final projection/SQL ledger agree, so only checking that
                # final state would miss the intermediate journal-free creation.
                for forged in (True, False):
                    changed = state.model_copy(
                        update={
                            "version": state.version + 1,
                            "balances": tuple(
                                Balance(asset="BTC", quantity=D(1), cost_gbp=D(10))
                                if forged and b.asset == "BTC"
                                else b
                                for b in original.balances
                            ),
                        }
                    )
                    document = delta(state, changed, None)
                    source = f"forged-no-journal:{changed.version}"
                    hashed = fingerprint(
                        {
                            "previous": previous,
                            "version": changed.version,
                            "kind": "valuation",
                            "source": source,
                            "state": document,
                        }
                    )
                    await c.execute(
                        events.insert().values(
                            account_id=store.account_id,
                            version=changed.version,
                            id=uuid4(),
                            source=source,
                            kind="valuation",
                            at=AT,
                            previous_hash=previous,
                            fingerprint=hashed,
                            document=document,
                        )
                    )
                    previous, state = hashed, changed
                assert await store.ledger_balances(c) == state.balances
                with pytest.raises(ValueError, match="journal and incremental projection differ"):
                    await store.audit(c)
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_legacy_full_state_prefix_gets_first_checkpoint_without_rewrite(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    state = opening()
    store = PaperStore(engine, state.account_id)
    old_document = state.document()
    old_hash = fingerprint(
        {
            "previous": "0" * 64,
            "version": 1,
            "kind": "initialised",
            "source": "initialise",
            "state": old_document,
        }
    )
    try:
        async with engine.begin() as c:
            await c.execute(
                accounts.insert().values(id=store.account_id, created_at=AT, mode="paper")
            )
            await c.execute(
                events.insert().values(
                    account_id=store.account_id,
                    version=1,
                    id=uuid4(),
                    source="initialise",
                    kind="initialised",
                    at=AT,
                    previous_hash="0" * 64,
                    fingerprint=old_hash,
                    document=old_document,
                )
            )
        async with PaperWriter(store) as owner:
            service = PaperExecution(owner, PaperExchange(store))
            assert await service._state() == state
            new = await service.cashflow("opening", D(250), AT)
            async with owner.transaction() as c:
                assert await store.audit(c) == await store.load(c) == new
                assert (
                    await c.scalar(
                        select(checkpoints.c.version).where(
                            checkpoints.c.account_id == store.account_id
                        )
                    )
                    == 2
                )
                assert (
                    await c.scalar(
                        select(events.c.document).where(
                            events.c.account_id == store.account_id, events.c.version == 1
                        )
                    )
                    == old_document
                )
                with pytest.raises(DBAPIError):
                    await c.execute(
                        checkpoints.insert().values(
                            account_id=store.account_id,
                            version=1,
                            event_hash=old_hash,
                            state_hash=fingerprint(old_document),
                            document=old_document,
                        )
                    )
    finally:
        await engine.dispose()
