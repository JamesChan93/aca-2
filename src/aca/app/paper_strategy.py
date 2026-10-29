"""Opt-in slow strategy decisions using the existing local paper execution core."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aca.domain.timing import APPROVAL_TTL, SLOW_FRESHNESS
from aca.domain.trading import TERMINAL, UNCERTAIN, Asset, Order, fingerprint
from aca.domain.values import utc_datetime
from aca.execution.paper import PaperExecution
from aca.market_data.paper_feed import FeedUnavailable, StoredPaperFeed
from aca.market_data.strategy_feed import StoredStrategyFeed
from aca.portfolio.state import entries_paused
from aca.risk.execution import DispatchDenied
from aca.storage.strategy import PaperDecision, PaperSelection, claim, finish, selection
from aca.storage.studies import PaperStudy, StudyMember, append, claim_signal
from aca.strategies.slow import signal


class PaperStrategy:
    def __init__(
        self,
        service: PaperExecution,
        *,
        study: PaperStudy | None = None,
        member: StudyMember | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service, self.clock = service, clock
        self.study, self.member = study, member
        self.chosen: PaperSelection | StudyMember | None = None
        self.market = StoredPaperFeed(service.store)
        self.candles = StoredStrategyFeed(service.store)
        self.last_watermark: datetime | None = None
        self.reason: str | None = "strategy_starting"
        self.decisions = 0

    async def start(self) -> None:
        if self.study is not None:
            if self.member is None:
                raise ValueError("study member required")
            self.chosen = self.member
        else:
            async with self.service.writer.transaction() as c:
                self.chosen = await selection(c, self.service.store.account_id)
        if self.chosen is None:
            raise ValueError("explicit operational strategy selection required")

    async def finish(
        self, client_id: UUID, outcome: str, at: datetime, reason: str | None = None
    ) -> None:
        if self.study is None:
            await finish(self.service.writer, client_id, outcome, at, reason)
        else:
            await append(
                self.service.writer,
                self.study,
                "signal_result",
                at,
                {"client_id": str(client_id), "outcome": outcome, "reason": reason},
            )

    async def tick(self, at: datetime) -> None:
        chosen = self.chosen
        if chosen is None:
            raise ValueError("paper strategy not started")
        at = utc_datetime(at)
        seconds = chosen.interval_minutes * 60
        watermark = datetime.fromtimestamp(
            int((at - timedelta(seconds=10)).timestamp()) // seconds * seconds, UTC
        )
        # Never replay an old backlog. Each due identity is consumed at most once,
        # including denied decisions and crashes before any intent is reserved.
        if watermark == self.last_watermark:
            return
        decision_at = watermark + timedelta(seconds=10)
        freshness = SLOW_FRESHNESS if chosen.strategy_version == "slow-v2" else APPROVAL_TTL
        prefix = None
        value = None
        reason = None
        if at >= decision_at + freshness or decision_at < chosen.selected_at:
            reason = "strategy_decision_expired"
        else:
            try:
                prefix = await self.candles.prefix(chosen, watermark, at)
            except FeedUnavailable:
                reason = "strategy_candles_unavailable"
        now = utc_datetime(self.clock())
        if now < at or now >= decision_at + freshness:
            reason = "strategy_decision_expired"
        if prefix is not None and reason is None:
            value = signal(prefix.bars, chosen.strategy, now, version=chosen.strategy_version)
            if chosen.strategy_version == "slow-v1":
                value = value.model_copy(update={"expires_at": decision_at + freshness})
        decision = PaperDecision(
            account_id=chosen.account_id,
            selection_hash=fingerprint(chosen.document()),
            watermark=watermark,
            decision_at=value.decision_at if value else decision_at,
            recorded_at=now,
            client_id=chosen.identity(watermark),
            signal=value,
            reason=reason,
            candle_hash=prefix.fingerprint if prefix else None,
            sources=prefix.sources if prefix else (),
        )
        claimed = (
            await claim_signal(self.service.writer, self.study, self.member, decision)
            if self.study is not None and self.member is not None
            else await claim(self.service.writer, decision)
        )
        if not claimed:
            self.last_watermark = watermark
            self.reason = None
            return
        self.decisions += 1
        self.last_watermark = watermark
        self.service.fault("after_strategy_claim")
        if reason is not None or value is None:
            self.reason = reason
            await self.finish(decision.client_id, reason or "no_signal", now)
            return
        denial = None
        try:
            state = await self.service._state()
            if (
                state.kill.hard
                or not state.ready
                or state.incident
                or any(o.state in UNCERTAIN for o in state.orders)
            ):
                raise DispatchDenied("strategy_account_unsafe")
            asset: Asset = "BTC" if chosen.symbol == "BTC/GBP" else "ETH"
            pending = any(
                o.intent.symbol == chosen.symbol
                and o.intent.purpose == "entry"
                and o.state not in TERMINAL
                for o in state.orders
            )
            if value.desired == "flat" and pending:
                # Stop the remaining buy before sizing an exit from confirmed
                # inventory. Cancellation races are reconciled by the core.
                for pending_order in state.orders:
                    if (
                        pending_order.intent.symbol == chosen.symbol
                        and pending_order.intent.purpose == "entry"
                        and pending_order.state not in TERMINAL
                        and pending_order.state not in UNCERTAIN
                    ):
                        await self.service.cancel(
                            pending_order.intent.client_id, utc_datetime(self.clock())
                        )
                state = await self.service._state()
                if any(o.state in UNCERTAIN for o in state.orders):
                    raise DispatchDenied("strategy_cancellation_uncertain")
            if (
                value.desired == "hold"
                or (value.desired == "long" and (state.quantity(asset) or pending))
                or (value.desired == "flat" and not state.quantity(asset))
            ):
                outcome = "no_change"
            else:
                now = utc_datetime(self.clock())
                markets = await self.market.current(now)
                market = next(m for m in markets if m.book.symbol == chosen.symbol)
                now = utc_datetime(self.clock())
                if not value.decision_at <= now < value.expires_at:
                    raise DispatchDenied("strategy_decision_expired")
                if value.desired == "long":
                    if entries_paused(state, now):
                        raise DispatchDenied("strategy_risk_suspended")
                    entry = await self.service.queue(
                        value,
                        market,
                        now,
                        time_in_force=chosen.entry_time_in_force,
                        client_id=decision.client_id,
                    )
                    self.service.fault("after_strategy_queue")
                    # Re-read immediately before the independent dispatch veto;
                    # metadata or public status may have changed during sizing.
                    markets = await self.market.current(utc_datetime(self.clock()))
                    market = next(m for m in markets if m.book.symbol == chosen.symbol)
                    order: Order | None = await self.service.dispatch(
                        entry.intent.client_id, market, utc_datetime(self.clock())
                    )
                else:
                    order = await self.service.exit_position(
                        chosen.symbol,
                        market,
                        now,
                        client_id=decision.client_id,
                        expires_at=value.expires_at,
                        clock=self.clock,
                        refresh=self.market.current,
                    )
                outcome = "submitted" if order is not None else "no_inventory_or_quarantined"
        except (DispatchDenied, FeedUnavailable) as exc:
            denial = str(exc)[:240]
            # A second-check veto must not leave an unused buy reservation behind.
            current = await self.service._state()
            for queued in current.orders:
                if queued.intent.client_id == decision.client_id and queued.state == "VALIDATED":
                    await self.service.cancel(
                        decision.client_id, max(current.as_of, utc_datetime(self.clock()))
                    )
            outcome = (
                "execution_denied" if isinstance(exc, DispatchDenied) else "market_unavailable"
            )
        self.reason = outcome if outcome in {"execution_denied", "market_unavailable"} else None
        await self.finish(decision.client_id, outcome, utc_datetime(self.clock()), denial)
