"""Portfolio checks on exact candidate economics, separate from quantity sizing."""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from aca.accounting.ledger import stored
from aca.domain.market import InstrumentRules
from aca.domain.trading import (
    TERMINAL,
    UNCERTAIN,
    ZERO,
    Asset,
    OrderIntent,
    PortfolioSnapshot,
    RiskDecision,
    fingerprint,
)
from aca.domain.values import FINANCIAL_CONTEXT
from aca.operations.config import RiskPolicy
from aca.portfolio.state import entries_paused, episode_risk, free_cash, market_exposure, nav


def planned_loss(intent: OrderIntent) -> Decimal:
    with localcontext(FINANCIAL_CONTEXT):
        return stored(
            intent.quantity
            * (
                intent.limit_price
                - intent.protective_limit
                + intent.fee_bound * (intent.limit_price + intent.protective_limit)
                + intent.allowance_per_unit
            )
        )


def reasons(
    state: PortfolioSnapshot,
    intent: OrderIntent,
    rules: InstrumentRules,
    policy: RiskPolicy,
    at: datetime,
) -> tuple[str, ...]:
    denied: list[str] = []
    if not state.ready or entries_paused(state, at):
        denied.append("not_ready_or_halted")
    if any(o.state in UNCERTAIN for o in state.orders):
        denied.append("uncertain_command")
    if (
        rules.symbol != intent.symbol
        or rules.status != "online"
        or fingerprint(rules.model_dump(mode="json")) != intent.rules_hash
    ):
        denied.append("rules_changed_or_offline")
    if not intent.created_at <= at < intent.expires_at:
        denied.append("expired_intent")
    if intent.purpose != "entry" or intent.side != "buy":
        denied.append("not_an_entry")
    if (
        intent.limit_price - intent.stop_trigger < intent.limit_price * Decimal("0.05")
        or intent.fee_bound < Decimal("0.008")
        or intent.allowance_per_unit < intent.limit_price * Decimal("0.002")
    ):
        denied.append("stop_or_cost_floor")
    if (
        intent.quantity % rules.quantity_increment
        or intent.limit_price % rules.price_increment
        or intent.protective_limit % rules.price_increment
        or intent.stop_trigger % rules.price_increment
    ):
        denied.append("increments")
    if (
        intent.quantity < rules.minimum_quantity
        or intent.quantity * min(intent.limit_price, intent.protective_limit) < rules.minimum_cost
    ):
        denied.append("entry_or_exit_minimum")
    asset: Asset = "BTC" if intent.symbol == "BTC/GBP" else "ETH"
    if state.quantity(asset) or any(
        o.intent.symbol == intent.symbol and o.intent.side == "buy" and o.state not in TERMINAL
        for o in state.orders
    ):
        denied.append("no_pyramiding")
    active: set[str] = {b.asset for b in state.balances if b.asset != "GBP" and b.quantity}
    active |= {
        o.intent.symbol.split("/")[0]
        for o in state.orders
        if o.intent.side == "buy" and o.state not in TERMINAL
    }
    if len(active) >= policy.max_positions:
        denied.append("position_count")
    try:
        with localcontext(FINANCIAL_CONTEXT):
            equity = nav(state, at)
            value = intent.quantity * intent.limit_price
            loss = planned_loss(intent)
            if equity <= 0:
                denied.append("nonpositive_equity")
            if value * (1 + intent.fee_bound) > free_cash(state):
                denied.append("cash")
            if (
                value > equity * policy.asset_exposure
                or market_exposure(state, at) + value > equity * policy.total_exposure
            ):
                denied.append("exposure")
            if (
                loss > equity * policy.planned_risk_per_trade
                or episode_risk(state) + loss > equity * policy.aggregate_planned_risk
            ):
                denied.append("planned_risk")
    except ValueError:
        denied.append("unsafe_valuation")
    return tuple(denied)


def approve(
    state: PortfolioSnapshot,
    intent: OrderIntent,
    rules: InstrumentRules,
    policy: RiskPolicy,
    at: datetime,
) -> RiskDecision:
    denied = reasons(state, intent, rules, policy, at)
    return RiskDecision(
        id=uuid4(),
        intent_id=intent.id,
        allowed=not denied,
        reasons=denied,
        state_version=state.version + 1,
        policy_hash=fingerprint(policy.model_dump(mode="json")),
        intent_hash=fingerprint(intent.document()),
        rules_hash=intent.rules_hash,
        approved_at=at,
        expires_at=min(intent.expires_at, at + timedelta(seconds=2)),
        planned_loss=planned_loss(intent) if intent.purpose == "entry" else ZERO,
    )
