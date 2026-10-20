"""Original-time report arithmetic and read-only evidence boundaries on PostgreSQL."""

import argparse
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from test_historical_replay import fok, latest
from test_stored_replay import AT, Feed, evidence

from aca.accounting.ledger import stored
from aca.domain.trading import Valuation, fingerprint
from aca.domain.values import FINANCIAL_CONTEXT
from aca.operations import paper
from aca.operations.config import Settings
from aca.reporting import historical
from aca.reporting.historical import historical_performance
from aca.simulation.stored_replay import StoredReplay
from aca.storage.paper import PaperStore, PaperWriter

pytest_plugins = ("test_stored_replay",)
pytestmark = pytest.mark.integration
D = Decimal


async def report(runner: StoredReplay, **bounds: int) -> dict[str, Any]:
    return await historical_performance(runner.store, **bounds)


async def reconcile(runner: StoredReplay, at: Any, bid: str = "999") -> None:
    await runner.service.mark(
        (Valuation(asset="BTC", bid_gbp=D(bid), observed_at=at, source="fixture"),), at
    )
    assert (await runner.service.reconcile(at)).complete


async def test_cash_only_report_is_deterministic_and_needs_no_writer_lock(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    before = await runner.store.status()
    result = await report(runner)  # The fixture's execution writer remains active.
    period = result["period"]
    assert period["opening"]["nav_gbp"] == "1000.000000000000000000"
    assert D(period["pnl_gbp"]) == D(period["fees_gbp"]) == D(period["twr"]) == 0
    assert period["investment_flow_count"] == 0  # Opening funding precedes enrolment.
    assert period["mwr_annualised"] is None and period["mwr_unavailable_reason"] == "zero_duration"
    assert period["observations"] == 6 and period["valuation_complete"]
    assert result["provisional"] and not result["promotion_evidence"]
    assert not result["live_execution_available"] and not result["tax_report_final"]
    assert result == await report(runner)
    assert result["report_hash"] == fingerprint(
        {k: v for k, v in result.items() if k != "report_hash"}
    )
    assert await runner.store.status() == before


async def test_fill_fees_pnl_and_mark_provenance_reconstruct_without_processing_time_quotes(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    with pytest.raises(ValueError, match="reconciled historical fills"):
        await report(runner)
    at = AT + timedelta(milliseconds=300)
    await reconcile(runner, at)
    result = await report(runner)
    period = result["period"]
    remote = await runner.venue.snapshot(at)
    fill = remote.fills[0]
    expected_fee = sum(f.quantity * f.rate_gbp for f in fill.fees)
    expected_pnl = fill.quantity * (D(999) - fill.price) - expected_fee
    assert D(period["fees_gbp"]) == expected_fee
    assert D(period["pnl_gbp"]) == expected_pnl
    assert D(period["twr"]) == stored(expected_pnl / 1000)
    assert period["trade_fill_count"] == 1
    assert period["closing"]["marks"][0]["source"].startswith("replay-book:")
    assert period["closing"]["economic_as_of"] == at.isoformat()
    assert D(period["max_observed_drawdown"]) == -D(period["twr"])
    # A later operational mark cannot rewrite the historical bid or report P&L.
    await runner.service.mark(
        (Valuation(asset="BTC", bid_gbp=D(1001), observed_at=at, source="later"),), at
    )
    later = await report(runner)
    assert later["period"] == period
    assert later["financial_head_version"] > result["financial_head_version"]


async def test_exact_subperiod_excludes_opening_fill_and_includes_later_deposit_and_withdrawal(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    at = AT + timedelta(milliseconds=300)
    await reconcile(runner, at)
    start = await latest(runner)
    for number, amount in enumerate(("250", "-100"), start=1):
        when = at + timedelta(milliseconds=number * 10)
        await runner.service.cashflow(f"flow-{number}", D(amount), when)
        await feed.marker("heartbeat", when)
        await runner.batch(at=when)
    end = await latest(runner)
    result = await report(runner, start_source=start.number, end_source=end.number)
    period = result["period"]
    assert D(period["contributions_gbp"]) == 250
    assert D(period["withdrawals_gbp"]) == 100
    assert D(period["net_investment_flows_gbp"]) == 150
    assert abs(D(period["pnl_gbp"])) <= D("1e-18")
    assert abs(D(period["twr"])) <= D("1e-18")
    assert D(period["fees_gbp"]) == 0 and period["trade_fill_count"] == 0
    assert period["investment_flow_count"] == 2
    assert D(period["mwr_annualised"]) == 0
    # An explicit endpoint remains unchanged after a subsequently applied flow.
    at += timedelta(milliseconds=100)
    await runner.service.cashflow("later-flow", D(50), at)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    assert (await report(runner, start_source=start.number, end_source=end.number))[
        "period"
    ] == period


async def test_intermediate_loss_and_rebound_remain_visible_in_observed_drawdown(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    at = AT + timedelta(milliseconds=300)
    await reconcile(runner, at)
    opening = await latest(runner)
    for millis, bid, ask in ((600, "9999", "10000"), (700, "1", "2"), (900, "999", "1000")):
        await feed.book(AT + timedelta(milliseconds=millis), bid=bid, ask=ask)
    await runner.batch(at=AT + timedelta(milliseconds=1100))
    period = (await report(runner, start_source=opening.number))["period"]
    assert D(period["max_observed_drawdown"]) > D("0.1")
    assert D(period["twr"]) == 0
    assert period["valuation_complete"]


async def test_closed_position_attributes_actual_realised_pnl_and_both_fees_once(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    at = AT + timedelta(milliseconds=300)
    await reconcile(runner, at)
    at += timedelta(milliseconds=10)
    order = await runner.service.exit_position("BTC/GBP", evidence(at, feed.epoch, depth="10"), at)
    assert order is not None
    at += timedelta(milliseconds=300)
    await feed.book(at, depth="10")
    await runner.batch(at=at)
    await reconcile(runner, at)
    period = (await report(runner))["period"]
    assert period["trade_fill_count"] == 2
    assert D(period["unrealised_change_gbp"]) == 0
    assert D(period["realised_pnl_gbp"]) - D(period["fees_gbp"]) == D(period["pnl_gbp"])
    assert D(period["closing"]["balances"][1]["quantity"]) == 0
    assert (await report(runner))["period"] == period


async def test_gap_suppresses_returns_without_hiding_known_endpoint_pnl_or_fees(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await fok(runner, feed)
    await reconcile(runner, AT + timedelta(milliseconds=300))
    at = AT + timedelta(milliseconds=400)
    await feed.marker("coverage_gap", at)
    await runner.batch(at=at)
    missing = await latest(runner)
    invalid = (await report(runner))["period"]
    assert invalid["closing"]["nav_gbp"] is None and invalid["pnl_gbp"] is None
    assert invalid["twr"] is None and invalid["max_observed_drawdown"] is None
    await feed.bootstrap(AT + timedelta(milliseconds=500))
    period = (await report(runner))["period"]
    assert not period["valuation_complete"] and period["missing_nav_observations"] > 0
    assert period["twr"] is None and period["mwr_annualised"] is None
    assert period["pnl_gbp"] is not None and D(period["fees_gbp"]) > 0
    assert set(period["issues"]) == {"missing_gbp_valuation", "incomplete_historical_prefix"}
    # Selecting a later endpoint does not erase the prefix's permanent gap.
    period = (await report(runner, start_source=(await latest(runner)).number))["period"]
    assert period["twr"] is None and period["issues"] == ["incomplete_historical_prefix"]
    assert (await latest(runner)).incomplete_since == missing.state.as_of


async def test_paginated_prefix_and_exact_endpoints(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    for n in range(140):
        at = AT + timedelta(microseconds=n)
        await feed.marker("heartbeat", at)
        await runner.batch(at=at)
    complete = await report(runner)
    assert complete["period"]["observations"] == 146
    head = (await latest(runner)).number
    for bounds in (
        {"start_source": -1},
        {"end_source": -1},
        {"end_source": head + 1},
        {"start_source": head, "end_source": head - 1},
    ):
        with pytest.raises(ValueError):
            await report(runner, **bounds)
    # Source sequences may have gaps after rolled-back PostgreSQL allocations.
    at = AT + timedelta(milliseconds=1)
    await feed.marker("heartbeat", at)
    source = (await runner.batch(at=at)).cursor
    async with runner.store.engine.begin() as c:
        next_value = await c.scalar(text("SELECT nextval('aca.public_replay_events_number_seq')"))
    assert next_value is not None and next_value > source
    at += timedelta(milliseconds=1)
    await feed.marker("heartbeat", at)
    await runner.batch(at=at)
    for bounds in ({"start_source": next_value}, {"end_source": next_value}):
        with pytest.raises(ValueError, match="exact recorded source"):
            await report(runner, **bounds)


async def test_baseline_only_and_no_enrolment_are_distinct(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    baseline_source = (await report(runner))["baseline"]["source_number"]
    period = (await report(runner, end_source=baseline_source))["period"]
    assert period["observations"] == 1 and period["mwr_unavailable_reason"] == "zero_duration"
    store = PaperStore(runner.store.engine, uuid4())
    async with PaperWriter(store) as owner:
        from aca.execution.paper import PaperExecution
        from aca.simulation.paper import PaperExchange

        await PaperExecution(owner, PaperExchange(store)).initialise(AT)
    with pytest.raises(ValueError, match="historical enrolment"):
        await historical_performance(store)


async def test_pending_cash_flow_is_reported_as_suffix_without_changing_historical_period(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    before = await report(runner)
    await runner.service.cashflow("not-yet-observed", D(250), AT + timedelta(milliseconds=1))
    after = await report(runner)
    assert after["unapplied_cash_flows"] == 1
    assert after["period"] == before["period"]


async def test_budget_failure_is_read_only(
    replay: tuple[StoredReplay, Feed], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = replay
    before = await runner.store.status()
    monkeypatch.setattr(historical, "MAX_REPORT_VALUES", 1)
    with pytest.raises(ValueError, match="budget exceeded"):
        await report(runner)
    assert await runner.store.status() == before


async def test_concurrent_source_and_cash_flow_commit_does_not_mix_report_snapshot(
    replay: tuple[StoredReplay, Feed],
    monkeypatch: pytest.MonkeyPatch,
    database_settings: Settings,
) -> None:
    from aca.storage.database import create_engine

    runner, feed = replay
    before = await report(runner)
    original = historical._baseline

    async def change(c: Any, store: PaperStore, value: Any) -> None:
        await original(c, store, value)
        at = AT + timedelta(milliseconds=1)
        await runner.service.cashflow("concurrent-flow", D(250), at)
        await feed.marker("heartbeat", at)
        await runner.batch(at=at)

    monkeypatch.setattr(historical, "_baseline", change)
    # The real CLI is a separate process/engine; do not consume both slots in
    # the execution owner's deliberately small pool with an artificial reader.
    engine = create_engine(database_settings)
    try:
        assert await historical_performance(PaperStore(engine, runner.store.account_id)) == before
    finally:
        await engine.dispose()
    monkeypatch.setattr(historical, "_baseline", original)
    after = await report(runner)
    assert D(after["period"]["closing"]["nav_gbp"]) == 1250
    assert after["period"]["observations"] == before["period"]["observations"] + 1


async def test_cli_query_uses_database_enforced_read_only_snapshot(
    replay: tuple[StoredReplay, Feed], database_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = replay
    seen: list[tuple[str, str]] = []

    original = historical._report

    async def check(c: Any, *args: Any) -> dict[str, object]:
        seen.append(
            (
                await c.scalar(text("SHOW transaction_read_only")),
                await c.scalar(text("SHOW transaction_isolation")),
            )
        )
        return await original(c, *args)

    monkeypatch.setattr(historical, "_report", check)
    result = await paper.execute(
        argparse.Namespace(
            action="performance",
            account=runner.store.account_id,
            start_source=None,
            end_source=None,
        ),
        database_settings,
    )
    assert isinstance(result, dict) and result["report_type"] == "historical_paper_performance"
    assert seen == [("on", "repeatable read")]
    # Pool reuse restores defaults; reporting cannot leave the writer read-only.
    await runner.service.halt("Check read-only report pool reset", AT)


async def test_corrupt_but_rehashed_projection_does_not_pass_financial_reconstruction(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    value = await latest(runner)
    # An internally valid hash/NAV/source anchor must not legitimise false units.
    at = AT + timedelta(milliseconds=1)
    await Feed(runner).marker("heartbeat", at)
    async with runner.service.writer.transaction() as c:
        from aca.storage.replay import public_events

        source = (
            (
                await c.execute(
                    select(public_events)
                    .where(public_events.c.number > value.number)
                    .order_by(public_events.c.number)
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        assert source is not None
        previous = await runner.history.latest(c)
        cursor = await runner._latest(c)
        assert previous is not None and cursor is not None
        altered = value.model_copy(
            update={
                "number": source["number"],
                "source_hash": source["fingerprint"],
                "source_received_at": at,
                "state": value.state.model_copy(update={"units": D(2000), "as_of": at}),
            }
        )
        await runner.history.save(c, altered, previous["fingerprint"])
        updated = runner._cursor(cursor).model_copy(
            update={
                "number": source["number"],
                "source_hash": source["fingerprint"],
            }
        )
        await runner._save(c, updated, cursor["fingerprint"])
    with pytest.raises(ValueError, match="balances/units do not reconstruct"):
        await report(runner)
    from aca.observability.notices import daily_summary

    # A later-day financial forgery cannot be hidden by selecting an empty day.
    async with runner.store.engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        with pytest.raises(ValueError, match="balances/units do not reconstruct"):
            await daily_summary(c, runner.store, (AT - timedelta(days=1)).date())


async def test_financial_decimal_precision_survives_report_and_flow_reconstruction(
    database_settings: Settings,
) -> None:
    from aca.execution.paper import PaperExecution
    from aca.simulation.paper import PaperExchange
    from aca.storage.database import create_engine

    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    amount = D("10000000000000.123456789012345678")
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            await service.initialise(AT)
            await service.cashflow("opening", amount, AT)
            runner = StoredReplay(service, venue)
            await runner.enrol()
            feed = Feed(runner)
            await feed.bootstrap()
            when = AT + timedelta(milliseconds=1)
            await service.cashflow("second", amount, when)
            await feed.marker("heartbeat", when)
            await runner.batch(at=when)
            result = await report(runner)
            with localcontext(FINANCIAL_CONTEXT):
                assert D(result["period"]["closing"]["nav_gbp"]) == amount * 2
            assert D(result["period"]["contributions_gbp"]) == amount
            assert D(result["period"]["pnl_gbp"]) == 0
    finally:
        await engine.dispose()
