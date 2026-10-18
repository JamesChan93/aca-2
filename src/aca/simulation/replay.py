"""Bounded offline depth/trade replay, using the operational paper core."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from aca.domain.market import Candle, InstrumentRules, Level, MarketTrade, OrderBook
from aca.domain.trading import Asset, Record, Time, Valuation
from aca.domain.values import FinancialDecimal
from aca.execution.paper import PaperExecution
from aca.reporting.paper import performance
from aca.risk.execution import DispatchDenied, MarketEvidence
from aca.simulation.paper import PaperExchange
from aca.strategies.slow import Variant, signal


class ReplayStep(Record):
    at: Time
    book: OrderBook
    trades: tuple[MarketTrade, ...] = ()
    candles: tuple[Candle, ...] = ()
    variant: Variant = "sma200"
    entry_style: Literal["GTD", "IOC", "FOK"] = "GTD"
    request_exit: bool = False

    @model_validator(mode="after")
    def observation(self) -> ReplayStep:
        if self.at != self.book.received_at or len(self.candles) > 1000 or len(self.trades) > 100:
            raise ValueError("replay observation timing/size")
        return self


class ReplayPlan(Record):
    kind: Literal["synthetic_paper_replay_v1"]
    initial_capital_gbp: FinancialDecimal = Field(gt=0)
    rules: tuple[InstrumentRules, ...] = Field(min_length=1, max_length=2)
    steps: tuple[ReplayStep, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def chronology(self) -> ReplayPlan:
        times = [step.at for step in self.steps]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("replay steps must be strictly chronological")
        if len({r.symbol for r in self.rules}) != len(self.rules):
            raise ValueError("duplicate replay rules")
        return self


async def run_replay(
    service: PaperExecution, venue: PaperExchange, plan: ReplayPlan
) -> dict[str, object]:
    at = plan.steps[0].at
    await service.initialise(at)
    state = await service._state()
    if state.version != 1:
        raise ValueError("a replay requires a new empty paper account")
    await service.cashflow("synthetic-opening", plan.initial_capital_gbp, at)
    rejected = 0
    for step in plan.steps:
        rule = next(r for r in plan.rules if r.symbol == step.book.symbol)
        market = MarketEvidence(book=step.book, rules=rule)
        # Marks are tied to actual input receipt time, not refreshed by a timer.
        old = await service._state()
        asset: Asset = "BTC" if step.book.symbol == "BTC/GBP" else "ETH"
        mark = Valuation(
            asset=asset,
            bid_gbp=step.book.bids[0].price,
            observed_at=step.at,
            source="synthetic-replay-book",
        )
        marks = (*tuple(m for m in old.marks if m.asset != asset), mark)
        await service.mark(marks, step.at)
        await venue.advance(step.book, rule, step.at)
        for trade in step.trades:
            await venue.advance(step.book, rule, step.at, trade)
        result = await service.reconcile(step.at)
        if not result.complete:
            rejected += 1
            continue
        if step.request_exit:
            await service.exit_position(step.book.symbol, market, step.at)
        if step.candles:
            decision = signal(step.candles, step.variant, step.at)
            if decision.desired == "long":
                try:
                    order = await service.queue(
                        decision, market, step.at, time_in_force=step.entry_style
                    )
                    await service.dispatch(order.intent.client_id, market, step.at)
                except DispatchDenied:
                    rejected += 1
            elif decision.desired == "flat":
                await service.exit_position(step.book.symbol, market, step.at)
    final = await service._state()
    async with service.writer.transaction() as c:
        history = await service.store.history(c)
    return {
        "schema_version": 1,
        "kind": "synthetic_paper_replay_report",
        "account_id": str(final.account_id),
        "steps": len(plan.steps),
        "blocked_steps": rejected,
        "performance": performance(final, history, final.as_of),
        "state": final.document(),
        "live_execution_available": False,
        "promotion_evidence": False,
    }


def demo_plan(epoch: UUID) -> ReplayPlan:
    """A disclosed synthetic rising history, one entry and a bounded full exit."""
    at = datetime(2026, 10, 18, tzinfo=UTC)
    rule = InstrumentRules(
        venue="local-synthetic",
        symbol="BTC/GBP",
        venue_symbol="BTC-GBP",
        rest_pair="local-only",
        price_increment=Decimal("0.1"),
        quantity_increment=Decimal("0.00000001"),
        minimum_quantity=Decimal("0.00006"),
        minimum_cost=Decimal("0.43"),
        status="online",
        observed_at=at,
    )
    candles = tuple(
        Candle(
            venue="local-synthetic",
            symbol="BTC/GBP",
            interval_minutes=1440,
            start=at - timedelta(days=202 - i),
            end=at - timedelta(days=201 - i),
            open=Decimal(800 + i),
            high=Decimal(802 + i),
            low=Decimal(799 + i),
            close=Decimal(801 + i),
            volume=Decimal(10),
            trade_count=100,
            complete=True,
            quality=(),
            source="synthetic",
            observed_at=at,
        )
        for i in range(201)
    )

    def book(time: datetime) -> OrderBook:
        return OrderBook(
            venue="local-synthetic",
            symbol="BTC/GBP",
            bids=(Level(price=Decimal(999), quantity=Decimal(10)),),
            asks=(Level(price=Decimal(1000), quantity=Decimal(10)),),
            checksum_valid=True,
            event_at=time,
            received_at=time,
            epoch=epoch,
        )

    times = (
        at,
        at + timedelta(milliseconds=300),
        at + timedelta(milliseconds=400),
        at + timedelta(milliseconds=700),
    )
    return ReplayPlan(
        kind="synthetic_paper_replay_v1",
        initial_capital_gbp=Decimal(250),
        rules=(rule,),
        steps=tuple(
            ReplayStep(
                at=time,
                book=book(time),
                candles=candles if i == 0 else (),
                entry_style="FOK",
                request_exit=i == 2,
            )
            for i, time in enumerate(times)
        ),
    )
