"""Manual earmarks preserve financial/risk history and cannot consume order holds."""

import argparse
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from test_historical_replay import latest
from test_paper_execution import AT, filled_position, market, signal
from test_stored_replay import Feed

from aca.domain.trading import (
    Balance,
    KillSwitchState,
    Order,
    OrderIntent,
    PortfolioSnapshot,
    fingerprint,
)
from aca.domain.values import FINANCIAL_CONTEXT
from aca.execution.paper import PaperExecution, SimulatedCrash
from aca.operations import paper as paper_cli
from aca.operations.cli import _parser
from aca.operations.config import Settings
from aca.portfolio.reserves import TaxReserveRecord, TaxReserveRequest, validate_reserve_transition
from aca.portfolio.state import free_cash, nav, reserved_cash, unit_price
from aca.reporting.historical import historical_performance
from aca.risk.execution import DispatchDenied
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.paper import events, journals, outbox
from aca.storage.paper_projection import delta
from aca.storage.reserves import for_event, insert_record, read_reserves, reserves

pytest_plugins = ("test_paper_execution", "test_stored_replay")
D = Decimal
REASON = "Synthetic manual tax cash allocation for fixture"


def request(version: int, amount: str = "100") -> TaxReserveRequest:
    return TaxReserveRequest(
        request_id=uuid4(),
        actor=uuid4(),
        expected_version=version,
        amount_gbp=D(amount),
        evidence_sha256="1" * 64,
        reason=REASON,
    )


@pytest.mark.parametrize("amount", [1.0, 1, True, "NaN", "Infinity", "-1", "1e20", "1e-19"])
def test_reserve_rejects_inexact_or_invalid_amounts(amount: Any) -> None:
    with pytest.raises(ValidationError):
        TaxReserveRequest.model_validate({**request(1).model_dump(), "amount_gbp": amount})


@pytest.mark.parametrize(
    "reason", ["too short", " padded reason here", "newline\nin reason", "x" * 241]
)
def test_reserve_rejects_invalid_reason(reason: str) -> None:
    with pytest.raises(ValidationError):
        TaxReserveRequest.model_validate({**request(1).model_dump(), "reason": reason})


@given(
    pounds=st.integers(1, 100_000),
    pending_percent=st.integers(0, 90),
    earmark_percent=st.integers(1, 100),
    halted=st.booleans(),
)
@settings(max_examples=1000, derandomize=True, deadline=None, print_blob=True)
def test_generated_reserves_preserve_nav_units_latches_and_order_cash(
    pounds: int, pending_percent: int, earmark_percent: int, halted: bool
) -> None:
    with localcontext(FINANCIAL_CONTEXT):
        cash = D(pounds)
        pending = cash * pending_percent / 100
        orders: tuple[Order, ...] = ()
        if pending:
            intent = OrderIntent(
                id=uuid4(),
                client_id=uuid4(),
                symbol="BTC/GBP",
                purpose="entry",
                side="buy",
                quantity=pending / 100,
                limit_price=D(100),
                stop_trigger=D(95),
                protective_limit=D(94),
                fee_bound=D(0),
                allowance_per_unit=D(0),
                created_at=AT,
                expires_at=AT + timedelta(seconds=2),
                strategy_version="slow-v1",
                rules_hash="a" * 64,
                episode_id=uuid4(),
                time_in_force="FOK",
            )
            orders = (Order(intent=intent, approval=None, state="UNKNOWN"),)
        old = PortfolioSnapshot(
            account_id=uuid4(),
            version=10,
            as_of=AT,
            balances=(Balance(asset="GBP", quantity=cash, cost_gbp=cash),),
            units=cash,
            kill=KillSwitchState(policy_epoch=uuid4(), hard=halted, soft=halted),
            orders=orders,
            high_water=D("1.1"),
            inception_high_water=D("1.2"),
            daily_day=AT.date(),
            daily_unit_price=D("1.05"),
        )
        amount = (cash - pending) * earmark_percent / 100
        record = TaxReserveRecord(
            **request(old.version, str(amount)).model_dump(),
            account_id=old.account_id,
            event_version=old.version + 1,
            previous_gbp=D(0),
            recorded_at=AT,
        )
        new = old.model_copy(update={"tax_reserve_gbp": amount, "version": 11})
        validate_reserve_transition(old, new, record)
        assert nav(old, AT) == nav(new, AT) == cash
        assert unit_price(old, AT) == unit_price(new, AT) == 1
        assert free_cash(new) == cash - pending - amount >= 0
        assert reserved_cash(new) == reserved_cash(old) == pending
        assert new.kill == old.kill and new.units == old.units
        excessive = record.model_copy(update={"amount_gbp": cash - pending + D("1e-18")})
        with pytest.raises(ValueError, match="uncommitted GBP"):
            validate_reserve_transition(
                old, new.model_copy(update={"tax_reserve_gbp": excessive.amount_gbp}), excessive
            )


