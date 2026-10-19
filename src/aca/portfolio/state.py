"""Flow-neutral portfolio economics and persistent risk thresholds."""

from datetime import datetime
from decimal import Decimal, localcontext

from aca.accounting.ledger import stored
from aca.domain.trading import TERMINAL, ZERO, PortfolioSnapshot
from aca.domain.values import FINANCIAL_CONTEXT, parse_decimal, utc_datetime
from aca.operations.config import RiskPolicy


def nav(state: PortfolioSnapshot, at: datetime, *, max_age: int = 5) -> Decimal:
    at = utc_datetime(at)
    marks = {m.asset: m for m in state.marks}
    with localcontext(FINANCIAL_CONTEXT):
        value = state.quantity("GBP")
        for balance in state.balances:
            if balance.asset == "GBP" or balance.quantity == 0:
                continue
            mark = marks.get(balance.asset)
            if mark is None or not 0 <= (at - mark.observed_at).total_seconds() <= max_age:
                raise ValueError("missing/stale GBP bid valuation")
            value += balance.quantity * mark.bid_gbp
        return stored(value)


def unit_price(state: PortfolioSnapshot, at: datetime) -> Decimal:
    if state.accounting_invalidated:
        raise ValueError("accounting correction invalidated investment units")
    value = nav(state, at)
    if not state.units:
        if value:
            raise ValueError("positive NAV without investment units")
        return state.last_unit_price
    with localcontext(FINANCIAL_CONTEXT):
        price = stored(value / state.units)
        if price <= 0:
            raise ValueError("non-positive unit price")
        return price


def flow_units(state: PortfolioSnapshot, amount: Decimal, at: datetime) -> Decimal:
    parse_decimal(amount)
    price = unit_price(state, at)
    with localcontext(FINANCIAL_CONTEXT):
        result = ZERO if amount == -nav(state, at) else stored(state.units + amount / price)
    if result < 0:
        raise ValueError("withdrawal exceeds portfolio units")
    return result


def reserved_cash(state: PortfolioSnapshot) -> Decimal:
    with localcontext(FINANCIAL_CONTEXT):
        return stored(
            sum(
                (
                    o.remaining * o.intent.limit_price * (1 + o.intent.fee_bound)
                    for o in state.orders
                    if o.intent.side == "buy" and o.state not in TERMINAL
                ),
                ZERO,
            )
        )


def free_cash(state: PortfolioSnapshot) -> Decimal:
    with localcontext(FINANCIAL_CONTEXT):
        return stored(state.quantity("GBP") - reserved_cash(state) - state.tax_reserve_gbp)


def market_exposure(state: PortfolioSnapshot, at: datetime) -> Decimal:
    with localcontext(FINANCIAL_CONTEXT):
        pending = sum(
            (
                o.remaining * o.intent.limit_price
                for o in state.orders
                if o.intent.side == "buy" and o.state not in TERMINAL
            ),
            ZERO,
        )
        return stored(nav(state, at) - state.quantity("GBP") + pending)


def episode_risk(state: PortfolioSnapshot) -> Decimal:
    """Conservative remaining held plus pending entry economics; no sell credit."""
    with localcontext(FINANCIAL_CONTEXT):
        total = ZERO
        for entry in (o for o in state.orders if o.intent.purpose == "entry"):
            # One episode per instrument. Once an exit closes inventory, the old
            # episode contributes zero. No new entry is admitted while held.
            sold = sum(
                (
                    o.filled
                    for o in state.orders
                    if o.intent.side == "sell" and o.intent.episode_id == entry.intent.episode_id
                ),
                ZERO,
            )
            held = max(ZERO, entry.filled - sold)
            pending = ZERO if entry.state in TERMINAL else entry.remaining
            i = entry.intent
            per_unit = (
                i.limit_price
                - i.protective_limit
                + i.fee_bound * (i.limit_price + i.protective_limit)
                + i.allowance_per_unit
            )
            total += (held + pending) * per_unit
        return stored(total)


def observe_risk(state: PortfolioSnapshot, at: datetime, policy: RiskPolicy) -> PortfolioSnapshot:
    """Equality breaches. Daily reset affects only the daily suspension."""
    at = utc_datetime(at)
    price = unit_price(state, at)
    high = max(state.high_water, price)
    inception = max(state.inception_high_water, price)
    kill = state.kill
    daily_price = state.daily_unit_price
    day = state.daily_day
    # Without an actual 00:00 mark, a first late observation must not invent a
    # midnight baseline. Remain suspended for that UTC day instead.
    if day != at.date():
        day = at.date()
        daily_price = price
        kill = kill.model_copy(update={"daily_day": None})
        if state.units and (at.hour or at.minute or at.second or at.microsecond):
            kill = kill.model_copy(
                update={"daily_day": day, "reason": "missing_utc_midnight_mark", "latched_at": at}
            )
    with localcontext(FINANCIAL_CONTEXT):
        dd = 1 - price / high
        daily = price / daily_price - 1
        updates: dict[str, object] = {}
        if dd >= policy.soft_drawdown and not kill.hard:
            updates.update(soft=True, reason="soft_drawdown", latched_at=kill.latched_at or at)
        if dd >= policy.hard_drawdown:
            updates.update(
                hard=True, soft=True, reason="hard_drawdown", latched_at=kill.latched_at or at
            )
        if daily <= -policy.daily_loss:
            updates.update(daily_day=day, latched_at=kill.latched_at or at)
            if not (kill.hard or kill.soft or updates.get("soft")):
                updates.update(reason="daily_loss")
        kill = type(kill).restore(kill.model_copy(update=updates).document())
    return state.model_copy(
        update={
            "kill": kill,
            "high_water": high,
            "inception_high_water": inception,
            "daily_day": day,
            "daily_unit_price": daily_price,
            "last_unit_price": price,
        }
    )


def entries_paused(state: PortfolioSnapshot, at: datetime) -> bool:
    return bool(
        state.accounting_invalidated
        or state.kill.soft
        or state.kill.hard
        or state.kill.daily_day == at.date()
        or state.incident
    )
