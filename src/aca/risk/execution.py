"""Dispatch rechecks current state and market evidence without trusting admission."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from aca.domain.market import InstrumentRules, OrderBook
from aca.domain.trading import TERMINAL, UNCERTAIN, ZERO, Order, PortfolioSnapshot, fingerprint
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal
from aca.operations.config import RiskPolicy
from aca.portfolio.state import episode_risk, free_cash, market_exposure, nav, unit_price


@dataclass(frozen=True)
class MarketEvidence:
    book: OrderBook
    rules: InstrumentRules
    clock_offset_ms: Decimal = ZERO
    metadata_consistent: bool = True
    system_online: bool = True


class DispatchDenied(ValueError):
    """No transmission has been authorised."""


def execution_guard(
    state: PortfolioSnapshot, order: Order, market: MarketEvidence, policy: RiskPolicy, at: datetime
) -> None:
    i, approval = order.intent, order.approval
    if i.purpose != "entry" or i.side != "buy":
        raise DispatchDenied("entry dispatcher cannot authorise other economic purposes")
    if order.state != "VALIDATED" or approval is None or not approval.allowed:
        raise DispatchDenied("order has no unconsumed approval")
    if (
        approval.state_version != state.version
        or approval.intent_id != i.id
        or approval.intent_hash != fingerprint(i.document())
        or approval.policy_hash != fingerprint(policy.model_dump(mode="json"))
        or approval.rules_hash != i.rules_hash
        or not approval.approved_at <= at < approval.expires_at
        or not i.created_at <= at < i.expires_at
    ):
        raise DispatchDenied("stale or mismatched approval")
    if (
        not state.ready
        or state.incident
        or state.kill.soft
        or state.kill.hard
        or state.kill.daily_day == at.date()
        or any(o.state in UNCERTAIN for o in state.orders)
    ):
        raise DispatchDenied("account paused or uncertain")
    if (
        state.account_evidence_at is None
        or not 0 <= (at - state.account_evidence_at).total_seconds() <= 30
        or state.reconciled_at is None
        or not 0 <= (at - state.reconciled_at).total_seconds() <= 60
    ):
        raise DispatchDenied("stale account/reconciliation evidence")
    b, r = market.book, market.rules
    parse_decimal(market.clock_offset_ms)
    if (
        abs(market.clock_offset_ms) > 500
        or not market.metadata_consistent
        or not market.system_online
        or b.symbol != i.symbol
        or r.symbol != i.symbol
        or b.venue != r.venue
        or not b.checksum_valid
        or not 0 <= (at - b.received_at).total_seconds() <= 5
        or (b.event_at is not None and not 0 <= (at - b.event_at).total_seconds() <= 5)
        or not 0 <= (at - r.observed_at).total_seconds() <= 3600
        or r.status != "online"
        or fingerprint(r.model_dump(mode="json")) != i.rules_hash
    ):
        raise DispatchDenied("invalid market, clock or metadata evidence")
    if (
        i.quantity % r.quantity_increment
        or i.limit_price % r.price_increment
        or i.stop_trigger % r.price_increment
        or i.protective_limit % r.price_increment
        or i.quantity < r.minimum_quantity
        or i.quantity * min(i.limit_price, i.protective_limit) < r.minimum_cost
    ):
        raise DispatchDenied("current precision/minimum check failed")
    with localcontext(FINANCIAL_CONTEXT):
        if (
            i.limit_price - i.stop_trigger < i.limit_price * Decimal("0.05")
            or i.fee_bound < Decimal("0.008")
            or i.allowance_per_unit < i.limit_price * Decimal("0.002")
        ):
            raise DispatchDenied("independent protective distance/cost veto")
        equity = nav(state, at)
        price = unit_price(state, at)
        if (
            equity <= 0
            or 1 - price / state.high_water >= policy.soft_drawdown
            or state.daily_day != at.date()
            or price / state.daily_unit_price - 1 <= -policy.daily_loss
        ):
            raise DispatchDenied("independent loss threshold veto")
        # Reservations include this exact intent. Check the whole commitment,
        # giving no exposure/cash credit to pending sells.
        if (
            free_cash(state) < 0
            or market_exposure(state, at) > equity * policy.total_exposure
            or i.quantity * i.limit_price > equity * policy.asset_exposure
            or episode_risk(state) > equity * policy.aggregate_planned_risk
        ):
            raise DispatchDenied("independent funding/exposure veto")
        loss = i.quantity * (
            i.limit_price
            - i.protective_limit
            + i.fee_bound * (i.limit_price + i.protective_limit)
            + i.allowance_per_unit
        )
        if loss > equity * policy.planned_risk_per_trade or loss != approval.planned_loss:
            # Compare at the same explicit functional-currency quantum.
            from aca.accounting.ledger import stored

            if (
                stored(loss) != approval.planned_loss
                or loss > equity * policy.planned_risk_per_trade
            ):
                raise DispatchDenied("independent planned-loss veto")
        if i.time_in_force == "GTD":
            if i.limit_price >= b.asks[0].price:
                raise DispatchDenied("post-only entry would take liquidity")
        else:
            mid = (b.asks[0].price + b.bids[0].price) / 2
            spread = (b.asks[0].price - b.bids[0].price) / mid
            remaining, cost = i.quantity, ZERO
            for level in b.asks:
                if level.price > i.limit_price:
                    break
                quantity = min(remaining, level.quantity)
                cost += quantity * level.price
                remaining -= quantity
            if (
                spread > Decimal("0.002")
                or remaining
                or cost / i.quantity / b.asks[0].price - 1 > Decimal("0.002")
                or i.limit_price / mid - 1 > Decimal("0.005")
            ):
                raise DispatchDenied("marketable limit bounds/liquidity")
    # No second position episode can use the same instrument or its inventory.
    if state.quantity("BTC" if i.symbol == "BTC/GBP" else "ETH"):
        raise DispatchDenied("independent no-pyramiding veto")
    if any(
        o.intent.symbol == i.symbol
        and o.intent.client_id != i.client_id
        and o.intent.side == "buy"
        and o.state not in TERMINAL
        for o in state.orders
    ):
        raise DispatchDenied("another pending entry exists")