async def counts(service: PaperExecution) -> tuple[int, ...]:
    async with service.writer.transaction() as c:
        return tuple(
            [
                int(
                    await c.scalar(
                        select(func.count())
                        .select_from(t)
                        .where(t.c.account_id == service.store.account_id)
                    )
                    or 0
                )
                for t in (events, journals, outbox, reserves)
            ]
        )


@pytest.mark.integration
async def test_reserve_is_atomic_audited_and_idempotent_after_a_later_decision(
    paper: tuple[PaperExecution, PaperExchange],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, venue = paper

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("reserve operation attempted a venue command")

    monkeypatch.setattr(venue, "submit", forbidden)
    monkeypatch.setattr(venue, "cancel", forbidden)
    old = await service._state()
    before = await counts(service)
    first = request(old.version)
    result = await service.reserve_tax_cash(first, at=AT)
    new = await service._state()
    assert new == old.model_copy(update={"tax_reserve_gbp": D(100), "version": old.version + 1})
    assert result["inserted"] and not result["financial_posting_created"]
    assert await counts(service) == (before[0] + 1, before[1], before[2] + 1, before[3] + 1)
    async with service.writer.transaction() as c:
        assert (await for_event(c, old.account_id, new.version)).request() == first
        assert await service.store.audit(c) == new
    second = request(new.version, "200")
    await service.reserve_tax_cash(second, at=AT)
    latest_state, saved_counts = await service._state(), await counts(service)
    duplicate = await service.reserve_tax_cash(first)  # DB time need not match original time.
    assert duplicate["decision"] == result["decision"] and duplicate["inserted"] is False
    assert D(str(duplicate["current_tax_cash_reserve_gbp"])) == 200
    assert await service._state() == latest_state and await counts(service) == saved_counts
    for field, value in (
        ("amount_gbp", D(101)),
        ("actor", uuid4()),
        ("reason", REASON + " changed"),
        ("evidence_sha256", "2" * 64),
        ("expected_version", new.version),
    ):
        with pytest.raises(ValueError, match="different decision"):
            await service.reserve_tax_cash(first.model_copy(update={field: value}), at=AT)
    assert await counts(service) == saved_counts


@pytest.mark.integration
async def test_reserve_blocks_spending_but_preserves_equity_sizing_and_safety_exits(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    old = await service._state()
    await service.reserve_tax_cash(request(old.version, "999.9"), at=AT)
    with pytest.raises(DispatchDenied):
        await service.queue(signal(), market(), AT, time_in_force="FOK")
    with pytest.raises(ValueError, match="reserved funds"):
        await service.cashflow("invalid-withdrawal", D(-2), AT)
    state = await service._state()
    await service.reserve_tax_cash(request(state.version, "100"), at=AT)
    await filled_position(service, venue)
    held = await service._state()
    assert held.tax_reserve_gbp == 100 and held.quantity("BTC") > 0
    assert (
        held.order(
            next(o.intent.client_id for o in held.orders if o.intent.purpose == "entry")
        ).filled
        > 0
    )
    at = AT + timedelta(milliseconds=400)
    exit_order = await service.exit_position("BTC/GBP", market(at), at)
    assert exit_order is not None
    at += timedelta(milliseconds=300)
    m = market(at)
    await venue.advance(m.book, m.rules, at)
    assert (await service.reconcile(at)).complete
    closed = await service._state()
    assert closed.quantity("BTC") == 0 and closed.tax_reserve_gbp == 100


@pytest.mark.integration
async def test_reserve_cannot_spend_pending_or_uncertain_order_holds_and_invalidates_approval(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    order = await service.queue(signal(), market(), AT)
    old = await service._state()
    available = old.quantity("GBP") - reserved_cash(old)
    before = await counts(service)
    with pytest.raises(ValueError, match="uncommitted GBP"):
        await service.reserve_tax_cash(request(old.version, str(available + D("1e-18"))), at=AT)
    assert await service._state() == old and await counts(service) == before
    await service.reserve_tax_cash(request(old.version, str(available)), at=AT)
    state = await service._state()
    assert free_cash(state) == 0 and reserved_cash(state) == reserved_cash(old)
    with pytest.raises(DispatchDenied):
        await service.dispatch(order.intent.client_id, market(), AT)
    assert not (await service.venue.snapshot(AT)).orders


@pytest.mark.integration
@pytest.mark.parametrize(
    "point", ["before_tax_reserve", "before_tax_reserve_commit", "after_tax_reserve_commit"]
)
async def test_reserve_crash_rolls_back_or_retries_exactly_once(
    paper: tuple[PaperExecution, PaperExchange],
    point: str,
) -> None:
    service, _ = paper
    old, before = await service._state(), await counts(service)
    decision = request(old.version)

    def crash(where: str) -> None:
        if where == point:
            raise SimulatedCrash(where)

    service.fault = crash
    with pytest.raises(SimulatedCrash):
        await service.reserve_tax_cash(decision, at=AT)
    committed = point == "after_tax_reserve_commit"
    assert (await service._state()).tax_reserve_gbp == (100 if committed else 0)
    if not committed:
        assert await counts(service) == before
    service.fault = lambda where: None
    result = await service.reserve_tax_cash(decision, at=AT)
    assert result["inserted"] is not committed
    assert await counts(service) == (before[0] + 1, before[1], before[2] + 1, before[3] + 1)


@pytest.mark.integration
async def test_halt_recovery_cashflows_and_release_do_not_erase_reserve_or_risk_history(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    await service.halt("Synthetic reviewed hard halt for reserve fixture", AT)
    old = await service._state()
    await service.reserve_tax_cash(request(old.version), at=AT)
    await service.cashflow("new-contribution", D(250), AT)
    assert (await service.recover(AT)).complete
    state = await service._state()
    assert state.tax_reserve_gbp == 100 and state.kill == old.kill
    await service.reserve_tax_cash(request(state.version, "0"), at=AT)
    released = await service._state()
    assert released.kill == old.kill and released.high_water == old.high_water
    assert released.inception_high_water == old.inception_high_water
    assert nav(released, AT) == 1250 and unit_price(released, AT) == 1
    with pytest.raises(DispatchDenied):
        await service.queue(signal(), market(), AT)


@pytest.mark.integration
async def test_stale_noop_bad_clock_and_exhausted_budget_have_no_side_effects(
    paper: tuple[PaperExecution, PaperExchange],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = paper
    old, before = await service._state(), await counts(service)
    for decision, at in (
        (request(old.version - 1), AT),
        (request(old.version, "0"), AT),
        (request(old.version), AT - timedelta(microseconds=1)),
        (request(old.version), datetime(2026, 10, 18)),
    ):
        with pytest.raises(ValueError):
            await service.reserve_tax_cash(decision, at=at)
    assert await service._state() == old and await counts(service) == before
    monkeypatch.setattr("aca.execution.paper.MAX_RESERVES", 0)
    with pytest.raises(ValueError, match="budget exhausted"):
        await service.reserve_tax_cash(request(old.version), at=AT)
    assert await service._state() == old and await counts(service) == before


@pytest.mark.integration
async def test_central_append_rejects_unreviewed_reserve_change(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    old = await service._state()
    new = old.model_copy(update={"version": old.version + 1, "tax_reserve_gbp": D(100)})
    for kind in ("tax_reserve_set", "valuation"):
        async with service.writer.transaction() as c:
            with pytest.raises(ValueError, match="tax cash reserve"):
                await service.store.append(c, old, new, source="forged", kind=kind)
    assert await service._state() == old


@pytest.mark.integration
async def test_database_rejects_unpaired_events_or_records_and_history_mutation(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    old = await service._state()
    new = old.model_copy(update={"version": old.version + 1, "tax_reserve_gbp": D(100)})
    for kind, doc in (
        ("tax_reserve_set", delta(old, new, None)),
        ("valuation", delta(old, new, None)),
        ("valuation", new.document()),
    ):
        async with service.writer.transaction() as c:
            previous = await c.scalar(
                select(events.c.fingerprint).where(
                    events.c.account_id == old.account_id, events.c.version == old.version
                )
            )
        source = "tax-reserve:" + str(uuid4())
        with pytest.raises(DBAPIError):
            async with service.store.engine.begin() as c:
                await c.execute(
                    events.insert().values(
                        account_id=old.account_id,
                        version=new.version,
                        id=uuid4(),
                        source=source,
                        kind=kind,
                        at=AT,
                        previous_hash=previous,
                        fingerprint=fingerprint(
                            {
                                "previous": previous,
                                "version": new.version,
                                "kind": kind,
                                "source": source,
                                "state": doc,
                            }
                        ),
                        document=doc,
                    )
                )
    wrong_record = TaxReserveRecord(
        **request(old.version - 1).model_dump(),
        account_id=old.account_id,
        event_version=old.version,
        previous_gbp=D(0),
        recorded_at=AT,
    )
    with pytest.raises(DBAPIError):
        async with service.store.engine.begin() as c:
            await insert_record(c, wrong_record)
    await service.reserve_tax_cash(request(old.version), at=AT)
    for statement in (reserves.update().values(amount_gbp=D(0)), reserves.delete()):
        with pytest.raises(DBAPIError):
            async with service.store.engine.begin() as c:
                await c.execute(statement.where(reserves.c.account_id == old.account_id))
    async with service.writer.transaction() as c:
        assert await service.store.audit(c) == await service.store.load(c)


@pytest.mark.integration
async def test_reserve_cli_database_clock_and_read_only_paginated_query(
    paper: tuple[PaperExecution, PaperExchange],
    database_settings: Settings,
) -> None:
    service, _ = paper
    # The reader can run while another writer owns the account.
    first = await read_reserves(service.store.engine, service.store.account_id, limit=1)
    assert first["items"] == [] and first["next_after_version"] is None
    old = await service._state()
    decision = request(old.version)
    args = _parser().parse_args(
        [
            "paper",
            "tax-reserve",
            "--account",
            str(old.account_id),
            "--request-id",
            str(decision.request_id),
            "--actor",
            str(decision.actor),
            "--expected-version",
            str(old.version),
            "--amount-gbp",
            "100",
            "--evidence-sha256",
            decision.evidence_sha256,
            "--reason",
            decision.reason,
        ]
    )
    # Release the fixture owner before invoking the actual CLI service.
    await service.writer.__aexit__()
    result = await paper_cli.execute(args, database_settings)
    assert isinstance(result, dict) and result["inserted"] is True
    history = await paper_cli.execute(
        argparse.Namespace(
            action="tax-reserves",
            account=old.account_id,
            after_version=0,
            limit=1,
        ),
        database_settings,
    )
    assert isinstance(history, dict) and history["state_version"] == old.version + 1
    assert D(str(history["manual_tax_cash_reserve_gbp"])) == 100
    assert isinstance(history["items"], list) and len(history["items"]) == 1
    record = TaxReserveRecord.restore(history["items"][0])
    assert record.recorded_at >= old.as_of and record.request() == decision
    assert history["tax_estimate"] is False and history["live_execution_available"] is False
    empty = await read_reserves(
        service.store.engine, old.account_id, after_version=record.event_version
    )
    assert empty["items"] == []


@pytest.mark.integration
async def test_reserve_replay_and_report_preserve_returns_and_disclose_earmark(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    old = await runner.service._state()
    historical_before = await latest(runner)
    decision = request(old.version)
    at = AT + timedelta(milliseconds=100)
    await runner.service.reserve_tax_cash(decision, at=at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    value = await latest(runner)
    assert value.state.tax_reserve_gbp == 100 and value.nav_gbp == historical_before.nav_gbp
    assert value.state.units == historical_before.state.units
    assert value.state.kill == historical_before.state.kill
    report = await historical_performance(runner.store)
    period = report["period"]
    assert isinstance(period, dict)
    assert D(period["twr"]) == D(period["pnl_gbp"]) == 0
    assert period["investment_flow_count"] == 0
    assert D(period["closing"]["manual_tax_cash_reserve_gbp"]) == 100
    # A later contribution is still a flow; the reserve remains a cash restriction.
    await runner.service.cashflow("later-capital", D(100), at)
    await feed.marker("heartbeat", at + timedelta(milliseconds=100))
    await runner.batch(at=at + timedelta(milliseconds=100))
    report = await historical_performance(runner.store)
    assert isinstance(report["period"], dict)
    assert D(report["period"]["twr"]) == D(report["period"]["pnl_gbp"]) == 0
    assert report["period"]["investment_flow_count"] == 1


@pytest.mark.integration
async def test_reserve_refuses_undrained_replay(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    old = await runner.service._state()
    await feed.marker("heartbeat", AT + timedelta(milliseconds=100))
    with pytest.raises(ValueError, match="drained replay"):
        await runner.service.reserve_tax_cash(request(old.version), at=AT)
    assert await runner.service._state() == old


@pytest.mark.integration
async def test_unknown_submission_stays_reserved_and_cannot_be_rearmed_by_a_release(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT)

    def crash(where: str) -> None:
        if where == "after_submitting_before_send":
            raise SimulatedCrash(where)

    service.fault = crash
    with pytest.raises(SimulatedCrash):
        await service.dispatch(order.intent.client_id, market(), AT)
    service.fault = lambda where: None
    assert not (await service.recover(AT)).complete
    old = await service._state()
    held = reserved_cash(old)
    assert held > 0 and old.order(order.intent.client_id).state == "UNKNOWN"
    available = old.quantity("GBP") - held
    with pytest.raises(ValueError, match="uncommitted GBP"):
        await service.reserve_tax_cash(request(old.version, str(available + D("1e-18"))), at=AT)
    await service.reserve_tax_cash(request(old.version, "100"), at=AT)
    state = await service._state()
    await service.reserve_tax_cash(request(state.version, "0"), at=AT)
    final = await service._state()
    assert final.orders == old.orders and reserved_cash(final) == held
    assert final.kill == old.kill and final.incident == old.incident and not final.ready
    with pytest.raises(DispatchDenied):
        await service.queue(signal(), market(), AT)
    assert not (await venue.snapshot(AT)).orders


@pytest.mark.integration
async def test_unposted_authoritative_fill_blocks_a_reserve_until_recovered(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT, time_in_force="FOK")

    def crash(where: str) -> None:
        if where == "after_send_before_ack":
            raise SimulatedCrash(where)

    service.fault = crash
    with pytest.raises(SimulatedCrash):
        await service.dispatch(order.intent.client_id, market(), AT)
    service.fault = lambda where: None
    at = AT + timedelta(milliseconds=300)
    m = market(at)
    await venue.advance(m.book, m.rules, at)
    old, before = await service._state(), await counts(service)
    assert (await venue.snapshot(at)).fills
    with pytest.raises(ValueError, match="reconciled fills"):
        await service.reserve_tax_cash(request(old.version), at=at)
    assert await service._state() == old and await counts(service) == before
    assert (await service.recover(at)).complete
    current = await service._state()
    await service.reserve_tax_cash(request(current.version), at=at)
    assert (await service._state()).tax_reserve_gbp == 100


@pytest.mark.integration
async def test_reserve_history_pages_retain_released_decisions_without_writes(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    old = await service._state()
    await service.reserve_tax_cash(request(old.version), at=AT)
    new = await service._state()
    await service.reserve_tax_cash(request(new.version, "0"), at=AT)
    before = await counts(service)
    first = await read_reserves(service.store.engine, old.account_id, limit=1)
    assert first["next_after_version"] == new.version
    second = await read_reserves(
        service.store.engine, old.account_id, after_version=new.version, limit=1
    )
    assert second["next_after_version"] is None
    assert isinstance(second["items"], list) and len(second["items"]) == 1
    assert D(second["items"][0]["amount_gbp"]) == 0
    assert D(str(first["manual_tax_cash_reserve_gbp"])) == 0
    for bad_after, bad_limit in ((-1, 1), (0, 0), (0, 101)):
        with pytest.raises(ValueError, match="budget"):
            await read_reserves(
                service.store.engine, old.account_id, after_version=bad_after, limit=bad_limit
            )
    assert await counts(service) == before
