"""Connected C1 workflow: real PostgreSQL, stored public evidence, no demo orders."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Table, func, select, text
from test_stored_replay import Feed, evidence

from aca.app.paper_strategy import PaperStrategy
from aca.app.paper_supervisor import PaperSupervisor, SupervisorStatus
from aca.domain.market import Candle
from aca.domain.trading import TERMINAL, fingerprint
from aca.execution.paper import PaperExecution, SimulatedCrash
from aca.market_data.paper_feed import StoredPaperFeed
from aca.observability.notices import daily_summary
from aca.operations.config import RiskPolicy, Settings
from aca.portfolio.state import reserved_cash
from aca.reporting.historical import historical_performance
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter, fills, journals, venue_events
from aca.storage.strategy import PaperSelection, decisions, select_strategy, strategy_status

pytestmark = pytest.mark.integration
D = Decimal
_days = count(1)


class Workflow:
    def __init__(self, service: PaperExecution, at: datetime) -> None:
        self.service, self.at, self.now = service, at, at
        assert isinstance(service.venue, PaperExchange)
        self.replay = StoredReplay(service, service.venue)
        self.feed = Feed(self.replay)
        self.strategy = PaperStrategy(service, clock=lambda: self.now)
        self.status = SupervisorStatus()
        self.supervisor = self.supervision()

    def supervision(self) -> PaperSupervisor:
        async def capacity() -> None:
            pass

        return PaperSupervisor(
            self.service,
            self.status,
            marks=StoredPaperFeed(self.service.store).marks,
            capacity=capacity,
            clock=lambda: self.now,
            timer=lambda: (self.now - self.at).total_seconds(),
            replay=self.replay,
            strategy=self.strategy,
        )

    async def fresh(self, at: datetime, *, bid: str = "999", ask: str = "1000") -> None:
        self.now = at
        await self.feed.marker(
            "instrument_rules",
            at,
            {
                "rules": [
                    evidence(at, self.feed.epoch, symbol)
                    .rules.model_copy(update={"observed_at": at})
                    .model_dump(mode="json")
                    for symbol in ("BTC/GBP", "ETH/GBP")
                ]
            },
        )
        await self.feed.book(at, bid=bid, ask=ask, depth="10")
        await self.feed.book(at, "ETH/GBP", depth="10")
        await self.feed.marker("capture_health", at, {"market_ready": True})

    async def candles(
        self, end: datetime, *, direction: str = "long", fault: str | None = None
    ) -> None:
        bars = []
        for i in range(201):
            stop = end - timedelta(hours=4 * (200 - i))
            price = D("1010" if direction == "long" else "990") if i == 200 else D(1000)
            bars.append(
                Candle(
                    venue="kraken",
                    symbol="BTC/GBP",
                    interval_minutes=240,
                    start=stop - timedelta(hours=4),
                    end=stop,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=D(100),
                    trade_count=100,
                    complete=True,
                    quality=(),
                    source="operational-fixture",
                    observed_at=stop + timedelta(seconds=10),
                ).model_dump(mode="json")
            )
        if fault == "gap":
            del bars[120]
        if fault == "warmup":
            bars = bars[-199:]
        if fault == "unfinished":
            bars[-1]["complete"] = False
            bars[-1]["quality"] = ["partial_capture_interval"]
        await self.feed.marker(
            "rest:closed_candles",
            end + timedelta(seconds=10),
            {"candles": bars, "history_complete": False},
        )

    async def begin(self, fault: str | None = None, *, lag_ms: int = 0) -> None:
        end = self.at + timedelta(hours=4)
        await self.feed.marker("epoch_started", end + timedelta(seconds=9))
        await self.fresh(end + timedelta(seconds=9))
        await self.candles(end, fault=fault)
        self.now = end + timedelta(seconds=10, milliseconds=lag_ms)
        await self.supervisor.start()
        await self.supervisor.tick()

    async def tick(self, at: datetime) -> None:
        self.now = at
        await self.supervisor.tick()


@pytest.fixture
async def workflow(database_settings: Settings) -> AsyncIterator[Workflow]:
    engine = create_engine(database_settings)
    at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=next(_days))
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            await service.initialise(at)
            await service.cashflow("opening", D(1000), at)
            await select_strategy(
                owner,
                PaperSelection(
                    account_id=store.account_id,
                    strategy="sma200",
                    symbol="BTC/GBP",
                    interval_minutes=240,
                    entry_time_in_force="FOK",
                    selected_at=at,
                    policy_hash=fingerprint(RiskPolicy().model_dump(mode="json")),
                ),
            )
            runner = Workflow(service, at)
            await runner.replay.enrol()
            yield runner
    finally:
        await engine.dispose()


async def count_rows(workflow: Workflow, table: Table) -> int:
    async with workflow.service.writer.transaction() as c:
        # Test helper only: callers supply the declared account-scoped Core tables.
        return int(
            await c.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.account_id == workflow.service.store.account_id)
            )
            or 0
        )


async def restart(workflow: Workflow) -> None:
    # Release the old dedicated database connection and re-audit through a new
    # writer session. A controller-only reset would miss the ownership boundary.
    writer = workflow.service.writer
    await writer.__aexit__()
    await writer.__aenter__()
    workflow.service = PaperExecution(writer, PaperExchange(writer.store))
    workflow.replay = StoredReplay(workflow.service, workflow.service.venue)  # type: ignore[arg-type]
    workflow.feed.replay = workflow.replay
    workflow.strategy = PaperStrategy(workflow.service, clock=lambda: workflow.now)
    workflow.status = SupervisorStatus()
    workflow.supervisor = workflow.supervision()
    await workflow.supervisor.start()
    await workflow.supervisor.tick()


async def test_continuous_signal_fill_protection_exit_and_restart(
    workflow: Workflow, tmp_path: Path
) -> None:
    await workflow.begin()
    service = workflow.service
    state = await service._state()
    assert len(state.orders) == 1 and state.orders[0].state == "ACKNOWLEDGED"
    first = state.orders[0].intent.client_id
    assert state.orders[0].intent.quantity * state.orders[0].intent.limit_price < D(100)
    assert reserved_cash(state) > 0
    assert workflow.status.document()["strategy_dispatch_enabled"] is True
    assert workflow.status.document()["trading_ready"] is False
    # Same candle/tick and a fresh controller cannot create a second order.
    await workflow.supervisor.tick()
    await restart(workflow)
    assert len((await service._state()).orders) == 1
    # Startup cancels outstanding entries, so use a new account decision to test
    # fill/restart retention below, rather than authorise a resend of this action.
    assert (await service._state()).order(first).state == "CANCELLED"
    end = workflow.at + timedelta(hours=8)
    await workflow.fresh(end + timedelta(seconds=10))
    await workflow.candles(end)
    await workflow.tick(end + timedelta(seconds=10))
    entry = (await service._state()).orders[-1]
    assert entry.state == "ACKNOWLEDGED"
    await workflow.fresh(end + timedelta(seconds=10, milliseconds=300))
    await workflow.tick(workflow.now)
    state = await service._state()
    assert state.quantity("BTC") > 0
    assert state.order(entry.intent.client_id).state == "FILLED"
    assert any(o.intent.purpose == "protection" and o.state not in TERMINAL for o in state.orders)
    assert await count_rows(workflow, fills) == 1
    before = state.quantity("GBP"), state.quantity("BTC")
    await restart(workflow)
    assert (
        (await service._state()).quantity("GBP"),
        (await service._state()).quantity("BTC"),
    ) == before
    end += timedelta(hours=4)
    await workflow.fresh(end + timedelta(seconds=10))
    await workflow.candles(end, direction="flat")
    await workflow.tick(end + timedelta(seconds=10))
    state = await service._state()
    assert any(o.intent.purpose == "exit" and o.state == "ACKNOWLEDGED" for o in state.orders)
    assert all(o.state in TERMINAL for o in state.orders if o.intent.purpose == "protection")
    await workflow.fresh(end + timedelta(seconds=10, milliseconds=300))
    await workflow.tick(workflow.now)
    state = await service._state()
    assert state.quantity("BTC") == 0 and reserved_cash(state) == 0
    assert await count_rows(workflow, fills) == 2
    assert await count_rows(workflow, journals) == 3
    posted = await count_rows(workflow, venue_events)
    await restart(workflow)
    assert await count_rows(workflow, fills) == 2
    assert await count_rows(workflow, venue_events) == posted
    report = await historical_performance(service.store)
    assert report["financial_evidence_reconciled"] is True
    async with service.writer.transaction() as c:
        status = await strategy_status(c, service.store.account_id)
        summary = await daily_summary(c, service.store, workflow.at.date())
    assert status["qualifying_days"] == status["qualifying_episodes"] == 0
    assert summary["daily_return_complete"] is False
    assert D(str(summary["fees_gbp"])) > 0
    assert summary["day"] == workflow.at.date().isoformat()
    (tmp_path / "lifecycle.json").write_text(
        json.dumps(
            {
                "account_id": str(service.store.account_id),
                "state": (await service._state()).document(),
                "strategy_status": status,
                "performance": report,
                "daily_summary": summary,
                "fill_postings": 2,
                "journals": 3,
                "qualifying_days": 0,
                "qualifying_episodes": 0,
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.parametrize("fault", ["gap", "warmup", "unfinished"])
async def test_bad_candle_prefix_cannot_enter(workflow: Workflow, fault: str) -> None:
    await workflow.begin(fault)
    assert not (await workflow.service._state()).orders
    assert workflow.strategy.reason == "strategy_candles_unavailable"
    assert await count_rows(workflow, decisions) == 1
    await restart(workflow)
    assert not (await workflow.service._state()).orders


@pytest.mark.parametrize(
    "point",
    [
        "after_strategy_claim",
        "after_strategy_queue",
        "after_submitting_before_send",
        "after_send_before_ack",
    ],
)
async def test_runner_crash_never_retries_economic_action(workflow: Workflow, point: str) -> None:
    def crash(actual: str) -> None:
        if actual == point:
            raise SimulatedCrash()

    workflow.service.fault = crash
    with pytest.raises(SimulatedCrash):
        await workflow.begin()
    workflow.service.fault = lambda point: None
    orders_before = len((await workflow.service._state()).orders)
    await restart(workflow)
    await workflow.supervisor.tick()
    state = await workflow.service._state()
    assert len(state.orders) == orders_before
    assert await count_rows(workflow, decisions) == 1
    if point == "after_submitting_before_send":
        assert state.orders[0].state == "UNKNOWN" and reserved_cash(state) > 0
    async with workflow.service.writer.transaction() as c:
        status = await strategy_status(c, workflow.service.store.account_id)
    assert len(status["decisions"]) == 1  # type: ignore[arg-type]


async def test_stale_feed_and_loss_latch_block_scheduled_entry(workflow: Workflow) -> None:
    end = workflow.at + timedelta(hours=4)
    await workflow.feed.marker("epoch_started", end)
    await workflow.fresh(end)
    await workflow.candles(end)
    workflow.now = end + timedelta(seconds=10)
    await workflow.supervisor.start()
    await workflow.supervisor.tick()
    assert not (await workflow.service._state()).orders
    assert not workflow.status.supervision_ready
    # A fresh feed cannot clear an existing hard loss/operator latch.
    await workflow.fresh(end + timedelta(seconds=10, milliseconds=500))
    await workflow.service.halt("operational fault fixture", workflow.now)
    await workflow.tick(workflow.now)
    assert not (await workflow.service._state()).orders
    assert (await workflow.service._state()).kill.hard


async def test_expired_decision_and_backlog_do_not_catch_up(workflow: Workflow) -> None:
    await workflow.begin("gap")
    end = workflow.at + timedelta(hours=8)
    await workflow.candles(end)
    await workflow.fresh(end + timedelta(seconds=13))
    await workflow.tick(end + timedelta(seconds=13))
    assert workflow.strategy.reason == "strategy_decision_expired"
    assert not (await workflow.service._state()).orders


@pytest.mark.parametrize("late", [False, True])
async def test_running_replay_captures_midnight_or_keeps_daily_suspension(
    workflow: Workflow, late: bool
) -> None:
    await workflow.begin("gap")
    boundary = workflow.at + timedelta(days=1)
    await workflow.fresh(boundary - timedelta(seconds=1))
    await workflow.tick(workflow.now)
    await workflow.fresh(boundary + timedelta(seconds=6 if late else 0.2))
    await workflow.tick(workflow.now)
    state = await workflow.service._state()
    if late:
        assert state.kill.daily_day == boundary.date()
        assert state.kill.reason == "missing_utc_midnight_mark"
    else:
        assert state.daily_day == boundary.date() and state.kill.daily_day is None
        async with workflow.service.writer.transaction() as c:
            value = workflow.replay.history.restore(await workflow.replay.history.latest(c))  # type: ignore[arg-type]
            # Boundary lives in the first following valuation, not every receipt.
            rows = (
                (
                    await c.execute(
                        text(
                            "SELECT document FROM aca.paper_replay_valuations "
                            "WHERE account_id=:account "
                            "AND document->'day_boundary'->>'at' IS NOT NULL"
                        ),
                        {"account": state.account_id},
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0]["day_boundary"]["at"] == boundary.isoformat().replace("+00:00", "Z")
        assert value.state.daily_day == boundary.date()
        await workflow.replay.audit()


async def test_daily_summary_uses_two_running_boundaries_and_excludes_investment_flow(
    workflow: Workflow,
    tmp_path: Path,
) -> None:
    await workflow.begin("gap")
    opening = workflow.at + timedelta(days=1)
    for boundary in (opening, opening + timedelta(days=1)):
        await workflow.fresh(boundary - timedelta(seconds=1))
        await workflow.tick(workflow.now)
        await workflow.fresh(boundary + timedelta(milliseconds=200))
        await workflow.tick(workflow.now)
        if boundary == opening:
            noon = boundary + timedelta(hours=12)
            await workflow.fresh(noon)
            await workflow.tick(noon)
            await workflow.service.cashflow(
                "midday-contribution", D(100), noon + timedelta(seconds=1)
            )
    async with workflow.service.writer.transaction() as c:
        summary = await daily_summary(c, workflow.service.store, opening.date())
    assert summary["daily_return_complete"] is True
    assert D(str(summary["daily_pnl_gbp"])) == 0
    assert D(str(summary["daily_twr"])) == 0
    assert D(str(summary["contributions_gbp"])) == 100
    assert summary["day_boundaries"] is not None
    assert (await workflow.service._state()).kill.daily_day is None
    await workflow.replay.audit()
    (tmp_path / "daily-boundaries.json").write_text(json.dumps(summary, indent=2) + "\n")


async def test_actual_daily_loss_is_observed_before_another_scheduled_entry(
    workflow: Workflow,
) -> None:
    await workflow.begin()
    first = workflow.at + timedelta(hours=4, seconds=10, milliseconds=300)
    await workflow.fresh(first)
    await workflow.tick(first)
    assert (await workflow.service._state()).quantity("BTC") > 0
    # Gap through the protective limit: no invented stop execution. The scheduled
    # flat signal closes the owned position with a bounded IOC at the new book.
    end = workflow.at + timedelta(hours=8)
    await workflow.fresh(end + timedelta(seconds=10), bid="600", ask="600.5")
    await workflow.candles(end, direction="flat")
    await workflow.tick(workflow.now)
    assert (await workflow.service._state()).kill.daily_day == workflow.at.date()
    await workflow.fresh(end + timedelta(seconds=10, milliseconds=300), bid="600", ask="600.5")
    await workflow.tick(workflow.now)
    assert (await workflow.service._state()).quantity("BTC") == 0
    before = len((await workflow.service._state()).orders)
    end += timedelta(hours=4)
    await workflow.fresh(end + timedelta(seconds=10), bid="600", ask="600.5")
    await workflow.candles(end)
    await workflow.tick(workflow.now)
    state = await workflow.service._state()
    assert state.kill.daily_day == workflow.at.date()
    assert len(state.orders) == before
    assert workflow.strategy.reason == "execution_denied"


async def test_changed_rules_at_dispatch_expires_reservation_without_submission(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aca.risk.execution import MarketEvidence

    current = workflow.strategy.market.current
    calls = 0

    async def changed(at: datetime) -> tuple[MarketEvidence, ...]:
        nonlocal calls
        calls += 1
        markets = await current(at)
        if calls == 2:
            return tuple(
                MarketEvidence(
                    book=m.book, rules=m.rules.model_copy(update={"minimum_quantity": D(100)})
                )
                for m in markets
            )
        return markets

    monkeypatch.setattr(workflow.strategy.market, "current", changed)
    await workflow.begin()
    state = await workflow.service._state()
    assert len(state.orders) == 1 and state.orders[0].state == "EXPIRED"
    assert reserved_cash(state) == 0
    assert await count_rows(workflow, venue_events) == 0


async def test_selection_is_explicit_immutable_and_operational(workflow: Workflow) -> None:
    async with workflow.service.writer.transaction() as c:
        status = await strategy_status(c, workflow.service.store.account_id)
    chosen = PaperSelection.restore(status["selection"])  # type: ignore[arg-type]
    assert chosen.operational_only and not chosen.promotion_evidence
    assert await select_strategy(workflow.service.writer, chosen) == chosen
    with pytest.raises(ValueError, match="immutable"):
        await select_strategy(
            workflow.service.writer, chosen.model_copy(update={"strategy": "momentum60"})
        )
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with workflow.service.writer.transaction() as c:
            await c.execute(
                text("DELETE FROM aca.paper_strategy_selections WHERE account_id=:account"),
                {"account": chosen.account_id},
            )


async def test_actual_decision_time_does_not_extend_original_expiry(workflow: Workflow) -> None:
    await workflow.begin(lag_ms=1500)
    state = await workflow.service._state()
    assert len(state.orders) == 1
    intent = state.orders[0].intent
    assert intent.created_at == workflow.at + timedelta(hours=4, seconds=11, milliseconds=500)
    assert intent.expires_at == workflow.at + timedelta(hours=4, seconds=12)
    async with workflow.service.writer.transaction() as c:
        status = await strategy_status(c, workflow.service.store.account_id)
    item = status["decisions"][0]  # type: ignore[index]
    assert item["decision"]["decision_at"] == intent.created_at.isoformat().replace("+00:00", "Z")


async def test_duplicate_candle_receipt_does_not_create_another_economic_action(
    workflow: Workflow,
) -> None:
    await workflow.begin()
    end = workflow.at + timedelta(hours=4)
    await workflow.candles(end)
    await workflow.tick(workflow.now)
    assert len((await workflow.service._state()).orders) == 1
    await restart(workflow)
    assert len((await workflow.service._state()).orders) == 1
    assert await count_rows(workflow, decisions) == 1


async def test_maintenance_arriving_after_queue_vetoes_dispatch(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aca.risk.execution import MarketEvidence

    current = workflow.strategy.market.current
    calls = 0

    async def maintenance(at: datetime) -> tuple[MarketEvidence, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            workflow.now += timedelta(microseconds=100)
            await workflow.feed.marker("status", workflow.now, {"system": "maintenance"})
            return await current(workflow.now)
        return await current(at)

    monkeypatch.setattr(workflow.strategy.market, "current", maintenance)
    await workflow.begin()
    state = await workflow.service._state()
    assert len(state.orders) == 1 and state.orders[0].state == "EXPIRED"
    assert reserved_cash(state) == 0
    assert await count_rows(workflow, venue_events) == 0
    assert workflow.strategy.reason == "market_unavailable"


async def test_cli_create_fund_select_enrol_and_read_evidence(database_settings: Settings) -> None:
    from aca.operations.cli import _parser
    from aca.operations.paper import execute

    account = str(uuid4())

    async def cli(action: str, *args: str) -> dict[str, object]:
        parsed = _parser().parse_args(["paper", action, "--account", account, *args])
        result = await execute(parsed, database_settings)
        assert isinstance(result, dict)
        return result

    await cli("create")
    await cli(
        "fund",
        "--reference",
        "cli-opening",
        "--amount-gbp",
        "250",
        "--occurred-at",
        datetime.now(UTC).isoformat(),
    )
    chosen = await cli(
        "select",
        "--strategy",
        "sma200",
        "--symbol",
        "BTC/GBP",
        "--interval",
        "240",
        "--operational-experiment",
    )
    assert chosen["operational_only"] is True and chosen["promotion_evidence"] is False
    await cli("enrol")
    selected = await cli("decisions")
    assert selected["selection"] == chosen and selected["decisions"] == []
    status = await cli("status")
    assert status["live_execution_available"] is False
    summary = await cli("daily", "--day", datetime.now(UTC).date().isoformat())
    assert summary["daily_return_complete"] is False
    assert D(str(summary["contributions_gbp"])) == 250
