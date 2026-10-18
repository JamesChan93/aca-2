"""Historical risk must see intra-batch losses that recovery-time marks conceal."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_paper_execution import signal
from test_stored_replay import AT, Feed, evidence, passive

from aca.domain.trading import TERMINAL, Valuation
from aca.execution.paper import SimulatedCrash
from aca.simulation.historical import HistoricalValuation
from aca.simulation.stored_replay import StoredReplay
from aca.storage.replay import valuations

pytest_plugins = ("test_stored_replay",)
pytestmark = pytest.mark.integration
D = Decimal


async def latest(runner: StoredReplay) -> HistoricalValuation:
    async with runner.service.writer.transaction() as c:
        row = await runner.history.latest(c)
        assert row is not None
        return runner.history.restore(row)


async def fok(runner: StoredReplay, feed: Feed) -> UUID:
    order = await runner.service.queue(
        signal(), evidence(AT, feed.epoch, depth="10"), AT, time_in_force="FOK"
    )
    await runner.service.dispatch(order.intent.client_id, evidence(AT, feed.epoch, depth="10"), AT)
    at = AT + timedelta(milliseconds=300)
    await feed.book(at, depth="10")
    await runner.batch(at=at)
    return order.intent.client_id


async def test_original_fill_fees_and_bid_nav_precede_financial_import(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    ident = await fok(runner, feed)
    value = await latest(runner)
    assert value.state.as_of == AT + timedelta(milliseconds=300)
    remote = await runner.venue.snapshot(value.state.as_of)
    fill = remote.fills[0]
    expected_cash = D(1000) - fill.quantity * fill.price - fill.fees[0].quantity
    assert value.state.quantity("GBP") == expected_cash
    assert value.state.quantity("BTC") == fill.quantity
    assert value.nav_gbp == expected_cash + fill.quantity * D(999)
    assert value.state.units == D(1000)
    assert value.state.last_unit_price < 1
    assert value.state.marks[0].source.startswith("replay-book:")
    # Operational accounting still imports the canonical fill separately.
    assert (await runner.service._state()).quantity("BTC") == 0
    assert (await runner.service.reconcile(value.state.as_of)).complete
    assert (await runner.service._state()).order(ident).filled == fill.quantity
    assert (await latest(runner)) == value


async def test_intra_batch_peak_crash_and_rebound_cannot_hide_hard_halt(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    peak, crash, rebound = (AT + timedelta(milliseconds=n) for n in (600, 700, 1000))
    await feed.book(peak, bid="9999", ask="10000")
    await feed.book(crash, bid="1", ask="2")
    await feed.book(rebound)
    result = await runner.batch(at=AT + timedelta(milliseconds=1100))
    assert result.processed == 3
    historical = await latest(runner)
    current = await runner.service._state()
    assert historical.state.kill.hard and current.kill.hard
    assert historical.state.kill.latched_at == crash
    assert current.kill.latched_at == crash
    assert current.as_of == AT + timedelta(milliseconds=1100)  # No operational rewind.
    assert historical.state.inception_high_water > D("1.5")
    assert historical.nav_gbp is not None and historical.nav_gbp > D(990)
    assert not historical.incomplete_since
    assert not historical.promotion_evidence
    snapshot = await runner.venue.snapshot(rebound)
    protection = [o for o in snapshot.orders if o.intent.purpose == "protection"]
    assert len(protection) == 1 and protection[0].state not in TERMINAL
    assert protection[0].triggered  # The gap through its limit did not fill.


async def test_daily_loss_cancels_remaining_entry_before_later_replay_trade(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    ident = await passive(runner, feed)
    first = AT + timedelta(milliseconds=400)
    await feed.trade(first, size="4")
    await runner.batch(at=first)
    before = await runner.venue.snapshot(first)
    entry = next(o for o in before.orders if o.intent.client_id == ident)
    assert D("0.02") < entry.filled < entry.intent.quantity
    loss, later = (AT + timedelta(milliseconds=n) for n in (500, 600))
    await feed.book(loss, bid="1", ask="2")
    await feed.trade(later, size="100")
    await runner.batch(at=later)
    snapshot = await runner.venue.snapshot(later)
    entry = next(o for o in snapshot.orders if o.intent.client_id == ident)
    assert entry.state == "CANCELLED" and entry.cancelled_at == loss
    assert len(snapshot.fills) == 1
    assert (await latest(runner)).state.kill.daily_day == AT.date()
    assert (await runner.service._state()).kill.daily_day == AT.date()
    assert any(
        o.intent.purpose == "protection" and o.state not in TERMINAL for o in snapshot.orders
    )


@pytest.mark.parametrize("point", ["before_cursor", "before_commit", "after_commit"])
async def test_historical_state_risk_and_cursor_commit_together(
    replay: tuple[StoredReplay, Feed], point: str
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    before = await latest(runner)
    await feed.book(AT + timedelta(milliseconds=400), bid="9999", ask="10000")
    at = AT + timedelta(milliseconds=500)
    await feed.book(at, bid="1", ask="2")

    def crash(where: str) -> None:
        if where == point:
            raise SimulatedCrash()

    runner.fault = crash
    with pytest.raises(SimulatedCrash):
        await runner.batch(at=at)
    assert ((await latest(runner)).state.kill.hard) == (point == "after_commit")
    assert (await runner.service._state()).kill.hard == (point == "after_commit")
    if point != "after_commit":
        assert (await latest(runner)) == before
    recovered = StoredReplay(runner.service, runner.venue)
    await recovered.audit()
    await recovered.batch(at=at)
    assert (await latest(recovered)).state.kill.hard
    assert (await runner.service._state()).kill.hard
    async with runner.service.writer.transaction() as c:
        count = await c.scalar(
            select(func.count())
            .select_from(valuations)
            .where(
                valuations.c.account_id == runner.store.account_id,
                valuations.c.number > before.number,
            )
        )
        assert count == 2


async def test_deposit_reconstructs_units_without_hiding_loss_or_changing_high_water(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    at = AT + timedelta(milliseconds=300)
    await runner.service.mark(
        (Valuation(asset="BTC", bid_gbp=D(999), observed_at=at, source="fixture"),), at
    )
    assert (await runner.service.reconcile(at)).complete
    before = await latest(runner)
    at += timedelta(milliseconds=10)
    await runner.service.cashflow("monthly", D(250), at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    after = await latest(runner)
    assert after.state.units == (await runner.service._state()).units
    assert abs(after.state.last_unit_price - before.state.last_unit_price) <= D("1e-18")
    assert after.state.high_water == before.state.high_water
    assert after.nav_gbp == before.nav_gbp + D(250)  # type: ignore[operator]
    await runner.batch(at=at)
    assert await latest(runner) == after


async def test_gap_with_holdings_keeps_incomplete_history_after_feed_recovers(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    gap = AT + timedelta(milliseconds=400)
    await feed.marker("coverage_gap", gap)
    await runner.batch(at=gap)
    incomplete = await latest(runner)
    assert incomplete.nav_gbp is None and incomplete.incomplete_since == gap
    assert incomplete.reason is not None
    at = AT + timedelta(milliseconds=500)
    await feed.bootstrap(at)
    value = await latest(runner)
    assert value.nav_gbp is not None and value.reason is None
    assert value.incomplete_since == gap


async def test_malformed_historical_chain_is_append_only_and_blocks_recovery(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    for mutation in ("DELETE FROM", "TRUNCATE", "UPDATE"):
        async with runner.store.engine.begin() as c:
            suffix = " SET fingerprint = 'bad'" if mutation == "UPDATE" else ""
            with pytest.raises(DBAPIError):
                await c.execute(text(f"{mutation} aca.paper_replay_valuations{suffix}"))
    value = await latest(runner)
    async with runner.store.engine.begin() as c:
        await c.execute(
            valuations.insert().values(
                account_id=runner.store.account_id,
                number=value.number + 1,
                previous_hash="0" * 64,
                fingerprint="f" * 64,
                document=value.document(),
            )
        )
    with pytest.raises(ValueError, match="corrupt historical"):
        await runner.audit()


async def test_empty_batch_validates_historical_hash_and_policy(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    runner.history.policy_hash = "f" * 64
    with pytest.raises(ValueError, match="policy mismatch"):
        await runner.batch(at=AT)


@pytest.mark.parametrize("unposted", [False, True])
async def test_cash_flow_cannot_overtake_unread_evidence_or_unposted_fills(
    replay: tuple[StoredReplay, Feed], unposted: bool
) -> None:
    runner, feed = replay
    if unposted:
        await fok(runner, feed)
    else:
        await feed.book(AT + timedelta(milliseconds=300))
    with pytest.raises(ValueError, match="drained replay and reconciled fills"):
        await runner.service.cashflow("unsafe", D(100), AT + timedelta(milliseconds=400))
    assert (await runner.service._state()).units == D(1000)


@pytest.mark.parametrize("amount", [D(250), D(-250)])
async def test_flat_contributions_and_withdrawals_preserve_unit_price(
    replay: tuple[StoredReplay, Feed], amount: Decimal
) -> None:
    runner, feed = replay
    at = AT + timedelta(milliseconds=100)
    await runner.service.cashflow("flow", amount, at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    value = await latest(runner)
    assert value.state.units == D(1000) + amount
    assert value.state.last_unit_price == 1 and value.state.high_water == 1
    assert value.nav_gbp == D(1000) + amount
    assert not value.incomplete_since


async def test_manual_halt_and_review_are_applied_at_their_original_times(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    at = AT + timedelta(milliseconds=100)
    await runner.service.halt("operator review fixture", at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    halted = await latest(runner)
    assert halted.state.kill.hard and halted.state.kill.latched_at == at
    at += timedelta(milliseconds=100)
    await runner.service.review("Reviewed all paper account evidence", at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    reviewed = await latest(runner)
    assert not reviewed.state.kill.hard
    assert reviewed.state.kill.policy_epoch != halted.state.kill.policy_epoch
    assert reviewed.state.inception_high_water == halted.state.inception_high_water
    assert reviewed.state.kill.policy_epoch == (await runner.service._state()).kill.policy_epoch
    await runner.audit()


async def test_midnight_gap_suspends_daily_risk_without_inventing_a_midnight_mark(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    value = await latest(runner)
    yesterday = AT - timedelta(days=1)
    value = value.model_copy(
        update={
            "state": value.state.model_copy(
                update={"as_of": yesterday, "daily_day": yesterday.date()}
            )
        }
    )
    at = AT + timedelta(seconds=1)
    value = runner.history.observe(value, at, (), "replay_evidence_unavailable")
    assert value.state.kill.daily_day == at.date()
    assert value.state.kill.reason == "missing_utc_midnight_mark"
    assert value.incomplete_since == at


async def test_exact_midnight_cash_observation_sets_daily_baseline(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    value = await latest(runner)
    yesterday = AT - timedelta(days=1)
    value = value.model_copy(
        update={
            "state": value.state.model_copy(
                update={"as_of": yesterday, "daily_day": yesterday.date()}
            )
        }
    )
    at = AT
    value = runner.history.observe(value, at, (), "replay_evidence_unavailable")
    assert value.state.daily_day == at.date()
    assert value.state.kill.daily_day is None
    assert value.incomplete_since is None


async def test_withdrawal_after_loss_cannot_remove_latched_historical_risk(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    at = AT + timedelta(milliseconds=600)
    await feed.book(at, bid="1", ask="2")
    await runner.batch(at=at)
    await runner.service.mark(
        (Valuation(asset="BTC", bid_gbp=D(1), observed_at=at, source="fixture"),), at
    )
    assert (await runner.service.reconcile(at)).complete
    before = await latest(runner)
    assert before.state.kill.soft
    at += timedelta(milliseconds=10)
    await runner.service.cashflow("withdraw", D(-100), at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    after = await latest(runner)
    assert after.state.kill.soft
    assert after.state.high_water == before.state.high_water
    assert abs(after.state.last_unit_price - before.state.last_unit_price) <= D("1e-18")


async def test_legacy_flat_account_requires_explicit_historical_baseline(
    replay: tuple[StoredReplay, Feed],
) -> None:
    from uuid import uuid4

    from aca.execution.paper import PaperExecution
    from aca.simulation.paper import PaperExchange
    from aca.storage.paper import PaperStore, PaperWriter
    from aca.storage.replay import cursors

    original, _ = replay
    store = PaperStore(original.store.engine, uuid4())
    async with PaperWriter(store) as writer:
        venue = PaperExchange(store)
        service = PaperExecution(writer, venue)
        await service.initialise(AT)
        await service.cashflow("opening", D(1000), AT)
        current = await original.audit()
        legacy = current.model_copy(update={"account_id": store.account_id})
        runner = StoredReplay(service, venue)
        async with writer.transaction() as c:
            await runner._save(c, legacy, "0" * 64)
        with pytest.raises(ValueError, match="baseline"):
            await runner.batch(at=AT)
        assert await runner.enrol() == legacy
        assert (await latest(runner)).state.quantity("GBP") == 1000
        await runner.audit()
        async with writer.transaction() as c:
            count = await c.scalar(
                select(func.count())
                .select_from(cursors)
                .where(cursors.c.account_id == store.account_id)
            )
            assert count == 1  # No cursor reset or replacement.


async def test_valuation_query_is_bounded_read_only_and_cursor_paginated(
    replay: tuple[StoredReplay, Feed], database_settings: object
) -> None:
    import argparse

    from aca.operations.config import Settings
    from aca.operations.paper import execute

    assert isinstance(database_settings, Settings)
    runner, _ = replay
    args = argparse.Namespace(
        action="valuations", account=runner.store.account_id, limit=2, before=None
    )
    result = await execute(args, database_settings)
    assert isinstance(result, dict)
    items = result["items"]
    assert isinstance(items, list) and len(items) == 2
    assert items[0]["number"] > items[1]["number"]
    assert result["provisional"] and not result["promotion_evidence"]
    args.before = result["next_before"]
    page = await execute(args, database_settings)
    assert isinstance(page, dict) and isinstance(page["items"], list)
    assert page["items"][0]["number"] < items[-1]["number"]


async def test_epoch_review_cannot_skip_unread_replay_evidence(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    epoch = (await runner.service._state()).kill.policy_epoch
    at = AT + timedelta(milliseconds=100)
    await feed.book(at)
    with pytest.raises(ValueError, match="drained replay"):
        await runner.service.review("Reviewed all paper account evidence", at)
    assert (await runner.service._state()).kill.policy_epoch == epoch


async def test_historical_restore_rejects_inconsistent_nav_even_with_valid_hash(
    replay: tuple[StoredReplay, Feed],
) -> None:
    from aca.domain.trading import fingerprint

    runner, _ = replay
    async with runner.service.writer.transaction() as c:
        row = await runner.history.latest(c)
        assert row is not None
        corrupt = dict(row)
        document = {**row["document"], "nav_gbp": "999999"}
        corrupt.update(
            document=document,
            fingerprint=fingerprint({"previous": row["previous_hash"], "valuation": document}),
        )
        with pytest.raises(ValueError, match="NAV does not match"):
            runner.history.restore(corrupt)  # type: ignore[arg-type]


async def test_midnight_baseline_propagates_without_replacing_operator_halt_reason(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    old = await runner.service._state()
    yesterday = (AT - timedelta(days=1)).date()
    await runner.service._save(
        old,
        old.model_copy(
            update={
                "daily_day": yesterday,
                "kill": old.kill.model_copy(
                    update={
                        "hard": True,
                        "soft": True,
                        "daily_day": yesterday,
                        "reason": "operator incident",
                        "latched_at": AT,
                        "actor": "operator",
                    }
                ),
            }
        ),
        at=AT,
        kind="hard_halt",
    )
    value = await latest(runner)
    async with runner.service.writer.transaction() as c:
        await runner.history.propagate(c, value, AT)
    state = await runner.service._state()
    assert state.daily_day == AT.date() and state.kill.daily_day is None
    assert state.kill.hard and state.kill.reason == "operator incident"
    await runner.service.mark((), AT + timedelta(seconds=1))
    assert (await runner.service._state()).kill.reason == "operator incident"


async def test_contribution_preserves_all_stored_decimal_places_outside_ambient_context(
    replay: tuple[StoredReplay, Feed],
) -> None:
    from decimal import localcontext

    from aca.domain.values import FINANCIAL_CONTEXT

    runner, feed = replay
    at = AT + timedelta(milliseconds=100)
    amount = D("10000000000000.123456789012345678")
    with localcontext(FINANCIAL_CONTEXT):
        expected = D(1000) + amount
        await runner.service.cashflow("precision-boundary", amount, at)
    # Historical replay executes outside that caller's financial context. Reading
    # a single authoritative GBP movement must not round it through a sum.
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    value = await latest(runner)
    assert value.state.units == expected
    assert value.nav_gbp == expected
    assert value.state.last_unit_price == 1
